from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  benchmark TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  git_dirty INTEGER NOT NULL,
  dirty_diff_path TEXT,
  timestamp TEXT NOT NULL,
  harness_version TEXT NOT NULL,
  host TEXT NOT NULL,
  seed INTEGER,
  meta_seed INTEGER,
  repetition_index INTEGER,
  status TEXT NOT NULL,
  metric REAL,
  metric_components_json TEXT,
  wall_clock_seconds REAL,
  project_metadata_json TEXT,
  stderr_log_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_lookup ON runs(project, benchmark, git_sha);
CREATE INDEX IF NOT EXISTS idx_runs_timeline ON runs(project, benchmark, timestamp);
"""

_RUN_COLUMNS: tuple[str, ...] = (
    "project", "benchmark", "git_sha", "git_dirty", "dirty_diff_path",
    "timestamp", "harness_version", "host", "seed", "meta_seed",
    "repetition_index", "status", "metric", "metric_components_json",
    "wall_clock_seconds", "project_metadata_json", "stderr_log_path",
)


@dataclass(frozen=True)
class Run:
    id: int
    project: str
    benchmark: str
    git_sha: str
    git_dirty: bool
    dirty_diff_path: str | None
    timestamp: str
    harness_version: str
    host: str
    seed: int | None
    meta_seed: int | None
    repetition_index: int | None
    status: str
    metric: float | None
    metric_components: dict[str, Any] | None
    wall_clock_seconds: float | None
    project_metadata: dict[str, Any] | None
    stderr_log_path: str | None


class Store:
    """Append-only SQLite store for benchmark runs.

    The public API exposes inserts and reads only — no UPDATE/DELETE on runs.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def insert_run(self, **fields: Any) -> int:
        row = _prepare_run_row(fields)
        cols = ",".join(row.keys())
        placeholders = ",".join("?" for _ in row)
        cur = self._conn.execute(
            f"INSERT INTO runs ({cols}) VALUES ({placeholders})",
            tuple(row.values()),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def fetch_runs(
        self,
        project: str,
        benchmark: str,
        git_sha: str | None = None,
    ) -> list[Run]:
        if git_sha is None:
            cur = self._conn.execute(
                "SELECT * FROM runs WHERE project=? AND benchmark=? ORDER BY id",
                (project, benchmark),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM runs WHERE project=? AND benchmark=? AND git_sha=? ORDER BY id",
                (project, benchmark, git_sha),
            )
        return [_row_to_run(r) for r in cur.fetchall()]

    def get_run(self, run_id: int) -> Run | None:
        cur = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,))
        row = cur.fetchone()
        return _row_to_run(row) if row is not None else None


def _prepare_run_row(fields: dict[str, Any]) -> dict[str, Any]:
    required = ("project", "benchmark", "git_sha", "timestamp",
                "harness_version", "host", "status")
    for name in required:
        if name not in fields:
            raise ValueError(f"insert_run: missing required field {name!r}")

    def jdump(v: Any) -> str | None:
        return None if v is None else json.dumps(v, sort_keys=True)

    return {
        "project": fields["project"],
        "benchmark": fields["benchmark"],
        "git_sha": fields["git_sha"],
        "git_dirty": 1 if fields.get("git_dirty") else 0,
        "dirty_diff_path": fields.get("dirty_diff_path"),
        "timestamp": fields["timestamp"],
        "harness_version": fields["harness_version"],
        "host": fields["host"],
        "seed": fields.get("seed"),
        "meta_seed": fields.get("meta_seed"),
        "repetition_index": fields.get("repetition_index"),
        "status": fields["status"],
        "metric": fields.get("metric"),
        "metric_components_json": jdump(fields.get("metric_components")),
        "wall_clock_seconds": fields.get("wall_clock_seconds"),
        "project_metadata_json": jdump(fields.get("project_metadata")),
        "stderr_log_path": fields.get("stderr_log_path"),
    }


def _row_to_run(row: sqlite3.Row) -> Run:
    def jload(v: Any) -> Any:
        return None if v is None else json.loads(v)

    return Run(
        id=row["id"],
        project=row["project"],
        benchmark=row["benchmark"],
        git_sha=row["git_sha"],
        git_dirty=bool(row["git_dirty"]),
        dirty_diff_path=row["dirty_diff_path"],
        timestamp=row["timestamp"],
        harness_version=row["harness_version"],
        host=row["host"],
        seed=row["seed"],
        meta_seed=row["meta_seed"],
        repetition_index=row["repetition_index"],
        status=row["status"],
        metric=row["metric"],
        metric_components=jload(row["metric_components_json"]),
        wall_clock_seconds=row["wall_clock_seconds"],
        project_metadata=jload(row["project_metadata_json"]),
        stderr_log_path=row["stderr_log_path"],
    )
