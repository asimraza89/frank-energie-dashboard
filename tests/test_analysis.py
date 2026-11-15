import asyncio
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from frankdash import analysis as an
from frankdash import client, weather
from frankdash.config import TZ
from frankdash.mock import SITE_REF, MockFrankEnergie, demo_weather
from frankdash.storage import Store
from frankdash.sync import sync

NOW = datetime(2026, 11, 15, 19, 0, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture(scope="module")
def frames(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("db") / "data.sqlite")
    fe = MockFrankEnergie(now=NOW)
    asyncio.run(client.login(fe, "demo@example.com", "pw"))
    asyncio.run(sync(fe, store, SITE_REF, date(2024, 11, 1), 120, today=TODAY))
    weather.sync_weather(store, "Utrecht / De Bilt", date(2024, 11, 1), TODAY - timedelta(6), fetch=demo_weather)
    hourly = an.to_hourly(store.hourly_usage(SITE_REF))
    month_slots = store.daily_usage(SITE_REF)
    return {
        "hourly": hourly,
        "daily": an.daily_totals(month_slots, hourly),
        "monthly": an.monthly_totals(month_slots, hourly),
        "prices": store.prices(SITE_REF),
        "weather": store.weather(weather.location_key("Utrecht / De Bilt")),
    }


def test_monthly_matches_daily(frames):
    m = frames["monthly"]
    d = frames["daily"]
    oct_m = m[(m.energy == "electricity") & (m.year == 2026) & (m.month == 10)]["usage"].sum()
    oct_d = d[(d.energy == "electricity") & (pd.to_datetime(d.day).dt.strftime("%Y-%m") == "2026-10")]["usage"].sum()
    assert oct_m == pytest.approx(oct_d, rel=1e-3)
    t = an.yoy_table(m, "electricity", "usage")
    assert {2025, 2026} <= set(t.columns)


def test_hourly_handles_dst(frames):
    h = frames["hourly"]
    day = h[(h.energy == "electricity") & (h.hour.dt.date == date(2026, 10, 25))]
    assert len(day) == 25  # clocks went back


def test_top_days(frames):
    t = an.top_days(frames["daily"], "electricity", "costs", since=TODAY - timedelta(365))
    assert len(t) == 5 and t["costs"].is_monotonic_decreasing


def test_period_vs_last_year(frames):
    r = an.period_vs_last_year(frames["daily"], "gas", date(2026, 11, 1), date(2026, 11, 14))
    assert r["usage"] > 0 and r["usage_last"] > 0


def test_price_analyses(frames):
    up = an.usage_with_prices(frames["hourly"], frames["prices"])
    assert set(up["band"]) == {"expensive", "cheap", "normal"}
    assert (up.groupby("day")["band"].apply(lambda b: (b == "expensive").sum()) == 6).all()
    t = an.timing_by_month(up)
    assert ((t["share_expensive"] > 0) & (t["share_expensive"] < 1)).all()
    s0 = an.shift_savings(up, 0.0)
    s3 = an.shift_savings(up, 0.3)
    assert s0["savings"] == 0 and s3["savings"] > 0


def test_baseload(frames):
    bl = an.baseload(frames["hourly"], frames["prices"])
    assert 60 < bl["watts"].iloc[-1] < 250  # mock always-on is ~140 W


def test_best_windows():
    start = datetime(2026, 11, 16, 0, tzinfo=TZ)
    prices = [0.30, 0.25, 0.10, 0.12, 0.40, 0.11, 0.35]
    up = pd.DataFrame(
        {
            "start": [pd.Timestamp(start + timedelta(hours=i)) for i in range(7)],
            "end": [pd.Timestamp(start + timedelta(hours=i + 1)) for i in range(7)],
            "price": prices,
        }
    )
    w = an.best_windows(up, [1, 2]).set_index("hours")
    assert w.loc[1, "start"].hour == 2
    assert w.loc[2, "start"].hour == 2 and w.loc[2, "end"].hour == 4
    assert w.loc[2, "avg_price"] == pytest.approx(0.11)


def test_gas_vs_temperature(frames):
    df, fit = an.gas_vs_temperature(frames["daily"], frames["weather"])
    assert fit is not None
    assert 0.2 < fit["m3_per_hdd"] < 0.5  # mock uses ~0.33 m³ per degree day


def test_feed_in(frames):
    fi = an.feed_in_summary(frames["monthly"])
    summer = fi[fi.month_label == "2025-07"].iloc[0]
    assert summer["usage_fed"] > 50 and summer["eur_per_kwh_fed"] < summer["eur_per_kwh_used"]
