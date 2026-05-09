from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from benchstone import api
from benchstone import paths as bs_paths
from benchstone import references
from benchstone.cli import main
from benchstone.gate import Verdict
from benchstone.references import Reference, ReferenceError
from benchstone.registry import RegistryError
from benchstone.store import Store


def test_evaluate_returns_no_baseline_when_unset(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])

    v = api.evaluate("FakeProject", "fake_quality")
    assert isinstance(v, Verdict)
    assert v.kind == "NO_BASELINE"


def test_evaluate_after_baseline_returns_a_verdict(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )
    v = api.evaluate("FakeProject", "fake_quality")
    # The fake project's fresh seeds produce metrics ~5x larger than baseline,
    # direction='minimize' → REJECT. We don't pin the value, just that it
    # produced a categorical, non-NEEDS_MORE_DATA result.
    assert v.kind in ("PROMOTE", "REJECT")
    assert v.baseline_mean is not None
    assert v.candidate_mean is not None


def test_evaluate_unregistered_project_raises(isolated_home: Path) -> None:
    with pytest.raises(RegistryError):
        api.evaluate("NotAProject", "fake_quality")


def test_evaluate_unknown_benchmark_raises(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    with pytest.raises(KeyError):
        api.evaluate("FakeProject", "no_such_benchmark")


def test_history_returns_runs(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])

    rows = api.history("FakeProject", "fake_quality")
    assert len(rows) == 3
    # Ordered by id ascending.
    assert rows == sorted(rows, key=lambda r: r.id)


def test_history_limit_filter(
    fake_project_git: Path, isolated_home: Path
) -> None:
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])

    rows = api.history("FakeProject", "fake_quality", limit=2)
    assert len(rows) == 2


def test_history_unregistered_project_raises(isolated_home: Path) -> None:
    with pytest.raises(RegistryError):
        api.history("NotAProject", "fake_quality")


def test_compute_verdict_round_trip_after_promote(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """compute_verdict honors the baseline pointer's meta_seed: after promote
    moves the pointer with meta_seed=N recorded, compute_verdict reads the
    promoted candidate runs as the baseline distribution."""
    runner = CliRunner()
    runner.invoke(main, ["register", str(fake_project_git)])
    runner.invoke(main, ["baseline", "establish", "FakeProject", "fake_quality"])
    runner.invoke(
        main,
        ["run", "FakeProject", "fake_quality",
         "--seed-set", "fresh", "--meta-seed", "42"],
    )
    runner.invoke(
        main,
        ["promote", "FakeProject", "fake_quality", "--force", "--notes", "rt"],
    )
    v = api.evaluate("FakeProject", "fake_quality")
    # Baseline (promoted, meta_seed=42) and candidate (latest group at the
    # same SHA, same meta_seed=42) coincide — sigma == 0 → REJECT — but
    # importantly NOT NEEDS_MORE_DATA, which is the bug this fix addresses.
    assert v.kind != "NEEDS_MORE_DATA"


# --- Write-side API ---------------------------------------------------------


def test_register_returns_registered_project(
    fake_project_git: Path, isolated_home: Path
) -> None:
    rp = api.register(fake_project_git)
    assert rp.name == "FakeProject"
    assert rp.path == fake_project_git
    assert rp.manifest_hash.startswith("sha256:")


def test_run_inserts_rows_and_returns_them(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    runs = api.run(
        "FakeProject", "fake_quality",
        seed_set="fresh", meta_seed=7,
    )
    # fake_quality declares repetitions=3.
    assert len(runs) == 3
    assert all(r.meta_seed == 7 for r in runs)
    assert all(r.status == "ok" for r in runs)
    # All three rows landed in the store.
    rows = api.history("FakeProject", "fake_quality")
    assert len(rows) == 3


def test_run_baseline_seed_set(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """seed_set='baseline' uses the manifest's explicit baseline_seeds, which
    record meta_seed=NULL on the inserted rows."""
    api.register(fake_project_git)
    runs = api.run("FakeProject", "fake_quality", seed_set="baseline")
    assert [r.seed for r in runs] == [1, 2, 3]
    assert all(r.meta_seed is None for r in runs)


def test_run_invalid_seed_set_raises(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    with pytest.raises(ValueError, match="seed_set"):
        api.run("FakeProject", "fake_quality", seed_set="bogus")


def test_establish_baseline_sets_pointer(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    runs = api.establish_baseline("FakeProject", "fake_quality")
    assert len(runs) == 3
    assert all(r.meta_seed is None for r in runs)
    # Baseline pointer should now be set; evaluate at the same SHA returns
    # something other than NO_BASELINE.
    v = api.evaluate("FakeProject", "fake_quality")
    assert v.kind != "NO_BASELINE"


def test_promote_returns_verdict_and_moves_pointer(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """promote evaluates the gate and (under force) moves the baseline pointer,
    recording the candidate run set's meta_seed so subsequent evaluate calls
    reuse the same runs as baseline."""
    api.register(fake_project_git)
    api.establish_baseline("FakeProject", "fake_quality")
    api.run("FakeProject", "fake_quality", meta_seed=42)
    v = api.promote("FakeProject", "fake_quality", force=True, notes="api-rt")
    assert isinstance(v, Verdict)
    # After promote, evaluating at the same SHA must not regress to
    # NEEDS_MORE_DATA — the promoted runs are the baseline.
    follow_up = api.evaluate("FakeProject", "fake_quality")
    assert follow_up.kind != "NEEDS_MORE_DATA"
    assert follow_up.kind != "NO_BASELINE"


def test_promote_refuses_non_promote_verdict_without_force(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """The fake project's fresh seeds produce a worse metric than the baseline
    (REJECT). promote must refuse without force=True."""
    api.register(fake_project_git)
    api.establish_baseline("FakeProject", "fake_quality")
    api.run("FakeProject", "fake_quality", meta_seed=42)
    with pytest.raises(ValueError, match="refusing to promote"):
        api.promote("FakeProject", "fake_quality")


def test_promote_meta_seed_disambiguation(
    fake_project_git: Path, isolated_home: Path
) -> None:
    """With two candidate meta_seed groups at the same SHA, promote must
    refuse to pick silently."""
    api.register(fake_project_git)
    api.establish_baseline("FakeProject", "fake_quality")
    api.run("FakeProject", "fake_quality", meta_seed=42)
    api.run("FakeProject", "fake_quality", meta_seed=43)
    with pytest.raises(ValueError, match="multiple meta_seeds"):
        api.promote("FakeProject", "fake_quality", force=True)
    # Specifying meta_seed resolves the ambiguity.
    v = api.promote(
        "FakeProject", "fake_quality", meta_seed=42, force=True,
    )
    assert isinstance(v, Verdict)


def test_freeze_reference_round_trip(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    api.run("FakeProject", "fake_correctness", meta_seed=1)
    ref = api.freeze_reference(
        "FakeProject", "fake_correctness", notes="initial",
    )
    assert isinstance(ref, Reference)
    assert ref.project == "FakeProject"
    assert ref.benchmark == "fake_correctness"
    assert ref.content_hash.startswith("sha256:")
    # A subsequent run with an identical artifact PASSes.
    api.run("FakeProject", "fake_correctness", meta_seed=2)
    v = api.evaluate("FakeProject", "fake_correctness")
    assert v.kind == "PASS"


def test_freeze_reference_refuses_non_correctness(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    with pytest.raises(ValueError, match="correctness"):
        api.freeze_reference("FakeProject", "fake_quality")


def test_freeze_reference_already_frozen_raises(
    fake_project_git: Path, isolated_home: Path
) -> None:
    api.register(fake_project_git)
    api.run("FakeProject", "fake_correctness", meta_seed=1)
    api.freeze_reference("FakeProject", "fake_correctness")
    api.run("FakeProject", "fake_correctness", meta_seed=2)
    with pytest.raises(ReferenceError, match="already exists"):
        api.freeze_reference("FakeProject", "fake_correctness")
