from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from benchstone import api
from benchstone import paths as bs_paths
from benchstone.cli import main
from benchstone.gate import Verdict
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
