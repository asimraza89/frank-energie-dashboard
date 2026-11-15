import asyncio
from datetime import UTC, date, datetime, timedelta

from frankdash import client
from frankdash.mock import MockFrankEnergie, SITE_REF
from frankdash.storage import Store
from frankdash.sync import day_is_final, month_is_final, plan, sync

NOW = datetime(2026, 11, 15, 19, 0, tzinfo=UTC)  # 20:00 in Amsterdam, tomorrow's prices are out
TODAY = date(2026, 11, 15)


def _run(store, now, history_start=date(2025, 10, 1), detail_days=30):
    fe = MockFrankEnergie(now=now)
    asyncio.run(client.login(fe, "demo@example.com", "pw"))
    report = asyncio.run(sync(fe, store, SITE_REF, history_start, detail_days, today=now.date()))
    return fe, report


def test_finality_rules():
    assert not month_is_final("2026-11", TODAY)
    assert not month_is_final("2026-10", date(2026, 11, 7))
    assert month_is_final("2026-10", date(2026, 11, 8))
    assert not day_is_final(TODAY - timedelta(1), TODAY, 24)  # too fresh
    assert not day_is_final(TODAY - timedelta(5), TODAY, 20)  # incomplete
    assert day_is_final(TODAY - timedelta(5), TODAY, 24)
    assert day_is_final(TODAY - timedelta(12), TODAY, 0)  # gave up waiting


def test_plan_skips_final_items():
    state = {("day_usage", "2026-11-01"): True, ("day_usage", "2026-11-14"): False, ("month_usage", "2026-01"): True}
    tasks = plan(TODAY, date(2026, 1, 1), 30, state)
    keys = {(t.kind, t.key) for t in tasks}
    assert ("day_usage", "2026-11-01") not in keys
    assert ("day_usage", "2026-11-14") in keys  # not final -> refetch
    assert ("month_usage", "2026-01") not in keys
    assert ("day_prices", "2026-11-16") in keys  # tomorrow
    assert ("day_usage", "2026-11-15") not in keys  # never today


def test_second_sync_only_fetches_recent(tmp_path):
    store = Store(tmp_path / "data.sqlite")
    fe1, r1 = _run(store, NOW)
    assert r1.failed == 0 and r1.stopped_early is None
    first_calls = sum(fe1.calls.values())
    assert fe1.calls["PeriodUsageAndCosts"] == 30 + 14  # 30 days + 14 months

    fe2, r2 = _run(store, NOW)
    # Only the last few unsettled days and the open months are fetched again.
    assert fe2.calls["PeriodUsageAndCosts"] <= 6
    assert fe2.calls["MarketPrices"] == 0  # today and tomorrow are complete
    assert sum(fe2.calls.values()) < first_calls / 5

    # Next day: one new day of usage, plus the day after tomorrow's prices.
    fe3, r3 = _run(store, NOW + timedelta(days=1))
    assert fe3.calls["PeriodUsageAndCosts"] <= 7
    assert fe3.calls["MarketPrices"] == 1


def test_refresh_replaces_rows_instead_of_duplicating(tmp_path):
    store = Store(tmp_path / "data.sqlite")
    _run(store, NOW, detail_days=5)
    n1 = len(store.hourly_usage(SITE_REF))
    _run(store, NOW, detail_days=5)
    assert len(store.hourly_usage(SITE_REF)) == n1


def test_tomorrow_prices_not_published_yet(tmp_path):
    store = Store(tmp_path / "data.sqlite")
    morning = datetime(2026, 11, 15, 8, 0, tzinfo=UTC)
    _run(store, morning, detail_days=3)
    state = store.fetch_state(SITE_REF)
    assert state[("day_prices", "2026-11-15")] is True
    assert ("day_prices", "2026-11-16") not in state  # will be asked again later
