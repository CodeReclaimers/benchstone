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
    """Runs at the same SHA are split by meta_seed: NULL = baseline, set = candidate."""
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


def test_set_and_get_baseline(tmp_path: Path) -> None:
    with Store(tmp_path / "store.db") as s:
        assert s.get_baseline("P", "B") is None
        s.set_baseline("P", "B", "aaa", "2026-04-19T14:22:11Z", notes="initial")
        base = s.get_baseline("P", "B")
        assert base is not None
        assert base.git_sha == "aaa"
        assert base.notes == "initial"

        # set_baseline on (P, B) again updates the pointer.
        s.set_baseline("P", "B", "bbb", "2026-04-19T15:00:00Z", notes="moved")
        base2 = s.get_baseline("P", "B")
        assert base2 is not None
        assert base2.git_sha == "bbb"
        assert base2.notes == "moved"


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
