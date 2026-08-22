"""Agent runner: drive one vibe fork (or the mock) on a single instance.

This is the main seam between the harness and vibe. ``run_agent`` returns a
normalized ``AgentRun`` regardless of backend, so the rest of the pipeline never
depends on vibe's exact CLI/JSON shape — only ``_parse_vibe_json`` does, and that
is the one place to adjust when the real fork's output is known.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import repos
from .config import ForkConfig
from .metrics import SEARCH_TOOLS


@dataclass
class AgentRun:
    patch: str                          # unified diff = the model_patch
    wall_clock_s: float                 # agent time ONLY (excludes index build)
    index_build_s: float = 0.0          # one-time per repo; 0 if none
    turns: int | None = None
    tool_calls: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    raw: dict | None = None             # raw transcript, kept for debugging

    @property
    def search_calls(self) -> int:
        return sum(v for k, v in self.tool_calls.items() if k in SEARCH_TOOLS)


def run_agent(fork: ForkConfig, instance: dict, repo_path: Path) -> AgentRun:
    if fork.backend == "mock":
        return _run_mock(instance, repo_path)
    return _run_cli(fork, instance, repo_path)


# --------------------------------------------------------------------------- CLI

def _run_cli(fork: ForkConfig, instance: dict, repo_path: Path) -> AgentRun:
    if not fork.vibe_bin or not Path(fork.vibe_bin).exists():
        raise SystemExit(
            f"vibe executable not found for fork '{fork.name}': {fork.vibe_bin}\n"
            f"Install the fork into its venv, or use --fork mock to test the pipeline."
        )

    # 1. Optional index build — timed separately so it never inflates task time.
    index_build_s = 0.0
    if fork.index_build_cmd:
        cmd = fork.index_build_cmd.format(repo=str(repo_path), vibe=fork.vibe_bin)
        t0 = time.perf_counter()
        subprocess.run(cmd, shell=True, cwd=repo_path, check=True,
                       capture_output=True, text=True)
        index_build_s = time.perf_counter() - t0

    # 2. Run the agent headless.
    cmd = [
        fork.vibe_bin,
        "--prompt", instance["problem_statement"],
        "--yolo",
        "--workdir", str(repo_path),
        "--output", "json",
        "--model", fork.model,
        "--max-turns", str(fork.max_turns),
        "--max-price", str(fork.max_price),
        *fork.extra_args,
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True)
    wall_clock_s = time.perf_counter() - t0

    parsed = _parse_vibe_json(proc.stdout)
    patch = repos.diff(repo_path)

    return AgentRun(
        patch=patch,
        wall_clock_s=round(wall_clock_s, 3),
        index_build_s=round(index_build_s, 3),
        turns=parsed.get("turns"),
        tool_calls=parsed.get("tool_calls", {}),
        tokens=parsed.get("tokens", {}),
        cost_usd=parsed.get("cost_usd"),
        raw=parsed.get("raw"),
    )


def _parse_vibe_json(stdout: str) -> dict:
    """Best-effort normalization of `vibe --output json`.

    ADJUST HERE once the real fork's JSON schema is confirmed. Kept tolerant so a
    schema change degrades to missing metrics rather than a crash.
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {"raw": None}

    # Tool-call tally: look for a list of steps/messages carrying tool names.
    tool_calls: dict[str, int] = {}
    steps = data.get("messages") or data.get("steps") or data.get("turns") or []
    for step in steps if isinstance(steps, list) else []:
        name = None
        if isinstance(step, dict):
            name = step.get("tool") or step.get("tool_name") or (
                (step.get("tool_call") or {}).get("name")
                if isinstance(step.get("tool_call"), dict) else None
            )
        if name:
            tool_calls[name] = tool_calls.get(name, 0) + 1

    usage = data.get("usage") or data.get("tokens") or {}
    tokens = {
        "input": usage.get("input_tokens") or usage.get("input"),
        "output": usage.get("output_tokens") or usage.get("output"),
        "total": usage.get("total_tokens") or usage.get("total"),
    }

    return {
        "turns": data.get("num_turns") or data.get("turns_used")
        or (len(steps) if isinstance(steps, list) else None),
        "tool_calls": tool_calls,
        "tokens": {k: v for k, v in tokens.items() if v is not None},
        "cost_usd": data.get("cost_usd") or data.get("total_cost")
        or data.get("price"),
        "raw": data,
    }


# -------------------------------------------------------------------------- Mock

def _run_mock(instance: dict, repo_path: Path) -> AgentRun:
    """Synthetic agent: returns the gold patch with deterministic fake telemetry.

    Lets the full pipeline (predictions -> grade -> compare) be validated with no
    vibe install and no model calls. Grading should mark these 'resolved'.
    """
    seed = int(hashlib.sha1(instance["instance_id"].encode()).hexdigest(), 16)
    grep = 3 + seed % 6
    read = 2 + (seed >> 4) % 5
    edit = 1 + (seed >> 8) % 2
    turns = grep + read + edit + 2
    return AgentRun(
        patch=instance.get("patch", ""),  # gold patch -> should resolve
        wall_clock_s=round(20 + (seed % 40) + turns * 1.5, 3),
        index_build_s=0.0,
        turns=turns,
        tool_calls={"grep": grep, "read": read, "edit": edit},
        tokens={"input": 8000 + seed % 4000, "output": 1500 + seed % 1000,
                "total": 9500 + seed % 5000},
        cost_usd=round(0.05 + (seed % 20) / 100, 4),
        raw={"mock": True},
    )
