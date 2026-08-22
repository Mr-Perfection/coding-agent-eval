"""Generate SWE-bench predictions + metrics for one fork.

Writes two files under ``runs/<run-id>/``:

* ``predictions.jsonl`` — the input the official grader consumes. One line per
  instance: ``{"instance_id", "model_patch", "model_name_or_path"}``.
* ``metrics.jsonl`` — our richer per-instance telemetry (schema below), later
  joined with grades by ``compare``.

metrics.jsonl record schema::

    {
      "instance_id": str,
      "fork": str,
      "resolved": null,          # filled in by compare/grade join
      "wall_clock_s": float,     # agent subprocess time; for the ultra-index fork
                                 # this INCLUDES the passive index build (see AgentRun)
      "index_build_s": float,    # out-of-band build only (0 for passive/none)
      "turns": int | null,
      "tool_calls": {name: count},
      "search_calls": int,       # grep/read/glob... -> "flailing" proxy
      "tokens": {input, output, total},
      "cost_usd": float | null,
      "patch_nonempty": bool,
      "localization": {gold_files, edited_files, precision, recall, f1}
    }
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import repos
from .config import get_fork
from .metrics import localization
from .vibe_agent import run_agent

SPLITS = {
    "lite": "princeton-nlp/SWE-bench_Lite",
    "verified": "princeton-nlp/SWE-bench_Verified",
    "full": "princeton-nlp/SWE-bench",
}


def read_instance_ids(path: str) -> list[str]:
    """Read a curated subset file. Accepts either:

    * a ``tasks/*.json`` with a top-level ``tasks`` list of objects carrying
      ``instance_id`` (our curated format), or a bare JSON list of ids/objects, or
    * a plain text file with one id per line (``#`` comments and blanks ignored).

    Order is preserved (defines run order); duplicates are dropped.
    """
    text = Path(path).read_text()
    ids: list[str] = []
    if path.endswith(".json"):
        data = json.loads(text)
        items = data.get("tasks", data) if isinstance(data, dict) else data
        for it in items:
            ids.append(it if isinstance(it, str) else (
                it.get("instance_id") or it.get("id")))
    else:
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                ids.append(line)
    seen: set[str] = set()
    return [i for i in ids if i and not (i in seen or seen.add(i))]


def load_instances(
    split: str,
    dataset: str | None,
    limit: int | None,
    only_ids: list[str] | None = None,
) -> list[dict]:
    from datasets import load_dataset  # imported lazily so --help needs no deps

    name = dataset or SPLITS[split]
    ds = load_dataset(name, split="test")
    rows = [dict(r) for r in ds]

    if only_ids:
        by_id = {r["instance_id"]: r for r in rows}
        missing = [i for i in only_ids if i not in by_id]
        if missing:
            raise SystemExit(
                f"{len(missing)} instance id(s) not in {name}: {', '.join(missing)}"
            )
        rows = [by_id[i] for i in only_ids]  # preserve curated order
    return rows[:limit] if limit else rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fork", required=True, help="baseline | ultra-index | mock")
    ap.add_argument("--split", default="lite", choices=SPLITS)
    ap.add_argument("--dataset", default=None, help="override HF dataset name")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--instances", default=None,
                    help="comma-separated instance ids to run (subset)")
    ap.add_argument("--instances-file", default=None,
                    help="path to a subset file: tasks/*.json or a newline id list")
    ap.add_argument("--max-turns", type=int, default=None,
                    help="per-task turn cap (else fork default, currently 100)")
    ap.add_argument("--max-price", type=float, default=None,
                    help="per-task $ cap (else fork default, currently 3.00)")
    ap.add_argument("--run-id", required=True)
    args = ap.parse_args()

    only_ids: list[str] | None = None
    if args.instances_file:
        only_ids = read_instance_ids(args.instances_file)
    if args.instances:
        cli_ids = [s.strip() for s in args.instances.split(",") if s.strip()]
        only_ids = (only_ids or []) + cli_ids

    fork = get_fork(args.fork)
    out_dir = Path("runs") / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pred_f = (out_dir / "predictions.jsonl").open("w")
    met_f = (out_dir / "metrics.jsonl").open("w")

    instances = load_instances(args.split, args.dataset, args.limit, only_ids)
    print(f"[{args.run_id}] fork={fork.name} model={fork.model} "
          f"instances={len(instances)}")

    for i, inst in enumerate(instances, 1):
        iid = inst["instance_id"]
        # Mock returns the gold patch directly, so it needs no repo checkout.
        repo_path = None if fork.backend == "mock" else repos.checkout(
            inst["repo"], inst["base_commit"]
        )
        try:
            run = run_agent(fork, inst, repo_path, args.max_turns, args.max_price)
        except Exception as e:  # keep the batch going; record the failure
            print(f"  [{i}/{len(instances)}] {iid} ERROR: {e}")
            run = None

        patch = run.patch if run else ""
        pred_f.write(json.dumps({
            "instance_id": iid,
            "model_patch": patch,
            "model_name_or_path": f"vibe-{fork.name}",
        }) + "\n")

        record = {
            "instance_id": iid,
            "fork": fork.name,
            "resolved": None,
            "wall_clock_s": run.wall_clock_s if run else None,
            "index_build_s": run.index_build_s if run else None,
            "turns": run.turns if run else None,
            "tool_calls": run.tool_calls if run else {},
            "search_calls": run.search_calls if run else None,
            "tokens": run.tokens if run else {},
            "cost_usd": run.cost_usd if run else None,
            "timed_out": run.timed_out if run else None,
            "patch_nonempty": bool(patch.strip()),
            "localization": localization(inst.get("patch", ""), patch),
        }
        met_f.write(json.dumps(record) + "\n")
        print(f"  [{i}/{len(instances)}] {iid} "
              f"turns={record['turns']} search={record['search_calls']} "
              f"t={record['wall_clock_s']}s loc_f1={record['localization']['f1']}")

    pred_f.close()
    met_f.close()
    print(f"Wrote {out_dir}/predictions.jsonl and metrics.jsonl")


if __name__ == "__main__":
    main()
