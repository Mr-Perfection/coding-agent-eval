"""Fork configuration for the A/B comparison.

A ``ForkConfig`` describes how to invoke one build of vibe. The two real forks
(``baseline`` and ``ultra-index``) MUST pin the same model so that any measured
delta is attributable to the indexing feature, not the model.

The ``mock`` fork uses a synthetic in-process backend (see ``vibe_agent``) so the
whole pipeline can be exercised before any real fork is installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Pin BOTH real forks to the same model so deltas reflect the index, not the model.
# vibe has NO --model CLI flag: pin it in ~/.vibe/config.toml via `active_model`
# (must match a key under [models]). This constant is recorded in metrics for
# traceability but is not passed on the command line.
PINNED_MODEL = "mistral-medium-3.5"


@dataclass(frozen=True)
class ForkConfig:
    name: str
    # Path to the fork's `vibe` executable (typically inside its own venv).
    vibe_bin: str | None = None
    # Path to the fork's python (same venv). Used by the "inproc" backend to run
    # harness/_vibe_inproc.py, which captures token usage + cost. Defaults next to
    # vibe_bin (…/bin/vibe -> …/bin/python) when left None.
    vibe_python: str | None = None
    model: str = PINNED_MODEL
    # Extra CLI args appended to every headless invocation ("cli" backend only).
    extra_args: tuple[str, ...] = ()
    # Optional shell command, run + timed once per repo before the agent starts,
    # to build/refresh the index. Runs with cwd = the checked-out repo.
    # Placeholders: {repo} = repo path, {vibe} = vibe_bin. None = no index step.
    index_build_cmd: str | None = None
    # Turn / spend caps for headless runs (overridable per run via CLI flags).
    # None = uncapped: vibe only installs TurnLimitMiddleware / PriceLimitMiddleware
    # when the value is not None (core/agent_loop/_loop.py).
    #
    # These are sized to NOT bind on a healthy run — they are runaway guards, not
    # budget knobs. A binding cap censors the very signal an indexing A/B measures:
    # at max_turns=40 all 3 ladder tasks stopped at the cap, one of them (12589)
    # having made 22 reads and zero edits, so its "empty patch" measured the cap
    # rather than the agent. Re-derive these from a calibration run rather than
    # trusting them blindly; see README.
    #
    # Cost is ~99% input tokens (history resent every turn), so spend grows roughly
    # QUADRATICALLY in turns: ~$0.28/task at 40 turns, ~$1.8 at 100, ~$4 at 150.
    # 100 is ~2.5x the point where every ladder task was still mid-exploration,
    # while keeping the 3-task ladder near $5/arm.
    max_turns: int | None = 100
    max_price: float | None = 3.00
    # Wall-clock guard on the agent subprocess, in seconds. This is the real
    # backstop: a wedged agent burns no tokens, so max_price never fires on it.
    # On timeout the child is killed and we still harvest the repo's git diff,
    # so partial edits survive as a patch. None = no timeout (not recommended).
    timeout_s: int | None = 1800
    # Backend: "inproc" runs _vibe_inproc.py (history + tokens + cost),
    # "cli" runs the raw `vibe` binary (history only, no cost),
    # "mock" runs the synthetic agent (no vibe needed).
    backend: str = "inproc"

    def resolved_vibe_python(self) -> str | None:
        if self.vibe_python:
            return self.vibe_python
        if self.vibe_bin:
            p = Path(self.vibe_bin).with_name("python")
            return str(p)
        return None


FORKS: dict[str, ForkConfig] = {
    # Synthetic agent — no vibe required. Validates the pipeline end-to-end.
    "mock": ForkConfig(name="mock", backend="mock", model="mock-model"),

    # Baseline vibe: no indexing.
    "baseline": ForkConfig(
        name="baseline",
        vibe_bin="venvs/baseline/bin/vibe",
        index_build_cmd=None,
    ),

    # Ultra-index vibe: same model. This fork's index is PASSIVE — built lazily on
    # session start (service.ensure_ready(); a barrier blocks the agent's first tool
    # use until the repo map is ready), so there is no explicit build command. The
    # build cost therefore lands inside per-task wall-clock rather than on its own
    # line. (branch: codex/passive-repository-index)
    "ultra-index": ForkConfig(
        name="ultra-index",
        vibe_bin="venvs/ultra-index/bin/vibe",
        index_build_cmd=None,
    ),
}


def get_fork(name: str) -> ForkConfig:
    try:
        return FORKS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown fork '{name}'. Known: {', '.join(FORKS)}"
        )
