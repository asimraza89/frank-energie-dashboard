"""Pure pandas analyses. No I/O here, so everything is easy to test.

Frames coming from the store have local-time ``start``/``end`` columns.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from .config import TZ

EXPENSIVE_HOURS = 6  # the 6 priciest hours of each day count as "expensive"
CHEAP_HOURS = 6
HDD_BASE = 18.0  # Dutch heating degree days use 18 °C
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

EMPTY_HOURLY = pd.DataFrame(columns=["energy", "hour", "usage", "costs"])


def _floor_hour(ts: pd.Series) -> pd.Series:
    # Floor in UTC so the repeated hour at the end of DST is not ambiguous.
    return ts.dt.tz_convert("UTC").dt.floor("h").dt.tz_convert(TZ)


# --------------------------------------------------------------------------
# Shaping
# --------------------------------------------------------------------------
def to_hourly(slots: pd.DataFrame) -> pd.DataFrame:
    """Hourly usage/cost per energy type (quarter-hours are summed)."""
    if slots.empty:
        return EMPTY_HOURLY.copy()
    df = slots.assign(hour=_floor_hour(slots["start"]))
    return df.groupby(["energy", "hour"], as_index=False)[["usage", "costs"]].sum()


def hourly_prices(prices: pd.DataFrame, energy: str = "electricity", column: str = "price") -> pd.DataFrame:
    """Mean price per hour (quarter-hour prices are averaged), returned as ``price``."""
    p = prices[prices["energy"] == energy]
    if p.empty:
        return pd.DataFrame(columns=["hour", "price"])
    p = p.assign(hour=_floor_hour(p["start"]), price=p[column])
    return p.groupby("hour", as_index=False)["price"].mean()


def daily_totals(month_slots: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """One row per (energy, day). Hourly data wins where both exist."""
    frames = []
    if not hourly.empty:
        h = hourly.assign(day=hourly["hour"].dt.date)
        frames.append(h.groupby(["energy", "day"], as_index=False)[["usage", "costs"]].sum())
    if not month_slots.empty:
        m = month_slots.assign(day=month_slots["start"].dt.date)
        m = m.groupby(["energy", "day"], as_index=False)[["usage", "costs"]].sum()
        if frames:
            have = set(zip(frames[0]["energy"], frames[0]["day"]))
            m = m[[(e, d) not in have for e, d in zip(m["energy"], m["day"])]]
        frames.append(m)
    if not frames:
        return pd.DataFrame(columns=["energy", "day", "usage", "costs"])
    return pd.concat(frames, ignore_index=True).sort_values(["energy", "day"]).reset_index(drop=True)


def monthly_totals(month_slots: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """Monthly usage/cost per energy type, from the month fetches (fallback: hourly)."""
    src = month_slots
    if src.empty and not hourly.empty:
        src = hourly.rename(columns={"hour": "start"})
    if src.empty:
        return pd.DataFrame(columns=["energy", "year", "month", "usage", "costs"])
    df = src.assign(year=src["start"].dt.year, month=src["start"].dt.month)
    return df.groupby(["energy", "year", "month"], as_index=False)[["usage", "costs"]].sum()


# --------------------------------------------------------------------------
# Year over year and top days
# --------------------------------------------------------------------------
def yoy_table(monthly: pd.DataFrame, energy: str, metric: str) -> pd.DataFrame:
    """Months as rows, years as columns."""
    df = monthly[monthly["energy"] == energy]
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="month", columns="year", values=metric, aggfunc="sum").sort_index()


def _same_days_last_year(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29 February
        return d.replace(year=d.year - 1, day=28)


def period_vs_last_year(daily: pd.DataFrame, energy: str, start: date, end: date) -> dict | None:
    """Totals for start..end (inclusive) and for the same dates one year earlier."""
    df = daily[daily["energy"] == energy]
    if end < start:
        return None
    now = df[(df["day"] >= start) & (df["day"] <= end)]
    then = df[(df["day"] >= _same_days_last_year(start)) & (df["day"] <= _same_days_last_year(end))]
    if now.empty:
        return None
    return {
        "usage": now["usage"].sum(),
        "costs": now["costs"].sum(),
        "usage_last": then["usage"].sum() if not then.empty else None,
        "costs_last": then["costs"].sum() if not then.empty else None,
    }


def top_days(daily: pd.DataFrame, energy: str, metric: str, since: date | None = None, n: int = 5) -> pd.DataFrame:
    df = daily[daily["energy"] == energy]
    if since is not None:
        df = df[df["day"] >= since]
    return df.nlargest(n, metric)[["day", "usage", "costs"]].reset_index(drop=True)


# --------------------------------------------------------------------------
# When do we use energy?
# --------------------------------------------------------------------------
def recent(hourly: pd.DataFrame, days: int, today: date) -> pd.DataFrame:
    cutoff = pd.Timestamp(today - timedelta(days), tz=TZ)
    return hourly[hourly["hour"] >= cutoff]


def hour_profile(hourly: pd.DataFrame, energy: str = "electricity") -> pd.DataFrame:
    """Average usage per hour of day, split weekday/weekend."""
    df = hourly[hourly["energy"] == energy]
    if df.empty:
        return pd.DataFrame(columns=["hour_of_day", "daytype", "usage"])
    df = df.assign(
        hour_of_day=df["hour"].dt.hour,
        daytype=np.where(df["hour"].dt.dayofweek >= 5, "Weekend", "Weekday"),
    )
    both = df.assign(daytype="All days")
    out = pd.concat([df, both]).groupby(["daytype", "hour_of_day"], as_index=False)["usage"].mean()
    return out


def weekday_hour_heatmap(hourly: pd.DataFrame, energy: str = "electricity") -> pd.DataFrame:
    df = hourly[hourly["energy"] == energy]
    if df.empty:
        return pd.DataFrame()
    df = df.assign(dow=df["hour"].dt.dayofweek, hod=df["hour"].dt.hour)
    grid = df.pivot_table(index="dow", columns="hod", values="usage", aggfunc="mean")
    grid.index = [DAY_NAMES[i] for i in grid.index]
    return grid


def peak_hours(hourly: pd.DataFrame, energy: str = "electricity", n: int = 3) -> pd.DataFrame:
    prof = hour_profile(hourly, energy)
    prof = prof[prof["daytype"] == "All days"]
    return prof.nlargest(n, "usage")[["hour_of_day", "usage"]].reset_index(drop=True)


def weekday_vs_weekend(daily: pd.DataFrame, energy: str, since: date) -> pd.DataFrame:
    df = daily[(daily["energy"] == energy) & (daily["day"] >= since)]
    if df.empty:
        return pd.DataFrame(columns=["daytype", "usage", "costs"])
    dow = pd.to_datetime(df["day"]).dt.dayofweek
    df = df.assign(daytype=np.where(dow >= 5, "Weekend day", "Weekday"))
    return df.groupby("daytype", as_index=False)[["usage", "costs"]].mean()


# --------------------------------------------------------------------------
# Prices: timing, shifting, upcoming hours
# --------------------------------------------------------------------------
def usage_with_prices(hourly: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Hourly electricity usage joined with that hour's all-in price and its rank within the day."""
    use = hourly[hourly["energy"] == "electricity"][["hour", "usage", "costs"]]
    hp = hourly_prices(prices)
    if use.empty or hp.empty:
        return pd.DataFrame(columns=["hour", "usage", "costs", "price", "day", "band"])
    df = hp.merge(use, on="hour", how="left").fillna({"usage": 0.0, "costs": 0.0})
    df["day"] = df["hour"].dt.date
    # Only keep days for which we have usage (i.e. drop today/tomorrow's price-only rows).
    have_usage = set(use["hour"].dt.date)
    df = df[df["day"].isin(have_usage)]
    rank_desc = df.groupby("day")["price"].rank(method="first", ascending=False)
    rank_asc = df.groupby("day")["price"].rank(method="first", ascending=True)
    df["band"] = np.select([rank_desc <= EXPENSIVE_HOURS, rank_asc <= CHEAP_HOURS], ["expensive", "cheap"], "normal")
    return df.reset_index(drop=True)


def timing_by_month(up: pd.DataFrame) -> pd.DataFrame:
    """Your usage-weighted price vs the plain average price of the month.

    Negative ``timing_effect`` means your timing saved money compared with
    using the same energy evenly over the day.
    """
    if up.empty:
        return pd.DataFrame(columns=["month", "kwh", "your_price", "avg_price", "timing_effect", "share_expensive", "share_cheap"])
    df = up.assign(month=up["hour"].dt.strftime("%Y-%m"), weighted=up["usage"] * up["price"])
    g = df.groupby("month")
    out = pd.DataFrame(
        {
            "kwh": g["usage"].sum(),
            "your_price": g["weighted"].sum() / g["usage"].sum(),
            "avg_price": g["price"].mean(),
            "share_expensive": df[df["band"] == "expensive"].groupby("month")["usage"].sum() / g["usage"].sum(),
            "share_cheap": df[df["band"] == "cheap"].groupby("month")["usage"].sum() / g["usage"].sum(),
        }
    ).fillna(0.0)
    out["timing_effect"] = (out["your_price"] - out["avg_price"]) * out["kwh"]
    return out.reset_index()


def shift_savings(up: pd.DataFrame, flexible_share: float) -> dict:
    """What if ``flexible_share`` of the usage in each day's expensive hours ran in that day's cheap hours instead?"""
    if up.empty:
        return {"moved_kwh": 0.0, "savings": 0.0, "days": 0, "per_year": 0.0}
    exp = up[up["band"] == "expensive"]
    cheap = up[up["band"] == "cheap"].groupby("day")["price"].mean()
    per_day = exp.groupby("day").apply(
        lambda d: pd.Series({"kwh": d["usage"].sum(), "price": (d["usage"] * d["price"]).sum() / max(d["usage"].sum(), 1e-9)}),
        include_groups=False,
    )
    per_day = per_day.join(cheap.rename("cheap_price"), how="inner")
    moved = per_day["kwh"] * flexible_share
    savings = float((moved * (per_day["price"] - per_day["cheap_price"])).sum())
    days = len(per_day)
    return {
        "moved_kwh": float(moved.sum()),
        "savings": savings,
        "days": days,
        "per_year": savings / days * 365 if days else 0.0,
    }


def baseload(hourly: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """Always-on load: lowest hourly use between 00:00 and 05:00 each night, in watts, by month."""
    df = hourly[(hourly["energy"] == "electricity") & (hourly["hour"].dt.hour < 5)]
    if df.empty:
        return pd.DataFrame(columns=["month", "watts", "kwh_year", "cost_year"])
    nightly = df.groupby(df["hour"].dt.date)["usage"].min().rename("kwh")
    nightly.index = pd.to_datetime(nightly.index)
    by_month = nightly.groupby(nightly.index.strftime("%Y-%m")).median()
    hp = hourly_prices(prices)
    avg_price = float(hp["price"].mean()) if not hp.empty else np.nan
    out = pd.DataFrame({"month": by_month.index, "watts": by_month.values * 1000})
    out["kwh_year"] = out["watts"] / 1000 * 8760
    out["cost_year"] = out["kwh_year"] * avg_price
    return out


def upcoming_prices(prices: pd.DataFrame, now: datetime, energy: str = "electricity") -> pd.DataFrame:
    p = prices[(prices["energy"] == energy) & (prices["end"] > pd.Timestamp(now))]
    return p.sort_values("start")[["start", "end", "price"]].reset_index(drop=True)


def best_windows(upcoming: pd.DataFrame, hours: list[float]) -> pd.DataFrame:
    """Cheapest contiguous block of each length among the upcoming slots."""
    rows = []
    if upcoming.empty:
        return pd.DataFrame(columns=["hours", "start", "end", "avg_price", "vs_avg"])
    slot_h = (upcoming["end"] - upcoming["start"]).dt.total_seconds().median() / 3600
    prices = upcoming["price"].to_numpy()
    overall = prices.mean()
    for h in hours:
        n = max(1, int(round(h / slot_h)))
        if n > len(prices):
            continue
        sums = np.convolve(prices, np.ones(n), "valid")
        i = int(np.argmin(sums))
        rows.append(
            {
                "hours": h,
                "start": upcoming["start"].iloc[i],
                "end": upcoming["end"].iloc[i + n - 1],
                "avg_price": sums[i] / n,
                "vs_avg": sums[i] / n - overall,
            }
        )
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Gas vs weather, feed-in
# --------------------------------------------------------------------------
def gas_vs_temperature(daily: pd.DataFrame, weather: pd.DataFrame) -> tuple[pd.DataFrame, dict | None]:
    """Daily gas use against heating degree days, with a simple linear fit.

    Returns the joined frame and the fit: m³ per degree day (insulation /
    thermostat behaviour) and the base use per day (hot water, cooking).
    """
    gas = daily[daily["energy"] == "gas"][["day", "usage", "costs"]]
    if gas.empty or weather.empty:
        return pd.DataFrame(), None
    df = gas.merge(weather, on="day", how="inner").dropna()
    if len(df) < 20:
        return df, None
    df["hdd"] = (HDD_BASE - df["tmean"]).clip(lower=0)
    d = pd.to_datetime(df["day"])
    start_year = np.where(d.dt.month >= 7, d.dt.year, d.dt.year - 1)
    df["season"] = [f"{y}/{str(y + 1)[2:]}" for y in start_year]
    slope, intercept = np.polyfit(df["hdd"], df["usage"], 1)
    # Heating gas per degree day per season: subtract the base use (hot water,
    # cooking) first, otherwise mild autumn months look inefficient.
    heating = df[df["hdd"] > 0]
    seasons = (
        heating.groupby("season")
        .apply(lambda s: (s["usage"].sum() - intercept * len(s)) / s["hdd"].sum(), include_groups=False)
        .rename("m3_per_hdd")
        .reset_index()
    )
    return df, {"m3_per_hdd": slope, "base_m3_per_day": intercept, "seasons": seasons}


def feed_in_summary(monthly: pd.DataFrame) -> pd.DataFrame:
    fi = monthly[monthly["energy"] == "feed_in"]
    el = monthly[monthly["energy"] == "electricity"]
    if fi.empty:
        return pd.DataFrame()
    df = fi.merge(el, on=["year", "month"], suffixes=("_fed", "_used"), how="left")
    df["earned"] = df["costs_fed"].abs()
    df["eur_per_kwh_fed"] = df["earned"] / df["usage_fed"].replace(0, np.nan)
    df["eur_per_kwh_used"] = df["costs_used"] / df["usage_used"].replace(0, np.nan)
    df["month_label"] = df["year"].astype(str) + "-" + df["month"].astype(str).str.zfill(2)
    return df[["month_label", "usage_fed", "earned", "eur_per_kwh_fed", "eur_per_kwh_used"]]


def negative_price_feed_in(hourly: pd.DataFrame, prices: pd.DataFrame) -> dict:
    """Feed-in during hours when the wholesale (market) price was below zero."""
    fi = hourly[hourly["energy"] == "feed_in"][["hour", "usage"]]
    hp = hourly_prices(prices, column="market_price")
    if fi.empty or hp.empty:
        return {"hours": 0, "kwh": 0.0}
    df = fi.merge(hp, on="hour")
    neg = df[(df["price"] < 0) & (df["usage"] > 0)]
    return {"hours": int(len(neg)), "kwh": float(neg["usage"].sum())}
