#!/usr/bin/env python
"""Preflight: resolve what vibe will ACTUALLY send on the wire, and check auth.

Run with a fork's own python. Prints the resolved alias/model/provider and exits
non-zero with an explanation if the run would fail or silently bill the wrong
account. Catches the two traps that let a run reach the model with nothing pinned
and burn a full ladder into HTTP 402 Payment Required:

  1. `active_model = ""` is vibe's "unpinned" sentinel, so the old
     `grep -q active_model` preflight passed while nothing was pinned.
  2. The default model `mistral-vibe-cli-latest` carries the alias
     "mistral-medium-3.5", so the banner looked correct while the wire model was
     the subscription-metered one.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

EXPECTED_ALIAS = sys.argv[1] if len(sys.argv) > 1 else None

# Aliases/names metered against the Vibe CLI subscription rather than API credit.
SUBSCRIPTION_MODELS = {"mistral-vibe-cli-latest"}


async def main() -> int:
    from vibe.core.config.vibe_schema import load_dotenv_values

    load_dotenv_values()
    from vibe.core.config.harness_files import init_harness_files_manager

    init_harness_files_manager("user", "project")
    from vibe.core.config.default_orchestrator import build_user_config_orchestrator

    orch = await build_user_config_orchestrator()
    cfg = orch.config if hasattr(orch, "config") else orch.get()
    model = cfg.get_active_model()
    provider = cfg.get_provider_for_model(model)

    print(f"  active_model alias : {cfg.active_model or '(unpinned -> default)'}")
    print(f"  wire model         : {model.name}")
    print(f"  provider           : {provider.name}  {provider.api_base}")
    print(f"  price in/out /M    : ${model.input_price} / ${model.output_price}")

    fail = []
    if not cfg.active_model:
        fail.append(
            "no active_model pinned in ~/.vibe/config.toml (empty string = unpinned).\n"
            "     Both forks must pin the SAME alias or the A/B measures the model."
        )
    if EXPECTED_ALIAS and cfg.active_model != EXPECTED_ALIAS:
        fail.append(
            f"pinned alias {cfg.active_model!r} != harness PINNED_MODEL "
            f"{EXPECTED_ALIAS!r} — metrics would mislabel the run."
        )
    if model.name in SUBSCRIPTION_MODELS:
        fail.append(
            f"wire model {model.name!r} bills against the Vibe CLI subscription,\n"
            "     not La Plateforme API credit. It returns 402 once that quota is\n"
            "     spent. Pin an alias whose `name` is a plain API model."
        )

    key_env = provider.api_key_env_var
    from vibe.utils.api_keys import resolve_api_key

    key = resolve_api_key(key_env)
    if not key:
        fail.append(f"no API key: ${key_env} unset and not in the keychain.")
    else:
        src = "env" if os.environ.get(key_env) else "keychain"
        print(f"  api key            : found via {src} (${key_env}, {len(key)} chars)")

    # Live probe: a free GET that distinguishes an API key from a subscription key.
    # A subscription key answers /v1/models with {"detail": "Check your
    # subscription ..."} — the same account state that 402s the chat endpoint.
    # Network trouble is non-fatal; we only fail on a definitive negative.
    if key:
        req = urllib.request.Request(
            f"{provider.api_base.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.load(r)
            ids = {m.get("id") for m in body.get("data", [])}
            if ids and model.name not in ids:
                fail.append(
                    f"key is valid but does not expose {model.name!r}.\n"
                    f"     Closest available: "
                    f"{sorted(i for i in ids if 'medium' in str(i)) or sorted(ids)[:5]}"
                )
            else:
                print(f"  live check         : OK, {len(ids)} models visible")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            fail.append(
                f"key rejected by {provider.api_base}/models: HTTP {e.code} {detail}\n"
                "     'Check your subscription' here means this is the Vibe CLI\n"
                "     subscription key, not a La Plateforme API key. Put an API key\n"
                "     in ~/.vibe/.env (it wins over the keychain)."
            )
        except Exception as e:  # network/DNS/timeout — don't block on it
            print(f"  live check         : skipped ({type(e).__name__})")

    if fail:
        print("\nERROR: preflight failed:", file=sys.stderr)
        for f in fail:
            print(f"  -> {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
