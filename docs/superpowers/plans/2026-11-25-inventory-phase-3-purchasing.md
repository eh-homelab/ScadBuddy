# Inventory phase 3: generic purchasing and invoice line links — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suppliers, offers, price points and invoices serve every item. A spool type is an
item of kind `filament` and a spool is a unit of it, in the same model as every other item
and unit (owner, R-P3-30). An invoice line of any kind receives lots or units, or goes to the
entities it links to. A confirmed invoice can be amended, and the confirmed invoices that
are missing their non-filament lines get them back through a reviewed amend. Order purchases
link invoice lines. Job cost reads spool cost from the spool's unit (#7068, epic #6949).

**Architecture:** The filament purchasing tables get their generic names (`suppliers`,
`invoices`, `invoice_lines`). `filament_supplier_products` and `filament_price_points` move
into phase 2's `supplier_offers` and `offer_price_points`, with their ids kept. A spool type
becomes an item with the type's id, and a spool becomes a unit with the spool's id. The unit
carries the spool's cost, as every unit carries its own. One receive path, one confirm path
and one candidates path serve every item. The spool workflow (grams, the ledger of spool
events, trays, tags, labels, presets, K values, Bambuddy sync) hangs off the unit and the
item as an extension; it never forks the shared model. A new `invoice_line_links` table
links a line to any entity. A link on an expense line writes one consumer row (a printer
maintenance event or an order cost entry) and remembers it, so a change reverses it exactly.
Only the API process runs any of it (#5295).

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, React + TypeScript, TypeScript
agent tools (Claude Agent SDK).

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.2 (the
invoice file), §6.3a (the filament kind's schema), §6.4 (filament items and units), §6.5,
§6.6 (cost snapshots, job cost), §6.7, §6.11, D1, D7, D9, D13, D14, D15, D16, §9 phase 3.
The phase 2 plan `docs/superpowers/plans/2026-11-23-inventory-phase-2-item-core.md` and its
rulings R-P2-1 to R-P2-44 (comment on #7067). #7648 (the filament skill and non-filament
invoice lines).

## What exists on main today (verified at `814f1285c`, 2026-11-25)

- Phase 2 is merged (#7393, #7405, #7457, #7474, #7475, #7476). `supplier_offers` and
  `offer_price_points` exist, for non-filament items only, with FKs to
  `filament_suppliers` and `filament_invoice_lines`
  (`backend/scadbuddy/migrations/20261124T0323Z_inventory_core.sql:56-87`).
  `supplier_offers.sku` is `CHECK (length(sku) BETWEEN 1 AND 100)` (l.60). `stock_lots` and
  `stock_units` have `invoice_line_id` (l.107, l.134). `FilamentItemRefused` blocks filament
  items in `ItemStore.create` (`inventory/items.py:176`), offers (`inventory/offers.py:249`)
  and stock (`inventory/stock.py:785`). `receive_units` writes one `receive` move per unit;
  `set_unit_status` writes `use`, `sell`, `retire`, `scrap` or `return`
  (`inventory/stock.py:610-698`).
- Filament purchasing: `filament_suppliers`, `filament_supplier_products` (unique
  `(supplier_id, upper(sku))`, `pack_size integer`, `product_url`), `filament_invoices` (four
  `file_*` columns, blob holder `filament_invoice`), `filament_invoice_lines`
  (`quantity integer` 1..1000, `type_id`, `supplier_product_id`, `proposed_type`,
  `update_spool_ids`), `filament_price_points`, `filament_shopping_items` (`material text NOT
  NULL`) (`migrations/20261117T2042Z_filament_suppliers.sql`). A line's `quantity` counts
  spools: `price_per_pack = line_total / quantity × pack_size` (`filament/invoice_money.py:87`).
  `InvoiceStore.confirm` (`filament/invoices.py:534`) refuses an invoice outside the shop's
  currency (Decision 6), locks the claimed spools in id order, then confirms line by line.
  Confirm checks only `total` against the lines plus shipping (`difference`, l.571); the
  header `subtotal` and `tax` are not compared with the lines. Product SKUs reach 128
  characters (`_SKU_MAX`, l.46). Every line must be filament (#7648).
- SQL on those tables: `filament/suppliers.py` (31 statements), `invoices.py` (30),
  `bb_sync_plan.py` (13), `shopping.py` (8), `metrics_store.py` (7), `spools.py` (6),
  `inventory/part_alias.py` (5), `inventory/offers.py` (4), `bb_sync_results.py` (2),
  `bambuddy_pull.py` (1), `entities/registry.py` (2) (`grep -rc`).
- `bb_sync_plan._suppliers()` (l.1665-1690) keeps a supplier that is linked, has a filament
  product, has an invoice, or has an open create intent (R-P2-8 to R-P2-10). Products and
  prices are read at l.350 and l.727; l.727 reads `pp.updated_at`, which
  `offer_price_points` lacks.
- Spool inserts: `FilamentSpoolStore.create_on` (`filament/spools.py:711`) and the pull
  (`filament/bambuddy_pull.py:1290`). Spool delete: `spools.py:1097`. Type insert:
  `filament/types.py:253`. `filament_spool_types.code` has no length CHECK.
  `filament_spools.cost_per_kg` and `filament_spool_types.cost_per_kg` are `numeric(12,4)`.
- Spool events: `filament_spool_events` has no cost column. A LAN start books
  `print_started` per slot with `ref_kind = 'print_run'`, `ref_id = run/subtask/slot`
  (`filament/print_uses.py`); a Bambuddy usage row books `print_ended` with
  `ref_kind = 'bb_usage'`, paired by job (`filament/ledger.py` rule 3).
- Job cost: an attempt's filament is its grams at the quote's price per gram, else the
  price book (`orders/costs.py:419-475`). `Fulfilment.print_run_id` names its run.
- Order purchases: `OrderLifecycle.record_purchase` writes a cost entry with source
  `purchase:<source id>` and a `purchase` charge (`orders/lifecycle.py:2654`).
  `order_item_sources.cost_entry_id` is `UNIQUE`. `order_cost_entries` has a unique index
  `(order_id, kind, source)` and `amount_minor bigint CHECK (> 0)`
  (`migrations/20261105T1508Z_order_cost_entries.sql:29-46`).
- `printer_maintenance_events` (`migrations/20261118T1105Z_machine_rate_actuals.sql:8-31`):
  `id uuid` with no default, `consumables numeric(14,4)`, `UNIQUE (printer, source)`.
- Entities: `KINDS` names `spool` and `spool_type` on their filament tables, `supplier` on
  `filament_suppliers` and `invoice` on `filament_invoices` (`entities/registry.py:37-55`).
  Attachments hold their blob as holder `attachment` (`orders/store.py:373`), key
  `attachment-<sha256>` (`orders/attachments.py:120`). The invoice file uses the same key
  form (`attachments.keep_file`).
- Agent: `filament_supplier_*`, `filament_product_*`, `filament_price_*`,
  `filament_invoice_*`, `filament_shopping_*` (`agent/src/tools/filamentPurchasing.ts`); the
  import route `POST /api/v1/ai/filament/invoice-import` refuses a confirmed invoice (409
  `confirmed`, `agent/src/routes/invoiceImport.ts:134`); its prompt
  `agent/src/filament/invoiceImportPrompt.ts`. Tool risks are `read`, `write` and `outward`
  (gated). Skill `plugins/scadbuddy/skills/filament/SKILL.md`, plugin version `0.1.61`.
- UI: `frontend/src/pages/filament/InvoicePage.tsx`, `InvoicesTab.tsx`, `SupplierPage.tsx`,
  `SuppliersTab.tsx`, `ShoppingListTab.tsx`; routes `filament/invoices/:id`,
  `filament/suppliers/:id` (`frontend/src/App.tsx:133-140`). Purchases are recorded in
  `components/orders/CustomerModels.tsx`.

## The shared model and the spool workflow (R-P3-30)

The owner's ruling (2026-11-25): "Spools and other items should be treated the same --
spools just have a different work flow associated with them."

| Shared model (every item and unit, filament included) | Spool workflow (filament only) |
|---|---|
| The item (`items`, kind `filament`), its kind schema, properties, links, files | The spool type's catalog fields (`filament_spool_types`: material, colour, temperatures, label weight, insert type) |
| Offers, price points, suppliers, invoice lines | Bambuddy sync of suppliers, offers and prices of filament items (D16) |
| The unit (`stock_units`): identity, status, cost, origin, invoice line, `receive`/`retire`/`return` moves | The spool row (`filament_spools`): grams left, label weight, location and tray, tags, labels |
| Receive, confirm, amend, candidates, re-cost (`StockStore`, `InvoiceStore`) | The ledger of spool events (`filament_spool_events`, D1), AMS capture, presets, K values |
| The unit's cost (`stock_units.cost_minor`) | `filament_spools.cost_per_kg`, the per-kg form of the unit's cost, written only with it |
| Job cost's snapshot rule (D14) and "Re-cost past uses" | A use is a ledger event with `cost_per_kg_at`, not a `stock_moves` row (D1) |

The workflow joins the model at two points. `StockStore` calls a `UnitExtension` for a kind
whose `tracking` is `filament` when it receives, retypes, changes the status of, or deletes a
unit. The filament component registers the extension; it makes or changes the spool row in
the same transaction. A `filament` kind takes every call a `unit` kind takes; its lot calls
answer `WrongTracking`, as a unit kind's do.

## Global Constraints

- Only the API process holds SQL. Agent tools and the frontend call CRUD routes (root
  CLAUDE.md "Database access (#5295)"). Add no database client, role or grant.
- Migrations: new files only, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, later than every file
  on main when made. Never edit a merged one (root CLAUDE.md "Migrations (#491)"). The
  layer-3 data migration ships like any other PR: no owner-run gate and no deploy window
  (owner, 2026-11-25; R-P3-29). It handles every data case itself, each with a test, and
  old pods keep working through views during the rollout.
- Never commit `backend/openapi.json` or any `schema.d.ts`.
- Print and slicer settings take no per-key allowlist (decision on #2998). Phase 3 adds
  none.
- Kinds and attribute schemas are rows. No allowlist of kinds or keys in code (spec §4).
- One model: no code path refuses or forks on a filament item or a spool unit except where
  the table above puts the work in the spool workflow (R-P3-30).
- A `none` kind (`service`) is not counted: it takes offers and prices, never lots, units or
  moves (owner Ruling 1 of phase 2). On an invoice it is an `expense` line only.
- Job cost keeps its `parts` figure from `stock_moves` (owner Ruling 3 of phase 2). Phase 3
  changes only how an attempt's filament is priced (D7).
- Pack prices are offer price points (owner Ruling 4 of phase 2). Filament price points move
  there too.
- #7278 (spool parts) starts after #7068 (owner Ruling 7 of phase 2). Phase 3 gives it
  `filament_spools.unit_id`.
- Money: invoices stay in the shop's `orders_currency`; confirm refuses another
  (Decision 6). Stock costs are shop minor units, `numeric(20,6)`. A lot received by hand in
  another currency opens cost-unknown and keeps `foreign_unit_cost_minor` (R-P2-11, R-P2-12);
  an invoice lot or unit never does, because its invoice is in the shop's currency. A price
  point is in its source's currency (R-P2-36).
- A move's cost is a snapshot (D14). A spool use event's cost is `cost_per_kg_at`, taken
  from its unit's cost when the event is written. A later cost change reaches past job cost
  only through "Re-cost past uses".
- Lock order, in one transaction: orders (by id), then the invoice row, then each item's
  `_open_lock` and its lots and units in `item_id` order (R-P2-32), then spool rows in id
  order (R-P3-11).
- Only filament reaches Bambuddy (D16). A supplier, offer or price point of a non-filament
  item is never sent.
- No input reaches SQL unchecked: NUL is 422, integers are bounded to their columns, a 500 is
  never an answer to input (R-P2-16).
- SKU unique per `(supplier_id, upper(sku), item_id)` among the unarchived (D13).
- Entity wire form `kind:ref`. New kind `invoice_line` (uuid). `spool_type:<id>` and
  `spool:<id>` stay accepted as aliases of `item:<id>` and `unit:<id>` (R-P3-5).
- Nothing changes a confirmed invoice's lines without a person: an amend that writes is
  confirmed by a person in the UI, or by a person's approval of the agent's gated
  `invoice_amend` (R-P3-34).
- Agent tool risks: reads `read`; writes `write`; `invoice_amend` `outward`. Every result
  names `ui_path` or `<field>_ui_path` (R-P2-43). Descriptions cite their sources (#259).
- Each layer: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format
  --check . && uv run --frozen mypy`; `cd frontend && pnpm lint && pnpm typecheck`;
  `cd agent && pnpm lint && pnpm typecheck` for the trees it touches.
- Conventional-commit titles with `(#7068, N/7)`; PR body `Part of #7068` (the last layer:
  `Fixes #7068`; layer 6 also `Fixes #7648`). Every fact in docs and comments cites its
  source (#259). Every doc and comment a layer touches matches its code.
- Load `frontend-design` before Task 11.

## Pre-flight (cross-layer contracts; check before Task 1)

1. **Ids are kept.** An offer keeps its product's id. An offer price point keeps its price
   point's id. A filament item keeps its spool type's id (`filament_spool_types.item_id =
   id`). A spool's unit keeps the spool's id (`filament_spools.unit_id = id`). So every id
   in an alias route, an activity detail and an `entities.ref` stays valid (R-P3-2, R-P3-3).
2. **Renames land in layer 1 with views.** `suppliers`, `invoices` and `invoice_lines` are
   the tables from layer 1 on. Views with the old names serve the old pods during the
   rolling update, as `20261122T0426Z_entities.sql` kept `order_attachments`. A follow-up
   issue drops the views (R-P3-1).
3. **Receive in the caller's transaction.** Every stock write that an invoice confirm or
   amend makes takes a `conn`: `StockStore.receive_in`, `receive_units_in`,
   `set_lot_cost_in`, `set_unit_cost_in` (Task 3). `set_unit_cost_in` is the one writer of a
   unit's cost and, through the extension, of its spool's `cost_per_kg` (R-P3-6).
4. **Consumers.** Only a confirmed `expense` line's links write consumer rows (R-P3-9, owner
   accepted). `InvoiceLinkStore.rewrite_in(conn, line_id, by)` is the one writer. Confirm,
   amend, the link routes and the order purchase routes call it.
5. **Coverage.** Each new route is a tool or a `coverage.ts` entry, or
   `agent/test/coverage.test.ts` fails (`agent/CLAUDE.md` l.397). Layer 2 adds temporary
   entries that name the layer-6 tools; layer 6 drops them.
6. **Data cases the migration handles** (R-P3-29). Each query below names a case that
   Task 7 handles in SQL and pins with a seeded-row test; none stops the migration except a
   true invariant (step 1). The control session runs them read only on prod
   (`deploy/CLAUDE.md` "Prod checks") as a cross-check when it can, and records the answers
   in the layer-3 PR body. They are not a merge gate:
   - counts of `filament_supplier_products`, `filament_price_points`, `filament_spool_types`,
     `filament_spools`, `invoices` by `status`, and `filament_spool_events` by `ref_kind`
     where `ref_kind IN ('print_run','bb_usage')`;
   - `SELECT count(*) FROM filament_supplier_products WHERE length(sku) > 128` (expect 0);
   - `SELECT count(*) FROM filament_supplier_products WHERE product_url IS NOT NULL AND product_url !~* '^https?://'` (Task 7 stores those as no URL);
   - `SELECT max(length(code)) FROM filament_spool_types` (expect at most 200, the item name cap);
   - `SELECT count(*) FROM items WHERE id IN (SELECT id FROM filament_spool_types)` and `SELECT count(*) FROM items WHERE kind_id = 'filament'` (expect 0 and 0);
   - `SELECT count(*) FROM filament_spool_types a JOIN filament_spool_types b ON a.id < b.id AND lower(a.code) = lower(b.code) WHERE a.archived_at IS NULL AND b.archived_at IS NULL` (expect 0; the items index is on `lower(name)`);
   - `SELECT count(*) FROM filament_spools WHERE type_id IS NULL` (these get no unit);
   - `SELECT count(*) FROM filament_spools WHERE cost_per_kg IS NULL` (their units open cost-unknown);
   - `SELECT count(*) FROM filament_spool_events e JOIN filament_spools s ON s.id = e.spool_id LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE e.ref_kind IN ('print_run','bb_usage') AND coalesce(s.cost_per_kg, t.cost_per_kg) IS NULL` (uses with no cost; the spec expects 0, §6.6);
   - `SELECT kind, count(*) FROM entities WHERE kind IN ('spool','spool_type') GROUP BY 1` and `SELECT kind FROM entity_property_schemas` (Task 7 moves them to `item` and `unit`);
   - `SELECT count(*) FROM invoices WHERE file_key IS NOT NULL AND file_key !~ '^attachment-[0-9a-f]{64}$'` (expect 0; Task 7 reads the sha256 from the key);
   - `SELECT count(*) FROM blob_refs WHERE holder_kind = 'filament_invoice'` and the count of invoices with a file (expect equal);
   - `SELECT currency, count(*) FROM filament_price_points GROUP BY 1`;
   - `SELECT count(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE i.status = 'confirmed' AND l.supplier_product_id IS NULL` (expect 0);
   - the backfill signals (R-P3-31): `SELECT count(*) FROM invoices i WHERE status = 'confirmed' AND (abs(subtotal - (SELECT sum(line_subtotal) FROM invoice_lines WHERE invoice_id = i.id)) > 0.05 OR abs(tax - (SELECT sum(line_tax) FROM invoice_lines WHERE invoice_id = i.id)) > 0.05)`.
   Where an "expect" does not hold, Task 7 must already convert that case (for example:
   cut a SKU to 128, store a bad URL as no URL, suffix a clashing name, leave a typeless
   spool without a unit, price a costless use as unknown).
7. **Job cost before and after** (layer 4, R-P3-18). Just before layer 4 deploys, the owner
   saves `GET /api/v1/orders/reports/profit?from=<now − 30 days>` and `GET
   /api/v1/orders/{id}/costs` for each order accepted in that range. Right after the deploy,
   the owner reads them again. Layer 4 changes only reads, so the diff is the change. The
   layer-4 PR gets the table as a comment.

## Review Focus

1. An amend whose new header tax or shipping is below what the confirmed lines already hold:
   409 `amend_short`, nothing written; with "Re-spread over all lines" it is allowed. Tests
   in Task 2 and Task 5.
2. A link change on a confirmed expense line whose order is closed: the old cost entry is
   deleted and the new one written in one transaction, the order gets a
   `cost.invoice_line_changed` event, and a repeat of the same change writes nothing.
   Tests in Task 4.
3. A line whose SKU covers two items at the supplier (a bundle, D13) and that names no item:
   confirm refuses it with a line error that names both items, never a guess. Test in Task 5.
4. A spool that is archived, restored, retyped or deleted after the migration, or whose cost
   a person edits: its unit follows (`retired`, `in_stock`, the new type's item, gone) and
   `cost_minor` and `cost_per_kg` still agree. A typeless spool gets its unit when it gets a
   type. Test in Task 8.
5. A backfill amend on an invoice whose spools fed past orders: after "Re-spread over all
   lines", the spools' unit cost drops, and the unit re-cost lowers those orders' filament
   cost by the same share; an attempt on a spool with no cost falls back to the quote, then
   the price book, and says which basis priced it (R-P2-33). Tests in Task 5 and Task 10.

---

## Layer 1 — `feat/7068-1-purchasing-store` (from main)

### Task 1: The purchasing schema migration and the renames

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_purchasing_core.sql`
- Modify: every SQL string that names a renamed table (Pre-flight 2; `grep -rln
  "filament_suppliers\|filament_invoices\|filament_invoice_lines" backend/scadbuddy
  backend/tests`), `backend/scadbuddy/entities/registry.py` (`supplier` on `suppliers`,
  `invoice` on `invoices`, new `invoice_line` on `invoice_lines`, uuid)
- Test: `backend/tests/inventory/test_purchasing_schema.py`, `backend/tests/test_pg_migrations.py`,
  `backend/tests/entities/test_registry.py`

The migration, in one file (a top comment cites #7068 and spec §6.5, §6.7):
- `ALTER TABLE filament_suppliers RENAME TO suppliers`, `filament_invoices` → `invoices`,
  `filament_invoice_lines` → `invoice_lines`; rename their indexes to the new prefix. Then
  `CREATE VIEW filament_suppliers AS SELECT * FROM suppliers` and the same for the other
  two, with a comment naming the follow-up that drops them (R-P3-1).
- `invoices ADD COLUMN amended_at timestamptz`, `lines_review text CHECK (lines_review IN
  ('flagged','done'))`, `amend_proposal jsonb` (R-P3-32, R-P3-34).
- `invoice_lines ADD COLUMN item_id uuid REFERENCES items (id)`, `offer_id uuid REFERENCES
  supplier_offers (id)`, `kind text NOT NULL DEFAULT 'stock' CHECK (kind IN
  ('stock','expense','asset'))`, `pack_size numeric(20,6) NOT NULL DEFAULT 1 CHECK (pack_size
  > 0)`, `proposed_item jsonb`, `CHECK (proposed_type IS NULL OR proposed_item IS NULL)`.
  Index `(item_id) WHERE item_id IS NOT NULL` (R-P3-7).
- `invoice_line_links (id uuid PK DEFAULT gen_random_uuid(), line_id uuid NOT NULL REFERENCES
  invoice_lines (id) ON DELETE CASCADE, entity_id uuid NOT NULL REFERENCES entities (id),
  share numeric(9,4) NOT NULL DEFAULT 1 CHECK (share > 0), note text NOT NULL DEFAULT ''
  CHECK (length(note) <= 1000), maintenance_event_id uuid REFERENCES
  printer_maintenance_events (id) ON DELETE SET NULL, cost_entry_id text REFERENCES
  order_cost_entries (id) ON DELETE SET NULL, created_at timestamptz NOT NULL DEFAULT
  clock_timestamp(), created_by jsonb NOT NULL, UNIQUE (line_id, entity_id), CHECK
  (maintenance_event_id IS NULL OR cost_entry_id IS NULL))`; index `(entity_id)`.
- `invoice_events (id uuid PK, invoice_id uuid NOT NULL REFERENCES invoices (id) ON DELETE
  CASCADE, at timestamptz NOT NULL DEFAULT clock_timestamp(), kind text NOT NULL CHECK (kind
  IN ('confirmed','amended','respread','links_changed','lines_review','proposal')), detail jsonb NOT
  NULL, actor jsonb NOT NULL)`; index `(invoice_id, at)` (R-P3-13).
- `order_item_sources ADD COLUMN invoice_line_id uuid REFERENCES invoice_lines (id)`.
- `filament_spool_events ADD COLUMN cost_per_kg_at numeric(12,4) CHECK (cost_per_kg_at >= 0)`.
- `filament_spool_types ADD COLUMN item_id uuid REFERENCES items (id), ADD CHECK (item_id IS
  NULL OR item_id = id)`; `filament_spools ADD COLUMN unit_id uuid REFERENCES stock_units
  (id), ADD CHECK (unit_id IS NULL OR unit_id = id)` (R-P3-3).
- `filament_shopping_items ADD COLUMN item_id uuid REFERENCES items (id)`, `ALTER COLUMN
  material DROP NOT NULL`, `CHECK (type_id IS NOT NULL OR item_id IS NOT NULL OR material IS
  NOT NULL)` (R-P3-19).
- `supplier_offers`: drop the `sku` length CHECK and add `CHECK (length(sku) BETWEEN 1 AND
  128)` (R-P3-26). `CREATE UNIQUE INDEX offer_price_points_line ON offer_price_points
  (invoice_line_id) WHERE invoice_line_id IS NOT NULL` (the filament table's rule).

- [ ] **Step 1: Write the failing tests** (`requires_postgres`, `pg_pool`):
  - `test_the_old_names_still_read_and_write`: an `INSERT INTO filament_suppliers (name)
    VALUES ('Acme')` through the view lands in `suppliers`; `SELECT … FROM filament_invoices
    … FOR UPDATE` works.
  - `test_a_link_is_unique_per_line_and_entity`: a second `(line, entity)` raises
    `UniqueViolation`; `share = 0` raises `CheckViolation`; a link with both consumer ids
    raises `CheckViolation`.
  - `test_a_type_item_and_a_spool_unit_keep_the_owner_id`: `item_id` ≠ `id` raises
    `CheckViolation`.
  - `test_an_offer_sku_takes_128_characters`: 128 inserts; 129 raises `CheckViolation`.
  - `test_a_shopping_item_needs_a_type_an_item_or_a_material`.
  - `test_a_line_pack_size_is_positive`: `pack_size = 0` raises `CheckViolation`; the
    default is 1.
  - `test_registry.py`: `KINDS["invoice_line"].table == "invoice_lines"`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_purchasing_schema.py tests/entities/test_registry.py -q` — Expected: FAIL.
- [ ] **Step 3: Write the migration; move every SQL name.** No behavior changes in this step.
- [ ] **Step 4: Run** the same plus `uv run --frozen pytest tests/filament tests/inventory tests/test_pg_migrations.py -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): generic purchasing tables, links and events (#7068, 1/7)`

### Task 2: Amend arithmetic

**Files:**
- Modify: `backend/scadbuddy/filament/invoice_money.py`
- Test: `backend/tests/filament/test_invoice_money.py`

**Interfaces:**
- Produces: `class KeptLine(NamedTuple): line_tax: Decimal; line_total: Decimal;
  allocated_cost: Decimal`; `class Allocation(NamedTuple): line_tax: Decimal; line_total:
  Decimal; allocated_cost: Decimal; tax_basis: str`; `class AmendShortError(ValueError)`
  with `.field: Literal["tax", "shipping"]` and `.short: Decimal`;
  `amend_spread(kept: Sequence[KeptLine], subtotals: Sequence[Decimal], given_tax:
  Sequence[Decimal | None], tax: Decimal, shipping: Decimal) -> list[Allocation]`. The
  tax left (`tax − Σ kept.line_tax`) goes through `tax_per_line` over the new lines; the
  shipping left (`shipping − Σ (allocated_cost − line_total)` of the kept) goes through
  `allocate` over the new lines' totals (spec §6.7 "What an amend may change", D15).
  `pack_price(line_total: Decimal, quantity: int, line_pack: Decimal, offer_pack: Decimal)
  -> Decimal` = `line_total / (quantity × line_pack) × offer_pack` (R-P3-7).

- [ ] **Step 1: Write the failing tests**:
  - `test_an_amend_spreads_only_the_tax_left_over_the_new_lines` (spec §6.7's receipt):
    kept `[KeptLine(11.12, 171.12, 171.12)]`, subtotals `[4740.00]`, tax `334.28`,
    shipping `0` → `[Allocation(323.16, 5063.16, 5063.16, "receipt-wide 6.8%")]`;
    `171.12 + 5063.16 == 5234.28`.
  - `test_an_amend_spreads_only_the_shipping_left`: kept `[KeptLine(0, 100.00, 105.00)]`,
    subtotals `[300.00, 100.00]`, tax `0`, shipping `25.00` → allocated `[315.00, 105.00]`.
  - `test_an_amend_below_what_is_held_is_short`: kept `[KeptLine(10.00, 110.00, 110.00)]`,
    tax `8.00` → `AmendShortError` with `field == "tax"`, `short == Decimal("2.0000")`.
  - `test_pack_price`: `pack_price(Decimal("30.00"), 4, Decimal(1), Decimal(1)) ==
    Decimal("7.5000")`; a spool line of 8 spools at 160.00 (`line_pack` 1) for an offer of
    4 → `Decimal("80.0000")`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament/test_invoice_money.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): amend arithmetic spreads only what no line holds (#7068, 1/7)`

### Task 3: Receive from a line; the unit extension; the ledger snapshots cost

**Files:**
- Modify: `backend/scadbuddy/inventory/stock.py`, `backend/scadbuddy/inventory/offers.py`,
  `backend/scadbuddy/inventory/items.py`, `backend/scadbuddy/inventory/models.py`,
  `backend/scadbuddy/filament/ledger.py` (`record`)
- Test: `backend/tests/inventory/test_stock.py`, `test_offers.py`, `test_items.py`,
  `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Produces (`inventory/models.py`): `class UnitExtension(Protocol)` with
  `received(conn, item: Row, units: Sequence[Unit], body: ReceiveUnits, by: Actor) -> None`,
  `retyped(conn, unit_id: UUID, item_id: UUID) -> None`, `status_set(conn, unit_id: UUID,
  status: UnitStatus) -> None`, `cost_set(conn, unit_id: UUID, cost_minor: Decimal | None)
  -> None`, `deleting(conn, unit_id: UUID) -> None`. `StockStore(pool, entities,
  shop_currency, extensions: Mapping[Tracking, UnitExtension] = {})`. Layer 3 registers the
  filament one (Task 8). `ReceiveUnits` gains `unit_ids: list[UUID] | None` (ids the caller
  chose, one per unit) and `extra: dict[str, Any] | None` (the extension's fields, such as a
  spool's label weight).
- Produces (`StockStore`):
  - `receive_in(conn, item_id, body, by, *, shop, origin: Literal["manual", "invoice_line"] =
    "manual", invoice_line_id: UUID | None = None) -> Lot` (the phase 2 method gains two
    keywords).
  - `receive_units_in(conn, item_id: UUID, body: ReceiveUnits, by: Actor, *, origin=…,
    invoice_line_id=None, in_service: bool = False) -> list[Unit]`; `receive_units` calls it.
    It takes `unit` and `filament` kinds alike and calls the kind's extension, if any, after
    the inserts. `in_service` writes a `use` move per unit, as `set_unit_status` does
    (R-P3-8).
  - `fill_units_in(conn, item_id, unit_ids: Sequence[UUID], cost_minor: Decimal, by, *,
    invoice_line_id: UUID) -> list[Unit]`: units received earlier with no invoice line take
    the line and its cost (today's `update_spool_ids`, #6700, #6707, for every unit item;
    R-P3-30). A unit on another line, of another item, or listed twice is refused.
  - `set_lot_cost_in(conn, lot_id, unit_cost_minor, by) -> Lot`;
    `set_unit_cost_in(conn, unit_id, cost_minor: Decimal | None, by) -> Unit` (None sets
    `cost_unknown`; no move changes, D14; calls the extension's `cost_set`). It is the one
    writer of a unit's cost (R-P3-6).
  - `set_unit_status_in(conn, unit_id, body: UnitStatusInput, by) -> Unit` (`set_unit_status`
    in the caller's transaction; calls the extension's `status_set`).
  - `recost_unit(conn, unit_id) -> RecostResult`: the unit's `use`/`return` moves at its
    cost now; for a kind with an extension, the extension's re-snapshot too (Task 10).
  - `of_line(conn, line_id: UUID) -> tuple[list[Lot], list[Unit]]`.
- Produces (`OfferStore`): `match_in(conn, supplier_id, item_id, sku) -> Offer | None`
  (unarchived, `upper(sku)`); `by_sku_in(conn, supplier_id, sku) -> list[Offer]`;
  `create_in(conn, body: OfferInput, by) -> Offer`; `add_price_in(conn, offer_id, *, price:
  Decimal, currency: str, on_date: date, invoice_line_id: UUID) -> OfferPricePoint`
  (`source = 'invoice'`); `set_line_price_in(conn, invoice_line_id, price) -> None`
  (respread, R-P3-12).
- Produces (`ItemStore`): `create_in(conn, body: ItemInput, by) -> Item`.
- Produces (`ledger.record`): an event with `ref_kind` in `('print_run','bb_usage')` stores
  `cost_per_kg_at` = the spool's `cost_per_kg`, else its type's (spec §6.6, D7). Layer 3
  makes the spool's `cost_per_kg` the per-kg form of its unit's cost (R-P3-6).

- [ ] **Step 1: Write the failing tests**:
  - `test_a_receive_names_its_invoice_line`: `receive_in(…, origin="invoice_line",
    invoice_line_id=L)` → lot `origin == "invoice_line"`, `invoice_line_id == L`;
    `of_line(conn, L)` returns it.
  - `test_units_received_in_service_write_a_use_move`: 2 units at 3000, `in_service=True` →
    both `in_service`, two `use` moves of −1 @ 3000.
  - `test_a_received_unit_calls_its_kinds_extension`: a fake extension registered for
    `unit` records one `received` call with the two units; `set_unit_cost_in` calls
    `cost_set`; a lot item calls nothing.
  - `test_fill_units_in_takes_the_line`: a unit received by hand at 0 cost-unknown takes
    line `L` and 3000; a unit on another line is refused and nothing is written.
  - `test_set_unit_cost_in_moves_no_past_move`; `test_recost_unit_names_the_order_items`.
  - `test_match_in_finds_the_offer_of_one_item_in_a_bundle`: one SKU, two items → `match_in`
    with item A returns A's offer; `by_sku_in` returns both (D13).
  - `test_a_print_event_snapshots_the_spool_cost`: spool `cost_per_kg` 25.0000 →
    `print_started` event `cost_per_kg_at == 25.0000`; a spool with none, type 20.0000 →
    20.0000; a `weighed` event → NULL.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_stock.py tests/inventory/test_offers.py tests/filament/test_ledger.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Every lot insert still takes `_open_lock` first (R-P2-32).
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/inventory tests/filament -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): receive from an invoice line; unit extensions (#7068, 1/7)`

### Task 4: Invoice line links and their consumers

**Files:**
- Create: `backend/scadbuddy/inventory/invoice_links.py`, `backend/scadbuddy/orders/invoice_costs.py`
- Modify: `backend/scadbuddy/inventory/models.py`, `backend/scadbuddy/inventory/component.py`
  (`.links`), `backend/scadbuddy/entities/sweep.py` and `entities/registry.py`
  (`repoint_entity`: the sweep skips, and a merge moves, an entity that a link names;
  R-P3-27, as R-P2-4)
- Test: `backend/tests/inventory/test_invoice_links.py`, `backend/tests/entities/test_sweep.py`

**Interfaces:**
- Produces (`inventory/models.py`): `LinkInput(entity: str, share: Decimal = 1 (gt 0,
  max_digits 9, decimal_places 4), note: str ≤ 1000)`, `LinkPatch(share?, note?)`,
  `LineLink(id, line_id, invoice_id, entity, entity_ui_path, share, note, consumer:
  Literal["maintenance_event", "cost_entry"] | None, consumer_id: str | None, amount:
  Decimal | None, created_at, created_by)`, `LinkedLine(link: LineLink, invoice_id, number,
  supplier_name, position, description, kind, allocated_cost, currency)`.
- Produces (`orders/invoice_costs.py`): `class OrderLineCosts(store: OrderStore)` with
  `lock_orders(conn, order_ids: Sequence[str]) -> dict[str, Order]` (sorted by id),
  `write_entry(conn, order: Order, order_item_id: str | None, line_id: UUID, amount_minor:
  int, by: Actor) -> str` (kind `material`, source `invoice_line:<line_id>`, spec §6.7),
  `drop_entry(conn, order: Order, entry_id: str, by: Actor, *, after_minor: int | None)`
  (deletes it and inserts the order event `cost.invoice_line_changed` with `line_id`,
  `before_minor`, `after_minor`).
- Produces (`inventory/invoice_links.py`): `class InvoiceLinkStore(pool, entities, orders:
  OrderLineCosts)`: `for_line(line_id) -> list[LineLink]`, `for_entity(ref) ->
  list[LinkedLine]`, `add(line_id, body, by) -> LineLink`, `update(link_id, patch, by) ->
  LineLink`, `delete(link_id, by) -> None`, `rewrite_in(conn, line_id, by) -> None`.
  `rewrite_in` deletes every consumer row of the line's links, then, when the line is a
  confirmed `expense` line (R-P3-9), shares `allocated_cost` by `share` with
  `invoice_money.spread` and writes one consumer per link:
  - `printer:<id>`, or `unit:<id>` with `printer_id` set: a `printer_maintenance_events` row
    (`id` uuid4, `task` = the line's description cut to 100, `performed_at` = the invoice
    date, `consumables` = the share, `origin = 'scadbuddy'`, `source =
    'invoice_line:<line_id>'`, `created_by` the actor's label) (spec §6.7, D9);
  - `order:<id>` or `order_item:<id>`: `OrderLineCosts.write_entry` with the share in the
    order's minor units; none for a share that rounds to 0;
  - any other kind: no consumer.
  Each add, update and delete writes one `invoice_events` row `links_changed` with each
  link's amount before and after. Errors: `LineNotFound`, `LinkNotFound`, `LinkExists` (409
  `link_exists`), `LineLinkedTwice` (409 `line_linked_twice`: a second link of one line to
  one order, or to one printer, R-P3-10), `CurrencyMismatch` (409 `currency_mismatch`: an
  order whose currency is not the invoice's), and the entities errors.

- [ ] **Step 1: Write the failing tests** (`test_invoice_links.py`; seed a confirmed USD
  invoice with one `expense` line, `allocated_cost` 90.00, order `o1` in USD, printer `p1`):
  - `test_a_share_goes_to_its_consumer`: links `order:o1` (share 2) and `printer:p1` (share
    1) → one cost entry 6000 with source `invoice_line:<L>`; one maintenance event,
    `consumables == Decimal("30.0000")`, `source == "invoice_line:<L>"`.
  - `test_a_change_reverses_exactly` (Review Focus 2): with `o1` closed, set the printer's
    share to 2 → the old entry and event are gone, a new entry 4500 and consumables
    45.0000; `order_events` has `cost.invoice_line_changed` with `before_minor == 6000`,
    `after_minor == 4500`; a repeat of the same PATCH writes no new row.
  - `test_deleting_a_link_respreads_the_rest`: delete the order link → no entry; consumables
    90.0000.
  - `test_a_stock_line_link_is_a_reference`: a link on a `stock` line writes no consumer.
  - `test_a_draft_link_waits_for_confirm`: a link on a draft line writes nothing.
  - `test_refusals`: a second link to `order_item:` of `o1` → `LineLinkedTwice`; `order:o2`
    in EUR → `CurrencyMismatch`; the same entity twice → `LinkExists`.
  - `test_sweep.py`: an entity a link names survives the sweep.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_invoice_links.py tests/entities/test_sweep.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Lock order R-P3-11: `lock_orders` before the invoice row.
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): invoice line links with exact reversal (#7068, 1/7)`

### Task 5: One confirm path for item lines; amend

**Files:**
- Create: `backend/scadbuddy/inventory/invoice_lines.py`
- Modify: `backend/scadbuddy/filament/invoices.py` (`InvoiceStore`: `confirm` sends every
  line with an item to `receive_line`; `amend`; `candidates`; `events`; `set_lines_review`;
  `set_proposal`), `backend/scadbuddy/filament/purchasing_models.py`
- Test: `backend/tests/filament/test_invoice_confirm.py`, `backend/tests/inventory/test_invoice_lines.py`,
  `backend/tests/filament/test_invoice_amend.py`, `backend/tests/filament/test_invoice_candidates.py`

In layers 1 and 2 a filament line (`type_id` or `proposed_type`) still takes today's
`_confirm_line`, because no filament item exists yet. Task 9 deletes that branch: from
layer 3 on, `receive_line` is the one path (R-P3-30).

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces (`purchasing_models.py`): `LineKind = Literal["stock", "expense", "asset"]`;
  `InvoiceLineIn` gains `item_id: UUID | None`, `offer_id: UUID | None`, `kind: LineKind =
  "stock"`, `pack_size: Decimal | None` (None takes the offer's, else 1), `proposed_item:
  ItemInput | None`. `proposed_type` and `proposed_item` are exclusive (422).
  `InvoiceLine` gains the same fields plus `lot_ids`, `unit_ids`, `links: list[LineLink]`.
  `ItemCandidate(item_id, name, kind_id, matched_by: Literal["sku", "mpn", "name",
  "colour"], offer_id)`; `LineCandidates.items: list[ItemCandidate]`. `InvoiceAmend(subtotal,
  tax, shipping, total, lines: list[InvoiceLineIn] (1..200), respread: bool = False)`;
  `AllocationChange(position, before: Decimal | None, after: Decimal)`;
  `RecostTarget(kind: Literal["lot", "unit"], id: UUID, ui_path: str)`;
  `AmendResult(invoice: InvoiceDetail, changes: list[AllocationChange], recost:
  list[RecostTarget])`; `InvoiceEvent(id, at, kind, detail, actor)`;
  `LinesReview = Literal["flagged", "done"]`; `InvoiceDetail` gains `lines_review`,
  `amend_proposal: AmendProposal | None` (`AmendProposal(body: InvoiceAmend, session_id: str
  | None, created_at)`).
- Produces (`inventory/invoice_lines.py`): `receive_line(conn, head: Row, line: Row, by:
  Actor, *, shop: str, stores: InventoryComponent) -> None`. One rule for every item (R-P3-7,
  R-P3-8, R-P3-30):
  - the item is `line.item_id`, else one made from `proposed_item` by `ItemStore.create_in`
    (a name clash is `InvoiceLineError` "an item like '<name>' exists; choose it", R-P3-22);
  - the offer is `line.offer_id` (this supplier's, this item's, unarchived), else
    `match_in(supplier, item, sku or description)`, else a new one (`sku` = the line's SKU,
    else its description, at most 128). With no item named and a SKU that covers two or more
    items (`by_sku_in`), the line fails and names them (R-P3-23);
  - units = `quantity × line.pack_size`. `stock` or `asset` on a `lot` item: one lot of
    `units` at `allocated_cost × 10^exp / units`; on a `unit` or `filament` item: the
    line's `fill_unit_ids` are filled (`fill_units_in`) and `units − filled` more are
    received (`receive_units_in`, at most 100, else a line error), each at that cost,
    `in_service` for `asset`; `asset` on a lot item and `stock` or `asset` on a `none` item
    are line errors; `expense` receives nothing;
  - with an item, one invoice price point `pack_price(line_total, quantity, line.pack_size,
    offer.pack_size)` in the invoice's currency on its date (R-P2-36).
  The line's `item_id` and `offer_id` are written back. Then `rewrite_in` runs for the line.
- Produces (`InvoiceStore`): `amend(invoice_id, body: InvoiceAmend, *, actor: str, preview:
  bool = False) -> AmendResult | None`. In one transaction under the lock order: the new
  lines go after the kept ones; without `respread`, `amend_spread` sets them and
  `AmendShortError` becomes `InvoiceAmendShortError` (409 `amend_short`); with it,
  `_recompute` re-allocates every line, and each kept line's lots (`set_lot_cost_in`), units
  (`set_unit_cost_in`, spools included from layer 3) and price point (`set_line_price_in`)
  take the new cost, the cost per unit being the new `allocated_cost` over the units the
  line received (`of_line`), and become `recost` targets. The reconcile check runs; the new
  lines are confirmed; every line's links are rewritten; one `invoice_events` row (`amended`
  or `respread`) holds each line's allocation before and after (spec §6.7); the stored
  `amend_proposal` is cleared and `lines_review` becomes `done`. `preview` raises a private
  sentinel at the end so the transaction rolls back, and returns what it would have
  written (R-P3-12). `events(invoice_id) -> list[InvoiceEvent]`. `confirm` writes a
  `confirmed` event. `set_lines_review(invoice_id, value: LinesReview | None, *, actor)` and
  `set_proposal(invoice_id, body: InvoiceAmend | None, *, session_id, actor)` work on a
  confirmed invoice only and write an `invoice_events` row (R-P3-32, R-P3-34).
- Produces (`InvoiceStore.candidates`): one path for every line (R-P3-24): items whose offer
  at this supplier has the line's SKU (`sku`), whose `mpn` property equals the SKU (`mpn`),
  whose name equals or is inside the description (`name`); and, per item candidate of a
  unit or filament kind, its units with no invoice line (today's spool candidates, archived
  ones marked). A kind's extension may add a matcher: the filament one adds `colour` (the
  line's colour code or proposed type's material and RGBA, today's `_line_key`).

- [ ] **Step 1: Write the failing tests**:
  - `test_a_lot_line_receives_one_lot` (`test_invoice_lines.py`): a `hardware` item, an offer
    with `pack_size` 100, line quantity 2 (`pack_size` defaults to 100), `allocated_cost`
    30.00 (USD, exponent 2) → one lot, `received_qty == 200`, `unit_cost_minor ==
    Decimal("15")`, `origin == "invoice_line"`; one price point `15.0000`.
  - `test_a_unit_line_receives_units`: a `nozzle` item, no offer, quantity 3, allocated
    90.00 → 3 units at `cost_minor == 3000`; a new offer with the line's SKU; 101 units → a
    line error.
  - `test_a_unit_line_fills_units_first`: a hand-received nozzle unit in `fill_unit_ids`,
    quantity 2 → that unit takes the line and one new unit is received.
  - `test_an_asset_line_puts_units_in_service`.
  - `test_an_expense_line_receives_nothing_and_feeds_its_links`: a `service` item, link to
    `order:o1` → no lot, a cost entry of the whole `allocated_cost`.
  - `test_a_service_on_a_stock_line_is_refused`: `InvoiceLineError` naming the line; nothing
    written.
  - `test_a_bundle_sku_with_no_item_is_refused` (Review Focus 3): the message names both items.
  - `test_a_proposed_item_is_made_at_confirm`; a clash → line error.
  - `test_filament_lines_confirm_as_before`: the existing `test_invoice_confirm.py` cases pass unchanged.
  - `test_invoice_amend.py`: `test_an_amend_keeps_the_old_allocation` (the Task 2 receipt: the
    filament line keeps 171.12 and its spools' `cost_per_kg`; the new nozzle line gets
    5063.16 and its units); `test_an_amend_short_writes_nothing` (Review Focus 1);
    `test_a_respread_names_what_to_recost`: a lot and a unit whose cost moved are `recost`
    targets and their costs changed; `test_a_preview_writes_nothing`; one `invoice_events`
    row per amend with before and after; the amend clears `amend_proposal` and sets
    `lines_review == "done"`.
  - `test_candidates_propose_items` (`test_invoice_candidates.py`): the SKU matches an offer
    (`matched_by == "sku"`); an item whose property `mpn` equals the SKU (`"mpn"`); an item
    named as the description (`"name"`), in that order; a nozzle candidate lists its unit
    with no invoice line.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_invoice_lines.py tests/filament/test_invoice_confirm.py tests/filament/test_invoice_amend.py tests/filament/test_invoice_candidates.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/filament tests/inventory tests/orders -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): invoice lines of any kind receive stock; amend (#7068, 1/7)`

## Layer 2 — `feat/7068-2-purchasing-routes` (on layer 1)

### Task 6: Routes; order purchases link lines; the backfill list

**Files:**
- Create: `backend/scadbuddy/api/purchasing.py`
- Modify: `backend/scadbuddy/api/filament_purchasing.py` (the `/filament/suppliers` CRUD and
  `/filament/invoices…` routes call the same handlers; each description says "Alias of
  /inventory/…; goes after one release", spec §6.7; the product, price and price-sheet routes
  stay, R-P3-20), `backend/scadbuddy/inventory/problems.py`, `backend/scadbuddy/main.py`,
  `backend/scadbuddy/orders/models.py` (`PurchaseRecordInput.invoice_line_id: UUID | None`,
  `PurchaseSource.invoice_line_id`), `backend/scadbuddy/orders/lifecycle.py`
  (`record_purchase`, `update_purchase`, `decline_purchase`),
  `backend/scadbuddy/filament/shopping.py` and `purchasing_models.py` (`item_id`),
  `backend/scadbuddy/filament/invoices.py` (`review_list`), `agent/src/tools/coverage.ts`
  (Pre-flight 5)
- Test: `backend/tests/api/test_purchasing.py`, `backend/tests/api/test_filament_purchasing.py`
  (aliases), `backend/tests/orders/test_purchases.py`, `backend/tests/filament/test_shopping.py`,
  `backend/tests/filament/test_invoices.py` (`review_list`)

**Interfaces:**
- Produces (under `/api/v1`, tag `inventory`, store calls in `asyncio.to_thread`, a
  `summary` and `description` on each route):
  - `GET|POST /inventory/suppliers`, `GET|PATCH /inventory/suppliers/{supplier_id}`.
  - `GET|POST /inventory/invoices`, `GET|PATCH|DELETE /inventory/invoices/{invoice_id}`
    (PATCH of a confirmed invoice takes `note` and `lines_review`),
    `PUT /inventory/invoices/{invoice_id}/lines`, `POST /inventory/invoices/{invoice_id}/confirm`,
    `POST /inventory/invoices/{invoice_id}/amend?preview=` → `AmendResult`,
    `PUT|DELETE /inventory/invoices/{invoice_id}/amend-proposal` (stores or clears an
    `InvoiceAmend`; writes no line), `GET /inventory/invoices/{invoice_id}/candidates`,
    `GET /inventory/invoices/{invoice_id}/events`, `POST|GET /inventory/invoices/{invoice_id}/file`.
  - `GET /inventory/invoices/review?all=` → `{invoices: InvoiceReview[]}` (R-P3-31).
  - `GET|POST /inventory/invoice-lines/{line_id}/links`, `PATCH|DELETE
    /inventory/invoice-line-links/{link_id}`, `GET /entities/{ref}/invoice-lines` →
    `{lines: LinkedLine[]}`.
  - Problems: 404 `invoice_line_not_found`, `link_not_found`; 409 `amend_short`,
    `link_exists`, `line_linked_twice`, `currency_mismatch`, `invoice_not_confirmed`,
    `line_not_expense`; the invoice problems keep their codes.
- Produces (`InvoiceStore.review_list(*, all: bool = False) -> list[InvoiceReview]`,
  R-P3-31): `InvoiceReview(invoice: Invoice, signals: list[ReviewSignal], ui_path)`, with
  `ReviewSignal = Literal["subtotal_differs", "tax_differs", "flagged", "has_file",
  "imported", "has_note"]`. Each is a fact the data holds: `subtotal_differs` = `|subtotal −
  Σ line_subtotal| > 0.05` (`RECONCILE_TOLERANCE`; confirm never compared them);
  `tax_differs` = `|tax − Σ line_tax| > 0.05`; `flagged` = `lines_review = 'flagged'`;
  `has_file` = an invoice file is stored; `imported` = `import_run_id` is set; `has_note` =
  a note is set (spec §6.7: the Micro Center invoice kept its full totals there). The list
  holds confirmed invoices with `lines_review` not `done` and at least one of the first
  three signals; with `all`, every confirmed invoice not `done`. Newest first.
- Produces (`OrderLifecycle.record_purchase`): with `invoice_line_id`, under the order's
  lock, then the invoice's (R-P3-11): the line must be on a confirmed invoice (409
  `invoice_not_confirmed`), an `expense` line (409 `line_not_expense`), in the order's
  currency (409 `currency_mismatch`). It adds the link `order_item:<item>` if the line has
  none for this order, runs `rewrite_in`, and sets `cost_entry_id` to that link's entry; it
  writes no second cost entry (spec §6.7). The `purchase` charge is `body.amount_minor`, as
  today. `update_purchase` with a line changes the charge only. `decline_purchase` deletes
  the link, which respreads the line. A line serves at most one purchase per order
  (`order_item_sources.cost_entry_id` is unique, R-P3-10).
- Produces (shopping): `ShoppingItemCreate.item_id: UUID | None`; a row of a filament item
  stores it as `type_id` too (Task 7 makes the ids equal).

- [ ] **Step 1: Write the failing tests** with the app `client` fixture: one round trip per
  family; each problem code once; `POST …/amend?preview=true` answers the allocation and a
  second `GET` shows no new line; `PUT …/amend-proposal` then `GET` shows it and no line
  changed; `GET /entities/order:o1/invoice-lines` lists the line; every
  `/filament/invoices…` and `/filament/suppliers…` call answers as its `/inventory/…` twin;
  `POST /orders/{id}/purchases/{source}/record` with an `invoice_line_id` sets
  `cost_entry_id` to the `invoice_line:<id>` entry and the order has one entry for it; a
  draft line → 409 `invoice_not_confirmed`; a NUL in a supplier name → 422 (R-P2-16).
  `test_review_list_uses_the_signals` (`test_invoices.py`): a confirmed invoice with header
  subtotal 4900.00 over lines of 160.00 → `subtotal_differs`, `has_note`; one whose
  subtotal and tax match its lines → absent, and present with `all=true`; a person's flag →
  `flagged`; `lines_review = 'done'` → absent even with `all=true`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_purchasing.py tests/api/test_filament_purchasing.py tests/orders/test_purchases.py tests/filament/test_shopping.py tests/filament/test_invoices.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** the routes and the coverage entries (reason: "Inventory phase 3
  (#7068, spec §6.5, §6.7); its `invoice_*` and `supplier_*` tools follow in layer 6 of the
  same stack, which drops these entries").
- [ ] **Step 4: Run** the tests, `uv run --frozen pytest tests/auth -q`, and `cd agent && pnpm exec vitest run test/coverage.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): supplier, invoice, amend, review and link routes (#7068, 2/7)`

## Layer 3 — `feat/7068-3-purchasing-to-generic` (on layer 2; ships like any PR, R-P3-29)

### Task 7: Migrate filament purchasing onto the generic tables

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_purchasing_to_generic.sql`
- Test: `backend/tests/test_pg_migrations.py` (`test_the_purchasing_migration_*`, with a
  `_before_purchasing(conn)` helper: `migrate(conn, MIGRATIONS[: _index("purchasing_to_generic")])`,
  as `_before_parts` does, l.1128)

The migration, in one file, in this order (actor `{"kind": "provider", "principal":
"purchasing_migration"}`; shop exponent as a SQL `CASE` that cites `orders/currency.py`
`minor_exponent`, as the parts migration did):
0. `LOCK TABLE filament_supplier_products, filament_price_points, filament_spool_types,
   filament_spools, invoices, invoice_lines IN EXCLUSIVE MODE` (R-P2-20). No deploy window
   (R-P3-29): every table an old pod reads keeps a view under its old name, so an old pod's
   product or price read still answers during the rollout.
1. If an `items` row has a spool type's id, RAISE (Pre-flight 6, as R-P2-24).
2. `items` from `filament_spool_types`: `id` = the type's id, `kind_id = 'filament'`,
   `name = code` (cut to 200; a clash among the unarchived gets ` (2)`, ` (3)`),
   `manufacturer` = its brand's name (else `''`), `archived_at`, `created_at`,
   `updated_at`, `source = 'manual'`. `filament_spool_types.item_id = id`, then `SET NOT
   NULL` (R-P3-3).
3. Schemas: `item_kinds.attribute_schema` of `filament` = the `spool_type` row of
   `entity_property_schemas`; the `spool` row's `properties` merge into the `unit` row (made
   when absent); both old rows are deleted (spec §6.3a, R-P3-5).
4. Entities: `entities` of kind `spool_type` become `item`, of kind `spool` become `unit`
   (same `ref`, the ids are equal); their `entity_attachments.subject` likewise (R-P3-5).
5. `stock_units` from every spool with a type: `id` = the spool's id, `item_id` = its type,
   `status` = `retired` when archived else `in_stock`, `cost_minor` = `round(cost_per_kg ×
   label weight / 1000 × 10^exp, 6)` with the spool's own label weight, else its type's;
   `cost_unknown` and `cost_minor = 0` when the spool has no own `cost_per_kg`; `name = '#'
   || number`, `origin` = `invoice_line` with `invoice_line_id` when it has one, else
   `opening`, `received_at = created_at`. One `receive` move each (+1 at `cost_minor`), and
   a `retire` move (−1) for an archived one, as `receive_units` and `set_unit_status` write
   them. `filament_spools.unit_id = id` for those (R-P3-6).
6. `supplier_offers` from `filament_supplier_products`: `id` kept, `item_id = type_id`, `url`
   = `product_url` when it matches `^https?://` else NULL, `sku` cut to 128, `pack_size`,
   `pack_uom = 'spool'`, `note`, `archived_at`, `created_at`, `updated_at` (R-P3-2).
7. `offer_price_points` from `filament_price_points`: `id` kept, `offer_id = product_id`,
   `price`, `currency`, `on_date`, `source`, `invoice_line_id`, `created_at`.
8. `invoice_lines`: `offer_id = supplier_product_id`, `item_id = type_id` where set,
   `pack_size = 1` (a filament line counted spools, so each spool is a pack of 1, R-P3-7);
   rename `update_spool_ids` → `fill_unit_ids`; drop `supplier_product_id`. The view
   `filament_invoice_lines` is made again with `offer_id AS supplier_product_id` and
   `fill_unit_ids AS update_spool_ids`.
9. Invoice files: for each invoice with a `file_key`, its `invoice:<id>` entity (made here),
   one `entity_attachments` row (`id = gen_random_uuid()::text`, `subject`, `kind` = `image`
   for an `image/*` type else `document`, `role = 'invoice'`, `filename = file_name`,
   `media_type`, `size_bytes = file_size`, `sha256` = the key after `attachment-`,
   `blob_key = file_key`, `caption = ''`), a `blob_refs` hold `('attachment', id)`, and the
   `filament_invoice` hold deleted. A key of another form keeps its old hold and gets no
   attachment row; the column stays for it. The `file_*` columns stay for the old pods; a
   follow-up drops them (R-P3-14).
10. `filament_spool_events.cost_per_kg_at` = the spool's effective cost today
    (`coalesce(s.cost_per_kg, t.cost_per_kg)`) for every event with `ref_kind IN
    ('print_run','bb_usage')` (spec §6.6 "Phase 3 backfills"); NULL stays NULL.
11. `filament_shopping_items.item_id = type_id` where set.
12. Rename `filament_supplier_products` → `filament_supplier_products_retired`,
    `filament_price_points` → `filament_price_points_retired`, and make read views under the
    old names over `supplier_offers` and `offer_price_points` of filament items, with the
    old column names (R-P3-29; a follow-up drops the retired tables after a prod check, as
    phase 2's Ruling 6, and the views).

- [ ] **Step 1: Write the failing tests** (seed with `_before_purchasing`):
  - `test_the_purchasing_migration_moves_products_and_prices`: supplier "Acme", type
    `PLA-BLK`, product `P` (sku `S1`, `pack_size` 4, url `https://x.test/p`), an invoice line
    `L` naming it with quantity 8, a price point `79.9600` USD of source `invoice` → an offer
    with id `P`, `item_id` = the type's id, `pack_size == 4`, `pack_uom == "spool"`; a price
    point with the old id and `invoice_line_id == L`; `invoice_lines.offer_id == P`,
    `quantity == 8`, `pack_size == 1`; `SELECT … FROM filament_supplier_products` answers
    `P` with `type_id`.
  - `test_a_bad_product_url_becomes_no_url`: `product_url = 'www.x'` → offer `url IS NULL`.
  - `test_spool_types_become_filament_items`: a live and an archived type → two items with
    their ids, `kind_id == "filament"`, `name` = the code, the archived one archived; two
    live types `pla` and `PLA` (an archived pair cannot clash) → names `pla` and `PLA (2)`.
  - `test_spools_become_units_with_their_cost`: a typed spool at 25.0000/kg, label 1000 g,
    USD, on line `L` → a unit with its id, `cost_minor == Decimal("2500")`, `origin ==
    "invoice_line"`, `invoice_line_id == L`, `status == "in_stock"`, one `receive` move of
    +1 @ 2500; a spool with no cost → `cost_unknown`; an archived spool → `retired` and a
    `retire` move; a typeless spool → no unit and `unit_id IS NULL`.
  - `test_spool_entities_become_item_and_unit_entities`: a link on `spool_type:<t>` and a
    file on `spool:<s>` list under `item:<t>` and `unit:<s>`; the `spool` schema's
    `lot_number` is in the `unit` row.
  - `test_an_invoice_file_becomes_an_invoice_attachment`: key `attachment-` + 64 hex → one
    row with `role == "invoice"`, `subject == "invoice:<id>"`, `sha256` = the hex; a hold
    `('attachment', <id>)`; no `filament_invoice` hold.
  - `test_use_events_get_their_cost`: spool 25.0000 → its `print_started` event 25.0000; a
    spool with no cost and a type at 20.0000 → 20.0000; a `weighed` event stays NULL.
  - `test_a_type_id_already_an_item_raises`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_pg_migrations.py -k purchasing_migration -q` — Expected: FAIL.
- [ ] **Step 3: Write the migration.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): filament purchasing moves onto offers, items and units (#7068, 3/7)`

### Task 8: One model for spool types and spools; the spool workflow as an extension

**Files:**
- Create: `backend/scadbuddy/filament/spool_units.py` (`SpoolUnits`, the `UnitExtension` of
  the `filament` tracking)
- Modify: `backend/scadbuddy/filament/component.py` (register it on the stock store),
  `backend/scadbuddy/filament/types.py` (`create_on`, `update`, `archive` write the item in
  the same transaction; `ItemStore` writes of a filament item's name or archive write the
  type's `code` or archive), `backend/scadbuddy/filament/spools.py` (`create_on` and
  `bulk_on` receive units through `StockStore.receive_units_in`; `_update_on` retypes and
  sets cost through `StockStore`; `_archive` sets the unit status; `delete`),
  `backend/scadbuddy/filament/bambuddy_pull.py` (l.1290: the same receive),
  `backend/scadbuddy/inventory/items.py`, `stock.py`, `offers.py` (`FilamentItemRefused`
  goes), `backend/scadbuddy/entities/refs.py` and `registry.py` (`spool_type` and `spool`
  parse as `item` and `unit`; their `KINDS` rows go), `backend/scadbuddy/entities/properties.py`,
  every `spool_type:` and `spool:` ref built in backend code (`grep -rn "spool_type\b\|\"spool\""
  backend/scadbuddy`)
- Test: `backend/tests/filament/test_spool_units.py`, `test_types.py`, `test_spools.py`,
  `backend/tests/inventory/test_items.py`, `test_offers.py`, `test_stock.py`,
  `backend/tests/entities/test_registry.py`, `test_properties.py`

**Interfaces:**
- Produces (`SpoolUnits`, R-P3-6, R-P3-30): `received` inserts one `filament_spools` row per
  unit with the unit's id (`grams_left` = the label weight, the type from the item, the
  fields in `ReceiveUnits.extra`), and its `created` ledger event; `retyped` sets the spool's
  `type_id`; `status_set` sets or clears `archived_at` (`retired` ↔ `in_stock`; a spool
  workflow archive calls `StockStore.set_unit_status_in`, which calls back here, so either
  side writes both); `cost_set` writes `filament_spools.cost_per_kg = cost_minor /
  10^exp / (label weight / 1000)`, or NULL for an unknown cost; `deleting` deletes the
  spool row's workflow rows as `spools.delete` does today.
- Produces: `FilamentSpoolStore.create_on` becomes `StockStore.receive_units_in(item = the
  type, unit_ids=[new id], extra=…)` and returns the spool; a spool `cost_per_kg` write from
  a person, the agent or the pull calls `set_unit_cost_in` with `cost_per_kg × label weight
  / 1000 × 10^exp`. A label weight change keeps `cost_minor` (what was paid) and writes the
  new `cost_per_kg`.
- Produces (`Item` for a `filament` item): `on_hand` = the grams left of its in-stock units'
  spools, `stock_value_minor` = Σ `cost_minor × grams_left / label weight`, `cost_unknown`
  when a unit's cost is unknown; `type_ui_path`. `Unit.spool_ui_path`. Creating a filament
  item through `POST /inventory/items` takes the type's fields in `spool_type:
  SpoolTypeCreate` (422 without it) and makes both rows.
- Spool deletes: the spool workflow's delete refusals stay (`_USED`); a deleted spool's unit
  goes with its `receive` and `retire` moves; a unit with a move that names an order is
  refused as a used spool is.
- The extension never forks the model: `StockStore`, `ItemStore`, `OfferStore` and the
  entity store take filament items and spool units with no refusal (R-P3-4, R-P3-5).

- [ ] **Step 1: Write the failing tests** (Review Focus 4): `POST
  /inventory/items/{type item}/units` with `count` 2 makes two spools with the units' ids and
  label weight; creating a type makes an item with its id; renaming the item renames the
  type's code and the reverse; a spool made by `create_on` and one by the pull each get a
  unit; archive → unit `retired`, restore → `in_stock`; a retype moves the unit to the new
  item; a typeless spool gets its unit when typed; delete removes the unit; a person's
  `cost_per_kg` of 30.0000 on a 1000 g spool → `cost_minor == 3000`, and
  `set_unit_cost_in(…, 2000)` → `cost_per_kg == 20.0000`; a label weight change from 1000 to
  500 keeps `cost_minor` and writes 40.0000; `ItemStore.list(kind="filament")` gives a type
  with spools of 600 g and 400 g left of 1000 g at 2500 each → `on_hand == 1000`,
  `stock_value_minor == 2500`; an offer for it is created; `entity_link` on `item:<it>` and
  on `spool_type:<it>` land on one entity; a property on `spool:<s>` lists under `unit:<s>`;
  `set_schema("spool_type", …)` raises `SchemaOwnedByKind` (as R-P2-5); a lot call on a
  filament item → `WrongTracking`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament tests/inventory tests/entities -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same with `-n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): spool types are items and spools are units (#7068, 3/7)`

### Task 9: One confirm and candidates path; offers; Bambuddy sees only filament; the invoice file is an attachment

**Files:**
- Modify: `backend/scadbuddy/filament/invoices.py` (delete the filament branch of
  `_confirm_line`, `_line_type`, `_line_product` and `_claimed`: a filament line is a line
  whose item is a filament item, received by `receive_line`; a `proposed_type` becomes its
  item through the type store; `file_key`/`set_file`/`delete` over the `invoice`
  attachment), `backend/scadbuddy/filament/purchasing_models.py` (`type_id` reads and
  writes `item_id`, `supplier_product_id` writes `offer_id`, `update_spool_ids` writes
  `fill_unit_ids`, each as a `validation_alias`), `backend/scadbuddy/filament/suppliers.py`
  (products, prices, sheet, stats and `type_prices` over `supplier_offers` of filament items;
  `SupplierProduct.type_id` = `item_id`; `_newest` over `offer_price_points`),
  `backend/scadbuddy/filament/bb_sync_plan.py` (l.350, l.727 with `created_at`,
  `_suppliers` l.1665), `backend/scadbuddy/filament/spools.py:512-523`,
  `backend/scadbuddy/filament/metrics_store.py:344-356`, `backend/scadbuddy/api/purchasing.py`
  (the file routes)
- Test: `backend/tests/filament/test_suppliers.py`, `test_invoice_confirm.py`,
  `test_invoice_candidates.py`, `test_bb_sync_plan.py`, `backend/tests/api/test_purchasing.py`,
  `test_filament_purchasing.py`

**Interfaces:**
- Produces: one path (R-P3-30). A spool line of 4 spools at 100.00 allocated, label 1000 g,
  is 4 units at 2500 each, so its spools read `cost_per_kg == 25.0000`, as today's
  `spool_cost_per_kg`. A filled spool with its own cost keeps today's `edited` ledger event
  with the person as actor (#6700, R-6700-2), written by the extension's `cost_set`.
- Produces (`_suppliers()`, R-P3-15, updates R-P2-8 to R-P2-10): a supplier is planned when
  it is linked, has an offer (archived too) whose item's kind has `tracking = 'filament'`,
  has an invoice with a line of a filament item, or has an open create intent. Products and
  prices sent are those of filament items only (D16).
- Produces (invoice file): `InvoiceStore.set_file_in(conn, invoice_id, *, key, name,
  media_type, size, sha256, by) -> FileRef` deletes the invoice's `invoice`-role attachment
  and its hold, then `EntityAttachmentStore.add(role="invoice")`, in one transaction;
  `Invoice.file` reads that attachment; a draft's delete removes it.

- [ ] **Step 1: Write the failing tests**: every `test_invoice_confirm.py` case passes on the
  one path (spools made, filled, archived filled, `cost_per_kg` as before, one price point);
  a mixed invoice (a filament line, an NFC tag `consumable` lot line, a `tool` unit line)
  confirms in one call; the filament supplier, product, price and sheet tests pass with
  offers; the orders price book import (`orders/spool_import.py`) and the type page's "Use
  as default cost" (`type_prices`) read a migrated price point (spec §6.5); candidates of a
  filament line list colour-matched types and their spools as before;
  `test_a_supplier_with_only_bearing_offers_stays_out_of_bambuddy` (D16): a supplier with one
  `hardware` offer and one invoice of only `expense` lines is not planned; one with an
  archived filament offer is; a price point of a bearing offer is never in a plan; `POST
  /inventory/invoices/{id}/file` twice leaves one `invoice` attachment and one hold; `GET
  …/file` serves the bytes; `GET /entities/invoice:<id>/attachments` lists it.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament/test_suppliers.py tests/filament/test_invoice_confirm.py tests/filament/test_invoice_candidates.py tests/filament/test_bb_sync_plan.py tests/api/test_purchasing.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the whole backend suite `cd backend && uv run --frozen pytest -n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): one confirm path for every item; only filament reaches Bambuddy (#7068, 3/7)`

## Layer 4 — `feat/7068-4-spool-job-cost` (on layer 3; Pre-flight 7)

### Task 10: Job cost reads spool cost; units re-cost past uses

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_inventory_moves_view.sql`
- Modify: `backend/scadbuddy/filament/ledger.py` (`run_uses`),
  `backend/scadbuddy/filament/spool_units.py` (`recost`), `backend/scadbuddy/orders/store.py`
  (`cost_rows` gains `spool_uses`, one query for every order of the read),
  `backend/scadbuddy/orders/costs.py` (`_Item.attempt`; the docstring's **Prints** paragraph
  cites spec §6.6, D7 and #7068), `backend/scadbuddy/orders/lifecycle.py` (`recost_unit`),
  `backend/scadbuddy/api/inventory_stock.py` (`POST /inventory/units/{unit_id}/recost`),
  `backend/scadbuddy/api/filament.py` (`POST /filament/spools/{spool_id}/recost`, an alias)
- Test: `backend/tests/filament/test_ledger.py`, `backend/tests/orders/test_costs.py`,
  `backend/tests/api/test_inventory_stock.py`, `backend/tests/test_pg_migrations.py`

The view (spec §6.6 "view `inventory_moves`", deferred by phase 2): `CREATE VIEW
inventory_moves` with columns `source` (`stock` or `spool`), `id`, `at`, `item_id`,
`lot_id`, `unit_id`, `kind`, `qty`, `unit_cost_minor`, `amount_minor`, `cost_unknown`,
`order_item_id`, `print_run_id`. Its `stock` rows are `stock_moves`. Its `spool` rows are the
use events that the ledger counts (`filament/ledger.py` rules 1 and 3): a `bb_usage`
`print_ended`; a `print_run` `print_ended` that no `bb_usage` event of the same spool and job
pairs; a `print_run` `print_started` with neither. A `spool` row has `item_id` = the spool's
type, `unit_id` = the spool's unit, `kind = 'use'`, `qty = delta_g`, `unit_cost_minor =
cost_per_kg_at / 1000` in the shop's minor units, `cost_unknown` when `cost_per_kg_at` is
NULL, and `print_run_id` = the first part of its `ref_id`, or, for a `bb_usage` row, its
paired `print_run` event's (R-P3-28).

**Interfaces:**
- Produces: `class RunUse(NamedTuple): spool_id: UUID; grams: Decimal; cost_per_kg_at:
  Decimal | None`; `ledger.run_uses(conn, run_ids: Sequence[str]) -> dict[str,
  list[RunUse]]`: per run, per spool, the grams of its `spool` rows in `inventory_moves`,
  each at its own `cost_per_kg_at` (R-P3-16, R-P3-28).
- Produces: `AttemptCost.filament_basis: Literal["spools", "quote", "price_book"] | None`.
  An attempt with run uses and an order in the shop's currency: filament = Σ grams ×
  `cost_per_kg_at` / 1000, in minor units; a use with no `cost_per_kg_at` is priced at the
  quote's price per gram, else the price book, and marks `filament` estimated. Other
  attempts keep today's rule. An order in another currency never uses spool cost (R-P2-33).
  This is D7's order: the spool's unit cost, its type's cost (both in `cost_per_kg_at`), the
  quote, the price book.
- Produces (R-P3-17): `StockStore.recost_unit` for a filament unit calls
  `SpoolUnits.recost(conn, unit_id) -> RecostResult`, which re-snapshots `cost_per_kg_at` of
  its spool's `print_run`/`bb_usage` events at the spool's cost now and gives, per order item
  whose attempts' runs name them, the filament cost before and after.
  `OrderLifecycle.recost_unit(unit_id, by) -> RecostResult` writes one `item.parts_recosted`
  event per order for a unit's moves and one `item.filament_recosted` event for a spool's
  events. `POST /inventory/units/{unit_id}/recost` serves every unit; `POST
  /filament/spools/{spool_id}/recost` is its alias.

- [ ] **Step 1: Write the failing tests**:
  - `test_the_inventory_moves_view_counts_a_plate_once` (`test_pg_migrations.py`): a stock
    move and a spool's `print_started` of −30 g at 25.0000 (USD) → two rows, the spool row
    `unit_cost_minor == Decimal("2.5")`, `amount_minor == Decimal("-75")`; add a
    `print_ended` of −28 g → one spool row of −28; add a paired `bb_usage` row of −29 g → one
    spool row of −29 whose `print_run_id` is the run's.
  - `test_run_uses_counts_a_plate_once`: start 30 g est., end 28 g → 28; a paired BB row of
    29 g → 29.
  - `test_spool_cost_beats_the_quote` (D7): an attempt with a run that used 100 g of a spool
    at `cost_per_kg_at` 25.0000, a quote at 0.03/g, USD → `filament_minor == 250`,
    `filament_basis == "spools"`.
  - `test_a_use_with_no_cost_falls_back` (Review Focus 5): the same with NULL cost →
    `filament_minor == 300`, `"filament" in estimated`.
  - `test_an_order_in_another_currency_keeps_the_quote`: EUR order, USD shop →
    `filament_basis == "quote"`.
  - `test_an_attempt_with_no_run_is_priced_as_before`.
  - `test_a_respread_then_recost_lowers_the_order` (Review Focus 5): a spool unit at 2500
    (1000 g) used 100 g on `oi1`; a respread sets the unit to 2000; `POST
    /inventory/units/{id}/recost` → `orders == [RecostedItem("o1", "oi1", 250, 200)]`, one
    `item.filament_recosted` event, and `GET /orders/o1/costs` reads 200; the
    `/filament/spools/{id}/recost` alias answers the same.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament/test_ledger.py tests/orders/test_costs.py tests/api/test_inventory_stock.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** One shared reader for job cost and the profit report (R-P2-31).
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/orders tests/filament tests/api/test_orders_job_cost.py -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): job cost prices filament at its unit's cost (#7068, 4/7)`

## Layer 5 — `feat/7068-5-purchasing-ui` (on layer 4)

### Task 11: Suppliers and invoices move to Inventory

Load `frontend-design` first.

**Files:**
- Move: `frontend/src/pages/filament/InvoicePage.tsx`, `InvoicesTab.tsx`, `SupplierPage.tsx`,
  `SuppliersTab.tsx` and their tests to `frontend/src/pages/inventory/` (`git mv`)
- Modify: `frontend/src/App.tsx` (`inventory/invoices`, `inventory/invoices/:id`,
  `inventory/suppliers`, `inventory/suppliers/:id`; `filament/invoices/:id` and
  `filament/suppliers/:id` → `<Navigate replace>` to them, spec §6.7; the filament tabs
  `invoices` and `suppliers` redirect too), `frontend/src/lib/inventoryLinks.ts`
  (`.invoice(id)`, `.invoiceLine(invoiceId, lineId)`, `.supplier(id)`, `.invoiceReview`),
  `frontend/src/lib/filamentLinks.ts` (invoice and supplier paths come from
  `inventoryPaths`), `test-fixtures/filament/ui-paths.json` and
  `test-fixtures/inventory/ui-paths.json` (the new paths), `frontend/src/components/AppShell.tsx`
  (Inventory sub-links), the mocks (`frontend/src/mocks/features/`)
- Test: the moved tests, `frontend/src/lib/inventoryLinks.test.ts`, `filamentLinks.test.ts`,
  `frontend/e2e/page-titles.spec.ts`

**Interfaces:**
- Consumes: the layer 2 routes (`pnpm gen:api`).
- Produces (InvoicePage): one line editor for every item: an item search over
  `/inventory/items` (a filament item shows its colour swatch; "New spool type" opens the
  type fields as `proposed_type`), a `kind` select, an offer, a pack size, and units to
  fill (spools or any unit item's units); candidates show `matched_by`; each confirmed line
  has a links list (add entity by `kind:ref` search, share, note, its amount and consumer
  link); "Amend" opens new lines plus header totals and a "Re-spread over all lines" switch,
  shows the preview's before and after per line, then confirms; after a respread it lists
  each `recost` target with a "Re-cost past uses" button (`POST
  /inventory/lots/{id}/recost` or `/inventory/units/{id}/recost`); an events list; the file
  preview reads the `invoice` attachment (#6889).

- [ ] **Step 1: Write the failing tests**: `/filament/invoices/x` lands on
  `/inventory/invoices/x`; a `hardware` line sends `{item_id, kind: "stock"}`; a filament
  line sends `{item_id, fill_unit_ids}`; an amend preview shows `171.12 → 171.12` and the new
  line's `5063.16`; a 409 `amend_short` shows its detail; adding a link to `order:o1` shows
  its amount; a respread lists a spool's unit with "Re-cost past uses"; the fixture paths
  match both link modules.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/inventory src/lib` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `pnpm typecheck && pnpm lint`, and `pnpm exec playwright test e2e/page-titles.spec.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): suppliers, invoices, amend and line links in Inventory (#7068, 5/7)`

### Task 12: Linked lines; purchases; spools; shopping; the backfill review

**Files:**
- Create: `frontend/src/components/inventory/InvoiceLinesPanel.tsx` (`{entity}`; reads `GET
  /entities/{ref}/invoice-lines`), `frontend/src/pages/inventory/InvoiceReviewPage.tsx`
- Modify: `frontend/src/pages/inventory/ItemPage.tsx` (the panel; a filament item's page
  shows its units as spools and links to its type page; "Add to shopping list"),
  `LotPage.tsx` and `UnitPage.tsx` (the panel; "From invoice line" links; "Re-cost past
  uses" on a unit; a spool unit links to its spool), `frontend/src/pages/OrderPage.tsx`
  (the panel for the order), `frontend/src/components/orders/CustomerModels.tsx` (record a
  purchase with an optional invoice line picked from the order's linked or the supplier's
  expense lines), `frontend/src/pages/filament/SpoolPage.tsx` (the panel; "Re-cost past
  uses" through the unit route), `frontend/src/pages/filament/ShoppingListTab.tsx` (item
  rows), `frontend/src/pages/inventory/InvoicePage.tsx` ("May be missing lines" flag;
  "Review missing lines" starts the backfill session (Task 14) and opens Amend prefilled
  from `amend_proposal`, with "Re-spread over all lines" on), `frontend/src/App.tsx`
  (`inventory/invoices/review`)
- Test: `InvoiceLinesPanel.test.tsx`, `ItemPage.test.tsx`, `UnitPage.test.tsx`,
  `CustomerModels.test.tsx`, `SpoolPage.test.tsx`, `ShoppingListTab.test.tsx`,
  `InvoiceReviewPage.test.tsx`, `InvoicePage.test.tsx`

**Interfaces:**
- Produces (`InvoiceReviewPage`, R-P3-31, R-P3-35): the `GET /inventory/invoices/review`
  list, newest first, each row with its supplier, number, date, total and signal chips
  (`subtotal_differs` shows header and line subtotals); an "All confirmed" toggle (`all`);
  per row "Open", "Mark done" (`lines_review = done`). A row without `has_file` says the
  session needs the file and offers the upload.

- [ ] **Step 1: Write the failing tests**: the panel lists a line with its invoice link and
  amount; recording a purchase with a line sends `invoice_line_id`; "Re-cost past uses" on a
  spool calls `/inventory/units/{id}/recost` and shows the returned orders with links; an
  item row on the shopping list links to its item; the review page shows a row with
  `subtotal_differs` and "4900.00 / 160.00"; "Mark done" sends `{lines_review: "done"}`;
  the invoice page with an `amend_proposal` opens Amend with its lines and the respread
  switch on, and nothing is sent until the person confirms.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/inventory src/pages src/components/orders/CustomerModels.test.tsx` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd frontend && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): invoice lines on items, orders and spools; the backfill review (#7068, 5/7)`

## Layer 6 — `feat/7068-6-purchasing-agent` (on layer 5)

### Task 13: `invoice_*` and `supplier_*` tools; the filament aliases

**Files:**
- Create: `agent/src/tools/purchasing.ts`
- Modify: `agent/src/tools/filamentPurchasing.ts` (`filament_invoice_*` and
  `filament_supplier_list|upsert` descriptions say "Alias of invoice_… / supplier_…; goes
  after one release" (spec §6.7) and call the `/inventory/…` routes),
  `agent/src/tools/filamentLinks.ts` and `inventoryLinks.ts` (invoice and supplier
  `ui_path`), `agent/src/tools/orders.ts` (`order_purchase_record` takes `invoice_line_id`),
  `agent/src/tools/inventory.ts` (`inventory_unit_recost`; `filament_spool_recost` in
  `filament.ts` is its alias), `agent/src/tools/entities.ts` (`item:` and `unit:` refs for
  types and spools; `spool_type:` and `spool:` still parse), `agent/src/tools/index.ts`,
  `agent/src/tools/coverage.ts` (drop the layer-2 entries), `agent/src/sessions/touched.ts`
  (every new write tool)
- Test: `agent/test/purchasing.test.ts`, `agent/test/filamentPurchasing.test.ts`,
  `agent/test/filamentLinks.test.ts`, `agent/test/inventoryLinks.test.ts`,
  `agent/test/orders.test.ts`, `agent/test/coverage.test.ts`

**Interfaces:**
- Produces tools (route; risk): `supplier_list` (read), `supplier_upsert` (write),
  `invoice_list` (read), `invoice_get` (read; lines with links, lots, units),
  `invoice_candidates` (read; items and their fillable units), `invoice_upsert` (write;
  lines take `item_id`, `offer_id`, `kind`, `pack_size`, `fill_unit_ids`,
  `proposed_item`, `proposed_type`), `invoice_confirm` (write), `invoice_amend_preview`
  (read; `POST …/amend?preview=true` writes nothing), `invoice_amend_propose` (write; `PUT
  …/amend-proposal`, no line changes), `invoice_amend` (outward, gated: a person approves it,
  R-P3-34), `invoice_delete` (write), `invoice_events` (read), `invoice_review_list` (read),
  `invoice_line_link_list` (read; by line or by entity), `invoice_line_link_set` (write; add
  or update), `invoice_line_link_delete` (write), `inventory_unit_recost` (write). Each
  description says what it writes: amend spreads only the tax and shipping no line holds
  unless `respread` (D15); a link's share becomes a maintenance event or an order cost entry
  only on an expense line (R-P3-9); re-cost changes past order costs (D14).

- [ ] **Step 1: Write the failing tests**: each tool's route and risk (the table style of
  `agent/test/inventory.test.ts`); `invoice_amend` is `outward` and gated;
  `filament_invoice_get` answers as `invoice_get`; `order_purchase_record` with
  `invoice_line_id` sends it; `uiPath("invoice:<id>")` matches the fixture.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/purchasing.test.ts test/filamentPurchasing.test.ts test/filamentLinks.test.ts test/inventoryLinks.test.ts test/orders.test.ts test/coverage.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd agent && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): invoice and supplier tools for every item (#7068, 6/7)`

### Task 14: The backfill session for confirmed invoices; the filament skill

**Files:**
- Create: `agent/src/filament/invoiceBackfillPrompt.ts`
- Modify: `agent/src/routes/invoiceImport.ts` (body `{invoice_id, mode?: "import" |
  "missing_lines"}`; `missing_lines` takes a confirmed invoice with an `invoice` attachment
  and answers 409 `no_file` without one; `import` keeps today's 409 `confirmed`),
  `agent/src/filament/invoiceImportPrompt.ts` (the `invoice_*` tools; every printed line
  goes on the invoice, a non-filament one as an item line; items of any kind by SKU, then
  MPN, then name), `plugins/scadbuddy/skills/filament/SKILL.md`,
  `plugins/scadbuddy/.claude-plugin/plugin.json` (`0.1.62`, `check-plugin-version.sh`)
- Test: `agent/test/invoiceImportRoute.test.ts`, `agent/test/invoiceImportPrompt.test.ts`,
  `agent/test/invoiceBackfillPrompt.test.ts`

**Interfaces:**
- Produces (`invoiceBackfillPrompt(invoiceId: string) -> string`, R-P3-33): the session
  reads the invoice with `invoice_get` (its kept lines), reads the stored file again (the
  `invoice` attachment, sent with the prompt as the import sends it), lists every printed
  line, and pairs each with a kept line by SKU, then description, then amount. It proposes
  only the printed lines with no kept line, as item lines (`stock` for a lot or unit item,
  `asset` for equipment put in use, `expense` for a service, a fee or a repair), each with
  an item from `invoice_candidates` or a `proposed_item`, plus the printed header subtotal,
  tax, shipping and total. It checks the proposal with `invoice_amend_preview`
  (`respread: true`) and stores it with `invoice_amend_propose`. It never calls
  `invoice_amend`. It ends by naming the lines it proposed, the ones it could not read, and
  each spool whose cost the respread lowers.
- Produces (`SKILL.md`): #7648's section, changed to phase 3 (R-P3-36): "### A line that is
  not filament": an invoice may hold NFC tags, nozzles, desiccant or tools; each goes on the
  invoice as an item line (`invoice_upsert` with `item_id` from `inventory_item_list` `q`,
  or `proposed_item`; `kind` `stock`, `asset` or `expense`), so the header keeps the printed
  totals, reconciles, and spreads tax and shipping over every line; never leave a line off
  and never cut the header to the filament share. And "### Confirmed invoices that are
  missing lines": use `invoice_review_list`, start the backfill from the invoice page, and
  leave the amend to the person. The section cites #7648 and spec §6.7.

- [ ] **Step 1: Write the failing tests**: `POST /api/v1/ai/filament/invoice-import` with
  `mode: "missing_lines"` on a confirmed invoice with a file starts a session whose prompt
  is `invoiceBackfillPrompt`; without a file → 409 `no_file`; `mode` absent on a confirmed
  invoice → 409 `confirmed` as today; the backfill prompt names `invoice_amend_propose` and
  `invoice_amend_preview` and says it never calls `invoice_amend`; the import prompt names
  `invoice_get` and `invoice_upsert`, not the old names, and says a non-filament line is an
  item line.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/invoiceImportRoute.test.ts test/invoiceImportPrompt.test.ts test/invoiceBackfillPrompt.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd agent && pnpm lint && pnpm typecheck && pnpm test` and `.github/scripts/lint-plugin.sh` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): a reviewed backfill of lines a confirmed invoice misses (#7068, 6/7)`

## Layer 7 — `feat/7068-7-purchasing-docs` (on layer 6)

### Task 15: Docs

**Files:**
- Modify: `docs/features/inventory.md` (purchasing, line kinds, links, amend, the backfill,
  the shared model and the spool workflow), the filament feature page that describes
  invoices (`grep -ln "filament_invoice" docs/features`), `docs/features/orders-and-quotes.md`
  (purchases with a line; job cost's filament basis), `docs/architecture/data.md` (the
  renames, the views, the two new tables, the moved and retired tables, the
  `inventory_moves` view, the three migrations), `docs/architecture/frontend.md` (Inventory
  invoice, supplier and review routes; the redirects), `backend/CLAUDE.md` (the
  `inventory/` and `filament/` layout lines), `agent/CLAUDE.md` (the tool families), the
  spec's §9 phase 3 line (a "done in" note with the PRs), and §6.4's filament paragraph and
  D1 where R-P3-30 changes them (the unit carries the cost; grams stay in the ledger).

- [ ] **Step 1: Edit**, each fact with its source (file:line, PR or issue).
- [ ] **Step 2: Run** `grep -rn "filament_supplier_products\|filament_price_points\|filament_invoices\b\|filament_invoice_lines\|supplier_product_id\|update_spool_ids" docs backend/CLAUDE.md frontend/CLAUDE.md agent/CLAUDE.md plugins` — Expected: only historical specs and plans, and the retired names in `data.md`.
- [ ] **Step 3: Commit** `docs: generic purchasing and invoice line links (#7068, 7/7)`

## Follow-ups (issues to file when layer 3 merges)

- Drop the views `filament_suppliers`, `filament_invoices`, `filament_invoice_lines`,
  `filament_supplier_products`, `filament_price_points` (R-P3-1, R-P3-29) and the
  `invoices.file_*` columns (R-P3-14).
- Drop `filament_supplier_products_retired` and `filament_price_points_retired` after a prod
  check.
- Remove the `/filament/invoices…`, `/filament/suppliers` and `/filament/spools/{id}/recost`
  route aliases, the `filament_invoice_*`, `filament_supplier_list|upsert` and
  `filament_spool_recost` tool aliases, and the `spool_type:` and `spool:` ref aliases after
  one release (spec §6.7).
- Move `filament/invoices.py`, `invoice_money.py`, `suppliers.py` and the purchasing models
  into `inventory/` (R-P3-21).
- Phase 4 (#7069): printers as units, so a printer link is a `unit:` link.

## Rulings (R-P3, the planner's, where the spec leaves a choice open)

- **R-P3-1.** The three renames land in layer 1, with views under the old names for the
  rolling update (precedent `20261122T0426Z_entities.sql`). Reason: a mechanical rename is
  reviewed apart from the data move, and old pods keep working.
- **R-P3-2.** An offer keeps its product's id, and an offer price point its price point's id;
  lines gain `offer_id`, filled from `supplier_product_id`. Reason: alias routes, activity
  details and invoice lines stay valid with no map table.
- **R-P3-3.** A filament item keeps its spool type's id and is named by its `code`; a spool's
  unit keeps the spool's id. Reason: one id per thing; `code` is unique among unarchived
  types, as the item name must be.
- **R-P3-4** (changed by R-P3-30). A filament item takes every call a unit item takes:
  receive, status, cost, re-cost, offers. Receiving its units makes its spools. Its lot
  calls answer `WrongTracking`, as a unit item's do. Creating one takes the type's fields.
  Reason: the owner's ruling; the type fields are what the spool workflow needs.
- **R-P3-5** (changed by R-P3-30). `item:` and `unit:` refs of spool types and spools work.
  The migration moves the `spool_type` and `spool` entities, files and schemas to `item` and
  `unit`; `spool_type:` and `spool:` stay accepted as aliases for one release. Reason: one
  entity per thing, with no refusal.
- **R-P3-6** (changed by R-P3-30). The unit carries the spool's cost in `cost_minor`, as
  every unit does. `filament_spools.cost_per_kg` is its per-kg form (cost over label
  weight), kept for the spool workflow (Bambuddy, labels, summaries). `set_unit_cost_in` is
  the one writer of both. A label weight change keeps what was paid. The type's
  `cost_per_kg` stays the item's default cost (D7's second step). A typeless spool has no
  unit until it is typed. Reason: one cost source per thing; the per-kg form is derived.
- **R-P3-7** (owner accepted 2026-11-25; changed by R-P3-30). One counting rule for every
  line: units = `quantity × line.pack_size`; `pack_size` defaults to the offer's, else 1.
  A spool line is a pack of 1 unless its offer says otherwise. Migrated lines get
  `pack_size` 1, because they counted spools. Price per offer pack = `line_total /
  (quantity × line pack) × offer pack`. Reason: one rule, and the old lines keep their
  meaning.
- **R-P3-8.** `asset` lines are for unit items and receive units `in_service`; a `none` item
  goes on `expense` lines only. Reason: the spec names the kind but not its effect.
- **R-P3-9** (owner accepted 2026-11-25). Only links on confirmed `expense` lines write
  consumer rows; links on `stock` and `asset` lines are references. Reason: a received lot or
  unit already carries the cost, and a second consumer would count it twice.
- **R-P3-10.** A line links to one order and one printer at most, and serves one purchase per
  order. Reason: the unique `(order_id, kind, source)` index, `UNIQUE (printer, source)` and
  the unique `order_item_sources.cost_entry_id`, with source `invoice_line:<id>` as the spec
  names it.
- **R-P3-11.** Lock order: orders by id, the invoice, items with their lots and units
  (R-P2-32), spool rows by id. Reason: `record_purchase` already locks the order first.
- **R-P3-12.** Amend is one atomic call with a preview that rolls back. A respread also moves
  each kept line's invoice price point. Reason: no half-amended invoice can exist.
- **R-P3-13.** The amend's "activity entry" is an `invoice_events` row. Reason: invoices have
  no activity log, and confirm is not an operation kind.
- **R-P3-14.** The invoice file is its one `invoice`-role attachment; an upload replaces it.
  The `file_*` columns stay until a follow-up. Reason: old pods read them during the rollout.
- **R-P3-15.** The Bambuddy supplier filter reads offers of filament items and invoices with
  a filament item's line (updates R-P2-8 to R-P2-10). Reason: D16 after the offers merge.
- **R-P3-16** (changed by R-P3-30). An attempt's spool uses are its run's events, counted by
  the ledger's rules, each at its `cost_per_kg_at`, which is taken from the spool's unit
  cost; a use with no cost falls to the quote, then the book. Reason: D7, with the unit as
  the cost source and the ledger as the gram source (D1).
- **R-P3-17** (changed by R-P3-30). "Re-cost past uses" is `POST
  /inventory/units/{id}/recost` for every unit; for a spool unit it re-snapshots its use
  events. The spool route is an alias. Reason: one re-cost path; spec §6.6 names it for
  spools.
- **R-P3-18.** Job cost before and after is read on prod just before and after layer 4
  deploys. Reason: the change is read-only, and prod data never leaves prod.
- **R-P3-19.** Shopping items take `item_id`; `material` becomes nullable under a CHECK.
  Reason: spec §6.5; a non-filament row has no material.
- **R-P3-20.** Supplier CRUD moves to `/inventory/suppliers`, with the `/filament` routes as
  aliases for one release; product, price and price-sheet routes stay filament routes over
  offers. Reason: suppliers are generic; products are the filament view of offers.
- **R-P3-21.** The invoice and supplier modules stay in `filament/` in phase 3; new code goes
  in `inventory/`. Reason: a move is a separate, mechanical change.
- **R-P3-22.** Lines take `proposed_item`, made at confirm as `proposed_type` is; a
  `proposed_type` makes a filament item through the type store. Reason: the import proposes
  items of any kind (spec §6.7), and a filament item needs its type's fields.
- **R-P3-23.** A bundle SKU with no item named is a line error that names the items. Reason:
  D13 says the line splits per item; a guess would receive the wrong stock.
- **R-P3-24** (changed by R-P3-30). One candidates path: an offer's SKU, then an item's `mpn`
  property against the line's SKU, then the item name against the description; a kind's
  extension may add a matcher (filament: colour). Each unit item candidate lists its units
  with no invoice line. Reason: spec §6.7's order, for every item.
- **R-P3-25.** The agent's invoice-import route keeps its path. Reason: only its prompt, its
  tools and a `mode` change.
- **R-P3-26.** Offer SKUs take 128 characters. Reason: filament products and invoice lines
  already do (`_SKU_MAX`).
- **R-P3-27.** The entities sweep skips, and a merge moves, an entity that a link names.
  Reason: as R-P2-4; a sweep must not fail on the link's foreign key.
- **R-P3-28.** The `inventory_moves` view applies the ledger's counting rules, and job cost
  reads spool uses from it. Reason: one implementation of "a plate counts once" serves
  reports and job cost.
- **R-P3-29** (owner, 2026-11-25). The layer-3 data migration ships like any other PR: no
  owner-run gate and no deploy window. It handles every data case itself, with a test for
  each, and keeps views under the old table names for old pods during the rollout.
  Reason: the owner's ruling ("use a migration ... it should ship like any other pr").
- **R-P3-30** (owner, 2026-11-25). "Spools and other items should be treated the same --
  spools just have a different work flow associated with them." A spool type is an item of
  kind `filament` and a spool is a unit of it; one receive, confirm, candidates, cost and
  re-cost path serves every item. The spool workflow (grams and the ledger, trays, tags,
  labels, presets, K values, Bambuddy sync) is a `UnitExtension` and a few type fields; the
  table "The shared model and the spool workflow" draws the line. Changes R-P3-4, -5, -6,
  -7, -16, -17, -22, -24 and Tasks 3, 5, 7, 8, 9, 10.
- **R-P3-31.** The backfill list uses only signals the data holds: header `subtotal` or
  `tax` that differ from the lines' sums by more than 0.05 (confirm never compared them,
  `filament/invoices.py:571`), and a person's flag; `has_file`, `imported` and `has_note`
  are shown, not filtered on. Reason: a cut header still reconciles on `total`, so only the
  subtotal, the tax or a person can show a missing line.
- **R-P3-32.** `invoices.lines_review` (`flagged`, `done`) holds a person's flag and the end of
  a review; an amend sets `done`. Reason: the list must shrink as invoices are reviewed.
- **R-P3-33.** The backfill is a `missing_lines` mode of the existing invoice-import session.
  It re-reads the stored `invoice` attachment, pairs printed lines with kept ones by SKU,
  description and amount, and proposes only the unpaired ones as item lines. Reason: the
  session already reads the file and knows the tools.
- **R-P3-34.** The proposal is stored on the invoice (`amend_proposal`) and writes no line.
  A person applies it from the invoice page; the agent's `invoice_amend` is `outward` and
  gated. Reason: the owner's rule that a person confirms each amend.
- **R-P3-35.** A backfill amend opens with "Re-spread over all lines" on, and its result lists
  each spool unit whose cost dropped with "Re-cost past uses". Reason: the old header spread
  tax and shipping over the filament lines only, so their spools' cost is overstated, and
  the re-cost carries the fix to past job cost.
- **R-P3-36.** The filament skill takes #7648's section, changed so that a non-filament line
  becomes an item line on the same invoice, with the printed header kept; layer 6 closes
  #7648. Reason: phase 3 answers #7648's open question with line kinds.
