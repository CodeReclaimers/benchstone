from __future__ import annotations

import os
from pathlib import Path


def benchstone_home() -> Path:
    override = os.environ.get("BENCHSTONE_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "benchstone"
    return Path.home() / ".local" / "share" / "benchstone"


def ensure_home() -> Path:
    home = benchstone_home()
    home.mkdir(parents=True, exist_ok=True)
    return home


def registry_path() -> Path:
    return benchstone_home() / "registry.json"


def store_path() -> Path:
    return benchstone_home() / "store.db"


def references_dir() -> Path:
    return benchstone_home() / "references"


def logs_dir() -> Path:
    return benchstone_home() / "logs"


def jobs_dir() -> Path:
    return benchstone_home() / "jobs"


def artifacts_dir() -> Path:
    return benchstone_home() / "artifacts"
