# ECOM1-PROD — token cost per full run (OpenAI API)

> Measured from the v0.1.162 100-task PROD run (`run-22S3byCTcqQR684Ftwwv5oevy`,
> trace dir `logs/prod_full2_20260530T213343Z`). Updated 2026-05-31.

## Measured token usage — one full run (100 tasks)

| Metric | Tokens |
|---|---|
| Total prompt (input) | **13,706,708** |
| — cached (75.8%) | 10,388,480 |
| — uncached | 3,318,228 |
| Total output (completion, incl. reasoning) | **249,745** |
| Total LLM calls | 531 (≈5.3/task) |

Per-run usage **varies** run-to-run (worlds re-instantiate); treat as a representative sample.
This run had the **aux layer blacked out**, so it's essentially all main-model (gpt-5.4) tokens.

## OpenAI API price per 1M tokens (2026)

| Model | Input | Cached input | Output |
|---|---|---|---|
| **GPT-5.4** (main model used) | $2.50 | $0.25 | $15.00 |
| **GPT-5.5** (current latest, ~2×) | $5.00 | $0.50 | $30.00 |
| **gpt-4.1-mini** (aux/classifier) | $0.40 | $0.10 | $1.60 |

## Cost of one full run on OpenAI API

| Scenario | Cost |
|---|---|
| **GPT-5.4, warm cache (75.8%, as measured)** | **≈ $14.64** |
| GPT-5.4, cold cache (no prefix reuse) | ≈ $38.01 |
| GPT-5.5, warm cache | ≈ $29.28 |
| GPT-5.5, cold cache | ≈ $76.03 |
| + aux (gpt-4.1-mini, ~200 calls) when aux is alive | + ≈ $0.30 (negligible) |

### Bottom line
- **One full PROD run ≈ $15 on OpenAI API** with GPT-5.4 and a warm prompt cache
  (matches the historical "~$15/run" figure). The aux model (gpt-4.1-mini) adds ~$0.30.
- The **prompt cache is load-bearing**: 75.8% of input is cached → cold-cache runs cost
  ~2.6× more (~$38). Cache hits need runs close in time with the same system-prompt prefix.
- On **GPT-5.5** (the post-2026-04-23 latest) the same run is ~$29 warm / ~$76 cold.

### Caveats
- The contest actually runs via the **linkapi proxy**, not OpenAI direct — proxy billing may differ.
- Reasoning tokens (66,694 here) are billed as output; they're included in the completion total above.
- Reads/tool calls against the BitGN runtime are free (no token cost).

## How to refresh these numbers
```bash
# Sum token usage across a run's traces:
python3 - <<'PY'
import json, glob
t={'p':0,'o':0,'c':0}; 
for f in glob.glob('logs/<RUN_DIR>/*/t*__run0.jsonl'):
    for l in open(f):
        o=json.loads(l)
        if o.get('kind')=='outcome':
            t['p']+=o.get('total_prompt_tokens',0); t['o']+=o.get('total_completion_tokens',0); t['c']+=o.get('total_cached_tokens',0)
print(t)
PY
# then: cost = (p-c)/1e6*INPUT + c/1e6*CACHED + o/1e6*OUTPUT
```

Sources: [pricepertoken GPT-5.4](https://pricepertoken.com/pricing-page/model/openai-gpt-5.4),
[OpenAI API pricing](https://openai.com/api/pricing/), [GPT-4.1-mini / OpenRouter](https://openrouter.ai/openai/gpt-4.1-mini).
