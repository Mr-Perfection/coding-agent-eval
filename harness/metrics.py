"""Pure metric functions: patch parsing, localization overlap, run aggregation.

These operate on the normalized per-instance records emitted by ``run_predictions``
(see its module docstring for the schema) and on unified-diff patch text.
"""

from __future__ import annotations

import re
from statistics import mean
from typing import Iterable

# Effect kinds / tool names that mean the agent is *searching* for the right code
# rather than editing it. This is the signal indexing should shrink most.
# vibe emits effect `detail.kind` values (file_search = grep-like, file_read = open
# a file to look). The rest are legacy/mock aliases kept so both vocabularies count.
SEARCH_TOOLS = {
    "file_search", "file_read",          # real vibe effect kinds
    "grep", "read", "glob", "search", "ls", "find", "cat",  # aliases / mock
}

_DIFF_GIT = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)
_PLUSPLUS = re.compile(r"^\+\+\+ b/(.+?)(?:\t.*)?$", re.MULTILINE)


def files_from_patch(patch: str) -> set[str]:
    """Set of file paths touched by a unified diff."""
    if not patch:
        return set()
    files = {m.group(2) for m in _DIFF_GIT.finditer(patch)}
    files |= {m.group(1) for m in _PLUSPLUS.finditer(patch)}
    files.discard("/dev/null")
    return files


def localization(gold_patch: str, model_patch: str) -> dict:
    """File-level precision/recall/F1 of the agent's edits vs. the gold patch.

    This is the metric most directly sensitive to retrieval/index quality: did the
    agent even edit the right files, independent of whether the fix was correct?
    """
    gold = files_from_patch(gold_patch)
    edited = files_from_patch(model_patch)
    if not edited and not gold:
        return _loc(gold, edited, 1.0, 1.0, 1.0)
    hit = gold & edited
    precision = len(hit) / len(edited) if edited else 0.0
    recall = len(hit) / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return _loc(gold, edited, precision, recall, f1)


def _loc(gold, edited, p, r, f1) -> dict:
    return {
        "gold_files": sorted(gold),
        "edited_files": sorted(edited),
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1, 4),
    }


def _safe_mean(xs: list[float]) -> float | None:
    xs = [x for x in xs if x is not None]
    return round(mean(xs), 4) if xs else None


def aggregate(records: Iterable[dict]) -> dict:
    """Roll up per-instance records into run-level summary stats."""
    recs = list(records)
    n = len(recs)
    if n == 0:
        return {"n": 0}

    solved = [r for r in recs if r.get("resolved") is True]
    n_solved = len(solved)
    graded = [r for r in recs if r.get("resolved") is not None]

    total_cost = sum(r.get("cost_usd") or 0.0 for r in recs)

    return {
        "n": n,
        "n_graded": len(graded),
        "resolution_rate": round(n_solved / len(graded), 4) if graded else None,
        "n_resolved": n_solved,
        # Efficiency
        "mean_turns": _safe_mean([r.get("turns") for r in recs]),
        "mean_search_calls": _safe_mean([r.get("search_calls") for r in recs]),
        # Time (agent subprocess; passive index builds fold into wall-clock — see AgentRun)
        "mean_wall_clock_s": _safe_mean([r.get("wall_clock_s") for r in recs]),
        "mean_index_build_s": _safe_mean([r.get("index_build_s") for r in recs]),
        # Cost
        "mean_cost_usd": _safe_mean([r.get("cost_usd") for r in recs]),
        "total_cost_usd": round(total_cost, 4),
        "cost_per_solve_usd": round(total_cost / n_solved, 4) if n_solved else None,
        # Localization
        "mean_localization_f1": _safe_mean(
            [(r.get("localization") or {}).get("f1") for r in recs]
        ),
    }
