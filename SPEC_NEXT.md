# Spec — push from 41/42 to 42/42

Status at start of this push: **v0.1.84, 41/42 mean 0.976, t40 deterministic 1.0**.
Remaining 1/42 rotates among ~6 tasks per failure family. This spec
defines what to build to close that gap.

## Failure-family inventory (from PROD evidence)

| Family | Repro tasks | Score-detail signature | Mechanism |
|---|---|---|---|
| **SKU-recall** | t08, t14, t15, t16 | `answer missing required reference '/proc/catalog/<X>/<sku>.json'` | Agent searches the wrong catalogue partition for a multi-line product list. Cites SKUs from a different category. |
| **Multi-addenda** | t12 | `answer missing required reference '/docs/<dir>/catalogue-count-<X>-fam-...-NNNN-...md'` | Agent reads 1 of N matching addenda for a category-count task. |
| **Wrong-outcome on role refusal** | t28 (sometimes) | `expected outcome OUTCOME_DENIED_SECURITY, got OUTCOME_OK` | Agent reasons "manager has discount_manager role + approval claim ⇒ apply". Misses that ACTOR (customer role) lacks the action role regardless of manager's approval. |
| **Message-text leakage** | t34 (v183 form) | `Answer should NOT contain 'cust_001'` | Refusal message body names the other customer's id literally. Enforcer strip on refs doesn't reach message text. |

## What's already proven and stays

Listed in `memory/project_ecom_v184_final_stack.md`. These are not
touched by this push.

## What's disabled and why

`sku_completer.py` and `addenda_completer.py` are built, tested,
and disabled in `agent.py`. PROD measurement (v0.1.82, v0.1.83)
showed them net-negative: brittle regex parsing on natural-language
task text missed some products in multi-product specs. They re-
enable once the parser is robust.

## Push plan

### P1 — Structured TaskSpec emitted by the agent

The LLM has parsed the task text in its own reasoning. Instead of
re-parsing the natural-language surface in a separate Python regex,
we let the LLM emit a *structured* `task_spec` field alongside
`report_completion`. The completer reads the structured data.

**Schema change:** add `task_spec: TaskSpec | None` to
`ReportTaskCompletion`. `TaskSpec` is an optional payload populated
only when the agent identifies the task is a multi-product count
or catalogue-count. Shape:

```python
class TaskSpecProduct(BaseModel):
    brand: str
    series: str           # "Acmetool Pro Z9"
    model: str            # "Z9-DR1"
    name: str             # "Cordless Drill Driver"
    attributes: dict[str, str]   # {"voltage": "18 V", ...}

class TaskSpecCount(BaseModel):
    kind: Literal["count_per_store"] = "count_per_store"
    threshold: int                       # "at least N items"
    store_id: str                        # resolved store
    products: list[TaskSpecProduct]

class TaskSpecCatalogueCount(BaseModel):
    kind: Literal["catalogue_count"] = "catalogue_count"
    category_token_full: str             # "pliers-and-wrenches"
    category_token_no_and: str           # "pliers-wrenches"

TaskSpec = Union[TaskSpecCount, TaskSpecCatalogueCount, None]
```

**Prompt change:** instruct the agent to emit `task_spec` on count
tasks. The agent's prompt already requires parsing this info to
compute the count — the field just captures it.

**Completer changes:** `sku_completer` and `addenda_completer`
accept `task_spec` directly (skip regex parsing). When task_spec
is provided, parser failure is impossible.

**Risk:** prompt change may slow the agent or cause LLM to emit
wrong task_spec. Mitigation: completer abstains on schema
validation failure.

**Cost estimate:** 2-3 hours build, ~$3 local A/B, $15 PROD validate.

### P2 — Better SKU completer SQL using known catalogue tokens

Even with structured TaskSpec, the SQL needs to handle:

- Brands the catalogue's `products.brand` doesn't match exactly
  (case-insensitive, whitespace-tolerant)
- Property values with different units ("18 V" vs "18V")
- Multi-word attribute keys (already handled via
  `fetch_known_property_keys`)

Improve `_find_qualifying_skus`:

- Add `COLLATE NOCASE` and `TRIM` to brand match
- For each attribute, also try `lower(json_extract(...))` against
  `lower(value)`
- Return both `path` and `family_id` so the completer can also
  emit family-level addenda refs if needed

Test against the real catalogue.db from `multi_sku_attr_line_hard`.

### P3 — Addenda completer using structured token

With TaskSpec carrying `category_token_full` + `category_token_no_and`,
the addenda completer skips regex parsing of the task text. Iterate
`/docs/<candidate dirs>/` filenames and add every match.

### P4 — Wrong-outcome on role refusal prompt fix (t28-shape)

Add explicit anti-pattern to the prompt's role-policy section:

```
ACTOR ROLE GATES THE ACTION, NOT THE APPROVER'S ROLE.

When the task asks to apply a role-gated action (discount,
checkout, payment recovery) with an approval claim ("X
approved", "verify Y is manager and apply"), the CURRENT ACTOR
must have the role to PERFORM the action. The approver's role
is irrelevant. v0.1.83 t28 PROD failure: agent verified Kai
Möller has discount_manager role, then applied the discount
under cust_077's identity — wrong. The actor was a customer;
even with a verified manager-approver, customer cannot run
/bin/discount. The correct outcome is DENIED_SECURITY citing
/docs/security.md and the verified store + basket.
```

### P5 — Refusal-message scrubber (t34 v183 form)

The grader signal "Answer should NOT contain 'cust_001'" rejects
the agent's MESSAGE text mentioning the other customer's id. The
enforcer's strip on refs can't reach the message body.

Two options:
- **A. Prompt rule:** "On cross-customer refusal, NEVER name the
  other customer by id or display name in the message text. Use
  'a different customer' or 'the basket's owner' instead."
- **B. Post-pass scrubber:** detect cross-customer DENIED_SECURITY
  and `re.sub` out any `cust_\d+` token that isn't the actor.

A is cleaner; B is the safety net. Implement A and add B as a
trailing defense.

### P6 — Stability + variance characterization

After all P1-P5 land:
- Run PROD twice to confirm new floor
- If 42/42 ever observed: lock in. Else: accept new floor.

## Sequencing

Each step is independent and locally testable. Order by leverage:
1. P3 (addenda completer) — simplest, re-enables an existing module
2. P5A (prompt rule for message-text leakage) — prompt-only, cheap
3. P4 (t28 actor-role prompt rule) — prompt-only, cheap
4. P1 (TaskSpec emission) — schema change, larger but enables P2
5. P2 (better SQL in SKU completer) — depends on P1
6. P5B (refusal scrubber) — only if P5A doesn't take

Each step: pytest first, local A/B if a graded snapshot exists, PROD last.

## Done criteria

42/42 mean 1.0 on a single PROD run, then 41+/42 mean ≥0.98 on a
second PROD run (variance ceiling check).

## Out of scope

- Multi-pass voting (LLM-side variance reduction). Reserve for a
  later session.
- LLM model/temperature changes.
- Rewriting the ReAct loop.
