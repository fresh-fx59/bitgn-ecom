---
name: bill-query
description: Strategy for answering questions about specific fields on a bill or invoice record
type: flexible
category: BILL_QUERY
matcher_patterns:
  - '(?i)(how many|number of)\s+lines?\s+(on|does|of|in)\s+(the|my|a)\s+bill'
  - '(?i)purchased?\s+date\s+(on|of|for)\s+(the|my|a)\s+bill'
  - '(?i)quantity\s+of\s+.+\s+(on|in|from)\s+(the|my|a)\s+bill'
  - '(?i)price\s+of\s+.+\s+(on|in|from)\s+(the|my|a)\s+bill'
  - '(?i)bill\s+from\s+.+\s+(how many|number of|lines|quantity|price|date)'
classifier_hint: "Tasks asking about specific fields on a bill: line count, purchased date, quantity, or price of items"
---

# Bill Query Strategy

You are answering a question about a specific field on a bill or invoice record.

## Step 0: Pre-fetched context

A `PREFLIGHT` user message above (auto-dispatched by the router for this task shape) contains the canonical narrowing — the matching record(s), entity canonicalization, or destination resolution. Treat it as ground truth and start from those references. Fall through to the strategy below only if preflight returned nothing usable or the question needs more than what was pre-fetched.

## Field Disambiguation

Bill records contain structured data. Map the task's question to the correct field:

- **"number of lines" / "how many lines"** → Count of **line items** in the bill (the number of distinct products/services listed), NOT the number of text lines in the file.
- **"purchased date" / "date of purchase"** → The **date** field on the bill record.
- **"quantity of [item]"** → The **qty** or **quantity** field for that specific line item.
- **"price of [item]"** → The **unit_price** or **price** field for that specific line item.
- **"total"** → The **total** or **amount** field (may be per-line or whole-bill depending on context).

## Search Strategy

1. Identify the vendor or entity mentioned in the task.
2. Search the workspace for bill or invoice records associated with that vendor. Search broadly — bills may be in finance directories, document stores, or project folders.
3. Read each candidate record fully to confirm it belongs to the correct vendor.
4. Extract the requested field using the disambiguation above.

## Common Pitfall

"Lines" on a bill means **line items** (distinct entries for products or services), not raw text lines. A bill with 3 products has 3 lines, regardless of how many text lines the file contains. Count the entries in the items/line_items section of the record.

## Answer Format

Return the exact value from the record. For line counts, return the integer count. For dates, return the date as stored. For quantities and prices, return the numeric value.
