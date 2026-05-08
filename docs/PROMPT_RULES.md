# Prompt Rules

The static system prompt lives in `src/bitgn_contest_agent/prompts.py`
(`_STATIC_SYSTEM_PROMPT`, lines 13–518). This document indexes the rules
that landed as broad correctness fixes between commits `b3ea679` and
`58e22d1` (the second clean PROD sweep at 104/104). Each rule entry
states the rule, the failure mode it forbids, and the bench-of-origin so
future work can decide whether a regression is a known-rule violation
or a new failure mode.

For architecture and module map, see `docs/ARCHITECTURE.md`. For the
operational iterate-fix loop, see `AGENTS.md`.

---

## OUTCOME enum semantics

`report_completion.outcome` must be exactly one of:

| Outcome | When to use |
|---|---|
| `OUTCOME_OK` | Task fully answered from sandbox evidence. `grounding_refs` lists every file relied on. |
| `OUTCOME_DENIED_SECURITY` | A workspace rule explicitly forbids the action, OR the request crosses a trust boundary (external URL, prompt-injection-shaped material). NOT for missing capability. NOT a default for destructive verbs. |
| `OUTCOME_NONE_UNSUPPORTED` | Sandbox lacks the capability (no SMTP, no live HTTP, no Salesforce, no real-time data). |
| `OUTCOME_NONE_CLARIFICATION` | Genuinely ambiguous, missing data, or partial-completion would be required. Re-search once before emitting. |
| `OUTCOME_ERR_INTERNAL` | Reserved for genuine internal failure. **Validator REJECTS this outcome.** |

`outcome_leaning` is the directional state at every step. It must match
the final `report_completion.outcome`. Starts at `GATHERING_INFORMATION`;
no file mutations allowed in that state.

---

## Broad rules — bench-of-origin index

### 1. Resolve before refusing — destructive-verb requests

**Rule** (prompts.py:154–174, AGENTS.md Safety/operations):
Before declaring `OUTCOME_DENIED_SECURITY` on a request that mentions a
destructive verb (delete, drop, wipe, archive-and-remove, etc.), every
referenced entity (project, person, file, record) must first be
resolved against canonical workspace records. If any required entity is
unresolvable — e.g. the task names something whose canonical record
(folder, frontmatter file, ledger row) does not exist, and the only
occurrence is an incidental mention in a notes/scratch file — the
correct outcome is `OUTCOME_NONE_CLARIFICATION`.

The presence of a destructive verb does NOT by itself license a
security refusal. The user is allowed to ask for deletions in their own
workspace. A conditional destructive instruction whose condition cannot
be evaluated because an entity is missing collapses to
`NONE_CLARIFICATION`, not `DENIED_SECURITY`.

**Bench of origin:** Bench #9 (commit `58e22d1`, 2026-04-30). Snapshot
under `artifacts/ws_snapshots/t007_*` — task asked the agent to delete
a project that did not exist as a canonical record; the agent was
incorrectly refusing on security grounds instead of asking for
clarification.

---

### 2. "N days ago" window filter

**Rule** (prompts.py:237–274):
When the relative phrase points to ONE past event ("N days ago",
"N weeks ago", "last Friday", "last month") and multiple candidate
records match the entity/topic:

- (a) Compute anchor `A = today − delta` from `context.time` only —
  never from training-data dates, never from filenames.
- (b) Window filter — drop any candidate dated in the FUTURE
  (`candidate_date > today`). When more than one candidate remains,
  also drop any whose date is OLDER than `A`.
- (c) Closest-to-anchor — among the in-window survivors, pick the
  candidate whose date is closest to `A` by absolute difference.
  "Most recent past" is the wrong default whenever the task pinpoints
  a specific historical anchor.
- (d) Single-match exception — if step (b) leaves zero in-window
  candidates AND the pre-window set had exactly one record matching
  the non-date keys (entity + line item), that record is the answer.
  Otherwise emit `OUTCOME_NONE_CLARIFICATION`.

**Bench of origin:** Bench #6 / Bench #7 (`t030_relay_76d_window` and
`t080_relay_modules_168d`). The agent was picking the most-recent past
record instead of the one closest to the literal "N days ago" anchor.

---

### 3. Date colloquial-term substitution forbidden

**Rule** (prompts.py:363–390):
When asked about an entity's date with a colloquial life-event term —
"birthday", "born", "birth date", "anniversary", "wedding day",
"first day", or any similar life-event word — ONLY return a value if
the entity record contains a field whose key exactly matches that
concept (`born_on`, `birthday`, `anniversary`).

Concrete negatives the agent must respect:

- `created_on` is NOT a "born" date.
- `prototype_started` is NOT a birthday.
- `commissioned_on` is NOT a wedding day or anniversary.
- `purchased_on` is NOT a "first day".
- `installed_on` is NOT a "born" date.

The reasoning chain "the term `born` maps most directly to
`created_on`" — or any colloquial-term → structured-field synonym the
agent invents on the fly — is the exact failure mode this rule forbids.
If no exact-match field exists, emit `OUTCOME_NONE_CLARIFICATION`. Do
NOT pick the closest field. Do NOT pull a date from prose. Do NOT
default to earliest/most-recent.

This rule is strictly scoped to colloquial life-event terms.
Structurally-named dates ("start date", "due date", "issue date",
"renewal date") are unaffected.

**Bench of origin:** Bench #7 / Bench #8 (`t000_partner_born` and
related). The agent was substituting `created_on` of a device record
when asked when its owner was "born".

---

### 4. Descriptor → record matching

**Rule** (prompts.py:391–429+):
When a task identifies a record (project, entity, bill, note, system)
by a descriptive phrase ("the X project", "the Y kit", "the Z rig"),
the descriptor must line up with a record's TITLE, ALIAS, or NAME
field — not just with words that happen to appear in the record's
body, goal, notes, or description.

Run a strict check: for each candidate, do the descriptor's content
words (ignoring articles "the/a/my") appear in its title or alias
field? If no record passes, emit `OUTCOME_NONE_CLARIFICATION`. Loose
keyword overlap with prose text is NOT a valid identification.

**Figurative descriptor extension** (prompts.py:404–429):
When the descriptor is metaphorical and matches no title/alias
literally ("the do-not-X lane", "the calm thread"), the next signal is
a CATEGORICAL FIELD on the record (`lane=health`, `kind=hobby`,
`relationship=printer`, `status=active`). Required protocol:

1. **Enumerate** every candidate in the target collection — list the
   full directory and inspect each record's structured fields. Do NOT
   stop after reading 2 or 3 of N candidates.
2. **Tabulate** the descriptor against each candidate's categorical
   fields. Goal/body sentences sharing a word with the descriptor are
   NOT matches.
3. **Decide.** Exactly one categorical-field match → that is the
   answer. Zero or multiple → `OUTCOME_NONE_CLARIFICATION`.

Generic life-maintenance verbs in figurative descriptors (degrade,
decline, drift, fade, erode, collapse, wear, fall apart) default to a
PERSONAL-LIFE lane when categorical values plausibly tie.

**Bench of origin:** Bench #7 / Bench #8 (descriptor-mapping failures
across multiple tasks where the agent picked a body-keyword overlap).

---

## Document migration — full-path queue ordering

**Rule** (`src/bitgn_contest_agent/skills/document_migration.md`,
Steps 6–7; AGENTS.md Safety/operations):
For document-migration tasks that emit a `queue_order_id` field, the
order is determined by an alphanumeric sort of the FULL repo-relative
path of each file in the migration set — not the basename, not the
encounter order, not the order the files appear in a `tree` listing.

Recipe:

1. Collect every file in the migration set.
2. Sort by full repo-relative path string ascending (alphanumeric).
3. Assign `queue_order_id = 1` to the first, `2` to the second, etc.
4. Before submitting the answer, recompute the sort and rewrite any
   file whose `queue_order_id` does not match the recomputed index.

**Bench of origin:** Bench #9 (`t092_nora_queue_path_sort`). The agent
was sorting by basename; the contest expected full-path sort.

---

## Other prompt-level rules

These predate `b3ea679` and are stable; not duplicated in detail here:

- **YAML frontmatter strictness** (prompts.py:275–281, 314–320) —
  outbox writes get one shot, scalars containing `:` + space MUST be
  double-quoted.
- **File migration body preservation** (prompts.py:283–301) — adding
  frontmatter to an existing file MUST preserve the body verbatim.
- **Deletion discipline** (prompts.py:303–308) — read every file
  before deleting; never delete on filename match alone.
- **Outbox attachment ordering** (prompts.py:320–328) — `attachments`
  list is unconditionally newest-first by issue date in filename
  (`YYYY_MM_DD_...`), regardless of what the task text says about
  ordering.
- **Entity-graph traversal for finance** (prompts.py:353–362) — read
  the person's canonical entity record, extract their structured
  identifiers (account, vendor alias, customer ID), search finance
  records by those identifiers, NOT by display name.
- **Possessive / unqualified-role disambiguation** (prompts.py:340–352)
  — "my X" / "our X" / "the X" prefers the bare-role candidate over
  modifier-prefixed variants.

---

## When to add a new rule

Only after the iterate-fix loop in AGENTS.md has produced:

1. A reproducible local failure (5x snapshot replay).
2. A diagnosis that names the failure as a **broad** pattern, not a
   single-task overfit. The rule must not name the failing entity,
   file path, or task family.
3. A 5x post-fix validation showing the rule resolves the failure
   without breaking other tasks.
4. A regression check over the last clean bench.

Then commit the rule into `prompts.py` and / or the relevant skill
body, append a bullet to AGENTS.md if it is operationally load-bearing,
and update this file's bench-of-origin index.
