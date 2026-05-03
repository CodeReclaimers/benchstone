from __future__ import annotations

from pathlib import Path

import pytest

from benchstone.store import Store


def _base_row(**overrides: object) -> dict:
    row = dict(
        project="P",
        benchmark="B",
        git_sha="deadbeef",
        git_dirty=False,
        timestamp="2026-04-19T14:22:11Z",
        harness_version="0.1.0",
        host="testhost",
        status="ok",
    )
    row.update(overrides)
    return row


def test_insert_and_fetch_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    with Store(db) as s:
        run_id = s.insert_run(
            **_base_row(
                seed=42,
                meta_seed=1234,
                repetition_index=0,
                metric=1.0527,
                metric_components={"mean_fitness": 1.0527, "best_fitness": 0.9841},
                wall_clock_seconds=1043.2,
                project_metadata={"julia_version": "1.10.4"},
                stderr_log_path="/tmp/stderr.log",
            )
        )
        assert run_id == 1
        runs = s.fetch_runs("P", "B")
        assert len(runs) == 1
        r = runs[0]
        assert r.metric == pytest.approx(1.0527)
        assert r.metric_components == {"mean_fitness": 1.0527, "best_fitness": 0.9841}
        assert r.project_metadata == {"julia_version": "1.10.4"}
        assert r.seed == 42
        assert r.meta_seed == 1234
        assert r.git_dirty is False


def test_fetch_runs_filters_by_git_sha(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        s.insert_run(**_base_row(git_sha="aaa", seed=1, repetition_index=0, metric=1.0))
        s.insert_run(**_base_row(git_sha="aaa", seed=2, repetition_index=1, metric=1.1))
        s.insert_run(**_base_row(git_sha="bbb", seed=1, repetition_index=0, metric=2.0))

        at_aaa = s.fetch_runs("P", "B", git_sha="aaa")
        assert len(at_aaa) == 2
        assert {r.metric for r in at_aaa} == {1.0, 1.1}

        at_bbb = s.fetch_runs("P", "B", git_sha="bbb")
        assert len(at_bbb) == 1
        assert at_bbb[0].metric == 2.0

        all_runs = s.fetch_runs("P", "B")
        assert len(all_runs) == 3


def test_missing_required_field_raises(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        with pytest.raises(ValueError, match="missing required field 'host'"):
            s.insert_run(
                project="P", benchmark="B", git_sha="x", timestamp="t",
                harness_version="0.1.0", status="ok",
            )


def test_dirty_flag_and_diff_path(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        s.insert_run(**_base_row(git_dirty=True, dirty_diff_path="/tmp/diff.patch"))
        r = s.fetch_runs("P", "B")[0]
        assert r.git_dirty is True
        assert r.dirty_diff_path == "/tmp/diff.patch"


def test_get_run_by_id(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        rid = s.insert_run(**_base_row(metric=3.14))
        r = s.get_run(rid)
        assert r is not None
        assert r.metric == pytest.approx(3.14)
        assert s.get_run(9999) is None


def test_baseline_vs_candidate_split_by_meta_seed(tmp_path: Path) -> None:
    """Default behavior: NULL meta_seed = baseline, latest meta_seed group = candidate."""
    with Store(tmp_path / "store.db") as s:
        # Three baseline runs (meta_seed=None) at sha=aaa
        s.insert_run(**_base_row(git_sha="aaa", seed=1, meta_seed=None, metric=1.0))
        s.insert_run(**_base_row(git_sha="aaa", seed=2, meta_seed=None, metric=1.1))
        s.insert_run(**_base_row(git_sha="aaa", seed=3, meta_seed=None, metric=1.2))
        # Three candidate runs (meta_seed=42) at the same sha=aaa
        s.insert_run(**_base_row(git_sha="aaa", seed=101, meta_seed=42, metric=5.0))
        s.insert_run(**_base_row(git_sha="aaa", seed=102, meta_seed=42, metric=5.1))
        s.insert_run(**_base_row(git_sha="aaa", seed=103, meta_seed=42, metric=5.2))

        baseline = s.fetch_baseline_runs("P", "B", "aaa")
        candidate = s.fetch_candidate_runs("P", "B", "aaa")
        assert [r.metric for r in baseline] == [1.0, 1.1, 1.2]
        assert [r.metric for r in candidate] == [5.0, 5.1, 5.2]
        # The broad fetch still returns all six.
        assert len(s.fetch_runs("P", "B", "aaa")) == 6


def test_fetch_baseline_runs_with_meta_seed_selects_promoted_set(
    tmp_path: Path,
) -> None:
    """When the baseline pointer records a meta_seed (i.e. came from `bench
    promote`), fetch_baseline_runs(meta_seed=N) returns the candidate runs
    that were promoted, not the meta_seed=NULL set."""
    with Store(tmp_path / "store.db") as s:
        s.insert_run(**_base_row(git_sha="aaa", seed=1, meta_seed=None, metric=1.0))
        s.insert_run(**_base_row(git_sha="aaa", seed=2, meta_seed=None, metric=1.1))
        s.insert_run(**_base_row(git_sha="aaa", seed=10, meta_seed=7, metric=2.5))
        s.insert_run(**_base_row(git_sha="aaa", seed=11, meta_seed=7, metric=2.6))

        promoted = s.fetch_baseline_runs("P", "B", "aaa", meta_seed=7)
        assert [r.metric for r in promoted] == [2.5, 2.6]


def test_fetch_candidate_runs_returns_latest_meta_seed_group(
    tmp_path: Path,
) -> None:
    """Two `bench evaluate` invocations at the same SHA produce two distinct
    meta_seed groups. fetch_candidate_runs (default) returns only the most
    recently inserted group, instead of silently mixing distributions."""
    with Store(tmp_path / "store.db") as s:
        s.insert_run(**_base_row(git_sha="bbb", seed=1, meta_seed=11, metric=10.0))
        s.insert_run(**_base_row(git_sha="bbb", seed=2, meta_seed=11, metric=10.1))
        s.insert_run(**_base_row(git_sha="bbb", seed=3, meta_seed=22, metric=20.0))
        s.insert_run(**_base_row(git_sha="bbb", seed=4, meta_seed=22, metric=20.1))

        latest = s.fetch_candidate_runs("P", "B", "bbb")
        assert [r.metric for r in latest] == [20.0, 20.1]

        explicit_old = s.fetch_candidate_runs("P", "B", "bbb", meta_seed=11)
        assert [r.metric for r in explicit_old] == [10.0, 10.1]


def test_distinct_candidate_meta_seeds(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        s.insert_run(**_base_row(git_sha="ccc", seed=1, meta_seed=None, metric=1.0))
        s.insert_run(**_base_row(git_sha="ccc", seed=2, meta_seed=11, metric=2.0))
        s.insert_run(**_base_row(git_sha="ccc", seed=3, meta_seed=22, metric=3.0))
        s.insert_run(**_base_row(git_sha="ccc", seed=4, meta_seed=22, metric=3.1))

        assert s.distinct_candidate_meta_seeds("P", "B", "ccc") == [11, 22]
        assert s.distinct_candidate_meta_seeds("P", "B", "missing") == []


def test_set_and_get_baseline(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        assert s.get_baseline("P", "B") is None
        s.set_baseline("P", "B", "aaa", "2026-04-19T14:22:11Z", notes="initial")
        base = s.get_baseline("P", "B")
        assert base is not None
        assert base.git_sha == "aaa"
        assert base.notes == "initial"
        # Defaults to meta_seed=None for the establish path.
        assert base.meta_seed is None

        # set_baseline on (P, B) again updates the pointer.
        s.set_baseline("P", "B", "bbb", "2026-04-19T15:00:00Z", notes="moved")
        base2 = s.get_baseline("P", "B")
        assert base2 is not None
        assert base2.git_sha == "bbb"
        assert base2.notes == "moved"
        assert base2.meta_seed is None


def test_set_baseline_records_meta_seed_for_promote(tmp_path: Path) -> None:
    """When `bench promote` records the candidate run set's meta_seed, the
    pointer round-trips it so the next gate evaluation reads the same group."""
    with Store(tmp_path / "store.db") as s:
        s.set_baseline(
            "P", "B", "aaa", "2026-04-19T14:22:11Z",
            notes="from promote", meta_seed=42,
        )
        base = s.get_baseline("P", "B")
        assert base is not None
        assert base.meta_seed == 42

        # Updating back to a None meta_seed (e.g. a fresh `establish` over the
        # promoted pointer) clears the field.
        s.set_baseline(
            "P", "B", "bbb", "2026-04-19T15:00:00Z", notes="re-established",
        )
        base2 = s.get_baseline("P", "B")
        assert base2 is not None
        assert base2.meta_seed is None


def test_list_baselines(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        s.set_baseline("P1", "B1", "sha1", "t1")
        s.set_baseline("P2", "B2", "sha2", "t2")
        rows = s.list_baselines()
        assert [(r.project, r.benchmark) for r in rows] == [("P1", "B1"), ("P2", "B2")]


def test_store_persists_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "store.db"
    with Store(db) as s:
        s.insert_run(**_base_row(metric=42.0))
    with Store(db) as s:
        runs = s.fetch_runs("P", "B")
        assert len(runs) == 1
        assert runs[0].metric == 42.0


def test_baselines_meta_seed_column_migrates_in(tmp_path: Path) -> None:
    """Pre-existing DBs created before the meta_seed column existed gain it on
    next open and existing rows surface meta_seed=None."""
    import sqlite3

    db = tmp_path / "store.db"
    # Build a DB with the *old* baselines schema (no meta_seed column).
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
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
        CREATE TABLE baselines (
          project TEXT NOT NULL,
          benchmark TEXT NOT NULL,
          git_sha TEXT NOT NULL,
          established_at TEXT NOT NULL,
          notes TEXT,
          PRIMARY KEY (project, benchmark)
        );
        INSERT INTO baselines (project, benchmark, git_sha, established_at, notes)
        VALUES ('P', 'B', 'oldsha', '2026-04-01T00:00:00Z', 'pre-migration');
        """
    )
    conn.commit()
    conn.close()

    # Opening through Store applies the migration.
    with Store(db) as s:
        base = s.get_baseline("P", "B")
        assert base is not None
        assert base.git_sha == "oldsha"
        assert base.notes == "pre-migration"
        assert base.meta_seed is None
        # And we can write a new row with a non-None meta_seed.
        s.set_baseline("P", "B", "newsha", "2026-05-01T00:00:00Z",
                       notes="post-migration", meta_seed=99)
        assert s.get_baseline("P", "B").meta_seed == 99
