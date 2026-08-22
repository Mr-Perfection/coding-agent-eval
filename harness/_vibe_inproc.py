#!/usr/bin/env python
"""In-process vibe runner — emits history + token usage + cost as one JSON blob.

Run this with a *fork's own* python (it imports `vibe`, not our harness package):

    venvs/<fork>/bin/python harness/_vibe_inproc.py \
        --prompt-file F --workdir DIR [--max-turns N] [--max-price P] [--agent NAME]

--max-turns / --max-price are optional; omitted means uncapped.

Why this exists: the plain `vibe --output json` CLI returns only the history array —
token/cost totals live in in-memory session stats and are never printed or persisted.
This mirrors `vibe.cli.cli._run_programmatic_mode`'s bootstrap (headless, auto-approve,
trusted workspace, same disabled tools) so agent behavior matches the CLI, then reads
`session.exit_summary()` + the private `session._state.stats` to compute cost via
`vibe.utils.pricing`.

Output (stdout, single JSON object):
    {"history": [...], "usage": {"input","output","total"}, "cost_usd": float|null,
     "session_id": str|null, "error": str (only on failure)}

Robustness: git-diff (done by the caller) is the source of truth for the patch, so even
if usage/cost extraction fails this still emits the history and the run is usable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import aclosing
from pathlib import Path


def _init_globals() -> None:
    """Global startup init the vibe CLI performs before running a session
    (see vibe/cli/entrypoint.py main()). Without init_harness_files_manager the
    runtime raises 'HarnessFilesManager not initialized' when it loads the system
    prompt. dotenv (~/.vibe/.env) + file logging mirror the CLI. All idempotent."""
    try:
        from vibe.core.config.vibe_schema import load_dotenv_values
        load_dotenv_values()
    except Exception:
        pass
    try:
        from vibe.core.paths import LOG_FILE
        from vibe.observability.logging import init_file_logging
        init_file_logging(LOG_FILE.path)
    except Exception:
        pass
    from vibe.core.config.harness_files import init_harness_files_manager
    init_harness_files_manager("user", "project")


def _terminal():
    try:
        from vibe.cli.cli import detect_terminal
        return detect_terminal()
    except Exception:
        return None


def build_options(prompt_cwd: str, max_turns: int | None, max_price: float | None,
                  agent: str | None):
    from vibe.app_server.local import (
        ClientDescriptor,
        LocalHarnessOptions,
        NewSessionIntent,
    )
    from vibe.app_server.protocol import (
        ClientCapabilities,
        ClientInfo,
        SessionOptions,
    )
    try:
        from vibe import __version__
    except Exception:
        __version__ = "0"

    return LocalHarnessOptions(
        experimental_harness=False,
        client=ClientDescriptor(
            info=ClientInfo(
                name="coding_agent_eval",
                title="coding-agent-eval harness",
                version=str(__version__),
                entrypoint="programmatic",
                terminal_emulator=_terminal(),
            ),
            capabilities=ClientCapabilities(callback_kinds=["approval", "user_input"]),
        ),
        session_options=SessionOptions(
            cwd=prompt_cwd,
            workspace_roots=[],
            agent=agent,
            auto_approve=True,
            enabled_tools=[],
            disabled_tools=["ask_user_question", "exit_plan_mode"],
            max_turns=max_turns,
            max_price=max_price,
            max_session_tokens=None,
            headless=True,
            trust_workspace=True,
        ),
        session=NewSessionIntent(),
    )


def _usage_and_cost(session) -> tuple[dict, float | None]:
    usage: dict = {}
    cost: float | None = None
    try:
        u = session.exit_summary().usage
        usage = {"input": u.input_tokens, "output": u.output_tokens,
                 "total": u.total_tokens}
    except Exception:
        pass
    try:
        st = session._state.stats  # AgentStatsSnapshot (private; tokens + prices)
        from vibe.utils.pricing import session_token_cost
        cost = round(session_token_cost(
            prompt_tokens=st.session_prompt_tokens,
            completion_tokens=st.session_completion_tokens,
            cached_tokens=st.session_cached_tokens,
            input_price_per_million=st.input_price_per_million,
            output_price_per_million=st.output_price_per_million,
            cached_input_price_per_million=st.cached_input_price_per_million,
        ), 6)
        if not usage:
            usage = {"input": st.session_prompt_tokens,
                     "output": st.session_completion_tokens,
                     "total": st.session_total_llm_tokens}
    except Exception:
        pass
    return usage, cost


async def _run(options, prompt: str) -> dict:
    from vibe.app_server.events import CallbackRequested
    from vibe.app_server.local import LocalHarness

    session = await LocalHarness(options).start()
    try:
        await session.resources.runtime.wait_until_ready()
        async with aclosing(session.act(prompt)) as events:
            async for event in events:
                # Headless: deny any interactive callback so we never block.
                if isinstance(event, CallbackRequested):
                    await session.deny_callback(event.callback)
        history = [e.model_dump(mode="json", by_alias=True) for e in session.history]
        usage, cost = _usage_and_cost(session)
        steps = None
        try:
            steps = session._state.stats.steps  # accurate agent-step count
        except Exception:
            pass
        try:
            session_id = session.exit_summary().session_id
        except Exception:
            session_id = None
        return {"history": history, "usage": usage, "cost_usd": cost,
                "steps": steps, "session_id": session_id}
    finally:
        await session.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file")
    ap.add_argument("--workdir", default=".")
    # Default None = uncapped (vibe skips the limit middleware entirely).
    ap.add_argument("--max-turns", type=int, default=None)
    ap.add_argument("--max-price", type=float, default=None)
    ap.add_argument("--agent", default=None)
    ap.add_argument("--check", action="store_true",
                    help="build options only (no model call) and print {'ok':true}")
    args = ap.parse_args()

    # Resolve paths before we chdir, then cd into the workspace like the CLI does.
    workdir = str(Path(args.workdir).absolute())
    prompt_file = str(Path(args.prompt_file).absolute()) if args.prompt_file else None
    os.chdir(workdir)
    _init_globals()  # must precede any session start (loads system prompt etc.)

    if args.check:
        build_options(workdir, args.max_turns, args.max_price, args.agent)
        print(json.dumps({"ok": True}))
        return

    prompt = Path(prompt_file).read_text()
    try:
        options = build_options(workdir, args.max_turns, args.max_price, args.agent)
        result = asyncio.run(_run(options, prompt))
    except Exception as e:
        # Never hard-crash: emit an empty-history blob so the caller degrades to
        # git-diff-only metrics rather than losing the whole instance.
        print(f"[_vibe_inproc] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        print(json.dumps({"history": [], "usage": {}, "cost_usd": None,
                          "session_id": None, "error": f"{type(e).__name__}: {e}"}))
        return
    print(json.dumps(result))


if __name__ == "__main__":
    main()
