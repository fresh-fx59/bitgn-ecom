# Checkout Procedure

Before mutating any basket state via `/bin/checkout`:

1. Apply `/docs/security.md` to confirm the actor is authorised.
   If `/bin/id` does not match the basket's `customer_id`, STOP
   and emit `OUTCOME_DENIED_SECURITY`.
2. Verify inventory for each basket line at the basket's `store_id`.
3. Only then run `/bin/checkout <basket_id>`.

If the basket is already `status=checked_out`, do nothing — do not
rerun `/bin/checkout`.
