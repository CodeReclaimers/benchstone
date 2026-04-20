# Arborist → benchstone migration briefing

**For:** a fresh agent session in `~/Arborist.jl`.
**Not for:** editing the benchstone harness itself — that's out of scope.

## Starting state

- benchstone MVP is complete through Phase 4 (commit `a778cc0`, 129 tests pass). Installed at `/home/alan/benchstone`, CLI `bench` on the venv path at `/home/alan/benchstone/.venv/bin/bench`.
- Arborist has unit tests and initial benchmarks on common problems; no manifest, no standardized result format, no baseline store.
- Your goal is guide §7: make Arborist a real benchstone customer. No harness-side changes. If you think you need one, flag it and stop — that's a Phase 5+ decision, not an escape hatch.

## Read in this order

1. `/home/alan/benchstone/benchmark-harness-implementation-guide.md` §3 (contract) and §7 (migration steps). §7 is the checklist.
2. `/home/alan/benchstone/tests/fixtures/fake_project/bench/manifest.toml` — fullest example of the manifest schema.
3. `/home/alan/benchstone/tests/fixtures/fake_project/bench/benchmarks.py` — Python-side protocol implementation, ~55 lines. Your Julia entry point is the Julia analog of this.
4. `/home/alan/benchstone/benchstone/protocol.py` — `InvocationConfig` (harness → project JSON) and `ProjectResult` (project → harness JSON). One place, authoritative.
5. refstore project `benchstone`, session notes Phase 1 (protocol) and Phase 3 (correctness artifacts). Only if something in the briefing is unclear.

## Contract at a glance

**Invocation template (Arborist example, already in guide §3.1):**
```toml
invocation = "julia --project=. bench/benchmarks.jl --entry={entry_point} --config={config_path} --output={output_path}"
```

**Config JSON the harness writes to `{config_path}`:**
```json
{"benchmark": "...", "seed": 42, "corpus_path": "/abs/.../bench/corpus/...",
 "repetition_index": 0, "repetition_total": 5, "artifact_path": null}
```
`artifact_path` is non-null only for correctness-tier benchmarks.

**Result JSON your entry point writes to `{output_path}`:**
```json
{"status": "ok", "metric": 1.0527,
 "metric_components": {"mean_fitness": 1.0527, "best_fitness": 0.9841},
 "wall_clock_seconds": 1043.2,
 "metadata": {"julia_version": "1.10.4", "notes": "..."}}
```
`status` is `"ok"` or `"error"`. `ok` requires a non-null `metric`. `error` requires `"message"`. Use `sort_keys=true` JSON or equivalent.

## Locked-in decisions — do not re-litigate

- **Manifest lives at `bench/manifest.toml`** (guide §2 convention). Not configurable.
- **baseline_seeds vs meta_seed distinguish baseline runs from candidate runs** — `bench baseline establish` uses the explicit list and stores `meta_seed=NULL`; `bench run --seed-set fresh` derives N seeds from one meta-seed and stores it. The gate splits on meta_seed nullness, which is what makes comparison work even at the same SHA. Your Julia entry point just takes `seed` from the config — it doesn't need to know anything about meta_seed.
- **The 1.0527 Karpathy-loop result is NOT the baseline.** Guide §7 step 5 is explicit: baseline is whatever the 5-rep run at current main SHA produces. The 1.0527 is a historical data point, imported later with `harness_version="pre-0.1.0"` — decide with Alan when.
- **Dirty tree refused by default**; `--allow-dirty` records a diff file alongside the run. Don't use it for baselines.
- **Correctness tier: byte artifact at `config.artifact_path`.** Harness hashes and archives. Your Julia code writes bytes to that path; that's it.
- **Subprocess inherits env** — Julia gets whatever env `bench` runs under. If Arborist needs specific env vars (e.g., `JULIA_NUM_THREADS`), set them before invoking `bench`, or bake them into the invocation template.

## Migration steps (from §7, annotated)

1. **`mkdir -p bench/corpus` in the Arborist root.**
2. **Write `bench/manifest.toml`.** Two entries:
   - `nsga2_binpack_mean_fitness` — tier=quality, minimize, `threads=8`, `gpu="none"`, `expected_runtime_seconds=1080`, `repetitions=5`, `baseline_seeds=[1,2,3,4,5]`, `promotion_sigma=2.0`, `background_required=true` (so `bench run` auto-backgrounds), `corpus_path="bench/corpus/binpack_common"`, `corpus_hash="sha256:..."` (fill after step 4).
   - A second benchmark from Arborist's existing suite, **picked to be most structurally different** from the first (different tier, different problem shape, or different RNG discipline). This is the real contract-stress test.
3. **Write `bench/benchmarks.jl`.** One `main()` that parses `--entry`, `--config`, `--output`; dispatches to per-benchmark functions; each function reads the config JSON, runs the existing benchmark code, writes the result JSON. Seed the RNG from `config["seed"]`. Don't reinvent the benchmarks — wrap them.
4. **Freeze the corpus.** If it's currently regenerated each run, generate once, commit under `bench/corpus/binpack_common/`, compute `sha256` with `shasum -a 256`, paste into `corpus_hash`.
5. **Commit everything on a branch** (clean tree; required by provenance check).
6. **`/home/alan/benchstone/.venv/bin/bench register .`** from Arborist root.
7. **`bench run Arborist nsga2_binpack_mean_fitness --seed-set baseline --foreground`** — one exercise run to catch protocol mistakes before the 90-minute baseline. If anything is wrong (exit non-zero, stderr log has Julia stack trace, result JSON malformed), fix before proceeding. `bench status` will show any detached jobs.
8. **`bench baseline establish Arborist nsga2_binpack_mean_fitness --notes "initial baseline on <sha>"`** — this is the 90-minute run (5 reps × ~18 min). Runs in the background by default since `background_required=true`; watch with `bench status`.
9. **Whitespace-only validation** (guide §7 step 6): whitespace-only commit, `bench run ... --seed-set fresh --meta-seed 1`, `bench evaluate ...`. Expect `REJECT` with sigma near zero. Confirms the pipeline round-trips cleanly.
10. **Known-good validation** (step 7): apply one edit from the recent Karpathy-loop run that you know produced an improvement, `bench run ... --seed-set fresh`, `bench evaluate ...`. Expect `PROMOTE` with sigma well past the 2.0 threshold.
11. **Short `bench/README.md`** pointing at `/home/alan/benchstone`.

## Arborist-specific parameters (what to put in the manifest)

| Field | Value | Why |
|---|---|---|
| `threads` | 8 | NSGA-II's thread count; scheduler needs to know |
| `gpu` | `"none"` | No direct GPU use |
| `expected_runtime_seconds` | 1080 | ~18 min per rep; sets scheduler expectations and drives background default |
| `background_required` | `true` | 90 min foreground would tie up a shell |
| `repetitions` | 5 | Matches the Karpathy-loop setup |
| `baseline_seeds` | `[1, 2, 3, 4, 5]` | Canonical reproducible set |
| `promotion_sigma` | `2.0` | Guide default |
| `metric_direction` | `"minimize"` | Lower `mean_fitness` is better |
| `corpus_hash` | `"sha256:..."` | Fill after committing the frozen corpus |

## Gotchas

- **Julia JIT warmup.** First rep includes compilation time. Either warm once before the timed section, or accept that rep 0 has a higher `wall_clock_seconds` — the gate doesn't use wall time, only `metric`. Flag this to Alan if the timing skew confuses interpretation.
- **The corpus must be bytewise frozen.** If the corpus is generated from a random seed at load time, freeze that output once (commit the bytes) rather than letting each run regenerate.
- **Don't let `bench/` and Arborist's existing test/benchmark layout collide.** If Arborist already has a `bench/` directory for another purpose, rename the existing one first; the `bench/manifest.toml` path is hardcoded.
- **Don't directly modify the baseline pointer.** Use `bench promote` or `bench baseline establish`. The `runs` table is append-only; only the `baselines` pointer table supports updates, and you want the harness to own that.
- **Artifacts archive grows monotonically.** Irrelevant for Arborist since the quality benchmarks don't produce artifacts (only correctness tier does); noted for completeness.

## Exit codes to wire into CI (if/when)

- `bench evaluate` → `0` PROMOTE/PASS · `1` REJECT/FAIL · `2` NEEDS_MORE_DATA or NO_BASELINE/NO_REFERENCE.
- `bench promote` → `0` on success, non-zero if verdict wasn't PROMOTE and `--force` wasn't passed.

## Ask Alan before committing to an approach

1. **Which second Arborist benchmark?** §7 step 2 says "most different to stress the contract" — Alan knows which existing benchmark best fits that. Don't pick one unilaterally.
2. **Import the 1.0527 historical result now or defer?** If now, it's a one-off SQL insert against `$BENCHSTONE_HOME/store.db` or a small helper; if deferred, it goes in a later pass.
3. **Is `JULIA_NUM_THREADS` handled at the invocation site?** Needs to be set correctly for the 8-thread declaration to hold; confirm whether it's baked into the invocation template or relies on caller env.
4. **Does Alan want you to wire the autoloop into `bench evaluate` / `bench promote` in the same session, or is that a separate pass?**

## Don't do

- Don't modify anything under `/home/alan/benchstone/`. Report needed changes instead.
- Don't skip the whitespace-validation step. It's cheap and catches a class of pipeline breakage before you've spent 90 minutes on a real baseline.
- Don't tune the benchmark code while wrapping it in the protocol. Refactor for protocol first, then optimize in a separate pass — mixing changes makes attribution impossible (per CLAUDE.md: "Don't combine refactoring with behavioral changes").
