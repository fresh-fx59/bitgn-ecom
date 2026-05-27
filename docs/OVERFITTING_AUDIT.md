# Overfitting audit — v0.1.112 (parked design doc)

> **Status:** parked. The audit found nothing that justifies a refactor
> against the one benchmark we currently target. Revisit when a second
> contest (`bitgn/ecom2-*` or similar) lands; this doc is the spec for
> the work at that point.
>
> Generated 2026-05-27. Repo state: v0.1.112, full bench 40-44/44 on
> `bitgn/ecom1-dev` with 4 speedups landed.

## TL;DR

- **Architectural principles generalize.** Post-pass enforcer chain
  (union-only, never rewrite numeric answers), refusal-cite taxonomy,
  i18n canonicalization split, fraud cluster detection — all
  domain-agnostic and proven portable.
- **~35-40% of the stack is contest-specific.** Brand lists, city
  mappings, policy doc paths, addenda directory layout, 14 explicit
  task-failure scars in the prompt.
- **Refactor cost: 2-3 days. Generalization gain: 60% → 85%.** Not
  worth doing today (no target to generalize FOR); the audit is the
  spec for when there is.

## Generalization classification

| Class | Examples | Action when porting |
|---|---|---|
| **GENERIC** | Enforcer chain order; `_post_process_terminal` shape; trace-writer schema | Lift verbatim |
| **DOMAIN-SHAPED** | `/proc/{stores,employees,payments,baskets,customers,returns}` namespace; fraud cluster window | Parameterise via a `ContestProfile` loaded at startup |
| **CONTEST-SPECIFIC** | `_CITY_TO_STORE_TOKENS`; `_ACTION_FAMILY_TRIPLES`; `_CANDIDATE_DIRS` for addenda; the 21-brand `_PRESERVE_REQUIRED` list | Move to `ContestProfile`; discover from `/AGENTS.MD` if possible |
| **SCAR TISSUE** | 14 "v0.X.Y t-NN failure" references in `prompts.py`; phrase lists in `refusal_cite_enforcer.py` | Keep per-contest; these are grader-language artefacts and don't transfer |

## Top-10 contest-specific elements ranked by removal cost vs benefit

| # | File:line | What | Removal cost | Generalization benefit |
|---|---|---|---|---|
| 1 | `sku_completer.py:243-268` | `_CITY_TO_STORE_TOKENS` — 15 city→store_id mappings | HIGH (need a discovery mechanism) | NONE — pure topology |
| 2 | `addenda_completer.py:80-86` | `_CANDIDATE_DIRS` — 5 hardcoded `/docs/<dir>` paths | MEDIUM (`/AGENTS.MD` parse) | MEDIUM (re-usable pattern) |
| 3 | `cite_completer.py:31-46` | `_ACTION_FAMILY_TRIPLES` — checkout/discount/3DS → fixed policy paths | LOW (move to config) | HIGH (fundamental e-commerce contract) |
| 4 | `task_canonicalizer.py:62` | 21-brand `_PRESERVE_REQUIRED` list | MEDIUM (auto-detect) | HIGH (i18n logic is otherwise portable) |
| 5 | `refusal_cite_enforcer.py:98-150` | ~160 English phrase patterns | HIGH (per-grader retune) | LOW (irreducible per contest) |
| 6 | `adapter/ecom.py:135-142, 477-505` | Prepass namespace assumption (`/proc/{...}`) | MEDIUM (parameterise) | HIGH (prepass logic itself is sound) |
| 7 | `sku_completer.py:666, 812` | Magic caps `family[:5]` and `skus[:1]` | LOW (already trivial constants) | MEDIUM (anti-pattern flag) |
| 8 | `fraud_cluster_filter.py:61` | `WINDOW_SECONDS = 1800` | LOW (parameterise via task text or `/docs/payments`) | MEDIUM (window pattern reusable) |
| 9 | `prompts.py` | 14 "v0.X.Y t-NN failure" references | HIGH (major prompt refactor) | ZERO (t-numbers don't transfer) |
| 10 | `agent.py:1014-1450` | Enforcer chain ORDER (9 steps) | LOW (already principled) | MEDIUM (order is contest-invariant; no work needed) |

## Top-5 hidden assumptions that would break on a similar contest

1. **Namespace shape is exactly `/proc/{stores,employees,payments,baskets,customers,returns}`** — prepass cache (`adapter/ecom.py:135-142`) + addenda discovery both hardcode this. Break scenario: contest adds `/proc/vendors` or renames `/proc/baskets` → `/proc/carts`. Mitigation: read `/AGENTS.MD` to discover namespaces at runtime.

2. **Policy docs are at fixed paths** (`/docs/security.md`, `/docs/checkout.md`, `/docs/discounts.md`, `/docs/payments/3ds.md`). `cite_completer._ACTION_FAMILY_TRIPLES` hardcodes them. Break scenario: contest organises policies under `/docs/policies/<family>/`. Mitigation: scan `/AGENTS.MD` for policy doc paths; infer action families from policy content.

3. **City descriptors map via a 15-entry substring table.** `sku_completer._CITY_TO_STORE_TOKENS` is exhaustive only for the current Austrian/CEE retail footprint. Break scenario: contest uses "shop 47" / "store alpha" / region abbreviations. Mitigation: require agent to read `/proc/stores/README.md` for mapping; require explicit store_id in task spec.

4. **Catalogue addenda live in 5 specific `/docs/` subdirs with stable filename patterns** (`addenda_completer._CANDIDATE_DIRS` + 3 hardcoded regex phrasings). Break scenario: contest moves addenda or uses different filename prefixes. Mitigation: parameterise dirs via `/AGENTS.MD` "clarification-documents" section.

5. **Brand list in task text is always from the 21 hardcoded brands.** `task_canonicalizer._PRESERVE_REQUIRED` silently falls back to raw text on mismatch (no error). Break scenario: any new brand or brand alias. Mitigation: auto-detect from `/proc/catalog` or SKU samples.

## Refactor proposal — `contest_profile.py`

A single dataclass replaces ~5 files' worth of scattered constants. Ideally **discovered from `/AGENTS.MD` or workspace probes** rather than hardcoded.

```python
# src/bitgn_contest_agent/contest_profile.py
from dataclasses import dataclass, field
import re
from typing import Callable

@dataclass(frozen=True)
class ContestProfile:
    """Contest-invariant configuration. Loaded once at agent init."""

    # Namespace shape
    entity_namespaces: dict[str, str]
        # "stores" → "/proc/stores", "baskets" → "/proc/baskets", ...
    proc_readme_paths: list[str]
        # Used by the prepass cross-task cache

    # Policy documents
    action_family_policies: dict[str, list[str]]
        # "checkout" → ["/docs/security.md", "/docs/checkout.md"]
    policy_root: str
        # "/docs" — base path for policy lookups

    # Addenda
    addenda_dirs: list[str]
    addenda_filename_patterns: dict[str, re.Pattern]
        # "catalogue_count" → compiled regex

    # Entity mapping
    city_to_store_mapping: dict[str, str]
        # "central Vienna" → "vienna_praterstern" (or learned at runtime)

    # SKU / product domain
    preserve_brand_list: list[str]
        # Auto-inferred from /proc/catalog if absent

    # Fraud invariants
    fraud_window_seconds: int = 1800

    # Currency
    primary_currency: str = "EUR"

    @classmethod
    def discover_from_workspace(
        cls,
        *,
        read: Callable[[str], str],
        tree: Callable[[str, int], str],
    ) -> "ContestProfile":
        """Probe /AGENTS.MD + /proc + /docs to populate all fields
        without any hardcoded constants. Returns a profile that
        DOWNSTREAM enforcers consume."""
        # 1. Read /AGENTS.MD → extract namespaces, policy doc paths,
        #    addenda directory hints
        # 2. tree("/proc", level=1) → discover entity namespaces
        # 3. tree("/docs", level=2) → discover policy doc layout
        # 4. /proc/catalog sample → infer brand list
        ...
```

### Conversion plan (when activated)

| Move | Source | Sink |
|---|---|---|
| `_CITY_TO_STORE_TOKENS` | `sku_completer.py:243` | `ContestProfile.city_to_store_mapping` |
| `_ACTION_FAMILY_TRIPLES` | `cite_completer.py:31` | `ContestProfile.action_family_policies` |
| `_CANDIDATE_DIRS` | `addenda_completer.py:80` | `ContestProfile.addenda_dirs` |
| 21-brand list | `task_canonicalizer.py:62` | `ContestProfile.preserve_brand_list` |
| Namespace paths | `adapter/ecom.py:135` | `ContestProfile.entity_namespaces` |
| `WINDOW_SECONDS` | `fraud_cluster_filter.py:61` | `ContestProfile.fraud_window_seconds` |

Pass the profile into `AgentLoop` and `_post_process_terminal`; threaded through enforcer calls. Default profile (for back-compat with `bitgn/ecom1-dev`) hardcoded as `ECOM1_DEV_PROFILE` constant — auto-discovery activates when `--auto-discover` flag is set or when `/AGENTS.MD` includes a `contest-profile` section.

## When to actually do this

Trigger conditions (any one is sufficient):

1. A second BitGN benchmark drops (`bitgn/ecom2-*` or any sibling) and we need to support both.
2. The current contest's workspace shape changes (new `/proc/*` namespace, new policy doc layout, new brand catalogue).
3. We commit to selling/open-sourcing the agent as a reusable framework rather than as a contest-tuned tool.

Until then: park. Every hardcoded constant in the list above is a **correct, principled** choice for the current target. Removing them speculatively would burn the 40-44/44 floor for no concrete benefit.

## What we DON'T need to change

- **Enforcer chain order in `agent.py:1014-1450`.** Contest-invariant; no work needed.
- **The "v0.X.Y t-NN failure" comments in `prompts.py`.** Each is a rule with documented provenance — healthier than rules without it. Don't strip them; they're the audit trail.
- **Magic caps `family[:5]` / `skus[:1]`.** Already cheap to retune. Move to `ContestProfile` only when you do the rest of the refactor.
- **Fraud `WINDOW_SECONDS = 1800`.** Domain-reasonable default; trivial to override via env var or task spec if needed.

## References

- Memory: `project_ecom_v108_deterministic_42_42.md` — the locked stack
- Memory: `project_ecom_v111_44_44_closerouter_i18n.md` — i18n + CloseRouter compat
- Memory: `project_ecom_v112_speedups_minus_38pct_wall.md` — current ship
- Memory: `feedback_enforcer_cannot_replace_adaptive_llm.md` — key principle
