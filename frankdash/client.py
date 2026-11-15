"""Thin wrapper around python-frank-energie.

It turns the package's model objects into plain dicts for the cache, and keeps
the login tokens in one place. The password is only passed to ``login`` and is
never stored.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from python_frank_energie import FrankEnergie
from python_frank_energie.exceptions import NoMarketPricesAvailableException, RequestException

ClientFactory = Callable[["Tokens | None"], FrankEnergie]


@dataclass
class Tokens:
    auth_token: str
    refresh_token: str

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"auth_token": self.auth_token, "refresh_token": self.refresh_token}))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: Path) -> Tokens | None:
        try:
            data = json.loads(path.read_text())
            return cls(data["auth_token"], data["refresh_token"])
        except (OSError, ValueError, KeyError):
            return None


def real_client(tokens: Tokens | None) -> FrankEnergie:
    if tokens is None:
        return FrankEnergie()
    return FrankEnergie(auth_token=tokens.auth_token, refresh_token=tokens.refresh_token)


def current_tokens(fe: FrankEnergie) -> Tokens | None:
    """Tokens may be renewed during a session; read the latest ones back."""
    auth = getattr(fe, "_auth", None)
    if auth is None or not auth.authToken:
        return None
    return Tokens(auth.authToken, auth.refreshToken)


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value}")
    return ts


async def login(fe: FrankEnergie, email: str, password: str) -> Tokens:
    auth = await fe.login(email, password)
    return Tokens(auth.authToken, auth.refreshToken)


def _addr(address) -> str | None:
    if address is None:
        return None
    parts = [getattr(address, a, None) for a in ("street", "houseNumber", "houseNumberAddition", "zipCode", "city")]
    return " ".join(str(p) for p in parts if p) or None


def _iso(value) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


async def site_info(fe: FrankEnergie) -> list[dict]:
    """All delivery sites on the account, the one currently in delivery first."""
    result = await fe.user_sites()
    sites = []
    for s in result.deliverySites or []:
        sites.append(
            {
                "reference": s.reference,
                "address": _addr(s.address) or s.reference,
                "status": str(s.status or "").upper(),
                "delivery_start": _iso(s.deliveryStartDate),
                "first_reading": _iso(s.firstMeterReadingDate),
                "last_reading": _iso(s.lastMeterReadingDate),
                "segments": list(s.segments or []),
            }
        )
    if not sites:  # older package versions: only the first site is parsed
        sites.append(
            {
                "reference": result.reference,
                "address": result.reference,
                "status": str(result.status or "").upper(),
                "delivery_start": result.deliveryStartDate,
                "first_reading": result.firstMeterReadingDate,
                "last_reading": result.lastMeterReadingDate,
                "segments": list(result.segments or []),
            }
        )
    sites.sort(key=lambda s: s["status"] not in ("IN_DELIVERY", "ACTIVE"))
    return sites


async def usage(fe: FrankEnergie, site: str, period: str) -> list[dict]:
    """Usage slots for a day ('YYYY-MM-DD', hourly) or a month ('YYYY-MM', daily)."""
    result = await fe.period_usage_and_costs(site, period)
    slots: list[dict] = []
    for energy, category in (("electricity", result.electricity), ("gas", result.gas), ("feed_in", result.feed_in)):
        if category is None:
            continue
        for item in category.items:
            slots.append(
                {
                    "energy": energy,
                    "start": _parse_ts(item.from_time),
                    "end": _parse_ts(item.till_time),
                    "usage": float(item.usage),
                    "costs": float(item.costs),
                    "unit": item.unit,
                }
            )
    return slots


async def prices(fe: FrankEnergie, site: str, day: date, country: str = "NL") -> list[dict]:
    """All-in customer prices for one day. Empty list when not published yet."""
    try:
        mp = await fe.user_prices(site, country, day)
    except NoMarketPricesAvailableException:
        return []
    except RequestException as err:
        if "No marketprices found" in str(err):
            return []
        raise
    rows = []
    for energy, data in (("electricity", mp.electricity), ("gas", mp.gas)):
        for p in data.all:
            rows.append(
                {
                    "energy": energy,
                    "start": p.date_from,
                    "end": p.date_till,
                    "price": float(p.total),
                    "market_price": float(p.market_price),
                }
            )
    return rows
