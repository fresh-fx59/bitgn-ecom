# Warehouse Systems Migration Runbook



This runbook describes how the merchant operations team prepares, rehearses, and validates a warehouse systems migration. It is written for internal migration coordinators who need a shared procedure for dry runs, field mapping, cutover communication, and post-migration cleanup.

This runbook is not live warehouse evidence. It does not determine where a customer package is, whether a fulfillment exception happened, whether a package is missing, or whether a refund, replacement, or escalation is allowed. For live customer questions, use current package, order, fulfillment, delivery, and policy records when those records exist.

## Migration Goals

The warehouse migration should move operational data from the legacy warehouse planning system into the new warehouse control surface without losing field meaning, audit timestamps, or store routing context. The migration team should prefer traceable field preservation over clever normalization.

Primary goals:

- Preserve stable identifiers where they are already used by store and warehouse teams.
- Map legacy location codes into documented new-zone labels.
- Keep rehearsal data separate from live operational records.
- Avoid turning migration notes into customer-facing status evidence.
- Provide a rollback plan for technical cutover only.
- Produce a readable summary for warehouse managers after each rehearsal.

Non-goals:

- Redefining customer refund or replacement policy.
- Creating new shipment-status meanings.
- Reclassifying package loss, delay, or delivery outcomes.
- Granting support agents permission to override customer-data rules.
- Changing payment, checkout, discount, or return workflows.

## Migration Phases

| Phase | Name | Owner | Output |
| --- | --- | --- | --- |
| M0 | Inventory of fields | Migration analyst | Field inventory table |
| M1 | Mapping rehearsal | Warehouse systems coordinator | Mapping report |
| M2 | Store routing rehearsal | Regional operations lead | Store-route diff |
| M3 | Parallel read | Systems coordinator | Comparison snapshot |
| M4 | Cutover window | Migration owner | Cutover log |
| M5 | Post-cutover review | Warehouse managers | Review memo |
| M6 | Archive legacy exports | Data steward | Archive index |

The phases are about system migration. They are not the lifecycle of a package. A package can be in a live fulfillment flow while the migration team is testing unrelated historical records. Do not confuse rehearsal output with live evidence.

## Field Mapping Table

The table below is intentionally representative. It shows how migration teams discuss field meaning. It is not a live data table.

| Legacy field | New field | Required | Notes |
| --- | --- | --- | --- |
| `legacy_item_ref` | `item_ref` | Yes | Preserve exact string; do not trim leading zeros |
| `wh_loc` | `warehouse_zone` | Yes | Map through zone dictionary |
| `bin_no` | `bin_code` | No | Preserve if present |
| `staged_flag` | `staging_indicator` | No | Boolean source may be `Y`, `N`, blank |
| `route_hint` | `store_route_hint` | No | Internal routing hint only |
| `hold_reason` | `migration_hold_note` | No | Not a customer-support reason code |
| `scan_dt` | `last_migrated_scan_at` | No | Timestamp from migration extract |
| `manifest_ref` | `manifest_reference` | No | Legacy manifest label, not carrier proof |
| `operator` | `migration_operator` | No | Person or service running the export |
| `batch_no` | `migration_batch` | Yes | Migration batch identifier |

If a field name resembles fulfillment language, remember the context: this is field mapping, not package status.

## Zone Dictionary

| Legacy zone | New zone label | Migration note |
| --- | --- | --- |
| A1 | ambient-small-parts | Standard shelf stock |
| A2 | ambient-tools | Large hand tools |
| B1 | bulky-inbound | Inbound bulky inventory |
| B2 | bulky-outbound | Outbound bulky staging |
| C1 | counter-service | Customer pickup support area |
| C2 | repair-hold | Repair desk storage |
| D1 | damaged-review | Internal quality inspection |
| D2 | vendor-return-hold | Supplier return staging |
| E1 | seasonal-overflow | Temporary seasonal stock |
| X9 | unmapped-review | Requires migration analyst review |

The dictionary is a migration aid. It does not say that any particular customer package is in one of these zones. Live package work requires live package records.

## Dry-Run Controls

Every dry run must be marked as rehearsal output.

Required dry-run header:

```text
run_type: rehearsal
warehouse_system: migration
batch_id:
source_extract_time:
target_environment:
operator:
customer_visible: false
```

The `customer_visible: false` field is important. Rehearsal output can contain stale routes, duplicate sample rows, synthetic holds, or historical package-like identifiers. It is not evidence for a support decision.

Dry-run files may include:

- Historical examples.
- Synthetic records.
- Store-route samples.
- Partial extracts.
- Invalid rows deliberately used to test validation.
- Archive records that no longer reflect current warehouse state.

Dry-run files must not be used to tell a customer that a package is lost, found, delayed, shipped, delivered, refunded, or eligible for replacement.

## Rehearsal Validation

Each rehearsal should produce three counts:

| Count | Meaning | Expected use |
| --- | --- | --- |
| Extract rows | Rows read from the source export | Migration completeness |
| Accepted rows | Rows that mapped into the target schema | Mapping quality |
| Review rows | Rows that require analyst review | Migration cleanup |

Review rows should be sampled by type:

- Missing required field.
- Unknown zone.
- Timestamp parse failure.
- Duplicate legacy reference.
- Store-route mismatch.
- Historical row outside migration range.
- Invalid rehearsal marker.

These review categories are not package exception categories. For example, "store-route mismatch" means a migration field did not map cleanly. It does not prove a customer's parcel was routed to the wrong store.

## Cutover Window Procedure

The cutover window should be quiet, boring, and reversible at the system level.

1. Freeze legacy warehouse configuration changes.
2. Confirm all rehearsal outputs are archived away from live operational folders.
3. Confirm target environment is empty or explicitly ready for import.
4. Export final source snapshot.
5. Run mapping import.
6. Compare row counts.
7. Compare zone totals.
8. Run store-route validation.
9. Ask warehouse managers to check a small sample of internal records.
10. Publish internal cutover note.
11. Keep old system read-only during observation period.

The cutover note should not include customer-specific status promises. It may say "warehouse migration complete" or "legacy system read-only"; it should not say "all delayed packages are resolved" unless a live package workflow separately proves that statement.

## Rollback Language

Rollback is a technical migration action. It returns system ownership to the legacy warehouse planning system. It does not reverse customer orders, refund payments, replace items, or change package status.

Acceptable rollback language:

- "Rollback returns warehouse planning edits to the legacy system."
- "Rollback should preserve the final source snapshot for audit."
- "Rollback requires store-route notice because managers may see old labels again."

Unacceptable rollback language:

- "Rollback means every missing package is found."
- "Rollback cancels all fulfillment exceptions."
- "Rollback authorizes automatic replacement for affected customers."
- "Rollback permits customer support to skip package evidence review."

## Manager Review Sample

Warehouse managers should review a small sample after rehearsal and after cutover.

Sample checklist:

- Does the item reference look familiar?
- Is the zone label plausible?
- Is the store route still meaningful?
- Are empty optional fields acceptable?
- Are historical rows clearly marked?
- Are dry-run records separated from live records?
- Are review rows easy for analysts to find?

Manager review is about operational readability. It is not a customer support investigation.

## Common Migration Traps

### Trap: Package-Like Identifiers

Legacy exports may contain identifiers that look like tracking numbers, package ids, manifests, tote ids, or basket ids. They may be historical, synthetic, or only meaningful inside the migration extract. Do not treat a matching string as live evidence unless it comes from the current live record source.

### Trap: Hold Reasons

Legacy `hold_reason` values are migration notes. They can say things like "damaged_review", "vendor_return", "missing_label", or "route_unknown". In this runbook, those labels explain why a row needed migration review. They do not decide a customer outcome.

### Trap: Store Route Hints

Store route hints are used to validate routing fields. A route hint may point to a store that no longer owns the customer interaction. Do not infer customer pickup or delivery status from a migration hint.

### Trap: Reused Batch Numbers

Batch numbers can repeat across rehearsal environments. A migration batch is not a customer shipment batch. It is a technical grouping used by import tooling.

### Trap: Archive Completeness

Archive completeness means the migration team saved enough files to explain the cutover. It does not mean all packages, returns, or customer cases are complete.

## Example Cutover Note

```text
Warehouse migration cutover note

Window: 2026-04-12 22:00-23:15 UTC
Owner: Warehouse systems coordinator
Source snapshot: final-legacy-export-20260412
Target import batch: wh-migrate-20260412-final
Row count: accepted within expected range
Review rows: analyst queue created
Observation period: 48 hours

Scope: This note confirms system migration status only. It is not package
status evidence and does not authorize customer-specific refunds, replacements,
discounts, payment changes, or support escalations.
```

The scope line should remain in cutover notes. Migration records are tempting because they are detailed and operational. Detail is not the same as authority.

## Archive Rules

Archive migration evidence under a migration-owned location. Include:

- Source extract checksum.
- Mapping script version.
- Target import checksum.
- Rehearsal batch ids.
- Cutover batch id.
- Analyst review summary.
- Manager sample checklist.
- Rollback readiness note.

Do not archive raw customer support conversations in the warehouse migration archive. If support references the migration in a customer case, the customer case should retain its own evidence in the appropriate support workflow.

## Final Boundary

This runbook helps the merchant move warehouse systems safely. It is intentionally rich in operational words like warehouse, hold, route, staging, manifest, and review. Those words describe migration mechanics here. They do not prove anything about a live package, customer entitlement, refund, replacement, checkout, payment, discount, installment, or risk decision.
