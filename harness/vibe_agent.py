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
import tempfile
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
    timed_out: bool = False             # agent hit fork.timeout_s and was killed
    raw: dict | None = None             # raw transcript, kept for debugging

    @property
    def search_calls(self) -> int:
        return sum(v for k, v in self.tool_calls.items() if k in SEARCH_TOOLS)


def _run_with_timeout(cmd: list[str], cwd: Path, timeout_s: int | None,
                      label: str) -> tuple[str, str, bool]:
    """Run `cmd`, returning (stdout, stderr, timed_out).

    A timeout is not an error here: the agent may have already edited files, and
    the patch comes from the repo's git diff rather than from stdout. So we kill
    the child, report it, and let the caller harvest whatever landed on disk.
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout_s)
        return proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as e:
        def _dec(b):  # capture_output=True gives bytes on the exception path
            return b.decode(errors="replace") if isinstance(b, bytes) else (b or "")
        print(f"    [{label}] TIMEOUT after {timeout_s}s — killed; "
              f"keeping partial git diff")
        return _dec(e.stdout), _dec(e.stderr), True


def run_agent(fork: ForkConfig, instance: dict, repo_path: Path,
              max_turns: int | None = None, max_price: float | None = None) -> AgentRun:
    if fork.backend == "mock":
        return _run_mock(instance, repo_path)
    mt = max_turns if max_turns is not None else fork.max_turns
    mp = max_price if max_price is not None else fork.max_price
    if fork.backend == "inproc":
        return _run_inproc(fork, instance, repo_path, mt, mp)
    return _run_cli(fork, instance, repo_path, mt, mp)


_ADAPTER = str(Path(__file__).with_name("_vibe_inproc.py"))


def _build_index(fork: ForkConfig, repo_path: Path) -> float:
    """Run + time the per-repo index build. Timed separately so it never inflates
    per-task wall-clock. Returns seconds (0.0 if the fork has no index step)."""
    if not fork.index_build_cmd:
        return 0.0
    vibe_abs = str(Path(fork.vibe_bin).absolute()) if fork.vibe_bin else ""
    cmd = fork.index_build_cmd.format(repo=str(repo_path), vibe=vibe_abs)
    t0 = time.perf_counter()
    subprocess.run(cmd, shell=True, cwd=repo_path, check=True,
                   capture_output=True, text=True, timeout=fork.timeout_s)
    return round(time.perf_counter() - t0, 3)


# ------------------------------------------------------------------------ inproc

def _run_inproc(fork: ForkConfig, instance: dict, repo_path: Path,
                max_turns: int | None, max_price: float | None) -> AgentRun:
    """Preferred backend: run the agent via harness/_vibe_inproc.py with the fork's
    python, capturing token usage + cost (not available from the plain CLI)."""
    py = fork.resolved_vibe_python()
    if not py or not Path(py).exists():
        raise SystemExit(
            f"fork '{fork.name}' python not found: {py}\n"
            f"Install the fork into its venv, or use --fork mock / backend 'cli'."
        )
    # Absolute (subprocess runs with cwd=<repo>) but WITHOUT resolving symlinks:
    # venvs/<fork>/bin/python is a symlink to the base interpreter, and following
    # it would bypass the venv (where `vibe` is installed). .absolute() keeps the
    # venv path; .resolve() would break it.
    py = str(Path(py).absolute())

    index_build_s = _build_index(fork, repo_path)

    # Prompt goes via a temp file — SWE-bench problem statements are large and would
    # be awkward/fragile as a shell argument.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(instance["problem_statement"])
        prompt_file = f.name
    cmd = [py, _ADAPTER, "--prompt-file", prompt_file, "--workdir", str(repo_path)]
    # Omit entirely when None: the adapter's own default is None (uncapped).
    if max_turns is not None:
        cmd += ["--max-turns", str(max_turns)]
    if max_price is not None:
        cmd += ["--max-price", str(max_price)]
    t0 = time.perf_counter()
    stdout, stderr, timed_out = _run_with_timeout(
        cmd, repo_path, fork.timeout_s, f"inproc:{instance['instance_id']}")
    wall_clock_s = round(time.perf_counter() - t0, 3)
    Path(prompt_file).unlink(missing_ok=True)

    parsed = _parse_vibe_json(stdout)
    blob = parsed.get("raw") if isinstance(parsed.get("raw"), dict) else {}
    if stderr.strip():
        print(f"    [inproc:{instance['instance_id']}] {stderr.strip()[:300]}")

    # Prefer vibe's own step count (agent iterations) over assistant-message count,
    # which undercounts badly (many tool calls land under one assistant message).
    steps = blob.get("steps")
    return AgentRun(
        patch=repos.diff(repo_path),
        wall_clock_s=wall_clock_s,
        index_build_s=index_build_s,
        turns=steps if steps is not None else parsed.get("turns"),
        tool_calls=parsed.get("tool_calls", {}),
        tokens=blob.get("usage") or {},
        cost_usd=blob.get("cost_usd"),
        timed_out=timed_out,
        raw=parsed.get("raw"),
    )


# --------------------------------------------------------------------------- CLI

def _run_cli(fork: ForkConfig, instance: dict, repo_path: Path,
             max_turns: int | None, max_price: float | None) -> AgentRun:
    """Raw `vibe` binary backend. History only — no token/cost (see _run_inproc)."""
    if not fork.vibe_bin or not Path(fork.vibe_bin).exists():
        raise SystemExit(
            f"vibe executable not found for fork '{fork.name}': {fork.vibe_bin}\n"
            f"Install the fork into its venv, or use --fork mock to test the pipeline."
        )
    vibe_bin = str(Path(fork.vibe_bin).absolute())  # absolute, keep venv symlink

    index_build_s = _build_index(fork, repo_path)

    # NOTE: vibe has no --model flag; the model is pinned via `active_model` in
    # ~/.vibe/config.toml (see README). --trust is required or vibe ignores the
    # repo's project config and warns. --yolo auto-approves all tool calls.
    cmd = [
        vibe_bin,
        "--prompt", instance["problem_statement"],
        "--yolo",
        "--trust",
        "--workdir", str(repo_path),
        "--output", "json",
        *(["--max-turns", str(max_turns)] if max_turns is not None else []),
        *(["--max-price", str(max_price)] if max_price is not None else []),
        *fork.extra_args,
    ]
    t0 = time.perf_counter()
    stdout, _stderr, timed_out = _run_with_timeout(
        cmd, repo_path, fork.timeout_s, f"cli:{instance['instance_id']}")
    wall_clock_s = time.perf_counter() - t0

    parsed = _parse_vibe_json(stdout)
    return AgentRun(
        patch=repos.diff(repo_path),
        wall_clock_s=round(wall_clock_s, 3),
        index_build_s=index_build_s,
        turns=parsed.get("turns"),
        tool_calls=parsed.get("tool_calls", {}),
        tokens=parsed.get("tokens", {}),
        cost_usd=parsed.get("cost_usd"),
        timed_out=timed_out,
        raw=parsed.get("raw"),
    )


def _parse_vibe_json(stdout: str) -> dict:
    """Normalize `vibe --output json` (verified against vibe 2.24.3).

    The output is a JSON *array* of history entries (or ``{"history": [...],
    "teleportUrl": ...}`` when teleporting). Each entry has a ``type``:

      * ``message``  -> role in {system,user,assistant}; assistant msgs ~= turns
      * ``effect``   -> a tool call; ``detail.kind`` is the tool kind
                        (file_search, file_read, file_edit, file_write, shell,
                         tool, web_search, subagent, ...) and ``detail.tool_name``
                        is the concrete tool name
      * ``reasoning`` / ``callback`` -> ignored for counting

    Token/cost totals are NOT in this array (they live in session stats), so we
    leave cost_usd/tokens empty here. `--max-price` still bounds spend; sourcing
    exact cost from the session log is a follow-up (see README).
    """
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return {"raw": None}

    history = data.get("history") if isinstance(data, dict) else data
    if not isinstance(history, list):
        return {"raw": data}

    turns = 0
    tool_calls: dict[str, int] = {}
    for entry in history:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype == "message" and entry.get("role") == "assistant":
            turns += 1
        elif etype == "effect":
            detail = entry.get("detail") or {}
            kind = detail.get("kind") or detail.get("tool_name") or "unknown"
            tool_calls[kind] = tool_calls.get(kind, 0) + 1

    return {
        "turns": turns or None,
        "tool_calls": tool_calls,
        "tokens": {},        # not present in programmatic JSON output
        "cost_usd": None,    # sourced from session stats later; --max-price bounds it
        "raw": data,
    }


# -------------------------------------------------------------------------- Mock

def _run_mock(instance: dict, repo_path: Path) -> AgentRun:
    """Synthetic agent: returns the gold patch with deterministic fake telemetry.

    Lets the full pipeline (predictions -> grade -> compare) be validated with no
    vibe install and no model calls. Grading should mark these 'resolved'.
    """
    seed = int(hashlib.sha1(instance["instance_id"].encode()).hexdigest(), 16)
    file_search = 3 + seed % 6
    file_read = 2 + (seed >> 4) % 5
    file_edit = 1 + (seed >> 8) % 2
    turns = file_search + file_read + file_edit + 2
    return AgentRun(
        patch=instance.get("patch", ""),  # gold patch -> should resolve
        wall_clock_s=round(20 + (seed % 40) + turns * 1.5, 3),
        index_build_s=0.0,
        turns=turns,
        tool_calls={"file_search": file_search, "file_read": file_read,
                    "file_edit": file_edit},
        tokens={"input": 8000 + seed % 4000, "output": 1500 + seed % 1000,
                "total": 9500 + seed % 5000},
        cost_usd=round(0.05 + (seed % 20) / 100, 4),
        raw={"mock": True},
    )
