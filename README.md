# Benchstone

**Trustworthy benchmark gating for code-optimization loops.**

If you point Claude Code, an [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) evaluator, or your own [Karpathy-style autoloop](https://github.com/karpathy/autoresearch) at a codebase and ask *"did that change actually help?"*, benchstone is the thing that answers `PROMOTE` / `REJECT` / `NEEDS_MORE_DATA` — with append-only history, harness-owned reference data the agent physically cannot reach, and statistical gates appropriate to whether you are measuring correctness, performance, or quality.

It also works without an agent in the loop. Anything that needs *"is this number meaningfully better than the last one?"* on a recurring basis benefits from a single place that knows the baseline for every benchmark across every project, refuses to run on a dirty git tree, pins the corpus, and never overwrites historical results.

## Why now

Agent-driven code modification is mainstream — Cursor, Claude Code, OpenEvolve, AIDE, Karpathy's autoresearch, internal autoloops at production companies. The 2025–2026 literature on agent evaluation has independently converged on a single observation: **the evaluator is the attack surface.** When the same agent writes both the code and the metric, the metric becomes a liability unless the evaluator and the reference data live somewhere the agent cannot reach.

Benchstone is the production CLI for that threat model. The harness owns the reference artifacts, refuses to run against a dirty tree, and produces a categorical verdict designed to be consumed by a script — not a chart for a human to interpret.

## What it does

- Runs a benchmark you've registered, captures wall clock and a scalar metric, persists one row per repetition.
- Stores results in an append-only SQLite database keyed by `(project, benchmark, git_sha, seed, repetition)`. Re-runs add rows; nothing gets overwritten.
- Tracks a baseline pointer per `(project, benchmark)`. Promoting a new state moves the pointer; the old runs stay in the timeline.
- Compares baseline and candidate distributions with a direction-aware sigma gate or a rank-based Mann-Whitney gate (selectable per benchmark; the latter is what you want for heavy-tailed metrics like Pareto-front quality).
- For correctness benchmarks, captures a frozen reference artifact under the harness's storage and compares future runs by content hash. Replacing a reference is explicit, requires a `--reason`, and is logged in an append-only history file.
- Refuses to run on a dirty git tree by default; `--allow-dirty` records the dirty diff at `0o600` so the run is auditable.
- Verifies the on-disk corpus matches the manifest's recorded hash before invoking the project. Silent corpus drift is an error, not a footnote.
- Dispatches long-running benchmarks as background jobs with PID tracking and a `bench status` view; the foreground/background scheduler reasons about thread budgets and GPU contention.
- Supports `bench baseline establish --at-sha <sha>` via a temporary `git worktree`, so you can establish a baseline at a historical commit without disturbing your working tree.

## Quick example (Arborist.jl)

```bash
# Register the project (one-time; reads bench/manifest.toml)
$ bench register ~/Arborist.jl
registered Arborist  ~/Arborist.jl
  manifest: sha256:1f0c...

# Establish a baseline at the current SHA, using the manifest's baseline_seeds.
# This is the 5-rep run that defines "what the baseline distribution looks like."
$ bench baseline establish Arborist nsga2_binpack_mean_fitness
dispatched 5 run(s)  git_sha=a1b2c3d4ef  dirty=False  meta_seed=None
  run=1 rep=0 seed=1 metric=1.054 wall=1042.3s
  ... (5 reps) ...
baseline set: Arborist/nsga2_binpack_mean_fitness -> a1b2c3d4ef (5 run(s))

# Make a code change. Run the candidate set against the same SHA-aware gate.
# bench run dispatches the work; bench evaluate is a separate, read-only step
# that reports the verdict — and ends the process with an exit code that
# encodes it (see "Consumer wiring" below).
$ bench run Arborist nsga2_binpack_mean_fitness
dispatched 5 run(s)  git_sha=9a8b7c6d  dirty=False  meta_seed=4427...
  run=6 rep=0 seed=...  metric=1.041  wall=1031.1s
  ... (5 reps) ...

$ bench evaluate Arborist nsga2_binpack_mean_fitness
verdict: PROMOTE  mann_whitney z=+2.61 >= threshold 2.0 (direction=minimize)
  baseline mean=1.0527  candidate mean=1.0413

# Move the baseline pointer if the gate passed. The candidate run set's
# meta_seed is recorded on the pointer, so the next bench evaluate at the
# new SHA reads those runs as baseline — no separate `baseline establish`
# is needed at the new SHA.
$ bench promote Arborist nsga2_binpack_mean_fitness
baseline promoted: Arborist/nsga2_binpack_mean_fitness -> 9a8b7c6d  (baseline meta_seed=4427...)
```

A consumer script (an autoloop, a CI step, etc.) branches on the exit code from `bench evaluate` (or calls `benchstone.api.evaluate(...)` directly — see below). There is no dashboard to interpret.

## Authoring a benchmark

The harness invokes the project as a subprocess. The contract is a small JSON-over-files protocol; the canonical schema lives in [`benchstone/protocol.py`](benchstone/protocol.py).

### Manifest

Each project has a `bench/manifest.toml` at its root. `bench register PATH` reads `PATH/bench/manifest.toml`. The manifest declares the project's invocation template and one or more benchmarks; see [`tests/fixtures/fake_project/bench/manifest.toml`](tests/fixtures/fake_project/bench/manifest.toml) for a working example.

For stochastic-tier benchmarks (`tier = "performance"` or `"quality"`), `repetitions` and `len(baseline_seeds)` must each be at least 2 — the gate's per-side floor. The manifest loader warns at load time if either is below 2; with one or zero, the gate returns `NEEDS_MORE_DATA` forever. See [Gate floors and ceilings](#gate-floors-and-ceilings) for the full reachability rules.

### Invocation template

`project.invocation` is a string with the placeholders `{entry_point}`, `{config_path}`, and `{output_path}`. It is executed via `subprocess.run(..., shell=True)` so shell pipelines, redirects, and env-var expansion work naturally. **Manifests are trusted code** (see [Security model](#security-model)).

```toml
invocation = "python bench/runner.py --entry={entry_point} --config={config_path} --output={output_path}"
```

### Subprocess protocol

The harness writes an `InvocationConfig` JSON object to `{config_path}` before launch:

```json
{
  "benchmark":         "fake_quality",
  "seed":              42,
  "corpus_path":       "/abs/path/to/corpus",
  "repetition_index":  0,
  "repetition_total":  5,
  "artifact_path":     "/abs/path/artifact.bin"
}
```

`artifact_path` is non-null only for correctness-tier benchmarks; non-correctness runners should ignore it. `corpus_path` is the empty string when the manifest does not declare one.

The runner does its work and writes a `ProjectResult` to `{output_path}`:

```json
{
  "status":              "ok",
  "metric":              1.0413,
  "metric_components":   {"...": "..."},
  "wall_clock_seconds":  1031.1,
  "metadata":            {"...": "..."},
  "message":             null
}
```

`status` is `"ok"` or `"error"`. `"ok"` requires a non-null `metric`; `"error"` requires a non-empty `message`. The other fields are optional.

For correctness benchmarks the runner must additionally write byte content to `artifact_path` — the harness sha256s those bytes and treats the digest as the verdict input. **Output must be byte-deterministic** (sort dict keys, write a single trailing newline, etc.); a content-hash gate is unforgiving about whitespace and ordering.

A minimal Python runner:

```python
# bench/runner.py
import argparse, json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--entry"); p.add_argument("--config"); p.add_argument("--output")
args = p.parse_args()

cfg = json.loads(Path(args.config).read_text())
metric = run_benchmark(cfg["benchmark"], cfg["seed"], cfg["corpus_path"])
Path(args.output).write_text(json.dumps({"status": "ok", "metric": metric},
                                        sort_keys=True))
```

### Dirty-tree default

`bench run` and `bench baseline establish` refuse to operate on a dirty git tree by default. Pass `--allow-dirty` to override; the diff is persisted at `0o600` under `$BENCHSTONE_HOME/logs/<project>/<benchmark>/` and recorded with the run row. Dirty-row gating is currently the same as clean rows — see [Limits](#limits-worth-being-explicit-about).

### Promote semantics

`bench promote` records the candidate run set's meta_seed on the baseline pointer, so the next `bench evaluate` at the new SHA reads the promoted runs as baseline. No separate `bench baseline establish` is needed at the new SHA. If two `bench evaluate` invocations have run at the same SHA (two distinct meta_seeds), promote refuses to choose silently; pass `--meta-seed N` to disambiguate.

### Setup gotchas

- **The corpus must exist before `bench register`.** Registration loads the manifest and verifies any declared `corpus_hash` against the on-disk content. If your runner generates the corpus, run the generator first.
- **`bench freeze-reference` needs an artifact at the current SHA.** Run the benchmark once (`bench run`) so the harness has bytes to freeze, then freeze.

## Consumer wiring

### `bench evaluate` exit codes

| Exit | Verdict kinds |
|------|---------------|
| 0    | `PROMOTE`, `PASS` |
| 1    | `REJECT`, `FAIL` |
| 2    | `NEEDS_MORE_DATA`, `NO_BASELINE`, `NO_REFERENCE` |
| 3    | unknown verdict kind (defensive fallback) |
| 4    | `--expect MISMATCH` (only when `--expect` is passed) |

The same mapping is exposed as `benchstone.cli.VERDICT_EXIT_CODES`.

### Gate floors and ceilings

Two reachability constraints are easy to trip over when authoring a manifest.

**Sample-size floor.** The stochastic gate (performance/quality tiers) requires at least 2 runs per side regardless of what the manifest declares. With fewer, the verdict is always `NEEDS_MORE_DATA`:

```
len(baseline_runs)  >= max(len(baseline_seeds), 2)   # else NEEDS_MORE_DATA
len(candidate_runs) >= max(repetitions, 2)           # else NEEDS_MORE_DATA
```

A manifest with `repetitions = 1` or `len(baseline_seeds) < 2` will warn at register time, but the symptom — a perpetual `NEEDS_MORE_DATA` verdict — is otherwise easy to misread as a missing-runs problem.

**Mann-Whitney `promotion_z` ceiling.** The Mann-Whitney U test is rank-based, so |z| is bounded by the sample sizes alone:

```
|z|_max = sqrt(3 * n_b * n_c / (n_b + n_c + 1))
```

where `n_b = max(len(baseline_seeds), 2)` and `n_c = max(repetitions, 2)`. Setting `promotion_z` above this ceiling makes the gate *physically unreachable* — no candidate distribution can produce a z that high. Two common settings:

| baseline_seeds | repetitions | `\|z\|_max` |
|---:|---:|---:|
| 2 | 2 | ≈ 1.549 |
| 2 | 3 | ≈ 1.964 |
| 5 | 5 | ≈ 2.611 |
| 10 | 10 | ≈ 3.780 |

The manifest loader warns at register time when `promotion_z` exceeds this ceiling for the configured sample sizes.

### Python API

For consumer scripts that don't want to shell out, two read-only entry points:

```python
from benchstone.api import evaluate, history

verdict = evaluate("Arborist", "nsga2_binpack_mean_fitness")
if verdict.kind == "PROMOTE":
    ...

rows = history("Arborist", "nsga2_binpack_mean_fitness", limit=20)
```

`evaluate` returns a `Verdict`; `history` returns a list of `Run`. Configuration errors (project not registered, manifest invalid, benchmark name unknown, no git state) raise their native exceptions; gate outcomes — including "no baseline yet" or "not enough runs" — come back as `Verdict` objects with the corresponding `kind`.

## How it differs from other benchmark tools

|                                                  | (a) append-only by construction | (b) reference data harness-owned | (c) categorical PROMOTE/REJECT verdict | (d) language-agnostic JSON-over-subprocess | (e) dirty-tree refusal default | (f) autoloop-substrate framing | (g) corpus hash pinning |
|--------------------------------------------------|:-------------------------------:|:--------------------------------:|:--------------------------------------:|:------------------------------------------:|:------------------------------:|:------------------------------:|:-----------------------:|
| **Benchstone**                                   | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| [Bencher.dev](https://github.com/bencherdev/bencher) | partial | — | partial | partial (per-tool adapters) | — | retrofitted | — |
| [Codspeed](https://github.com/CodSpeedHQ/codspeed) | — | — | partial (CI status) | — | — | retrofitted | — |
| [Conbench](https://github.com/conbench/conbench) | — | — | partial | ✓ | — | — | — |
| [asv (airspeed velocity)](https://github.com/airspeed-velocity/asv) | partial | — | partial | — (Python) | — | — | — |
| [pytest-benchmark](https://github.com/ionelmc/pytest-benchmark) | partial | — | partial (`--benchmark-compare-fail`) | — (Python) | — | — | — |
| [OpenEvolve](https://github.com/algorithmicsuperintelligence/openevolve) evaluator | n/a | — | delegated to user | ✓ | — | — | — |
| [Karpathy autoresearch](https://github.com/karpathy/autoresearch) | via git | partial (convention) | partial (binary keep/revert) | — | — | ✓ | — |

The closest single tool by architecture is **Conbench** (language-agnostic JSON publishing API, baseline/contender comparison), but it is dashboard-and-server-centric, has no harness-owned reference store, no dirty-tree refusal, and no autoloop framing. **Bencher.dev** is the closest by recent positioning ("benchmark on bare metal with no human in the loop"), but it is hosted-SaaS-first and trusts whatever numbers your benchmark code emits — no anti-tampering primitive. **Karpathy's autoresearch** is the closest by *philosophy* (git-as-ratchet on a measurable target), but it is a 600-line teaching artifact, hard-coded for one project, with no statistical gate.

## What it is not

- **Not a benchmark authoring tool.** Benchmarks are human-written and live in the project. Benchstone has no opinion on what makes a good benchmark, only on how to run one fairly and repeatedly.
- **Not a dashboard or visualization layer.** It produces an append-only SQLite store; trend graphs, sparklines, and reporting are downstream consumers. `bench history` is a textual timeline.
- **Not a CI system or hosted SaaS.** It can be invoked from CI and is designed to be, but it does not replace CI, post PR comments, or run on someone else's servers.
- **Not a distributed job scheduler.** Single-host execution today. The schema is multi-host-compatible (each row records its host) but multi-host dispatch is not implemented.
- **Not an experiment-tracking platform for ML.** No artifact lineage, hyperparameter sweep DSL, or learning-curve plots. MLflow / Weights & Biases / Neptune already do that.

## Limits worth being explicit about

- **Single host today.** If you need bare-metal runners, instrumentation-based measurement (cachegrind / Valgrind / wall-perf-counters), or distributed dispatch, benchstone is honest about not having them. Pin to a quiet host with `taskset`, disable turbo boost, and accept wall-clock variance plus the statistical gate's job of seeing through it.
- **No PR-comment integration today.** If a human reviewer needs a chart in the PR, point another tool at the SQLite store. Benchstone's consumer is a script.
- **`--allow-dirty` rows are not excluded from gates.** Dirty rows are recorded with `git_dirty=true` and write a per-run diff at `0o600`, but the gate currently treats them like any other `status=ok` row. If you opt into running dirty, you opt into the consequences. (A future flag will let you exclude dirty rows from baseline/candidate sets by default.)
- **Python 3.11+ for the harness.** Anything for the project under test.
- **Single-tenant local install.** Stores live under `$BENCHSTONE_HOME` (default `$XDG_DATA_HOME/benchstone` or `~/.local/share/benchstone`).

## Design principles (load-bearing)

1. **Append-only results.** Historical runs are never overwritten. A re-run is a new row, not a replacement.
2. **Git SHA is mandatory provenance.** Every result row records the project's git SHA; dirty trees are refused by default.
3. **Frozen references are immutable.** Reference artifacts live under `$BENCHSTONE_HOME/artifacts/<project>/<benchmark>/<sha256>.bin`, content-addressed and outside the project tree. The project's process never receives a path into the reference store. Replacing a reference requires `bench replace-reference --reason "..."` and is logged.
4. **Benchmarks come in tiers.** Correctness is pass/fail against a frozen reference; performance and quality are statistical. The gate behaves differently per tier.
5. **The harness is domain-ignorant.** It knows how to run a subprocess and reason about numbers; everything else belongs to the project.

## Security model

The manifest's `project.invocation` template is executed via `subprocess.run(..., shell=True)` so authors can write shell pipelines naturally. **Manifests are trusted code.** Registering a project grants its `bench/manifest.toml` the ability to run arbitrary commands on the host whenever benchstone touches that project. Only register projects whose manifest you have read.

The reference-data protection is path-isolation, not OS-level capability sandboxing: the harness writes reference bytes under `$BENCHSTONE_HOME/artifacts/...`, never passes that path to the project subprocess, and the subprocess's only writable artifact location is a per-run `/tmp` tempdir. A project process running as the same user *could* write to `$BENCHSTONE_HOME` if it went looking; the threat model is "agent edits the project's evaluator code," not "agent has arbitrary filesystem access from inside the entry point."

Diffs, stderr logs, and background-job spec files are written `0o600`. On shared hosts this limits exposure to the user running benchstone.

## Status

Early. v0.1, MIT licensed. First production user is [Arborist.jl](https://github.com/CodeReclaimers/Arborist.jl) (multi-objective evolutionary optimization research). Contract may evolve as additional projects onboard; breaking changes will be noted in `CHANGELOG.md`.

## Install

```bash
pip install benchstone
```

Or from source:

```bash
git clone https://github.com/CodeReclaimers/benchstone
cd benchstone
pip install -e .
```

The `bench` CLI is installed; run `bench --help` for the command surface.

## License

MIT — see [LICENSE](LICENSE).
