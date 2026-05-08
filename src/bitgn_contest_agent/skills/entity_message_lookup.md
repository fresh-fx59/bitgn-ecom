---
name: entity-message-lookup
description: Strategy for finding the last recorded message from an entity
type: rigid
category: entity_message_lookup
matcher_patterns:
classifier_hint: "Tasks asking to quote or find the last recorded message or communication from a person or entity"
---

## Step 0: Pre-fetched context

A `PREFLIGHT` user message above (auto-dispatched by the router for this task shape) contains the canonical narrowing — the matching record(s), entity canonicalization, or destination resolution. Treat it as ground truth and start from those references. Fall through to the strategy below only if preflight returned nothing usable or the question needs more than what was pre-fetched.

## Search Strategy

1. Identify the target entity and resolve to their canonical name.
   Check for both "Firstname Lastname" and "Lastname Firstname" forms.

2. Search ALL communication and transcript records for the entity's
   name. Use `search` across the entire workspace, not just the first
   communication directory you find. Check every channel, transcript,
   and message log.

3. Also search for the reversed name form. Records may store names in
   either order (Lastname Firstname or Firstname Lastname).

4. If you find messages, identify the most recent one by date and
   quote it exactly. Report OUTCOME_OK with the quoted message.

5. If zero matches across ALL records after exhaustive search: the
   outcome is OUTCOME_NONE_CLARIFICATION. Explain that no recorded
   message from this entity was found.

CRITICAL: Never use OUTCOME_OK with a negative message like "no message
found" or "there are no recorded messages." The absence of data is not
an answer — it is a clarification need. If you searched everything and
found nothing, the correct outcome is OUTCOME_NONE_CLARIFICATION.
