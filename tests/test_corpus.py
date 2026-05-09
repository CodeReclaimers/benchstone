from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from benchstone.corpus import (
    SPEC_FILENAME,
    CorpusError,
    corpus_status,
    verify_corpus,
)
from benchstone.manifest import Benchmark


def _bench(
    *,
    name: str = "b",
    corpus_path: str | None = "corpus",
    corpus_hash: str | None = None,
    corpus_type: str | None = "bytes",
) -> Benchmark:
    return Benchmark(
        name=name,
        entry_point=name,
        tier="performance",
        deterministic=True,
        metric_direction="minimize",
        expected_runtime_seconds=None,
        threads=1,
        gpu="none",
        background_required=False,
        repetitions=1,
        baseline_seeds=(1,),
        promotion_sigma=2.0,
        promotion_z=None,
        gate_policy="sigma",
        corpus_path=corpus_path,
        corpus_hash=corpus_hash,
        corpus_type=corpus_type,
        reference_policy=None,
    )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_no_op_when_hash_unset(tmp_path: Path) -> None:
    # If the manifest doesn't pin a hash, the verifier returns silently even
    # for nonsensical paths — no enforcement was requested.
    bench = _bench(corpus_path="does/not/exist", corpus_hash=None)
    verify_corpus(bench, tmp_path)


def test_bytes_match(tmp_path: Path) -> None:
    payload = b"frozen corpus content"
    (tmp_path / "corpus").write_bytes(payload)
    bench = _bench(corpus_hash=_sha256(payload), corpus_type="bytes")
    verify_corpus(bench, tmp_path)


def test_bytes_drift_raises_with_both_hashes(tmp_path: Path) -> None:
    (tmp_path / "corpus").write_bytes(b"actual content")
    bench = _bench(corpus_hash=_sha256(b"declared content"), corpus_type="bytes")
    with pytest.raises(CorpusError) as info:
        verify_corpus(bench, tmp_path)
    msg = str(info.value)
    assert "drift" in msg
    assert _sha256(b"declared content") in msg
    assert _sha256(b"actual content") in msg


def test_bytes_path_missing(tmp_path: Path) -> None:
    bench = _bench(
        corpus_path="absent", corpus_hash=_sha256(b"x"), corpus_type="bytes"
    )
    with pytest.raises(CorpusError, match="does not exist"):
        verify_corpus(bench, tmp_path)


def test_bytes_path_is_directory_points_to_spec(tmp_path: Path) -> None:
    (tmp_path / "corpus").mkdir()
    bench = _bench(corpus_hash=_sha256(b"x"), corpus_type="bytes")
    with pytest.raises(CorpusError, match="corpus_type='spec'"):
        verify_corpus(bench, tmp_path)


def test_spec_match(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    spec_bytes = b"[spec]\nseed = 7\n"
    (corpus_dir / SPEC_FILENAME).write_bytes(spec_bytes)
    # Other files alongside the spec are ignored — only the spec is hashed.
    (corpus_dir / "generated.bin").write_bytes(b"this should be ignored")
    bench = _bench(corpus_hash=_sha256(spec_bytes), corpus_type="spec")
    verify_corpus(bench, tmp_path)


def test_spec_drift_raises(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / SPEC_FILENAME).write_bytes(b"current spec")
    bench = _bench(corpus_hash=_sha256(b"prior spec"), corpus_type="spec")
    with pytest.raises(CorpusError, match="drift"):
        verify_corpus(bench, tmp_path)


def test_spec_missing_corpus_spec_file(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    bench = _bench(corpus_hash=_sha256(b"x"), corpus_type="spec")
    with pytest.raises(CorpusError, match=SPEC_FILENAME):
        verify_corpus(bench, tmp_path)


def test_spec_path_is_file(tmp_path: Path) -> None:
    (tmp_path / "corpus").write_bytes(b"oops, single file")
    bench = _bench(corpus_hash=_sha256(b"x"), corpus_type="spec")
    with pytest.raises(CorpusError, match="must be a directory|requires.*directory"):
        verify_corpus(bench, tmp_path)


def test_hash_without_path_is_an_error(tmp_path: Path) -> None:
    bench = _bench(corpus_path=None, corpus_hash=_sha256(b"x"), corpus_type="bytes")
    with pytest.raises(CorpusError, match="without corpus_path"):
        verify_corpus(bench, tmp_path)


def test_unknown_corpus_type_is_an_error(tmp_path: Path) -> None:
    # Defensive: manifest validation should catch this before we ever get here,
    # but the verifier refuses unknown types loudly rather than silently passing.
    (tmp_path / "corpus").write_bytes(b"x")
    bench = _bench(corpus_hash=_sha256(b"x"), corpus_type="generated")
    with pytest.raises(CorpusError, match="unknown corpus_type"):
        verify_corpus(bench, tmp_path)


def test_corpus_type_defaults_to_bytes_when_none(tmp_path: Path) -> None:
    payload = b"defaulted-to-bytes"
    (tmp_path / "corpus").write_bytes(payload)
    # Some manifests omit corpus_type and the loader defaults it to "bytes"
    # with a warning. The verifier follows that default for back-compat.
    bench = _bench(corpus_hash=_sha256(payload), corpus_type=None)
    verify_corpus(bench, tmp_path)


# --- corpus_status (discovery-time peek) -----------------------------------


def test_corpus_status_na_when_no_hash(tmp_path: Path) -> None:
    bench = _bench(corpus_path="anything", corpus_hash=None)
    assert corpus_status(bench, tmp_path) == "n/a"


def test_corpus_status_ok(tmp_path: Path) -> None:
    payload = b"frozen corpus content"
    (tmp_path / "corpus").write_bytes(payload)
    bench = _bench(corpus_hash=_sha256(payload), corpus_type="bytes")
    assert corpus_status(bench, tmp_path) == "ok"


def test_corpus_status_drift(tmp_path: Path) -> None:
    (tmp_path / "corpus").write_bytes(b"actual content")
    bench = _bench(corpus_hash=_sha256(b"declared content"), corpus_type="bytes")
    assert corpus_status(bench, tmp_path) == "drift"


def test_corpus_status_missing_path(tmp_path: Path) -> None:
    bench = _bench(
        corpus_path="absent", corpus_hash=_sha256(b"x"), corpus_type="bytes"
    )
    assert corpus_status(bench, tmp_path) == "missing"


def test_corpus_status_missing_when_path_unset(tmp_path: Path) -> None:
    bench = _bench(corpus_path=None, corpus_hash=_sha256(b"x"))
    assert corpus_status(bench, tmp_path) == "missing"


def test_corpus_status_missing_for_spec_without_spec_file(tmp_path: Path) -> None:
    (tmp_path / "corpus").mkdir()
    bench = _bench(corpus_hash=_sha256(b"x"), corpus_type="spec")
    assert corpus_status(bench, tmp_path) == "missing"


def test_corpus_status_spec_ok(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    spec_bytes = b"[spec]\nseed = 7\n"
    (corpus_dir / SPEC_FILENAME).write_bytes(spec_bytes)
    bench = _bench(corpus_hash=_sha256(spec_bytes), corpus_type="spec")
    assert corpus_status(bench, tmp_path) == "ok"
