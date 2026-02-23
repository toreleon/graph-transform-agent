#!/usr/bin/env bash
#
# Evaluate GraphPlan agent on SWE-bench Verified
#
# Usage:
#   ./scripts/eval_swebench_verified.sh                    # Full run (500 instances, 1 worker)
#   ./scripts/eval_swebench_verified.sh --workers 4        # Parallel workers
#   ./scripts/eval_swebench_verified.sh --slice 0:10       # First 10 instances only
#   ./scripts/eval_swebench_verified.sh --quick             # Quick smoke test (5 instances)
#   ./scripts/eval_swebench_verified.sh --resume            # Resume a previous run
#   ./scripts/eval_swebench_verified.sh --analyze           # Analyze existing results
#
# Environment variables:
#   MODEL          Model name (default: from config)
#   WORKERS        Number of parallel workers (default: 1)
#   OUTPUT_DIR     Output directory (default: output/swebench_verified_graphplan_<timestamp>)
#   CONFIG         Config file(s) (default: swebench_graphplan.yaml)
#   SPLIT          Dataset split (default: test)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

# ── Defaults ──────────────────────────────────────────────────────────────────

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEFAULT_OUTPUT="output/swebench_verified_graphplan_${TIMESTAMP}"
OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUTPUT}"
CONFIG="${CONFIG:-swebench_graphplan.yaml}"
SPLIT="${SPLIT:-test}"
WORKERS="${WORKERS:-1}"
MODEL="${MODEL:-}"
SLICE=""
FILTER=""
QUICK=false
RESUME=false
ANALYZE=false

# ── Parse arguments ───────────────────────────────────────────────────────────

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workers|-w)   WORKERS="$2"; shift 2 ;;
        --slice)        SLICE="$2"; shift 2 ;;
        --filter)       FILTER="$2"; shift 2 ;;
        --model|-m)     MODEL="$2"; shift 2 ;;
        --output|-o)    OUTPUT_DIR="$2"; shift 2 ;;
        --config|-c)    CONFIG="$2"; shift 2 ;;
        --split)        SPLIT="$2"; shift 2 ;;
        --quick)        QUICK=true; shift ;;
        --resume)       RESUME=true; shift ;;
        --analyze)      ANALYZE=true; shift ;;
        --help|-h)
            head -16 "$0" | tail -14
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ── Quick mode: 5 instances ───────────────────────────────────────────────────

if $QUICK; then
    SLICE="${SLICE:-0:5}"
    echo "Quick mode: running first ${SLICE#*:} instances"
fi

# ── Resume mode: find latest output dir ───────────────────────────────────────

if $RESUME; then
    if [[ "$OUTPUT_DIR" == "$DEFAULT_OUTPUT" ]]; then
        # Find the most recent output directory
        LATEST=$(ls -dt output/swebench_verified_graphplan_* 2>/dev/null | head -1)
        if [[ -z "$LATEST" ]]; then
            echo "Error: No previous run found to resume" >&2
            exit 1
        fi
        OUTPUT_DIR="$LATEST"
        echo "Resuming from: $OUTPUT_DIR"
    fi
fi

# ── Analyze mode: print results and exit ──────────────────────────────────────

if $ANALYZE; then
    if [[ "$OUTPUT_DIR" == "$DEFAULT_OUTPUT" ]]; then
        LATEST=$(ls -dt output/swebench_verified_graphplan_* 2>/dev/null | head -1)
        if [[ -z "$LATEST" ]]; then
            echo "Error: No output directory found to analyze" >&2
            exit 1
        fi
        OUTPUT_DIR="$LATEST"
    fi
    echo "Analyzing results in: $OUTPUT_DIR"
    echo ""

    if [[ -f "$OUTPUT_DIR/preds.json" ]]; then
        TOTAL=$(python3 -c "import json; d=json.load(open('$OUTPUT_DIR/preds.json')); print(len(d))")
        SUBMITTED=$(python3 -c "
import json
d = json.load(open('$OUTPUT_DIR/preds.json'))
submitted = sum(1 for v in d.values() if v.get('model_patch', '').strip())
print(submitted)
")
        echo "Instances completed: $TOTAL"
        echo "Patches submitted:   $SUBMITTED"
    else
        echo "No preds.json found"
    fi

    echo ""
    echo "Exit status reports:"
    for f in "$OUTPUT_DIR"/exit_statuses_*.yaml; do
        if [[ -f "$f" ]]; then
            echo "  $(basename "$f")"
            python3 -c "
import yaml
with open('$f') as fh:
    data = yaml.safe_load(fh)
if data and 'instances_by_exit_status' in data:
    for status, ids in data['instances_by_exit_status'].items():
        count = len(ids) if isinstance(ids, list) else 0
        print(f'    {status}: {count}')
" 2>/dev/null || echo "    (could not parse)"
        fi
    done

    echo ""
    echo "Trajectory files:"
    TRAJ_COUNT=$(find "$OUTPUT_DIR" -name "*.traj.json" 2>/dev/null | wc -l | tr -d ' ')
    echo "  Total: $TRAJ_COUNT"

    # Aggregate test results from trajectories
    python3 - "$OUTPUT_DIR" << 'PYEOF'
import json, sys, os
from pathlib import Path

output_dir = Path(sys.argv[1])
resolved = 0
partial = 0
not_resolved = 0
no_tests = 0
total_cost = 0.0
total_calls = 0

for traj_file in sorted(output_dir.rglob("*.traj.json")):
    try:
        data = json.loads(traj_file.read_text())
        info = data.get("info", {})
        stats = info.get("model_stats", {})
        total_cost += stats.get("instance_cost", 0)
        total_calls += stats.get("api_calls", 0)

        tr = info.get("test_results")
        if not tr:
            no_tests += 1
            continue
        if tr.get("all_passed"):
            resolved += 1
        elif tr.get("f2p_passed", 0) > 0:
            partial += 1
        else:
            not_resolved += 1
    except Exception:
        pass

total = resolved + partial + not_resolved + no_tests
if total > 0:
    print(f"\nTest results (from trajectories):")
    print(f"  Resolved:     {resolved}/{total} ({resolved/total*100:.1f}%)")
    print(f"  Partial:      {partial}/{total}")
    print(f"  Not resolved: {not_resolved}/{total}")
    print(f"  No test data: {no_tests}/{total}")
    print(f"\nCost summary:")
    print(f"  Total cost:   ${total_cost:.2f}")
    print(f"  Total calls:  {total_calls}")
    if total > 0:
        print(f"  Avg cost:     ${total_cost/total:.3f}/instance")
        print(f"  Avg calls:    {total_calls/total:.1f}/instance")
PYEOF

    echo ""
    echo "To submit results for official evaluation:"
    echo "  sb-cli submit swe-bench_verified test \\"
    echo "    --predictions_path $OUTPUT_DIR/preds.json \\"
    echo "    --run_id graphplan_$(basename "$OUTPUT_DIR")"
    exit 0
fi

# ── Pre-flight checks ────────────────────────────────────────────────────────

echo "=== SWE-bench Verified Evaluation ==="
echo ""
echo "  Config:     $CONFIG"
echo "  Split:      $SPLIT"
echo "  Output:     $OUTPUT_DIR"
echo "  Workers:    $WORKERS"
echo "  Model:      ${MODEL:-<from config>}"
echo "  Slice:      ${SLICE:-<all>}"
echo "  Filter:     ${FILTER:-<none>}"
echo ""

# Check docker is available
if ! command -v docker &>/dev/null; then
    echo "Error: docker is not installed or not in PATH" >&2
    exit 1
fi

# Check docker is running
if ! docker info &>/dev/null 2>&1; then
    echo "Error: Docker daemon is not running" >&2
    exit 1
fi

# ── Build command ─────────────────────────────────────────────────────────────

CMD=(uv run mini-extra swebench
    --subset verified
    --split "$SPLIT"
    -c "$CONFIG"
    -o "$OUTPUT_DIR"
    -w "$WORKERS"
)

if [[ -n "$MODEL" ]]; then
    CMD+=(-m "$MODEL")
fi

if [[ -n "$SLICE" ]]; then
    CMD+=(--slice "$SLICE")
fi

if [[ -n "$FILTER" ]]; then
    CMD+=(--filter "$FILTER")
fi

echo "Running: ${CMD[*]}"
echo ""

# ── Run ───────────────────────────────────────────────────────────────────────

mkdir -p "$OUTPUT_DIR"

# Save run metadata
cat > "$OUTPUT_DIR/run_info.json" << RUNEOF
{
    "timestamp": "$TIMESTAMP",
    "config": "$CONFIG",
    "split": "$SPLIT",
    "workers": $WORKERS,
    "model": "${MODEL:-null}",
    "slice": "${SLICE:-null}",
    "filter": "${FILTER:-null}",
    "command": "${CMD[*]}",
    "git_commit": "$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
}
RUNEOF

"${CMD[@]}" 2>&1 | tee "$OUTPUT_DIR/run.log"
EXIT_CODE=${PIPESTATUS[0]}

echo ""
echo "=== Run complete (exit code: $EXIT_CODE) ==="
echo ""

# ── Post-run analysis ─────────────────────────────────────────────────────────

exec "$0" --analyze -o "$OUTPUT_DIR"
