from __future__ import annotations

import hashlib
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Tier = Literal["correctness", "performance", "quality"]
MetricDirection = Literal["minimize", "maximize"]
GpuMode = Literal["none", "direct", "ollama"]

VALID_TIERS: frozenset[str] = frozenset({"correctness", "performance", "quality"})
VALID_DIRECTIONS: frozenset[str] = frozenset({"minimize", "maximize"})
VALID_GPU_MODES: frozenset[str] = frozenset({"none", "direct", "ollama"})

KNOWN_PROJECT_FIELDS: frozenset[str] = frozenset({"name", "language", "invocation"})
KNOWN_BENCHMARK_FIELDS: frozenset[str] = frozenset({
    "name", "entry_point", "tier", "deterministic", "metric_direction",
    "expected_runtime_seconds", "threads", "gpu", "background_required",
    "repetitions", "baseline_seeds", "promotion_sigma", "promotion_z",
    "gate_policy", "corpus_path", "corpus_hash", "corpus_type",
    "reference_policy",
})

VALID_CORPUS_TYPES: frozenset[str] = frozenset({"bytes", "spec"})
VALID_GATE_POLICIES: frozenset[str] = frozenset({"sigma", "mann_whitney"})
KNOWN_TOP_LEVEL_KEYS: frozenset[str] = frozenset({"project", "benchmarks"})


class ManifestError(ValueError):
    """Raised when a manifest file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Project:
    name: str
    language: str
    invocation: str


@dataclass(frozen=True)
class Benchmark:
    name: str
    entry_point: str
    tier: Tier
    deterministic: bool
    metric_direction: MetricDirection | None
    expected_runtime_seconds: int | None
    threads: int
    gpu: GpuMode
    background_required: bool
    repetitions: int
    baseline_seeds: tuple[int, ...]
    promotion_sigma: float | None
    promotion_z: float | None
    corpus_path: str | None
    corpus_hash: str | None
    corpus_type: str | None
    reference_policy: str | None
    gate_policy: str


@dataclass(frozen=True)
class Manifest:
    project: Project
    benchmarks: tuple[Benchmark, ...]
    source_path: Path
    content_hash: str

    def benchmark(self, name: str) -> Benchmark:
        for b in self.benchmarks:
            if b.name == name:
                return b
        raise KeyError(f"no benchmark named {name!r} in {self.project.name}")


def load(path: str | Path) -> Manifest:
    """Load a manifest. `path` may point to a manifest file or a project directory."""
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "bench" / "manifest.toml"
    if not path.is_file():
        raise ManifestError(f"manifest not found at {path}")
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{path}: invalid TOML: {exc}") from exc
    _warn_unknown("top level", data, KNOWN_TOP_LEVEL_KEYS)
    project = _parse_project(data)
    benchmarks = _parse_benchmarks(data)
    content_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
    return Manifest(
        project=project,
        benchmarks=tuple(benchmarks),
        source_path=path,
        content_hash=content_hash,
    )


def _parse_project(data: dict) -> Project:
    if "project" not in data:
        raise ManifestError("missing [project] table")
    proj = data["project"]
    if not isinstance(proj, dict):
        raise ManifestError("[project] must be a table")
    _warn_unknown("project", proj, KNOWN_PROJECT_FIELDS)
    for required in ("name", "language", "invocation"):
        if required not in proj:
            raise ManifestError(f"project.{required} is required")
    return Project(
        name=str(proj["name"]),
        language=str(proj["language"]),
        invocation=str(proj["invocation"]),
    )


def _parse_benchmarks(data: dict) -> list[Benchmark]:
    raw = data.get("benchmarks", [])
    if not isinstance(raw, list):
        raise ManifestError("benchmarks must be an array of tables")
    if not raw:
        raise ManifestError("at least one [[benchmarks]] entry is required")
    seen_names: set[str] = set()
    out: list[Benchmark] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ManifestError(f"benchmarks[{idx}] must be a table")
        bench = _parse_benchmark(entry, idx)
        if bench.name in seen_names:
            raise ManifestError(f"duplicate benchmark name: {bench.name}")
        seen_names.add(bench.name)
        out.append(bench)
    return out


def _parse_benchmark(entry: dict, idx: int) -> Benchmark:
    _warn_unknown(f"benchmarks[{idx}]", entry, KNOWN_BENCHMARK_FIELDS)
    for required in ("name", "entry_point", "tier"):
        if required not in entry:
            raise ManifestError(f"benchmarks[{idx}].{required} is required")
    tier = entry["tier"]
    if tier not in VALID_TIERS:
        raise ManifestError(
            f"benchmarks[{idx}].tier must be one of {sorted(VALID_TIERS)}, got {tier!r}"
        )
    is_correctness = tier == "correctness"

    direction = entry.get("metric_direction")
    if direction is not None and direction not in VALID_DIRECTIONS:
        raise ManifestError(
            f"benchmarks[{idx}].metric_direction must be one of "
            f"{sorted(VALID_DIRECTIONS)}, got {direction!r}"
        )
    if not is_correctness and direction is None:
        raise ManifestError(
            f"benchmarks[{idx}].metric_direction is required for tier={tier!r}"
        )

    gpu = entry.get("gpu", "none")
    if gpu not in VALID_GPU_MODES:
        raise ManifestError(
            f"benchmarks[{idx}].gpu must be one of {sorted(VALID_GPU_MODES)}, got {gpu!r}"
        )

    promotion_sigma = entry.get("promotion_sigma")
    if not is_correctness and promotion_sigma is None:
        raise ManifestError(
            f"benchmarks[{idx}].promotion_sigma is required for tier={tier!r}"
        )

    baseline_seeds = entry.get("baseline_seeds", [])
    if not isinstance(baseline_seeds, list) or not all(isinstance(s, int) for s in baseline_seeds):
        raise ManifestError(f"benchmarks[{idx}].baseline_seeds must be a list of integers")

    threads = int(entry.get("threads", 1))
    if threads < 1:
        raise ManifestError(f"benchmarks[{idx}].threads must be >= 1")

    repetitions = int(entry.get("repetitions", 1))
    if repetitions < 1:
        raise ManifestError(f"benchmarks[{idx}].repetitions must be >= 1")

    deterministic_default = is_correctness
    deterministic = bool(entry.get("deterministic", deterministic_default))

    corpus_type = entry.get("corpus_type")
    if corpus_type is not None and corpus_type not in VALID_CORPUS_TYPES:
        raise ManifestError(
            f"benchmarks[{idx}].corpus_type must be one of "
            f"{sorted(VALID_CORPUS_TYPES)}, got {corpus_type!r}"
        )
    if corpus_type is None and entry.get("corpus_hash"):
        # Default to 'bytes' when a hash is present — back-compat for
        # manifests authored before corpus_type existed. Warn so the
        # back-compat path is visible and can be removed on a schedule.
        warnings.warn(
            f"manifest: benchmarks[{idx}] sets corpus_hash without "
            f"corpus_type; defaulting to 'bytes' for back-compat. "
            f"Set corpus_type explicitly to silence this warning.",
            stacklevel=4,
        )
        corpus_type = "bytes"

    gate_policy = entry.get("gate_policy", "sigma")
    if gate_policy not in VALID_GATE_POLICIES:
        raise ManifestError(
            f"benchmarks[{idx}].gate_policy must be one of "
            f"{sorted(VALID_GATE_POLICIES)}, got {gate_policy!r}"
        )

    promotion_z = entry.get("promotion_z")
    if promotion_z is not None:
        promotion_z = float(promotion_z)
    if gate_policy == "mann_whitney" and promotion_z is None and not is_correctness:
        warnings.warn(
            f"manifest: benchmarks[{idx}] uses gate_policy='mann_whitney' without "
            f"promotion_z; falling back to promotion_sigma as the z-threshold. "
            f"Mann-Whitney z-scores are bounded by ~sqrt(3n^2/(2n+1)) and sigma "
            f"thresholds calibrated for parametric z do not transfer directly. "
            f"Set promotion_z explicitly to silence this warning.",
            stacklevel=4,
        )

    return Benchmark(
        name=str(entry["name"]),
        entry_point=str(entry["entry_point"]),
        tier=tier,
        deterministic=deterministic,
        metric_direction=direction,
        expected_runtime_seconds=(
            int(entry["expected_runtime_seconds"])
            if "expected_runtime_seconds" in entry else None
        ),
        threads=threads,
        gpu=gpu,
        background_required=bool(entry.get("background_required", False)),
        repetitions=repetitions,
        baseline_seeds=tuple(baseline_seeds),
        promotion_sigma=float(promotion_sigma) if promotion_sigma is not None else None,
        promotion_z=promotion_z,
        corpus_path=entry.get("corpus_path"),
        corpus_hash=entry.get("corpus_hash"),
        corpus_type=corpus_type,
        reference_policy=entry.get("reference_policy"),
        gate_policy=gate_policy,
    )


def _warn_unknown(context: str, data: dict, known: frozenset[str]) -> None:
    unknown = sorted(set(data.keys()) - known)
    if unknown:
        warnings.warn(
            f"manifest: unknown field(s) in {context}: {unknown}",
            stacklevel=3,
        )
