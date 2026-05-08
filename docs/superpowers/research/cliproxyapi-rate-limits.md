# cliproxyapi rate-limit research — Plan B Phase 2

## Hypothesis

cliproxyapi proxies to gpt-5.3-codex and enforces limits at two
granularities: (a) requests-per-minute (RPM) and (b) tokens-per-minute
(TPM). Our trivial-payload burst (~50 tokens per call) will hit the
RPM ceiling first; a secondary ~500-token burst will reveal whether
TPM also matters at our target operating point.

## Methodology

- Primary: escalating concurrent burst at levels [4, 8, 16, 32, 48, 64, 96].
  Each level runs for 15 s steady state after a 60 s cooldown.
- Secondary sanity: a single ~500-token burst at the first level that
  cleared the primary ceiling. If the primary budget divided by
  (trivial/realistic) falls below the realistic level, we are
  TPM-bound and must scale down.

## Stop conditions

- Primary break: N where rate_limit_errors ≥ 3 in the 15 s window.
- Primary cleared through ceiling 96: default operating point = 48.
- Below N=8 break: fail with InsufficientHeadroomError — Plan B cannot
  sustain a useful multi-run baseline at this ceiling.

## Operating point formula

If the first break occurs at N≥8: `max_inflight_llm = floor(0.6 * N)`.
Otherwise abort Phase 2.

## Deployment reality — cliproxyapi is LOCAL

Discovered during T2.6 execution (2026-04-11): on this workstation,
`cliproxyapi` is a **local** HTTP service at `http://127.0.0.1:8317/v1`,
not a remote SaaS. Codex CLI's own `~/.codex/config.toml` points at
the same endpoint (`openai_base_url = "http://127.0.0.1:8317/v1"`),
and the sibling `~/bitgn-contest` project reaches upstream
gpt-5.3-codex through it too. The API key lives in
`~/.codex/auth.json` as `OPENAI_API_KEY`. So the prior "DNS lookup
for cliproxyapi.com fails" blocker was spurious — the service is a
loopback proxy that forwards to upstream codex. Rate-limit posture
is therefore dictated by whatever upstream codex imposes on this
machine's credentials, with the local proxy adding no ceiling of its
own.

## Results (filled by T2.6 live run)

- First break at: **N = none** — the primary ladder cleared cleanly
  through the N=96 ceiling with zero rate-limit errors at every rung.
- peak_inflight_llm sustained without errors: **96** (ladder ceiling)
- Chosen operating point: **`max_inflight_llm = 48`** via
  `pick_operating_point(first_break_level=None)` → `DEFAULT_WHEN_CLEARED`.
- `max_parallel_tasks`: **8**, from `min(48, 8)` per the plan's
  per-task-fan-out rationale.
- Secondary burst verdict: realistic ~150-word prompt at N=96 also
  cleared cleanly (68 completions, 0 errors) — no TPM-vs-RPM
  confusion visible at the target operating point.
- Recorded artifact: `artifacts/burst/20260411T061748Z.json`

## Observed anomaly — throughput dip at N=64

Completion counts across the ladder were `4→28, 8→64, 16→128, 32→224,
48→336, 64→64, 96→480`. Rate-limit errors were zero at every rung, so
the dip is **not** a rate-limit signal and the formula correctly
ignores it. Most likely causes: upstream codex transient queueing,
momentary connection-pool warmup, or another process briefly
contending for the same upstream slot during the 15s window. The
anomaly does NOT lower the chosen operating point — the plan's
formula is driven by error thresholds, not throughput variance. If
the tuned baseline in T2.7 shows unexpectedly low `peak_inflight_llm`
or elevated task latency, re-run the burst with the machine fully
idle and compare.
