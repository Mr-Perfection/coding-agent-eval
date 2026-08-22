# coding-agent-eval

Evaluation harness for measuring the impact of a **codebase indexing** feature in
[mistral-vibe](https://github.com/mistralai/mistral-vibe).

We run the same open benchmark (**SWE-bench Lite**) against two builds of vibe — a
**baseline** fork and an **ultra-index** fork — pinned to the **same Mistral model**, so
any delta is attributable to the index, not the model. We don't reimplement the
benchmark: the official [`swebench`](https://github.com/SWE-bench/SWE-bench) harness
grades resolution; we only supply the *agent runner* and the *extra metrics* that make
the indexing benefit visible.

## Why these metrics

End-to-end **resolution rate** alone can hide a real indexing win, because indexing
mostly changes *how the agent finds code*, not always *whether* it eventually succeeds.
So we layer four signals, most-to-least sensitive to the index:

| Metric | What it captures | Why indexing moves it |
|---|---|---|
| **Efficiency** (turns, grep/read calls) | search "flailing" | index replaces search round-trips with one retrieval |
| **Time to completion** | per-task wall-clock | fewer LLM turns → less latency |
| **Cost per solve** | tokens / $ per resolved task | fewer turns & less context → fewer tokens |
| **Resolution rate** | did the real test suite pass | the downstream headline number |

### The two time effects of indexing

Indexing has **two opposing costs** and we keep them separate:

- **Index build time** — one-time per repo (parse/embed). Amortizable & cacheable, so
  it's reported on its own line, never folded into per-task time.
- **Per-task wall-clock** — this is what should *drop*. Agent time ≈ `turns × latency`,
  and most wasted turns are `grep`/`read` searches. Good indexing cuts those.

## Layout

```
harness/
  config.py          # ForkConfig: baseline vs ultra-index (paths, model, index build cmd)
  vibe_agent.py      # runs a vibe fork headless on a repo -> diff + transcript + timing
                     #   (includes a MOCK backend so the pipeline runs before forks exist)
  metrics.py         # parse transcript -> turns / tool counts / tokens / cost / time
                     #   + localization overlap (agent diff vs gold patch)
  repos.py           # clone-once cache + checkout base_commit per instance
  run_predictions.py # iterate SWE-bench Lite -> predictions.jsonl + metrics.jsonl
  grade.py           # thin wrapper over `swebench` run_evaluation (Docker)
  compare.py         # join predictions + grades + metrics -> A/B delta table
runs/                # per-run outputs (gitignored)
```

## Quickstart

```bash
uv venv && source .venv/bin/activate
uv pip install -e .

# 1. Smoke-test the whole pipeline with the mock agent (no vibe, no Docker needed):
python -m harness.run_predictions --fork mock --split lite --limit 5 --run-id smoke

# 2. Once a fork is installed, run it for real:
python -m harness.run_predictions --fork baseline    --split lite --run-id base_v1
python -m harness.run_predictions --fork ultra-index --split lite --run-id idx_v1

# 3. Grade with the official harness (requires Docker):
python -m harness.grade --run-id base_v1
python -m harness.grade --run-id idx_v1

# 4. A/B comparison:
python -m harness.compare --baseline base_v1 --candidate idx_v1
```

## Configuring the forks

Edit `harness/config.py`. Each `ForkConfig` points at an installed `vibe` executable
(its own venv), the model to pin, and an optional `index_build_cmd` run (and timed)
once per repo before the agent starts. Keep the **model identical** across forks.

## Status

Runner + metrics + mock backend are wired and testable now. Grading needs Docker and a
real fork. The `ultra-index` fork is still in development
([andred1729/mistral-vibe-ultra-index](https://github.com/andred1729/mistral-vibe-ultra-index)).
