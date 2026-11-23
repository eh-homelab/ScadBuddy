# Inventory, purchasing and production: one model for every thing (design)

Status: draft for the owner's review, 2026-11-21. Brainstormed in session scadbuddy-06,
coordinated with the filament epic #6260 session (`fix-sync-status-review-findings`) and the
`bambuddy-sync-dry-bins` session.

## 1. Intent

ScadBuddy is the shop's ERP. Today it tracks the cost and stock of filament spools only, and
of order parts in a separate, simpler model. The owner wants one model for *every* thing the
shop buys, makes or uses:

- filament, printers, nozzles, hotends, belts, build plates, AMS parts, bearings, magnets,
  screws, desiccant, dry bins, packaging, tools;
- parts the shop prints in ScadBuddy from models, outputs or library files, for its own use
  (a jig, a tool) or to sell.

Every thing has a real cost that traces to an invoice line or to the print jobs and parts that
made it. Every thing can carry links (support pages, wiki, manuals, purchase links) and file
or media attachments. Job cost then reads real cost from one place.

Why: the owner wants to know what each order, each printed part and each tool really cost,
down to the bearing that went into it, and wants the purchasing, stock and support
information for the whole shop in one place.

## 2. Goals

- G1. **Arbitrary kinds.** A new kind of thing (a new printer part, a new consumable) is data,
  not a schema change. Kinds carry their own attributes.
- G2. **One catalog** of items, with per-supplier offers: SKU, purchase link, pack size and
  price history per supplier per item.
- G3. **Links on everything:** support, wiki, manual, video, datasheet and other URLs.
- G4. **Attachments on everything:** one attachments model for every entity. It replaces
  `order_attachments` and `print_attachments`.
- G5. **Stock with real cost.** Counted stock (bearings) in lots, each lot at its own cost.
  Tracked units (a printer, a nozzle, a dry bin) one by one, each with its cost and status.
  Filament spools keep their gram ledger.
- G6. **Generic invoices.** Any invoice line links to the items it bought and receives them
  into stock, at the line's allocated cost (subtotal + tax + shipping share, as the filament
  invoices do today). A line can also link to any other entity (a repair line to a printer).
- G7. **Orders draw from inventory.** Order parts become inventory items. Using one takes it
  from a lot at that lot's cost, and the cost entry traces to the lot and its invoice line.
  This replaces the `parts` model.
- G8. **Production.** A production run turns print jobs (filament grams at the spool's cost,
  machine time at the machine rate) plus consumed items (a bearing) plus labour into a stock
  lot or units of a produced item, at the run's total cost over the good quantity.
- G9. **Sell or use produced stock.** The storefront and orders can fulfil from stock as well
  as print to order. The shop can consume or put into service a produced item (a tool).
- G10. **Catalog import from Bambu Lab** for the spare parts that fit the shop's printers
  (H2C and H3C today).
- G11. **Assets carry value over life.** A tracked unit with a purchase cost and a life
  (hours or months) loses value linearly. `printer_cost_profiles` spreads a printer's purchase
  price over `life_hours` into the hourly machine rate; it keeps no book value, and this
  design adds one (§6.4).

## 3. Non-goals

- Double-entry books, a chart of accounts, tax reports or period close. An accounting export
  may come later (owner, 2026-11-21: a true cost per order and per item is the target).
- Lifespan telemetry and wear tracking themselves (hours on a nozzle, belt tension). Other
  epics own them. This design gives them a unit to attach to.
- Purchase orders and reorder automation (the shopping list stays as it is, item-scoped).
- Multi-currency stock. Stock is valued in the shop currency, as invoices are today
  (confirm refuses another currency). Each `parts` row has its own `currency` today; phase 2
  opens a lot at the part's unit cost only when its currency is the shop's, and opens a
  zero-cost lot flagged "cost unknown" otherwise.
- Spare parts for printer models the shop does not run.

## 4. Anti-goals

- A second or third stock model next to the new one. When this is done, `parts` and
  `filament_supplier_products` are gone and job cost reads one cost source.
- An allowlist of kinds or attributes in code. Kinds and their attribute schemas are rows.
- Base64 bytes anywhere. Files are blobs plus `{name, mediaType}` pointers (#2479).
- A sync that overwrites a person's edit (the filament rule: a person's edit beats a sync).

## 5. Constraints

- Only the CRUD service (the API process) holds SQL (CLAUDE.md "Database access", #5295).
  The Bambu import runs in a worker with a service credential and writes through CRUD routes.
- Migrations: new files only, one per change, per CLAUDE.md "Migrations (#491)".
- Every setting is overridable on the instance and every print/quote option is a remembered
  choice with a toggle (#6219).
- Filament epic #6260 has open PRs in the invoice, catalog and Bambuddy sync code (#6892,
  #6915, #6878, #6903 at 2026-11-21). Phases that touch that code (§9 phases 2 and 3) start
  after they merge, and the epic's session reviews this spec.
- The `bambuddy-sync-dry-bins` session keeps dry bins as `filament_locations` rows (kind "Dry
  box"). A bin as a thing bought is a unit that links to its location row (§6.4).
- Measured 2026-11-21 with Playwright on `us.store.bambulab.com`: the store is a Next.js app,
  not Shopify (`/products.json` and `/collections.json` answer 404). Each product page
  (for example `/products/y-axis-belt-r1`) carries schema.org `ProductGroup` JSON-LD with
  `productGroupID`, and per variant `sku`, `image`, `offers.price`, `priceCurrency`,
  `availability`. The page text has a "Compatibility" list (for example "R1") and an
  "Installation" paragraph that points to the Bambu Lab Wiki, often only its home page.
  The spare-parts collection (`/collections/spare-parts`) lists the products. Plain HTTP
  fetches from the dev machine got 402/404, so the import needs a real browser.

## 6. Design

### 6.1 Entities: one reference for everything

An **entity** is anything links, attachments and invoice-line links can point at: an item, a
lot, a unit, a spool, a printer, an order, an order item, a print run, a library file, a
model, a supplier, an invoice, a production run, a location.

- Table `entities (id uuid PK, kind text, ref text, UNIQUE (kind, ref), created_at)`.
  `kind` is a code-registered name (`item`, `unit`, `lot`, `spool`, `printer`, `order`,
  `order_item`, `print_run`, `library_file`, `model`, `supplier`, `invoice`,
  `invoice_line`, `production_run`, `location`, `print_archive`, …); `ref` is the owner
  row's id as text.
- An entity row is made lazily, the first time something attaches to the owner row
  (`ensure_entity(kind, ref)`), so no existing table needs a backfill.
- Deleting an owner row deletes its entity row in the same transaction (the owner store's
  delete calls `drop_entity`); attachments and links cascade from it. A housekeeping check
  reports entity rows whose owner is gone. `print_archive` is the exception: its `ref` is a
  Bambuddy archive id that no ScadBuddy store deletes, so those entities live until a person
  deletes their attachments, as `print_attachments` rows do today.
- The text form `kind:ref` (as `order_attachments.subject` uses today) is the wire form in
  routes and agent tools.

### 6.2 Attachments

- One table `entity_attachments` keyed by `entity_id`, with today's `order_attachments`
  columns (kind, role, filename, media_type, size_bytes, sha256, blob_key, caption,
  created_at, created_by). `role` becomes open text with known values (`reference`, `issue`,
  `finished`, `packed`, `timelapse`, `render`, `test_print`, `receipt`, `invoice`, `manual`, `photo`, `label`,
  `other`).
- `order_attachments` rows move over with all four subject kinds (`order`, `order_item`,
  `order_item_fulfilment`, `order_item_revision`) as entities. The table keeps a nullable
  `order_id` (FK `ON DELETE RESTRICT`, as today, so the blob holds stay safe) and the
  `attempt_item_id` / `fulfilment_id` columns, set only on rows that came from an order; `print_attachments` rows move over keyed by the print archive's entity, keeping
  `promoted_to` and the `archive_photo` / `library_file_id` source columns as nullable
  columns (they point at bytes held elsewhere instead of a blob). `order_items.
  delivered_attachment_id` repoints at the new table.
- The invoice file is held today in the blob store and referenced by columns on
  `filament_invoices` (`file_key`, `file_name`, `file_media_type`, `file_size`;
  `filament/invoices.py` `file_key`, l.178). In phase 3 it becomes an attachment with role
  `invoice` on the invoice's entity, and the invoice page's preview (#6889) reads it there.
  Phase 1 leaves it alone, because phase 1 touches no filament code.
- One UI component (`AttachmentsPanel`) and one route family
  (`/api/v1/entities/{kind}:{ref}/attachments`) serve every page. The existing order and
  print routes stay as thin aliases until their callers move.

### 6.3 Links

- Table `entity_links (id, entity_id, url, kind, title, note, position, source, created_at,
  created_by)`. `kind`: `support`, `wiki`, `manual`, `video`, `datasheet`, `firmware`,
  `purchase`, `other` (open text, known values listed). `source` is `manual` or the import
  that wrote it; a person's edit sets `source = manual` and the import never touches it again.
- Purchase links that belong to one supplier live on the supplier offer (§6.5), not here. A
  `purchase` link here is for a link with no supplier record.

### 6.3a Properties (#7026)

The owner decided on 2026-11-22 (#7026, comment on #6949): things carry **arbitrary
key-value properties**, not a column per identifier. A spool's lot number, a type's GTINs,
ASINs, manufacturer part number and product page, an empty spool's weight, a filament's TD.

- Table `entity_properties (entity_id, key, value jsonb, source, updated_at, updated_by)`,
  primary key `(entity_id, key)`, cascading from `entities` like links. Any registered entity
  kind takes them, so spools and spool types have them from the first PR, and items, lots and
  units (phase 2) use the same table with no migration.
- A key is `^[a-z][a-z0-9_]{0,63}$`; there is no list of allowed keys in code. A value is a
  string, a number, a boolean or a list of strings.
- **Schemas document, never restrict.** A kind's property schema (a JSON Schema object) types
  and describes the keys it knows; any other key is stored as given. A known key's value is
  checked against its subschema. A schema uses only an allowed subset of Draft 2020-12, so it
  can refuse no key and run no regex. At the top level: `type` (`"object"`), `properties`,
  `additionalProperties` (only `true`), `$schema`, `$comment`, `title`, `description`,
  `examples`. In each key's subschema (an object): `type` (`string`, `number`, `integer`,
  `boolean`, `array`, or a list of them with `null`), the annotations (`title`,
  `description`, `$comment`, `default`, `examples`, `deprecated`, `readOnly`, `writeOnly`),
  `enum`, `const`, the number bounds (`minimum`, `maximum`, `exclusiveMinimum`,
  `exclusiveMaximum`, `multipleOf`), `minLength`, `maxLength`, `format` (never `regex`),
  `items` (a subschema of a scalar type, with no `items`), `minItems`, `maxItems`,
  `uniqueItems`. The values of `enum`, `const`, `default` and `examples` are data. A schema
  with any other keyword is refused.
- Where the schema lives: for an entity kind registered in code (`spool`, `spool_type`, …),
  a row of `entity_property_schemas (kind, schema)`, seeded by migration and editable by a
  person. For an item (phase 2), its item kind's `attribute_schema`, which follows the same
  rule (a child kind inherits its parent's keys). In phase 3, when a spool type becomes an
  item of kind `filament`, that kind's schema starts as the `spool_type` row's.
- Well-known keys, documented in the seeded schemas and still plain properties: on a spool
  type `mpn`, `gtin` (list: spool and refill), `asin` (list of `marketplace:ASIN`),
  `manufacturer_url`, `empty_spool_weight_g`, `td`; on a spool `lot_number`.
- `source` is `manual` or the import that wrote the key (as for links): a person's write
  makes it `manual`, and an import never replaces a manual value. There is one row per
  entity and key, never one per source: an import's write to a key whose row is `manual` is
  skipped, not stored beside it. Imports that fill
  properties (invoice import, label photos, the 3DFP fetch, #7025) come after the first PR.
- Search: the filament lists and the agent's type and spool search match property values,
  so a scanned GTIN or a lot number finds its type or spool.

### 6.4 Kinds, items, lots and units

**Kinds** (`item_kinds`): `id` slug, `name`, `parent_id` (a tree: `part` > `nozzle`),
`tracking` (`lot` = counted stock, `unit` = one by one, `filament` = spools with a gram
ledger), `uom` (`each`, `g`, `m`, `ml`…), `attribute_schema` jsonb (a JSON Schema that documents and types the
item's properties and never restricts them, §6.3a; a child kind inherits its parent's keys), `life_basis` (`none`, `hours`, `months`; default
for its units), `builtin` bool, `archived_at`. Built-in kinds seeded by migration:
`filament`, `printer`, `part` (with children `nozzle`, `hotend`, `belt`, `build_plate`,
`ams_part`, `hardware`), `consumable` (`desiccant`, `packaging`, `lubricant`),
`equipment` (`dry_bin`, `tool`), `printed_part`, `assembly`, `service`. People add more.

**Items** (`items`): what a thing is, the catalog entry.
`id`, `kind_id`, `name`, `manufacturer`, `compatible_with text[]` (printer
model codes, for example `{H2C,H3C}`), `low_stock` numeric, `life_hours` / `life_months`
(defaults for units), `source` (`manual`, `bambu_store`, …) and `source_ref`,
`edited_fields text[]` (fields a person changed; an import skips them), `note`,
`archived_at`. An item's attributes, its manufacturer part number (`mpn`) included, are its
entity properties (§6.3a), not columns. A produced item also has a **recipe** (§6.8).

**Lots** (`stock_lots`), for `tracking = lot`: `id`, `item_id`, `location_id` (nullable FK
to `filament_locations`, which becomes the shop's location tree; renaming it is left out),
`received_qty`, `on_hand` (derived from moves, kept as a column for reads), `unit_cost`
(shop currency, numeric(20,6)), `origin` (`invoice_line`, `production_run`, `opening`,
`adjustment`) with `invoice_line_id` / `production_run_id`, `received_at`.

**Units** (`stock_units`), for `tracking = unit`: `id`, `item_id`, `serial`, `name`,
`location_id`, `status` (`in_stock`, `in_service`, `retired`, `scrapped`, `sold`),
`installed_in_unit_id` (a nozzle in a printer, a belt in an H2C), `cost`, `origin` and its
ids as for lots, `in_service_at`, `retired_at`, `life_hours` / `life_months` (override the
item's), `printer_id` (nullable link to `printers`, for a unit that is a printer),
`location_ref_id` (nullable link to the `filament_locations` row a dry bin *is*, as the dry
bins session proposed).

**Filament** (`tracking = filament`): a spool type is an item of kind `filament` (one item
per `filament_spool_types` row, linked by `filament_spool_types.item_id`); a spool is a unit
of it (`filament_spools.unit_id`). The gram ledger `filament_spool_events` stays the source
of grams, and `filament_spools.cost_per_kg` the source of a spool's cost: moving grams into
the generic move table would duplicate a ledger the filament epic is still changing, for no
gain in cost accuracy. Reports read filament through a view (§6.6).

**Value over life** (G11): a unit's book value (display and stock valuation only; job cost
gets a unit's cost through the machine rate, below) = `cost × (1 − used / life)`, floor 0, where
`used` is the unit's hours in service (`life_basis = hours`, from the telemetry epics; a unit
with no hours source shows no value) or the months since `in_service_at`
(`life_basis = months`). Printers keep using `printer_cost_profiles` for
the machine rate; phase 4 points a printer's profile `purchase_price` at its unit's cost so
there is one number.

**Installed parts and the machine rate** (no double count): a part unit installed in a
printer (`installed_in_unit_id` = the printer's unit) reaches job cost only through that
printer's machine rate. Phase 4 derives the printer's `printer_wear_parts` rows from its
installed units: a wear part whose name matches an installed unit's item takes the unit's
`cost` and `life_hours` and records it in a new nullable `printer_wear_parts.unit_id`, and a wear part with no installed unit keeps
its hand-entered cost. Installing a unit writes a `use` move with `entity_id` = the printer,
not an order cost. `printer_maintenance_costs.consumables` stays hand-entered; a consumable
item used on a printer (`use` move to the printer's entity) is shown beside it as the
measured figure, and phase 4 lets a person switch the rate to the measured figure.

### 6.5 Suppliers and offers

- `filament_suppliers` becomes `suppliers` (renamed in phase 3; same ids). Until then, phase
  2's offers reference `filament_suppliers` rows directly. `parts.supplier` text maps to
  supplier rows when `parts` migrates (phase 2).
- **Offers are split until phase 3.** Phase 2 creates `supplier_offers` for non-filament
  items only: `supplier_id`, `item_id`, `sku`, `url` (the purchase link for this item at this
  supplier), `pack_size`, `pack_uom`, `archived_at`. An item has any number of offers. In
  phase 3, when spool types get their items, the `filament_supplier_products` rows move into
  `supplier_offers` (their `type_id` mapped to the type's item) and the old table goes.
  Phase 2 changes no filament table.
- **SKU uniqueness:** unique per (supplier, `upper(sku)`, item), not per supplier. One SKU may
  cover several items (a colour bundle split into N spools of different types). Invoice
  matching by SKU then proposes every item that SKU covers, and the line splits per item.
- `filament_price_points` becomes `offer_price_points` (`offer_id`, price per pack
  numeric(12,4), currency, on_date, source `invoice|manual`, invoice_line_id), keeping the
  CHECK that ties `source = invoice` to `invoice_line_id`. Both of today's readers keep
  working through the rename: the orders price book import (`orders/spool_import.py`) and the
  type page's "Use as default cost".
- **Bambuddy sync** (`filament/bb_sync_*`, `filament/bambuddy_pull.py`, the `suppliers` row of
  the per-kind sync settings): the rename carries `bb_supplier_id` and every other Bambuddy
  link column, and reset-links keeps counting them. **Rule: only filament reaches Bambuddy.**
  A supplier is sent only when it has at least one filament offer or already has a
  `bb_supplier_id`; an offer or price point is sent only when its item is of kind
  `filament`. Bambuddy knows only filament, so a bearing offer must never be sent.
- The shopping list (`filament_shopping_items`) gains `item_id` and works for any item.
- Spool inserts (`filament_insert_types`, `empty_weight_g`) stay a filament catalog. If an
  insert later becomes an item (to buy cardboard inserts), the insert type gains an
  `item_id` and `empty_weight_g` stays where grams-left reads it.

### 6.6 Moves and cost

- `stock_moves`: `id`, `at`, `item_id`, `lot_id` or `unit_id`, `kind` (`receive`, `use`,
  `return`, `adjust`, `produce`, `transfer`, `sell`, `retire`, `scrap`), `qty` (signed),
  `unit_cost`, `amount` (qty × unit_cost), `reason`, and what it was for: `order_id`,
  `order_item_id`, `production_run_id`, `print_run_id`, `entity_id` (any other target, such
  as the printer a nozzle went into), `actor`.
- **Taking from lots:** FIFO by `received_at` unless the caller names a lot. A use that
  needs more than one lot writes one move per lot. Stock may go negative (as parts may
  today); a negative move takes the newest lot's cost and is flagged for review.
- **Cost is a snapshot at the time of the move.** A `stock_moves` row stores its
  `unit_cost`. A filament use event (`filament_spool_events` with ref_kind `print_run` or
  `bb_usage`) gains `cost_per_kg_at`, set from the spool's effective cost when the event is
  written. A later change to a spool's or lot's cost (an invoice confirm or amend, a person's
  edit) does not move past job cost by itself. The spool or lot page offers "Re-cost past
  uses", which re-snapshots its events or moves at the new cost and writes an activity entry
  that names the orders whose cost changed. Phase 3 backfills `cost_per_kg_at` on existing
  events from today's spool cost (on prod, all 110 spools have an invoice cost as of
  2026-11-21, per the filament epic session).
- **Filament** stays in `filament_spool_events`; view `inventory_moves` unions
  `stock_moves` with spool events at their `cost_per_kg_at`, for reports.
- **Job cost** (`orders/costs.py`) reads actual cost: parts from `stock_moves` for the item,
  filament from the attempt's spool uses at each spool's cost. Precedence for an attempt's
  filament: (1) the spool's `cost_per_kg`, (2) its type's `cost_per_kg`, (3) the quote's
  per-gram price, (4) the price book. Today the quote's price comes first (`costs.py`
  l.12-14); real cost now wins, because the quote is what was charged, not what was spent. Every
  spool on prod has an invoice cost, so rule (1) applies to every order and moves the cost
  figure on every recent order. The PR shows before/after job cost and margin for all orders
  of the last 30 days.

### 6.7 Invoices

- `filament_invoices` / `filament_invoice_lines` become `invoices` / `invoice_lines` (phase
  3). Tax and shipping spreading (`invoice_money.py`) are unchanged.
- A line gets `item_id` (or `offer_id`), `kind` (`stock`, `expense`, `asset`), and keeps its
  filament fields for filament lines. Confirming a line:
  - filament: as today (spools at `cost_per_kg`, `invoice_line_id`);
  - `lot` items: one lot of `quantity × pack_size` at `allocated_cost / units`;
  - `unit` items: `quantity` units at `allocated_cost / quantity` each, `in_stock`;
  - expense lines (`service`, shipping-only, a repair): no stock, the cost goes to the linked
    entities (below).
- `invoice_line_links (line_id, entity_id, share)` links a line to any entities (the printer a
  repair was for, the order a special purchase was for). `share` splits the line's cost
  between them (default equal). Who reads a share, by the linked entity's kind:
  - a printer (or its unit): a `printer_maintenance_events` row (task named by the line,
    the share in its `consumables` column), so the machine-rate actuals (#5774) count it;
  - an order or order item: an `order_cost_entries` row, kind `material`, source
    `invoice_line:<id>`;
  - any other entity: no cost consumer; the link is shown on both pages for reference.
- Order purchases (`order_item_sources` with purpose `purchase`) gain `invoice_line_id`. When a
  purchase is recorded with a line, its cost entry *is* the line's share: the purchase's
  `cost_entry_id` points at the `invoice_line:<id>` entry and no second entry is written. The
  customer charge (`order_charges` kind `purchase`) is unchanged.
- **Links after confirm.** A confirmed invoice keeps its money and lines frozen, as today.
  Its `invoice_line_links` can still be added, changed or removed. Each link remembers the
  consumer row it wrote (`maintenance_event_id` or `cost_entry_id`). A change deletes that
  row and writes a new one in the same transaction, so a reversal is always exact. A cost
  entry that a closed order already reported is still replaced, and the order's activity
  log records the change.
- **Amending a confirmed invoice.** The 9 confirmed invoices on prod are filament-only on
  purpose. For example, Micro Center 185-PO-519748 is $5,234.28 (an H2C, a laser module, an
  AMS 2 Pro, hotends and a protection plan), but only $171.12 of filament is on the invoice,
  and the full totals are in its note. Phase 3 adds **Amend**:
  - A person adds lines and re-enters the header's subtotal, tax and shipping to the full
    receipt. The reconcile check runs again.
  - On confirm, the new lines receive their stock or units.
  - **What an amend may change.** The already-confirmed lines keep their `allocated_cost`.
    Only the header tax and shipping that no line holds yet (the new header totals minus
    the sum already allocated) are spread, and only over the new lines. So adding a $4,000
    H2C line does not move the filament lines' shipping share, or their spools'
    `cost_per_kg`.
  - A person may instead choose "Re-spread over all lines". This re-allocates every line,
    updates the costs of the spools, lots and units that change, and then offers "Re-cost
    past uses" (§6.6) for each one that changed.
  - Each amend writes an activity entry with the old and new allocation for each line.
  - This is how the H2C's purchase price reaches its unit cost (phase 4).
- The assistant's invoice import (`scadbuddy:filament` skill, `filament_invoice_*` tools)
  becomes `invoice_*` tools that propose items for any kind, matching by supplier SKU, then
  MPN, then name.
- **Renamed paths and tools keep working.** `/filament/invoices/:id` (in
  `test-fixtures/filament/ui-paths.json` and the agent's `ui_path`, #6878) redirects to
  `/inventory/invoices/:id`. The fixture and `ui_path` change to the new path. The
  `filament_invoice_*` tools stay as aliases of the `invoice_*` tools for one release, then
  go. The `scadbuddy:filament` skill and the plugin are updated in the same PR, with the
  `plugin.json` version bump that `check-plugin-version.sh` requires.

### 6.8 Production

- **Recipe** (`recipes`): what makes one thing: an owner (a produced **item**, or a **model**
  slug for print-to-order work), the source to print (`model` slug + params/preset, an
  `output_id`, or a `library_file_id`; for a model-owned recipe, the model itself), the
  expected yield per print, and component lines (`recipe_components (recipe_id, item_id,
  qty, when jsonb)`: 2 × 608 bearing). `when` keeps today's colour conditions (the part
  chosen by the order item's colour).
- `model_part_rules` migrates to model-owned recipes, one per slug, its lines and `when`
  conditions kept with `part_id` mapped to the part's new item. A new order item copies the
  model's recipe components into `order_item_parts` (now `item_id`), as `rule_parts` does
  today. Print-to-order items do **not** become inventory items; only things made to stock
  have an item.
- **Production run** (`production_runs`): `item_id`, `recipe_id`, `planned_qty`, `good_qty`,
  `scrap_qty`, status (`planned`, `printing`, `assembling`, `done`, `cancelled`), its print
  runs (`production_run_prints`, linking ScadBuddy print runs), labour minutes.
  On `done`: it uses the components from lots (moves with `production_run_id`), totals
  filament (spool uses of its print runs at spool cost), machine time (machine rate), labour
  (the labour rate) and components, and receives `good_qty` into a new lot (or units) at
  `total / good_qty`. Scrap cost stays in the total.
- A run starts from an item's page ("Make 10") or from an order item. Printing uses the
  existing print flow; the run only links the print runs.

### 6.9 Using produced stock

- **Shop use:** a produced tool is either consumed (`use` move, `entity_id` = what it was
  for) or a unit put `in_service` (a jig). Either way its cost is known.
- **Orders:** an order item can be **fulfilled from stock**: pick the item (and lot), the
  fulfil writes a `sell` move, and the order's cost is the lot's cost instead of a print
  attempt. Print-to-order stays the default.
- **Storefront:** a listing may name a stock item (`storefront_listings.item_id`). The
  storefront shows availability from on-hand stock when the listing has one, and an order
  for it is fulfilled from stock when stock exists, else printed. Selling from stock or
  printing is a remembered choice with a toggle per listing.

### 6.10 Bambu Lab catalog import

- A worker on the profile-fetch pattern (#6625, #6780): its own Temporal workflow, no
  database secret, a service credential for CRUD routes, the browser in the browser-service
  pod with its security profile (`deploy/security/`).
- It walks `us.store.bambulab.com/collections/spare-parts` (and the printer accessory
  collections), opens each product page, reads the JSON-LD and the Compatibility list, and
  keeps a product only when its compatibility names a model in the setting
  `inventory_import_printer_models` (default: the models of the shop's printers, today
  `H2C`, plus `H3C` set by hand until the H3C is set up).
- For each kept variant: an item (kind guessed from the title: nozzle, hotend, belt,
  build plate, AMS part, else `part`; a person can change it), a Bambu Lab supplier offer
  (SKU = variant `sku`, URL = offer URL, price point = offer price on the run's date), the
  product image as an attachment, and a `support` link to any wiki URL on the page.
  `edited_fields` and `source` keep a person's edits.
- Runs on demand ("Import Bambu parts") and weekly, both settings.

### 6.11 UI and agent tools

- One **Inventory** area: items by kind (tree), item page (properties, offers with purchase
  links, links, attachments, lots/units with cost and location, moves, recipe, production
  runs), units page (status, installed-in, value over life), production runs, suppliers,
  invoices. Filament keeps its own pages; a spool page shows the same links and
  attachments panels. The Parts page (`/orders/parts`) redirects to Inventory.
- Agent tools mirror the routes: `inventory_item_*`, `inventory_kind_*`, `inventory_lot_*`,
  `inventory_unit_*`, `inventory_move_*`, `production_run_*`, `invoice_*`,
  `entity_attachment_*`, `entity_link_*`, `entity_property_*`, `inventory_import_bambu`. `part_list`,
  `part_stock_add` and `order_item_parts_set` become aliases, then go.
- Every entity reference in UI and tool results links to its page (the #6878 pattern).

## 7. Approaches considered

- **A. One item core; filament and parts move onto it (chosen).** One catalog, one stock
  model for lots and units, generic invoices; filament keeps its gram ledger as an extension.
  Largest change and it migrates live data, but it is the only one where job cost reads one
  cost source and nothing is modelled twice.
- **B. A new core next to filament and parts.** Cheaper now; leaves three stock models and
  three cost sources, which every report must join. Rejected: it works against "everything
  in one place".
- **C. An entity registry only** (attachments, links and invoice links on every row; no
  unified stock). Fast, but cost and stock stay split. Kept as phase 1 of A.

The owner did not pick between A and C explicitly; the designer chose A, phased so that C
ships first.

## 8. Decisions made by the designer (owner to check)

- D1. Filament grams stay in `filament_spool_events`; reports union them (§6.4, §6.6).
- D2. FIFO lot consumption by default; a caller may name the lot (§6.6). Parts' weighted
  average cost goes away; a lot keeps its own cost.
- D3. `filament_locations` stays the location table for all stock; no rename (§6.4).
- D4. Entity rows are lazy and deleted by the owner's store, not by triggers (§6.1).
- D5. Value over life is linear and shown only where a time or hours basis exists (§6.4).
- D6. Bambu import reads the US store only; other regions are out of scope.
- D7. Filament cost precedence: spool, type, quote, price book; real cost beats the quote
  (§6.6).
- D8. Installed part units reach job cost only through the printer's machine rate, by
  deriving `printer_wear_parts` from installed units (§6.4). Book value is display only.
- D9. An expense line's share becomes a printer maintenance event or an order cost entry,
  by the linked entity's kind; a purchase recorded with a line reuses that entry (§6.7).
- D10. Recipes are owned by an item or a model slug; colour conditions stay; print-to-order
  items get no inventory item (§6.8).
- D11. Parts in a non-shop currency open as zero-cost lots flagged "cost unknown" (§3).
- D12. Supplier offers stay split from `filament_supplier_products` until phase 3 (§6.5).
- D13. SKU is unique per (supplier, SKU, item); a bundle SKU maps to several items (§6.5).
- D14. Move costs are snapshots. A later cost change reaches past job cost only through
  "Re-cost past uses" (§6.6).
- D15. Invoice line links stay editable after confirm, with an exact reversal. A confirmed
  invoice can be amended to add lines. An amend spreads only the not-yet-allocated tax and
  shipping over the new lines; re-spreading over all lines is an explicit choice (§6.7).
- D16. Only filament suppliers, offers and prices sync to Bambuddy (§6.5).
- D17. Properties are rows of their own keyed by entity, not a jsonb column per owner
  table, so one route family, panel and search serve every kind, and a person's value
  wins over an import's for its key: one row per key, which an import skips once it is
  `manual` (§6.3a). Owner's rule, 2026-11-22: schemas never
  restrict keys.

## 9. Phases (each one or more PRs, each an issue under one new epic)

1. **Entities, attachments, links.** `entities`, `entity_attachments`, `entity_links`;
   move `order_attachments` and `print_attachments`; one panel and route family. Touches no
   filament code.
1b. **Properties** (#7026). `entity_properties`, `entity_property_schemas` seeded for
   `spool_type` and `spool`, routes, panel on the spool and type pages, search, agent
   tools, `properties` on the filament upsert tools. Independent of the filament PRs.
2. **Item core.** Kinds, items, offers on new tables, lots, units, moves, Inventory UI and
   tools; `parts` migrates to items + lots (opening lots at today's unit cost), order parts
   and `model_part_rules` repoint; job cost reads part cost from moves. Starts after the
   filament purchasing PRs merge, because offers replace `filament_supplier_products`.
3. **Generic purchasing.** Rename suppliers, offers, price points, invoices; non-filament
   invoice lines receive lots/units; `invoice_line_links`; order purchases link lines;
   invoice tools generalised; spool types and spools link to items/units; job cost reads
   spool cost.
4. **Units for equipment.** Printers as units (with `printers` and cost profiles linked),
   installed-in, value over life, dry bins linked to their locations.
5. **Production.** Recipes, production runs, receive produced lots, shop use.
6. **Sell from stock.** Order fulfil from stock, storefront listing stock and availability.
7. **Bambu import.** Worker, workflow, settings, weekly run.

## 10. Left to the builder

Route shapes and names beyond those above, index choices, the exact JSON Schema subset kinds
may use, the kind-guessing rules for imports, UI layout (load `frontend-design`), how the
attachments move is batched, and how the aliases are retired.
