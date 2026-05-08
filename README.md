# bitgn-ecom-agent

A hardened, single-session agentic system for the [BitGN ECOM](https://github.com/bitgn/sample-agents/tree/main/ecom-py) competition. The agent autonomously solves ecommerce-operations tasks against the `bitgn/ecom1-dev` runtime — reading files, querying catalogue tables via `/bin/sql`, handling security-aware workflows — by driving a ReAct-style tool loop against the BitGN ECOM runtime.

This repository is a port of [`bitgn-contest-with-claude`](https://github.com/fresh-fx59/bitgn-contest-with-claude), my PAC1 entry that scored **104/104 with gpt-5.4**. The runtime layer was swapped from `bitgn.vm.pcm` to `bitgn.vm.ecom`; the architecture (ReAct loop, validator, enforcer, router, parallel reads, trace writer) is preserved verbatim.

---

## About

The challenge was launched and is curated by [Rinat Abdullin](https://abdullin.com/), whose Telegram channel [@llm_under_the_hood](https://t.me/llm_under_the_hood) is the canonical place for ECOM updates, leaderboards, and design discussion.

I'm documenting the engineering process behind this agent — prompt hardening, grounding enforcement, determinism debugging, and per-failure fix flow — on my own Telegram channel: [@ai_engineer_helper](https://t.me/ai_engineer_helper). If you're building agents against hard benchmarks and want to see the debugging notebook, follow along there.

---

## How it works

The agent runs a structured loop per task:

1. **Pre-pass** — fans out `tree(/, level=2)`, `read(/AGENTS.MD)`, and `context()` in parallel to ground itself in the runtime environment.
2. **Step loop** (up to 40 steps) — LLM emits a `NextStep` JSON with a reasoning scratchpad, a short plan, and a single tool call; the result feeds back as the next user message. Optional `parallel_reads` collapse N independent reads into a single LLM turn.
3. **Terminal** — `report_completion` emits an outcome with mandatory `grounding_refs` (every cited file must have been successfully read).

Reliability layers: exponential-backoff retry (P2), validation-error critique injection (P3), loop detection with nudges (P4), and an enforcer that hard-gates fabricated refs and surrender outcomes.

ECOM-specific surface (vs the PAC1 lineage):
- New tools: `stat`, `exec` (the latter for `/bin/sql` catalogue queries and other in-VM executables).
- Removed: `mkdir`, `move` (not exposed by the ECOM RPC).
- `read` gains line-slicing (`start_line`/`end_line`) for big files; `tree` gains a `level` cap; `find` keys on `kind` (`all`/`files`/`dirs`); `list` keys on `path`.
- Prepass reads `/AGENTS.MD` (uppercase, leading slash) — the PAC1 prepass read `AGENTS.md` from the vault root.

---

## Quick start

### 1. Install

```bash
# Python 3.12+ required
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### 2. Set credentials

```bash
export BITGN_API_KEY=<your-bitgn-key>
export CLIPROXY_BASE_URL=<cliproxy-endpoint>
export CLIPROXY_API_KEY=<cliproxy-key>
```

### 3. Run a single task

```bash
bitgn-agent run-task --task-id t01
```

Logs are written to `logs/` as JSONL traces.

### 4. Run the full benchmark

```bash
bitgn-agent run-benchmark
```

Optional flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--benchmark` | `bitgn/ecom1-dev` | Override benchmark slug |
| `--runs N` | `1` | Repeat each task N times |
| `--max-parallel N` | `8` | Parallel task workers |
| `--smoke` | off | Run fixed smoke subset (t01..t05, 180s budget) |
| `--output path` | none | Write `bench_summary.json` |

### 5. Triage failures

```bash
# Single run
bitgn-agent triage artifacts/bench/my_run.json

# Diff two runs
bitgn-agent triage --before artifacts/bench/baseline.json --after artifacts/bench/candidate.json
```

---

## Deploy the agent

To run the agent against a live BitGN contest VM (PROD grading):

### 1. Provision a VM

Request an ECOM VM from the organizers (see [@llm_under_the_hood](https://t.me/llm_under_the_hood) for the intake form). You will receive a hostname and a per-VM `BITGN_API_KEY`.

### 2. Prepare a `.env` file

Create `.env` at the repo root (it is gitignored) with the required secrets:

```bash
BITGN_API_KEY=<vm-issued-bitgn-key>
BITGN_BASE_URL=https://api.bitgn.com
CLIPROXY_BASE_URL=http://127.0.0.1:8317   # or your proxy endpoint
CLIPROXY_API_KEY=<cliproxy-key>
```

### 3. Start the LLM proxy

The agent talks to an OpenAI-compatible endpoint via [`cliproxyapi`](https://github.com/router-for-me/CLIProxyAPI) (default) or any OpenAI-compat backend. Start it locally:

```bash
cliproxyapi --bind 127.0.0.1:8317 &
```

### 4. Run the full benchmark

```bash
set -a; source .env; set +a
bitgn-agent run-benchmark \
  --max-parallel 3 \
  --max-inflight-llm 6 \
  --runs 1 \
  --output artifacts/bench/$(git rev-parse --short HEAD)_prod_runs1.json
```

Recommended p3i6 config (`--max-parallel 3 --max-inflight-llm 6`) keeps LLM concurrency under the proxy's fair-use limit while still exploiting task-level parallelism.

---

## Configuration

All tunables are set via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_MODEL` | `gpt-5.3-codex` | LLM model ID |
| `AGENT_REASONING_EFFORT` | `medium` | Reasoning effort (`low`/`medium`/`high`) |
| `BITGN_BENCHMARK` | `bitgn/ecom1-dev` | Benchmark slug |
| `MAX_STEPS` | `40` | Max tool steps per task |
| `TASK_TIMEOUT_SEC` | `900` | Per-task wall-clock budget |
| `MAX_PARALLEL_TASKS` | `4` | Concurrent task workers |
| `MAX_INFLIGHT_LLM` | `6` | Concurrent LLM calls across all workers |
| `LOG_DIR` | `logs` | Trace output directory |
| `BITGN_HARNESS_RAW_JSON` | (unset) | When `1`, falls back to urllib for `StartRun` (in case a future SDK pin lags the proto schema) |

---

## Project layout

```
src/bitgn_contest_agent/
  cli.py            # Entry point — run-task, run-benchmark, triage
  agent.py          # AgentLoop: step iteration, LLM calls, P2/P3/P4 patterns
  orchestrator.py   # ThreadPoolExecutor task dispatch with deadline/cancel
  adapter/ecom.py   # Bridge to BitGN ECOM runtime (read, write, search, exec, …)
  adapter/ecom_tracing.py  # TracingEcomClient — per-call ecom_op trace records
  backend/          # Provider-agnostic LLM interface (OpenAI-compat + cliproxyapi)
  schemas.py        # Pydantic tool schemas (NextStep discriminated union)
  enforcer.py       # Terminal policy: grounding-refs reachability, no-surrender gate
  session.py        # Per-task state + loop detector
  prompts.py        # Static system prompt (bit-identical for caching)
  task_hints.py     # Narrow per-failure-cluster hint injections
  trace_writer.py   # Thread-safe incremental JSONL tracing

artifacts/bench/    # Saved benchmark run summaries
docs/               # Design specs (PAC1-era, kept as historical reference)
tests/              # Unit + coverage tests (450 passing)
```

---

## Development

```bash
# Run tests
pytest

# Run smoke benchmark (fast subset, ~3 min)
bitgn-agent run-benchmark --smoke --output artifacts/bench/smoke.json
```

Benchmark results in `artifacts/bench/` follow the naming convention:
`<git-sha>_<label>_<model>_<timestamp>_<env>_runs<n>.json`

---

## Provenance

This repository was forked from [`bitgn-contest-with-claude`](https://github.com/fresh-fx59/bitgn-contest-with-claude) at commit `479b7c8`. See `git log` for the full porting trail; the docs under `docs/superpowers/` are PAC1-era design records kept for historical reference but no longer authoritative.
