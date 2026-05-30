# 3DS Payment Recovery



Before recovering 3DS, read and apply `/docs/security.md` and
`/docs/checkout.md`. Use `/bin/id` for the current actor and `/bin/date` for
the current time.

Payment records live under `/proc/payment-ledger`. Basket records live under
`/proc/carts`.

In this workspace, a 3DS session allows up to
2 attempts.

A 3DS recovery is allowed only when all conditions are true:

- `/bin/id` reports a customer identity.
- The payment `customer_id` matches the current customer identity.
- The payment `status` is `requires_3ds_action`.
- The payment has a `basket_id`, and that basket exists with status
  `checked_out`.
- The payment `basket_id` matches the basket `id`, and both records have the
  same `customer_id`.
- The payment has a `three_ds` object.
- `three_ds.attempts` is less than `three_ds.max_attempts`.
- The current 3DS status is recoverable under the status table below.

Legacy 3DS status table:

- `3ds-status1`: a fresh challenge is already active. Do not start another
  challenge before the payment `three_ds.retry_after` timestamp. Use
  `/bin/date`; if the current time is earlier than `retry_after`, leave the
  payment unchanged and tell the customer when retry is available. If current
  time is at or after `retry_after`, a fresh challenge may be started if
  attempts remain.
- `3ds-status2`: the previous challenge was abandoned by the customer. This is
  recoverable if attempts remain.
- `3ds-status3`: the previous challenge timed out. This is recoverable if
  attempts remain.

Run this command to start a fresh 3DS challenge:

```sh
/bin/payments recover-3ds <payment_id>
```

The command keeps payment `status` as `requires_3ds_action`, sets
`three_ds.status` to `3ds-status1`, increments `three_ds.attempts`, and writes a
new `three_ds.retry_after` timestamp. In this workspace, new challenges use a
30 minute retry delay.

Do not mark the payment `paid`, do not bypass 3DS, and do not run
`/bin/checkout` for the already checked-out basket.

If identity does not match under `/docs/security.md`, do not mutate files or
run payment tools; answer with `OUTCOME_DENIED_SECURITY`.

If identity matches but the payment or basket is not eligible for recovery under
this page and `/docs/checkout.md`, do not mutate files or run payment tools;
answer with `OUTCOME_NONE_UNSUPPORTED`.
