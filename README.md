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
  report.py          # render a run (or an A/B) as a self-contained report.html
runs/                # per-run outputs (gitignored): metrics.jsonl + report.html
```

## Setup

**Prerequisites:** [`uv`](https://docs.astral.sh/uv/), **Python 3.12**, and — only if you
want to *grade* — **Docker** running (Docker Desktop on macOS). A vibe login (below) is
needed only to run the agent for real; the mock backend needs none of this.

**First-time setup after a fresh clone:**

```bash
# 1. Harness venv (dataset loader; grading adds 'swebench<4' automatically — see below):
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python datasets

# 2. Baseline fork = latest main of upstream vibe, in ITS OWN venv:
uv venv venvs/baseline --python 3.12
uv pip install --python venvs/baseline/bin/python "git+https://github.com/mistralai/mistral-vibe"
# ...later, the ultra-index fork the same way into venvs/ultra-index.

# 3. Log in + pin the model (see "Auth & model pinning" below), then smoke-test the pipeline:
.venv/bin/python -m harness.run_predictions --fork mock --split lite --limit 5 --run-id smoke

# 4. Real run over the curated 8-task subset (agent + grade + HTML report):
./run_eval.sh --open
```

Two separate venvs by design: each **vibe fork** gets its own venv (so the two
builds can't interfere), and the **harness** gets its own. `runs/`, `repo_cache/`, and
`logs/` are gitignored, so a teammate's clone starts clean and regenerates them.

### Auth & model pinning (important)

* **Auth:** vibe uses a cached browser login (console.mistral.ai) *or* a
  `MISTRAL_API_KEY` env var / `~/.vibe/.env`. Run `venvs/baseline/bin/vibe --setup`
  once if not already logged in.
* **Model:** vibe has **no `--model` flag**. Pin the model in `~/.vibe/config.toml`
  with `active_model = "<alias>"` (the alias must exist under `[models]`). Set the
  **same** model before running both forks so the delta reflects the index, not the
  model.

## Quickstart

```bash
# 1. Pipeline smoke test — mock agent, no vibe/Docker/network-agent calls:
.venv/bin/python -m harness.run_predictions --fork mock --split lite --limit 5 --run-id smoke

# 2. One real baseline task (small, cost-bounded by max_price in config.py):
.venv/bin/python -m harness.run_predictions --fork baseline --split lite --limit 1 --run-id base_smoke
#    inspect: runs/base_smoke/predictions.jsonl  and  runs/base_smoke/metrics.jsonl

# 3. Full A/B once the ultra-index fork exists (same model pinned for both):
.venv/bin/python -m harness.run_predictions --fork baseline    --split lite --run-id base_v1
.venv/bin/python -m harness.run_predictions --fork ultra-index --split lite --run-id idx_v1

# 4. Grade with the official harness (needs Docker + `uv pip install --python .venv/bin/python 'swebench<4'`):
#    (pin <4: swebench 4.x/5.x need a prebuilt-image dataset; 3.x builds images from source)
.venv/bin/python -m harness.grade --run-id base_v1
.venv/bin/python -m harness.grade --run-id idx_v1

# 5. A/B comparison table:
.venv/bin/python -m harness.compare --baseline base_v1 --candidate idx_v1
```

### Grading notes (read before your first grade)

* **Docker must be running.** Grading applies the patch + the repo's real test suite in a
  container — no model involved, fully deterministic.
* **First grade per repo is slow, then cached.** `grade.py` builds the swebench eval images
  **locally from source** (`--namespace ""`): base image → per-repo env image (pip-installs
  the project's pinned deps) → instance image (a full `git clone` of the target repo). Expect
  ~10–15 min the first time you touch a repo; subsequent tasks in that repo reuse the images.
  This local build is required on **Apple Silicon / arm64**, where swebench's default prebuilt
  Docker Hub images don't exist (they're x86_64-only).
* **swebench is auto-pinned to `<4`.** `run_eval.sh`/`grade.py` install `swebench<4`; 4.x/5.x
  switched to a prebuilt-image dataset model that can't grade classic SWE-bench_Lite rows.
* **If a build hangs at `FROM ubuntu:22.04` with an empty `docker ps`,** it's a flaky Docker
  Hub connection (swebench's builder blocks instead of erroring). Fix: `docker pull ubuntu:22.04`
  manually, then re-run the grade — it resumes from cache.
* Random-named containers (e.g. `upbeat_shannon`) showing *"configured logging driver does not
  support reading"* are just swebench build steps run with logging disabled — **harmless**.

## Hackathon subset (recommended)

Running all 300 Lite tasks is slow and expensive. `tasks/indexing_subset.json` is a
curated **8-task** pool chosen for the indexing story: all from large repos
(django/sympy/matplotlib/scikit-learn) where finding the *one* correct file is hard,
split into two buckets:

* **localized** (4) — small, specific issues; target file findable. Index should trim
  search turns; resolution probably already high. (~ "vibe good-at")
* **buried** (4) — deep/obscure target file, long vague issue. Index should help most
  on resolution + localization + search turns. (~ "vibe bad-at")

See details on those tasks: https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite/viewer/default/test


```bash
# Run only the curated subset (any fork):
.venv/bin/python -m harness.run_predictions --fork baseline \
    --instances-file tasks/indexing_subset.json --run-id base_subset
# ...or an ad-hoc list:
.venv/bin/python -m harness.run_predictions --fork baseline \
    --instances django__django-14672,sympy__sympy-20049 --run-id spot
```

## Configuring the forks

Edit `harness/config.py`. Each `ForkConfig` points at an installed `vibe` executable
(its own venv), safety rails (`max_turns`, `max_price`), and an optional
`index_build_cmd` run (and timed) once per repo before the agent starts.

### Backends (how the agent is invoked)

* **`inproc`** (default for real forks): runs `harness/_vibe_inproc.py` with the fork's
  own python. It mirrors vibe's programmatic bootstrap (headless, auto-approve, trusted
  workspace) **and** captures token usage + computes cost — the plain CLI can't. This is
  what populates **cost-per-solve**.
* **`cli`**: the raw `vibe --prompt … --yolo --trust --workdir … --output json` binary.
  History/turns/tools only, no cost. Kept as a fallback.
* **`mock`**: synthetic agent, no vibe needed.

### Metric sourcing status
All four metrics are wired: **resolution** (grader), **turns + tool breakdown**
(`file_search`/`file_read` = retrieval signal), **time** (per-task wall-clock, index
build timed separately), and **cost/tokens** (inproc backend via `vibe.utils.pricing`).
The inproc bootstrap is validated offline (`_vibe_inproc.py --check` → `{"ok":true}`);
the token/cost read-back validates on the first real run and **degrades gracefully** —
if extraction ever fails, the patch still comes from `git diff` and cost shows `None`.

### Cost & turn caps (per run)
`--max-price` and `--max-turns` override the fork defaults (`max_price=1.00`,
`max_turns=40` in `config.py`) for a single run, e.g. `--max-price 0.50`. Lower =
safer spend, but too low truncates hard "buried" tasks before they finish.

## Status

* Runner + metrics + mock backend wired; 8 smoke tests pass (`python tests/test_smoke.py`).
* Baseline vibe integration verified against **vibe 2.24.3** (real CLI flags + the
  `--output json` history-array schema; parser tested against that shape).
* **Cost/tokens capture validated** on a real run (inproc backend via `vibe.utils.pricing`).
* **Grading validated end-to-end** (swebench 3.x, local image build). First graded task
  `django__django-12113`: baseline vibe **unresolved** — it edited the wrong file
  (`tests/test_sqlite.py` instead of the gold `.../sqlite3/creation.py`), and `Loc F1 = 0`
  predicted the miss before grading. A good task to show the index earning its keep.
* Pending: install the `ultra-index` fork once ready
  ([andred1729/mistral-vibe-ultra-index](https://github.com/andred1729/mistral-vibe-ultra-index))
  and run the A/B.
