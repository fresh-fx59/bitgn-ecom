# Availability Checks



Use only branch inventory rows when answering branch availability, inventory,
or stock-count questions.

Same-day availability is `max(on_hand - reserved, 0)`.

If a SKU is absent from a branch inventory record, treat that SKU as `0`
same-day availability at that branch.

Incoming stock counts only when the user asks to include incoming stock. When
the user gives a due-within window, include only incoming rows with
`arrival_in_days` inside that window.

Availability checks are read-only unless the user asks for an export file. Do
not mutate catalogue, store, or inventory records.

## Inventory Exports

When exporting inventory for a product family, include every product whose
product JSON has the requested `family_id`, even if that SKU is absent from the
branch inventory.

Sort export rows alphabetically by SKU.

For today's date column, write same-day availability. For each future date
column, write the incoming quantity arriving exactly on that date. Use `0` when
there is no branch inventory row or no incoming quantity for that date.

Export writes are report files only.
