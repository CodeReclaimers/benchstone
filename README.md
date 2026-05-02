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
baseline set: Arborist/nsga2_binpack_mean_fitness @ a1b2c3d4ef

# Make a code change. Run the candidate set against the same SHA-aware gate.
$ bench evaluate Arborist nsga2_binpack_mean_fitness
dispatched 5 run(s)  git_sha=9a8b7c6d  dirty=False  meta_seed=4427...
  run=6 rep=0 seed=...  metric=1.041  wall=1031.1s
  ... (5 reps) ...
verdict: PROMOTE  mann_whitney z=+2.61 >= threshold 2.0 (direction=minimize)
  baseline mean=1.0527  candidate mean=1.0413

# Move the baseline pointer if the gate passed.
$ bench promote Arborist nsga2_binpack_mean_fitness
baseline updated: Arborist/nsga2_binpack_mean_fitness  a1b2c3d4ef -> 9a8b7c6d
```

A consumer script (an autoloop, a CI step, etc.) reads the verdict line — `PROMOTE`, `REJECT`, `NEEDS_MORE_DATA`, or `NO_BASELINE` — and acts. There is no dashboard to interpret.

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
