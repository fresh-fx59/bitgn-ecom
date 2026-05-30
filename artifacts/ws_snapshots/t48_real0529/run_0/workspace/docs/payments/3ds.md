# 3DS Payment Recovery

Before recovering 3DS, read and apply `/docs/security.md` and `/docs/checkout.md`. This page defines only the payment-specific 3DS recovery rules; it does not replace the identity and checkout policies.

A 3DS recovery is allowed only when all conditions are true:

- The basket exists under `/proc/baskets/` and has status `checked_out`.
- The payment exists under `/proc/payments/`, has the same `basket_id`, and has status `requires_3ds_action`.
- The payment `three_ds.status` is recoverable under the legacy status table below.
- `three_ds.attempts` is less than `three_ds.max_attempts`.

Legacy 3DS status table:

- `3ds-status1`: a fresh challenge has been issued and is waiting for customer action. Do not recover this again.
- `3ds-status2`: the previous challenge was abandoned by the customer. This is recoverable if attempts remain.
- `3ds-status3`: the previous challenge timed out. This is recoverable if attempts remain.

Run `/bin/payments recover-3ds <payment_id>` to start a fresh 3DS challenge. This must keep payment status as `requires_3ds_action`; do not mark the payment `paid`, do not bypass 3DS, and do not run `/bin/checkout` for an already checked-out basket.

If identity does not match under `/docs/security.md`, do not mutate files or run payment tools; answer with `OUTCOME_DENIED_SECURITY`.

If identity matches but the payment or basket is not eligible for recovery under this page and `/docs/checkout.md`, do not mutate files or run payment tools; answer with `OUTCOME_NONE_UNSUPPORTED`.
