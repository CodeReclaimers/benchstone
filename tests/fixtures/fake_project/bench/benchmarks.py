#!/usr/bin/env python3
"""Entry points for the fake_project test fixture.

Each entry point reads the InvocationConfig from --config and writes a
ProjectResult to --output. The metric functions are deterministic in the seed
so the whole pipeline is reproducible given a fixed meta-seed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path


def fake_quality(cfg: dict) -> dict:
    """Quality benchmark: metric = 1.0 + (seed % 1000) / 10000.0 + small sin perturbation.

    Direction is 'minimize', so lower metric is better. The sin perturbation
    provides enough spread across seeds that sample variance is non-zero —
    otherwise SE would be zero and the gate would never discriminate.
    """
    seed = int(cfg["seed"])
    base = 1.0 + (seed % 1000) / 10000.0
    metric = base + 0.0005 * math.sin(seed)
    return {
        "status": "ok",
        "metric": metric,
        "metric_components": {"base": base, "perturbation": metric - base},
        "wall_clock_seconds": 0.001,
        "metadata": {"python_version": f"{sys.version_info[0]}.{sys.version_info[1]}"},
    }


def fake_correctness(cfg: dict) -> dict:
    """Correctness benchmark: always returns the same bytes. Reserved for Phase 3."""
    _ = cfg  # correctness ignores seed
    return {
        "status": "ok",
        "metric": 0.0,
        "wall_clock_seconds": 0.001,
        "metadata": {"reference_bytes_sha256": "placeholder"},
    }


ENTRY_POINTS = {
    "fake_quality": fake_quality,
    "fake_correctness": fake_correctness,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    fn = ENTRY_POINTS.get(args.entry)
    if fn is None:
        Path(args.output).write_text(json.dumps({
            "status": "error",
            "message": f"unknown entry point: {args.entry}",
        }))
        return 1

    cfg = json.loads(Path(args.config).read_text())
    start = time.monotonic()
    result = fn(cfg)
    elapsed = time.monotonic() - start
    result.setdefault("wall_clock_seconds", elapsed)
    Path(args.output).write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
