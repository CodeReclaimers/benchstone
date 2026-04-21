from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
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
  stderr_log_path TEXT,
  artifact_hash TEXT,
  artifact_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_lookup ON runs(project, benchmark, git_sha);
CREATE INDEX IF NOT EXISTS idx_runs_timeline ON runs(project, benchmark, timestamp);

CREATE TABLE IF NOT EXISTS baselines (
  project TEXT NOT NULL,
  benchmark TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  established_at TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (project, benchmark)
);
"""

_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("artifact_hash", "ALTER TABLE runs ADD COLUMN artifact_hash TEXT"),
    ("artifact_path", "ALTER TABLE runs ADD COLUMN artifact_path TEXT"),
)

_RUN_COLUMNS: tuple[str, ...] = (
    "project", "benchmark", "git_sha", "git_dirty", "dirty_diff_path",
    "timestamp", "harness_version", "host", "seed", "meta_seed",
    "repetition_index", "status", "metric", "metric_components_json",
    "wall_clock_seconds", "project_metadata_json", "stderr_log_path",
)


@dataclass(frozen=True)
class Baseline:
    project: str
    benchmark: str
    git_sha: str
    established_at: str
    notes: str | None


@dataclass(frozen=True)
class Run:
    id: int
    project: str
    benchmark: str
    git_sha: str
    git_dirty: bool
    timestamp: str
    harness_version: str
    host: str
    status: str
    dirty_diff_path: str | None = None
    seed: int | None = None
    meta_seed: int | None = None
    repetition_index: int | None = None
    metric: float | None = None
    metric_components: dict[str, Any] | None = None
    wall_clock_seconds: float | None = None
    project_metadata: dict[str, Any] | None = None
    stderr_log_path: str | None = None
    artifact_hash: str | None = None
    artifact_path: str | None = None


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
        self._apply_migrations()
        self._conn.commit()
        # When > 0, inner insert/set_baseline calls skip the per-op commit so
        # the surrounding transaction() block can batch them atomically.
        self._in_txn: int = 0

    def _apply_migrations(self) -> None:
        existing = {row[1] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        for column_name, sql in _MIGRATIONS:
            if column_name not in existing:
                self._conn.execute(sql)

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
        if self._in_txn == 0:
            self._conn.commit()
        return int(cur.lastrowid)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Batch inserts and set_baseline into a single atomic commit.

        Nested ``with store.transaction()`` blocks join the outermost scope —
        only the outermost commit is observable. If the body raises the
        connection is rolled back.
        """
        outermost = self._in_txn == 0
        self._in_txn += 1
        try:
            yield
        except BaseException:
            self._in_txn -= 1
            if outermost:
                self._conn.rollback()
            raise
        else:
            self._in_txn -= 1
            if outermost:
                self._conn.commit()

    def fetch_runs(
        self,
        project: str,
        benchmark: str,
        git_sha: str | None = None,
        *,
        git_sha_prefix: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Run]:
        """Fetch runs with optional server-side filters.

        - ``git_sha``: exact match on the full SHA.
        - ``git_sha_prefix``: match any row whose git_sha starts with this string.
        - ``since``: ISO timestamp lower bound (inclusive). Lexical comparison
          works because all stored timestamps share the UTC-second format.
        - ``limit``: keep only the most recent N after filters are applied.
          The returned list is still in ascending-id order (the timeline).
        """
        clauses: list[str] = ["project=? AND benchmark=?"]
        args: list[Any] = [project, benchmark]
        if git_sha is not None:
            clauses.append("git_sha=?")
            args.append(git_sha)
        if git_sha_prefix is not None:
            clauses.append("git_sha LIKE ?")
            args.append(git_sha_prefix + "%")
        if since is not None:
            clauses.append("timestamp>=?")
            args.append(since)
        sql = f"SELECT * FROM runs WHERE {' AND '.join(clauses)}"
        if limit is not None and limit >= 0:
            # Take the last N rows by descending id then reverse, so SQLite
            # doesn't decode rows we're about to throw away.
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit)
            cur = self._conn.execute(sql, tuple(args))
            rows = list(cur.fetchall())
            rows.reverse()
            return [_row_to_run(r) for r in rows]
        sql += " ORDER BY id"
        cur = self._conn.execute(sql, tuple(args))
        return [_row_to_run(r) for r in cur.fetchall()]

    def fetch_baseline_runs(
        self, project: str, benchmark: str, git_sha: str
    ) -> list[Run]:
        """Runs with ``meta_seed IS NULL`` at the given SHA — the baseline seed set.

        The baseline runner uses the manifest's explicit ``baseline_seeds`` and
        records ``meta_seed=NULL``; evaluation runs derive seeds from a meta-seed
        and record it. This lets the gate distinguish the two sets even when
        the baseline and candidate share a git SHA (e.g., a sanity-check eval
        against the current head before any change has been made).
        """
        cur = self._conn.execute(
            "SELECT * FROM runs WHERE project=? AND benchmark=? "
            "AND git_sha=? AND meta_seed IS NULL ORDER BY id",
            (project, benchmark, git_sha),
        )
        return [_row_to_run(r) for r in cur.fetchall()]

    def fetch_candidate_runs(
        self, project: str, benchmark: str, git_sha: str
    ) -> list[Run]:
        """Runs with ``meta_seed IS NOT NULL`` at the given SHA — fresh-seed evaluations."""
        cur = self._conn.execute(
            "SELECT * FROM runs WHERE project=? AND benchmark=? "
            "AND git_sha=? AND meta_seed IS NOT NULL ORDER BY id",
            (project, benchmark, git_sha),
        )
        return [_row_to_run(r) for r in cur.fetchall()]

    def get_run(self, run_id: int) -> Run | None:
        cur = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,))
        row = cur.fetchone()
        return _row_to_run(row) if row is not None else None

    # -- baselines: the one pointer table that allows updates. Historical runs
    # that were ever marked baseline remain discoverable via the runs table
    # filtered by SHA; promoting a new state just moves the pointer.

    def set_baseline(
        self,
        project: str,
        benchmark: str,
        git_sha: str,
        established_at: str,
        notes: str | None = None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO baselines (project, benchmark, git_sha, established_at, notes)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project, benchmark) DO UPDATE SET
              git_sha=excluded.git_sha,
              established_at=excluded.established_at,
              notes=excluded.notes
            """,
            (project, benchmark, git_sha, established_at, notes),
        )
        if self._in_txn == 0:
            self._conn.commit()

    def get_baseline(self, project: str, benchmark: str) -> Baseline | None:
        cur = self._conn.execute(
            "SELECT * FROM baselines WHERE project=? AND benchmark=?",
            (project, benchmark),
        )
        row = cur.fetchone()
        return _row_to_baseline(row) if row is not None else None

    def list_baselines(self) -> list[Baseline]:
        cur = self._conn.execute(
            "SELECT * FROM baselines ORDER BY project, benchmark"
        )
        return [_row_to_baseline(r) for r in cur.fetchall()]


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
        "artifact_hash": fields.get("artifact_hash"),
        "artifact_path": fields.get("artifact_path"),
    }


def _row_to_baseline(row: sqlite3.Row) -> Baseline:
    return Baseline(
        project=row["project"],
        benchmark=row["benchmark"],
        git_sha=row["git_sha"],
        established_at=row["established_at"],
        notes=row["notes"],
    )


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
        artifact_hash=row["artifact_hash"],
        artifact_path=row["artifact_path"],
    )
