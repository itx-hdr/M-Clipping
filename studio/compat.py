"""Keep existing installations working after the project rename."""
import os
from pathlib import Path


def env(name, default=None):
    """The new setting wins, including an explicitly empty value."""
    if name in os.environ:
        return os.environ[name]
    if name.startswith("MCLIPPER_"):
        return os.environ.get("PEAKCUT_" + name[len("MCLIPPER_"):], default)
    return default


def database_path(folder):
    """Use an existing database in place, including its SQLite journal files."""
    folder = Path(folder)
    current, previous = folder / "mclipper.db", folder / "peakcut.db"
    return previous if previous.exists() and not current.exists() else current


LEGACY_COOKIE = "peakcut_session"


def session_token(cookies):
    return cookies.get("mclipper_session") or cookies.get(LEGACY_COOKIE, "")
