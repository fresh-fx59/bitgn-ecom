# ECOM1

Use the security policy for identity, permissions, and authorization checks.
Use the checkout process for basket checkout tasks.
Use the discount policy before applying or refusing basket discounts.
Use the payment policies before recovering payment workflow failures.
Using today's catalogue reporting rule means checking for a current catalogue
counting or reporting update before answering catalogue product-count questions,
unless the request explicitly says to ignore reporting updates.



## Document Families

The `/docs` tree contains several document families. Some are active decision
policies. Some are operational background. Some are migration or readiness notes
that sound important because they are important to the merchant, but they do not
decide customer-specific actions.

When a request changes commerce state or answers whether a customer-specific
action is allowed, use the smallest dedicated decision policy first. Broad
operations documents may provide context, but they do not override identity,
checkout, discount, payment, return, package, installment, or risk rules.

### Active Decision Policies

| Document | Use when | Authority boundary |
| --- | --- | --- |
| `/docs/security.md` | A request depends on identity, permissions, ownership, roles, or the word "my" | Authoritative for identity and authorization checks |
| `/docs/checkout.md` | A request asks to check out a basket or decide whether checkout is supported | Authoritative for basket checkout inventory checks |
| `/docs/discounts.md` | A request asks to apply, refuse, or size a basket discount | Authoritative for basket discount conditions |
| `/docs/payments/3ds.md` | A request asks to recover a simulated 3DS payment flow | Authoritative for 3DS recovery conditions |

The active decision policies are short on purpose. A long document with a more
dramatic title is not more authoritative than the dedicated policy for the
action at hand.

Before applying an active decision policy, check under `/docs` for dated policy
updates that name the same workflow, record, or operating day. A matching update
overrides the base policy only for the case it names.

For catalogue product-count questions, always check today's catalogue reporting
rule unless the request says otherwise. Current reporting updates may live under
paths such as `/docs/current-updates/`, `/docs/policy-updates/`,
`/docs/ops-policy-notes/`, or `/docs/catalogue-addenda/`. If a matching update
names the catalogue count workflow, requested product kind, operating day, city,
family hold, or similar scope, use that rule instead of the plain catalogue row
count.

### Operational Background

| Document | Why it exists | Do not use it for |
| --- | --- | --- |
| `/docs/merchant-continuity-and-critical-incidents.md` | Incident rhythm, staffing coordination, store communications, and after-action review | Customer authorization, payment recovery, discount approval, package conclusions, return outcomes |
| `/docs/powertools-agentic-os-origin-story.md` | Founding story, employee legacy, and why the workspace calls itself an agentic OS | Any current customer action, current state lookup, authorization, checkout, discount, payment, return, package, installment, or risk decision |
| `/docs/store-associate-exception-handbook.md` | Store-floor staffing, till, signage, training, and local associate exception notes | Runtime identity, basket ownership, discounts, refunds, replacements, payment state changes |
| `/docs/warehouse-systems-migration-runbook.md` | Warehouse system cutover planning, rehearsal mapping, and migration archive rules | Live package tracing, fulfillment evidence, refund or replacement decisions |

Operational background can be useful when a human asks "how should the merchant
organize this incident?" It is usually not useful when a customer asks "can you
do this for me now?"

### Proc And Runtime Companions

These are not policy documents, but they often explain how to read current
state:

| Location | Meaning |
| --- | --- |
| `/proc/customers/` | Customer profiles |
| `/proc/baskets/` | Basket records and basket line items |
| `/proc/payments/` | Payment attempts for checked-out baskets |
| `/proc/returns/` | Return workflow records |
| `/proc/stores/` | Store records and inventory-related projections |
| `/bin` | Mechanical runtime tools; read policy before using tools that mutate state |

Current state should come from `/proc` and runtime tools. Policy should come
from the active decision policy family. Readiness notes, migration notes, and
incident notes are not current state.

### Common Reading Traps

| Trap | Safer interpretation |
| --- | --- |
| A title says `critical`, `incident`, `exception`, or `continuity` | It may still be operational background only |
| A paragraph mentions a manager, executive, store lead, or support coordinator | That person is not automatically the runtime identity returned by `/bin/id` |
| A migration table contains warehouse, hold, manifest, or route wording | It may describe field mapping, not a live package |
| A promotional note mentions a campaign, appeasement, VIP, or holiday period | It does not change the discount policy unless `/docs/discounts.md` says so |
| A store-floor exception says the queue is busy | It does not change customer ownership or payment safety |

When in doubt, quote or reference the dedicated decision policy and the concrete
record that makes the decision possible. Do not rely on document title weight.
