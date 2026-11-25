# Inventory phase 3: one item model for filament, generic purchasing, and the Filament section in Inventory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filament is ordinary inventory. A spool type is an item and a spool is a unit of
it (owner, R-P3-30). Material and subtype are child kinds of `filament` in `item_kinds`
(owner, R-P3-45). Entities of inventory things are `item:` and `unit:` only (R-P3-47).
Suppliers, offers, price points, invoices and locations serve every item, with no
`filament_` prefix (R-P3-49). An invoice line of any kind receives lots or units, or goes to
the entities it links to; every line keeps billed apart from received (R-P3-37). A confirmed
invoice can be amended. Job cost reads spool cost from the spool's unit. The Filament
section becomes Inventory's `filament` view, and the spool workflow's pages live under
Inventory (owner, R-P3-50). Invoice import and the backfill of confirmed invoices that miss
lines are one built-in flow whose person decisions are typed prompts (owner, R-P3-51) (#7068,
epic #6949).

**Size (owner rule E, R-P3-55).** This is two phases, each with its own layers and its own
issue:

- **Phase 3a, the data model** (#7068): the kinds tree, the entity fold, the plain
  `suppliers`, `invoices`, `invoice_lines`, `locations` and `location_kinds` tables, the
  generic line model, receive, links, amend, the migrations, job cost, the routes and the
  agent tools. Layers A1 to A6. The UI stays where it is and keeps working through the
  route aliases.
- **Phase 3b, the move** (a new issue under #6949; see "Issues to file"): the Filament
  section moves into Inventory, the invoice editor gets the line model, and the invoice
  import and backfill become the `invoice-import` flow. Layers B1 to B4. It starts when
  phase 3a's layer A5 has merged.

**Architecture:** The filament purchasing tables and the location tables get plain names,
with views under the old names for the rolling update. `filament_supplier_products` and
`filament_price_points` move into phase 2's `supplier_offers` and `offer_price_points`, with
their ids kept. Each material and each material-subtype pair a spool type uses becomes a
child kind of `filament`, named with the catalog's exact text. A spool type becomes an item
of its kind with the type's id; a spool becomes a unit with the spool's id. The unit carries
the spool's cost. One receive, one confirm and one candidates path serve every item. The
spool workflow (grams, the ledger, trays, tags, labels, presets, K values, Bambuddy sync)
hangs off the unit and the item as an extension. A new `invoice_line_links` table links a
line to any entity; a link on an expense line writes one consumer row and remembers it. The
HTTP routes are the stable contract and every SQL statement lives in a store (#7667, R-P3-54).
Only the API process runs SQL (#5295). In phase 3b the invoice flow runs on the `projects`
queue as an HTTP client: an `agent()` step reads the file, the server's candidates route
matches, and `prompt()` tasks take each decision.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, Temporal flows
(`ProjectWorkflow`, temporal-agent-harness), React + TypeScript, TypeScript agent tools
(Claude Agent SDK).

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.2 (the
invoice file), §6.3a (the filament kind's schema), §6.4 (kinds; filament items and units;
locations), §6.5, §6.6 (cost snapshots, job cost), §6.7, §6.11, D1, D7, D9, D13, D14, D15,
D16, §9 phase 3. The phase 2 plan `docs/superpowers/plans/2026-11-23-inventory-phase-2-item-core.md`
and its rulings R-P2-1 to R-P2-44 (comment on #7067). The flows spec
`docs/superpowers/specs/2026-11-24-shop-floor-flows-design.md` §6.3 and §10, and its A1 plan
`docs/superpowers/plans/2026-11-24-flow-prompts-a1.md` (epic #7361; A1 merged in #7365 and
#7445). #7648 (the filament skill and non-filament invoice lines; read whole). #7681
(weighing as reconciliation, spec in PR #7687, owned by session scadbuddy-53): phase 3 reads
its weighings and builds no second weighing model. #7667 (draft: the backend moves to Go
behind the same HTTP contract).

## What exists on main today (verified at `814f1285c`, 2026-11-25; the kinds, locations, entities, flows and UI facts at `cf8590f53`)

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
- Kinds (`migrations/20261124T0323Z_inventory_core.sql:13-27`): `item_kinds.id text CHECK
  (id ~ '^[a-z][a-z0-9_]{0,39}$')`, `name text CHECK (length(name) BETWEEN 1 AND 100)`,
  `parent_id text REFERENCES item_kinds (id)`, `tracking`, `attribute_schema` ("a child kind
  inherits its parent's keys"). `filament` is a built-in root kind with `tracking =
  'filament'`, `uom = 'g'` (l.189-190). `inventoryPaths.kind(id)` lists the items of a kind
  and of its descendants (`frontend/src/lib/inventoryLinks.ts:23-24`).
- Material and subtype are catalog tables (`migrations/20261114T1300Z_filament_catalogs.sql`):
  `filament_materials` (unique `upper(name)` among the unarchived; density, temperatures,
  drying, and the handling flags `needs_enclosure`, `abrasive`, `hygroscopic`, `flexible`,
  `hardened_nozzle`) and `filament_subtypes` (`material_id` NULL means "for every
  material"; unique per material by `upper(name)`). Aliases are in `filament_material_names`
  and `filament_subtype_names` (`20261120T0602Z_filament_catalog_names.sql`). A spool type
  names them by `material_id` and `subtype_id` (`filament/models.py:240-266`). The names go
  as plain text to 3DFP and to Bambuddy's profile matching: `profile_fetch/wire.py` `ranked`,
  `profile_fetch/site.py` `_choose` (l.671), `profile_fetch/duplicates.py` `_agrees` (l.41),
  `bambuddy/filament_profiles.py` `ProfileMatch` (l.41, compares `self.material.upper()`
  at l.53) and `spool.material` (l.374), and the agent tools `filament_tdfp_search` and
  `filament_tdfp_create`.
- Locations: `filament_locations` (a tree by `parent_id`; an AMS tray names its printer,
  AMS and tray) and `filament_location_kinds`
  (`20261114T1300Z_filament_catalogs.sql:130-170`). SQL on them: `filament/locations.py`
  (41 lines name the table), `sensors.py` (7), `bambuddy_pull.py` (7), `spools.py` (5),
  `bb_sync_plan.py` (5), `capture.py` (4), `inventory.py` (3), `csv_import.py` (3),
  `printers/store.py` (2), `labels.py` (2), `bb_sync_results.py` (2), `codes/resolve.py`
  (2: code prefix `L`), `inventory/stock.py` (1), `metrics_store.py` (1),
  `entities/registry.py` (1) (`git grep -c filament_locations origin/main`).
  `filament_location_sensors` has an FK to it (`20261118T0556Z_filament_sensors.sql`).
- Entity kinds (`entities/registry.py` `KINDS`): `order`, `order_item`,
  `order_item_fulfilment`, `order_item_revision`, `spool` (`filament_spools`), `spool_type`
  (`filament_spool_types`), `supplier` (`filament_suppliers`), `invoice`
  (`filament_invoices`), `location` (`filament_locations`), `printer`, `customer`, `item`,
  `lot`, `unit`, and the filament catalogs (`material`, `subtype`, `brand`, `category`,
  `effect`, `insert_material`, `insert_type`, `colour`, `location_kind`). There is no `part`
  kind: phase 2 moved `part:` subjects to `item:`
  (`migrations/20261124T0918Z_parts_to_items.sql:110`). Code prefixes: `L` on
  `filament_locations`, `S` on `filament_spools` (`codes/resolve.py:24-30`). Attempt media
  are archive copies under `order_item:<id>` and `order_item_fulfilment:<id>`, matched by
  subject string in `OrderStore.archive_media_present`, `archive_media_attachments` and
  `attempts_missing_media` (`orders/store.py`; tests `tests/orders/test_archives.py`,
  `test_attempt_files.py`).
- Flows (#7361 A1 is merged: #7365, #7445). Host functions in
  `backend/scadbuddy/workflows/flow_tools.py`: `prompt(title, schema, context=None,
  place=None, ui=None, timeout_s=None) -> dict` (l.188-248; opens a `tasks` row, waits with
  no limit unless `timeout_s`, returns the answer filled from the schema);
  `agent(prompt, skills, result_schema, images=None, grant=None, on_outside="escalate",
  browser=None)` (`_agent_session`, a durable agent session whose answer is checked against
  `result_schema`); `tool(name, args) -> str` and `tool_result(name, args) -> ToolResult`
  (an outward tool waits for approval); `wait_for_human`. Routes: `GET /api/v1/tasks`,
  `GET /api/v1/tasks/{task_id}`, `POST /api/v1/tasks/{task_id}/answer` (browser only,
  `Idempotency-Key`, 422 names the field), `POST /api/v1/workflows/{definition_id}/runs`
  (`api/flows.py:725`). A prompt's schema is the `core/input_schema.py` subset: a top-level
  object of `string` (with `enum`, `minLength`, `maxLength`, `pattern`, and `format`
  `email`, `date`, `date-time`), `integer`, `number`, `boolean`, or an `array` of those
  (l.103-107); no nested object, no `$ref`. The run page's `PendingCard` draws the form; it
  does not draw `context` blocks yet. A built-in flow is registered at start by
  `FlowDefinitions.register_new` and upgraded by `upgrade_builtin` while unedited
  (`orders/builtin_flow.py`, #4252, #4673; author `{"kind": "builtin", "name"}`). Not on
  main: the A2 widgets (`x-sb-widget` `entity`, `scan`, `weight`), context blocks on
  screen, the station view, scan-to-answer, the agent's `task_list`/`task_answer` tools,
  `step()` and projects (B2).
- `agent()`'s `images` are this run's images (`flow_image`, `agent/src/tools/imageRefs.ts:58`).
  No ref kind names an invoice file or any document. The import route sends the invoice PDF
  or photo as a session attachment (`agent/src/routes/invoiceImport.ts:140-174`,
  `checkedAttachments` with `DOCUMENT_MEDIA_TYPES`).
- Filament UI (`frontend/src/App.tsx:127-145`, `pages/filament/tabs.ts`): `/filament`
  redirects to `FILAMENT_HOME`; tabs `/filament/spools`, `types`, `tags`, `suppliers`,
  `invoices`, `locations`, `catalog`, `metrics`, `shopping-list`, `ledger`
  (`FilamentPage`); pages `/filament/spools/:id` (`SpoolPage`), `/filament/types/:id`
  (`TypePage`), `/filament/locations/:id` (`LocationPage`), `/filament/suppliers/:id`,
  `/filament/invoices/:id`, `/filament/labels` (`LabelsPage`), `/filament/scan`
  (`ScanPage`). Inventory: `/inventory`, `/inventory/kinds`, `/inventory/items/:id`,
  `/inventory/units/:id`, `/inventory/lots/:id` (l.147-151). The filament settings are a
  Settings section (`pages/settings/sections/filament.tsx`), not a Filament tab.

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
| Billed against received on every line (`received_units`, the free part) | Received grams: a spool's first scale or person weighing, read from the ledger (#7681 owns weighing) |
| The kind tree: `filament` → material (`PLA`) → subtype (`Silk`), each a row of `item_kinds` named with the catalog's exact text (R-P3-45, R-P3-56) | Each material's and subtype's print, drying and handling defaults (`filament_materials`, `filament_subtypes`, reached from the kind through `filament_kind_catalog`) |
| Item properties typed by `filament`'s `attribute_schema`: `rgba`, `colour_name`, `diameter_mm` (R-P3-46) | The spool type's mirror columns of those, written only by the property store, for the workflow's readers |
| `suppliers`, `invoices`, `invoice_lines`, `locations`, `location_kinds` (R-P3-49) | AMS trays as locations with a printer; sensors (`filament_location_sensors`); printer slot tables (`filament_override_slots`, `printer_kprofile_notes`, `printer_slot_presets`, #7670) |

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
  layer-A3 data migration ships like any other PR: no owner-run gate and no deploy window
  (owner, 2026-11-25; R-P3-29). It handles every data case itself, each with a test, and
  old pods keep working through views, kept columns and the
  start-time catch-up during every layer's rollout (R-P3-63).
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
  `spool:<id>` stay accepted as aliases of `item:<id>` and `unit:<id>` for one release
  (R-P3-47).
- Billed and received are two figures on every line, filament included (owner,
  2026-11-25; R-P3-37). The price paid is spread over everything received; the free part
  (received − billed) is stored as both figures and shown, never hidden in a cost.
- No double receive: a good already received outside an invoice (#7648's interim rule) is
  adopted by its line, never received again (R-P3-43).
- Weighing belongs to #7681. Phase 3 reads a spool's measured net grams from the ledger and
  adds no weighing column or event (R-P3-39).
- Nothing changes a confirmed invoice's lines without a person: an amend that writes is
  confirmed by a person in the UI, or by a person's approval of the agent's gated
  `invoice_amend` (R-P3-34).
- Agent tool risks: reads `read`; writes `write`; `invoice_amend` `outward`. Every result
  names `ui_path` or `<field>_ui_path` (R-P2-43). Descriptions cite their sources (#259).
- Each layer: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format
  --check . && uv run --frozen mypy`; `cd frontend && pnpm lint && pnpm typecheck`;
  `cd agent && pnpm lint && pnpm typecheck` for the trees it touches.
- Conventional-commit titles with `(#7068, 3a N/6)` or `(#<3b>, N/5)`; PR bodies as
  "Phases and layers" says. Every fact in docs and comments cites its
  source (#259). Every doc and comment a layer touches matches its code.
- Full parity (R-P3-58): no purchasing or type feature exists for filament only, except the
  spool workflow of R-P3-30's table. Names: "Item types", "Stock", "Lots", "Units"
  (R-P3-59).
- Load `frontend-design` before Task 15.
- **#7667 (draft, not approved) moves the backend to Go area by area behind the same HTTP
  contract, with Python left only as Temporal activities.** Phase 3 builds in Python, but
  every route it adds or changes is the contract: a route handler holds no SQL and no
  business rule beyond parsing and problem mapping, every SQL statement is in a store
  method, and the flow, the agent tools and the frontend reach data only through routes
  (R-P3-54).
- Kind names are the catalog's exact text. A material kind is named `PLA`, `PETG`,
  `PETG-CF`; a subtype kind `Basic`, `Silk`, `Matte`; never a slug, an id or a path. Every
  read that today answers `spool.material` or a subtype name answers the same text after
  the move (3DFP and Bambuddy matching compare it, R-P3-56).
- Entity kinds of inventory things are `item` and `unit` (and `lot`). `spool_type:` and
  `spool:` stay accepted as ref aliases for one release. The order kinds (`order`,
  `order_item`, `order_item_fulfilment`, `order_item_revision`) and every other kind stay as
  they are (R-P3-47).
- Phase 3 renames no printer workflow table (`filament_override_slots`,
  `printer_kprofile_notes`, `printer_slot_presets`, #7670) and no sensor table
  (`filament_location_sensors`); their FKs follow the renamed `locations` table. The new
  `label_sheets` table (#7216) already has a plain name (R-P3-57).
- The HTTP routes under `/api/v1/filament/` that the spool workflow serves keep their paths.
  Only suppliers, invoices and locations get `/inventory/` routes, with the old paths as
  aliases for one release (R-P3-20, R-P3-49).
- Phase 3b's flow uses only the #7361 API that is on main (A1, Pre-flight 9). It works with
  plain forms and gains the A2 widgets and context blocks when they land, with no change to
  the flow's answers (R-P3-53).

## Pre-flight (cross-layer contracts; check before Task 1)

1. **Ids are kept.** An offer keeps its product's id. An offer price point keeps its price
   point's id. A filament item keeps its spool type's id (`filament_spool_types.item_id =
   id`). A spool's unit keeps the spool's id (`filament_spools.unit_id = id`). So every id
   in an alias route, an activity detail and an `entities.ref` stays valid (R-P3-2, R-P3-3).
2. **Renames land in layer A1 with views.** `suppliers`, `invoices`, `invoice_lines`,
   `locations`, `location_kinds` and `shopping_items` are the tables from layer A1 on. Views with the old names serve the old pods during the
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
   `agent/test/coverage.test.ts` fails (`agent/CLAUDE.md` l.397). Layer A2 adds temporary
   entries that name the layer-A5 tools; layer A5 drops them.
6. **Data cases the migration handles** (R-P3-29). Each query below names a case that
   Task 9 handles in SQL and pins with a seeded-row test; none stops the migration except a
   true invariant (step 1). The control session runs them read only on prod
   (`deploy/CLAUDE.md` "Prod checks") as a cross-check when it can, and records the answers
   in the layer-A3 PR body. They are not a merge gate:
   - counts of `filament_supplier_products`, `filament_price_points`, `filament_spool_types`,
     `filament_spools`, `invoices` by `status`, and `filament_spool_events` by `ref_kind`
     where `ref_kind IN ('print_run','bb_usage')`;
   - `SELECT count(*) FROM filament_supplier_products WHERE length(sku) > 128` (expect 0);
   - `SELECT count(*) FROM filament_supplier_products WHERE product_url IS NOT NULL AND product_url !~* '^https?://'` (Task 9 stores those as no URL);
   - `SELECT max(length(code)) FROM filament_spool_types` (expect at most 200, the item name cap);
   - `SELECT count(*) FROM items WHERE id IN (SELECT id FROM filament_spool_types)` and `SELECT count(*) FROM items WHERE kind_id = 'filament'` (expect 0 and 0);
   - `SELECT count(*) FROM filament_spool_types a JOIN filament_spool_types b ON a.id < b.id AND lower(a.code) = lower(b.code) WHERE a.archived_at IS NULL AND b.archived_at IS NULL` (expect 0; the items index is on `lower(name)`);
   - `SELECT count(*) FROM filament_spools WHERE type_id IS NULL` (these get no unit);
   - `SELECT count(*) FROM filament_spools WHERE cost_per_kg IS NULL` (their units open cost-unknown);
   - `SELECT count(*) FROM filament_spool_events e JOIN filament_spools s ON s.id = e.spool_id LEFT JOIN filament_spool_types t ON t.id = s.type_id WHERE e.ref_kind IN ('print_run','bb_usage') AND coalesce(s.cost_per_kg, t.cost_per_kg) IS NULL` (uses with no cost; the spec expects 0, §6.6);
   - `SELECT kind, count(*) FROM entities WHERE kind IN ('spool','spool_type') GROUP BY 1` and `SELECT kind FROM entity_property_schemas` (Task 9 moves them to `item` and `unit`);
   - `SELECT count(*) FROM invoices WHERE file_key IS NOT NULL AND file_key !~ '^attachment-[0-9a-f]{64}$'` (expect 0; Task 9 reads the sha256 from the key);
   - `SELECT count(*) FROM blob_refs WHERE holder_kind = 'filament_invoice'` and the count of invoices with a file (expect equal);
   - `SELECT currency, count(*) FROM filament_price_points GROUP BY 1`;
   - `SELECT count(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id WHERE i.status = 'confirmed' AND l.supplier_product_id IS NULL` (expect 0);
   - the backfill signals (R-P3-31): `SELECT count(*) FROM invoices i WHERE status = 'confirmed' AND (abs(subtotal - (SELECT sum(line_subtotal) FROM invoice_lines WHERE invoice_id = i.id)) > 0.05 OR abs(tax - (SELECT sum(line_tax) FROM invoice_lines WHERE invoice_id = i.id)) > 0.05 OR note LIKE 'Not filament:%')`, and the count of lots and units with no invoice line whose note names a supplier (#7648's interim receipts, which Task 22 adopts).
   Where an "expect" does not hold, Task 9 must already convert that case (for example:
   cut a SKU to 128, store a bad URL as no URL, suffix a clashing name, leave a typeless
   spool without a unit, price a costless use as unknown).
7. **Job cost before and after** (layer A4, R-P3-18). Just before layer A4 deploys, the owner
   saves `GET /api/v1/orders/reports/profit?from=<now − 30 days>` and `GET
   /api/v1/orders/{id}/costs` for each order accepted in that range. Right after the deploy,
   the owner reads them again. Layer A4 changes only reads, so the diff is the change. The
   layer-A4 PR gets the table as a comment. Owner accepted (2026-11-25): it is posted on the
   PR after the deploy.
8. **#7681's interface** (session scadbuddy-53). Phase 3 needs only what is on main:
   `ledger.events(conn, spool_id)`, whose first `weighed` event with `source` in `('scale',
   'person')` has the spool's measured net grams in `grams_after` (filament only; the empty
   spool is already off), and `filament_spools.last_weighed_g` / `last_weighed_at`. Slice A
   (#7682: `SpoolDetail.last_weighing`, `data.reading_g`, `data.empty_g`, `data.expected_g`,
   `data.drift_g`, `last_drift_g`, `ledger.expected_before`) is optional: Task 20 shows
   `data.drift_g` when it is there and nothing when it is not. Phase 3 waits for no slice.
   In return, phase 3 keeps `filament_spools.unit_id` on the spool row (equal to its id, set
   for every typed spool), so #7681's empty-weight resolver
   (`filament/empty_weight.py`) can reach the carrier and the parts through it (#7278,
   R-P3-41).
9. **The #7361 API phase 3b uses** (all on main at `cf8590f53`): `prompt(title, schema,
   context=None, place=None, ui=None, timeout_s=None) -> dict`; `agent(prompt, skills,
   result_schema, images=None, grant=None, on_outside="escalate")`; `tool(name, args)` and
   `tool_result(name, args)`; `POST /api/v1/tasks/{task_id}/answer`; `POST
   /api/v1/workflows/{definition_id}/runs`; `FlowDefinitions.register_new` and
   `upgrade_builtin`. Not on main, and not needed: the A2 widgets (`x-sb-widget: entity`
   would replace Task 22's numbered enum labels), context blocks drawn on screen (Task 22
   also writes the line into the title and the field descriptions), the station view, and
   B2's `step()`. Task 22 adds one thing of its own: an `invoice_file` ref kind, so
   `agent()` can send the invoice's PDF or photo (R-P3-52). If #7361 changes `prompt()`'s
   signature before Task 22, Task 22 follows main.
10. **Kind names and ids** (R-P3-45, R-P3-56). Before Task 8, run on a copy of prod's
    catalog (or read only on prod, `deploy/CLAUDE.md` "Prod checks"):
    `SELECT name FROM filament_materials WHERE archived_at IS NULL`, `SELECT m.name, s.name
    FROM filament_subtypes s LEFT JOIN filament_materials m ON m.id = s.material_id WHERE
    s.archived_at IS NULL`, and the distinct `(material_id, subtype_id)` pairs of
    `filament_spool_types`. Task 8 handles every name: a kind id is `filament_` plus a slug
    of the material name (`[a-z0-9_]`, cut so the id fits 40 characters), and for a subtype
    the material kind's id plus `_` plus the subtype slug, cut the same way; a clash takes
    `_2`, `_3`. A name longer than 100 characters (the kind name cap) cannot exist: the
    catalog caps names at 100 (check `filament/models.py` and record it in the PR).
11. **The location rename's readers.** Task 1 moves every SQL string that names
    `filament_locations` or `filament_location_kinds` (the counts in "What exists on main")
    and `codes/resolve.py` `PREFIXES["L"]`. The Bambuddy sync (`bb_sync_plan.py`,
    `bb_sync_results.py`, `bambuddy_pull.py`) reads the new names in the same commit; its
    tests run in Task 1's step 4.

## Review Focus

1. An amend whose new header tax or shipping is below what the confirmed lines already hold:
   409 `amend_short`, nothing written; with "Re-spread over all lines" it is allowed. Tests
   in Task 2 and Task 5.
2. A link change on a confirmed expense line whose order is closed: the old cost entry is
   deleted and the new one written in one transaction, the order gets a
   `cost.invoice_line_changed` event, and a repeat of the same change writes nothing.
   Tests in Task 4.
3. A backfill line for goods that #7648's interim rule already received as a lot outside
   the invoice: the amend adopts the lot, `on_hand` does not change, and no second receive
   move is written. A line that received 11 for 10 billed spreads the price over 11 and
   shows 1 free. Tests in Task 3 and Task 5.
4. A spool that is archived, restored, retyped or deleted after the migration, or whose cost
   a person edits: its unit follows (`retired`, `in_stock`, the new type's item, gone) and
   `cost_minor` and `cost_per_kg` still agree. A typeless spool gets its unit when it gets a
   type. Test in Task 10.
5. A backfill amend on an invoice whose spools fed past orders: after "Re-spread over all
   lines", the spools' unit cost drops, and the unit re-cost lowers those orders' filament
   cost by the same share; an attempt on a spool with no cost falls back to the quote, then
   the price book, and says which basis priced it (R-P2-33). Tests in Task 5 and Task 12.
6. A spool type of material `PLA` and subtype `Silk` after Task 10: its item's kind is named
   `Silk` under `PLA` under `Filament`; `spool.material` and the 3DFP query still say `PLA`
   and `Silk`; Bambuddy's `ProfileMatch` still matches it. A subtype "for every material"
   used with two materials gives two kinds, each named `Silk`. Tests in Task 8 and Task 10.
7. An old pod during the rollout of layer A1 or A3 reads and writes `filament_locations`,
   `filament_suppliers`, `filament_supplier_products` and `filament_price_points` through the
   views, and a Bambuddy sync pass on the new pod plans the same suppliers and locations as
   before. Tests in Task 1 and Task 9.
8. The invoice flow (Task 22): a person who answers a line's prompt with a candidate that the
   flow did not offer gets 422 and the task stays open; a run cancelled at the final prompt
   leaves a confirmed invoice as it was and a draft with its proposed lines but nothing
   received; a second run on the same invoice while one holds its claim
   (`import_run_id`) is refused. Tests in Task 22.

---

## Phases and layers

Each layer is one PR on the one before, green alone. Phase 3a's PRs say `Part of #7068`
(the last: `Fixes #7068`). Phase 3b's say `Part of #<3b issue>` (the last: `Fixes #<3b
issue>`; layer B5 also `Fixes #7648`).

| Layer | Branch | Tasks | What it ships |
|---|---|---|---|
| A1 | `feat/7068-a1-purchasing-store` | 1-5 | Renames with views (suppliers, invoices, lines, locations, location kinds, shopping items); `adopt_ids`; links, events, billed and received; receive from a line; amend |
| A2 | `feat/7068-a2-purchasing-routes` | 6-7 | `/inventory/` routes for suppliers, invoices, lines, locations; purchases link lines; the review list; price sheets, price history, supplier stats, metrics, the cost calculator, the stock list and shopping for every item |
| A3 | `feat/7068-a3-filament-items` | 8-11 | The kind tree; spool types become item types and spools units; entities fold to `item`/`unit`; the catch-up; one confirm path; Bambuddy sees only filament |
| A4 | `feat/7068-a4-spool-job-cost` | 12 | `inventory_moves`; job cost at the unit's cost; re-cost; derived grams |
| A5 | `feat/7068-a5-purchasing-agent` | 13 | Generic purchasing, stock and location tools, with the `filament_*` aliases |
| A6 | `feat/7068-a6-docs` | 14 | Phase 3a docs |
| B1 | `feat/<3b>-b1-filament-in-inventory` | 15 | The Filament section moves into Inventory; names; every old URL redirects |
| B2 | `feat/<3b>-b2-item-types` | 16-17 | The Item types and Stock lists; the item type page at parity |
| B3 | `feat/<3b>-b3-purchasing-ui` | 18-21 | The invoice editor, suppliers, price sheets, metrics, shopping, panels and the review page for every item; the parity test |
| B4 | `feat/<3b>-b4-invoice-flow` | 22 | The built-in `invoice-import` flow with typed prompts |
| B5 | `feat/<3b>-b5-skill-docs` | 23-24 | The filament skill points to the flow (closes #7648); phase 3b docs |

Until phase 3b, the Filament pages keep working on phase 3a: the request fields `type_id`,
`proposed_type`, `supplier_product_id` and `update_spool_ids` are aliases (Task 11), and the
`/filament/` routes answer as their `/inventory/` twins (Tasks 6 and 7).

## Parity: every purchasing feature serves every item (owner, R-P3-58)

The owner's verdict (2026-11-25): "The invoice experience for filament was great, but the
implementation for the non spool is trash. We don't see invoice line items. We don't have
price sheets etc." And: "We don't have Item types". So phase 3 has a hard requirement: every
purchasing and catalog feature that filament has today serves every item, through the same
pages, routes and tools. Where filament shows a price per kg, every item shows a price per
unit (a unit item) or per pack and per unit of measure (a lot item); a filament item shows
per kg as well, as its kind's view. No feature exists for filament only, except the spool
workflow of R-P3-30's table (grams, ledger, trays, tags, labels, presets, K values,
temperatures, drying, 3DFP and slicer profiles, Bambu colour codes, the orders price book,
Bambuddy sync).

The list below is every filament purchasing and type feature on main (read from
`frontend/src/pages/filament/`, `frontend/src/components/filament/purchasing/`,
`frontend/src/components/filament/CostCalculator.tsx`, `api/filament_purchasing.py`,
`api/filament.py` l.2361-2400, `agent/src/tools/filamentPurchasing.ts` and
`agent/src/tools/filament.ts` l.1369-1440, at `cf8590f53`). "Task" names the task that
delivers the generic home; the parity test of each row is in that task, and Task 21 runs the
whole set on a filament item and a hardware item.

| # | Filament feature today (source) | Generic home: page / route / tool | Task |
|---|---|---|---|
| 1 | Types tab: list of spool types with code, colour, label g, newest price, price per kg, product, spool count, archived toggle (`TypesTab.tsx`) | "Item types" list `/inventory/types` (name, kind, manufacturer, newest price per pack and per unit, offers, stock on hand, archived toggle; a `kind` filter; the filament kind adds colour, label g and per kg columns) / `GET /inventory/items` / `inventory_item_list` | 16 |
| 2 | Spools tab: filters, status, table and cards views, columns shown, bulk bar (bulk edit, archive, move), CSV import, forecast (`SpoolsTab.tsx`, `BulkBar`, `SpoolImport`, `SpoolForecast`) | "Stock" list `/inventory/stock` (lots and units of every item; filters kind, type, status, location, supplier, invoice; table and cards; columns shown; bulk bar: move to a location, set status, archive, edit note; CSV import of stock; forecast from `use` moves); spools are its filament view / `GET /inventory/stock`, `POST /inventory/stock/bulk`, `POST /inventory/stock/import` / `inventory_stock_list`, `inventory_stock_bulk` | 7, 16 |
| 3 | Type page: identity (code, brand, material, subtype, colour), weights and price (label weight, cost per kg, `CostCalculator`, `PricePanel` with price history, sparkline, "Use as default cost"), reordering, spools of this type, files, links and properties (`TypePage.tsx`) | Item type page `/inventory/items/:id`: identity (name, kind path, manufacturer, MPN, GTIN and every property of the kind's schema), price (default unit cost, the cost calculator, the price panel with history per offer, sparkline, "Use as default cost"), reordering (`low_stock`, "Add to shopping list"), stock of this type (the Stock list filtered), linked invoices, files, links and properties. Filament items add the extension sections (temperatures, drying, slicer presets, K values, pressure advance, 3DFP) | 17 |
| 4 | Spool page "Bought on" and "Where to buy" (`SpoolPurchasing.tsx`) | Lot page and unit page: the invoice line it came from (billed, received, free, cost per unit) and each offer of its type with the newest price / `GET /inventory/items/{id}/prices` | 7, 20 |
| 5 | Suppliers tab: name, customer number, website, products, spend, spools bought, archived (`SuppliersTab.tsx`) | `/inventory/suppliers`: the same columns, "units bought" for every item and "spools bought" in the filament view / `GET /inventory/suppliers`, `GET /inventory/suppliers/stats` / `supplier_list`, `supplier_stats` | 6, 7, 19 |
| 6 | Supplier page: products table (SKU, type, pack, page, newest price, add price), add product sheet, price sheet, files and links; non-filament offers shown apart as "Other offers" (`SupplierPage.tsx`, `ProductsTable`, `AddProductSheet`, `PriceSheet`) | `/inventory/suppliers/:id`: ONE offers table for every item (SKU, item type, pack size and unit, page, newest price per pack and per unit, add price), one "Add offer" sheet with an item type search, one price sheet; "Other offers" goes / `GET /inventory/suppliers/{id}/offers`, `GET /inventory/suppliers/{id}/price-sheet` / `inventory_offer_list`, `supplier_price_sheet` | 7, 19 |
| 7 | Price points with history; add and delete a price (`AddPriceForm`, `PriceCell`, `Sparkline`) | Offer price history for every offer; add and delete / `GET\|POST /inventory/offers/{id}/prices`, `DELETE /inventory/offer-prices/{price_id}` / `inventory_offer_price_list`, `inventory_offer_price_add`, `inventory_offer_price_delete` | 7, 19 |
| 8 | A type's prices at every supplier (`GET /filament/types/{id}/prices`, `type_prices`) | `GET /inventory/items/{id}/prices` (per pack, per unit; per kg for filament) | 7 |
| 9 | Invoices tab: filter by supplier, status, has file; spools made or filled; total (`InvoicesTab.tsx`) | `/inventory/invoices`: the same filters; "stock received" (lots and units) for every line / `GET /inventory/invoices` / `invoice_list` | 6, 18 |
| 10 | Invoice page: header form, lines table (description, SKU, type, product, quantity, money, the server's figures), candidates and the spool match picker ("fills existing spools"), totals, confirm, file preview and upload, links to the spools made, line anchors (`InvoicePage.tsx`, `InvoiceLinesTable`, `SpoolMatchPicker`, `InvoiceTotals`, `InvoiceFile`, `InvoicePhoto`, `InvoiceUpload`) | `/inventory/invoices/:id`: one line editor for every item: item type search, offer, kind, pack, quantity, received; candidates by SKU, MPN, name (and colour for filament); "Adopts existing stock" picker for lots and units; totals; confirm; file preview; every confirmed line shows its item type, what it received (lots or units, billed against received, free) with links, and its entity links. A hardware line looks and works as a filament line does | 5, 6, 18 |
| 11 | Invoice import with the assistant (`InvoiceImport.tsx`, `POST /api/v1/ai/filament/invoice-import`) | The `invoice-import` flow, for every item, started from the invoice page; the chat route stays for ad-hoc use | 22 |
| 12 | Purchasing metrics: price per kg over time per type, spend by supplier and month (`MetricsTab.tsx`, `GET /filament/metrics/purchasing`, `filament_purchasing_metrics`) | `/inventory/metrics` purchasing section: price per unit over time per item type (per kg in the filament view), spend by supplier and month / `GET /inventory/metrics/purchasing` / `purchasing_metrics` | 7, 19 |
| 13 | Cost per kg calculator (`CostCalculator.tsx`, `GET /filament/cost-per-kg`, `filament_cost_per_kg`) | Cost per unit calculator: price for a quantity of packs, pack size, tax, shipping → cost per unit and price per unit (and per kg with a net weight) / `GET /inventory/cost-per-unit` / `inventory_cost_per_unit`; the filament route and tool stay as its per-kg view | 7, 17 |
| 14 | Shopping list: type, material, subtype, brand, colour, spools, pending or purchased, note, free-text product (`ShoppingListTab.tsx`, `filament_shopping_items`, `filament_shopping_*`) | `/inventory/shopping-list` for every item: item type (any kind) or free text, supplier, offer, quantity in the item's unit (packs shown with the offer's pack size), pending or purchased, note; "Add to shopping list" on every item type page and on a low-stock row; the filament view keeps material, subtype, brand and colour / table `shopping_items` (renamed, Task 1), `/inventory/shopping-list` routes / `shopping_list`, `shopping_add`, `shopping_update`, `shopping_remove` | 1, 7, 19 |
| 15 | Price sheet compare: newest against previous price per pack, change chip (`PriceSheet.tsx`, `filament_price_sheet`) | The supplier price sheet for every offer, change per pack and per unit (per kg for filament) | 7, 19 |
| 16 | Supplier products CRUD (`filament_product_list`, `filament_product_upsert`) | Offers (`inventory_offer_list`, `inventory_offer_upsert`, phase 2); the filament tools become aliases | 13 |
| 17 | Supplier stats (`GET /filament/suppliers/stats`, `filament_supplier_stats`) | `GET /inventory/suppliers/stats` / `supplier_stats` (spend, lines, units bought per supplier and currency) | 7, 13 |
| 18 | Catalog tab: brands, materials, subtypes, colours, inserts (`CatalogTab.tsx`, `filament_catalog_*`) | Materials and subtypes are kinds, edited on `/inventory/kinds` (R-P3-45); the rest stays the filament kind's catalog under `/inventory/filament/catalog`. A generic item catalog (a manufacturer list and a product lookup new item types come from) is not in phase 3: follow-up issue (R-P3-60) | 8, 15 |
| 19 | Locations tab and page (`LocationsTab.tsx`, `LocationPage.tsx`, `filament_location_*`) | `/inventory/locations`, `/inventory/locations/:id` for every item's stock / `/inventory/locations` routes / `location_*` tools | 6, 13, 15 |

Rows 1, 2 and 3 are the owner's "Item types" (R-P3-59): an item is already the type of its
lots and units, as a spool type is of its spools; the UI shows that split exactly as the
Filament section shows Types against Spools.

## Phase 3a — the data model (#7068)

## Layer A1 — `feat/7068-a1-purchasing-store` (from main)

### Task 1: The purchasing schema migration and the renames

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_purchasing_core.sql`
- Modify: every SQL string that names a renamed table (Pre-flight 2; `grep -rln
  "filament_suppliers\|filament_invoices\|filament_invoice_lines" backend/scadbuddy
  backend/tests`), `backend/scadbuddy/entities/registry.py` (`supplier` on `suppliers`,
  `invoice` on `invoices`, `location` on `locations`, `location_kind` on `location_kinds`,
  new `invoice_line` on `invoice_lines`, uuid), every SQL string that names
  `filament_locations`, `filament_location_kinds` or `filament_shopping_items` (Pre-flight
  11), `backend/scadbuddy/codes/resolve.py` (`PREFIXES["L"]` on `locations`)
- Test: `backend/tests/inventory/test_purchasing_schema.py`, `backend/tests/test_pg_migrations.py`,
  `backend/tests/entities/test_registry.py`

The migration, in one file (a top comment cites #7068 and spec §6.5, §6.7):
- `ALTER TABLE filament_suppliers RENAME TO suppliers`, `filament_invoices` → `invoices`,
  `filament_invoice_lines` → `invoice_lines`, `filament_locations` → `locations`,
  `filament_location_kinds` → `location_kinds`, `filament_shopping_items` →
  `shopping_items` (R-P3-49); rename their indexes to the new prefix. Then `CREATE VIEW
  filament_suppliers AS SELECT * FROM suppliers` and the same for the other five, with a
  comment naming the follow-up that drops them (R-P3-1). Each is a plain `SELECT *` over
  one table, so Postgres updates through it (R-P3-63); the old pods' statement forms are
  tested through it in step 1. The views come last in the file, after every column change
  below (a view's `*` is fixed when it is made).
- `invoices ADD COLUMN amended_at timestamptz`, `lines_review text CHECK (lines_review IN
  ('flagged','done'))` (R-P3-32).
- `invoice_lines ADD COLUMN item_id uuid REFERENCES items (id)`, `offer_id uuid REFERENCES
  supplier_offers (id)`, `kind text NOT NULL DEFAULT 'stock' CHECK (kind IN
  ('stock','expense','asset'))`, `pack_size numeric(20,6) NOT NULL DEFAULT 1 CHECK (pack_size
  > 0)`, `received_units numeric(20,6) CHECK (received_units > 0)` (NULL: as billed; layer A3's
  migration and catch-up fill the old rows, R-P3-37, R-P3-63), `proposed_item jsonb`, `CHECK (proposed_type IS NULL OR proposed_item IS NULL)`.
  `RENAME COLUMN update_spool_ids TO adopt_ids`, drop its CHECK `cardinality(update_spool_ids)
  <= quantity` (`20261117T2042Z_filament_suppliers.sql:99`) and add `CHECK
  (cardinality(adopt_ids) <= 1000)`; `receive_line` refuses more adopted than received
  (R-P3-62). The `filament_invoice_lines` view lists every column of `invoice_lines` by
  name with `adopt_ids AS update_spool_ids` in its place (still one plain reference per
  column, so the view stays updatable), so old pods read and write the old name.
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
  IN ('confirmed','amended','respread','links_changed','lines_review')), detail jsonb NOT
  NULL, actor jsonb NOT NULL)`; index `(invoice_id, at)` (R-P3-13).
- `order_item_sources ADD COLUMN invoice_line_id uuid REFERENCES invoice_lines (id)`.
- `filament_spool_events ADD COLUMN cost_per_kg_at numeric(12,4) CHECK (cost_per_kg_at >= 0)`.
- `filament_spool_types ADD COLUMN item_id uuid REFERENCES items (id), ADD CHECK (item_id IS
  NULL OR item_id = id)`; `filament_spools ADD COLUMN unit_id uuid REFERENCES stock_units
  (id), ADD CHECK (unit_id IS NULL OR unit_id = id)` (R-P3-3).
- `shopping_items ADD COLUMN item_id uuid REFERENCES items (id)`, `supplier_id uuid
  REFERENCES suppliers (id)`, `offer_id uuid REFERENCES supplier_offers (id)`, `quantity
  numeric(20,6) CHECK (quantity > 0)` (NULL reads as `quantity_spools`, which old pods
  still write), `product text CHECK (length(product) <= 200)` (free text), `ALTER COLUMN
  material DROP NOT NULL`, `CHECK (type_id IS NOT NULL OR item_id IS NOT NULL OR material IS
  NOT NULL OR product IS NOT NULL)` (R-P3-19, parity row 14).
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
    default is 1; `received_units = 0` raises `CheckViolation`.
  - `test_old_pod_statements_work_through_the_views`: for each renamed table, the
    statement forms the code at `cf8590f53` uses on it (grep: `INSERT … RETURNING`,
    `INSERT … ON CONFLICT`, `UPDATE`, `DELETE`, `SELECT … FOR UPDATE`) run through the old
    name and land in the new table; `UPDATE filament_invoice_lines SET update_spool_ids =
    …` writes `adopt_ids`.
  - `test_adopt_ids_takes_more_than_quantity`: a line of quantity 1, `pack_size` 10, with
    2 ids in `adopt_ids` inserts; 1001 ids raise `CheckViolation`.
  - `test_registry.py`: `KINDS["invoice_line"].table == "invoice_lines"`,
    `KINDS["location"].table == "locations"`; `GET /codes/L-<n>` resolves on `locations`.
  - the Bambuddy sync tests (`tests/filament/test_bb_sync_plan.py`,
    `test_bambuddy_pull.py`) pass unchanged.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_purchasing_schema.py tests/entities/test_registry.py -q` — Expected: FAIL.
- [ ] **Step 3: Write the migration; move every SQL name.** No behavior changes in this step.
- [ ] **Step 4: Run** the same plus `uv run --frozen pytest tests/filament tests/inventory tests/codes tests/printers tests/test_pg_migrations.py -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): generic purchasing tables, links and events (#7068, 3a 1/6)`

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
- [ ] **Step 5: Commit** `feat(inventory): amend arithmetic spreads only what no line holds (#7068, 3a 1/6)`

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
  shop_currency, extensions: Mapping[Tracking, UnitExtension] = {})`. Layer A3 registers the
  filament one (Task 10). `ReceiveUnits` gains `unit_ids: list[UUID] | None` (ids the caller
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
  - `adopt_in(conn, item_id, ids: Sequence[UUID], unit_cost_minor: Decimal, by, *,
    invoice_line_id: UUID) -> tuple[list[Lot], list[Unit]]`: lots and units of the item
    received earlier with no invoice line take the line and the cost (today's
    `update_spool_ids`, #6700, #6707, now for every item, and #7648's interim receipts,
    R-P3-30, R-P3-43). It writes no move, so `on_hand` does not change. A lot or unit on
    another line, of another item, or listed twice is refused.
  - `adoptable(conn, item_id, *, near: date, note_has: Sequence[str] = ()) -> list[Lot |
    Unit]`: the item's lots and units with no invoice line, those whose note holds every
    term of `note_has` (case-insensitive) first,
    then by distance of `received_at` from `near`, at most 30 days away (R-P3-43).
  - `set_lot_cost_in(conn, lot_id, unit_cost_minor, by) -> Lot`;
    `set_unit_cost_in(conn, unit_id, cost_minor: Decimal | None, by) -> Unit` (None sets
    `cost_unknown`; no move changes, D14; calls the extension's `cost_set`). It is the one
    writer of a unit's cost (R-P3-6).
  - `set_unit_status_in(conn, unit_id, body: UnitStatusInput, by) -> Unit` (`set_unit_status`
    in the caller's transaction; calls the extension's `status_set`).
  - `recost_unit(conn, unit_id) -> RecostResult`: the unit's `use`/`return` moves at its
    cost now; for a kind with an extension, the extension's re-snapshot too (Task 12).
  - `of_line(conn, line_id: UUID) -> tuple[list[Lot], list[Unit]]`.
- Produces (`OfferStore`): `match_in(conn, supplier_id, item_id, sku) -> Offer | None`
  (unarchived, `upper(sku)`); `by_sku_in(conn, supplier_id, sku) -> list[Offer]`;
  `create_in(conn, body: OfferInput, by) -> Offer`; `add_price_in(conn, offer_id, *, price:
  Decimal, currency: str, on_date: date, invoice_line_id: UUID) -> OfferPricePoint`
  (`source = 'invoice'`); `set_line_price_in(conn, invoice_line_id, price) -> None`
  (respread, R-P3-12).
- Produces (`ItemStore`): `create_in(conn, body: ItemInput, by) -> Item`.
- Produces (`ledger.record`): an event with `ref_kind` in `('print_run','bb_usage')` stores
  `cost_per_kg_at` = the spool's `cost_per_kg`, else its type's (spec §6.6, D7). Layer A3
  makes the spool's `cost_per_kg` the per-kg form of its unit's cost (R-P3-6, R-P3-64).

- [ ] **Step 1: Write the failing tests**:
  - `test_a_receive_names_its_invoice_line`: `receive_in(…, origin="invoice_line",
    invoice_line_id=L)` → lot `origin == "invoice_line"`, `invoice_line_id == L`;
    `of_line(conn, L)` returns it.
  - `test_units_received_in_service_write_a_use_move`: 2 units at 3000, `in_service=True` →
    both `in_service`, two `use` moves of −1 @ 3000.
  - `test_a_received_unit_calls_its_kinds_extension`: a fake extension registered for
    `unit` records one `received` call with the two units; `set_unit_cost_in` calls
    `cost_set`; a lot item calls nothing.
  - `test_adopt_in_takes_the_line`: a unit received by hand at 0 cost-unknown takes
    line `L` and 3000; a unit on another line is refused and nothing is written.
  - `test_an_adopted_lot_keeps_its_on_hand`: a lot of 100 received by hand on 2026-11-02
    with note "Amazon order 112-4455", 40 used, so `on_hand == 60`; `adopt_in(…, [lot],
    Decimal("15"), invoice_line_id=L)` → `invoice_line_id == L`, `unit_cost_minor == 15`,
    `on_hand == 60`, and the lot's move count is unchanged; `adoptable(item, near=2026-11-01,
    note_has=["Amazon", "112-4455"])` lists it first; with `["Acme", "112-4455"]` it is listed
    after the note matches, by date.
  - `test_set_unit_cost_in_moves_no_past_move`; `test_recost_unit_names_the_order_items`.
  - `test_match_in_finds_the_offer_of_one_item_in_a_bundle`: one SKU, two items → `match_in`
    with item A returns A's offer; `by_sku_in` returns both (D13).
  - `test_a_print_event_snapshots_the_spool_cost`: spool `cost_per_kg` 25.0000 →
    `print_started` event `cost_per_kg_at == 25.0000`; a spool with none, type 20.0000 →
    20.0000; a `weighed` event → NULL.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_stock.py tests/inventory/test_offers.py tests/filament/test_ledger.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Every lot insert still takes `_open_lock` first (R-P2-32).
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/inventory tests/filament -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): receive from an invoice line; unit extensions (#7068, 3a 1/6)`

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
  `set_entry_amount(conn, order: Order, entry_id: str, amount_minor: int, by: Actor)`
  (updates the row in place, keeps its id, and inserts the order event
  `cost.invoice_line_changed` with `line_id`, `before_minor`, `after_minor`; no event when
  the amount is unchanged), `drop_entry(conn, order: Order, entry_id: str, by: Actor)`
  (deletes it, sets `order_item_sources.cost_entry_id` that names it to NULL, and inserts
  the event with `after_minor` NULL) (R-P3-61).
- Produces (`inventory/invoice_links.py`): `class InvoiceLinkStore(pool, entities, orders:
  OrderLineCosts)`: `for_line(line_id) -> list[LineLink]`, `for_entity(ref) ->
  list[LinkedLine]`, `add(line_id, body, by) -> LineLink`, `update(link_id, patch, by) ->
  LineLink`, `delete(link_id, by) -> None`, `rewrite_in(conn, line_id, by) -> None`.
  `rewrite_in` computes each link's amount (when the line is a confirmed `expense` line,
  R-P3-9: `allocated_cost` shared by `share` with `invoice_money.spread`; otherwise none),
  then changes only what moved (R-P3-61): a link whose consumer exists for the same target
  gets its amount updated in place (`set_entry_amount`, or `consumables` on the
  maintenance row), keeping the id; a link with no consumer gets one; a consumer whose link
  is gone, whose line is no longer a confirmed expense line, or whose share rounds to 0 is
  deleted (`drop_entry`). Where a purchase's entry must be replaced (its order changed),
  the new entry's id goes into `order_item_sources.cost_entry_id` in the same
  transaction. The consumer of each target is:
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
    share to 2 → the order's entry keeps its id and reads 4500, the maintenance event keeps
    its id and reads consumables 45.0000; `order_events` has `cost.invoice_line_changed` with `before_minor == 6000`,
    `after_minor == 4500`; a repeat of the same PATCH writes no new row.
  - `test_a_purchase_entry_survives_a_share_change` (R-P3-61): an `order_item_sources` row of
    `o1` whose `cost_entry_id` names the line's entry (seeded directly; Task 6 adds the
    route that records it); change the printer's share, then amend the invoice with
    a respread → `order_item_sources.cost_entry_id` still names an existing
    `order_cost_entries` row with the new amount, and the order has one entry for the
    line.
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
- [ ] **Step 5: Commit** `feat(inventory): invoice line links with exact reversal (#7068, 3a 1/6)`

### Task 5: One confirm path for item lines; amend

**Files:**
- Create: `backend/scadbuddy/inventory/invoice_lines.py`
- Modify: `backend/scadbuddy/filament/invoices.py` (`InvoiceStore`: `confirm` sends every
  line with an item to `receive_line`; `amend`; `candidates`; `events`; `set_lines_review`;
  `set_proposal`), `backend/scadbuddy/filament/purchasing_models.py`
- Test: `backend/tests/filament/test_invoice_confirm.py`, `backend/tests/inventory/test_invoice_lines.py`,
  `backend/tests/filament/test_invoice_amend.py`, `backend/tests/filament/test_invoice_candidates.py`

In layers A1 and A2 a filament line (`type_id` or `proposed_type`) still takes today's
`_confirm_line`, because no filament item exists yet. Task 11 deletes that branch: from
layer A3 on, `receive_line` is the one path (R-P3-30).

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces (`purchasing_models.py`): `LineKind = Literal["stock", "expense", "asset"]`;
  `InvoiceLineIn` gains `item_id: UUID | None`, `offer_id: UUID | None`, `kind: LineKind =
  "stock"`, `pack_size: Decimal | None` (None takes the offer's, else 1),
  `received_units: Decimal | None` (None: as billed), `adopt_ids: list[UUID]`,
  `proposed_item: ItemInput | None`. `proposed_type` and `proposed_item` are exclusive (422).
  `InvoiceLine` gains the same fields plus `lot_ids`, `unit_ids`, `links: list[LineLink]`,
  and the computed `billed_units` (`quantity × pack_size`), `free_units` (`received_units −
  billed_units`; negative is a short delivery), `cost_per_unit` (`allocated_cost /
  received_units`, 4 places); for a filament item (from layer A3, Task 11) also `billed_g` (`billed_units × label
  weight`), `received_g` (Σ the spools' `received_net_g`, None until every spool is
  weighed), `received_g_source` (`weighed`, or `derived` when any spool's is), `weighed_spools`, `free_g` and `cost_per_kg` (`allocated_cost / received_g ×
  1000`, else over `billed_g`) (R-P3-37, R-P3-39, R-P3-42). `ItemCandidate` gains
  `adoptable: list[AdoptableStock]` (`AdoptableStock(kind: Literal["lot","unit"], id,
  received_at, qty, note, note_match: bool, ui_path)`).
  `ItemCandidate(item_id, name, kind_id, matched_by: Literal["sku", "mpn", "name",
  "colour"], offer_id)`; `LineCandidates.items: list[ItemCandidate]`. `InvoiceAmend(subtotal,
  tax, shipping, total, lines: list[InvoiceLineIn] (1..200), respread: bool = False)`;
  `AllocationChange(position, before: Decimal | None, after: Decimal)`;
  `RecostTarget(kind: Literal["lot", "unit"], id: UUID, ui_path: str)`;
  `AmendResult(invoice: InvoiceDetail, changes: list[AllocationChange], recost:
  list[RecostTarget])`; `InvoiceEvent(id, at, kind, detail, actor)`;
  `LinesReview = Literal["flagged", "done"]`; `InvoiceDetail` gains `lines_review`.
- Produces (`inventory/invoice_lines.py`): `receive_line(conn, head: Row, line: Row, by:
  Actor, *, shop: str, stores: InventoryComponent) -> None`. One rule for every item (R-P3-7,
  R-P3-8, R-P3-30):
  - the item is `line.item_id`, else one made from `proposed_item` by `ItemStore.create_in`
    (a name clash is `InvoiceLineError` "an item like '<name>' exists; choose it", R-P3-22);
  - the offer is `line.offer_id` (this supplier's, this item's, unarchived), else
    `match_in(supplier, item, sku or description)`, else a new one (`sku` = the line's SKU,
    else its description, at most 128). With no item named and a SKU that covers two or more
    items (`by_sku_in`), the line fails and names them (R-P3-23);
  - billed = `quantity × line.pack_size`; received = `line.received_units`, else billed
    (R-P3-37). The cost per received unit is `allocated_cost × 10^exp / received`, so the
    price paid is spread over everything received and the total stays the price paid. The
    line's `adopt_ids` (lots or units already received outside the invoice) are adopted
    first (`adopt_in`) and count toward received; then `stock` or `asset` on a `lot` item
    receives one lot of what is left; on a `unit` or `filament` item, `received − adopted`
    more units (`receive_units_in`, at most 100, else a line error), each at that cost,
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
  (`set_unit_cost_in`, spools included from layer A3) and price point (`set_line_price_in`)
  take the new cost, the cost per unit being the new `allocated_cost` over the line's
  `received_units`, and become `recost` targets. New lines may adopt lots and units
  (`adopt_ids`), as at confirm. The reconcile check runs; the new
  lines are confirmed; every line's links are rewritten; one `invoice_events` row (`amended`
  or `respread`) holds each line's allocation before and after (spec §6.7); `lines_review`
  becomes `done`. `preview` raises a private
  sentinel at the end so the transaction rolls back, and returns what it would have
  written (R-P3-12). `events(invoice_id) -> list[InvoiceEvent]`. `confirm` writes a
  `confirmed` event. `set_lines_review(invoice_id, value: LinesReview | None, *, actor)` works on a confirmed
  invoice only and writes an `invoice_events` row (R-P3-32).
- Produces (`InvoiceStore.candidates`): one path for every line (R-P3-24): items whose offer
  at this supplier has the line's SKU (`sku`), whose `mpn` property equals the SKU (`mpn`),
  whose name equals or is inside the description (`name`); and, per item candidate of a
  unit or filament kind, its units with no invoice line (today's spool candidates, archived
  ones marked), and for every item candidate its `adoptable` lots and units (`adoptable`,
  `near` = the invoice date, `note_has` = the supplier's name and the invoice number, which
  the interim rule writes in the lot's note). A kind's extension may add a matcher: the filament one adds `colour` (the
  line's colour code or proposed type's material and RGBA, today's `_line_key`).

- [ ] **Step 1: Write the failing tests**:
  - `test_a_lot_line_receives_one_lot` (`test_invoice_lines.py`): a `hardware` item, an offer
    with `pack_size` 100, line quantity 2 (`pack_size` defaults to 100), `allocated_cost`
    30.00 (USD, exponent 2) → one lot, `received_qty == 200`, `unit_cost_minor ==
    Decimal("15")`, `origin == "invoice_line"`; one price point `15.0000`.
  - `test_a_unit_line_receives_units`: a `nozzle` item, no offer, quantity 3, allocated
    90.00 → 3 units at `cost_minor == 3000`; a new offer with the line's SKU; 101 units → a
    line error.
  - `test_a_unit_line_fills_units_first`: a hand-received nozzle unit in `adopt_ids`,
    quantity 2 → that unit takes the line and one new unit is received.
  - `test_received_beyond_billed_is_free`: a `tool` line, quantity 10, `received_units` 11,
    `allocated_cost` 110.00 → 11 units at `cost_minor == 1000`, `free_units == 1`,
    `cost_per_unit == Decimal("10.0000")`; a lot line of 2 packs of 100 with
    `received_units` 210 and allocated 30.00 → one lot of 210 at `unit_cost_minor ==
    Decimal("14.285714")`, `free_units == 10`; `received_units` 9 of 10 billed →
    `free_units == -1`, cost per unit `12.2222`.
  - `test_an_asset_line_puts_units_in_service`.
  - `test_an_expense_line_receives_nothing_and_feeds_its_links`: a `service` item, link to
    `order:o1` → no lot, a cost entry of the whole `allocated_cost`.
  - `test_a_service_on_a_stock_line_is_refused`: `InvoiceLineError` naming the line; nothing
    written.
  - `test_a_bundle_sku_with_no_item_is_refused`: the message names both items.
  - `test_a_proposed_item_is_made_at_confirm`; a clash → line error.
  - `test_filament_lines_confirm_as_before`: the existing `test_invoice_confirm.py` cases pass unchanged.
  - `test_invoice_amend.py`: `test_an_amend_keeps_the_old_allocation` (the Task 2 receipt: the
    filament line keeps 171.12 and its spools' `cost_per_kg`; the new nozzle line gets
    5063.16 and its units); `test_an_amend_short_writes_nothing` (Review Focus 1);
    `test_a_respread_names_what_to_recost`: a lot and a unit whose cost moved are `recost`
    targets and their costs changed; `test_a_preview_writes_nothing`; one `invoice_events`
    row per amend with before and after; the amend sets `lines_review == "done"`; `test_an_amend_adopts_an_early_lot` (Review Focus 3): the lot
    of `test_an_adopted_lot_keeps_its_on_hand` on a new `consumable` line with `adopt_ids`
    → `on_hand` stays 60, no new lot, no `receive` move, `of_line` returns the lot.
  - `test_candidates_propose_items` (`test_invoice_candidates.py`): the SKU matches an offer
    (`matched_by == "sku"`); an item whose property `mpn` equals the SKU (`"mpn"`); an item
    named as the description (`"name"`), in that order; a nozzle candidate lists its unit
    with no invoice line.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_invoice_lines.py tests/filament/test_invoice_confirm.py tests/filament/test_invoice_amend.py tests/filament/test_invoice_candidates.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/filament tests/inventory tests/orders -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): invoice lines of any kind receive stock; amend (#7068, 3a 1/6)`

## Layer A2 — `feat/7068-a2-purchasing-routes` (on A1)

### Task 6: Routes; order purchases link lines; the backfill list

**Files:**
- Create: `backend/scadbuddy/api/purchasing.py`
- Modify: `backend/scadbuddy/api/filament_purchasing.py` and `api/filament.py` (the `/filament/suppliers`
  CRUD, `/filament/invoices…` and `/filament/locations…` routes call the same handlers; each description says "Alias of
  /inventory/…; goes after one release", spec §6.7; the product, price and price-sheet routes
  become aliases of Task 7's offer routes, R-P3-20, R-P3-58), `backend/scadbuddy/inventory/problems.py`, `backend/scadbuddy/main.py`,
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
    `GET /inventory/invoices/{invoice_id}/candidates`, `POST
    /inventory/invoices/{invoice_id}/candidates` (body `{lines: [InvoiceLineIn]}`: the same
    path for lines not stored yet, which Task 22's flow sends),
    `GET /inventory/invoices/{invoice_id}/events`, `POST|GET /inventory/invoices/{invoice_id}/file`.
  - `GET|POST /inventory/locations`, `GET|PATCH /inventory/locations/{location_id}`,
    `GET|POST /inventory/location-kinds`, `PATCH /inventory/location-kinds/{kind_id}`; the
    `/filament/locations…` and `/filament/location-kinds…` routes call the same handlers
    and say "Alias of /inventory/…; goes after one release" (R-P3-49).
  - `GET /inventory/invoices/review?all=` → `{invoices: InvoiceReview[]}` (R-P3-31).
  - `GET|POST /inventory/invoice-lines/{line_id}/links`, `PATCH|DELETE
    /inventory/invoice-line-links/{link_id}`, `GET /entities/{ref}/invoice-lines` →
    `{lines: LinkedLine[]}`.
  - Problems: 404 `invoice_line_not_found`, `link_not_found`; 409 `amend_short`,
    `link_exists`, `line_linked_twice`, `currency_mismatch`, `invoice_not_confirmed`,
    `line_not_expense`; the invoice problems keep their codes.
- Produces (`InvoiceStore.review_list(*, all: bool = False) -> list[InvoiceReview]`,
  R-P3-31): `InvoiceReview(invoice: Invoice, signals: list[ReviewSignal], ui_path)`, with
  `ReviewSignal = Literal["subtotal_differs", "tax_differs", "not_filament_note", "flagged",
  "has_file", "imported", "has_note"]`. Each is a fact the data holds: `subtotal_differs` = `|subtotal −
  Σ line_subtotal| > 0.05` (`RECONCILE_TOLERANCE`; confirm never compared them);
  `tax_differs` = `|tax − Σ line_tax| > 0.05`; `not_filament_note` = the note starts with
  `Not filament:` (the interim #7648 rule, PR #7704: the header took the filament lines'
  share so the invoice reconciled, and the note holds the printed totals and each other
  line with its description, quantity, subtotal and the inventory item it went to);
  `flagged` = `lines_review = 'flagged'`;
  `has_file` = an invoice file is stored; `imported` = `import_run_id` is set; `has_note` =
  a note is set (spec §6.7: the Micro Center invoice kept its full totals there). The list
  holds confirmed invoices with `lines_review` not `done` and at least one of the first
  four signals; with `all`, every confirmed invoice not `done`. Newest first.
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
  stores it as `type_id` too (Task 9 makes the ids equal).

- [ ] **Step 1: Write the failing tests** with the app `client` fixture: one round trip per
  family; each problem code once; `POST …/amend?preview=true` answers the allocation and a
  second `GET` shows no new line; `POST …/candidates` with an unstored line answers as
  `GET` does for the same stored line; every `/filament/locations…` call answers as its
  `/inventory/locations…` twin; `GET /entities/order:o1/invoice-lines` lists the line; every
  `/filament/invoices…` and `/filament/suppliers…` call answers as its `/inventory/…` twin;
  `POST /orders/{id}/purchases/{source}/record` with an `invoice_line_id` sets
  `cost_entry_id` to the `invoice_line:<id>` entry and the order has one entry for it; a
  draft line → 409 `invoice_not_confirmed`; a NUL in a supplier name → 422 (R-P2-16).
  `test_review_list_uses_the_signals` (`test_invoices.py`): a confirmed invoice with header
  subtotal 4900.00 over lines of 160.00 → `subtotal_differs`, `has_note`; one whose
  subtotal and tax match its lines → absent, and present with `all=true`; a person's flag →
  `flagged`; a note `Not filament: printed total 5234.28; AMS 2 Pro x1 …` with matching
  header and lines → `not_filament_note`; `lines_review = 'done'` → absent even with
  `all=true`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_purchasing.py tests/api/test_filament_purchasing.py tests/orders/test_purchases.py tests/filament/test_shopping.py tests/filament/test_invoices.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** the routes and the coverage entries (reason: "Inventory phase 3
  (#7068, spec §6.5, §6.7); its `invoice_*`, `supplier_*` and `location_*` tools follow in layer A5 of the
  same stack, which drops these entries").
- [ ] **Step 4: Run** the tests, `uv run --frozen pytest tests/auth -q`, and `cd agent && pnpm exec vitest run test/coverage.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): supplier, invoice, amend, review and link routes (#7068, 3a 2/6)`

### Task 7: Price sheets, price history, supplier stats, metrics, the calculator, the stock list and shopping for every item

Parity rows 2, 4-8, 12-15, 17 (R-P3-58). Every route reads `supplier_offers` and
`offer_price_points` of every item; before layer A3 filament still has its own tables, so a
filament row joins in through Task 11 and the filament routes keep serving it until then.

**Files:**
- Create: `backend/scadbuddy/inventory/purchasing_reads.py` (`PurchasingReads`, one store,
  SQL only here), `backend/scadbuddy/inventory/stock_list.py` (`StockList`)
- Modify: `backend/scadbuddy/api/purchasing.py`, `backend/scadbuddy/api/inventory_stock.py`,
  `backend/scadbuddy/inventory/models.py`, `backend/scadbuddy/inventory/offers.py`
  (`delete_price`), `backend/scadbuddy/filament/shopping.py` (generic rows),
  `backend/scadbuddy/filament/invoice_money.py` (`cost_per_unit`)
- Test: `backend/tests/inventory/test_purchasing_reads.py`, `test_stock_list.py`,
  `backend/tests/api/test_purchasing.py`, `backend/tests/filament/test_invoice_money.py`

**Interfaces:**
- Produces (`inventory/models.py`): `PricePer(pack: Decimal, unit: Decimal, kg: Decimal |
  None)` (`unit` = pack price over `offer.pack_size` in the offer's `pack_uom`; `kg` only
  for a filament item, over its label weight); `PriceHistoryRow(price_id, on_date, price,
  currency, source, invoice_line_id, per: PricePer)`; `OfferPrices(offer: Offer,
  supplier_name, history: list[PriceHistoryRow])`; `SheetRow(offer_id, item_id, item_name,
  kind_id, sku, newest: PriceHistoryRow | None, previous: PriceHistoryRow | None,
  change_pct: Decimal | None)` (the change of the per-unit price, per kg for filament,
  `ChangeChip`'s input); `SupplierStats(supplier_id, currency, spend, invoices, lines,
  units_bought, offers)`; `PurchasingMetrics(prices: list[PriceSeries], spend:
  list[SpendRow])`, `PriceSeries(item_id, item_name, currency, basis: Literal["unit",
  "kg"], points: list[tuple[date, Decimal]])`, `SpendRow(supplier_id, month, currency,
  total, invoices)`; `CostPerUnit(cost_per_unit, price_per_unit, cost_per_kg: Decimal |
  None, price_per_kg: Decimal | None)`; `StockRow(kind: Literal["lot", "unit"], id,
  item_id, item_name, kind_id, status, qty, uom, location_id, supplier_id, invoice_line_id,
  received_at, cost_per_unit, note, ui_path)`; `StockBulk(ids: list[StockRef] (1..500),
  action: Literal["move", "status", "archive", "note"], location_id, status, note)`.
- Produces (`PurchasingReads(pool)`): `item_prices(item_id) -> list[OfferPrices]`,
  `offer_prices(offer_id) -> OfferPrices`, `price_sheet(supplier_id) -> list[SheetRow]`,
  `supplier_stats() -> list[SupplierStats]`, `metrics(since: date, until: date) ->
  PurchasingMetrics` (since after until → `ValueError` → 422, as the filament route).
- Produces (`invoice_money.cost_per_unit(price: Decimal, quantity: int, pack_size:
  Decimal, tax: Decimal, shipping: Decimal, net_weight_g: Decimal | None) ->
  CostPerUnit`): `cost_per_unit` with tax and shipping, `price_per_unit` without shipping,
  as `/filament/cost-per-kg` does per kg; the filament route calls it with `pack_size` 1.
- Produces (`StockList(pool)`): `list(*, kind: str | None, item_id, status, location_id,
  supplier_id, invoice_id, q, archived: bool, limit ≤ 500, cursor) -> Page[StockRow]`
  (a `kind` filter covers its descendants; a filament item's units come with their
  spool's grams left in `qty` and `uom = 'g'`); `bulk(body: StockBulk, by) -> BulkResult`
  (each id under the lock order of R-P2-32; a status change through
  `set_unit_status_in`; a move writes the location; one result per id, no partial
  transaction); `import_csv(text: str, by, *, dry_run: bool) -> ImportResult` (columns
  `item`, `qty`, `unit_cost`, `location`, `note`, `received_at`; each row a `receive_in` or
  `receive_units_in` with `origin = 'opening'`; filament rows go to the spool import).
  `forecast(item_id) -> Forecast(daily_use, days_left)` from the `use` moves of the last
  90 days (the filament view keeps `SpoolForecast`'s own rule).
- Produces routes (tag `inventory`): `GET /inventory/items/{item_id}/prices`, `GET
  /inventory/offers/{offer_id}/prices` (phase 2's route answers `OfferPrices` now; the
  POST stays), `DELETE /inventory/offer-prices/{price_id}` (an `invoice` price point is
  409 `price_from_invoice`, as the filament delete), `GET
  /inventory/suppliers/{supplier_id}/price-sheet`, `GET /inventory/suppliers/stats`, `GET
  /inventory/metrics/purchasing?since&until`, `GET /inventory/cost-per-unit?price&quantity&pack_size&tax&shipping&net_weight_g`,
  `GET /inventory/stock`, `POST /inventory/stock/bulk`, `POST /inventory/stock/import?dry_run=`,
  `GET /inventory/items/{item_id}/forecast`, and `/inventory/shopping-list` (`GET`, `POST`,
  `DELETE`, `PATCH|DELETE /{row_id}`; a row names an `item_id` of any kind, or free text,
  with `quantity` in the item's unit). The `/filament/` twins (`types/{id}/prices`,
  `products/{id}/prices`, `suppliers/{id}/price-sheet`, `suppliers/stats`,
  `metrics/purchasing`, `cost-per-kg`, `shopping-list`) call the same store and keep
  their answer shapes.

- [ ] **Step 1: Write the failing tests** (`requires_postgres`; seed supplier "Acme" with
  an offer of `hardware` item "M3 bolt" (pack 100, `pack_uom` `each`) priced 12.00 on
  2026-10-01 and 15.00 on 2026-11-01, and a `nozzle` offer (pack 1) at 30.00):
  - `test_a_hardware_price_sheet_has_per_unit_and_change`: `price_sheet(acme)` → the bolt's
    row has `newest.per.pack == 15.00`, `newest.per.unit == Decimal("0.1500")`,
    `previous.per.unit == Decimal("0.1200")`, `change_pct == Decimal("25.0")`; the nozzle
    row has no change; no row is left out for its kind.
  - `test_item_prices_lists_every_offer_with_history`: two suppliers' offers of the bolt →
    two `OfferPrices`, newest first.
  - `test_supplier_stats_counts_every_item`: a confirmed invoice of 2 bolt packs (allocated
    30.00) and 1 nozzle (30.00) → `spend == 60.00`, `lines == 2`, `units_bought == 201`.
  - `test_metrics_price_per_unit_over_time`: two `PriceSeries` points for the bolt at
    0.12 and 0.15, `basis == "unit"`; spend per supplier and month.
  - `test_cost_per_unit`: `cost_per_unit(30.00, 2, 100, 2.40, 5.00, None)` →
    `cost_per_unit == Decimal("0.1872")`, `price_per_unit == Decimal("0.1620")`; with
    `pack_size` 1 and `net_weight_g` 1000 it equals today's `/filament/cost-per-kg`
    answer for the same inputs.
  - `test_deleting_an_invoice_price_is_refused`.
  - `test_stock_list_filters_and_bulk` (`test_stock_list.py`): a lot of bolts and two
    nozzle units → `list(kind="part")` has 3 rows; `bulk(move to L1)` moves all 3 and
    `bulk(status retired)` on a lot answers `WrongTracking` for that id only;
    `import_csv` of `M3 bolt,50,0.14,Shelf A,,2026-11-02` with `dry_run` writes nothing,
    without it receives one lot.
  - `test_a_shopping_row_takes_any_item`: a bolt row with `quantity` 300 lists with its
    item and kind; the filament view's material and colour columns are null for it.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_purchasing_reads.py tests/inventory/test_stock_list.py tests/api/test_purchasing.py tests/filament/test_invoice_money.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.** Route handlers hold no SQL (R-P3-54).
- [ ] **Step 4: Run** the same and `cd backend && uv run --frozen pytest tests/filament -n auto -q` (the filament twins answer as before) — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): price sheets, history, stats, metrics, stock list and shopping for every item (#7068, 3a 2/6)`

## Layer A3 — `feat/7068-a3-filament-items` (on A2; ships like any PR, R-P3-29, R-P3-63)

### Task 8: Material and subtype become child kinds of `filament`

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_filament_kinds.sql`,
  `backend/scadbuddy/filament/kinds.py`
- Modify: `backend/scadbuddy/filament/catalog.py` (a material's or subtype's create, rename
  and archive keep its kinds in step, in the same transaction),
  `backend/scadbuddy/filament/types.py` (`create_on` and `update` call
  `FilamentKinds.kind_for_in`), `backend/scadbuddy/filament/component.py`
- Test: `backend/tests/filament/test_kinds.py`, `backend/tests/test_pg_migrations.py`
  (`test_the_filament_kinds_migration_*`)

The migration (a top comment cites #7068, spec §6.4 and the owner's rule A.1 of
2026-11-25):
- `filament_kind_catalog (kind_id text PRIMARY KEY REFERENCES item_kinds (id),
  material_id uuid NOT NULL REFERENCES filament_materials (id), subtype_id uuid REFERENCES
  filament_subtypes (id), UNIQUE NULLS NOT DISTINCT (material_id, subtype_id))`.
- One kind per unarchived material: `id` per Pre-flight 10, `name` = the material's `name`
  unchanged, `parent_id = 'filament'`, `tracking = 'filament'`, `uom = 'g'`, `builtin =
  false`; and one per archived material that a spool type names, archived.
- One kind per `(material, subtype)` pair: every subtype with its own `material_id`, and
  every pair that a spool type names (a subtype "for every material" gets a kind under
  each material a type uses it with). `name` = the subtype's `name`, `parent_id` = the
  material's kind. Each kind gets its `filament_kind_catalog` row.
- `filament`'s `attribute_schema` gains `rgba` (`string`, pattern
  `^[0-9A-Fa-f]{8}$`), `colour_name` (`string`), `diameter_mm` (`number`) (R-P3-46). It
  documents and never restricts (§6.3a).

**Interfaces:**
- Produces (`filament/kinds.py`): `class KindNames(NamedTuple): material: str | None;
  subtype: str | None`; `class FilamentKinds(pool)`:
  - `kind_for_in(conn, material_id: UUID | None, subtype_id: UUID | None) -> str`: the
    kind of that pair, made when absent (the subtype's kind under the material's; the
    material's kind with no subtype; `filament` with neither). A subtype of another
    material is `CatalogMismatch` (422).
  - `names_in(conn, kind_id: str) -> KindNames`: the exact catalog text of the material
    and subtype on the kind's path; `KindNames(None, None)` for `filament`.
  - `catalog_in(conn, kind_id: str) -> tuple[UUID | None, UUID | None]`: the material and
    subtype ids, for the defaults (`types.py` "what a spool of the type inherits", l.496).
- Produces (`catalog.py`): a material or subtype rename renames its kinds to the same text;
  an archive archives them when no unarchived item has them; a restore restores them.

- [ ] **Step 1: Write the failing tests**:
  - `test_the_filament_kinds_migration_makes_the_tree` (seed with
    `migrate(conn, MIGRATIONS[: _index("filament_kinds")])`): materials `PLA`, `PETG-CF`;
    subtype `Basic` for every material, subtype `Silk` of `PLA`; a type `PLA`+`Basic` and a
    type `PETG-CF`+`Basic` → kinds named `PLA` (`id == "filament_pla"`, parent
    `filament`), `PETG-CF` (`id == "filament_petg_cf"`), `Silk` under `filament_pla`,
    `Basic` under `filament_pla` and `Basic` under `filament_petg_cf`; each kind's name is
    the exact text; `filament_kind_catalog` has 5 rows.
  - `test_a_slug_clash_takes_a_suffix`: materials `PETG CF` and `PETG-CF` → ids
    `filament_petg_cf` and `filament_petg_cf_2`, names unchanged.
  - `test_a_long_name_fits_the_id`: a material named with 90 letters → an id of 40
    characters that matches `^[a-z][a-z0-9_]{0,39}$`.
  - `test_kind_for_makes_the_pair_once` (`test_kinds.py`): `kind_for_in(PLA, Matte)` twice
    → one new kind named `Matte`; `kind_for_in(None, None) == "filament"`; a subtype of
    `PETG` with material `PLA` → `CatalogMismatch`.
  - `test_names_are_the_catalog_text` (R-P3-56): `names_in(<Silk kind>) == KindNames("PLA",
    "Silk")`; after a rename of the material to `PLA+`, the kind's name is `PLA+` and
    `names_in` answers `PLA+`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament/test_kinds.py tests/test_pg_migrations.py -k "kinds" -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/filament tests/inventory -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): material and subtype are child kinds of filament (#7068, 3a 3/6)`

### Task 9: Migrate filament purchasing onto the generic tables

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_purchasing_to_generic.sql`
- Test: `backend/tests/test_pg_migrations.py` (`test_the_purchasing_migration_*`, with a
  `_before_purchasing(conn)` helper: `migrate(conn, MIGRATIONS[: _index("purchasing_to_generic")])`, after `filament_kinds`,
  as `_before_parts` does, l.1128)

The migration, in one file, in this order (actor `{"kind": "provider", "principal":
"purchasing_migration"}`; shop exponent as a SQL `CASE` that cites `orders/currency.py`
`minor_exponent`, as the parts migration did):
0. `LOCK TABLE filament_supplier_products, filament_price_points, filament_spool_types,
   filament_spools, invoices, invoice_lines IN EXCLUSIVE MODE` (R-P2-20). No deploy window
   (R-P3-29, R-P3-63): this migration drops nothing an A2 pod reads or writes; see "The
   rollout and the catch-up" below.
1. If an `items` row has a spool type's id, RAISE (Pre-flight 6, as R-P2-24).
2. `items` from `filament_spool_types`: `id` = the type's id, `kind_id` = the kind of its
   `(material_id, subtype_id)` from `filament_kind_catalog` (Task 8; `filament` with
   neither),
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
   `received_units = quantity × pack_size` where NULL (an old line received what it
   billed, R-P3-37). `supplier_product_id` stays and `received_units` stays nullable until
   the follow-up (R-P3-63).
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
12. `filament_supplier_products` and `filament_price_points` stay tables; from this layer
    no new code writes them, and the catch-up copies what an old pod writes (R-P3-63). A
    follow-up drops them after a prod check, as phase 2's Ruling 6.
13. Properties: for each new item, `rgba`, `colour_name` and `diameter_mm` of its type
    become entity properties of `item:<id>` (R-P3-46).

**The rollout and the catch-up (R-P3-63, review finding 3).** During A3's rollout the old
pods are A2 pods. They read and write `invoice_lines.supplier_product_id`, write
`received_units` as NULL, insert into `filament_supplier_products` and
`filament_price_points`, create spool types and spools with no item or unit, and may make
`spool_type:` or `spool:` entities. So this migration drops, renames and constrains none of
those: `supplier_product_id` stays (new code writes both it and `offer_id`),
`received_units` stays nullable (a NULL reads as billed), and the two filament tables stay
tables that new code no longer writes. Then `backend/scadbuddy/inventory/catch_up.py`
`purchasing_catch_up_in(conn) -> CatchUp` runs at every API start (in the start hook, under
the advisory lock of the migrations) and once at the end of this migration. It is
idempotent and does, in one transaction: copy each `filament_supplier_products` and
`filament_price_points` row whose id has no `supplier_offers` or `offer_price_points` row
(steps 6 and 7's rules); set `offer_id` from `supplier_product_id` where NULL and
`received_units` where NULL; give each spool type with `item_id IS NULL` its item and kind
(steps 2 and Task 8) and each typed spool with `unit_id IS NULL` its unit and moves (step
5); fold each `spool_type` or `spool` entity into its `item` or `unit` twin with
`repoint_entity` (step 4); and log the counts (`CatchUp(offers, prices, lines, items,
units, entities)`). The follow-up that drops the old columns and tables runs only after the
catch-up logs all zeros on two starts in a row.

- [ ] **Step 1: Write the failing tests** (seed with `_before_purchasing`):
  - `test_the_purchasing_migration_moves_products_and_prices`: supplier "Acme", type
    `PLA-BLK`, product `P` (sku `S1`, `pack_size` 4, url `https://x.test/p`), an invoice line
    `L` naming it with quantity 8, a price point `79.9600` USD of source `invoice` → an offer
    with id `P`, `item_id` = the type's id, `pack_size == 4`, `pack_uom == "spool"`; a price
    point with the old id and `invoice_line_id == L`; `invoice_lines.offer_id == P`,
    `quantity == 8`, `pack_size == 1`, `received_units == 8`, `supplier_product_id == P`; `SELECT … FROM filament_supplier_products` answers
    `P` with `type_id`.
  - `test_a_bad_product_url_becomes_no_url`: `product_url = 'www.x'` → offer `url IS NULL`.
  - `test_spool_types_become_filament_items`: a live and an archived type → two items with
    their ids, `kind_id` = the `PLA` / `Basic` kind of Task 8, `name` = the code, the archived one archived; two
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
  - `test_the_catch_up_copies_what_an_old_pod_wrote` (`tests/inventory/test_catch_up.py`):
    after the migration, insert as an A2 pod would (a `filament_supplier_products` row, a
    `filament_price_points` row, a line with `supplier_product_id` and NULL
    `received_units`, a spool type with no item, a typed spool with `unit_id IS NULL`, an
    attachment on `spool:<s>`); `purchasing_catch_up_in` → an offer and a price point with
    the old ids, the line's `offer_id` and `received_units`, an item, a unit with its
    `receive` move and `unit_id == id`, the attachment under `unit:<s>`; a second run
    returns all zeros and writes nothing.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_pg_migrations.py -k purchasing_migration -q` — Expected: FAIL.
- [ ] **Step 3: Write the migration.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): filament purchasing moves onto offers, items and units (#7068, 3a 3/6)`

### Task 10: One model for spool types and spools; the spool workflow as an extension

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
  10^exp / (net g / 1000)`, or NULL for an unknown cost, where net g is
  `received_net_g`, else the label weight (R-P3-40); `deleting` deletes the
  spool row's workflow rows as `spools.delete` does today.
- Produces: `FilamentSpoolStore.create_on` becomes `StockStore.receive_units_in(item = the
  type, unit_ids=[new id], extra=…)` and returns the spool, with `unit_id` set on the spool
  row (R-P3-41); a spool `cost_per_kg` write from
  a person, the agent or the pull calls `set_unit_cost_in` with `cost_per_kg × net g / 1000
  × 10^exp`, where net g is the same basis `cost_set` uses (`received_net_g`, else the
  label weight), so the value typed is the value stored (R-P3-64). A label weight change keeps `cost_minor` (what was paid; owner
  accepted) and writes the new `cost_per_kg`.
- Produces (R-P3-39, R-P3-40): `class ReceivedGrams(NamedTuple): grams: Decimal; source:
  Literal["weighed", "derived"]`; `SpoolUnits.received_net_g(conn, spool_id) ->
  ReceivedGrams | None`: the `grams_after` of the spool's first `weighed` event with
  `source` in `('scale', 'person')`, read with `ledger.events` (Pre-flight 8), as
  `weighed` when no use event (`print_run`, `bb_usage`) comes before it; None with no such
  weighing (the label weight stays). In layer A3 a weighing after a use also gives None;
  Task 12 makes it `derived` once the `inventory_moves` view exists. `SpoolUnits.weighed_on(conn, spool_id, event)`, called by `spools.add_event`
  after it books a `weighed` event: when that event is the one `received_net_g` reads, it
  rewrites `cost_per_kg` by the rule above. No past use moves (D14); "Re-cost past uses"
  carries it to job cost.
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
- Produces (the kind is the source of material and subtype, R-P3-45): a filament item's
  `kind_id` is the one place that says its material and subtype. `filament_spool_types.material_id`
  and `subtype_id` stay as a mirror for one release, written only by
  `ItemStore.set_kind_in(conn, item_id, kind_id, by)` (and by the type store, which calls
  it), from `FilamentKinds.catalog_in`. Every reader of a material or subtype name reads
  `FilamentKinds.names_in` of the item's kind: `FilamentSpoolStore` (`spool.material`,
  `spool.subtype`), `bambuddy/filament_profiles.py` (l.374), `filament/profile_fetch.py`
  (the 3DFP query), `bb_sync_plan.py`, `labels.py`, `metrics_store.py`. A type's
  `material_id`/`subtype_id` request fields stay and are turned into the kind.
- Produces (colour and diameter are properties, R-P3-46): `rgba`, `colour_name` and
  `diameter_mm` of a filament item are entity properties of `item:<id>`; the migration in
  Task 9 copies them from the type. `filament_spool_types.rgba`, `colour_name` and
  `diameter_mm` stay as the mirror for the workflow's readers, written only by the
  property store's filament hook (`EntityPropertyStore` calls `FilamentTypeMirror.set_in`
  for a property of a filament item), so a type edit, a property edit and the agent's
  `filament_type_upsert` all write both.
- Produces (codes and attempt media): `codes/resolve.py` `PREFIXES["S"]` resolves to
  `unit:<id>` (the spool's unit) and `PREFIXES["L"]` to `location:<id>` on `locations`. The
  order kinds are untouched: attempt media stay under `order_item:<id>` and
  `order_item_fulfilment:<id>` (R-P3-47).

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
  filament item → `WrongTracking`; a spool of unit cost 2500 and label 1000 g weighed first
  by scale at 1010 g net → `received_net_g == ReceivedGrams(1010, "weighed")`,
  `cost_per_kg == 24.7525`, `cost_minor == 2500`; a spool with no weighing →
  `received_net_g is None`, `cost_per_kg == 25.0000`; every typed spool row has `unit_id == id`; a person's
  `cost_per_kg` of 30.0000 on a 1000 g spool weighed first at 1010 g → `cost_minor ==
  3030`, and reading it back gives `cost_per_kg == 30.0000` (R-P3-64).
  Also (R-P3-45, R-P3-46, R-P3-47, R-P3-56):
  `test_spool_material_is_the_catalog_text` (`tests/filament/test_spools.py`): a spool of a
  type made with `PLA` and `Silk` reads `material == "PLA"` and `subtype == "Silk"`, and
  its item's kind is the `Silk` kind under `PLA`;
  `test_profile_match_reads_the_kind_names` (`tests/bambuddy/test_filament_profiles.py`): a
  Bambuddy profile with material `PLA` matches that spool, as before the move;
  `test_the_3dfp_query_names_the_material` (`tests/filament/test_profile_fetch.py`): the
  query text holds `PLA` and `Silk`, not a kind id; `test_a_kind_change_writes_the_mirror`:
  `set_kind_in(<item>, <PETG kind>)` → the type's `material_id` is PETG's and its
  `subtype_id` NULL; `test_a_colour_property_writes_the_type`: `PUT
  /entities/item:<it>/properties` with `rgba` `FF0000FF` → the type's `rgba ==
  "FF0000FF"`, and a type edit of `rgba` lists under the item's properties;
  `test_an_s_code_resolves_to_the_unit`: `GET /codes/S-<n>` names `unit:<id>`; and
  `tests/orders/test_archives.py` and `test_attempt_files.py` pass unchanged.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/filament tests/inventory tests/entities -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same with `-n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(filament): spool types are items and spools are units (#7068, 3a 3/6)`

### Task 11: One confirm and candidates path; offers; Bambuddy sees only filament; the invoice file is an attachment

**Files:**
- Modify: `backend/scadbuddy/filament/invoices.py` (delete the filament branch of
  `_confirm_line`, `_line_type`, `_line_product` and `_claimed`: a filament line is a line
  whose item is a filament item, received by `receive_line`; a `proposed_type` becomes its
  item through the type store; `file_key`/`set_file`/`delete` over the `invoice`
  attachment), `backend/scadbuddy/filament/purchasing_models.py` (`type_id` reads and
  writes `item_id`, `supplier_product_id` writes `offer_id`, `update_spool_ids` writes
  `adopt_ids`, each as a `validation_alias`), `backend/scadbuddy/filament/suppliers.py`
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
  confirms in one call and reconciles with its printed totals (#7648, R-P3-42); a spool
  line of 4 spools, label 1000 g, allocated 100.00, weighed at 1010, 1000, 995 and 1015 g
  → `billed_g == 4000`, `received_g == 4020`, `free_g == 20`, `cost_per_kg ==
  Decimal("24.8756")`; with one spool not weighed → `received_g is None`, `weighed_spools
  == 3`; the filament supplier, product, price and sheet tests pass with
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
- [ ] **Step 5: Commit** `feat(inventory): one confirm path for every item; only filament reaches Bambuddy (#7068, 3a 3/6)`

## Layer A4 — `feat/7068-a4-spool-job-cost` (on A3; Pre-flight 7)

### Task 12: Job cost reads spool cost; units re-cost past uses

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
- Produces (R-P3-39, owner 2026-11-25): `SpoolUnits.received_net_g` for a spool first
  weighed after one or more uses returns `ReceivedGrams(grams_after + Σ |qty| of the spool's
  `spool` rows in `inventory_moves` before that weighing, "derived")`, by the counting rules
  of R-P3-28. `weighed_on` rewrites `cost_per_kg` from it as for a `weighed` value.
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
  - `test_received_grams_are_derived_after_a_print` (`tests/filament/test_spool_units.py`): a
    1000 g label, a print that uses 120 g, then a first weighing of 900 g →
    `received_net_g == ReceivedGrams(1020, "derived")`; the line's `received_g == 1020`,
    `received_g_source == "derived"`, `free_g == 20`.
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
- [ ] **Step 5: Commit** `feat(orders): job cost prices filament at its unit's cost (#7068, 3a 4/6)`

## Layer A5 — `feat/7068-a5-purchasing-agent` (on A4)

### Task 13: Purchasing, stock and location tools for every item; the filament aliases

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
  `agent/src/tools/coverage.ts` (drop the layer-A2 entries), `agent/src/tools/filament.ts`
  (`filament_purchasing_metrics`, `filament_cost_per_kg` and `filament_location_*` become
  aliases), `agent/src/tools/inventory.ts` (the stock, offer price, shopping and location
  tools), `agent/src/sessions/touched.ts`
  (every new write tool)
- Test: `agent/test/purchasing.test.ts`, `agent/test/filamentPurchasing.test.ts`,
  `agent/test/filamentLinks.test.ts`, `agent/test/inventoryLinks.test.ts`,
  `agent/test/orders.test.ts`, `agent/test/coverage.test.ts`

**Interfaces:**
- Produces tools (route; risk): `supplier_list` (read), `supplier_upsert` (write),
  `invoice_list` (read), `invoice_get` (read; lines with links, lots, units, `received_g_source` with the words
  "derived from weighing and prints" for `derived`, and billed, received, free and
  cost per unit or per kg, R-P3-42),
  `invoice_candidates` (read; items with their adoptable lots and units), `invoice_upsert` (write;
  lines take `item_id`, `offer_id`, `kind`, `pack_size`, `received_units`, `adopt_ids`,
  `proposed_item`, `proposed_type`), `invoice_confirm` (write), `invoice_amend_preview`
  (read; `POST …/amend?preview=true` writes nothing), `invoice_amend` (outward, gated: a person approves it,
  R-P3-34), `invoice_delete` (write), `invoice_events` (read), `invoice_review_list` (read),
  `invoice_line_link_list` (read; by line or by entity), `invoice_line_link_set` (write; add
  or update), `invoice_line_link_delete` (write), `inventory_unit_recost` (write); and for parity (R-P3-58, Task 7): `supplier_stats`
  (read), `supplier_price_sheet` (read), `purchasing_metrics` (read),
  `inventory_cost_per_unit` (read), `inventory_offer_price_list` (read),
  `inventory_offer_price_delete` (write), `inventory_stock_list` (read),
  `inventory_stock_bulk` (write), `inventory_stock_import` (write), `shopping_list`
  (read), `shopping_add`, `shopping_update`, `shopping_remove` (write), `location_list`,
  `location_get` (read), `location_upsert` (write). Aliases for one release, each
  description saying so: `filament_supplier_list|upsert|stats`, `filament_product_list|upsert`
  (offers), `filament_price_list|add|delete|sheet`, `filament_purchasing_metrics`,
  `filament_cost_per_kg` (the per-kg view of `inventory_cost_per_unit`),
  `filament_shopping_*`, `filament_location_*`. Descriptions say "item type" for an item
  and "stock" for lots and units (R-P3-59). Each
  description says what it writes: amend spreads only the tax and shipping no line holds
  unless `respread` (D15); a link's share becomes a maintenance event or an order cost entry
  only on an expense line (R-P3-9); re-cost changes past order costs (D14).

- [ ] **Step 1: Write the failing tests**: each tool's route and risk (the table style of
  `agent/test/inventory.test.ts`); `invoice_amend` is `outward` and gated; every
  alias answers as its generic tool; no `invoice_amend_propose` exists;
  `filament_invoice_get` answers as `invoice_get`; `order_purchase_record` with
  `invoice_line_id` sends it; `uiPath("invoice:<id>")` matches the fixture.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/purchasing.test.ts test/filamentPurchasing.test.ts test/filamentLinks.test.ts test/inventoryLinks.test.ts test/orders.test.ts test/coverage.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd agent && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): invoice and supplier tools for every item (#7068, 3a 5/6)`

## Layer A6 — `feat/7068-a6-docs` (on A5)

### Task 14: Phase 3a docs

**Files:**
- Modify: `docs/features/inventory.md` (purchasing, line kinds, links, amend, the backfill,
  the shared model and the spool workflow), the filament feature page that describes
  invoices (`grep -ln "filament_invoice" docs/features`), `docs/features/orders-and-quotes.md`
  (purchases with a line; job cost's filament basis), `docs/architecture/data.md` (the
  renames, the views, the two new tables, the moved and retired tables, the
  `inventory_moves` view, the kind tree, the catch-up, the migrations), `backend/CLAUDE.md` (the
  `inventory/` and `filament/` layout lines), `agent/CLAUDE.md` (the tool families), the
  spec's §9 phase 3 line (a "done in" note with the PRs), and §6.4's filament paragraph and
  D1 where R-P3-30 changes them (the unit carries the cost; grams stay in the ledger), and
  §6.7 for billed and received (R-P3-37).

- [ ] **Step 1: Edit**, each fact with its source (file:line, PR or issue).
- [ ] **Step 2: Run** `grep -rn "filament_supplier_products\|filament_price_points\|filament_invoices\b\|filament_invoice_lines\|filament_locations\|update_spool_ids" docs backend/CLAUDE.md frontend/CLAUDE.md agent/CLAUDE.md plugins` — Expected: only historical specs and plans, and the retired names in `data.md`.
- [ ] **Step 3: Commit** `docs: generic purchasing and invoice line links (#7068, 3a 6/6)`

## Phase 3b — the Filament section in Inventory, item types at parity, the invoice flow

Phase 3b starts when layer A5 has merged. Its issue is filed under #6949 ("Issues to file").
Load `frontend-design` before Task 15.

## Layer B1 — `feat/<3b>-b1-filament-in-inventory` (on main after A5)

### Task 15: The Filament section moves into Inventory; names; redirects

**Files:**
- Move (`git mv`): every page of `frontend/src/pages/filament/` to
  `frontend/src/pages/inventory/filament/`, and `components/filament/purchasing/` to
  `components/inventory/purchasing/`, with their tests
- Modify: `frontend/src/App.tsx` (the new routes and one `<Navigate replace>` per old
  path, table below), `frontend/src/lib/inventoryLinks.ts` (`types`, `stock`,
  `filament(tab)`, `invoice`, `invoiceLine`, `supplier`, `location`, `shoppingList`,
  `invoiceReview`), `frontend/src/lib/filamentLinks.ts` (every path from `inventoryPaths`;
  the module stays as the filament view's names), `frontend/src/pages/inventory/filament/tabs.ts`
  (the filament view's tabs), `frontend/src/components/AppShell.tsx` (no Filament entry;
  Inventory's sub-links: Item types, Stock, Suppliers, Invoices, Locations, Shopping list,
  Metrics, and a Filament group with Spools, Tags, Ledger, Catalog, Scan, Labels),
  `test-fixtures/filament/ui-paths.json`, `test-fixtures/inventory/ui-paths.json`,
  `agent/src/tools/filamentLinks.ts` and `inventoryLinks.ts` (the same paths),
  `backend/scadbuddy/filament/links.py` and `inventory/links.py` (every `ui_path` the
  backend writes), `frontend/e2e/page-titles.spec.ts`
- Test: `frontend/src/AppRedirects.test.tsx` (new), `inventoryLinks.test.ts`,
  `filamentLinks.test.ts`, `agent/test/filamentLinks.test.ts`, `inventoryLinks.test.ts`,
  `backend/tests/filament/test_links.py`, `e2e/page-titles.spec.ts`

Where each Filament tab and page goes (R-P3-50). Each old path redirects for one release
and keeps its query and hash:

| Today | In Inventory |
|---|---|
| `/filament` (`FILAMENT_HOME`) | `/inventory/types?kind=filament` |
| `/filament/types` (Types tab) | `/inventory/types?kind=filament` (the Item types list, filament view) |
| `/filament/types/:id` (`TypePage`) | `/inventory/items/:id` (the item type page; filament sections as the kind's extension) |
| `/filament/spools` (Spools tab) | `/inventory/stock?kind=filament` (the Stock list, filament view: cards, forecast, type proposals) |
| `/filament/spools/:id` (`SpoolPage`, with #7681's weighing UI) | `/inventory/units/:id` (a filament unit's page is the spool page) |
| `/filament/tags` | `/inventory/filament/tags` |
| `/filament/ledger` | `/inventory/filament/ledger` |
| `/filament/catalog` | `/inventory/filament/catalog` (materials and subtypes link to `/inventory/kinds`) |
| `/filament/metrics` | `/inventory/metrics?kind=filament` (purchasing, consumption, material numbers) |
| `/filament/scan` | `/inventory/filament/scan` |
| `/filament/labels` (#7216's page too) | `/inventory/filament/labels` |
| `/filament/shopping-list` | `/inventory/shopping-list?kind=filament` |
| `/filament/suppliers`, `/filament/suppliers/:id` | `/inventory/suppliers`, `/inventory/suppliers/:id` |
| `/filament/invoices`, `/filament/invoices/:id` | `/inventory/invoices`, `/inventory/invoices/:id` |
| `/filament/locations`, `/filament/locations/:id` | `/inventory/locations`, `/inventory/locations/:id` |
| `/inventory` | `/inventory/types` |
| Settings → Filament (`pages/settings/sections/filament.tsx`) | stays in Settings; the reset-links warning keeps its `filament_preset_mirror` gate (#7706) |

Names (R-P3-59): the UI says "Item types" for items (and "Types" inside a kind's view),
"Stock" for lots and units together, and "Lots" or "Units" where only one is meant. The
filament view keeps "Spool types" and "Spools" as its kind's words. No page says "Items"
for the list of types.

- [ ] **Step 1: Write the failing tests**: `AppRedirects.test.tsx` renders each old path of
  the table in a `MemoryRouter` and lands on its new path with `?x=1#line-2` kept;
  `/inventory/units/<filament unit>` renders the spool page's heading; the fixture paths
  match the backend's, the agent's and the frontend's link modules; the shell has no
  "Filament" top entry and has "Item types" and "Stock".
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/AppRedirects.test.tsx src/lib` and `cd agent && pnpm exec vitest run test/filamentLinks.test.ts test/inventoryLinks.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `cd backend && uv run --frozen pytest tests/filament/test_links.py tests/inventory -q`, `cd frontend && pnpm lint && pnpm typecheck && pnpm exec playwright test e2e/page-titles.spec.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): the Filament section moves into Inventory (#<3b>, 1/5)`

## Layer B2 — `feat/<3b>-b2-item-types` (on B1)

### Task 16: The Item types list and the Stock list

Parity rows 1 and 2 (R-P3-58, R-P3-59).

**Files:**
- Create: `frontend/src/pages/inventory/TypesPage.tsx` (`/inventory/types`),
  `frontend/src/pages/inventory/StockPage.tsx` (`/inventory/stock`),
  `frontend/src/components/inventory/StockTable.tsx`, `StockCards.tsx`, `StockBulkBar.tsx`,
  `StockImport.tsx`
- Modify: `frontend/src/pages/inventory/InventoryPage.tsx` (becomes `TypesPage`; its kind
  tree filter, search, low-stock and archived toggles stay), the filament view
  (`pages/inventory/filament/TypesTab.tsx` and `SpoolsTab.tsx` become the `kind=filament`
  columns and sections of the two pages: colour, label g, per kg, price book, fill from
  profiles; `SpoolCards`, `SpoolForecast`, `TypeProposals`, `SpoolImport`)
- Test: `TypesPage.test.tsx`, `StockPage.test.tsx`, `StockBulkBar.test.tsx`

**Interfaces:**
- Consumes: `GET /inventory/items`, `GET /inventory/stock`, `POST /inventory/stock/bulk`,
  `POST /inventory/stock/import`, `GET /inventory/items/{id}/forecast` (Task 7).
- Produces: one layout for every kind. The Item types list has the same columns, filters
  and actions as today's Types tab (name, kind, newest price per pack and per unit,
  offers, on hand, archived toggle, "New item type"); a `kind` filter that names
  `filament` adds the filament columns. The Stock list has the Spools tab's filters
  (kind, type, status, location, supplier, invoice, text), table and cards views, "Columns
  shown", the bulk bar (move, status, archive, note), CSV import with a dry run, and the
  forecast; a filament row is a spool row (grams left, tray, tag).

- [ ] **Step 1: Write the failing tests**: with the mocks, `/inventory/types` lists the
  `hardware` item "M3 bolt" with "0.15 / each" and the filament type "PLA Basic Black"
  with "25.00 / kg"; `?kind=filament` adds the colour column; `/inventory/stock` lists a
  bolt lot and a spool; selecting both and "Move to Shelf A" posts one bulk body with both
  refs; the bulk bar is the same component on `?kind=filament` and `?kind=part`; a CSV dry
  run shows the rows it would receive and sends nothing more.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/inventory src/components/inventory` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `pnpm lint && pnpm typecheck` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): item types and stock lists for every kind (#<3b>, 2/5)`

### Task 17: The item type page at parity with the spool type page

Parity row 3 and 13 (R-P3-58).

**Files:**
- Modify: `frontend/src/pages/inventory/ItemPage.tsx` (the sections below; a filament item
  renders today's `TypePage` sections as extension sections), `frontend/src/pages/inventory/filament/TypePage.tsx`
  (split into `FilamentTypeSections.tsx`: temperatures, drying, slicer presets, K values,
  pressure advance, `ProfileFetchPanel`, `ProfileFillDialog`),
  `frontend/src/components/filament/CostCalculator.tsx` → `components/inventory/CostCalculator.tsx`
  (per unit; per kg when a net weight is given),
  `components/inventory/purchasing/PricePanel.tsx` (every offer, per pack and per unit,
  per kg for filament)
- Test: `ItemPage.test.tsx`, `CostCalculator.test.tsx`, `PricePanel.test.tsx`

**Interfaces:**
- Produces (sections, in this order, for every item type): "What it is" (name, kind path,
  manufacturer, MPN, GTIN and the kind's schema properties, edited in place); "Price"
  (default unit cost, the cost calculator, the price panel with each offer's history,
  sparkline and "Use as default cost", which fills the cost field and waits for Save as
  today); "Reordering" (`low_stock`, "Add to shopping list" with the newest offer
  chosen); "Stock of this type" (the Stock list filtered to the item); "Invoice lines"
  (`InvoiceLinesPanel`, Task 20); "Files, links and properties". A filament item adds its
  extension sections after "Price".

- [ ] **Step 1: Write the failing tests**: the page of "M3 bolt" shows "What it is" with
  its MPN, the price panel with two price points of its Acme offer (0.12 and 0.15 per
  unit), the calculator answering `0.1872` for 2 packs of 100 at 30.00 with 2.40 tax and
  5.00 shipping, "Add to shopping list", and its lot under "Stock of this type"; "Use as
  default cost" fills 0.1500 and sends nothing until Save; the page of a filament type
  shows the same sections plus "Temperatures" and "Slicer presets", and its price panel
  shows per kg; the section headings of the two pages are equal but for the extension
  sections.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/inventory/ItemPage.test.tsx src/components/inventory` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `pnpm lint && pnpm typecheck` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): the item type page has every section the spool type page has (#<3b>, 2/5)`

## Layer B3 — `feat/<3b>-b3-purchasing-ui` (on B2)

### Task 18: The invoice editor for every item

Parity rows 9 and 10 (R-P3-58). Task 15 moved the pages; this task makes the one line
editor.

**Files:**
- Modify: `frontend/src/pages/inventory/InvoicePage.tsx`, `InvoicesPage.tsx`,
  `components/inventory/purchasing/InvoiceLinesTable.tsx` (one row component for every
  item), `SpoolMatchPicker.tsx` → `AdoptStockPicker.tsx` (lots and units of any item;
  spools in the filament view), `InvoiceTotals.tsx`, the mocks
  (`frontend/src/mocks/features/`)
- Test: `InvoicePage.test.tsx`, `InvoicesPage.test.tsx`, `InvoiceLinesTable.test.tsx`,
  `AdoptStockPicker.test.tsx`

**Interfaces:**
- Consumes: the layer A2 routes (`pnpm gen:api`).
- Produces (InvoicePage): one line editor for every item: an item search over
  `/inventory/items` (a filament item shows its colour swatch; "New spool type" opens the
  type fields as `proposed_type`), a `kind` select, an offer, a pack size, and units to
  adopt (lots or units received outside an invoice, note matches first, R-P3-43); a
  "Received" field beside the billed quantity (defaults to billed); each confirmed line
  shows "Billed 10, received 11, 1 free" and its cost per unit, and a filament line
  "Paid for 4,000 g, got 4,020 g (3 of 4 spools weighed)" with its cost per kg (R-P3-42);
  candidates show `matched_by`; each confirmed line
  has a links list (add entity by `kind:ref` search, share, note, its amount and consumer
  link); "Amend" opens new lines plus header totals and a "Re-spread over all lines" switch,
  shows the preview's before and after per line, then confirms; after a respread it lists
  each `recost` target with a "Re-cost past uses" button (`POST
  /inventory/lots/{id}/recost` or `/inventory/units/{id}/recost`); an events list; the file
  preview reads the `invoice` attachment (#6889).
  A `hardware` line and a filament line render through the same row component, with the
  same columns and controls; only the per-kg figures and the colour swatch are the
  filament view's. The Invoices list shows "Stock received" for every line.

- [ ] **Step 1: Write the failing tests**: a `hardware` line sends `{item_id, kind: "stock"}`; a filament
  line sends `{item_id, adopt_ids}`; a line with `received_units` 11 of 10 billed shows
  "1 free" and the cost per unit `10.00`; adopting a lot sends its id in `adopt_ids`; an amend preview shows `171.12 → 171.12` and the new
  line's `5063.16`; a 409 `amend_short` shows its detail; adding a link to `order:o1` shows
  its amount; a respread lists a spool's unit with "Re-cost past uses"; a confirmed hardware line shows its item type, "Billed 2, received 3, 1 free" and links
  to its lots; the row of a hardware line and of a filament line have the same column
  headers.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/inventory src/components/inventory/purchasing` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `pnpm typecheck && pnpm lint` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): suppliers, invoices, amend and line links in Inventory (#<3b>, 3/5)`

### Task 19: Suppliers, offers, price sheets, metrics and the shopping list for every item

Parity rows 5-8, 12, 14, 15, 17 (R-P3-58).

**Files:**
- Modify: `frontend/src/pages/inventory/SupplierPage.tsx` (one offers table and one price
  sheet for every item; "Other offers" goes), `SuppliersPage.tsx` (stats columns),
  `components/inventory/purchasing/ProductsTable.tsx` → `OffersTable.tsx`,
  `AddProductSheet.tsx` → `AddOfferSheet.tsx` (an item type search over every kind; "New
  spool type" stays as the filament choice), `PriceSheet.tsx`, `AddPriceForm.tsx`,
  `PriceCell.tsx` (per pack and per unit; per kg for filament),
  `frontend/src/pages/inventory/MetricsPage.tsx` (`/inventory/metrics`: purchasing for
  every item; the filament view adds consumption and material numbers from `MetricsTab`),
  `frontend/src/pages/inventory/ShoppingListPage.tsx` (from `ShoppingListTab.tsx`)
- Test: `SupplierPage.test.tsx`, `SuppliersPage.test.tsx`, `OffersTable.test.tsx`,
  `PriceSheet.test.tsx`, `MetricsPage.test.tsx`, `ShoppingListPage.test.tsx`

**Interfaces:**
- Consumes: Task 7's routes; `GET /inventory/suppliers/{id}/offers` (phase 2).
- Produces: the supplier page of Acme lists the bolt offer and a filament offer in one table
  with the same columns; one price sheet; "Add price" on each row; the Suppliers list's
  "Units bought" (and "Spools bought" in the filament view); the shopping list takes any
  item type, a supplier, an offer, a quantity in the item's unit, pending or purchased.

- [ ] **Step 1: Write the failing tests**: the Acme page shows one table with "M3 bolt" and
  "PLA Basic Black", each with a newest price and "Add price"; the price sheet shows the
  bolt's change chip "+25%" from 0.12 to 0.15 per unit; there is no "Other offers"
  heading; "Add offer" finds "M3 bolt" by name; the metrics page draws a price series for
  the bolt per unit and for the PLA type per kg; the shopping list adds "M3 bolt" with
  supplier Acme, quantity 300 and status pending, then marks it purchased.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/inventory src/components/inventory/purchasing` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `pnpm lint && pnpm typecheck` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): suppliers, price sheets, metrics and shopping for every item (#<3b>, 3/5)`

### Task 20: Linked lines; purchases; lots, units and spools; the backfill review

**Files:**
- Create: `frontend/src/components/inventory/InvoiceLinesPanel.tsx` (`{entity}`; reads `GET
  /entities/{ref}/invoice-lines`), `frontend/src/pages/inventory/InvoiceReviewPage.tsx`
- Modify: `frontend/src/pages/inventory/ItemPage.tsx` (the panel; a filament item's page
  shows its units as spools and links to its type page; "Add to shopping list"),
  `LotPage.tsx` and `UnitPage.tsx` (the panel; "From invoice line" links with the line's
  billed, received and free figures; "Re-cost past uses" on a unit; a spool unit links to
  its spool), `frontend/src/pages/OrderPage.tsx`
  (the panel for the order), `frontend/src/components/orders/CustomerModels.tsx` (record a
  purchase with an optional invoice line picked from the order's linked or the supplier's
  expense lines), `frontend/src/pages/inventory/filament/SpoolPage.tsx` (the panel; "Re-cost past
  uses" through the unit route; "Label 1,000 g, received 1,010 g (first weighing), cost per
  kg 24.75", or "received 1,020 g (derived from weighing and prints)" for a `derived`
  value; slice A's `drift_g` beside it when present, Pre-flight 8), `frontend/src/pages/inventory/InvoicePage.tsx` ("May be missing lines" flag;
  "Review missing lines", which Task 22 wires to the flow), `frontend/src/App.tsx`
  (`inventory/invoices/review`)
- Test: `InvoiceLinesPanel.test.tsx`, `ItemPage.test.tsx`, `UnitPage.test.tsx`,
  `CustomerModels.test.tsx`, `SpoolPage.test.tsx`, `InvoiceReviewPage.test.tsx`, `InvoicePage.test.tsx`

**Interfaces:**
- Produces (`InvoiceReviewPage`, R-P3-31, R-P3-35): the `GET /inventory/invoices/review`
  list, newest first, each row with its supplier, number, date, total and signal chips
  (`subtotal_differs` shows header and line subtotals); an "All confirmed" toggle (`all`);
  per row "Open", "Mark done" (`lines_review = done`). A row without `has_file` says the
  session needs the file and offers the upload.

- [ ] **Step 1: Write the failing tests**: the panel lists a line with its invoice link and
  amount; recording a purchase with a line sends `invoice_line_id`; "Re-cost past uses" on a
  spool calls `/inventory/units/{id}/recost` and shows the returned orders with links; the review page shows a row with
  `subtotal_differs` and "4900.00 / 160.00"; "Mark done" sends `{lines_review: "done"}`;
  a lot page and a unit page show "Bought on" with the line's billed, received and free
  figures and "Where to buy" with each offer's newest price (parity row 4); a line with
  `adopt_ids` shows the lot it adopts and "no new stock"; the spool page shows "received
  1,010 g" for a weighed spool, "received 1,020 g (derived from weighing and prints)" for a
  derived one, and "not weighed" for one without.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/inventory src/pages src/components/orders/CustomerModels.test.tsx` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd frontend && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): invoice lines on items, orders and spools; the backfill review (#<3b>, 3/5)`

### Task 21: The parity test

R-P3-58: one test runs the same purchasing steps on a filament item and on a hardware item,
so no feature can exist for one only.

**Files:**
- Create: `frontend/e2e/purchasing-parity.spec.ts`, `frontend/src/mocks/features/purchasingParity.ts`
  (the msw state both runs share)
- Test: that spec, run by the `frontend` e2e job as every spec in `frontend/e2e/` is

**Interfaces:**
- Produces: `for (const item of [{ kind: 'filament', name: 'PLA Basic Black', unitWord:
  'kg' }, { kind: 'hardware', name: 'M3 bolt', unitWord: 'each' }])`, one `test` each, the
  same steps: open `/inventory/types?kind=<kind>` and find the item type; open its page and
  see "Price", "Reordering", "Stock of this type", "Invoice lines"; open supplier Acme and
  see the item in the one offers table and the price sheet; add a price; make a draft
  invoice with one line of the item (quantity 2, received 3), confirm it; see the line's
  item type, "Billed 2, received 3, 1 free", its cost per unit (per kg for filament as
  well) and the links to the lots or units made; open one of them and see "Bought on" with
  the invoice; see the new price on the item type's price panel; add the item to the
  shopping list and mark it purchased. Each step uses the same locator for both items.

- [ ] **Step 1: Write the spec** — Expected: FAIL on main before Tasks 16-20.
- [ ] **Step 2: Run** `cd frontend && pnpm exec playwright test e2e/purchasing-parity.spec.ts` — Expected: PASS on this layer.
- [ ] **Step 3: Commit** `test(inventory): one purchasing flow for a filament item and a hardware item (#<3b>, 3/5)`

## Layer B4 — `feat/<3b>-b4-invoice-flow` (on B3)

### Task 22: The `invoice-import` flow: extract, match, prompt, confirm or amend

R-P3-51 to R-P3-53. One built-in flow serves the import of a draft and the backfill of a
confirmed invoice that misses lines, for every item (parity row 11).

**Files:**
- Create: `backend/scadbuddy/inventory/builtin_flow.py` (`NAME = "invoice-import"`,
  `DISPLAY_NAME = "Invoice import"`, `AUTHOR = {"kind": "builtin", "name": NAME}`,
  `INPUTS_SCHEMA`, `SCRIPT`, `register(definitions)`, as `orders/builtin_flow.py` does with
  `FlowDefinitions.register_new` and `upgrade_builtin`, #4252, #4673),
  `backend/scadbuddy/inventory/invoice_flow_script.py` (the script's text and its pure
  helpers, importable by tests), `frontend/src/components/inventory/purchasing/InvoiceFlowButton.tsx`
- Modify: `backend/scadbuddy/inventory/component.py` (register at start),
  `backend/scadbuddy/filament/invoices.py` and `api/purchasing.py` (PATCH of a confirmed
  invoice also takes `import_run_id` with `if_import_run_id`, the compare-and-set the
  draft PATCH has today), `agent/src/tools/imageRefs.ts` (ref kind `invoice_file`
  `{invoice_id}`: the invoice's file, PDF or image, through `GET
  /api/v1/inventory/invoices/{id}/file`, checked by `checkedAttachments` with
  `DOCUMENT_MEDIA_TYPES` and `IMAGE_MEDIA_TYPES`, sent only inside a flow run),
  `backend/scadbuddy/workflows/flow_tools.py` (`_refs` passes an `invoice_file` ref
  through unchanged; the call's input shape for other refs does not change, so recorded
  histories replay), `frontend/src/pages/inventory/InvoicePage.tsx` ("Import" on a draft
  and "Review missing lines" on a confirmed invoice start a run and open its page),
  `frontend/src/pages/inventory/InvoiceReviewPage.tsx` (each row's "Review" starts a run)
- Test: `backend/tests/inventory/test_invoice_flow.py`, `backend/tests/flows/test_project_replay.py`
  (unchanged fixtures replay), `agent/test/imageRefs.test.ts`,
  `frontend/src/components/inventory/purchasing/InvoiceFlowButton.test.tsx`

**Interfaces:**
- `INPUTS_SCHEMA`: `{"type": "object", "required": ["invoice_id"], "properties":
  {"invoice_id": {"type": "string", "pattern": "^[0-9a-f-]{36}$"}, "mode": {"type":
  "string", "enum": ["import", "missing_lines"], "default": "import"}}}`.
- The script, in order (host functions from Pre-flight 9 only):
  1. **Claim.** `tool("invoice_upsert", {invoice_id, import_run_id: "flow:" + run id,
     if_import_run_id: None})`; a 409 ends the run with "another import holds this
     invoice". `missing_lines` needs a confirmed invoice with a file; `import` a draft with
     a file; otherwise the run ends naming why.
  2. **Extract (the LLM step).** `agent(EXTRACT_PROMPT, skills=["filament"],
     result_schema=EXTRACTED, images=[{"kind": "invoice_file", "invoice_id": id}],
     grant={"rules": []}, on_outside="deny")`: a durable agent session that reads the
     file and answers only JSON; with an empty grant every tool it might call outside reads
     is denied, so it writes nothing. `EXTRACTED`: `{number, date, currency, subtotal, tax,
     shipping, total, lines: [{position, description, sku, quantity, unit_price,
     line_subtotal, line_tax}]}` (the agent checks it against the schema; a nested schema
     is allowed there, `result_schema` is not a prompt schema).
  3. **Pair (deterministic).** `missing_lines` only: `tool("invoice_get")` gives the kept
     lines; `pair(printed, kept) -> list[Printed]` (in `invoice_flow_script.py`) pairs by
     SKU (case folded), then by description (case and space folded), then by
     `line_subtotal` within 0.01, each kept line used once, and returns the printed lines
     with no pair.
  4. **Match (deterministic).** For the lines to decide, `tool("invoice_candidates",
     {invoice_id, lines})` (the server's one candidates path, R-P3-24) gives each line its
     item candidates with `matched_by` and their adoptable lots and units.
  5. **One prompt per line (a person).** `prompt(title, schema, context=[{"table": …}])`
     with `title` = `"Line {n} of {m}: {description} — {quantity} × {unit_price}"` (cut to
     500) and `schema`:
     `{"type": "object", "required": ["item"], "properties": {"item": {"type": "string",
     "title": "Item type", "enum": [<"1. PLA Basic Black (SKU)">, …, "New item type",
     "Skip this line"], "description": <one line per candidate: label, kind, why>},
     "new_name": {"type": "string", "maxLength": 200}, "new_kind": {"type": "string",
     "enum": [<kind ids>]}, "kind": {"type": "string", "enum": ["stock", "asset",
     "expense"], "default": "stock"}, "received": {"type": "number", "exclusiveMinimum":
     0}, "adopt": {"type": "array", "items": {"type": "string", "enum": [<"lot 2026-11-02
     Amazon order 112-4455 (60 left)">, <"spool #41 PLA Basic Black">, …]}, "uniqueItems":
     true}}}`. The labels are numbered and unique; the flow maps each back to its id. An
     answer outside the enum is 422 by the server (A1), and the task stays open. For a
     filament line, "adopt" lists the spools to update or adopt, as "fills existing
     spools" does today.
  6. **Write the lines.** `import`: `tool("invoice_upsert", {invoice_id, header, lines})`
     on the draft. `missing_lines`: the lines go into an `InvoiceAmend` body with the
     printed header and `respread: true` (R-P3-35), checked with
     `tool("invoice_amend_preview", …)`.
  7. **Final prompt (a person).** `prompt("Confirm invoice {number}", {"type": "object",
     "required": ["decision"], "properties": {"decision": {"type": "string", "enum":
     ["Confirm", "Leave as draft"]}}})` for `import`, or with `["Amend", "Cancel"]` and a
     `respread` boolean (default true) for `missing_lines`; the title and the `decision`
     description hold each line's allocation before and after and each unit whose cost
     drops. Then `tool("invoice_confirm")` or `tool("invoice_amend", …)`. `invoice_amend`
     is outward: in a run whose grant does not admit it, it waits for the usual approval
     (R-P3-34 holds: a person decides).
  8. **Release.** `tool("invoice_upsert", {import_run_id: None, if_import_run_id: "flow:" +
     run id})`, also when the run fails or is cancelled (a `try`/`finally`), and
     `lines_review` becomes `done` after an amend.
- The agent chat tools (`invoice_*`) and `POST /api/v1/ai/filament/invoice-import` stay for
  ad-hoc use (R-P3-25); the invoice page starts the flow.

- [ ] **Step 1: Write the failing tests** (`test_invoice_flow.py`, `requires_temporal`,
  `requires_postgres`, the flow harness of `tests/flows/test_prompt.py`, with `agent()`
  stubbed to answer a fixed `EXTRACTED` for the Micro Center receipt of spec §6.7: a
  filament line 171.12 and an "AMS 2 Pro" line 4740.00):
  - `test_pair_keeps_only_unpaired_lines` (pure): kept `[{sku: "PLA-BK", subtotal:
    160.00}]`, printed the same plus `{sku: "AMS2PRO", subtotal: 4740.00}` → one line,
    `AMS2PRO`; a printed line whose SKU differs only by case pairs.
  - `test_import_mode_prompts_each_line_then_confirms`: a draft with that file → two line
    tasks, then a final task; answering "1. PLA Basic Black (SKU)" and "New item type"
    with `new_kind` `ams_part`, then "Confirm" → the invoice is confirmed with 2 lines,
    the AMS line received one lot of a new `ams_part` item, and the totals reconcile.
  - `test_missing_lines_mode_amends_with_respread`: the confirmed invoice of
    `test_an_amend_keeps_the_old_allocation` (Task 5) with header subtotal 4900.00 → one
    line task (the AMS line), a final task whose text holds `171.12 → …` and the spool
    whose cost drops; "Amend" → one `respread` event, the sum of allocations equals
    5234.28, `lines_review == "done"`.
  - `test_an_answer_outside_the_offer_is_refused` (Review Focus 8): posting `{"item":
    "item:<other id>"}` → 422 naming `item`; the task stays open.
  - `test_cancel_at_the_final_prompt_changes_nothing`: confirmed invoice unchanged; a
    draft keeps its lines and nothing is received; the claim is released.
  - `test_a_second_run_is_refused_while_one_holds_the_claim`.
  - `test_the_extract_step_writes_nothing`: the stubbed session's call to
    `invoice_upsert` is denied by the empty grant.
  - `test_adopt_names_the_early_lot`: a lot with note "Amazon order 112-4455" appears in
    `adopt`; choosing it → the line adopts it and `on_hand` does not change (R-P3-43).
  - `imageRefs.test.ts`: an `invoice_file` ref outside a flow run is refused; inside, a
    PDF is sent as a document.
  - `InvoiceFlowButton.test.tsx`: "Review missing lines" posts `{inputs: {invoice_id,
    mode: "missing_lines"}}` to `/api/v1/workflows/invoice-import/runs` with an
    `Idempotency-Key` and opens the run page.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_invoice_flow.py tests/flows/test_project_replay.py -q` and `cd agent && pnpm exec vitest run test/imageRefs.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, `cd backend && uv run --frozen pytest tests/flows -n auto -q`, and `cd frontend && pnpm exec vitest run src/components/inventory/purchasing` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): the invoice-import flow with typed prompts (#<3b>, 4/5)`

## Layer B5 — `feat/<3b>-b5-skill-docs` (on B4)

### Task 23: The filament skill points to the flow; #7648 closes

**Files:**
- Modify: `plugins/scadbuddy/skills/filament/SKILL.md`, `plugins/scadbuddy/.claude-plugin/plugin.json`
  (above PR #7704's `0.1.62`: the next free version, `check-plugin-version.sh`),
  `agent/src/filament/invoiceImportPrompt.ts` (the `invoice_*` tools; every printed line
  goes on the invoice as an item line of any kind)
- Test: `agent/test/invoiceImportPrompt.test.ts`, `.github/scripts/lint-plugin.sh`

**Interfaces:**
- Produces (`SKILL.md`, R-P3-36, R-P3-44): the interim #7648 section (PR #7704) is replaced
  completely by "### Invoices": an invoice is imported, and a confirmed invoice that misses
  lines is reviewed, by the `invoice-import` flow started from the invoice page (`start_flow_run`
  with `invoice-import` and `mode`); a person answers each line's prompt. For ad-hoc work
  the `invoice_*` tools take lines of any kind (`item_id` from `inventory_item_list` `q`,
  or `proposed_item`; `kind` `stock`, `asset` or `expense`); the header keeps the printed
  totals; never leave a line off and never cut the header to the filament share; goods
  already received are adopted (`adopt_ids`), never received again; a delivery that
  differs from the bill sets `received_units`. It names "item types" and "stock"
  (R-P3-59) and cites #7648, spec §6.7 and `inventory/builtin_flow.py`.

- [ ] **Step 1: Write the failing tests**: the skill has no text of the interim section (no
  `inventory_lot_receive` outside an invoice, no `Not filament:` instruction); it names
  `invoice-import`; the import prompt names `invoice_get` and `invoice_upsert`, not the old
  names.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/invoiceImportPrompt.test.ts` and `.github/scripts/lint-plugin.sh` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `docs(plugin): the filament skill imports invoices through the flow (#<3b>, 5/5)` (body `Fixes #7648`)

### Task 24: Phase 3b docs

**Files:**
- Modify: `docs/features/inventory.md` (item types and stock, the filament view, parity,
  the flow), `docs/features/flows.md` (the built-in `invoice-import`), the filament feature
  page (its pages now under Inventory), `docs/architecture/frontend.md` (the routes and the
  redirect table of Task 15), `frontend/CLAUDE.md` (the `pages/inventory/filament/` and
  `components/inventory/purchasing/` layout lines), `agent/CLAUDE.md` (the `invoice_file`
  ref kind).
- [ ] **Step 1: Edit**, each fact with its source (file:line, PR or issue).
- [ ] **Step 2: Run** `grep -rn "/filament/\(types\|spools\|suppliers\|invoices\|locations\|shopping-list\|catalog\|metrics\|tags\|ledger\|scan\|labels\)" docs/features docs/architecture frontend/CLAUDE.md agent/CLAUDE.md plugins` — Expected: only the redirect table and historical specs and plans.
- [ ] **Step 3: Commit** `docs: the Filament section in Inventory and the invoice flow (#<3b>, 5/5)`

## Issues to file

- **#7068** is phase 3a (retitle: "inventory phase 3a: one item model for filament,
  generic purchasing tables and lines"). Layers A1-A6.
- **Phase 3b** (new, under epic #6949, blocked by #7068 layer A5): "inventory phase 3b:
  the Filament section in Inventory, item types at parity, the invoice-import flow".
  Layers B1-B5. Blocked by nothing in #7361 (A1 is on main); it gains from A2.
- **Generic item catalog** (new, under #6949, R-P3-60): a manufacturer list and a product
  lookup that new item types come from, as `filament_catalog_*` serves spool types.
- The follow-ups below, each filed when its layer merges.

## Follow-ups (issues to file when their layer merges)

- When A3 has run on prod and the catch-up (Task 9) reports 0 rows on two starts in a row:
  drop the views `filament_suppliers`, `filament_invoices`, `filament_invoice_lines`,
  `filament_locations`, `filament_location_kinds`, `filament_shopping_items`; drop
  `invoice_lines.supplier_product_id`, set `received_units NOT NULL`; drop the tables
  `filament_supplier_products` and `filament_price_points` (R-P3-63); drop the
  `invoices.file_*` columns (R-P3-14); drop the mirror columns
  `filament_spool_types.material_id`, `subtype_id`, `rgba`, `colour_name`, `diameter_mm`
  once no reader is left (R-P3-45, R-P3-46).
- After one release: remove the `/filament/invoices…`, `/filament/suppliers…`,
  `/filament/products…`, `/filament/prices…`, `/filament/locations…`,
  `/filament/shopping-list` and `/filament/spools/{id}/recost` route aliases; the
  `filament_invoice_*`, `filament_supplier_*`, `filament_product_*`, `filament_price_*`,
  `filament_location_*`, `filament_shopping_*` and `filament_spool_recost` tool aliases;
  the `spool_type:` and `spool:` ref aliases; the `/filament/*` UI redirects.
- Move `filament/invoices.py`, `invoice_money.py`, `suppliers.py`, `shopping.py` and the
  purchasing models into `inventory/` (R-P3-21).
- Phase 4 (#7069): printers as units, so a printer link is a `unit:` link.

## Questions for the owner

1. Colour and diameter (R-P3-46): they become item properties with the spool type's
   columns kept as a mirror for one release. Should the mirror columns go after that, or
   stay as the spool workflow's own fields?
2. The confirm in the flow (R-P3-51): the final prompt is the person's decision, and
   `invoice_amend` then waits for the usual outward approval unless a grant admits it. Is
   one decision enough (a standing grant for the built-in flow, as `order-fulfilment` has),
   or keep both?
3. Kind ids (R-P3-45): material and subtype kinds get ids such as `filament_pla` and
   `filament_pla_silk`; only their names show. Accept, or name a scheme?
4. Generic item catalog (R-P3-60): a follow-up, not phase 3. Confirm.
5. Phase 3b's issue: one issue with five layers, or one issue per layer?

## Rulings (R-P3, the planner's, where the spec leaves a choice open)

- **R-P3-1.** The three renames land in layer A1 (six with R-P3-49), with views under the old names for the
  rolling update (precedent `20261122T0426Z_entities.sql`). Reason: a mechanical rename is
  reviewed apart from the data move, and old pods keep working.
- **R-P3-2.** An offer keeps its product's id, and an offer price point its price point's id;
  lines gain `offer_id`, filled from `supplier_product_id`. Reason: alias routes, activity
  details and invoice lines stay valid with no map table.
- **R-P3-3.** A filament item keeps its spool type's id and is named by its `code`; a spool's
  unit keeps the spool's id. Reason: one id per thing; `code` is unique among unarchived
  types, as the item name must be.
- **R-P3-4** (changed by R-P3-30; owner accepted 2026-11-25 that creating a filament item
  takes the type's fields). A filament item takes every call a unit item takes:
  receive, status, cost, re-cost, offers. Receiving its units makes its spools. Its lot
  calls answer `WrongTracking`, as a unit item's do. Creating one takes the type's fields.
  Reason: the owner's ruling; the type fields are what the spool workflow needs.
- **R-P3-5** (replaced by R-P3-47; changed by R-P3-30; owner accepted 2026-11-25: the aliases last one release,
  then a follow-up removes them). `item:` and `unit:` refs of spool types and spools work.
  The migration moves the `spool_type` and `spool` entities, files and schemas to `item` and
  `unit`; `spool_type:` and `spool:` stay accepted as aliases for one release. Reason: one
  entity per thing, with no refusal.
- **R-P3-6** (changed by R-P3-30 and R-P3-40; owner accepted 2026-11-25 that a label weight
  change keeps the price paid). The unit carries the spool's cost in `cost_minor`, as
  every unit does. `filament_spools.cost_per_kg` is its per-kg form (cost over received
  grams, else label weight; R-P3-40, R-P3-64), kept for the spool workflow (Bambuddy, labels, summaries). `set_unit_cost_in` is
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
- **R-P3-18** (owner accepted 2026-11-25). Job cost before and after is read on prod just before and after layer A4
  deploys. Reason: the change is read-only, and prod data never leaves prod.
- **R-P3-19.** Shopping items take `item_id`; `material` becomes nullable under a CHECK.
  Reason: spec §6.5; a non-filament row has no material.
- **R-P3-20** (changed by R-P3-58: the product, price and price-sheet routes become
  aliases of generic offer routes). Supplier CRUD moves to `/inventory/suppliers`, with the `/filament` routes as
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
- **R-P3-25** (changed by R-P3-51: the invoice page starts the flow; the route stays for
  ad-hoc chat). The agent's invoice-import route keeps its path. Reason: only its prompt, its
  tools and a `mode` change.
- **R-P3-26.** Offer SKUs take 128 characters. Reason: filament products and invoice lines
  already do (`_SKU_MAX`).
- **R-P3-27.** The entities sweep skips, and a merge moves, an entity that a link names.
  Reason: as R-P2-4; a sweep must not fail on the link's foreign key.
- **R-P3-28.** The `inventory_moves` view applies the ledger's counting rules, and job cost
  reads spool uses from it. Reason: one implementation of "a plate counts once" serves
  reports and job cost.
- **R-P3-29** (owner, 2026-11-25; R-P3-63 says how). The layer-A3 data migration ships like any other PR: no
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
  `filament/invoices.py:571`), a note that starts with `Not filament:` (the interim #7648
  rule, PR #7704), and a person's flag; `has_file`, `imported` and `has_note` are shown,
  not filtered on. Reason: a cut header still reconciles on `total`, so only the subtotal,
  the tax, the interim note or a person can show a missing line.
- **R-P3-32.** `invoices.lines_review` (`flagged`, `done`) holds a person's flag and the end of
  a review; an amend sets `done`. Reason: the list must shrink as invoices are reviewed.
- **R-P3-33** (replaced by R-P3-51 and R-P3-52: the backfill is the flow's
  `missing_lines` mode, and pairing is plain code). The backfill is a `missing_lines` mode of the existing invoice-import session.
  It re-reads the stored `invoice` attachment, pairs printed lines with kept ones by SKU,
  description and amount, and proposes only the unpaired ones as item lines. Reason: the
  session already reads the file and knows the tools.
- **R-P3-34** (changed by R-P3-51: no stored proposal; a person decides in the flow's
  prompts; `invoice_amend` stays outward and gated for the agent). The proposal is stored on the invoice (`amend_proposal`) and writes no line.
  A person applies it from the invoice page; the agent's `invoice_amend` is `outward` and
  gated. Reason: the owner's rule that a person confirms each amend.
- **R-P3-35.** A backfill amend opens with "Re-spread over all lines" on, and its result lists
  each spool unit whose cost dropped with "Re-cost past uses". Reason: the old header spread
  tax and shipping over the filament lines only, so their spools' cost is overstated, and
  the re-cost carries the fix to past job cost.
- **R-P3-36.** The filament skill's invoice text makes a non-filament line an item line on
  the same invoice, with the printed header kept; layer B5 closes #7648. Reason: phase 3
  answers #7648's open question (R-P3-42).
- **R-P3-37** (owner, 2026-11-25: "we should be able to understand what we got 'for free'
  -- actual amount of filament or items. i.e. we bought 10 but got 11"). Every line keeps
  billed (`quantity × pack_size`) and received (`received_units`, as billed when not
  given). The price paid is spread over everything received, so the cost per unit falls
  and the total stays the price paid. The free part is received − billed; below 0 it is a
  short delivery and the cost per unit rises. Old rows get received = billed (layer A3).
  Reason: the owner's rule, for every item (R-P3-30).
- **R-P3-38.** The free part is a fact of the line, read through `invoice_line_id` from its
  lots and units; no lot or unit column holds it. Reason: one place, and all units of a
  line cost the same.
- **R-P3-39** (owner decided 2026-11-25 for a spool weighed after use). A spool's received
  grams are its measured net grams: the first `weighed` event by scale or person
  (`grams_after`), source `weighed`. When uses come before that weighing, received = its
  `grams_after` plus the grams the ledger counts for those uses (R-P3-28's rules), source
  `derived`, shown as "derived from weighing and prints". With no weighing, the label weight
  stays.
  Billed grams are units × label weight. Phase 3 reads this from the ledger on main and
  adds no weighing column or event; slice A of #7681 is optional. Reason: #7681 owns
  weighing (session scadbuddy-53's interface).
- **R-P3-40.** A spool's `cost_per_kg` is its unit cost over its received grams when known,
  else over its label weight; the first weighing rewrites it, and past uses move only by
  "Re-cost past uses". Reason: the price paid spread over what was got, as R-P3-37 does
  for units.
- **R-P3-41.** `filament_spools.unit_id` stays on the spool row, equal to its id and set for
  every typed spool. Reason: #7681's empty-weight resolver adds the carrier and the parts
  (#7278) through it.
- **R-P3-42.** #7648's open question (cut the header to the filament share, or add a "not
  filament" line kind) has the generic answer: every good is a line of the one line model,
  so a mixed invoice reconciles with its printed totals and nothing is cut. Lines, items,
  lots, units and spools show billed, received, free and the cost per unit or per kg.
  Reason: R-P3-30 and R-P3-37.
- **R-P3-43.** No double receive. A line's `adopt_ids` names lots or units of its item that
  were received outside an invoice (#7648's interim rule, or by hand); the line adopts them
  with no move, so `on_hand` does not change, and receives only the rest. Candidates rank
  them by the supplier's name and the order number in their note (the interim rule writes
  both), then by received date within 30 days of the invoice date. Reason: such lots may already exist on prod.
- **R-P3-44.** The interim #7648 skill section lands first in its own PR (#7704, plugin
  `0.1.62`); layer B5 replaces it completely and bumps the plugin past it. Reason: agents stop improvising before phase 3 ships.
- **R-P3-45** (owner, 2026-11-25, rule A.1). Material and subtype are child kinds of
  `filament` in `item_kinds`: `filament` → material → subtype, one subtype kind per material
  it is used with. A spool type's item has the subtype's kind, else the material's, else
  `filament`. The catalog rows stay as each kind's print, drying and handling defaults,
  reached through `filament_kind_catalog`; `material_id` and `subtype_id` on the type are a
  mirror for one release, written only by `ItemStore.set_kind_in`. Reason: the owner's
  rule; one classification for every item. Changes R-P3-4 (the type's fields no longer
  carry material and subtype).
- **R-P3-46.** Colour (`rgba`, `colour_name`) and `diameter_mm` are item properties typed by
  `filament`'s `attribute_schema`; the spool type's columns stay as a mirror written only by
  the property store's filament hook. Temperatures, label weight, insert, presets and K
  values stay spool type fields (R-P3-30). Reason: the owner allowed them as properties,
  and the workflow's many readers keep working (owner question 1).
- **R-P3-47** (owner, rule A.2; replaces R-P3-5). Entities of inventory things are `item:`,
  `lot:` and `unit:`. `spool_type` and `spool` leave `KINDS`; their entities, files, links,
  properties and schemas move to `item` and `unit` (Task 9), and `spool_type:` and `spool:`
  stay ref aliases for one release. `part` already left in phase 2
  (`20261124T0918Z_parts_to_items.sql:110`). The order kinds stay: attempt media are archive
  copies under `order_item:<id>` and `order_item_fulfilment:<id>`, and
  `OrderStore.archive_media_present`, `archive_media_attachments` and
  `attempts_missing_media` match those subject strings (`tests/orders/test_archives.py`,
  `test_attempt_files.py` pass unchanged). The filament catalog kinds (`material`,
  `subtype`, `brand` and the rest) stay on their rows. Reason: the owner's rule, limited to
  the inventory kinds.
- **R-P3-48** (owner, rule A.3). One line model with one list of line kinds (`stock`,
  `expense`, `asset`). A filament line is an item line whose item is a filament item.
  `type_id`, `proposed_type`, `supplier_product_id` and `update_spool_ids` are request
  aliases of `item_id`, `proposed_item` (with the type's fields), `offer_id` and `adopt_ids`
  for one release. Reason: the owner's rule; R-P3-30 and R-P3-42 already needed it.
- **R-P3-49** (owner, rule A.4). `filament_suppliers` → `suppliers`,
  `filament_locations` → `locations`, `filament_location_kinds` → `location_kinds`,
  `filament_shopping_items` → `shopping_items`, in layer A1, each with a view under its old
  name. Every reader moves in the same commit: the Bambuddy sync (`bb_sync_plan.py`,
  `bb_sync_results.py`, `bambuddy_pull.py`), codes `L-`, sensors, labels, capture, the CSV
  import, printers. `/inventory/locations` routes with `/filament/locations` aliases. D16
  holds: only filament (suppliers with a filament offer or line, filament offers and their
  prices, locations as today) reaches Bambuddy. Reason: the owner's rule; it overrides spec
  §6.4's "renaming it is left out".
- **R-P3-50** (owner, rule B). The Filament section moves into Inventory, as Task 15's table
  says; every old URL redirects for one release. The spool page is a filament unit's page
  and the type page a filament item's page. Settings stay in Settings (#7706's gate kept).
  #7681's weighing UI moves with the spool page; #7216's labels page moves to
  `/inventory/filament/labels`. Reason: the owner's rule ("everything").
- **R-P3-51** (owner, rule C; changes R-P3-25, R-P3-33 and R-P3-34). Invoice import and the
  backfill are the built-in flow `invoice-import` with modes `import` and `missing_lines`,
  registered as `order-fulfilment` is. The stored `amend_proposal` and the
  `invoice_amend_propose` tool are dropped: the person decides in the flow's prompts. The
  chat route and the `invoice_*` tools stay for ad-hoc use. Reason: the owner's rule; a
  prompt is a person's decision with its own record (`tasks.answered_by`).
- **R-P3-52.** The extract step is `agent()` (on main) with `result_schema` and an empty
  grant with `on_outside="deny"`, so it reads the file and writes nothing; the file reaches
  it through a new ref kind `invoice_file`. Pairing (SKU, description, amount) is plain code
  in the script and matching is the server's candidates route; neither is the LLM's.
  Reason: the owner's rule that matching is deterministic code.
- **R-P3-53.** Each person decision is a `prompt()` task with A1's schema subset: one per
  line (item type or new or skip, line kind, received, stock to adopt), then one final
  (confirm or leave as draft; amend or cancel, with respread). Choices are numbered,
  readable enum labels mapped back to ids by the script; the line goes in the title and the
  field descriptions because A1 does not draw context blocks. A2's `entity` widget and
  context blocks replace them later with the same answers. Reason: only A1 is on main.
- **R-P3-54** (owner, rule D). #7667 may move the backend to Go behind the same HTTP
  contract. Phase 3's routes are that contract; handlers hold no SQL; every statement is in
  a store; the flow, the tools and the UI call routes only. Reason: a later port changes
  stores, not callers.
- **R-P3-55** (owner, rule E). Phase 3 splits into 3a (data model, #7068, A1-A6) and 3b (the
  move, item types at parity and the flow, a new issue, B1-B5). Reason: one issue would be
  too large to review; 3a alone is useful through the aliases.
- **R-P3-56.** A material or subtype kind's name is the catalog's exact text and follows a
  rename. Every reader of `spool.material` and the 3DFP and Bambuddy matchers
  (`profile_fetch/wire.py` `ranked`, `site.py` `_choose`, `duplicates.py` `_agrees`,
  `bambuddy/filament_profiles.py` `ProfileMatch`, `filament_tdfp_search`,
  `filament_tdfp_create`) get the same text as before (tests in Tasks 8 and 10). Reason:
  those matchers compare plain text.
- **R-P3-57.** Phase 3 does not rename `filament_override_slots`, `printer_kprofile_notes`,
  `printer_slot_presets` (#7670) or `filament_location_sensors`; `label_sheets` (#7216) has
  a plain name already. Reason: printer and sensor workflow, out of scope.
- **R-P3-58** (owner, 2026-11-25: "The invoice experience for filament was great, but the
  implementation for the non spool is trash. We don't see invoice line items. We don't have
  price sheets etc."). Full parity: every purchasing and type feature that filament has
  serves every item through the same pages, routes and tools (the parity table). Per unit
  or per pack where filament shows per kg; filament keeps per kg as its kind's view. A
  hardware line on an invoice looks and works as a filament line does. Task 21 runs the
  same flow on both. Changes R-P3-20: the product and price routes become generic offer
  routes, with the filament routes as aliases. Reason: the owner's rule.
- **R-P3-59** (owner, 2026-11-25: "We don't have Item types"). An item is the type of its
  lots and units. The UI says "Item types" (and "Types" within a kind's view); lots and
  units together are "Stock"; "Lots" or "Units" where only one is meant. The filament view
  keeps "Spool types" and "Spools". Tools and docs use the same words: an
  `inventory_item_*` tool's description says "item type". Reason: the owner's rule; one
  word per thing.
- **R-P3-60.** A generic item catalog (a manufacturer list and a product lookup) is not in
  phase 3; it is a follow-up issue under #6949. Filament keeps its catalog under
  `/inventory/filament/catalog`, and materials and subtypes are kinds. Reason: phase 3 is
  large already, and no generic catalog source is chosen (owner question 4).
- **R-P3-61** (review of 9aeedfb7a, finding 1). `rewrite_in` changes only what moved: for
  each link it computes the amount, and where the link has a consumer of the same target it
  updates that row's amount in place and keeps its id; it inserts only for a new link or a
  new target and deletes only for a gone link or a share that rounds to 0. When an
  `order_cost_entries` row that `order_item_sources.cost_entry_id` names must be replaced,
  the same transaction points `cost_entry_id` at the new row. A repeat writes nothing.
  Reason: `cost_entry_id` has no FK, so a delete and insert left it dangling.
- **R-P3-62** (review finding 2). Task 1 renames `update_spool_ids` to `adopt_ids` (the view
  keeps the old name) and replaces its CHECK `cardinality(update_spool_ids) <= quantity`
  with `cardinality(adopt_ids) <= 1000`; `receive_line` refuses more adopted units than
  received (a line error). Reason: R-P3-7 counts units, not lines, and layers A1-A2 need
  the column.
- **R-P3-63** (review finding 3; refines R-P3-29). Every old pod during a layer's rollout
  keeps working: A1's renames are plain `SELECT *` views (auto-updatable, so the old pods'
  INSERT, UPDATE, `FOR UPDATE` and `ON CONFLICT` statements work, each tested); A3 keeps
  `invoice_lines.supplier_product_id`, a nullable `received_units` and the tables
  `filament_supplier_products` and `filament_price_points` (no longer written by new code),
  and the API runs `purchasing_catch_up_in` at every start, which copies what an old pod
  wrote after the migration. A read on an old pod may miss a row a new pod wrote during the
  rollout; no write is lost. The drops wait for the follow-up. Reason: R-P3-29's promise,
  kept for every rename.
- **R-P3-64** (review finding 4). A person's `cost_per_kg` on a spool becomes `cost_minor`
  over the same grams `cost_set` uses (`received_net_g`, else the label weight), so it
  survives its own round trip. Reason: one basis in both directions (R-P3-40).
