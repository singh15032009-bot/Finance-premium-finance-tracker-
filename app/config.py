"""Runtime paths, resolved to somewhere actually writable.

Serverless platforms (Vercel, AWS Lambda, Cloud Run) ship the application on a
read-only filesystem with only ``/tmp`` writable. The original code created its
data directory at import time, which turned a read-only filesystem into a
module-import crash and a 500 on every route.

This module picks a writable directory once, at startup, by *probing* rather
than by guessing from environment variables -- the probe is what actually
matters, and it works on platforms we have never heard of.

IMPORTANT: when the only writable location is a temp directory, storage is
ephemeral. Data written there disappears when the instance is recycled, and
concurrent instances do not share it. ``IS_EPHEMERAL`` says so, and the app
surfaces it rather than letting a user believe their portfolio was saved.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

_ENV_VAR = "WEALTHTRACK_DATA_DIR"


def _is_writable(path: Path) -> bool:
    """Actually try to write. Permission bits lie on some platforms."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except (OSError, PermissionError):
        return False


def _resolve_data_dir() -> tuple[Path, bool, str]:
    """Return (directory, is_ephemeral, how_it_was_chosen)."""
    # 1. Explicit override always wins, if it works.
    override = os.environ.get(_ENV_VAR)
    if override:
        candidate = Path(override).expanduser()
        if _is_writable(candidate):
            return candidate, False, f"{_ENV_VAR}={override}"

    # 2. The normal case: a data/ directory next to the code.
    project_data = BASE_DIR / "data"
    if _is_writable(project_data):
        return project_data, False, "project data directory"

    # 3. Read-only deployment. Fall back to temp so the app still runs, but
    #    flag it loudly -- nothing written here survives.
    temp_dir = Path(tempfile.gettempdir()) / "wealthtrack"
    if _is_writable(temp_dir):
        return temp_dir, True, "temp directory (read-only filesystem)"

    # 4. Nothing is writable. Return the temp path anyway; the stores degrade
    #    to in-memory rather than refusing to import.
    return temp_dir, True, "no writable location found"


DATA_DIR, IS_EPHEMERAL, DATA_DIR_SOURCE = _resolve_data_dir()

PORTFOLIO_PATH = DATA_DIR / "portfolio.json"
SUBSCRIPTION_PATH = DATA_DIR / "subscription.json"

EPHEMERAL_WARNING = (
    "This deployment has no persistent disk, so holdings are stored in "
    "temporary space and will be lost when the server restarts. Set "
    f"{_ENV_VAR} to a writable path, or connect a database, to keep data."
)


def configure_yfinance_cache() -> str | None:
    """Point yfinance's caches somewhere writable.

    yfinance keeps a timezone cache under the user's cache directory. On a
    serverless host ``$HOME`` is typically read-only, which raises *after* a
    successful import -- so this must be set before the first Yahoo call, not
    left to fail on the first request.
    """
    cache_dir = DATA_DIR / "yf-cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache_dir = Path(tempfile.gettempdir()) / "wealthtrack-yf-cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

    try:
        import yfinance as yf

        yf.set_tz_cache_location(str(cache_dir))
    except Exception:
        # Older/newer yfinance may not expose this; a failure here is not fatal.
        return None
    return str(cache_dir)


def storage_info() -> dict:
    return {
        "data_dir": str(DATA_DIR),
        "ephemeral": IS_EPHEMERAL,
        "resolved_from": DATA_DIR_SOURCE,
        "warning": EPHEMERAL_WARNING if IS_EPHEMERAL else None,
    }
