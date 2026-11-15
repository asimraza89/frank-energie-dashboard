"""Daily mean outdoor temperature from Open-Meteo (free, no account).

Only the coordinates of the nearby city you pick are sent, never your address.
Results are cached in the local database, so each day is fetched once.
"""

from __future__ import annotations

from datetime import date, timedelta

import requests

from .storage import Store

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

# A coarse location is plenty for heating analysis.
CITIES: dict[str, tuple[float, float]] = {
    "Amsterdam": (52.37, 4.90),
    "Rotterdam": (51.92, 4.48),
    "Den Haag": (52.08, 4.30),
    "Utrecht / De Bilt": (52.10, 5.18),
    "Eindhoven": (51.44, 5.48),
    "Groningen": (53.22, 6.57),
    "Zwolle": (52.52, 6.08),
    "Arnhem": (51.98, 5.91),
    "Maastricht": (50.85, 5.69),
    "Leeuwarden": (53.20, 5.80),
    "Middelburg": (51.50, 3.61),
}


def location_key(city: str) -> str:
    lat, lon = CITIES[city]
    return f"{lat:.2f},{lon:.2f}"


def fetch_archive(lat: float, lon: float, start: date, end: date) -> dict[str, float]:
    resp = requests.get(
        ARCHIVE_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_mean",
            "timezone": "Europe/Amsterdam",
        },
        timeout=30,
    )
    resp.raise_for_status()
    daily = resp.json().get("daily", {})
    return {d: t for d, t in zip(daily.get("time", []), daily.get("temperature_2m_mean", [])) if t is not None}


def sync_weather(store: Store, city: str, start: date, end: date, fetch=fetch_archive) -> int:
    """Fetch only the days not cached yet. Returns the number of new days."""
    key = location_key(city)
    have = store.weather_days(key)
    missing = [start + timedelta(n) for n in range((end - start).days + 1)]
    missing = [d for d in missing if d.isoformat() not in have]
    if not missing:
        return 0
    lat, lon = CITIES[city]
    rows = fetch(lat, lon, min(missing), max(missing))
    rows = {d: t for d, t in rows.items() if d not in have}
    store.save_weather(key, rows)
    return len(rows)
