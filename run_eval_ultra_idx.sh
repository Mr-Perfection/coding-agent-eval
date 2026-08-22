#!/usr/bin/env bash

# 1. Install the fork from THAT branch into its own venv
uv venv venvs/ultra-index --python 3.12
uv pip install --python venvs/ultra-index/bin/python \
    "git+https://github.com/andred1729/mistral-vibe-ultra-index@codex/passive-repository-index"

# 2. Pin the SAME model baseline used, in ~/.vibe/config.toml (active_model)
#    — vibe has no --model flag; a mismatch contaminates the A/B.

# 3. Run the eval (agent + Docker grade + HTML report)
./run_eval.sh --fork ultra-index --run-id idx_v1