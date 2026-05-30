# Purchase Request Crosslist



Use this policy for uploaded competitor purchase request OCRs and crosslist TSV
reports.

Resolve the target PowerTools branch from the OCR. Competitor codes are not
PowerTools SKUs.

Match a requested line only when the requested item description and every listed
spec match one catalogue product exactly. OCR spec labels with spaces correspond
to catalogue property keys with underscores.

Use the normalized catalogue product name for `requested_description` when the
request description resolves to a catalogue product, even if a listed spec
conflicts.

If a requested spec conflicts with the catalogue product, do not substitute
another item. Use `match_status` exactly `property_mismatch`, leave
`matched_sku` and `matched_product_name` blank, and set `available_today` and
`fulfillable_qty` to `0`.

For exact matches, use `match_status` exactly `exact`. `available_today` is
`max(on_hand - reserved, 0)`. `fulfillable_qty` is
`min(requested_qty, available_today)` only when `branch_open` is true;
otherwise it is `0`.

Use these `reason` values exactly:

- `requested properties do not exactly match catalogue product`
- `target branch is closed today`
- `exact property match; requested quantity available today`
- `exact property match; branch has insufficient same-day stock`

Report columns must be exactly:

```text
line_no, competitor_code, requested_description, requested_qty, branch_id, branch_open, match_status, matched_sku, matched_product_name, available_today, fulfillable_qty, short_qty, reason
```

Use tabs, no quotes, and no total row.
