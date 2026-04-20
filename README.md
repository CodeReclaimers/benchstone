# Benchstone

A measurement substrate for projects that care whether their numbers are real.

## What it is

Benchstone runs benchmarks, stores results, and decides whether a change is an improvement. It owns the parts of benchmarking that are the same across every project — how runs are dispatched, how results are recorded, how baselines are tracked, how statistical gates are applied — so that individual projects only have to say what their benchmarks are and how to exercise them.

A project registers with benchstone by adding a small `bench/` directory containing a manifest and entry-point functions. Benchstone handles everything else: invoking the project in a subprocess, passing seeds and corpus paths, collecting results, comparing against baseline with direction-aware sigma computation, and either promoting the new state or rejecting it. Frozen reference outputs for correctness benchmarks live in benchstone's own store, not in the project, so that optimization loops can't accidentally overwrite the ground truth they're being measured against.

## What it is not

- **Not a benchmark authoring tool.** Benchmarks are human-written and live in the project. Benchstone has no opinion on what makes a good benchmark, only on how to run one fairly and repeatedly.
- **Not a dashboard or visualization layer.** It produces an append-only SQLite store of results; visualization, trend analysis, and reporting are separate concerns that read from that store.
- **Not a CI system.** It can be invoked from CI and is designed to be, but it doesn't replace CI or try to be one.
- **Not a distributed job scheduler.** Runs happen on the host where benchstone is invoked. Multi-host execution is compatible with the schema but not implemented.

## Why it exists

Most codebases accumulate benchmarks the same way they accumulate tests: incrementally, inconsistently, with ad-hoc comparison scripts that eventually stop being run. When an autoloop or an agent starts proposing changes, the absence of a trustworthy measurement substrate is the first thing that breaks. Benchstone is the substrate that should have been there already.

It's also useful without any agent in the loop. Having a single place that knows the baseline for every benchmark across every project, refuses to run on dirty git state, and never overwrites historical results, removes a class of silent drift that most projects live with indefinitely.

## Design principles

- **Append-only results.** Historical runs are never overwritten.
- **Git SHA is mandatory provenance.** Dirty trees are refused by default.
- **Frozen references are immutable.** Replacing a reference is explicit and logged.
- **Benchmarks come in tiers.** Correctness is pass/fail; performance and quality are statistical.
- **The harness is domain-ignorant.** It knows how to run a subprocess and reason about numbers; everything else belongs to the project.

## Status

Early. First customer is [Arborist.jl](https://github.com/CodeReclaimers/Arborist.jl); contract may change as additional projects onboard.

## License

MIT
