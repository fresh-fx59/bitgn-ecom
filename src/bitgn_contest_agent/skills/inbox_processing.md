---
name: inbox-processing
description: Strategy for processing inbox items including OCR, forwarding, and multi-step workflows
type: rigid
category: INBOX_PROCESSING
matcher_patterns:
  - '(?i)\b(work|process|handle)\b.*\b(oldest|next|first|latest|newest)\b.*\b(inbox|message|item)\b'
  - '(?i)\b(oldest|next|first)\b.*\b(inbox|message)\b'
  - '(?i)\binbox\b.*\b(item|message|task)\b'
classifier_hint: "Tasks asking the agent to process, work on, handle, review, or act on a pending item arriving in the inbox lane — including OCR, forwarding, filing, adding frontmatter, or any multi-step intake workflow. Applies whenever the task points at the inbox lane or the next pending item there, regardless of the specific vocabulary the task uses."
---

# Inbox Processing Strategy

## Step 0: Resolve the complete target set

The inbox task almost always points to a set of files (an entity's
bills, a project's records, etc.). You MUST find ALL of them before
processing. Missing one file = failure.

**Search rules — case-insensitive, multi-field:**

When an inbox item references an entity by name (e.g. "all bills
related to Juniper"), the entity may appear in records under varied
casing. A bare lowercase `rg <name>` will miss capitalized hits in
structured fields like `related_entity | Juniper`.

Required search recipe for entity→files resolution:

1. Read `10_entities/cast/<entity>.md` first to learn aliases and
   canonical name casing.
2. Run case-insensitive search against the candidate folder, e.g.
   `rg -i <entity> 50_finance` — NOT `rg <entity> 50_finance`.
3. Also search the structured field directly:
   `rg -i "related_entity.*<entity>" 50_finance`.
4. Union the results. If step 2 found N files and step 3 found M
   files, your target set is the deduplicated union.

**Completeness rule:** If the search returns N related files, process
ALL N. Do not stop after the first match. Each match is a separate
record that needs attention.

## Step 1: Read the inbox item — LITERAL SOURCE OF TRUTH

Read the oldest (or specified) inbox item to understand what action
is requested. **The inbox item is your authoritative task spec.**

**CRITICAL — explicit file lists override everything else:** When the
inbox item lists specific file paths (numbered or bulleted), those
paths are the EXACT and COMPLETE set of files to process. Do NOT
substitute, add, or remove files based on preflight data, entity
relationships, or your own search results. The inbox item's file
list is the ground truth — process exactly those files, no more, no
fewer.

Common inbox actions include:

- **OCR / add frontmatter** — parse unstructured bill text and add
  structured YAML frontmatter
- **Forward / send** — route content to a channel or recipient
- **File / organize** — move or categorize a record

## Step 2: Build the task list

**If the inbox item lists explicit file paths:** your task list IS
that file list. Read each file path exactly as given. Do NOT search
for alternatives or widen the scope.

**Explicit file list → budget discipline. Do NOT bulk-explore the
workspace.** The inbox item plus the listed files carry enough
information to act. Broad sibling-lane reading burns the step
budget and causes "no answer provided" timeouts.

- Do NOT bulk-read directories unrelated to the listed files to
  "gather context". Entity records, knowledge captures, project
  READMEs, and general notes are not needed to act on a specific
  listed artifact. The listed file's own structured fields
  (`counterparty`, `related_entity`, `project`, etc.) are
  sufficient — if a pointer is missing, the action itself does
  not require it.
- Do NOT open nested `AGENTS.md` files in lanes you are not
  writing to. The top-level `AGENTS.md`, the inbox lane's own
  `AGENTS.md`, and the one workflow doc matching the requested
  action are enough policy. Other lanes' `AGENTS.md` are noise.
- Permitted reads for an explicit-file-list inbox task:
  (a) the inbox item itself,
  (b) every file in the listed task set,
  (c) the single workflow doc matching the requested action
      (OCR, forward, file, etc.),
  (d) the single schema doc for the structured fields the action
      writes, if the action writes structured fields.
  Going beyond this set is a budget violation.

**If the inbox item references entities instead of files** (e.g.
"process all bills for Hearthline"): apply the Step 0 search recipe
(case-insensitive `rg -i` across filename AND structured fields
like `related_entity`) to build the complete file list.

**Named-entity budget discipline — do NOT roster sibling entities.**
When the inbox item references a specific entity by name (a person,
pet, or system — e.g. "linked to <entity>", "bills for <entity>"):

- Read ONLY that one entity's file (`<entities-lane>/<entity>.md`)
  to learn its canonical alias and field shape. Do NOT `cat` sibling
  entity files one-by-one to "build a roster" — sibling entities are
  irrelevant to the task.
- Then go directly to the target lane (finance, projects, etc.) and
  filter records by that entity's canonical alias or `related_entity`
  field using `rg -i` / `search`. Do NOT read every file in the
  target lane; use search to narrow first.
- Reading every entity file or every lane file is a budget violation
  that causes "no answer provided" timeouts. One entity was named —
  focus on that one.

## Step 3: Process ALL items

For each file in your task list:

1. Read the file
2. Perform the requested action (OCR, update frontmatter, etc.)
3. Write the updated file
4. Continue to the NEXT file — do NOT stop early

**Re-read the inbox item** after step 2 if you find yourself
uncertain about which files to process. The inbox item is always
right.

## Step 4: Clean up

After processing ALL files:
- Delete the inbox item to mark it as handled
- Report OUTCOME_OK

## Common Pitfalls

1. Processing only ONE file when multiple are listed. Process every
   listed file.
2. **Substituting different files** based on preflight/search context
   instead of following the inbox item's explicit file list. This is
   the #1 cause of "unexpected write" grading failures.
3. Ignoring invoice files and only processing purchase files (or vice
   versa). The inbox item may list both — process ALL of them.
