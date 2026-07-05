"""Filesystem layout. Everything lives under one directory the user owns."""

from __future__ import annotations

import os
import stat
from pathlib import Path

APP_NAME = "tgvault"


def data_dir() -> Path:
    override = os.environ.get("TGVAULT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".tgvault"


def db_path() -> Path:
    return data_dir() / "vault.sqlite3"


def config_path() -> Path:
    return data_dir() / "config.json"


def session_path() -> Path:
    # Telethon appends .session itself
    return data_dir() / "telegram"


def ensure_data_dir() -> Path:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Owner-only: the session file and database grant full account access.
    # Best-effort on Windows, where POSIX modes don't fully apply.
    try:
        os.chmod(directory, stat.S_IRWXU)
    except OSError:
        pass
    return directory


def restrict_file(path: Path) -> None:
    if path.exists():
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
