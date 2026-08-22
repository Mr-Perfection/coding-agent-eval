#!/usr/bin/env bash
# run_eval.sh — one entry point for the mistral-vibe indexing eval.
#
# Examples:
#   ./run_eval.sh                              # baseline over the 3-task difficulty ladder
#   ./run_eval.sh --fork mock                  # free pipeline check (no API calls)
#   ./run_eval.sh --fork baseline --max-price 0.50
#   ./run_eval.sh --fork ultra-index --run-id idx_v1
#   ./run_eval.sh --fork baseline --no-grade   # skip Docker grading (grade is default)
#   ./run_eval.sh --compare base_v1 idx_v1     # A/B two finished runs
#
# Flags:
#   --fork NAME         baseline | ultra-index | mock         (default: baseline)
#   --run-id ID         output dir runs/ID          (default: <fork>_<timestamp>)
#   --subset FILE       task subset file            (default: tasks/difficulty_ladder.json)
#   --instances A,B     ad-hoc instance ids (overrides --subset)
#   --limit N           cap number of tasks
#   --max-price P       per-task $ cap (default: UNCAPPED)
#   --max-turns N       per-task turn cap (default: UNCAPPED)
#   --no-grade          skip grading (grading runs by DEFAULT; needs Docker + swebench)
#   --cache-level LVL   docker image cache: none|base|env|instance (default: instance)
#                       'instance' keeps the built repo image so reruns skip the git clone
#   --compare A B       skip running; just print the A/B table for runs A and B
set -euo pipefail

cd "$(dirname "$0")"
PY=.venv/bin/python
[ -x "$PY" ] || { echo "ERROR: $PY missing. Run:  uv venv .venv --python 3.12 && uv pip install --python $PY datasets"; exit 1; }

FORK=baseline
RUN_ID=""
SUBSET="tasks/difficulty_ladder.json"
INSTANCES=""
LIMIT=""
MAX_PRICE=""
MAX_TURNS=""
GRADE=1   # grade by default; --no-grade to skip (mock is auto-skipped)
CACHE_LEVEL=instance   # keep instance images: reruns skip the per-instance git clone
OPEN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --fork)      FORK="$2"; shift 2;;
    --run-id)    RUN_ID="$2"; shift 2;;
    --subset)    SUBSET="$2"; shift 2;;
    --instances) INSTANCES="$2"; shift 2;;
    --limit)     LIMIT="$2"; shift 2;;
    --max-price) MAX_PRICE="$2"; shift 2;;
    --max-turns) MAX_TURNS="$2"; shift 2;;
    --grade)     GRADE=1; shift;;
    --no-grade)  GRADE=0; shift;;
    --cache-level) CACHE_LEVEL="$2"; shift 2;;
    --compare)   "$PY" -m harness.compare --baseline "$2" --candidate "$3"
                 "$PY" -m harness.report --baseline "$2" --candidate "$3"
                 echo "open: file://$(pwd)/runs/compare_$2_$3.html"; exit 0;;
    --open)      OPEN=1; shift;;
    -h|--help)   sed -n '2,23p' "$0"; exit 0;;
    *) echo "Unknown flag: $1"; exit 1;;
  esac
done

[ -n "$RUN_ID" ] || RUN_ID="${FORK}_$(date +%Y%m%d_%H%M%S)"

# Pre-flight for real forks.
if [ "$FORK" != "mock" ]; then
  VBIN="venvs/${FORK}/bin/vibe"
  [ -x "$VBIN" ] || { echo "ERROR: $VBIN not found. Install the fork:"; \
    echo "  uv venv venvs/${FORK} --python 3.12 && uv pip install --python venvs/${FORK}/bin/python 'git+https://github.com/mistralai/mistral-vibe'"; exit 1; }
  if ! grep -q 'active_model' "$HOME/.vibe/config.toml" 2>/dev/null; then
    echo "WARN: no 'active_model' pinned in ~/.vibe/config.toml — vibe will use its default model."
    echo "      Pin the SAME model before running both forks for a fair A/B."
  fi
fi

# Build the run command.
ARGS=(--fork "$FORK" --run-id "$RUN_ID")
if [ -n "$INSTANCES" ]; then ARGS+=(--instances "$INSTANCES");
else ARGS+=(--instances-file "$SUBSET"); fi
[ -n "$LIMIT" ]     && ARGS+=(--limit "$LIMIT")
[ -n "$MAX_PRICE" ] && ARGS+=(--max-price "$MAX_PRICE")
[ -n "$MAX_TURNS" ] && ARGS+=(--max-turns "$MAX_TURNS")

echo "==> Running: fork=$FORK run-id=$RUN_ID"
"$PY" -m harness.run_predictions "${ARGS[@]}"

# Grading (Docker + swebench) - default on; skipped for the mock fork.
if [ "$GRADE" -eq 1 ] && [ "$FORK" != "mock" ]; then
  # Pin swebench<4: 4.x/5.x switched to a registry-image model that needs an
  # `image` field the classic SWE-bench_Lite rows don't carry. 3.x builds from source.
  "$PY" -c "import swebench,sys; sys.exit(0 if int(swebench.__version__.split('.')[0])<4 else 1)" 2>/dev/null \
    || uv pip install --python "$PY" 'swebench<4'
  echo "==> Grading (Docker, no model — runs the real test suite) run-id=$RUN_ID"
  "$PY" -m harness.grade --run-id "$RUN_ID" --cache-level "$CACHE_LEVEL" || echo "WARN: grading failed (is Docker running?)"
fi

# Summary.
echo
echo "==> Summary  runs/$RUN_ID/metrics.jsonl"
"$PY" - "$RUN_ID" "$SUBSET" <<'PY'
import json, sys
from pathlib import Path
from harness.metrics import aggregate
from harness.compare import load_resolved, apply_grades
rid = sys.argv[1]
subset_path = sys.argv[2] if len(sys.argv) > 2 else "tasks/difficulty_ladder.json"
recs = [json.loads(l) for l in (Path("runs")/rid/"metrics.jsonl").read_text().splitlines() if l.strip()]
apply_grades(recs, load_resolved(rid))  # merge swebench verdict (if graded) at read-time
print(f"  {'instance':34s} {'bucket':9s} loc_f1 turns search  cost      time")
# bucket lookup from the curated subset, if present
buckets = {}
sub = Path(subset_path)
if sub.exists():
    for t in json.loads(sub.read_text()).get("tasks", []):
        buckets[t["instance_id"]] = t.get("bucket", "")
for r in recs:
    b = buckets.get(r["instance_id"], "")
    cost = "n/a" if r.get("cost_usd") is None else f"${r['cost_usd']:.4f}"
    print(f"  {r['instance_id']:34s} {b:9s} {r['localization']['f1']:.2f}   "
          f"{str(r['turns']):>4s}  {str(r['search_calls']):>4s}  {cost:>8s}  {r['wall_clock_s']}s")
a = aggregate(recs)
print(f"\n  n={a['n']}  mean_turns={a['mean_turns']}  mean_search={a['mean_search_calls']}"
      f"  mean_cost={a['mean_cost_usd']}  total_cost={a['total_cost_usd']}"
      f"  mean_loc_f1={a['mean_localization_f1']}")
if a.get('resolution_rate') is not None:
    print(f"  resolution_rate={a['resolution_rate']}  resolved={a['n_resolved']}/{a['n_graded']}"
          f"  cost_per_solve={a['cost_per_solve_usd']}")
else:
    print("  (not graded yet — run with --grade for resolution_rate & cost_per_solve)")
PY

# Readable HTML report.
"$PY" -m harness.report --run-id "$RUN_ID" >/dev/null
REPORT="$(pwd)/runs/$RUN_ID/report.html"
echo
echo "==> HTML report: file://$REPORT"
[ "$OPEN" -eq 1 ] && open "$REPORT" 2>/dev/null || true
echo "Next: ./run_eval.sh --fork ultra-index --run-id <id>   then   ./run_eval.sh --compare $RUN_ID <id>"
