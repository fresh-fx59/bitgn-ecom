---
name: finance-lookup
description: Progressive search strategy for financial queries about past charges, invoices, or receipts
type: flexible
category: FINANCE_LOOKUP
matcher_patterns:
  - '(?i)charge.*total.*line.?item'
  - '(?i)how much.*\d+\s*days?\s*ago'
  - '(?i)total.*(invoice|receipt|bill).*ago'
  - '(?i)(invoice|receipt|bill).*charge.*total'
  - '(?i)(what was the total|total from).*\d+\s*days?\s*ago'
  - '(?i)how much.*(money|did we|have we).*(make|earn|made|earned).*(service\s+line|service\s+project)'
  - '(?i)(service\s+line|service\s+project).*(since|from|beginning)'
  - '(?i)\.md.*(these guys|them|this vendor|that company|the same folks)'
  - '(?i)how much.*(pay|paid|spend|spent).*(these guys|them|this vendor|that company)'
classifier_hint: "Tasks asking about financial totals, service line revenue, how much money earned from a service, or any query about past charges, invoices, receipts, or bills"
---

# Finance Lookup Strategy

You are answering a question about a past financial transaction — a charge, invoice, receipt, or bill from a specific vendor or for a specific item.

## Step 0: Pre-fetched context

A `PREFLIGHT` user message above (auto-dispatched by the router for this task shape) contains the canonical narrowing — the matching record(s), entity canonicalization, or destination resolution. Treat it as ground truth and start from those references. Fall through to the strategy below only if preflight returned nothing usable or the question needs more than what was pre-fetched.

**CRITICAL grounding rule:** You MUST `read` every file you reference in your answer or use for calculation. Preflight helps you *find* files faster, but the grader requires each referenced file to appear in your tool-call history. Never answer based solely on preflight summaries without reading the actual files.

## Step 0.4: Cited file + pronoun ("these guys", "them", "this vendor")

When the task cites a specific bill/invoice/receipt filename AND refers
to its source with a pronoun ("these guys", "them", "this vendor",
"that company", "the same folks"), the pronoun resolves to **that
file's `counterparty` field value** — not to filename tokens or the
bill alias.

**Why this matters:** Filename tokens like `studio_parts_*`,
`house_mesh_*`, `hearthline_*` are *product-line* / *project*
categories. A single product line often has bills from several
different vendors (counterparties). Summing by filename token
conflates vendors and returns a wrong answer.

**Procedure:**
1. Read the cited file first.
2. Extract its `counterparty` field value (that string is the
   vendor — e.g. a vendor name like `Filamenthütte Wien`).
3. Use that string — not the filename — as your search anchor:
   `search` (or `rg`) by the counterparty value across the finance
   records.
4. Read each match, confirm the `counterparty` field matches
   exactly, then sum `total_eur` across only those.

**Anti-pattern (do NOT do):** Task cites a bill file whose name
starts with `studio_parts_...` (or any other product-line tag) and
asks "how much to these guys?". Searching by the filename token
`studio_parts` can return bills with three different counterparties
(different vendors that all sold studio-parts category items).
Summing them gives a wrong total. The correct scope is a search by
the cited file's `counterparty` value, which pulls every bill from
that vendor regardless of product line.

## Step 0.5: Service Line vs Project Name

**CRITICAL distinction:** When the task mentions a "service line" or
"service project", the queried string is an INVOICE LINE ITEM name —
NOT a project name.

- **Service line** = a line item description inside an invoice's Line
  Items table (e.g. "operator workflow discovery sprint", "follow-up
  findings memo"). Search for it inside invoice files, in the line
  items section.
- **Project name** = the `project` field in invoice frontmatter (e.g.
  "Workflow Sprint", "Backfill Pack").

These are DIFFERENT things. An invoice's `project` field identifies
which project the invoice belongs to. The line items table lists the
specific services delivered. A "service line" query asks about line
items, NOT project names.

**How to compute the answer for service-line queries:**
1. Search all invoices for the exact line item name
2. Filter by date (issued_on >= start date)
3. Sum the `line_eur` values for matching line items — NOT the
   invoice `total_eur`. Each invoice may have multiple line items;
   only sum the lines that match the queried service name.

## Step 1: Anchor the Date

Calculate the reference date from the task's time expression (e.g., "51 days ago") using the current date from context. This is your approximate target — the actual filing date of records may differ significantly.

**CRITICAL — sandbox clock may be stale:** The sandbox `date -u` (or context_date) can lag the grader's reference date by weeks. If your computed target date falls *before* the earliest matching record, or if every matching record is *after* the computed target, the sandbox clock is stale. Trust the workspace: use the most recent matching record's date as a sanity check, and do not reject records simply because they are newer than your computed target.

## Step 2: Progressive Search

Start with the most specific artifact mentioned in the task and progressively broaden:

1. **Search by the most specific term first** — use the vendor name, item description, or amount mentioned in the task. Search across the entire workspace, not just one directory. **When preflight returned no entity match (match_found=false):** use `search` with the vendor name or item name — do NOT just read random files.
2. **If no results:** try partial matches — shorter vendor name, alternate spellings, abbreviations, or just the distinctive part of the name. For non-ASCII vendor names (Chinese, Arabic, etc.), try the exact Unicode characters from the task.
3. **If still no results:** search by a different artifact from the task — if you searched by vendor, now search by the item description, or vice versa.
4. **If still no results:** use broader workspace exploration — list financial directories, scan filenames for any recognizable fragment from the task.

Do NOT constrain your search to a narrow date range. Filing dates in filenames often differ from the transaction date the task references.

## Step 3: Cross-Validate and Select

When you find candidate files through any search path:

- Read each candidate fully
- **Primary match criteria: vendor name + item/line-item description.** These are the definitive identifiers.
- **Vendor mismatch is disqualifying.** If none of the candidate records' vendor fields match the vendor named in the task, do NOT answer with a number from any of them. Widen the search (Step 2.2 partial match, Step 2.3 different artifact, Step 2.4 broader listing) before falling back to `OUTCOME_NONE_CLARIFICATION`. A numeric answer pulled from a different vendor's invoice is worse than asking for clarification.
- **Date is contextual, NOT a strict filter.** The "N days ago" in the task is an approximate hint. The actual record's filing date or transaction date may differ significantly from the computed anchor date. Do NOT reject a record just because the date doesn't align — if vendor and item match, it IS the right record.
- **"Since" queries have NO upper bound.** When the task says "since January 2026" or "from date X", include ALL matching records from that date onward — even records dated after the context date. The workspace contains the full historical record; do NOT use today's date as an end filter. Read and sum ALL search results that match the query.
- **Single vendor+item match ALWAYS wins, regardless of date:** If only one record matches vendor + item, accept it. Do not reject it because its date is after the `context_date`, and do not report `OUTCOME_NONE_CLARIFICATION` just because the relative-date hint points at a different time window than the record. A single vendor+item match IS the answer. Apply no date filter.
- **Multiple matches for the same vendor + item — past-only floor, then MOST RECENT:** When two or more records match on vendor + item description AND the task uses past-tense relative-date phrasing (any wording that implies the event already happened), first drop candidates whose record date (`purchased_on`, `issued_on`, or equivalent) is strictly *after* the `context_date` from `context`, then select the latest date among the remaining. The past-only floor is purely a disambiguator between multiple otherwise-valid matches — it is NOT a global filter. If the floor leaves zero candidates, fall back to the MOST RECENT across the original multi-match pool (the original set was non-empty, so an answer exists — do not report `OUTCOME_NONE_CLARIFICATION`). Do **NOT** use closest-date-to-target; sandbox-clock staleness makes target-distance brittle.
  - The only exception: the task carries a concrete disambiguator beyond the relative-date hint (an explicit year, an explicit amount, or a unique line-item quantity). Absent such a disambiguator, past-only + most-recent wins.
  - **Never sum multiple candidates** when the task asks for a single-record quantity ("how much did X charge me for line Y"). Pick one, don't add.

## Step 4: Extract and Answer

- **Read every matching file** — if your search returned N results, read ALL N of them. Do not stop after reading 2 when your search showed more matches.
- Extract the exact numeric total for the requested line item from the selected record(s)
- For "total" or "since" queries, sum across ALL matching records — not just the first few
- Return the number only as your answer
- **Use OUTCOME_OK whenever you find a record matching vendor + item**, regardless of date alignment
- Do NOT use OUTCOME_NONE_CLARIFICATION when you have a matching record — a date mismatch is not grounds for clarification

Only use OUTCOME_NONE_CLARIFICATION if you have exhausted all progressive search strategies and genuinely found no matching vendor + item anywhere in the workspace.

## Revenue / Payment Aggregation

When asked "how much money", "total amount", or "total revenue" for a service line or vendor:

1. Use `search` to find **ALL** invoices matching the query term
2. Read **EVERY** matching invoice to extract amounts
3. **Sum all amounts** before reporting — do NOT answer from a single invoice
4. Include the count of invoices found in your `outcome_justification`

A revenue or payment total is always an aggregate across all matching records.
