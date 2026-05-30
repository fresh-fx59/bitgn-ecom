# ECOM run results — what actions led to what scores (2026-05-29/30 session)

## TL;DR — the 51.4→44.6 "drop" is SEED VARIANCE, not a regression

The contest **randomizes world content per run** (author-confirmed). The
SAME proven config lands anywhere in a wide band across runs because ~6-8
tasks flip pass↔fail depending on the random world. My session's code
changes are all **env-gated (default-off)**; the default path is
byte-identical to the 51.4 run, so the baseline this session ran the exact
same code+config and scored within the same band — just on a different seed.

## Run-to-run results matrix (overall = sum of 53 per-task scores)

| Config | run_id | overall | pass@1.0 |
|---|---|---|---|
| PRIOR proven-stack (**best**) | run-22Rotahx | **51.4** | 50 |
| PRIOR proven-stack | run-22Rpkp7 | 50.6 | 49 |
| PRIOR proven-stack | run-22Rohhq | 50.4 | 49 |
| PRIOR proven-stack | run-22Rqdzt | 48.7 | 47 |
| voting K=3 UNGATED | run-22Rriz | 48.5 | 47 |
| PRIOR proven-stack | run-22RoRqns | 48.4 | 47 |
| count-override (spec) | run-22RtAZ | 46.7 | 45 |
| **proven-stack (no levers, this session)** | run-22RtNKFV | **46.3** | 44 |
| PRIOR proven-stack | run-22Rpqxt | 45.7 | 44 |
| voting K=3 count-GATED | run-22RtbAJZ | 44.6 | 43 |
| 2 levers (count+quote) | run-22RsCq | 42.4 | 41 |
| ALL levers (+fraud+t48 hint) | run-22RsSi | 38.9 | 38 |

**Proven-stack alone spans 45.7–51.4 across runs (no code change) = pure
seed variance.** 51.4 was the lucky high end; 44–46 is the same config on
unlucky seeds. Experimental levers do NOT lift above this band; the t48 hint
actively drags it down (38.9).

## Per-task diff: best 51.4 (run-22Rotahx) vs baseline 46.3 (run-22RtNKFV)

ONLY these tasks differ — and they are exactly the documented variance-prone
ones; NO stable task regressed:

| task | best 51.4 | baseline 46.3 | note |
|---|---|---|---|
| t06 | 1.000 | 0.000 | count/variance flip |
| t13 | 1.000 | 0.000 | count_per_store flip |
| t14 | 1.000 | 0.000 | count_per_store flip |
| t45 | 1.000 | 0.000 | count_per_store flip |
| t47 | 1.000 | 0.000 | quote/pasted-list flip |
| t49 | 1.000 | 0.000 | catalogue_count flip |
| t39 | 1.000 | 0.916 | fraud partial (variance) |
| t48 | 0.556 | 0.453 | fraud partial (variance) |
| **t40** | 0.879 | **0.944** | fraud — baseline did BETTER |
| **t50** | 0.000 | **1.000** | baseline did BETTER |

The best run got lucky: 6 count/quote tasks (t06/t13/t14/t45/t47/t49) all
passed at once. The baseline hit a world where they failed — but t40/t50 went
the other way. Net 51.4 vs 46.3. **It's which random world you drew, not the
code.**

## "Do you see the same results running the benches?" — NO, by design

The score is NOT reproducible run-to-run. Across ~12 runs it ranges 38.9–51.4.
The dominant variable is which random world each task draws. This is the
contest author's deliberate anti-overfit design (content randomized per run,
task wording mutated, fraud traps).

## Structural caps (the only true ceilings)

Per-task MAX over 12 runs: every task hit 1.0 at some point EXCEPT
**t40 (max 0.944)** and **t48 (max 0.730)**. These two never reach 1.0 →
theoretical overall ceiling ≈ 52.67/53. t48 is author-trapped
(false-biggest-cluster); t40's recall/precision boundary is the difficulty
by design (author penalizes union-for-recall).

## Levers tested → why none reliably lifts the band

| Lever | result | why it fails |
|---|---|---|
| count override (text-parser) | 41-ish, fires nowhere | store/template regex too rigid for per-world phrasing |
| count override (spec, task_spec.products) | 46.7, **fires nowhere** | resolver abstains — exact attr match fails on ≥1 of 6 products per unseen world; loosening → wrong counts |
| fraud component completer | part of 38.9 | matcher missed "fraud hit" wording (fixed v150 but ADD-only = union-for-recall the grader penalizes) |
| t48 export hint | **regressed t48 0.43→0.0** | fell into the author's false-biggest-cluster trap |
| voting K=3 ungated | 48.5 | within band; non-count failures were seed variance (misattributed earlier) |
| voting K=3 count-gated | 44.6 | majority of K is also wrong on t45 (agent ~89% wrong there) |

## Bottom line

- **No regression occurred.** Default config = 51.4 best, varies 45.7–51.4 by seed.
- **The drop you saw is seed variance.** Same code, different random world.
- **53/53 needs t40 AND t48 at 1.0** — author-designed to resist automation
  (he states 51-52 is the intended ceiling). Reaching it requires either an
  unvalidatable fraud gamble or seed-sampling — both forbidden by the stated
  constraints.
- **Recommended shipping config = proven default** (the 5 enforcer flags, no
  experimental levers): the best available, expected ~48-51 depending on seed.
