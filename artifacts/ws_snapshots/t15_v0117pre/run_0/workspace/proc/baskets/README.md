# Baskets

Generated customer baskets live here. Basket files list customer, store, lifecycle status, and requested line items.

Basket records include `created_at`, generated from world time, so tasks can refer to basket recency such as "the last basket".

Baskets do not carry availability flags; compare basket lines with store inventory when checkout eligibility matters.

Discounted baskets carry a `discount` object with `percent`, `reason_code`, and `issuer_id`.

Basket status is lifecycle state. `active` baskets may still need inventory checks before checkout; `checked_out` baskets are historical orders and may have linked records under `/proc/payments/` and `/proc/returns/`.
