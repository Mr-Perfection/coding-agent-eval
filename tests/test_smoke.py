"""Smoke tests that need no external deps, network, or Docker.

Runnable two ways:
    pytest tests/test_smoke.py
    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json

from harness import metrics
from harness.compare import apply_grades
from harness.config import get_fork
from harness.vibe_agent import _parse_vibe_json, run_agent

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
    assert run.search_calls == (
        run.tool_calls["file_search"] + run.tool_calls["file_read"]
    )
    assert run.index_build_s == 0.0


def test_read_instance_ids_json_and_txt(tmp_path=None):
    import tempfile
    from harness.run_predictions import read_instance_ids

    d = Path(tempfile.mkdtemp())
    # Curated JSON format (tasks -> objects with instance_id), order preserved.
    j = d / "sub.json"
    j.write_text(json.dumps({"tasks": [
        {"instance_id": "b__2"}, {"instance_id": "a__1"}, {"instance_id": "b__2"},
    ]}))
    assert read_instance_ids(str(j)) == ["b__2", "a__1"]  # de-duped, ordered

    # Plain text list with comments/blanks.
    t = d / "sub.txt"
    t.write_text("# subset\na__1\n\nb__2  # trailing\n")
    assert read_instance_ids(str(t)) == ["a__1", "b__2"]


def test_parse_vibe_json_real_schema():
    # Mirrors `vibe --output json` (2.24.3): a top-level array of history entries.
    history = [
        {"type": "message", "role": "user", "content": []},
        {"type": "reasoning", "text": "thinking"},
        {"type": "effect", "detail": {"kind": "file_search", "tool_name": "grep"}},
        {"type": "effect", "detail": {"kind": "file_read", "tool_name": "read"}},
        {"type": "effect", "detail": {"kind": "file_read", "tool_name": "read"}},
        {"type": "message", "role": "assistant", "content": []},
        {"type": "effect", "detail": {"kind": "file_edit", "tool_name": "edit"}},
        {"type": "message", "role": "assistant", "content": []},
    ]
    out = _parse_vibe_json(json.dumps(history))
    assert out["turns"] == 2                       # two assistant messages
    assert out["tool_calls"] == {"file_search": 1, "file_read": 2, "file_edit": 1}
    # search_calls == file_search + file_read (the retrieval-flailing proxy)
    searchable = sum(v for k, v in out["tool_calls"].items() if k in metrics.SEARCH_TOOLS)
    assert searchable == 3
    # Also accept the teleport-wrapped {"history": [...]} shape.
    assert _parse_vibe_json(json.dumps({"history": history}))["turns"] == 2


def test_inproc_blob_parsing_and_python_resolution():
    # Shape emitted by harness/_vibe_inproc.py: history + usage + cost in one blob.
    blob = {
        "history": [
            {"type": "message", "role": "assistant", "content": []},
            {"type": "effect", "detail": {"kind": "file_search"}},
            {"type": "effect", "detail": {"kind": "file_edit"}},
        ],
        "usage": {"input": 1000, "output": 200, "total": 1200},
        "cost_usd": 0.0123,
        "session_id": "abc",
    }
    out = _parse_vibe_json(json.dumps(blob))
    assert out["turns"] == 1
    assert out["tool_calls"] == {"file_search": 1, "file_edit": 1}
    # _run_inproc reads usage/cost back off the retained raw blob:
    assert out["raw"]["usage"]["total"] == 1200
    assert out["raw"]["cost_usd"] == 0.0123

    # vibe_python defaults next to vibe_bin (…/bin/vibe -> …/bin/python).
    from harness.config import ForkConfig
    fc = ForkConfig(name="x", vibe_bin="venvs/x/bin/vibe")
    assert fc.resolved_vibe_python().endswith("venvs/x/bin/python")


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


def test_report_helpers():
    from harness import report
    assert 'class="c ok"' in report._f1_cell({"f1": 1.0})
    assert 'class="c bad"' in report._f1_cell({"f1": 0.0})
    assert 'class="c warn"' in report._f1_cell({"f1": 0.5})
    assert "search 2" in report._tools({"file_search": 2})
    assert report._doc("t", "<p>x</p>").startswith("<!doctype html>")
    # A hit vs miss renders as green vs red file spans.
    fhtml = report._files(
        {"localization": {"edited_files": ["a.py"], "gold_files": ["b.py"]}}, {})
    assert 'class="file bad">a.py' in fhtml and "gold:" in fhtml
    # Delta direction: fewer search (lower better) = good.
    assert "delta-good" in report._delta(10, 7, higher_better=False)
    assert "delta-bad" in report._delta(0.5, 0.2, higher_better=True)


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
