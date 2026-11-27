# Inventory

## What it does

The **Inventory** section holds everything the shop keeps except filament spools (those wait
for phase 3, #7068). It covers:

- item kinds, a tree of what things are (build plate, AMS part, hardware and more);
- items, the catalogue entries, with key-value properties, files and links (#7026);
- supplier offers for an item, each with a history of pack prices;
- stock in lots (counted) or in units (one by one), changed only by moves;
- a job's parts: an order item takes its parts from the lots when it is fulfilled, and job
  cost reads what they cost.

This is phase 2 of the inventory and purchasing design (#7067, epic #6949). The design is
[the inventory spec](../superpowers/specs/2026-11-21-inventory-and-purchasing-design.md)
(§6.4 to §6.6 and §9). The plan is
the phase 2 plan, `docs/superpowers/plans/2026-11-23-inventory-phase-2-item-core.md`
(on the `docs/7067-inventory-core-plan` branch). The plan's rulings are cited as R-P2-n (the controller) and "Ruling n" (the owner).

## How it works

All of it lives in the API process. No other process reads or writes these tables (root
`CLAUDE.md`, "Database access"). The code is `backend/scadbuddy/inventory/`, registered as
`InventoryComponent` (`backend/scadbuddy/inventory/component.py:25`). The routes are
`backend/scadbuddy/api/inventory.py` and `inventory_stock.py`.

### Kinds and tracking

An item kind is a row in `item_kinds` (`backend/scadbuddy/migrations/20261124T0323Z_inventory_core.sql:13`).
A kind has a parent, a unit of measure (`uom`), a service-life basis (`none`, `hours` or
`months`), a `saleable` flag and an `attribute_schema`. The schema documents and types the
properties of its items. It never restricts them (spec §6.3a). A child kind inherits its
parent's keys; its own subschema for a key wins (`inventory/kinds.py:1-5`).

Each kind has a `number` from the sequence `item_kinds_number_seq`
(`20261126T0218Z_inventory_class.sql`, step 1a). Its code is `K-<number>`, which
`GET /api/v1/codes/{code}` resolves (`codes/resolve.py` `PREFIXES`). A number is never
reused. The kinds that existed before that migration keep their text ids and got numbers
in `created_at`, then `id` order. A kind that ScadBuddy makes has the id `k<number>`. An id
of `k` and digits must be `'k' || number` (`item_kinds_made_id_check`), so a person cannot
choose one (`KindInput`, 422).

`tracking` says how a kind's items are counted:

| Tracking | Meaning | Stock |
|---|---|---|
| `lot` | counted stock | lots and moves |
| `unit` | one by one | units and moves |
| `filament` | spools with a gram ledger | none yet; items and offers of this kind are refused until phase 3 (Ruling 9, `FilamentItemRefused`, `inventory/items.py:32`) |
| `none` | not counted | none; any stock call is 409 `item_not_counted` (Ruling 1, spec §6.4) |

The migration seeds the built-in kinds (`20261124T0323Z_inventory_core.sql:189`): filament,
printer, part (with nozzle, hotend, belt, build plate, AMS part and hardware under it),
consumable (with desiccant, packaging and lubricant), equipment (with dry bin and tool),
printed part, assembly, service and spool part. A built-in kind cannot change its tracking.
A kind with items cannot either (`KindInUse`, `inventory/kinds.py:41`). Kinds are rows, never
a list in code (spec §4). A kind archives and restores; it never deletes.

An item's property schema is its kind's `attribute_schema`. So `GET` and `PUT
/entity-kinds/item/property-schema` answer 409 `schema_owned_by_item_kind` (R-P2-5, R-P2-6;
`backend/scadbuddy/entities/problems.py:123`).

### Items and properties

An item (`items`) has a kind, a name, a maker, a `compatible_with` list, a low-stock level,
a service life, a `saleable` override (null takes the kind's), a note and an archive time.
The name is unique per kind among unarchived items (index `items_kind_name`). A person's
edit is recorded in `edited_fields`, so an import skips it. The list takes `limit` (1 to
5000, default 1000; R-P2-17) and filters by kind (a kind and its descendants), search text
and low stock.

An item's attributes are entity properties, files and links (`entities/`): the item, lot and
unit are entity kinds (`entities/registry.py`). `part` is not (R-P2-18).
`saleable` is stored and shown only. Refusing to sell is phase 6 (#7071).

### Offers and price points

An offer (`supplier_offers`) is one item at one supplier, with a SKU, a link and a pack size.
A SKU is unique per supplier and item, not per supplier, so one SKU may cover several items,
such as a bundle (D13). The supplier is a `filament_suppliers` row until phase 3 renames it.
Offers are for non-filament items only until phase 3 (D12, Ruling 9; `inventory/offers.py:1-5`).

A price point (`offer_price_points`) is a pack price in any ISO currency (R-7067-11, Ruling 11).
It has a date and a source (`invoice` with its invoice line, or `manual`). Phase 2 writes
`manual` points only. The newest by date is the offer's price. The table already has the
final shape that the filament price points move into in phase 3 (Ruling 4).

Routes: `POST /inventory/offers`, `PATCH /inventory/offers/{id}`, `POST
.../archive`, `GET/POST /inventory/offers/{id}/prices`, `GET /inventory/items/{id}/offers`
and `GET /inventory/suppliers/{id}/offers` (`api/inventory.py`).

### Lots, units and moves

A **lot** (`stock_lots`) is a batch of a `lot` item: a received quantity, an `on_hand` that
the moves derive, a unit cost, a cost currency and an origin. `on_hand` may go below zero
(spec §6.6). A **unit** (`stock_units`) is one thing of a `unit` item: a serial, a status
(`in_stock`, `in_service`, `retired`, `scrapped`, `sold`), a cost and a service life.
Install, value over life and printers as units are phase 4 (#7069); the columns exist.

A **move** (`stock_moves`) is the only thing that changes stock. Its `qty` is signed. Its
kinds are `receive`, `use`, `return`, `adjust`, `produce`, `transfer`, `sell`, `retire` and
`scrap`; phase 2 writes the first four and the unit ones. A move holds a lot or a unit, never
both (`stock_moves_one_holder`).

- **FIFO.** `StockStore.take` takes from the lots in receive order (`stock_lots_fifo`;
  `inventory/stock.py:142` `_fifo`). A move that names a lot takes that lot first. If the lot
  holds less, the rest spills FIFO into the other lots. A named lot with nothing on hand is
  409 `lot_empty` (R-P2-14). A shortfall goes on the newest lot at its cost and sets
  `needs_review`. An item with no lot opens a zero-cost `adjustment` lot of unknown cost
  (`_open_lots`).
- **Snapshots.** A move keeps the unit cost it had when it was written (D14). A later change
  of a lot's cost moves no past move. The `stock_moves_amount` check holds
  `amount_minor = round(qty * unit_cost_minor, 6)` (R-P2-1). Money is in the shop currency
  (`orders_currency`), in minor units, unrounded (`numeric(20,6)`).
- **Reversal.** `give_back` writes one `return` for each `use` of an order item that no
  `return` names yet: the same lot, the same quantity, the use's own cost. Only a return
  names a move it reverses, and each move is reversed once (`reverses_move_id` is unique
  and checked). A second call writes nothing (`inventory/stock.py:557`).
- **Count.** `count_item` zeroes the negative lots first. Then it takes the difference FIFO,
  or adds it to the newest lot. No lot keeps an offsetting negative balance (R-P2-15).
- **Re-cost.** `POST /inventory/lots/{id}/recost` ("Re-cost past uses") rewrites the cost
  of every `use`, `return` and `adjust` move of the lot to the lot's cost now. It answers
  each order item whose parts cost changed, with both figures (R-P2-31; `recost_lot`,
  `inventory/stock.py:435`). It is tier `write` (Ruling 8). The orders layer audits it as
  `item.parts_recosted`.
- **The open lock and the lock order.** Two callers that both find no lot must not both open
  one. Every lot insert (`receive_in`, `_open_lots`) first takes a transaction advisory lock
  on the item (`_open_lock`, `inventory/stock.py:819`). `lock_items` takes, for each item in
  `item_id` order: the item row `FOR SHARE`, the open lock, then the item's lots `FOR UPDATE`
  in FIFO order. `take` locks the lots in the same order; it relies on its caller's
  `lock_items` for the open lock first. So no receive or count adds a lot
  under a caller that holds `lock_items` (R-P2-32), and a bulk fulfil of several items takes
  no lock out of order (R-P2-28).
- **Bounds.** No input reaches SQL unchecked (R-P2-16). A quantity or an amount past
  `numeric(20,6)` is 422 `bad_quantity` (a fulfil answers 409, an estimate 422; R-P2-30).
  An `each` item takes whole numbers only.

Routes: `GET/POST /inventory/items/{id}/lots`, `GET/PATCH /inventory/lots/{id}`, `POST
.../adjust`, `POST .../recost`, `POST /inventory/items/{id}/use`, `.../count`, the same for
units with `POST /inventory/units/{id}/status`, and `GET /inventory/moves`
(`api/inventory_stock.py`).

### Unknown and foreign costs

A lot's cost is *unknown* when nobody gave one (`cost_unknown`, unit cost 0). A lot received
at a cost in a currency other than the shop's opens with its cost unknown in the shop's
currency, as a part in another currency was uncosted before (R-7067-11). The lot keeps what
the person entered:

- `cost_currency` is the currency the lot was bought in (NULL means the shop's);
- `foreign_unit_cost_minor` is one unit's cost in that currency (R-P2-11, R-P2-12;
  `receive_in`, `inventory/stock.py:276`). `set_lot_cost` keeps both, for provenance.

The parts migration does the same for a foreign part (R-P2-22). A move of a `cost_unknown`
lot is `cost_unknown`, and so is a foreign lot's take (R-P2-19). The `/parts` unit cost
averages the known lots only, and `cost_unknown` is set when a lot is unknown. The UI shows
"unknown", never 0 (R-P2-35). A person sets the cost on the lot page; that clears the flag
for later moves, and "Re-cost past uses" carries it to the past ones.

### Job cost's `parts` figure

An order item's parts are in `order_item_parts` (now by `item_id`); a model's rule gives
them to a new item (`model_part_rules`). When `accept_fulfilment` or `fulfil_items` makes
an item `fulfilled`, it takes each part from the lots (`parts.used`). `unfulfil_item` gives
each use back at its own cost (`parts.returned`). No cost entry is written (see
[orders-and-quotes.md](orders-and-quotes.md)).

Job cost reads the moves. `PartsMoved` (`inventory/stock.py:903`) is `-sum(amount_minor)`
of the known-cost `use` and `return` moves, and `unknown` is true for a `use` of unknown
cost that no `return` names (R-P2-19). The figure is `Margin.parts_minor` (on `ItemCost` too)
(`backend/scadbuddy/orders/costs.py:183`). Job cost and the profit report share one reader
(R-P2-31). The quote estimate prices parts at what a FIFO take would cost now
(`estimate_unit_cost`, Ruling 10); it returns the cost of the known share and
`parts_unknown` (R-P2-29), and it is 0 and unknown outside the shop currency (R-P2-33).
The filament precedence in job cost (D7) is phase 3.

### Nothing reaches Bambuddy (D16)

Only filament reaches Bambuddy. Inventory code never calls it. One check protects the
suppliers: `bb_sync_plan._suppliers()` keeps a supplier only if Bambuddy has an id for it,
or it has a filament product (archived ones count), a filament invoice (a spool's purchase
source, Decision 14), or an open create intent (`filament/bb_sync_plan.py:1664`; R-P2-8 to
R-P2-10). A supplier made for item offers, or from `parts.supplier`, never reaches
Bambuddy. This is the one change phase 2 makes to the filament sync, because spec §6.5 states
the rule. Phase 2 changes no filament table.

### The `/parts` aliases

The part routes are aliases of the inventory for one release (spec §6.11):
`GET/POST /parts`, `PATCH /parts/{id}`, and its `stock`, `adjust` and `moves`
(`backend/scadbuddy/api/parts.py`, `inventory/part_alias.py` `PartAliases`). Each carries
a description that names it an alias. A part is an item of kind `hardware`, its pack and
price its newest offer and price point. They read and write through `StockStore`, so the
two views never differ.

- A part with no supplier has no offer. It reads as a pack of 1 at 0. A pack on a create or an
  edit is 422 with loc `supplier` (R-P2-34). Add stock takes the body's price and writes no
  price point (R-P2-25).
- The alias currency is the currency of the offer's latest price point, else the newest lot's
  `cost_currency`, else the shop's (R-P2-36).
- The agent's `part_list` and `part_stock_add` are aliases of `inventory_item_list` and
  `inventory_lot_receive` (`agent/src/tools/orders.ts:1572`).

**They go after one release.** That means: delete the `/parts` row routes, `PartAliases`
and the two agent tools (their `coverage.ts` entries too); keep `GET /orders/{id}/parts`,
`PUT /orders/{id}/items/{item}/parts` and the rule routes. Also drop `parts_retired` and
`part_stock_moves_retired` after a prod check (Ruling 6; the pack data of a part with no
supplier stays only there, R-P2-25). The `part_id` reads in `PartStore` go with them.

### The migrations

`20261124T0323Z_inventory_core.sql` makes the tables below and seeds the kinds.
`20261124T0918Z_parts_to_items.sql` moves the parts (see
[data.md](../architecture/data.md)). It runs in one transaction under `LOCK TABLE ...
IN EXCLUSIVE MODE` (R-P2-20): an old pod fails on its next fulfil after the rename. A part
keeps its id as its item id when that is a uuid. Each part becomes an item of kind
`hardware` (Ruling 2) with an opening lot at its unit cost. Its stock moves become
`stock_moves`. A part with a supplier gets an offer and a price point (Rulings 4 and 5). The
`part:` cost entries that the moves replace are deleted (Ruling 3). A costed use whose cost
entry a person had removed converts at cost 0 (R-P2-21). A `part` entity that names no part
stops the migration (R-P2-24).

## The agent tools

`agent/src/tools/inventory.ts` has 18 tools. Each answers a `ui_path` beside an id
(`agent/src/tools/inventoryLinks.ts`; the vectors are shared with the frontend in
`test-fixtures/inventory/ui-paths.json`). Reads are `read`; every write is `write`,
including `inventory_lot_recost` (Ruling 8). None reaches Bambuddy.

| Family | Tools |
|---|---|
| Kinds | `inventory_kind_list`, `inventory_kind_upsert` |
| Items | `inventory_item_list`, `inventory_item_get`, `inventory_item_upsert`, `inventory_item_archive` |
| Offers | `inventory_offer_list`, `inventory_offer_upsert`, `inventory_offer_price_add` |
| Lots | `inventory_lot_receive`, `inventory_lot_update`, `inventory_lot_recost` |
| Units | `inventory_unit_receive`, `inventory_unit_update`, `inventory_unit_status` |
| Moves | `inventory_move_list`, `inventory_move_use`, `inventory_move_count` |

`order_item_parts_set` sets an order item's parts by item.

## The UI

The sidebar entry is **Inventory**. The routes are in `frontend/src/App.tsx:147-151` and
[frontend.md](../architecture/frontend.md); the code is `frontend/src/pages/inventory/`.

- `/inventory` (`InventoryPage`): a kind tree on the left, the items on the right, with
  search, "Low stock only", "Show archived" and a New item form. The model part rules are
  on this page too.
- `/inventory/kinds` (`KindsPage`): the kind tree, with add, edit, archive and restore, and
  an editor for a kind's keys.
- `/inventory/items/:id` (`ItemPage`): the item's details, its Offers (`ItemOffers`, with
  "Add price"), its Stock (`ItemStock`: receive, use, count, units and their status, the
  moves table), and the Files, links and properties panel.
- `/inventory/units/:id` (`UnitPage`) and `/inventory/lots/:id` (`LotPage`): a lot's path
  opens its item's page at the lot's row, where its cost, "Re-cost past uses" and its
  result show.
- `/orders/parts` redirects to `/inventory?kind=hardware` (`PARTS_KIND`,
  `frontend/src/App.tsx:158`). The parts are the lot items under `hardware` and `ams_part`
  (`PART_KINDS`, `frontend/src/lib/inventoryLinks.ts`), which inventory K1 put under
  `consumable`.

The mock is `frontend/src/mocks/features/inventory.ts`.

## Code

- Backend: `backend/scadbuddy/inventory/` (`models.py`, `kinds.py`, `items.py`, `offers.py`,
  `stock.py`, `part_alias.py`, `problems.py`, `component.py`); routes `api/inventory.py`,
  `api/inventory_stock.py`, `api/parts.py`.
- Agent: `agent/src/tools/inventory.ts`, `inventoryLinks.ts`.
- Frontend: `frontend/src/pages/inventory/`, `frontend/src/lib/inventoryLinks.ts`.

## Data

Eight new tables (`20261124T0323Z_inventory_core.sql`): `item_kinds`, `items`,
`supplier_offers`, `offer_price_points`, `stock_lots`, `stock_units`, `stock_moves`, and the
map `part_item_map` that the second migration adds. The full list, with the retired
`parts_retired` and `part_stock_moves_retired`, is in
[data.md](../architecture/data.md).

## Tests

- Backend: `backend/tests/inventory/` (`test_kinds.py`, `test_items.py`, `test_offers.py`,
  `test_stock.py`, `test_schema.py`) and the parts and job cost tests under
  `backend/tests/`.
- Agent: `agent/test/inventory.test.ts`, `agent/test/inventoryLinks.test.ts`.
- Frontend: `frontend/src/pages/inventory/*.test.tsx`, `frontend/src/mocks/features/inventory.test.ts`.

## Limits and known issues

- Spool parts (#7278) wait for phase 3 (#7068). `spool_part` stays seeded (Ruling 7).
- Filament items, shopping lists, the `inventory_moves` view and spool cost in job cost
  are phase 3 (#7068). Install and value over life are phase 4 (#7069). Production is
  phase 5 (#7070). Selling from stock is phase 6 (#7071).
- `model_part_rules` become model-owned recipes in phase 5 (#7070). Phase 2 repoints the
  rules in place.
