"""Thin wrapper over the official swebench grader (Docker required).

Runs the harness on ``runs/<run-id>/predictions.jsonl`` and leaves the standard
``<model>.<run-id>.json`` report in the repo root (that's where swebench writes it),
which ``compare`` then reads.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SPLITS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--split", default="lite", choices=SPLITS)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--max-workers", type=int, default=4)
    args = ap.parse_args()

    preds = Path("runs") / args.run_id / "predictions.jsonl"
    if not preds.exists():
        sys.exit(f"No predictions at {preds}. Run run_predictions first.")

    cmd = [
        sys.executable, "-m", "swebench.harness.run_evaluation",
        "--dataset_name", args.dataset or SPLITS[args.split],
        "--predictions_path", str(preds),
        "--max_workers", str(args.max_workers),
        "--run_id", args.run_id,
    ]
    print("Grading (Docker):", " ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
