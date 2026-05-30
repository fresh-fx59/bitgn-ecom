# Prod-Faithful Local Test Harness — Design

**Goal:** Remove the fidelity drift between the local test harness and real ECOM1-PROD so count/availability and product-resolution families can be A/B'd locally. Build it as a **separate prod-faithful snapshot** (not by mutating the dev snapshots).

## The drift (root cause)

The local A/B of v0.1.156 levers showed count/resolution "OK" tasks failing **0% at baseline** under `BITGN_LOCAL_SQL_UNAVAILABLE=1` — not because the agent is wrong, but because the local harness does not expose the data PROD exposes as files. Two drifts:

1. **Data drift.** Local snapshots keep inventory in the SQL `store_inventory` table and stores at dev paths. With SQL off, the agent has no inventory to read → refuses. On PROD, inventory is **embedded in the store record** at `/proc/locations/<City>/<store-id>.json`; the agent reads it from files.
2. **Documentation drift.** Snapshot `/AGENTS.MD` + `/docs` are the **dev** versions ("inventory lives only in SQL projections"). PROD ships a different `/AGENTS.MD` (file-based, no-SQL) and `/docs/availability-checks.md` defining `same-day = max(on_hand - reserved, 0)`.

(PROD ground truth captured in `artifacts/prod_explore/prod_truth/`: real `AGENTS.MD` + 18 docs + store/catalog samples, mined from live raw dumps.)

## Approach: a snapshot **materializer**

`scripts/build_prod_snapshot.py --from <real_snapshot> --out <dir>` converts an existing `*_real*` snapshot (prod-schema `catalogue.db` + `metadata.json`) into a standalone prod-faithful workspace:

- `/AGENTS.MD` ← captured PROD `AGENTS.MD`
- `/docs/**` ← captured PROD docs (incl. nested `/docs/payments/3ds.md`)
- `/proc/catalog/<Brand>/<sku>.json` ← one file per `product_variants` row, PROD shape `{id,sku,name,brand,category_id,kind_id,family_id,price_cents,properties}` (no inventory)
- `/proc/locations/<City>/<store_id>.json` ← one file per `stores` row, PROD shape with **embedded `inventory:[{sku,on_hand,reserved,incoming?}]`** built from `store_inventory`. **`available_today` is intentionally omitted** — the agent must compute `max(on_hand-reserved,0)` (the real source of count errors).
- **No `catalogue.db`** in the output → SQL is genuinely unavailable.

Served by the existing `LocalEcomClient` (already serves real on-disk files), run with `BITGN_LOCAL_SQL_UNAVAILABLE=1`. Because every record is a real file in the PROD layout, no synth/`BITGN_LOCAL_PROD_PATHS` is needed.

### Faithful `/bin/sql`
`BITGN_LOCAL_SQL_UNAVAILABLE=1` currently returns a generic "sql backend unavailable" stderr. Update it to the **exact PROD ODBC stderr** (`Sqlcmd: Error: Microsoft ODBC Driver 18 for SQL Server : Login timeout expired...`, exit 1) so the agent sees the real failure and takes its filesystem-fallback path.

## Components / interfaces
- `scripts/build_prod_snapshot.py` — pure-Python; reads db, writes files; no LLM/network. CLI `--from/--out [--truth <dir>]`.
- `LocalEcomClient._exec_sql` — ODBC-timeout stderr under the SQL-unavailable flag.
- `tests/local/test_build_prod_snapshot.py` — asserts store JSON has `inventory[]` with `on_hand`/`reserved` and **no** `available_today`; catalog JSON shape; PROD `AGENTS.MD`/docs present; no `catalogue.db`.

## Scope (YAGNI)
- In: locations+inventory, catalog, docs/AGENTS.MD, faithful `/bin/sql`. These cover the broken families (count/availability, resolution).
- Deferred: materializing `/proc/staff|carts|payment-ledger|return-workflows` (those families were not the broken ones; add later if a family needs it).

## Validation
Materialize a count snapshot (e.g. `t16_real2 → t16_prod`), run the agent 2-3× with `BITGN_LOCAL_SQL_UNAVAILABLE=1`: the agent must now **read inventory from the store file and emit a count** (no longer refuse for data-absence). That proves the drift is removed and unblocks faithful count A/B (deferred Task 2B).
