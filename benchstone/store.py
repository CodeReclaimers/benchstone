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
  meta_seed INTEGER,
  PRIMARY KEY (project, benchmark)
);
"""

# (table, column_name, ALTER statement). Applied if the column is missing from
# the table — `CREATE TABLE IF NOT EXISTS` doesn't add columns to existing tables.
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("runs", "artifact_hash", "ALTER TABLE runs ADD COLUMN artifact_hash TEXT"),
    ("runs", "artifact_path", "ALTER TABLE runs ADD COLUMN artifact_path TEXT"),
    ("baselines", "meta_seed", "ALTER TABLE baselines ADD COLUMN meta_seed INTEGER"),
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
    # NULL when the baseline came from `bench baseline establish` (the manifest's
    # explicit baseline_seeds, which record meta_seed=NULL on the runs).
    # Non-NULL when the baseline came from `bench promote`: the meta_seed of the
    # candidate run set that justified the promotion. fetch_baseline_runs uses
    # this to select the runs that count as baseline at the pointer's SHA.
    meta_seed: int | None = None


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
        existing: dict[str, set[str]] = {}
        for table, column_name, sql in _MIGRATIONS:
            if table not in existing:
                existing[table] = {
                    row[1] for row in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            if column_name not in existing[table]:
                self._conn.execute(sql)
                existing[table].add(column_name)

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
        self,
        project: str,
        benchmark: str,
        git_sha: str,
        meta_seed: int | None = None,
    ) -> list[Run]:
        """Runs at the given SHA that count as baseline.

        ``meta_seed=None`` selects runs with ``meta_seed IS NULL`` — the seed
        set produced by ``bench baseline establish`` from the manifest's
        explicit ``baseline_seeds``. A non-None ``meta_seed`` selects runs with
        exactly that meta_seed value — the seed set produced by an evaluation
        that was later promoted (``bench promote``).

        The baseline pointer (see ``Baseline.meta_seed``) records which value
        to pass here, so the gate sees the same set the user promoted.
        """
        if meta_seed is None:
            cur = self._conn.execute(
                "SELECT * FROM runs WHERE project=? AND benchmark=? "
                "AND git_sha=? AND meta_seed IS NULL ORDER BY id",
                (project, benchmark, git_sha),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM runs WHERE project=? AND benchmark=? "
                "AND git_sha=? AND meta_seed=? ORDER BY id",
                (project, benchmark, git_sha, meta_seed),
            )
        return [_row_to_run(r) for r in cur.fetchall()]

    def fetch_candidate_runs(
        self,
        project: str,
        benchmark: str,
        git_sha: str,
        meta_seed: int | None = None,
    ) -> list[Run]:
        """Candidate (meta-seeded) runs at the given SHA.

        Default behavior (``meta_seed=None``) returns runs from the most
        recently inserted meta_seed group at this SHA — i.e., the latest
        ``bench evaluate`` invocation. This avoids silently mixing distributions
        from separate evaluations that happen to share a SHA. Callers that want
        a specific group pass ``meta_seed=N``.
        """
        if meta_seed is None:
            row = self._conn.execute(
                "SELECT meta_seed FROM runs WHERE project=? AND benchmark=? "
                "AND git_sha=? AND meta_seed IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (project, benchmark, git_sha),
            ).fetchone()
            if row is None:
                return []
            meta_seed = int(row["meta_seed"])
        cur = self._conn.execute(
            "SELECT * FROM runs WHERE project=? AND benchmark=? "
            "AND git_sha=? AND meta_seed=? ORDER BY id",
            (project, benchmark, git_sha, meta_seed),
        )
        return [_row_to_run(r) for r in cur.fetchall()]

    def distinct_candidate_meta_seeds(
        self, project: str, benchmark: str, git_sha: str
    ) -> list[int]:
        """All distinct meta_seed values for candidate runs at the given SHA.

        ``bench promote`` uses this to detect ambiguity: two separate ``bench
        evaluate`` invocations at the same SHA produce two distinct meta_seed
        groups, and promote refuses to pick silently.
        """
        cur = self._conn.execute(
            "SELECT DISTINCT meta_seed FROM runs WHERE project=? AND benchmark=? "
            "AND git_sha=? AND meta_seed IS NOT NULL ORDER BY meta_seed",
            (project, benchmark, git_sha),
        )
        return [int(r["meta_seed"]) for r in cur.fetchall()]

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
        meta_seed: int | None = None,
    ) -> None:
        """Move the baseline pointer to ``git_sha``.

        ``meta_seed`` records which seed group at ``git_sha`` counts as
        baseline. ``None`` (the default) is correct for ``bench baseline
        establish`` runs (manifest's explicit baseline_seeds, recorded with
        ``meta_seed=NULL``). For ``bench promote``, pass the candidate run
        set's meta_seed so the next gate evaluation reads the same runs that
        justified the promotion.
        """
        self._conn.execute(
            """
            INSERT INTO baselines
              (project, benchmark, git_sha, established_at, notes, meta_seed)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(project, benchmark) DO UPDATE SET
              git_sha=excluded.git_sha,
              established_at=excluded.established_at,
              notes=excluded.notes,
              meta_seed=excluded.meta_seed
            """,
            (project, benchmark, git_sha, established_at, notes, meta_seed),
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
        meta_seed=row["meta_seed"],
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
