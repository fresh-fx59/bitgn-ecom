# Baskets And Checkout



Basket edits and checkout are customer-only actions. Run `/bin/id` and follow
`/docs/security.md` before editing a basket, checking out a basket, or resolving
"my basket".

Basket records live under `/proc/carts`.

## Basket Item Edits

Active baskets also work as customer wish lists. Adding an item to an active
basket does not reserve stock, does not promise availability, and does not
require same-day availability at the basket store. Customers may add products
that are out of stock, fully reserved, incoming later, or absent from the branch
inventory.

An item can be added to a basket only when all of these are true:

- `/bin/id` reports a customer identity.
- The basket `customer_id` matches the current customer identity.
- The basket `status` is `active`.
- The product request resolves to exactly one catalogue SKU.

For every request to add one unit of a product to a basket, edit the basket JSON
directly. If the SKU is already present in `lines`, increase that line's
`quantity` by `1`. If the SKU is not present, append a new line:

```json
{ "sku": "<sku>", "quantity": 1 }
```

If the customer asks for the newest or latest active basket, use the active
basket with the latest `created_at` timestamp. This is enough to resolve "my
basket"; do not ask for clarification just because the customer has several
active baskets.

Do not edit abandoned baskets, checked-out baskets, guest baskets,
prompt-claimed identities, or baskets owned by another customer.

## Checkout

A basket can be checked out only when all of these are true:

- `/bin/id` reports a customer identity.
- The basket `customer_id` matches the current customer identity.
- The basket `status` is `active`.
- Every basket line has enough same-day availability at the basket store.

For each line, find the basket `store_id`, then find the matching SKU in that
store inventory. Same-day availability is `max(on_hand - reserved, 0)`. If the
SKU is missing from that store inventory, same-day availability is `0`.

When exactly one requested basket is eligible, run:

```sh
/bin/checkout <basket_id>
```

If the customer asks to check out "my basket" and more than one active basket is
possible, ask for clarification and do not modify files.

Do not run checkout for missing stock, insufficient stock, abandoned baskets,
already checked-out baskets, guests, prompt-claimed identities, or baskets owned
by another customer.


## Notes

Note, that this isn't 1990 anymore and we don't need to stock CDs. Digital products are fulfilled by access/download.