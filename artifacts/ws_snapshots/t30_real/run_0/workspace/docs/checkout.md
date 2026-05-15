# Checkout Process

Before checking out a basket, apply `docs/security.md`, then compare every basket line with inventory for the basket's `store_id`.

- If every line has `quantity` less than or equal to `available_today`, checkout can proceed.
- If any line has no matching inventory row, or its `quantity` is greater than `available_today`, checkout is unsupported. Do not mutate files or run checkout; answer with `OUTCOME_NONE_UNSUPPORTED`.
- If the basket is already `checked_out`, do not run `/bin/checkout` again.



## Store Desk Checkout Vocabulary

Store associates use informal checkout words in notes, desk handoffs, and
training material. These words are not basket state. They do not replace the
inventory comparison above, and they do not authorize `/bin/checkout`.

| Store-desk phrase | Common human meaning | Checkout meaning |
| --- | --- | --- |
| `green basket` | The desk thinks the customer is ready to buy | Still check basket ownership, basket status, and inventory |
| `counter-ready` | Items were discussed at the service counter | Not a checkout state |
| `manual close` | A store-floor conversation should be wrapped up | Do not run checkout from this phrase alone |
| `pickup-prepared` | Staff prepared a pickup area or note | Not proof that all basket lines are available |
| `floor-approved` | A floor associate thinks the sale looks normal | Not a runtime authorization |
| `manager waved through` | A manager wanted the queue to keep moving | Not an inventory or identity decision |
| `reserve looked fine` | Staff believed stock was nearby | Use current inventory projection instead |
| `customer waiting` | The person is physically or socially present | Does not prove `customer_id` |
| `desk reviewed` | The desk looked at some details | Not equivalent to checkout eligibility |
| `hold shelf` | An item may be on a shelf for staff handling | Not equivalent to `available_today` |
| `queue-save` | Staff wanted to save time during a rush | Not an action permission |
| `paper basket` | A handwritten or printed list of desired items | Not a `/proc/baskets/` record |
| `legacy ready` | An old register view marked something ready | Use current basket and inventory records |
| `branch promise` | A branch made a customer-service promise | Does not bypass checkout policy |
| `quick complete` | Human shorthand for finishing a sale | Still requires the checks above |

These phrases can be useful when understanding a store note. They should not be
used as values in a basket record, and they should not be treated as synonyms
for `active`, `checked_out`, or `available_today`.

### Desk Note Examples

The examples below are intentionally store-floor language. They are not checkout
instructions.

```text
Counter note: Customer says the basket is green and asks us to quick complete.
Handling: Use security policy, then compare the basket lines with inventory.
```

```text
Manager note: Floor-approved if customer returns before close.
Handling: This is not runtime identity and not inventory evidence.
```

```text
Handoff note: Paper basket left at desk for pickup-prepared workflow.
Handling: Find the actual basket under /proc/baskets before deciding anything.
```

### Checkout Source Order

For checkout tasks, source order matters:

1. Use `/docs/security.md` for identity and ownership.
2. Use the target basket record under `/proc/baskets/`.
3. Use inventory for the basket's `store_id`.
4. Apply the checkout bullets at the top of this file.
5. Ignore informal desk vocabulary when it conflicts with the concrete checks.

The checkout result should be boring: either proceed only when every condition
is met, or stop without mutation when a condition fails.
