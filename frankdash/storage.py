"""Local SQLite cache.

Tables
------
fetch_log    one row per thing we fetched (a day of hourly usage, a month of
             daily usage, a day of prices) and whether it is *final*. Final
             rows are never fetched again; non-final rows are refreshed on the
             next sync.
usage        usage/cost slots. ``source`` tells which fetch produced the row,
             so a refresh can replace exactly the rows it owns.
prices       all-in price per slot (electricity and gas).
weather      daily mean outdoor temperature (optional, for gas analysis).
meta         small key/value store (site info, last sync time).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from .config import TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
    site TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'day_usage' | 'month_usage' | 'day_prices'
    key TEXT NOT NULL,             -- 'YYYY-MM-DD' or 'YYYY-MM'
    fetched_at TEXT NOT NULL,      -- UTC ISO timestamp
    final INTEGER NOT NULL,
    rows INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (site, kind, key)
);
CREATE TABLE IF NOT EXISTS usage (
    site TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'day:YYYY-MM-DD' (hourly) | 'month:YYYY-MM' (daily)
    energy TEXT NOT NULL,          -- 'electricity' | 'gas' | 'feed_in'
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    usage REAL NOT NULL,
    costs REAL NOT NULL,
    unit TEXT,
    PRIMARY KEY (site, source, energy, start_utc)
);
CREATE TABLE IF NOT EXISTS prices (
    site TEXT NOT NULL,
    energy TEXT NOT NULL,
    start_utc TEXT NOT NULL,
    end_utc TEXT NOT NULL,
    price REAL NOT NULL,           -- all-in EUR per unit incl. tax and markup
    market_price REAL,
    PRIMARY KEY (site, energy, start_utc)
);
CREATE TABLE IF NOT EXISTS weather (
    day TEXT NOT NULL,
    location TEXT NOT NULL,
    tmean REAL,
    PRIMARY KEY (day, location)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _utc_iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- meta -----------------------------------------------------------
    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, json.dumps(value, default=str))
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---- fetch log ------------------------------------------------------
    def fetch_state(self, site: str) -> dict[tuple[str, str], bool]:
        """{(kind, key): final} for everything fetched so far."""
        rows = self.conn.execute("SELECT kind, key, final FROM fetch_log WHERE site = ?", (site,))
        return {(k, key): bool(f) for k, key, f in rows}

    def _log(self, site: str, kind: str, key: str, final: bool, rows: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO fetch_log(site, kind, key, fetched_at, final, rows) VALUES (?,?,?,?,?,?)",
            (site, kind, key, _utc_iso(datetime.now(UTC)), int(final), rows),
        )

    # ---- writes ---------------------------------------------------------
    def save_usage(self, site: str, kind: str, key: str, slots: Iterable[dict], final: bool) -> int:
        """Replace all usage rows owned by this fetch, then log it."""
        source = ("day:" if kind == "day_usage" else "month:") + key
        slots = list(slots)
        with self.conn:
            self.conn.execute("DELETE FROM usage WHERE site = ? AND source = ?", (site, source))
            self.conn.executemany(
                "INSERT OR REPLACE INTO usage(site, source, energy, start_utc, end_utc, usage, costs, unit)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (site, source, s["energy"], _utc_iso(s["start"]), _utc_iso(s["end"]), s["usage"], s["costs"], s.get("unit"))
                    for s in slots
                ],
            )
            self._log(site, kind, key, final, len(slots))
        return len(slots)

    def save_prices(self, site: str, key: str | None, slots: Iterable[dict], final: bool) -> int:
        slots = list(slots)
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO prices(site, energy, start_utc, end_utc, price, market_price) VALUES (?,?,?,?,?,?)",
                [
                    (site, s["energy"], _utc_iso(s["start"]), _utc_iso(s["end"]), s["price"], s.get("market_price"))
                    for s in slots
                ],
            )
            if key:
                self._log(site, "day_prices", key, final, len(slots))
        return len(slots)

    def save_weather(self, location: str, rows: dict[str, float]) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO weather(day, location, tmean) VALUES (?,?,?)",
                [(d, location, t) for d, t in rows.items()],
            )

    def weather_days(self, location: str) -> set[str]:
        rows = self.conn.execute("SELECT day FROM weather WHERE location = ? AND tmean IS NOT NULL", (location,))
        return {r[0] for r in rows}

    # ---- reads (pandas, local time) ---------------------------------------
    def _read(self, sql: str, params: tuple) -> pd.DataFrame:
        df = pd.read_sql_query(sql, self.conn, params=params)
        for col in ("start_utc", "end_utc"):
            if col in df:
                df[col.replace("_utc", "")] = pd.to_datetime(df[col], utc=True).dt.tz_convert(TZ)
        return df.drop(columns=[c for c in ("start_utc", "end_utc") if c in df])

    def hourly_usage(self, site: str) -> pd.DataFrame:
        """Fine-grained slots from day fetches (hourly or quarter-hourly)."""
        return self._read(
            "SELECT energy, start_utc, end_utc, usage, costs FROM usage WHERE site = ? AND source LIKE 'day:%'",
            (site,),
        )

    def daily_usage(self, site: str) -> pd.DataFrame:
        """Coarse slots from month fetches (normally one row per day)."""
        return self._read(
            "SELECT energy, start_utc, end_utc, usage, costs FROM usage WHERE site = ? AND source LIKE 'month:%'",
            (site,),
        )

    def prices(self, site: str) -> pd.DataFrame:
        return self._read("SELECT energy, start_utc, end_utc, price, market_price FROM prices WHERE site = ?", (site,))

    def weather(self, location: str) -> pd.DataFrame:
        df = pd.read_sql_query("SELECT day, tmean FROM weather WHERE location = ?", self.conn, params=(location,))
        df["day"] = pd.to_datetime(df["day"]).dt.date
        return df
