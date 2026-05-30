# Handoff — exact prompt to start the next session

Copy everything in the fenced block below as the first message of a fresh session
(context cleared). It is self-contained and points at the persisted docs.

```
We are making the BitGN ECOM contest agent RELIABLE (target ~52/53 deterministically;
t48 is a separate empirical probe). The prior "score is irreducible seed variance"
conclusion was disproven this session: the 38–51/53 swing is the agent improvising a
small set of systematic, per-world interpretation forks (e.g. on count_per_store it flips
INNER-vs-LEFT join + direction-blind missing-row handling), NOT noise on a fixed seed.
Every task except the two fraud tasks hit 1.0 on some PROD world, so each is a reliability
target. The fix is an in-trial, DETERMINISTIC (pure /bin/sql + Python, no aux-LLM call —
the Haiku route 400-blacks-out), BOUNCE-not-rewrite re-derivation of the agent's own answer
that abstains on ambiguity. This is "the agent catching its own variance," not a post-pass
override (those fired nowhere) and not voting (it amplifies the wrong majority).

READ THESE FIRST (in order), they are complete and self-contained:
  1. docs/FINDINGS_RELIABILITY_2026-05-30.md   — evidence, per-family root cause, the
       proven t45 missing-row convention (available = COALESCE(qty,0), direction-dependent),
       and why this isn't overfitting.
  2. docs/SPEC_RELIABILITY_53.md               — the design + non-negotiable principles +
       conventions + per-phase components + acceptance criteria.
  3. docs/superpowers/plans/2026-05-30-answer-rederivation-verifier.md — the task-by-task
       TDD plan (Phase 0 + Phase 1 fully coded; Phases 2–7 specified).
Also relevant memories (auto-loaded): project_variance_is_reliability_not_seed,
project_ecom_53_path_per_family, project_ecom_t48_genuine_wall, project_ecom_provider_400_blackout.

KEY PROVEN FACTS (don't re-derive):
  - t45 oracle = 4: missing store_inventory row → 0 available → qualifies for "fewer than 4"
    (LEFT JOIN/COALESCE gives 4; INNER gives 1). Direction-dependent. Verified on
    artifacts/ws_snapshots/t45_real2/run_0/workspace/catalogue.db.
  - t16 oracle = "result 3"; t40 oracle = the 26-row device∪method connected component
    (fraud_component_completer.py already computes it correctly, default-off); t47 = 4 SKUs;
    t50 = the unique-newest basket, NO fall-through; t01 = the agent's verdict is always
    right, the team's own yes_no completer corrupts it by flooding never-read refs.
  - t48 is the ONE genuine wall (no in-workspace fraud rule; PROD max ~0.305).

CONSTRAINTS:
  - Validate every phase against the *_real2 oracle snapshots LOCALLY (~$0) before any PROD run.
  - Re-derivation BOUNCES (Verdict(ok=False) → existing retry path), never silently rewrites,
    abstains on ambiguity (no-op = today). Everything env-gated default-off until A/B'd.
  - In-trial run_sql: self._adapter.dispatch(Req_Exec(tool="exec", path="/bin/sql", args=[],
    stdin=sql)); /bin/sql returns CSV. The count completer is already wired at agent.py:1620-1636.

START NOW with Phase 0 (provider-400 retry + verification_coverage logging) and Phase 1
(count_per_store re-derivation: build src/bitgn_contest_agent/count_rederive.py via TDD so
tests/test_count_rederive.py reproduces t45_real2→4 and t16_real2→3, then wire the bounce in
agent.py gated BITGN_USE_REDERIVE_COUNT). Use the superpowers:subagent-driven-development
skill (fresh subagent per task, review between tasks). After Phase 1's local + one PROD DEV
A/B is clean, STOP and report the band before doing Phases 2–6. Treat Phase 7 (t48) as the
final empirical probe that decides whether 53/53 pass@1.0 is reachable at all.

Confirm you've read the three docs and give me your Phase 0/1 task breakdown before writing code.
```
</content>
