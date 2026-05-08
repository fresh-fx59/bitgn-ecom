Run intent-based benchmark report on PROD runs. Classifies tasks by stable intent, shows pass rates vs baseline, and details failures.

## From local bench files (fast, no API call)
- Single run: `python3 scripts/intent_report.py <bench.json>`
- Compare runs: `python3 scripts/intent_report.py --baseline <prev.json> <new.json>`
- Filter intents: `python3 scripts/intent_report.py --intent receipt_total_relative --intent inbox_en <bench.json>`
- Failures only: `python3 scripts/intent_report.py --failures-only <bench.json>`
- JSON output: `python3 scripts/intent_report.py --json <bench.json>`

Latest local run:
```
python3 scripts/intent_report.py "$(ls -t artifacts/bench/*_prod_runs1.json | head -1)"
```

## From BitGN dashboard (fetches live server data)
- From URL: `uv run python scripts/fetch_intent_report.py https://eu.bitgn.com/runs/run-XXXX`
- From run ID: `uv run python scripts/fetch_intent_report.py run-XXXX`
- Compare: `uv run python scripts/fetch_intent_report.py --baseline run-PREV run-NEW`
- Save fetched data: `uv run python scripts/fetch_intent_report.py --save artifacts/bench/fetched.json run-XXXX`

Requires BITGN_API_KEY env var.
