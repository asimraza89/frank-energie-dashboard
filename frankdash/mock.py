"""Demo / test client: a FrankEnergie whose network layer returns synthetic data.

Only ``_query`` is replaced, so the real package code (query building, model
parsing, token handling) still runs. The numbers are made up but shaped like a
Dutch household with a dynamic contract, gas heating and a few solar panels.
"""

from __future__ import annotations

import math
import random
import zlib
from collections import Counter
from datetime import UTC, date, datetime, time, timedelta

import jwt
from python_frank_energie import FrankEnergie

from .config import TZ

SITE_REF = "1234AB 56"
DELIVERY_START = date(2023, 6, 1)


def _token(kind: str) -> str:
    exp = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
    return jwt.encode({"sub": "demo", "kind": kind, "exp": exp}, "demo-only-signing-key-not-a-real-secret-000", algorithm="HS256")


def _z(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _local_hours(day: date) -> list[datetime]:
    """Every hour of a local day, in UTC (23 or 25 on DST change days)."""
    start = datetime.combine(day, time(0), TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(1), time(0), TZ).astimezone(UTC)
    n = int((end - start).total_seconds() // 3600)
    return [start + timedelta(hours=i) for i in range(n)]


class Synthetic:
    """Deterministic synthetic household (same date -> same numbers)."""

    def __init__(self, seed: int = 7):
        self.seed = seed

    def _rng(self, *key) -> random.Random:
        return random.Random(zlib.crc32(repr((self.seed, *key)).encode()))

    def temp(self, day: date) -> float:
        doy = day.timetuple().tm_yday
        return 10.5 - 7.5 * math.cos(2 * math.pi * (doy - 20) / 365) + self._rng("t", day).gauss(0, 2.5)

    def market_price(self, ts: datetime) -> float:
        local = ts.astimezone(TZ)
        h, doy = local.hour, local.timetuple().tm_yday
        summer = math.sin(math.pi * (doy - 80) / 365) if 80 <= doy <= 445 else 0
        winter = 1 - max(summer, 0)
        p = 0.07 + 0.03 * winter
        p += 0.06 * math.exp(-((h - 19) ** 2) / 4) + 0.03 * math.exp(-((h - 8) ** 2) / 2)
        p -= 0.09 * max(summer, 0) * math.exp(-((h - 13) ** 2) / 6)
        p -= 0.02 * (h < 5)
        return round(p + self._rng("p", local.date(), h).gauss(0, 0.012), 5)

    def gas_market_price(self, day: date) -> float:
        return round(0.33 + 0.05 * math.cos(2 * math.pi * day.timetuple().tm_yday / 365) + self._rng("g", day).gauss(0, 0.01), 5)

    @staticmethod
    def all_in(market: float) -> float:
        return market * 1.21 + 0.02 * 1.21 + 0.1108

    @staticmethod
    def gas_all_in(market: float) -> float:
        return market * 1.21 + 0.06 + 0.70

    def hour(self, ts: datetime) -> tuple[float, float, float]:
        """(grid electricity kWh, feed-in kWh, gas m³) for one hour."""
        local = ts.astimezone(TZ)
        d, h = local.date(), local.hour
        r = self._rng("u", d, h)
        weekend = local.weekday() >= 5
        doy = d.timetuple().tm_yday
        use = 0.13 + 0.01 * (d >= date(2025, 1, 1))  # always-on, crept up a little
        use += 0.25 * math.exp(-((h - 7.5) ** 2) / 1.5)
        use += 0.55 * math.exp(-((h - 19) ** 2) / 4)
        use += (0.25 if weekend else 0.05) * math.exp(-((h - 13) ** 2) / 8)
        if self._rng("wash", d).random() < 0.35 and h == self._rng("washh", d).choice([9, 18, 19, 20, 21]):
            use += 1.6  # washing machine / dryer
        if self._rng("ev", d).random() < 0.18 and 18 <= h <= 21:
            use += 3.2  # occasional EV top-up straight after work
        use *= r.uniform(0.85, 1.15)
        sun = max(0.0, math.sin(math.pi * (h - 6) / 14)) if 6 <= h <= 20 else 0
        season = 0.25 + 0.75 * max(0.0, math.sin(math.pi * (doy - 60) / 245)) if 60 <= doy <= 305 else 0.2
        solar = 2.4 * sun * season * self._rng("cloud", d).uniform(0.3, 1.0)
        grid = max(0.0, use - solar)
        feed = max(0.0, solar - use)
        hdd = max(0.0, 18 - self.temp(d))
        heat_shape = [0.2, 0.15, 0.15, 0.15, 0.2, 0.6, 1.6, 1.8, 1.3, 1.0, 0.9, 0.9, 0.9, 0.9, 0.9, 1.0, 1.2, 1.5, 1.6, 1.5, 1.3, 1.1, 0.6, 0.3]
        gas = (0.33 * hdd * heat_shape[h] / sum(heat_shape)) + (0.08 if h in (7, 8, 21) else 0.01)
        return round(grid, 4), round(feed, 4), round(gas, 4)


class MockFrankEnergie(FrankEnergie):
    """FrankEnergie with synthetic responses. ``now`` fixes the clock for tests."""

    def __init__(self, *args, now: datetime | None = None, synthetic: Synthetic | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.now = now
        self.data = synthetic or Synthetic()
        self.calls: Counter[str] = Counter()

    def _now(self) -> datetime:
        return (self.now or datetime.now(UTC)).astimezone(TZ)

    async def _ensure_session(self) -> None:  # no network
        return None

    async def close(self) -> None:
        return None

    async def _query(self, query, extra_headers=None, *, recover_hvac_errors=False):
        op = query.operation_name
        v = query.variables or {}
        self.calls[op] += 1
        if op == "Login":
            if not v.get("password"):
                return {"errors": [{"message": "Invalid credentials"}]}
            return {"data": {"login": {"authToken": _token("a"), "refreshToken": _token("r")}, "version": "demo"}}
        if op == "RenewToken":
            return {"data": {"renewToken": {"authToken": _token("a"), "refreshToken": _token("r")}}}
        if op == "UserSites":
            return {"data": {"userSites": [self._site()]}}
        if op == "PeriodUsageAndCosts":
            return self._usage(v["date"])
        if op == "MarketPrices":
            return self._prices(date.fromisoformat(v["date"]))
        if op == "UserCountry":
            return {"data": {"me": {"countryCode": "NL"}}}
        return {"errors": [{"message": f"mock has no {op}"}]}

    def _site(self) -> dict:
        return {
            "address": {"addressFormatted": ["Demostraat 1", "1234 AB Demodorp"]},
            "addressHasMultipleSites": False,
            "deliveryStartDate": DELIVERY_START.isoformat(),
            "deliveryEndDate": None,
            "firstMeterReadingDate": DELIVERY_START.isoformat(),
            "lastMeterReadingDate": (self._now().date() - timedelta(1)).isoformat(),
            "propositionType": "DYNAMIC",
            "reference": SITE_REF,
            "segments": ["ELECTRICITY", "GAS"],
            "status": "IN_DELIVERY",
        }

    # ---- usage ---------------------------------------------------------------
    def _usage(self, period: str) -> dict:
        today = self._now().date()
        if len(period) == 10:  # one day, hourly
            day = date.fromisoformat(period)
            hours = _local_hours(day)
            if day >= today - timedelta(1):
                hours = hours[:20]  # yesterday's data is not complete yet
            if day >= today:
                hours = []
            items = {"electricity": [], "gas": [], "feedIn": []}
            for ts in hours:
                grid, feed, gas = self.data.hour(ts)
                mp = self.data.market_price(ts)
                gp = self.data.gas_all_in(self.data.gas_market_price(day))
                base = {"date": day.isoformat(), "from": _z(ts), "till": _z(ts + timedelta(hours=1))}
                items["electricity"].append({**base, "usage": grid, "costs": round(grid * self.data.all_in(mp), 5), "unit": "KWH"})
                items["feedIn"].append({**base, "usage": feed, "costs": round(-feed * mp * 1.21, 5), "unit": "KWH"})
                items["gas"].append({**base, "usage": gas, "costs": round(gas * gp, 5), "unit": "M3"})
        else:  # one month, daily
            y, m = map(int, period.split("-"))
            day = date(y, m, 1)
            items = {"electricity": [], "gas": [], "feedIn": []}
            while day.month == m and day < today:
                hours = _local_hours(day)
                if day >= today - timedelta(1):
                    hours = hours[:20]
                tot = {"electricity": [0.0, 0.0], "gas": [0.0, 0.0], "feedIn": [0.0, 0.0]}
                gp = self.data.gas_all_in(self.data.gas_market_price(day))
                for ts in hours:
                    grid, feed, gas = self.data.hour(ts)
                    mp = self.data.market_price(ts)
                    tot["electricity"][0] += grid
                    tot["electricity"][1] += grid * self.data.all_in(mp)
                    tot["feedIn"][0] += feed
                    tot["feedIn"][1] -= feed * mp * 1.21
                    tot["gas"][0] += gas
                    tot["gas"][1] += gas * gp
                start = datetime.combine(day, time(0), TZ)
                base = {"date": day.isoformat(), "from": _z(start), "till": _z(datetime.combine(day + timedelta(1), time(0), TZ))}
                for k, unit in (("electricity", "KWH"), ("gas", "M3"), ("feedIn", "KWH")):
                    items[k].append({**base, "usage": round(tot[k][0], 4), "costs": round(tot[k][1], 4), "unit": unit})
                day += timedelta(1)

        def cat(key: str, unit: str) -> dict:
            its = items[key]
            return {
                "usageTotal": round(sum(i["usage"] for i in its), 4),
                "costsTotal": round(sum(i["costs"] for i in its), 4),
                "unit": unit,
                "items": its,
            }

        return {
            "data": {
                "periodUsageAndCosts": {
                    "_id": f"{SITE_REF}:{period}",
                    "electricity": cat("electricity", "KWH"),
                    "gas": cat("gas", "M3"),
                    "feedIn": cat("feedIn", "KWH"),
                }
            }
        }

    # ---- prices ----------------------------------------------------------------
    def _prices(self, day: date) -> dict:
        now = self._now()
        published_until = now.date() + timedelta(1) if now.hour >= 15 else now.date()
        if day > published_until:
            return {"data": None, "errors": [{"message": f"No marketprices found for {day}"}]}
        elec, gas = [], []
        gm = self.data.gas_market_price(day)
        for ts in _local_hours(day):
            mp = self.data.market_price(ts)
            row = {"date": day.isoformat(), "from": _z(ts), "till": _z(ts + timedelta(hours=1)), "resolution": "PT60M", "perUnit": "KWH"}
            elec.append(
                {**row, "marketPrice": mp, "marketPriceTax": round(mp * 0.21, 6), "sourcingMarkupPrice": 0.0242,
                 "energyTaxPrice": 0.1108, "marketPricePlus": mp, "allInPrice": self.data.all_in(mp)}
            )
            gas.append(
                {**row, "perUnit": "M3", "marketPrice": gm, "marketPriceTax": round(gm * 0.21, 6),
                 "sourcingMarkupPrice": 0.06, "energyTaxPrice": 0.70, "marketPricePlus": gm}
            )
        return {"data": {"customerMarketPrices": {"id": str(day), "electricityPrices": elec, "gasPrices": gas}}}


def demo_weather(lat: float, lon: float, start: date, end: date, synthetic: Synthetic | None = None) -> dict[str, float]:
    s = synthetic or Synthetic()
    return {(start + timedelta(n)).isoformat(): round(s.temp(start + timedelta(n)), 1) for n in range((end - start).days + 1)}
