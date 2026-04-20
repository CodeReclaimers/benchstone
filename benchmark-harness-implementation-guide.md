# Benchmark Harness: Implementation Guide

**Working name:** `codereclaimers-bench` (rename at will)
**First customer:** Arborist.jl
**Purpose:** Provide a portfolio-wide measurement substrate that owns how benchmarks are run, stored, and statistically gated, while projects own what their benchmarks are and how to exercise them. The harness is the foundation that makes autoloop experimentation trustworthy and that incidentally patches the thin-binder problem across the portfolio.

---

## 1. Design Principles

These are load-bearing. Violating any of them quietly degrades the trustworthiness of every number the harness produces.

1. **Clean boundary.** The project owns what it is and how to exercise it. The harness owns how measurements are taken, stored, and gated. The harness is domain-ignorant; it knows nothing about bin packing, CAD reverse engineering, or anything else.
2. **Append-only results.** Historical results are never overwritten. A re-run is a new row, not a replacement. This preserves drift analysis and audit trails.
3. **Git SHA is mandatory provenance.** Every result row records the project's git SHA at run time. Benchmarks refuse to run in a dirty tree by default, with an explicit `--allow-dirty` override.
4. **Harness infrastructure lives outside the editable surface.** The harness's own repository, its manifest parsing logic, and its statistical gating code are never inside the optimization target. This is enforced by git-level separation, not trust.
5. **Frozen reference outputs are immutable.** Once a reference output is frozen for a benchmark, the harness never lets the project overwrite it. Replacing a frozen reference is an explicit, logged, human-gated action.
6. **Benchmarks come in tiers.** Correctness (tier 1) is pass/fail. Performance (tier 2) and quality (tier 3) are statistical. The promotion gate behaves differently per tier.
7. **Resist generalization.** The design is shaped by serving one real benchmark well, then a second, then a second project. Features invented for hypothetical projects are deferred.

---

## 2. Architecture Overview

```
+------------------------------------------+
|           Autoloop / CI / CLI            |
+------------------------------------------+
                   |
                   v
+------------------------------------------+
|            codereclaimers-bench          |
|  - Registry (finds projects)             |
|  - Runner (dispatches, captures output)  |
|  - Stats (aggregation, sigma, gating)    |
|  - Store (append-only results DB)        |
|  - Frozen Reference Output Store         |
|  - CLI                                   |
+------------------------------------------+
                   |
     Invokes via subprocess protocol
                   |
                   v
+------------------------------------------+
|              Project (e.g. Arborist.jl)  |
|  - Source code                           |
|  - Unit tests                            |
|  - bench/                                |
|     - manifest.toml                      |
|     - corpus/                            |
|     - benchmarks.jl (entry points)       |
+------------------------------------------+
```

**Recommended language for the harness: Python.** It's the most cross-cutting language across your portfolio, has excellent SQLite support, and subprocess-based invocation makes the language of each project irrelevant. This can be revisited but Python is the default.

---

## 3. The Project-Harness Contract

The contract has three parts: a manifest, an invocation protocol, and a result schema. Keep each narrow and stable.

### 3.1 Manifest Schema (`bench/manifest.toml`)

Each project has one manifest file. It registers benchmarks and declares metadata the harness needs to run them correctly.

```toml
[project]
name = "Arborist"
language = "julia"
# Command template used as the base for all benchmark invocations in this project.
# Placeholders: {entry_point}, {config_path}, {output_path}
invocation = "julia --project=. bench/benchmarks.jl --entry={entry_point} --config={config_path} --output={output_path}"

[[benchmarks]]
name = "nsga2_binpack_mean_fitness"
entry_point = "nsga2_binpack_mean_fitness"
tier = "quality"              # "correctness" | "performance" | "quality"
deterministic = false
metric_direction = "minimize" # "minimize" | "maximize"

# Execution characteristics
expected_runtime_seconds = 1080   # ~18 min; triggers background execution
threads = 8                       # how many threads the entry point will use
gpu = "none"                      # "none" | "direct" | "ollama"
background_required = true

# Statistical gating
repetitions = 5                   # number of runs per evaluation
baseline_seeds = [1, 2, 3, 4, 5]  # deterministic seed set for baselines
promotion_sigma = 2.0             # threshold for promotion

# Corpus
corpus_path = "bench/corpus/binpack_common"
corpus_hash = "sha256:..."        # frozen at registration; drift is an error

[[benchmarks]]
name = "sketch_constraint_solve_correctness"
entry_point = "sketch_constraint_solve_correctness"
tier = "correctness"
deterministic = true
threads = 1
gpu = "none"
background_required = false
# Correctness benchmarks skip sigma; they use frozen reference comparison.
reference_policy = "byte_equivalence"
```

**Field semantics worth being explicit about:**

- `deterministic`: if true, the harness runs once per evaluation and compares directly. If false, it runs `repetitions` times with seeds (either `baseline_seeds` for baseline establishment or fresh seeds for new-state evaluation) and applies statistical gating.
- `threads` and `gpu`: the harness uses these to decide scheduling. Two benchmarks that each claim 8 threads on a 16-thread machine can run concurrently; two that each claim the GPU cannot. `gpu = "ollama"` signals indirect GPU contention through a shared Ollama instance, which the scheduler handles differently from direct GPU ownership.
- `baseline_seeds`: the canonical seed set used when establishing a baseline for this benchmark. Re-running a baseline with these seeds should reproduce the baseline distribution exactly (given the same git SHA), which is a useful sanity check.
- `background_required`: makes the autoloop failure mode from your first Karpathy-loop run a first-class citizen. If true, the harness dispatches in the background rather than attempting a foreground run.

### 3.2 Invocation Protocol

The harness invokes the project's command, passing a JSON config on a known path and reading a JSON result from another known path. Stderr is captured as logs; stdout can be used for progress reporting but is not parsed for results.

**Config passed to the project (written to `config_path`):**

```json
{
  "benchmark": "nsga2_binpack_mean_fitness",
  "seed": 42,
  "corpus_path": "/absolute/path/to/bench/corpus/binpack_common",
  "repetition_index": 0,
  "repetition_total": 5
}
```

**Result written by the project (to `output_path`):**

```json
{
  "status": "ok",
  "metric": 1.0527,
  "metric_components": { "mean_fitness": 1.0527, "best_fitness": 0.9841 },
  "wall_clock_seconds": 1043.2,
  "metadata": {
    "julia_version": "1.10.4",
    "threads_actual": 8,
    "notes": "any free-form project-specific annotations"
  }
}
```

`status` is `"ok"` or `"error"`. For errors, include a `"message"` field. The primary `metric` is the scalar the gate compares. `metric_components` is optional structured detail for later analysis. Any additional structured fields are fine, but the top-level `metric` is what the gate sees.

### 3.3 Result Schema (as stored)

The harness extends the project's returned result with provenance fields before storing:

```json
{
  "project": "Arborist",
  "benchmark": "nsga2_binpack_mean_fitness",
  "git_sha": "a1b2c3d...",
  "git_dirty": false,
  "timestamp": "2026-04-19T14:22:11Z",
  "harness_version": "0.1.0",
  "host": "workstation-01",
  "seed": 42,
  "repetition_index": 0,
  "status": "ok",
  "metric": 1.0527,
  "metric_components": { ... },
  "wall_clock_seconds": 1043.2,
  "project_metadata": { ... }
}
```

---

## 4. Harness Components

### 4.1 Registry

Discovers projects. Two modes:

- **Explicit:** `bench register /path/to/Arborist.jl` adds the project to a local registry.
- **Implicit:** the CLI accepts `--project-path` for one-off runs.

The registry stores (project name, path, last-known manifest hash). A change in manifest hash prompts re-validation on next use.

### 4.2 Runner

Dispatches benchmark runs. Responsibilities:

- Read manifest, resolve the requested benchmark.
- Refuse to run if git tree is dirty (unless `--allow-dirty`); capture git SHA.
- For deterministic benchmarks: single invocation per evaluation.
- For stochastic benchmarks: `repetitions` invocations, each with a distinct seed. Seeds are either `baseline_seeds` (for baseline runs) or drawn from a fresh RNG (for evaluation runs), with the RNG seed itself recorded for reproducibility.
- Honor `background_required`: dispatch via a background process manager (a simple wrapper around `nohup` + PID file is sufficient initially; no need for a full job queue in phase 1).
- Respect scheduling constraints: two benchmarks competing for GPU direct access don't run concurrently; thread budgets are enforced against a configured host capacity.
- Capture stdout, stderr, timing, exit code.

### 4.3 Stats & Gating

Given a baseline run set and a candidate run set, compute:

- Mean and standard error of each set.
- Sigma of the difference: `(mean_candidate - mean_baseline) / sqrt(se_candidate² + se_baseline²)`.
- Direction-adjusted verdict using `metric_direction`.

The gate is applied based on tier:

- **Correctness (tier 1):** byte-equivalence against frozen reference. Pass/fail; sigma not applicable.
- **Performance and quality (tiers 2, 3):** sigma must meet or exceed `promotion_sigma`, direction-correct.

The stacking behavior you observed — two sub-threshold changes combining into a super-threshold one — is naturally handled because the gate is applied to the committed state's candidate set, whatever edits produced it. The gate doesn't need to know about individual edits.

### 4.4 Store

SQLite, append-only. Schema (abbreviated):

```sql
CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  benchmark TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  git_dirty BOOLEAN NOT NULL,
  timestamp TEXT NOT NULL,
  harness_version TEXT NOT NULL,
  host TEXT NOT NULL,
  seed INTEGER,
  repetition_index INTEGER,
  status TEXT NOT NULL,
  metric REAL,
  metric_components_json TEXT,
  wall_clock_seconds REAL,
  project_metadata_json TEXT,
  stderr_log_path TEXT
);

CREATE INDEX idx_runs_lookup ON runs(project, benchmark, git_sha);
CREATE INDEX idx_runs_timeline ON runs(project, benchmark, timestamp);

CREATE TABLE baselines (
  project TEXT NOT NULL,
  benchmark TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  established_at TEXT NOT NULL,
  notes TEXT,
  PRIMARY KEY (project, benchmark)
);
```

A baseline is a pointer — (project, benchmark) → git_sha. The associated run data is already in `runs` filtered by that SHA. Promoting a new state updates the baseline pointer; old runs remain in the store for history.

### 4.5 Frozen Reference Output Store

Lives in the harness's storage, not the project. Separate directory (`store/references/<project>/<benchmark>/`) keyed by hash. Each entry has metadata:

```
project: Arborist
benchmark: sketch_constraint_solve_correctness
frozen_at: 2026-04-19T14:22:11Z
frozen_git_sha: a1b2c3d...
content_hash: sha256:...
content_path: store/references/Arborist/sketch_constraint_solve_correctness/a1b2c3d.bin
notes: "Reference set captured on workstation-01, reviewed manually."
```

The harness has a `bench freeze-reference` command that takes an existing run's output, stores it as the reference, and records the metadata. Replacing a reference requires `bench replace-reference --reason="..."` and logs the replacement event.

This is the home for the accumulated reference data you mentioned across projects. It lives in one place, under version control (or at minimum under backup), and doesn't bloat the project repos.

### 4.6 CLI

Minimum viable commands:

- `bench register <path>` — add a project.
- `bench list [--project <name>]` — show registered benchmarks.
- `bench run <project> <benchmark> [--repetitions N] [--seed-set baseline|fresh] [--allow-dirty]` — execute and store results.
- `bench baseline establish <project> <benchmark>` — run the baseline-seed set and mark current SHA as baseline.
- `bench evaluate <project> <benchmark>` — run against current working tree, compare to baseline, print verdict.
- `bench promote <project> <benchmark>` — if evaluate passed, update baseline pointer.
- `bench freeze-reference <project> <benchmark> --from-run <run_id>` — capture a correctness reference.
- `bench history <project> <benchmark> [--since <date>]` — print timeline of results.

---

## 5. Promotion Gate Logic

Pseudocode for the quality-tier case (most common):

```
baseline_runs = fetch_runs(project, benchmark, git_sha=baseline_sha)
candidate_runs = fetch_runs(project, benchmark, git_sha=current_sha)

if len(candidate_runs) < manifest.repetitions:
    return NEEDS_MORE_DATA

sigma = directed_sigma(
    baseline_metrics=[r.metric for r in baseline_runs],
    candidate_metrics=[r.metric for r in candidate_runs],
    direction=manifest.metric_direction,
)

if sigma >= manifest.promotion_sigma:
    return PROMOTE(sigma=sigma)
else:
    return REJECT(sigma=sigma)
```

For correctness (byte-equivalence):

```
candidate_output = fetch_single_run_output(project, benchmark, git_sha=current_sha)
reference_output = fetch_frozen_reference(project, benchmark)

if hash(candidate_output) == reference_output.content_hash:
    return PASS
else:
    return FAIL(diff=compute_diff(candidate_output, reference_output))
```

---

## 6. Implementation Phases

Explicit phases because the temptation to build everything at once is strong and will lead to a harness that works for nothing rather than works for one thing well.

**Phase 0 — Bootstrap (half a day).**
Create the repo, stub the CLI with `argparse` or `click`, set up SQLite schema, get `bench register` and `bench list` working end-to-end against a hand-written manifest.

**Phase 1 — One benchmark, one project (one to two days).**
Target: `bench evaluate Arborist nsga2_binpack_mean_fitness` produces a verdict. This requires: subprocess invocation, JSON config/result protocol, stochastic runner with seeding, sigma computation, baseline storage. Skip background execution initially; run 5 reps × 18 min = 90 min foreground is tolerable for shakedown.

**Phase 2 — Background execution and second benchmark (one day).**
Add background dispatch for long-running benchmarks. Add a second Arborist benchmark (likely one of the existing initial benchmarks on common problems). This second benchmark is the one that shakes the contract; expect to discover one or two manifest fields that are wrong or missing.

**Phase 3 — Frozen reference outputs (half a day).**
Implement the reference store, `freeze-reference` and `replace-reference`. Migrate one existing correctness artifact as the first reference.

**Phase 4 — Second project (one to two days).**
Onboard a second project. By this point the contract should be stable. If onboarding requires changes to the harness, those changes are the real signal about what was over-specialized to Arborist.

**Phase 5 and beyond — deferred.**
Autoloop integration, cross-project dashboards, trend analysis, continuous scheduling. All of these read from the harness's store; none of them are the harness.

---

## 7. Arborist Migration Plan

Current state: Arborist.jl has a unit test suite and initial benchmarks on common problems. No manifest, no standardized result format, no baseline store.

**Steps:**

1. **Create `bench/` directory** at the project root.
2. **Write `bench/manifest.toml`** with entries for:
   - `nsga2_binpack_mean_fitness` (the one used in the recent Karpathy-loop run) — quality tier, stochastic, ~18 min, 5 reps.
   - One of the existing initial benchmarks on common problems — quality or performance tier depending on what it actually measures. Pick the one that's most different from the first benchmark to stress the contract.
3. **Refactor existing benchmark code into entry-point functions** that accept the JSON config shape and write the JSON result shape. Keep the existing benchmark logic; wrap it in the protocol layer.
4. **Move the corpus to `bench/corpus/`.** Compute hashes, record in manifest. If the corpus is currently inline in code or regenerated each run, freeze it once and commit the frozen version — benchmarking against a moving corpus is one of the ways rigor quietly leaks.
5. **Establish baseline:** run `bench baseline establish Arborist nsga2_binpack_mean_fitness` at the current main-branch SHA. The existing 1.0527 extended-run record is not the baseline; the baseline is whatever the 5-rep run produces at the current SHA. The 1.0527 becomes a historical note, not a gating target.
6. **Validate with a no-op change:** make a whitespace-only commit, run `bench evaluate`, confirm the sigma is near zero and the verdict is REJECT. This sanity-checks the whole pipeline.
7. **Validate with a known-good change:** apply one of the edits from the recent Karpathy-loop run that you know produced an improvement, run `bench evaluate`, confirm PROMOTE with appropriate sigma.
8. **Document** what moved and where, so future-you (and the autoloop) has a clear mental model. A short `bench/README.md` in Arborist pointing at the harness is sufficient.

After this, adding a second Arborist benchmark is straightforward: new entry in the manifest, new entry-point function, new corpus subdirectory. The refactoring tax is paid once.

---

## 8. Open Questions / Deferred Decisions

Worth naming these now so they don't become silent assumptions:

- **How are long-running background jobs surfaced?** A `bench status` command showing running jobs and their PIDs is probably sufficient initially. Integration with a notification system (email, desktop notification) is phase 5+.
- **How does the autoloop interact with the harness?** Presumably via the same CLI or a thin Python API layer over it. The autoloop is a consumer of `bench evaluate` and `bench promote`; it should have no special access.
- **Cross-project regression checking.** When an autoloop promotes a change in project A, should the harness automatically re-run baselines for projects B, C, D? For now, no — projects are independent units. Cross-project regression is a meta-loop concern, not a harness concern.
- **Benchmark generation / auto-benchmarks.** Explicitly out of scope. All benchmarks are human-authored and committed; the harness has no opinion on how they came to exist, only on how they are run.
- **Remote execution.** For now, the harness runs on a single host. Multi-host execution (dispatching heavy benchmarks to a beefier machine) is deferred. Host is already recorded in each run row, which means the multi-host future is compatible with the current schema.

---

## Appendix: Rationale for Key Decisions

**Why TOML for the manifest rather than JSON or YAML.** Human-editable, comment-supporting, and strongly-enough typed to prevent the most common YAML foot-guns. Dependencies are cheap (`tomli` is stdlib-adjacent in modern Python).

**Why SQLite rather than a flat file or a full database.** Append-only semantics are straightforward. Single-file portability means the store travels with the harness. Query performance on millions of rows is fine for this use case. Upgrading to Postgres later is mechanical if ever needed.

**Why subprocess invocation rather than language-specific integrations.** Language-agnostic by default. A Python-specific or Julia-specific fast path can be added later as an optimization for projects where subprocess overhead matters, but the portfolio is too polyglot to pick one.

**Why frozen references in the harness rather than the project.** Immutability guarantees are stronger when the data lives outside the optimization target. Also consolidates the reference-data problem you mentioned — currently scattered, often uncommitted, no standard location.

**Why the `baseline_seeds` field in addition to `repetitions`.** Baselines need to be reproducible exactly for auditing purposes. Candidate runs should explore the seed space freely. Separating these intents in the manifest makes the distinction explicit rather than convention-based.
