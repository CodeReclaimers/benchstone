from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from benchstone.manifest import ManifestError, load


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "manifest.toml"
    path.write_text(textwrap.dedent(body))
    return path


def test_load_valid_manifest(fake_project_path: Path) -> None:
    m = load(fake_project_path)
    assert m.project.name == "FakeProject"
    assert m.project.language == "python"
    assert "{entry_point}" in m.project.invocation
    names = [b.name for b in m.benchmarks]
    assert names == ["fake_quality", "fake_heavy", "fake_correctness"]
    assert m.content_hash.startswith("sha256:")


def test_benchmark_lookup(fake_project_path: Path) -> None:
    m = load(fake_project_path)
    b = m.benchmark("fake_quality")
    assert b.tier == "quality"
    assert b.metric_direction == "minimize"
    assert b.promotion_sigma == 2.0
    assert b.baseline_seeds == (1, 2, 3)
    assert b.repetitions == 3


def test_correctness_bench_defaults(fake_project_path: Path) -> None:
    m = load(fake_project_path)
    b = m.benchmark("fake_correctness")
    assert b.deterministic is True
    assert b.metric_direction is None
    assert b.promotion_sigma is None
    assert b.reference_policy == "byte_equivalence"


def test_missing_project_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [[benchmarks]]
        name = "b"
        entry_point = "b"
        tier = "quality"
        metric_direction = "minimize"
        promotion_sigma = 2.0
    """)
    with pytest.raises(ManifestError, match="missing .project."):
        load(path)


def test_invalid_tier_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"

        [[benchmarks]]
        name = "b"
        entry_point = "b"
        tier = "wrong"
    """)
    with pytest.raises(ManifestError, match="tier"):
        load(path)


def test_non_correctness_requires_metric_direction(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"

        [[benchmarks]]
        name = "b"
        entry_point = "b"
        tier = "quality"
        promotion_sigma = 2.0
    """)
    with pytest.raises(ManifestError, match="metric_direction"):
        load(path)


def test_non_correctness_requires_promotion_sigma(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"

        [[benchmarks]]
        name = "b"
        entry_point = "b"
        tier = "performance"
        metric_direction = "minimize"
    """)
    with pytest.raises(ManifestError, match="promotion_sigma"):
        load(path)


def test_duplicate_benchmark_name_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"

        [[benchmarks]]
        name = "b"
        entry_point = "b1"
        tier = "correctness"

        [[benchmarks]]
        name = "b"
        entry_point = "b2"
        tier = "correctness"
    """)
    with pytest.raises(ManifestError, match="duplicate benchmark name"):
        load(path)


def test_unknown_field_warns(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"
        future_field = "ignored"

        [[benchmarks]]
        name = "b"
        entry_point = "b"
        tier = "correctness"
    """)
    with pytest.warns(UserWarning, match="unknown field"):
        load(path)


def test_empty_benchmarks_list_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, """
        [project]
        name = "p"
        language = "python"
        invocation = "true"
    """)
    with pytest.raises(ManifestError, match="at least one"):
        load(path)
