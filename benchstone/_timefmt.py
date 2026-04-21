"""UTC timestamp helper.

Every persisted timestamp in the harness is UTC, second-precision, formatted
as ``%Y-%m-%dT%H:%M:%SZ``. The format is a silent cross-module contract
(SQLite sorts it lexically, the `bench history --since` filter compares
strings). Centralizing it here keeps the contract in one place.
"""
from __future__ import annotations

from datetime import datetime, timezone

UTC_SECOND_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_SECOND_FORMAT)


def utc_stamp_tag() -> str:
    """Filename-safe variant (no colons/dashes), same instant semantics."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
