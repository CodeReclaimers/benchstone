#!/usr/bin/env bash
# Non-Python project fixture for Phase 4 contract stress.
# Exercises the subprocess protocol using only bash + standard userspace
# tools (grep, awk, printf). The metric function mirrors the spirit of the
# Python fixture but stays deliberately simple.
set -euo pipefail

entry=""
config=""
output=""
for arg in "$@"; do
    case "$arg" in
        --entry=*)  entry="${arg#*=}" ;;
        --config=*) config="${arg#*=}" ;;
        --output=*) output="${arg#*=}" ;;
        *) ;;
    esac
done

if [[ -z "$entry" || -z "$config" || -z "$output" ]]; then
    echo "usage: benchmarks.sh --entry=E --config=PATH --output=PATH" >&2
    exit 64
fi

# Pluck individual scalar values out of the config JSON. The harness writes
# compact JSON via json.dumps(..., sort_keys=True), so this is a fixed shape
# (no nested quoting in the fields we read). Seeds are integers.
seed=$(grep -o '"seed": *[0-9]*' "$config" | head -n1 | awk '{print $2}')
artifact_path=$(sed -n 's/.*"artifact_path": *"\([^"]*\)".*/\1/p' "$config")

case "$entry" in
    shell_quality)
        metric=$(awk -v s="$seed" 'BEGIN { printf "%.6f", 2.0 + s / 10000.0 }')
        printf '{"status": "ok", "metric": %s, "wall_clock_seconds": 0.001, "metadata": {"impl": "bash"}}\n' \
            "$metric" > "$output"
        ;;
    shell_correctness)
        # Emit a fixed byte artifact at the path the harness provided. An
        # optional env var overrides the content so tests can simulate a
        # behavior change that breaks byte-equivalence.
        variant="${SHELL_CORRECTNESS_VARIANT:-v1}"
        if [[ -n "$artifact_path" ]]; then
            printf 'shell correctness artifact %s\n' "$variant" > "$artifact_path"
        fi
        printf '{"status": "ok", "metric": 0.0, "wall_clock_seconds": 0.001, "metadata": {"impl": "bash", "variant": "%s"}}\n' \
            "$variant" > "$output"
        ;;
    *)
        printf '{"status": "error", "message": "unknown entry: %s"}\n' "$entry" > "$output"
        exit 1
        ;;
esac
