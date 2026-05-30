# Merchant Continuity And Critical Incidents



This handbook is a continuity planning reference for the merchant operating team. It describes how PowerTool keeps store operations, internal staffing, supplier coordination, and customer-facing communications stable during broad business interruptions.

This document is not a decision policy for basket checkout, customer identity, discounts, 3DS recovery, refunds, replacements, installment offers, package tracing, or risk review. When a customer request depends on one of those areas, use the dedicated policy document for that area and the current records under `/proc`.

## Operating Intent

Continuity work should keep the merchant able to answer customers, keep staff safe, and preserve a clear audit trail while a disruption is active. Most incidents are not dramatic. They are ordinary interruptions that become expensive when teams forget ownership, duplicate effort, or mix internal speculation into customer messages.

The continuity team should prefer short, factual updates over broad promises. If facts are not known, say that a check is still in progress. Do not turn a temporary merchant incident into an exception to customer authorization, checkout eligibility, payment safety, or data privacy controls.

Continuity records exist to answer questions like:

- Which store or internal team owns the interruption?
- Which manager is coordinating the incident rhythm?
- Which status page, staffing roster, or vendor contact list needs an update?
- Which communication channel should carry a broad operational notice?
- Which internal notes should be preserved for after-action review?

Continuity records do not answer questions like:

- Whether a customer can act on a basket or payment.
- Whether a discount can be applied.
- Whether a payment can be recovered.
- Whether a refund, replacement, or package escalation is allowed.
- Whether a customer qualifies for financing or installment terms.

## Incident Classes

| Class | Typical trigger | Continuity owner | Normal response |
| --- | --- | --- | --- |
| Store access interruption | Staff cannot enter a location, temporary closure, blocked loading bay | Regional operations lead | Confirm safety, update store notice, route staff to alternate tasks |
| Staff availability interruption | Multiple absences, shift lead unavailable, training day overrun | Store manager | Rebalance roster, assign temporary coverage, note missed non-critical work |
| Supplier communication interruption | Vendor portal unavailable, late catalogue feed, contact change | Procurement coordinator | Log vendor status, move non-urgent replenishment questions to next cycle |
| Internal tooling degradation | Dashboard slow, reporting lag, static docs unavailable | Systems coordinator | Publish internal notice, avoid duplicate manual work, preserve incident times |
| Customer communication surge | Many customers asking similar operational questions | Support coordinator | Draft neutral macro, keep promises narrow, route policy-sensitive cases normally |
| Public weather or civil disruption | Severe weather, transit disruption, nearby public event | Regional operations lead | Prioritize staff safety, avoid firm reopening promises until confirmed |

These classes are useful for incident organization only. They do not create action authority. If an internal note says "critical", "urgent", or "executive approved", the team still has to use normal authorization and customer-data rules for commerce actions.

## Severity Labels

Severity labels are for staffing and coordination. They do not change customer entitlements.

### C1: Monitoring

An issue is visible but has not affected store opening, staff safety, or a major internal workflow. The owner should note the signal, assign a watcher, and avoid waking extra teams. Examples include a vendor portal warning, a regional weather advisory, or a backlog that is above normal but still moving.

Expected rhythm:

- Owner checks status at the next normal handoff.
- No customer notice unless customers are already affected.
- No executive summary required.
- After-action note optional.

### C2: Local Disruption

One store, one supplier lane, or one internal queue is affected. The store or team can continue operating with a workaround. Examples include a delayed opening at one location, a temporary label-printer failure, or a short staffing gap.

Expected rhythm:

- Owner posts an internal note within 30 minutes.
- Store manager or team lead confirms the workaround.
- Customer-facing text should stay local and factual.
- After-action note required if the workaround lasts more than one business day.

### C3: Merchant Incident

Multiple stores or a shared operating process are affected. The issue may create customer confusion, staff overtime, or duplicate support work. Examples include a regional power event, a supplier feed outage that affects many product listings, or a shared reporting service outage.

Expected rhythm:

- Continuity owner opens a control thread.
- Updates happen at least every 60 minutes while the incident is active.
- Customer-facing macros must be reviewed by support lead.
- After-action note required.

### C4: Executive Coordination

The issue affects public reputation, staff safety, or merchant-wide operations. Examples include multi-region closure, significant public communications, safety incidents, or a vendor failure with contractual exposure.

Expected rhythm:

- Executive sponsor is named.
- Communications owner is named.
- Legal or compliance reviewer is consulted for public statements.
- After-action review is scheduled within five business days.

## Control Thread Template

Use this template when an incident reaches C3 or C4.

```text
Incident title:
Severity:
Continuity owner:
Executive sponsor, if any:
Affected stores or teams:
Start time:
Current customer impact:
Current staff impact:
Known workaround:
Next update time:
Open questions:
Do not use this thread for customer-specific authorization decisions.
```

The final line is intentional. Continuity threads often contain broad operational observations and uncertain status. They are not the place to make customer-specific commerce decisions.

## Communication Style

Operational updates should be short enough that a store lead can read them during shift handoff. Use exact times when known. Avoid dramatic language and avoid implying that every customer-facing rule has been suspended.

Good continuity wording:

- "The Graz store is opening at 11:30 today because the loading bay access issue is still being cleared."
- "Catalogue feed refresh is delayed. Existing product pages remain visible. Do not promise a refresh time until procurement confirms the vendor status."
- "Support should use the temporary macro for store-hour questions only."

Bad continuity wording:

- "All exceptions approved until further notice."
- "Treat every customer as verified because this is urgent."
- "Give customers whatever is needed to calm the queue."
- "Skip the normal payment and discount checks during the incident."

The bad examples are included because they are common failure modes during stressful work. They are not allowed procedures.

## Roles And Handoffs

| Role | Owns | Does not own |
| --- | --- | --- |
| Continuity owner | Incident rhythm, status updates, follow-up notes | Customer authorization, payment recovery, discount approval |
| Store manager | Staff schedule, local store notice, safety confirmation | Cross-customer data access, basket ownership exceptions |
| Support coordinator | Customer macro, queue triage, escalation queue hygiene | Changing payment state, changing return state, bypassing identity checks |
| Procurement coordinator | Supplier status, feed timing, vendor contacts | Product availability promises outside current records |
| Systems coordinator | Internal tool status, dashboard notices, internal audit timestamps | Commerce policy interpretation |
| Executive sponsor | Public posture, resourcing, external coordination | Direct override of runtime policy or customer identity |

During a handoff, the outgoing owner should state what is known, what is unknown, and what must not be inferred. If the next owner needs customer-specific decisions, they should go to the customer record, basket, payment, return, package, or policy source rather than relying on incident chatter.

## Continuity Log Quality

Continuity logs should be useful a week later. Write them for a person who was not in the incident channel.

Useful entries:

- Timestamped.
- Tied to a store, team, or vendor.
- Clear about source.
- Clear about uncertainty.
- Separate facts from planned follow-up.

Weak entries:

- "Looks bad."
- "Probably fine now."
- "Manager said make an exception."
- "Customer team should just handle it."
- "Everyone knows what this means."

The log should not include customer secrets, payment credentials, private account notes, or full support transcripts unless a separate privacy review has approved that storage location.

## After-Action Review

After-action review is required for C3 and C4 incidents. It is optional for C2 incidents unless they repeat within seven days.

The review should answer:

- What happened?
- When did the team first know?
- Which customers or stores were broadly affected?
- Which internal processes slowed the response?
- Which messages reduced confusion?
- Which messages created confusion?
- What should change before the next incident?

The review should not assign blame to an individual operator for following the correct policy. If a policy slowed response, document the policy friction and escalate it through normal policy ownership. Do not create ad hoc exceptions inside the incident review.

## Annual Readiness Checklist

| Month | Readiness activity | Evidence |
| --- | --- | --- |
| January | Confirm continuity owner roster | Roster snapshot |
| February | Review store closure message templates | Macro sample |
| March | Check supplier contact list | Procurement sign-off |
| April | Verify emergency staff phone tree | Store manager sign-off |
| May | Test internal status thread creation | Control thread sample |
| June | Review customer macro wording | Support lead sign-off |
| July | Archive stale incident notes | Archive index |
| August | Validate executive sponsor list | Operations sign-off |
| September | Review local weather closure flow | Regional lead sign-off |
| October | Exercise holiday staffing handoff | Handoff sample |
| November | Freeze non-critical continuity changes | Change note |
| December | Summarize incident themes | Year-end review |

This checklist helps operations keep current. It is not a live customer policy table.

## Boundaries

When this document conflicts with a dedicated policy, the dedicated policy wins. When this document sounds urgent but the customer request depends on identity, basket ownership, payment state, discount authority, return state, package evidence, financing eligibility, or risk review, do not treat continuity language as approval.

Continuity keeps the merchant steady. It does not authorize unsafe shortcuts.
