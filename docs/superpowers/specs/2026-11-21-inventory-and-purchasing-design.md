# Inventory, purchasing and production: one model for every thing (design)

Status: draft for the owner's review, 2026-11-21. Brainstormed in session scadbuddy-06,
coordinated with the filament epic #6260 session (`fix-sync-status-review-findings`) and the
`bambuddy-sync-dry-bins` session.

Updated 2026-11-25 for the owner's rulings on #7068 (marked "(owner, 2026-11-25)"): one model
for spools and every other item, one kind tree, plain `locations` and `suppliers` tables, one
invoice line model with billed kept apart from received, the Filament section moved into
Inventory, the invoice import and the back-fill as flows, and full parity (§11). Phase 3
splits into 3a and 3b (§9).

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
- Purchase orders and reorder automation. The shopping list serves every item (owner,
  2026-11-25; §6.5); an auto-add puts a row on the list and orders nothing.
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
- **One kind per thing in stock (owner, 2026-11-25).** A thing the shop holds is an `item`
  (what it is) or a `unit` (one of it), or a `lot` (a counted batch). A spool type is
  `item:<id>` and a spool is `unit:<id>`, with the ids they have today. `spool_type:<id>` and
  `spool:<id>` are accepted as aliases for one release, then go (owner, 2026-11-25, ruling 5
  on #7068). The catalog kinds `material` and `subtype` go, because those rows become item
  kinds (§6.4). The order kinds stay. `supplier`, `invoice`, `invoice_line` and `location`
  stay as entity kinds of plain tables (§6.4, §6.5), because files, links and line links
  point at them; the owner is asked to check this reading (§11.4 Q6).

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
  `filament/invoices.py` `file_key`, l.178). In phase 3a it becomes an attachment with role
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
  `exclusiveMaximum`, `multipleOf`), `minLength`, `maxLength`, `format` (an annotation that
  checks nothing; never `regex`), `items` (a subschema of type `string`, with no `items`),
  `minItems`, `maxItems`, `uniqueItems`. `$schema`, when given, is the Draft 2020-12 URI.
  The values of `enum`, `const`, `default` and `examples` are data, never walked as
  keywords. A schema with any other keyword is refused, and so is one that leaves a key no
  value: an `enum` with no member a key can hold, a `const` a key cannot hold (null, an
  object), a lower bound past its upper bound, or a minimum length or count past the value
  caps (2000 characters, 50 strings of 500). The caps hold even where the key's `type` makes
  the bound inert: `{"type": "number", "minLength": 5000}` is refused, because
  `check_schema` (`backend/scadbuddy/entities/properties.py`, through `_check_bounds`)
  checks `minLength` and `minItems` whatever the type; `maxLength` and `maxItems` past the
  caps are not refused.
- Where the schema lives: for an entity kind registered in code (`spool`, `spool_type`, …),
  a row of `entity_property_schemas (kind, schema)`, seeded by migration and editable by a
  person. For an item (phase 2), its item kind's `attribute_schema`, which follows the same
  rule (a child kind inherits its parent's keys). In phase 3a, when a spool type becomes an
  item of kind `filament`, that kind's schema starts as the `spool_type` row's, and the
  `spool` row's keys move to the `filament` kind's unit keys (owner, 2026-11-25). The
  `spool_type` and `spool` rows of `entity_property_schemas` then go with their entity
  kinds (§6.1).
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

Amendment (owner, 2026-11-25, ruling 6 on #7068): **one kind tree.** Filament material and
subtype become child kinds of `filament`: `filament` > `PLA` > `PLA Matte`. Each
`filament_materials` row becomes a kind under `filament`, and each `filament_subtypes` row a
kind under its material (a subtype with no material goes under `filament`). A spool type is
an item whose kind is its subtype, else its material. The material's and subtype's values
(density, nozzle and bed temperatures, drying, `needs_enclosure`, `abrasive`,
`hygroscopic`, aliases) become the kind's property defaults, inherited down the tree. A
type's own value wins over its subtype's, and a subtype's over its material's, as today.
How a kind holds its defaults and aliases is left to the plan (§10). The Catalog tab's
Materials and Subtypes become the kind tree's `filament` branch. Colours, effects,
categories and inserts stay filament catalogs (§11.4 Q7).

Amendment (owner, 2026-11-23, ruling on the phase 2 plan
`docs/superpowers/plans/2026-11-23-inventory-phase-2-item-core.md`): `tracking` has a
fourth value, `none` ("not counted"). A `none` kind's items have offers and prices but
never lots, units or moves, and any move on such an item is refused. `service` is a
`none` kind.

**Items** (`items`): what a thing is, the catalog entry.
`id`, `kind_id`, `name`, `manufacturer`, `compatible_with text[]` (printer
model codes, for example `{H2C,H3C}`), `low_stock` numeric, `life_hours` / `life_months`
(defaults for units), `source` (`manual`, `bambu_store`, …) and `source_ref`,
`edited_fields text[]` (fields a person changed; an import skips them), `note`,
`archived_at`. An item's attributes, its manufacturer part number (`mpn`) included, are its
entity properties (§6.3a), not columns. A produced item also has a **recipe** (§6.8).

**Lots** (`stock_lots`), for `tracking = lot`: `id`, `item_id`, `location_id` (nullable FK
to `filament_locations`, which becomes the shop's location tree, renamed `locations` in
phase 3a; owner, 2026-11-25),
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

Amendment (owner, 2026-11-25, ruling 1 on #7068): **spools and other items are treated the
same; spools only have their own workflow.** The item and the unit are the shared model: a
spool type keeps its id as its item's id, and a spool its id as its unit's id. Every
purchasing and stock feature (offers, prices, invoice lines, receive, candidates, re-cost,
the shopping list, the lists and pages of §6.11) works on the item and the unit, so a
spool gets it as any unit does. The spool workflow is an extension of the unit: grams and
the spool event ledger, trays and AMS capture, tags, labels, presets, K values, drying, the
Bambuddy sync. The inventory calls the extension when it receives, retypes, changes the
status of or deletes a unit of a `filament` kind, and the extension writes the spool row in
the same transaction. A unit's cost is `stock_units.cost`; `filament_spools.cost_per_kg` is
its per-kg form, written only with it. Weighing stays on the spool ledger (#7681).

**Locations** (owner, 2026-11-25, ruling 6 on #7068): `filament_locations` becomes a plain
`locations` table (same ids, its sensors and printer places kept), and
`filament_location_kinds` becomes `location_kinds`. A place is not an item or a unit. A dry
bin bought is a unit that links to its place (`location_ref_id`, above). This replaces D3.

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

- `filament_suppliers` becomes `suppliers` (renamed in phase 3a; same ids). Until then, phase
  2's offers reference `filament_suppliers` rows directly. `parts.supplier` text maps to
  supplier rows when `parts` migrates (phase 2). A supplier is a plain table: not an item,
  not a unit, and not a filament row (owner, 2026-11-25, ruling 6 on #7068). Its stats
  (§11, row 18) count every item it sold; grams are the `filament` columns of them.
- **Offers are split until phase 3a.** Phase 2 creates `supplier_offers` for non-filament
  items only: `supplier_id`, `item_id`, `sku`, `url` (the purchase link for this item at this
  supplier), `pack_size`, `pack_uom`, `archived_at`. An item has any number of offers. In
  phase 3a, when spool types get their items, the `filament_supplier_products` rows move into
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
  Amendment (owner, 2026-11-25): it becomes `shopping_items`. A row names an item (or, as
  today, free text), a quantity in packs and an optional offer. A filament row's text
  (material, subtype, brand, colour) is read from its item. The filament auto-add (an empty
  spool adds its type, `filament_shopping_auto_add_empty`) becomes the auto-add of any item
  whose on-hand reaches its `low_stock`, a toggle per item, which orders nothing. That
  trigger is the designer's proposal (§11.4 Q3); a spool type keeps "a spool ran out".
  It moves to `/inventory/shopping-list` with the agent's `shopping_*` tools.
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
  that names the orders whose cost changed. Phase 3a backfills `cost_per_kg_at` on existing
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
  3a). Tax and shipping spreading (`invoice_money.py`) are unchanged.
- **One line model (owner, 2026-11-25, ruling 6 on #7068).** Every line, filament or not,
  has the same columns and the same confirm, candidates and amend paths. There are no
  filament line fields. A line has its description, SKU, `quantity` (packs billed),
  `pack_size` (the offer's, else 1), unit price, subtotal, tax, `allocated_cost`, `kind`
  (`stock`, `expense`, `asset`), `item_id` or `offer_id`, a `proposed_item` (a new item to
  make at confirm, with the fields its kind needs; a `filament` item needs the spool type's
  fields), `received_units` and `adopt_ids`. Today's filament fields map onto it:
  `type_id` → `item_id`, `supplier_product_id` → `offer_id`, `proposed_type` →
  `proposed_item`, `update_spool_ids` → `adopt_ids`. `colour_code` stays a line column,
  since a Bambu SKU's colour code names the item for any kind. Confirming a line:
  - `lot` items: one lot of the received units at `allocated_cost / received`;
  - `unit` items, `filament` included: one unit per received unit at
    `allocated_cost / received` each, `in_stock`; for a `filament` kind the spool
    extension makes each spool (§6.4) and writes its `cost_per_kg`;
  - adopted lots and units (`adopt_ids`: received by hand or by the interim rule of #7648
    before the invoice) take the line's cost and are not received again;
  - expense lines (`service`, shipping-only, a repair): no stock, the cost goes to the linked
    entities (below). Only a confirmed `expense` line's links write consumer rows; links on
    `stock` and `asset` lines are references, because the lot or unit already carries the
    cost (owner, 2026-11-25, ruling 3 on #7068).
- **Billed is kept apart from received (owner, 2026-11-25, ruling 4 on #7068).** A line keeps
  what was billed (`quantity × pack_size`) and what was received (`received_units`, as
  billed when not given). The price paid stays fixed. It is spread over everything received,
  so 11 units received on a bill for 10 cost `allocated_cost / 11` each, and the free unit
  is visible on the line, its lots and units, and the item. Fewer received than billed is a
  short delivery, and the cost per unit rises. For filament the same rule runs in grams:
  billed grams are units × label weight, and received grams are a spool's measured net
  grams (its first weighing in the spool ledger, which #7681 owns), else the label weight.
  A spool's `cost_per_kg` is its unit cost over its received grams. The free part is read
  from the line through `invoice_line_id`; no lot or unit column holds it.
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
  and the full totals are in its note. Phase 3a adds **Amend**:
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
- **The invoice import is a flow (owner, 2026-11-25, ruling 8 on #7068).** Today the
  invoice page's "Import" starts an assistant session with a prompt
  (`agent/src/routes/invoiceImport.ts`, `agent/src/filament/invoiceImportPrompt.ts`). It
  becomes a flow on the flows of epic #7361, with typed prompts (#7445: `prompt(title,
  schema)` opens a task and the run waits for the answer). An agent step reads the file and
  fills the draft's header and lines, with candidates for each line. Each choice that needs
  a person is a typed prompt: which item a line is (or a new item to make), which lots or
  units to adopt, the received count when it differs from the bill, and the confirm itself.
  A good that is not filament is made only at the person's yes, as a spool type is (owner,
  2026-11-25, ruling 9 on #7068). Who may answer follows the answerers design (#7471,
  `docs/superpowers/specs/2026-11-25-flow-answerers-design.md`). The flow works for every
  item; there is no filament-only import.
- **The back-fill is a flow too (owner, 2026-11-25, ruling 8 on #7068).** The confirmed
  invoices that miss their non-filament lines (filament-only on purpose, with the full
  totals in the note, as Micro Center 185-PO-519748 above) are listed by a review query:
  the header subtotal or tax differs from the sum of the lines by more than the reconcile
  tolerance, or the note starts "Not filament:" (the interim rule of #7648). For each one,
  the back-fill flow proposes an amend (the missing lines, the full header, the lots or
  units received by hand to adopt) and asks the person, as a typed prompt, to accept, edit
  or skip it. An accepted amend runs the amend above. Nothing changes without that yes.
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
  invoices. The Parts page (`/orders/parts`) redirects to Inventory.
- **The Filament section moves into Inventory (owner, 2026-11-25, ruling 7 on #7068).** The
  sidebar keeps one entry, Inventory. Each Filament tab becomes an Inventory page for every
  item, and the spool workflow shows where the item or unit is a `filament` one:
  - Types → **Item types** (`/inventory/types`): every item, with the Types tab's columns,
    search and filters; a kind filter of `filament` is today's Types tab. The type page
    (`/inventory/types/:id`, the item page) has the price panel, "Use as default cost", the
    cost calculator and the stock under the type (lots and units; spools for filament).
    The spool type's presets, K values and 3DFP panels show for a `filament` item.
  - Spools → **Stock** (`/inventory/stock`): lots and units across items; spools are its
    `filament` rows, with grams, trays and tags. A spool page is the unit page with the
    spool panels.
  - Suppliers, Invoices, Shopping list → `/inventory/suppliers`, `/inventory/invoices`,
    `/inventory/shopping-list`, for every item (§6.5, §6.7).
  - Locations → `/inventory/locations`: a place lists its lots, units and spools.
  - Catalog → the kind tree (`/inventory/kinds`) for materials and subtypes (§6.4); the
    filament catalogs that stay (§11.4 Q7) under `/inventory/catalog`.
  - Metrics → `/inventory/metrics`: stock, use and purchasing for every item, from
    `inventory_moves` (§6.6); the filament charts (grams, material numbers, forecast) as its
    `filament` view.
  - Ledger → `/inventory/moves`, spool events included through `inventory_moves`.
  - Tags, Labels and the phone page stay spool workflow pages, under `/inventory`.
  - Every `/filament/*` path redirects to its new path for one release; the
    `test-fixtures/filament/ui-paths.json` vectors and the agent's `ui_path` change with
    them. The phone intake (agreed 2026-11-25 with the intake session) adds no `/filament`
    route.
- **Parity (owner, 2026-11-25).** No purchasing or stock feature exists for filament only.
  §11 lists each one and where it lands. The phase 3b exit check is a parity test that runs
  the same invoice (upload, lines, candidates, confirm, amend) and the same price-sheet
  flow on a `filament` item and on a `hardware` item, and gets the same pages and tools.
- Agent tools mirror the routes: `inventory_item_*`, `inventory_kind_*`, `inventory_lot_*`,
  `inventory_unit_*`, `inventory_move_*`, `production_run_*`, `invoice_*`,
  `entity_attachment_*`, `entity_link_*`, `entity_property_*`, `inventory_import_bambu`. `part_list`,
  `part_stock_add` and `order_item_parts_set` become aliases, then go. Amendment (owner,
  2026-11-25): `supplier_*`, `offer_*` (with the price list and price sheet),
  `shopping_*`, `purchasing_metrics` and `cost_per_unit` serve every item. The
  `filament_supplier_*`, `filament_product_*`, `filament_price_*`, `filament_invoice_*`,
  `filament_shopping_*`, `filament_supplier_stats`, `filament_purchasing_metrics` and
  `filament_cost_per_kg` tools stay as aliases for one release, then go. The invoice
  import part of the `filament` skill moves to a `purchasing` skill for every item; the
  `filament` skill keeps the spool workflow.
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
  Replaced (owner, 2026-11-25, ruling 6 on #7068): it is renamed `locations`, a plain
  table, in phase 3a.
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
- D12. Supplier offers stay split from `filament_supplier_products` until phase 3a (§6.5).
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

Rulings of the owner, 2026-11-25 (on #7068), not the designer's:

- D18. Spools and other items share one model; spools only have their own workflow, an
  extension of the unit (§6.4).
- D19. One kind tree: filament material and subtype are child kinds of `filament`. The
  entity kinds of things in stock are `item` and `unit` (and phase 2's `lot`, §11.4 Q6);
  `spool_type:` and `spool:` are aliases for one release (§6.1, §6.4).
- D20. Locations and suppliers are plain tables, `locations` and `suppliers` (§6.4, §6.5).
- D21. One invoice line model; billed is kept apart from received; only `expense` lines
  write consumer rows (§6.7).
- D22. The invoice import and the back-fill of confirmed invoices are flows with typed
  prompts (§6.7).
- D23. The Filament section moves into Inventory, and no purchasing or stock feature exists
  for filament only (§6.11, §11).

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
   *Done in #7393, #7405, #7457, #7474, #7475 and #7476 (#7067, layers 1 to 6;
   see `docs/features/inventory.md`).*
3. **Generic purchasing.** Rename suppliers, offers, price points, invoices; non-filament
   invoice lines receive lots/units; `invoice_line_links`; order purchases link lines;
   invoice tools generalised; spool types and spools link to items/units; job cost reads
   spool cost.
   Amendment (owner, 2026-11-25): the rulings on #7068 and full parity (§11) make phase 3
   too large for one plan. It splits in two. Each part ships like any other PR; the data
   migration has no owner-run gate and no deploy window (owner, 2026-11-25, ruling 2 on
   #7068).

   **3a. The data model** (#7068). §11 rows 10, 11 (if Q2 is yes), 13, 20, 21, 26, 27, 28,
   32 (amend) and the data of 24 and 38:
   - the kinds: materials and subtypes become child kinds of `filament`; spool types become
     items and spools units, with their ids kept; the spool workflow as the unit's
     extension (§6.4);
   - the entity kinds: `spool_type:` and `spool:` move to `item:` and `unit:`, aliases for
     one release; the `material` and `subtype` kinds go (§6.1);
   - the plain tables: `locations`, `location_kinds`, `suppliers` (§6.4, §6.5);
   - one offer and price point table: `filament_supplier_products` and
     `filament_price_points` move into `supplier_offers` and `offer_price_points` (§6.5);
   - `invoices` and `invoice_lines` with the one line model, billed against received,
     `adopt_ids`, `invoice_line_links` and their consumers, amend, order purchases that
     link a line (§6.7);
   - the invoice file as an `invoice` attachment (§6.2);
   - `shopping_items` with `item_id` (§6.5);
   - `cost_per_kg_at` on spool use events, the `inventory_moves` view, and job cost that
     reads spool cost (§6.6, D7), compared before and after the deploy (owner, 2026-11-25,
     ruling 5 on #7068);
   - the migrations, each table an old pod reads kept as a view under its old name for one
     release; the routes under `/inventory`, with the `/filament` purchasing routes as
     aliases, so the filament UI and tools keep working until 3b;
   - Bambuddy still gets only filament (D16).

   **3b. The UI, the parity features and the flows** (a new issue under #6949, after 3a).
   §11 rows 1 to 6, 8, 14, 18, 19, 22 to 25, 29 to 31, 32 (the back-fill), 34, 35, 38
   (UI), 39, 46 and 47, plus whichever of Q1, Q3 and Q5 the owner says yes to:
   - the Filament section moves into Inventory, `/filament/*` redirects (§6.11);
   - Item types (list and type page), the stock list, bulk edit, copy;
   - suppliers, supplier pages and stats, products, the price panel and price sheet,
     purchasing and stock metrics, the moves page, locations with their stock;
   - the invoices list and page for every line, with candidates, confirm, amend, the file
     preview and the links to what a line made;
   - the shopping list for every item;
   - the invoice import flow and the back-fill flow, on #7445's typed prompts (§6.7);
   - the generic agent tools with the filament aliases, and the `purchasing` skill;
   - the exit check: the parity test of §6.11 on a `filament` item and a `hardware` item.

   Left as follow-ups unless the owner says otherwise: the forecast for lot items (Q4), a
   generic external catalog beyond the Bambu import of phase 7 (owner, 2026-11-25, on
   #7068: "either planned or filed as a follow-up").
4. **Units for equipment.** Printers as units (with `printers` and cost profiles linked),
   installed-in, value over life, dry bins linked to their locations.
5. **Production.** Recipes, production runs, receive produced lots, shop use.
6. **Sell from stock.** Order fulfil from stock, storefront listing stock and availability.
7. **Bambu import.** Worker, workflow, settings, weekly run.

## 10. Left to the builder

Route shapes and names beyond those above, index choices, the exact JSON Schema subset kinds
may use, the kind-guessing rules for imports, UI layout (load `frontend-design`), how the
attachments move is batched, and how the aliases are retired.

## 11. Parity with filament (gap analysis, 2026-11-25)

Why (owner, 2026-11-25, on #7068): "The invoice experience for filament was great, but the
implementation for the non spool is trash. We don't see invoice line items. We don't have
price sheets etc. We don't have Item types. We need parity." The rule that follows: no
purchasing or stock feature exists for filament only (owner, 2026-11-25).

How it was measured: read from the code on main at `4b2438ba0` (2026-11-25). The filament
side is `frontend/src/pages/filament/`, `frontend/src/components/filament/`,
`frontend/src/App.tsx` (routes l.127-145), `backend/scadbuddy/filament/`,
`backend/scadbuddy/api/filament.py`, `backend/scadbuddy/api/filament_purchasing.py`,
`agent/src/tools/filament*.ts`, `plugins/scadbuddy/skills/filament/SKILL.md` and
`docs/features/spools-and-filament.md`. The generic side is phase 2:
`backend/scadbuddy/inventory/`, `backend/scadbuddy/api/inventory.py`,
`backend/scadbuddy/api/inventory_stock.py`, `frontend/src/pages/inventory/`,
`agent/src/tools/inventory.ts` and `docs/features/inventory.md`.

### 11.1 Classes

- **(a) Purchasing or stock.** Every item must have it. A gap is planned work, with its
  generic form and a size: S (one task, a few files), M (one layer: store, route, UI and
  tool), L (more than one layer, or a data migration).
- **(b) The spool workflow.** It stays filament: grams, the spool event ledger, AMS trays,
  tags, presets, K values, drying, the Bambuddy sync. It hangs off the unit and the item as
  an extension (§6.4) and never forks the shared model.
- **(c) Unclear.** A question for the owner (§11.4).

### 11.2 The table

"Generic today" is what an item of any other kind has on main. "Phase" is where the gap
closes (§9).

| # | Feature | Filament today (where) | Generic today | Class | Generic form | Size | Phase |
|---|---|---|---|---|---|---|---|
| 1 | Types list: search, filters, columns, archive | Types tab (`pages/filament/TypesTab.tsx`; `GET /filament/types`) | Items list by kind tree, search (name, maker, property values), low stock, archived (`pages/inventory/InventoryPage.tsx`; `GET /inventory/items`) | (a) | "Item types": the items list with the Types tab's columns (stock, on hand, value, newest price) at `/inventory/types`; a spool type is a row of kind `filament` | M | 3b |
| 2 | Type page: details, stock under it, price panel, cost calculator | `pages/filament/TypePage.tsx`; `PricePanel.tsx` (each product's price history, per pack and per kg, a sparkline, "Use as default cost") | Item page: details, offers with the newest price only, "Add price", lots, units, moves (`pages/inventory/ItemPage.tsx`, `ItemOffers.tsx`, `ItemStock.tsx`). No price history in the UI, though `GET /inventory/offers/{id}/prices` exists | (a) | One type page for every item: the price panel per offer (per pack and per unit of measure), "Use as default cost", and its stock (lots and units, or spools) under it | M | 3b |
| 3 | Cost per kg calculator | `GET /filament/cost-per-kg` (`filament/cost_calc.py`); `components/filament/CostCalculator.tsx` | None | (a) | Cost per unit of measure: price, tax, shipping, packs, pack size (`GET /inventory/cost-per-unit`); per kg stays the filament form of it | S | 3b |
| 4 | Spools list: filter, sort, cards, tiles, CSV export | Spools tab (`pages/filament/SpoolsTab.tsx`, `SpoolsTable.tsx`, `SpoolCards.tsx`, `spoolsCsv.ts`) | Units and lots only on their item's page | (a) | A stock list across items (`/inventory/stock`): lots and units, with place, cost, status, CSV export; spools are its `filament` rows | M | 3b |
| 5 | Bulk create, copy | `POST /filament/spools/bulk`, `GET /filament/spools/{id}/copy` | Receive N units or one lot (`POST /inventory/items/{id}/units`, `/lots`) | (a) | "Copy" on an item and a unit | S | 3b |
| 6 | Bulk edit, archive, restore | `POST /filament/spools/bulk-update`, `bulk-archive`, `bulk-restore`; `components/filament/BulkBar.tsx` | None | (a) | Bulk edit, archive and restore of items and units, one request, all or none | M | 3b |
| 7 | CSV import | `POST /filament/spools/import` (`filament/csv_import.py`; `SpoolImport.tsx`) | None | (c) | Item and stock CSV import (Q5) | M | 3b or follow-up |
| 8 | Search by property values | Spools and Types tabs, agent (`lib/filamentSearch.ts`, `agent/src/tools/filamentSearch.ts`) | Items list matches property values (`inventory/items.py` l.113) | (a) | Done for items; the stock list (row 4) takes the same search | S | 3b |
| 9 | Search by tag UID | `filamentSearch.ts` hex match | n/a | (b) | — | — | — |
| 10 | Catalog: materials, subtypes | Catalog tab (`filament/catalog.py` `CATALOGS`; `pages/filament/CatalogTab.tsx`) | Item kinds tree with attribute schemas (`pages/inventory/KindsPage.tsx`) | (a) | Child kinds of `filament` (owner, 2026-11-25; §6.4): the material's and subtype's values become the kind's defaults, inherited down the tree | L | 3a (data), 3b (UI) |
| 11 | Catalog: brands | `filament_brands`, with aliases | `items.manufacturer` text | (c) | A shared manufacturers table (Q2) | M | 3a or follow-up |
| 12 | Catalog: colours, effects, categories, inserts, colour sync | `filament_colours`, `colour-syncs`, `filament_insert_types` | n/a | (b) | — | — | — |
| 13 | Catalog: location kinds | `filament_location_kinds` | Lots and units take a place (`pages/inventory/PlaceSelect.tsx`) | (a) | Move with the plain `locations` table (§6.4) | S | 3a |
| 14 | Locations: tree, place page with what is there | Locations tab, `/filament/locations/:id` (`LocationPage.tsx`: spools only) | A lot or a unit names a place; no page lists them | (a) | `/inventory/locations/:id` lists the lots, units and spools at the place | S | 3b |
| 15 | Sensors, drying, AMS places | `filament/sensors.py`, `dry_hours.py`, `locations.py` `ensure_printer_on` | n/a | (b) | — | — | — |
| 16 | Codes and QR labels | `L-` and `S-` codes (`codes/resolve.py` `PREFIXES`), `/filament/labels`, label PDF (`filament/label_pdf.py`) | None for items, lots or units | (c) | Codes and labels for units and lots (Q1) | M | 3b or the intake epic |
| 17 | RFID and NTAG tags, the phone page | `filament/tags.py`, `/filament/scan`, NFC jobs | n/a | (b) | The phone intake (agreed 2026-11-25 with the intake session) resolves a tag to any entity page; the tag formats stay filament | — | — |
| 18 | Suppliers list with stats | Suppliers tab (`SuppliersTab.tsx`; `GET /filament/suppliers/stats`: spools, grams left, grams used, spend) | None; offers pick a supplier from the filament list (`ItemOffers.tsx` l.174) | (a) | `/inventory/suppliers`: products (offers) per supplier, spend, items and units received, stock value; grams stay as the `filament` columns | M | 3b |
| 19 | Supplier page: details, products, add price | `/filament/suppliers/:id` (`SupplierPage.tsx`, `ProductsTable.tsx`, `AddProductSheet.tsx`) | A section on the filament supplier page lists the other offers (`SupplierPage.tsx` l.295) | (a) | One supplier page: every offer, any kind, with "Add product" for any item | M | 3b |
| 20 | Supplier products (SKU, URL, pack) | `filament_supplier_products` (`filament/suppliers.py`) | `supplier_offers` (`inventory/offers.py`), non-filament only | (a) | One offer table (§6.5) | M | 3a |
| 21 | Price points with history | `filament_price_points`; `GET /filament/types/{id}/prices` | `offer_price_points`, manual only; no history view | (a) | One price point table; source `invoice` from a confirmed line for every item | M | 3a (data), 3b (UI) |
| 22 | Price sheet: newest, previous, change | `GET /filament/suppliers/{id}/price-sheet`; `PriceSheet.tsx` | None | (a) | Price sheet per supplier for every offer; change per pack and per unit of measure | S | 3b |
| 23 | Invoices list: status and supplier filter, remembered view | Invoices tab (`InvoicesTab.tsx`, `invoicesQuery.ts`) | None | (a) | `/inventory/invoices`; `/filament/invoices` redirects | S | 3b |
| 24 | Invoice from a file or photo | `InvoiceUpload.tsx`; `POST /filament/invoices/{id}/file` | None | (a) | The same upload; the file is an `invoice` attachment (§6.2) | S | 3a (data), 3b (UI) |
| 25 | Invoice page: header, reconcile, totals | `InvoicePage.tsx`, `InvoiceHeaderForm.tsx`, `InvoiceTotals.tsx`; `filament/invoices.py` `_recompute`, `invoice_money.py` | None. Non-filament lines live only as a note on the invoice (the interim rule, #7648, #7704) | (a) | One invoice page at `/inventory/invoices/:id` | M | 3b |
| 26 | Invoice lines editor | `InvoiceLinesTable.tsx`, `invoiceLines.ts`; a line takes a type, a product, a proposed type, spools to fill | None | (a) | One line model for every item (§6.7): item or offer, a proposed item, `kind` (`stock`, `expense`, `asset`), packs billed and units received, links to entities | L | 3a (data), 3b (UI) |
| 27 | Line candidates | `GET /filament/invoices/{id}/candidates` (`LineCandidates`: uncosted and costed spools of the line's colour) | None | (a) | Candidates for every line: items by offer SKU, then MPN, then name; and lots or units with no invoice line that the line can adopt | M | 3a |
| 28 | Confirm | `POST /filament/invoices/{id}/confirm`: makes or fills spools at `cost_per_kg` | None | (a) | One confirm: lots, units (spools are units) or consumer rows by line kind (§6.7) | L | 3a |
| 29 | Links to what a line made | Confirmed lines list their spools (`InvoiceLine.spool_ids`); a spool links its line (`SpoolPurchasing.tsx`) | A lot has an `invoice_line` origin column, unused | (a) | A line lists its lots, units and linked entities; a lot or unit page shows "bought on" its line, with unit cost | S | 3b |
| 30 | File preview | `InvoiceFile.tsx` (PDF frame, photo zoom, open and download) | None | (a) | Reads the `invoice` attachment | S | 3b |
| 31 | Invoice import (assistant) | `POST /api/v1/ai/filament/invoice-import` (`agent/src/routes/invoiceImport.ts`, `agent/src/filament/invoiceImportPrompt.ts`); `InvoiceImport.tsx`; the `filament` skill | None | (a) | A flow with typed prompts (§6.7) for every item | L | 3b |
| 32 | Amend a confirmed invoice; back-fill | None (spec §6.7, D15) | None | (a) | Amend (§6.7); the back-fill of the confirmed invoices as a flow (§6.7) | L | 3a (amend), 3b (flow) |
| 33 | Bambu colour code lookup | `GET /filament/bambu-colour-codes` | n/a | (b) | — | — | — |
| 34 | Purchasing metrics | `GET /filament/metrics/purchasing` (`metrics_store.py` l.335: price per kg per type per supplier per currency; spend by month and supplier); Metrics tab | None | (a) | Price per unit of measure per item per supplier, and spend by month, supplier and kind | M | 3b |
| 35 | Stock metrics and summary | `GET /filament/summary`, `GET /filament/metrics` (stock, value, use, waste by material, brand, type, supplier or place) | On hand per item only | (a) | Stock value and use by kind, supplier and place from `inventory_moves` (§6.6) | M | 3b |
| 36 | Forecast (run-out date) | `GET /filament/forecast` (burn rate per spool and type; `filament/metrics.py`) | None | (c) | Run-out date for lot items from `use` moves (Q4) | M | follow-up |
| 37 | Material number stats | `GET /filament/metrics/material-numbers` | n/a | (b) | — | — | — |
| 38 | Shopping list | `filament_shopping_items` (`filament/shopping.py`; `ShoppingListTab.tsx`): by type or text, spools, purchased, clear; auto-add of an empty spool's type (`filament_shopping_auto_add_empty`) | None | (a) | One list for every item (`item_id`, packs, an optional offer); `/inventory/shopping-list`; the auto-add for any item at its low-stock level (Q3) | M | 3a (data), 3b (UI) |
| 39 | Ledger, history, timeline | `filament_spool_events`; Ledger tab, `GET /filament/spools/{id}/history`, `SpoolTimeline.tsx` | Moves table per item (`ItemStock.tsx`); `GET /inventory/moves` | (a) for moves; (b) for the spool ledger | A moves page across items (`/inventory/moves`), with spool events through `inventory_moves` | S | 3b |
| 40 | Low stock | `filament_low_stock_pct` (grams left) | `items.low_stock`, "Low stock only" filter | (a) | Done for items; a spool type's low stock reads its spools' grams | — | — |
| 41 | Properties, files, links | `PropertiesPanel.tsx` on spool and type pages | The same panels on the item page | (a) | Done | — | — |
| 42 | Type proposals for untyped spools | `filament/type_proposals.py` | n/a | (b) | — | — | — |
| 43 | Presets, K values, 3DFP, profile fetch | `filament/sb_presets.py`, `profile_fetch.py`, `tdfp.py` | n/a | (b) | — | — | — |
| 44 | Bambuddy sync and pull | `filament/bb_sync_*.py`, `bambuddy_pull.py` | Never (D16) | (b) | Only `filament` items, their suppliers, offers and prices reach Bambuddy (D16) | — | — |
| 45 | Weights, insert weight, consumed counter | `filament/insert_weights.py`; `POST /filament/spools/reset-consumed` | n/a | (b) | Weighing stays on the spool ledger (#7681) | — | — |
| 46 | Agent tools, purchasing | `filament_supplier_*`, `filament_product_*`, `filament_price_*`, `filament_invoice_*`, `filament_shopping_*`, `filament_supplier_stats`, `filament_purchasing_metrics`, `filament_cost_per_kg` (`agent/src/tools/filamentPurchasing.ts`, `filament.ts`) | `inventory_offer_*`, `inventory_offer_price_add` (`agent/src/tools/inventory.ts`) | (a) | `supplier_*`, `offer_*`, `invoice_*`, `shopping_*`, `purchasing_metrics`, `cost_per_unit`; the filament names stay as aliases for one release | M | 3b |
| 47 | The skill | `plugins/scadbuddy/skills/filament/SKILL.md` "Import an invoice" | None (the interim rule is #7704) | (a) | A `purchasing` skill for every item; the `filament` skill keeps the spool workflow | S | 3b |

### 11.3 What phase 2 shipped short of this spec

Phase 2 shipped what §9 phase 2 named. These parts of the spec it did not reach, read from
the code at `4b2438ba0`:

- **No price history in the UI.** G2 asks for price history per supplier per item. The
  route exists (`GET /inventory/offers/{id}/prices`); the item page shows only the newest
  price (`pages/inventory/ItemOffers.tsx` `priceText`).
- **No supplier home for items.** The item page picks a supplier from the filament list
  (`ItemOffers.tsx` l.174). A supplier is created only on the filament Suppliers tab. The
  non-filament offers show as one section of the filament supplier page.
- **The place tree is not the shop's yet.** §6.4 makes `filament_locations` the shop's
  location tree. A lot or a unit names a place, but the place page lists spools only
  (`pages/filament/LocationPage.tsx`).
- **No `inventory_moves` view.** §6.6 unions `stock_moves` with spool events for reports.
  It is not on main; `docs/features/inventory.md` moves it to phase 3.
- **No codes or labels for items, lots or units.** `codes/resolve.py` `PREFIXES` knows `L`
  and `S` only (see Q1).
- **Invoices still make no stock for items.** That is phase 3 by design, but meanwhile
  non-filament lines are kept as a note on the invoice and received by hand (#7648, #7704).

### 11.4 Questions for the owner (class c)

- **Q1. Codes and labels.** Do units and lots get codes (for example `U-<n>` for a unit) and
  printed QR labels, as spools and places do? The designer's proposal: yes for units and
  lots, in 3b or in the phone intake epic, so a tap or a scan opens any entity page.
- **Q2. Brands.** Do filament brands become one manufacturers table that every item's
  `manufacturer` names, or stay a filament catalog? The designer's proposal: one table in
  3a, since a brand is not filament-specific (Bambu Lab sells nozzles too).
- **Q3. The shopping list's auto-add.** Filament adds a type when a spool runs out. For an
  item, is the trigger "on hand at or under `low_stock`", as a toggle per item? §3 keeps
  reorder automation out; this adds a list row and orders nothing.
- **Q4. The forecast.** Must lot items get a run-out date from their `use` moves, or is
  that a follow-up?
- **Q5. CSV import and export.** Do items and stock get a CSV import, as spools have?
- **Q6. Entity kinds.** The designer reads "entity kinds become `item:` and `unit:` only"
  as: `spool_type`, `spool`, `material` and `subtype` go; `lot`, `supplier`, `invoice`,
  `invoice_line` and `location` stay as entity kinds, because files, links and line links
  point at them. Is that right?
- **Q7. The other catalogs.** Colours, effects, categories and inserts stay filament
  catalogs (b). Is that right, or must any of them serve every item?
