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
    # Safety rails for headless runs (overridable per run via CLI flags).
    max_turns: int = 40
    max_price: float = 1.00
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

    # Ultra-index vibe: same model, plus an index build step per repo.
    # Adjust index_build_cmd to whatever the fork exposes (e.g. `vibe index build`).
    "ultra-index": ForkConfig(
        name="ultra-index",
        vibe_bin="venvs/ultra-index/bin/vibe",
        index_build_cmd="{vibe} index build",
    ),
}


def get_fork(name: str) -> ForkConfig:
    try:
        return FORKS[name]
    except KeyError:
        raise SystemExit(
            f"Unknown fork '{name}'. Known: {', '.join(FORKS)}"
        )
