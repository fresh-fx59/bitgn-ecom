# Payments

Generated payment records live here. Current payment files represent one payment attempt for a `checked_out` basket.

Older payments may have `basket_archived: true`. Those basket records have aged out of `/proc/baskets/` into cold storage, so the payment file carries its own `lines` snapshot with SKU, quantity, and unit price.

Payment statuses are compact:

- `paid`: payment completed successfully.
- `requires_3ds_action`: payment is stuck in the simulated 3DS workflow.

Stuck payments include a `three_ds` object with legacy challenge status, failure reason, attempts, and maximum attempts. The `three_ds.status` values intentionally use inherited 3DS codes; consult the payment policy before deciding whether a payment can be recovered.

Payment fingerprints are opaque instrument/device identifiers. `observed_lat` and `observed_lon` are checkout-time coordinates from the payment risk log, not a customer address.
