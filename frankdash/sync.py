"""Incremental sync: work out what is missing, fetch only that.

Rules (all dates in Europe/Amsterdam):

* Monthly usage (daily totals, one request per month) covers the whole history.
  A month is final once it has been over for 7 days.
* Hourly usage (one request per day) covers the last ``detail_days`` days up to
  yesterday. A day is final once it is at least 3 days old *and* has a full
  day of electricity slots, or once it is 10 days old (whatever arrived by
  then is what Frank has).
* Prices are fetched for the same days plus today and tomorrow. A past day is
  final as soon as it is stored. Today and tomorrow are final once a full day
  of prices is stored (tomorrow's prices appear in the afternoon).

Final items are never requested again, so a second sync on the same day makes
only a handful of requests.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from python_frank_energie import FrankEnergie
from python_frank_energie.exceptions import AuthException, RateLimitException

from . import client
from .config import MAX_CONCURRENCY, TZ
from .storage import Store

_LOGGER = logging.getLogger(__name__)

MONTH_SETTLE_DAYS = 7
DAY_SETTLE_DAYS = 3
DAY_GIVE_UP_DAYS = 10
FULL_DAY_HOURS = 23  # DST days have 23 hours


@dataclass(frozen=True)
class Task:
    kind: str  # 'month_usage' | 'day_usage' | 'day_prices'
    key: str  # 'YYYY-MM' or 'YYYY-MM-DD'


@dataclass
class SyncReport:
    planned: int = 0
    done: int = 0
    failed: int = 0
    stopped_early: str | None = None
    errors: list[str] = field(default_factory=list)


def _months(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(n) for n in range((end - start).days + 1)]


def month_is_final(month: str, today: date) -> bool:
    y, m = map(int, month.split("-"))
    next_month = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return today >= next_month + timedelta(MONTH_SETTLE_DAYS)


def day_is_final(day: date, today: date, hours_covered: float) -> bool:
    age = (today - day).days
    if age >= DAY_GIVE_UP_DAYS:
        return True
    return age >= DAY_SETTLE_DAYS and hours_covered >= FULL_DAY_HOURS


def plan(
    today: date,
    history_start: date,
    detail_days: int,
    state: dict[tuple[str, str], bool],
) -> list[Task]:
    """Everything that is not yet stored as final, newest first."""
    tasks: list[Task] = []
    detail_start = max(history_start, today - timedelta(detail_days))
    yesterday = today - timedelta(1)

    for d in reversed(_days(detail_start, yesterday)):
        if not state.get(("day_usage", d.isoformat()), False):
            tasks.append(Task("day_usage", d.isoformat()))
    for d in reversed(_days(detail_start, today + timedelta(1))):
        if not state.get(("day_prices", d.isoformat()), False):
            tasks.append(Task("day_prices", d.isoformat()))
    for m in reversed(_months(history_start, today)):
        if not state.get(("month_usage", m), False):
            tasks.append(Task("month_usage", m))
    return tasks


def _hours_covered(slots: list[dict], energy: str = "electricity") -> float:
    return sum((s["end"] - s["start"]).total_seconds() for s in slots if s["energy"] == energy) / 3600


async def _run_task(fe: FrankEnergie, store: Store, site: str, task: Task, today: date) -> None:
    if task.kind == "month_usage":
        slots = await client.usage(fe, site, task.key)
        store.save_usage(site, task.kind, task.key, slots, month_is_final(task.key, today))
    elif task.kind == "day_usage":
        slots = await client.usage(fe, site, task.key)
        day = date.fromisoformat(task.key)
        # Sites without electricity (gas only) judge completeness on gas.
        energy = "electricity" if any(s["energy"] == "electricity" for s in slots) else "gas"
        store.save_usage(site, task.kind, task.key, slots, day_is_final(day, today, _hours_covered(slots, energy)))
    elif task.kind == "day_prices":
        day = date.fromisoformat(task.key)
        rows = await client.prices(fe, site, day)
        full = _hours_covered(rows) >= FULL_DAY_HOURS
        final = full or (day < today and (today - day).days >= DAY_GIVE_UP_DAYS)
        if rows or final:
            store.save_prices(site, task.key, rows, final)
    else:
        raise ValueError(task.kind)


async def sync(
    fe: FrankEnergie,
    store: Store,
    site: str,
    history_start: date,
    detail_days: int,
    today: date | None = None,
    progress: Callable[[int, int], None] | None = None,
    concurrency: int = MAX_CONCURRENCY,
) -> SyncReport:
    today = today or datetime.now(TZ).date()
    tasks = plan(today, history_start, detail_days, store.fetch_state(site))
    report = SyncReport(planned=len(tasks))
    if progress:
        progress(0, len(tasks))
    sem = asyncio.Semaphore(concurrency)
    stop = asyncio.Event()

    async def worker(task: Task) -> None:
        if stop.is_set():
            return
        async with sem:
            if stop.is_set():
                return
            try:
                await _run_task(fe, store, site, task, today)
                report.done += 1
            except (AuthException, RateLimitException) as err:
                # No point hammering the API; keep what we have and stop.
                report.stopped_early = f"{type(err).__name__}: {err}"
                stop.set()
            except Exception as err:  # one bad day must not kill the sync
                report.failed += 1
                report.errors.append(f"{task.kind} {task.key}: {err}")
                _LOGGER.warning("Fetch failed for %s %s: %s", task.kind, task.key, err)
            if progress:
                progress(report.done + report.failed, len(tasks))

    await asyncio.gather(*(worker(t) for t in tasks))
    store.set_meta("last_sync", datetime.now(TZ).isoformat(timespec="seconds"))
    return report
