"""Fork configuration for the A/B comparison.

A ``ForkConfig`` describes how to invoke one build of vibe. The two real forks
(``baseline`` and ``ultra-index``) MUST pin the same model so that any measured
delta is attributable to the indexing feature, not the model.

The ``mock`` fork uses a synthetic in-process backend (see ``vibe_agent``) so the
whole pipeline can be exercised before any real fork is installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Pin BOTH real forks to the same model. Change in one place only.
PINNED_MODEL = "mistral-medium-3.5"


@dataclass(frozen=True)
class ForkConfig:
    name: str
    # Path to the fork's `vibe` executable (typically inside its own venv).
    vibe_bin: str | None = None
    model: str = PINNED_MODEL
    # Extra CLI args appended to every headless invocation.
    extra_args: tuple[str, ...] = ()
    # Optional shell command, run + timed once per repo before the agent starts,
    # to build/refresh the index. Runs with cwd = the checked-out repo.
    # Placeholders: {repo} = repo path, {vibe} = vibe_bin. None = no index step.
    index_build_cmd: str | None = None
    # Safety rails for headless runs.
    max_turns: int = 40
    max_price: float = 2.00
    # Backend: "cli" runs the real executable; "mock" runs the synthetic agent.
    backend: str = "cli"


FORKS: dict[str, ForkConfig] = {
    # Synthetic agent — no vibe required. Validates the pipeline end-to-end.
    "mock": ForkConfig(name="mock", backend="mock", model="mock-model"),

    # Baseline vibe: no indexing. Point vibe_bin at the installed executable.
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
