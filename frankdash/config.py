"""Where local data lives and shared constants.

Everything personal (cached energy data, optional login tokens) is stored in a
per-account folder under the user's home directory, never inside the repo.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")

DATA_ROOT = Path(os.environ.get("FRANKDASH_HOME", Path.home() / ".frank-dashboard"))

# Hourly detail is fetched one day per request, so it is limited to this many
# days back. Monthly totals (one request per month) cover the full history.
DEFAULT_DETAIL_DAYS = 400
# Never go further back than this, even if the contract is older.
DEFAULT_MAX_HISTORY_YEARS = 3
# How many requests may run at the same time against the Frank API.
MAX_CONCURRENCY = 4


def account_id(email: str) -> str:
    """Stable, non-reversible folder name for an account."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def account_dir(account: str) -> Path:
    path = DATA_ROOT / account
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_ROOT, 0o700)
        os.chmod(path, 0o700)
    except OSError:  # e.g. Windows; folder is already in the user's profile
        pass
    return path
