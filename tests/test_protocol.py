from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchstone.protocol import InvocationConfig, ProjectResult, ProtocolError


def test_invocation_config_roundtrip(tmp_path: Path) -> None:
    cfg = InvocationConfig(
        benchmark="b", seed=42, corpus_path="/tmp/c",
        repetition_index=1, repetition_total=3,
    )
    path = tmp_path / "cfg.json"
    cfg.write(path)
    loaded = json.loads(path.read_text())
    assert loaded == {
        "benchmark": "b", "seed": 42, "corpus_path": "/tmp/c",
        "repetition_index": 1, "repetition_total": 3,
    }


def test_project_result_ok() -> None:
    r = ProjectResult.from_json(json.dumps({
        "status": "ok",
        "metric": 1.5,
        "metric_components": {"x": 1.0},
        "wall_clock_seconds": 2.5,
        "metadata": {"julia_version": "1.10"},
    }))
    assert r.status == "ok"
    assert r.metric == 1.5
    assert r.metric_components == {"x": 1.0}
    assert r.wall_clock_seconds == 2.5
    assert r.metadata == {"julia_version": "1.10"}


def test_project_result_error() -> None:
    r = ProjectResult.from_json(json.dumps({"status": "error", "message": "boom"}))
    assert r.status == "error"
    assert r.message == "boom"
    assert r.metric is None


def test_project_result_ok_without_metric_raises() -> None:
    with pytest.raises(ProtocolError, match="requires a non-null 'metric'"):
        ProjectResult.from_json(json.dumps({"status": "ok"}))


def test_project_result_error_without_message_raises() -> None:
    with pytest.raises(ProtocolError, match="requires a 'message'"):
        ProjectResult.from_json(json.dumps({"status": "error"}))


def test_project_result_unknown_status_raises() -> None:
    with pytest.raises(ProtocolError, match="status must be"):
        ProjectResult.from_json(json.dumps({"status": "maybe"}))


def test_project_result_non_numeric_metric_raises() -> None:
    with pytest.raises(ProtocolError, match="metric must be a number"):
        ProjectResult.from_json(json.dumps({"status": "ok", "metric": "not-a-num"}))


def test_project_result_integer_metric_is_coerced() -> None:
    r = ProjectResult.from_json(json.dumps({"status": "ok", "metric": 5}))
    assert isinstance(r.metric, float)
    assert r.metric == 5.0


def test_project_result_read_from_file(tmp_path: Path) -> None:
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"status": "ok", "metric": 2.0}))
    r = ProjectResult.read(p)
    assert r.metric == 2.0
