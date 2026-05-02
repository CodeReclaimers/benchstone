"""Verify a benchmark's on-disk corpus matches the manifest's recorded hash.

Pinning the corpus content makes "the result changed because the corpus
changed" detectable instead of silent. The manifest declares the corpus path
and ``sha256:<hex>`` hash; this module recomputes the hash on demand and
refuses to run when they disagree.

Two corpus types are supported:

- ``"bytes"``: ``corpus_path`` is a single file; the hash is sha256 of its
  byte contents. Use this for small, frozen corpora committed as one file.
- ``"spec"``: ``corpus_path`` is a directory containing ``corpus_spec.toml``
  (typically a generator specification); the hash is sha256 of *just* that
  spec file. Use this when the actual corpus is regenerated on demand from a
  spec but the spec itself is the load-bearing input.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .manifest import Benchmark

SPEC_FILENAME = "corpus_spec.toml"


class CorpusError(Exception):
    """Raised when the on-disk corpus does not match the manifest's recorded hash."""


def verify_corpus(benchmark: Benchmark, project_path: Path) -> None:
    """Refuse to proceed if the on-disk corpus diverges from ``benchmark.corpus_hash``.

    No-op when ``corpus_hash`` is None (the manifest did not pin a hash). Resolves
    ``corpus_path`` against ``project_path`` and dispatches on ``corpus_type``;
    the type defaults to ``"bytes"`` because the manifest loader already applies
    that default (with a warning) when ``corpus_hash`` is set without an
    explicit type.
    """
    if benchmark.corpus_hash is None:
        return
    if benchmark.corpus_path is None:
        raise CorpusError(
            f"benchmark {benchmark.name!r} declares corpus_hash without corpus_path"
        )
    corpus_root = (project_path / benchmark.corpus_path).resolve()
    corpus_type = benchmark.corpus_type or "bytes"
    actual = _hash_corpus(corpus_root, corpus_type, benchmark.name)
    if actual != benchmark.corpus_hash:
        raise CorpusError(
            f"corpus drift for benchmark {benchmark.name!r}:\n"
            f"  declared:  {benchmark.corpus_hash}\n"
            f"  on disk:   {actual}\n"
            f"  path:      {corpus_root}\n"
            f"  type:      {corpus_type}\n"
            f"If the change is intentional, update corpus_hash in the manifest."
        )


def _hash_corpus(root: Path, corpus_type: str, bench_name: str) -> str:
    if corpus_type == "bytes":
        if not root.exists():
            raise CorpusError(
                f"benchmark {bench_name!r}: corpus_path does not exist at {root}"
            )
        if not root.is_file():
            raise CorpusError(
                f"benchmark {bench_name!r}: corpus_type='bytes' requires "
                f"corpus_path to be a single file, but {root} is a directory. "
                f"Set corpus_type='spec' if the directory contains a "
                f"{SPEC_FILENAME}."
            )
        return "sha256:" + hashlib.sha256(root.read_bytes()).hexdigest()
    if corpus_type == "spec":
        if not root.exists():
            raise CorpusError(
                f"benchmark {bench_name!r}: corpus_path does not exist at {root}"
            )
        if not root.is_dir():
            raise CorpusError(
                f"benchmark {bench_name!r}: corpus_type='spec' requires "
                f"corpus_path to be a directory containing {SPEC_FILENAME}, "
                f"but {root} is a file."
            )
        spec = root / SPEC_FILENAME
        if not spec.is_file():
            raise CorpusError(
                f"benchmark {bench_name!r}: corpus_type='spec' requires "
                f"{SPEC_FILENAME} inside {root}; not found"
            )
        return "sha256:" + hashlib.sha256(spec.read_bytes()).hexdigest()
    raise CorpusError(
        f"benchmark {bench_name!r}: unknown corpus_type {corpus_type!r}"
    )
