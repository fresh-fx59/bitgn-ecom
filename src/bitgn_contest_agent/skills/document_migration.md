---
name: document-migration
description: Strategy for queuing documents for migration to a target system
type: rigid
category: document_migration
matcher_patterns:
classifier_hint: "Tasks asking to queue, migrate, or prepare documents for transfer to another system"
---

## Step 0: Pre-fetched context

A `PREFLIGHT` user message above (auto-dispatched by the router for this task shape) contains the canonical narrowing — the matching record(s), entity canonicalization, or destination resolution. Treat it as ground truth and start from those references. Fall through to the strategy below only if preflight returned nothing usable or the question needs more than what was pre-fetched.

## Search Strategy

1. Read the workspace documentation for migration instructions BEFORE
   taking any action. Look for process docs, migration guides, or
   system-specific instructions in the docs directory.

2. The target system's requirements, format, and conventions are defined
   in workspace docs — do not assume them. Read the relevant
   documentation to understand:
   - What format the migration queue expects
   - What metadata fields are required
   - What naming conventions to follow

3. Follow the documented migration format exactly. Do not invent fields
   or structure that the documentation does not specify.

4. Verify each referenced document exists before including it in the
   migration queue. Read the document to confirm it is the correct one.

5. If the migration instructions reference a specific directory structure
   or naming convention, follow it precisely. Do not use alternative
   paths or structures.

6. Ordering / batch-position fields (e.g. `queue_order_id`,
   `batch_position`, `migration_index`): when the workspace workflow
   says to derive an ordering from a sort, the sort key is the FULL
   repo-relative path of each file, NOT the basename. Compute the
   sort BEFORE any file is written. Concrete recipe:

   a. Resolve every requested file to its repo-relative path.
      Two files with the same basename in different directories
      are different files and the directory prefix matters for
      sorting.
   b. Build the list of full repo-relative paths exactly as
      `find` / the workspace tree would emit them (i.e. the
      directory chain plus the basename, joined with `/`).
   c. Sort that list with plain alphanumeric (lexicographic)
      ordering on the full path string. Earlier directory
      segments dominate later ones; basenames only break ties
      within the same directory.
   d. Assign the ordering field 1..N in that exact sorted order.
   e. Do NOT sort by basename, do NOT sort by user's listed
      order, do NOT sort by file-encounter order in your
      tool-calls, and do NOT split the batch by directory and
      assign per-directory IDs.

   Worked example (placeholder paths). User lists five basenames
   in arbitrary order: `bulk-foo.md, what-x.md, parking.md,
   sending.md, processing.md`. Resolved full paths in two
   directories `dir_a/notes/` and `dir_b/system/`:
     - `dir_a/notes/parking.md`
     - `dir_a/notes/what-x.md`
     - `dir_b/system/bulk-foo.md`
     - `dir_b/system/processing.md`
     - `dir_b/system/sending.md`
   Sorted alphanumerically by full path (this is the order the
   `queue_order_id` values must follow):
     - `dir_a/notes/parking.md` → 1
     - `dir_a/notes/what-x.md` → 2
     - `dir_b/system/bulk-foo.md` → 3
     - `dir_b/system/processing.md` → 4
     - `dir_b/system/sending.md` → 5
   Note that `bulk-foo.md` is NOT 1 even though `b` is the
   smallest letter of the basenames — its directory prefix
   (`dir_b/system/`) is greater than `dir_a/notes/`, so it
   sorts third. This is the most common failure mode for this
   skill: sorting by basename and putting `bulk-` first.
   Always sort the full path, not the basename.

7. Before submitting the answer, recompute the sort one more time
   against the actual list of files you wrote and verify each
   file's ordering field matches its position in the sorted
   full-path sequence. If a mismatch is detected, rewrite the
   affected files before answering.
