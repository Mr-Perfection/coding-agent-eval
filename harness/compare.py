"""A/B comparison: baseline fork vs. candidate (ultra-index) fork.

Joins each run's ``metrics.jsonl`` with the swebench grade report (if present),
then prints a side-by-side summary with deltas. Runs fine before grading exists —
resolution/cost-per-solve simply show as n/a and the efficiency/time/cost columns
still populate.
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

from .metrics import aggregate


def load_metrics(run_id: str) -> list[dict]:
    path = Path("runs") / run_id / "metrics.jsonl"
    if not path.exists():
        raise SystemExit(f"No metrics at {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_resolved(run_id: str) -> set[str] | None:
    """Find the swebench report for run_id and return the resolved instance ids.

    Report filename/location varies across swebench versions, so we scan a few
    likely spots and pull whichever 'resolved' key exists. None = not graded yet.
    """
    candidates = [
        *glob.glob(f"*{run_id}*.json"),
        *glob.glob(f"runs/{run_id}/*{run_id}*.json"),
        *glob.glob(f"logs/**/*{run_id}*.json", recursive=True),
    ]
    for c in candidates:
        try:
            data = json.loads(Path(c).read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for key in ("resolved_ids", "resolved", "resolved_instances"):
            if isinstance(data.get(key), list):
                return set(data[key])
    return None


def apply_grades(records: list[dict], resolved: set[str] | None) -> None:
    if resolved is None:
        return
    for r in records:
        r["resolved"] = r["instance_id"] in resolved


ROWS = [
    ("Resolution rate", "resolution_rate", "{:.1%}", True),
    ("Resolved / graded", None, None, None),  # special-cased below
    ("Mean turns", "mean_turns", "{:.2f}", False),
    ("Mean search calls", "mean_search_calls", "{:.2f}", False),
    ("Mean wall-clock (s)", "mean_wall_clock_s", "{:.1f}", False),
    ("Mean index build (s)", "mean_index_build_s", "{:.1f}", None),
    ("Mean cost ($)", "mean_cost_usd", "{:.4f}", False),
    ("Cost per solve ($)", "cost_per_solve_usd", "{:.4f}", False),
    ("Mean localization F1", "mean_localization_f1", "{:.3f}", True),
]


def _fmt(val, spec) -> str:
    return spec.format(val) if (val is not None and spec) else "n/a"


def _delta(base, cand, higher_better) -> str:
    if base is None or cand is None or higher_better is None:
        return ""
    d = cand - base
    arrow = "→"
    if d != 0:
        good = (d > 0) == higher_better
        arrow = "✅" if good else "⚠️"
    return f"  {arrow} {d:+.4f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", required=True, help="baseline run-id")
    ap.add_argument("--candidate", required=True, help="ultra-index run-id")
    args = ap.parse_args()

    runs = {}
    for label, rid in (("baseline", args.baseline), ("candidate", args.candidate)):
        recs = load_metrics(rid)
        apply_grades(recs, load_resolved(rid))
        runs[label] = (rid, recs, aggregate(recs))

    (b_id, _, b) = runs["baseline"]
    (c_id, _, c) = runs["candidate"]

    w = 22
    print(f"\n{'Metric':<{w}} {b_id:>14} {c_id:>14}   delta (cand-base)")
    print("-" * (w + 46))
    for label, key, spec, higher in ROWS:
        if key is None:  # resolved / graded counts
            bv = f"{b.get('n_resolved', 0)}/{b.get('n_graded', 0)}"
            cv = f"{c.get('n_resolved', 0)}/{c.get('n_graded', 0)}"
            print(f"{label:<{w}} {bv:>14} {cv:>14}")
            continue
        bv, cv = b.get(key), c.get(key)
        print(f"{label:<{w}} {_fmt(bv, spec):>14} {_fmt(cv, spec):>14}"
              f"{_delta(bv, cv, higher)}")
    print(f"\nn: baseline={b['n']}  candidate={c['n']}")
    print("(index build time is reported separately and NOT added to wall-clock)")


if __name__ == "__main__":
    main()
