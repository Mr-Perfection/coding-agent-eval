"""Smoke tests that need no external deps, network, or Docker.

Runnable two ways:
    pytest tests/test_smoke.py
    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import metrics
from harness.compare import apply_grades
from harness.config import get_fork
from harness.vibe_agent import run_agent

GOLD = """diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,3 +1,3 @@
-def f():
-    return 1
+def f():
+    return 2
"""


def test_files_from_patch():
    assert metrics.files_from_patch(GOLD) == {"pkg/core.py"}
    assert metrics.files_from_patch("") == set()


def test_localization_perfect_and_miss():
    perfect = metrics.localization(GOLD, GOLD)
    assert perfect["precision"] == perfect["recall"] == perfect["f1"] == 1.0

    wrong = GOLD.replace("pkg/core.py", "pkg/other.py")
    miss = metrics.localization(GOLD, wrong)
    assert miss["f1"] == 0.0


def test_mock_agent_returns_gold_patch():
    fork = get_fork("mock")
    inst = {"instance_id": "demo__repo-1", "problem_statement": "fix f",
            "patch": GOLD}
    run = run_agent(fork, inst, repo_path=None)
    assert run.patch == GOLD
    assert run.turns and run.turns > 0
    assert run.search_calls == run.tool_calls["grep"] + run.tool_calls["read"]
    assert run.index_build_s == 0.0


def test_aggregate_and_grades():
    records = []
    for iid in ("a__r-1", "b__r-2", "c__r-3"):
        inst = {"instance_id": iid, "problem_statement": "x", "patch": GOLD}
        run = run_agent(get_fork("mock"), inst, repo_path=None)
        records.append({
            "instance_id": iid, "resolved": None,
            "turns": run.turns, "search_calls": run.search_calls,
            "wall_clock_s": run.wall_clock_s, "index_build_s": run.index_build_s,
            "cost_usd": run.cost_usd,
            "localization": metrics.localization(GOLD, run.patch),
        })

    # Grade two of three as resolved.
    apply_grades(records, resolved={"a__r-1", "b__r-2"})
    agg = metrics.aggregate(records)
    assert agg["n"] == 3
    assert agg["n_graded"] == 3
    assert agg["n_resolved"] == 2
    assert agg["resolution_rate"] == round(2 / 3, 4)
    assert agg["cost_per_solve_usd"] is not None
    assert agg["mean_localization_f1"] == 1.0

    # Ungraded run: resolution stays None, efficiency still computes.
    ungraded = metrics.aggregate([{**r, "resolved": None} for r in records])
    assert ungraded["resolution_rate"] is None
    assert ungraded["mean_turns"] is not None


def _main():
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✅ {name}")
            passed += 1
    print(f"\n{passed} tests passed")


if __name__ == "__main__":
    _main()
