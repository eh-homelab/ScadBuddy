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

This is phase 2 of the inventory and purchasing design (#7067, epic #6949). Phase K1 (#6949)
moved its base model into Go: the kinds, items, lots, units and moves. The K1 design is
[the kinds and handlers spec](../superpowers/specs/2026-11-25-inventory-kinds-and-handlers-design.md).
Its plan rulings are cited as R-K1-n. The phase 2 design is
[the inventory spec](../superpowers/specs/2026-11-21-inventory-and-purchasing-design.md)
(§6.4 to §6.6 and §9). The plan is
the phase 2 plan, `docs/superpowers/plans/2026-11-23-inventory-phase-2-item-core.md`
(on the `docs/7067-inventory-core-plan` branch). The plan's rulings are cited as R-P2-n (the controller) and "Ruling n" (the owner).

## How it works

Since K1, scadbuddy-core (Go) writes the kinds, items, lots, units and moves
(`tools/go/core/internal/inventory`, area `inventory`). The Python API is a forwarder. It reads
over core's CRUD routes (`SCADBUDDY_CORE_CRUD_URL`; unset, a read is 503
`inventory_unavailable`) and writes by starting a Temporal standalone activity on the queue
`inventory`. Python keeps the offers and price points (`OfferStore`, until K3) and the
reads that job cost needs (`stock.py`). The routes are `backend/scadbuddy/api/inventory.py`
and `inventory_stock.py`. The code is `backend/scadbuddy/inventory/`, registered as
`InventoryComponent` (`backend/scadbuddy/inventory/component.py:26`).

### A write is an activity

Each write is one activity of core's worker, named `inventory.<Name>` (19 names, listed in
`tools/go/core/README.md`, "The inventory worker"). The activity id is
`inventory.<Name>:<scoped key>` (Python `core_client.activity_id`, Go `store.ActivityID`; both
run the vectors in `tools/go/core/internal/inventory/store/testdata/activity_ids.json`). The key
is the request's `Idempotency-Key`. Go keeps it in `crud_requests` under the caller
`inventory`, so an activity that runs twice writes once (R-K1-2). A route waits 15 s for the
activity. If it is not done, the route answers 202 with `OperationAccepted`, and the
frontend follows `GET /api/v1/inventory/operations/{id}` (R-K1-24; the agent tool is
`inventory_operation_get`). A repeat with the same key is never an error. If Temporal
cannot start the activity, the route answers 503 and nothing was written.

An offer write is not an activity: Python's `OfferStore` writes it (#8437). An offer's add,
edit, archive and price add take an `Idempotency-Key` too. The store keeps the reply in
`crud_requests` under the caller `inventory`, in the write's own transaction, with a key
that starts with `offer:` and names the sender (`inventory/offers.py` `offer_key`). A retry
with the same key gets the first reply and writes nothing. The same key with another body is
a 422. A refused write keeps no reply. A write with no key is a write of its own. The
agent's `inventory_offer_upsert` and `inventory_offer_price_add` send one key per write of a
call, the same on each retry of that call.

Go writes one `inventory_operations` row for each activity in its own transaction. It then
sends `inventory_operation.changed` on `scadbuddy_events`. This is a typed event kind with
no log row (NOTIFY only; `tools/go/core/internal/bus/event.go`,
`backend/scadbuddy/agent_changes/hub.py`). Python's `ActivityIndex.ingest_inventory_operation`
turns it into an `activity` row (R-K1-22). An attempt that fails with an error stays
`running` while Temporal retries it. An error that Temporal does not retry (a bad actor) ends
the row `failed` with `inventory_activity_failed`, since no attempt follows to end it
(`tools/go/core/internal/inventory/activities/record.go`, #8256).

A finished row (`succeeded` or `failed`) stays 8 days after its end. Then the next start of
an activity deletes it (`tools/go/core/internal/inventory/store/operations.go`
`pruneOperations`, #8438). A `running` row is never deleted. The 8 days are longer than the
7 days that `crud_requests` keeps an `inventory` key, so a replay of the key and a read of
its operation still find the row. The `activity` row stays after its source row is gone.

### The class and the steps

A kind answers a **class** with four keys (`tools/go/core/internal/inventory/model/class.go`,
design §3): `nature` (`physical` or `service`), `role` (`consumable` or `durable`), `counting`
(`lot` or `unit`) and `billing` (`term`, `hourly` or `flat`). A child kind may answer a key
its parent left open. It may not change a key its parent set (`class_changed`). A durable
thing is counted by unit (`class_invalid`). An item of a kind whose class is open is refused
(`class_open`). A kind also names at most one special type. The Go catalog has two: `filament_spool` and
`printer`. The handlers of `printer` are `handler_not_built` in K1, so the seeded `printer`
kind keeps `special_type` NULL until K6 (R-K1-7).

A kind, an item and a unit can set `steps`: which handler runs each step (receive, count,
consume, and so on). The registry resolves the steps from the class defaults down to the
unit (`internal/inventory/handlers`, `handlers.yaml`). Go ported each default from phase 2
(R-K1-1). A step whose handler K1 does not build is 409 `handler_not_built`; a step with a
missing required parameter marks the kind or item incomplete (`incomplete_steps`), and
calling it is 409 `step_incomplete` (R-K1-8). The parameter history starts in K1: Go
publishes `handler_config.changed` and a subscriber writes `handler_config_history`
(R-K1-13).

The new problem codes replace phase 2's (R-K1-23): `item_not_counted` became
`step_not_applicable`, `wrong_tracking` became `wrong_counting`, and a consume of a durable
is `handler_not_built`. `unit_installed` and `filament_item_refused` stay.

The public contract only grew (R-K1-6). Kinds and items gain the class keys, `special_type`,
`steps` and `incomplete_steps`. `tracking`, `life_basis`, `life_hours`, `life_months` and
`compatible_with` stay, and Go derives `tracking` and `life_basis` from the class. The model
kinds are found by name under the parent `printer` (R-K1-11).

### Kinds and tracking

An item kind is a row in `item_kinds` (`backend/scadbuddy/migrations/20261124T0323Z_inventory_core.sql:13`).
A kind has a parent, a unit of measure (`uom`), a service-life basis (`none`, `hours` or
`months`), a `saleable` flag and an `attribute_schema`. The schema documents and types the
properties of its items. It never restricts them (spec §6.3a). A child kind inherits its
parent's keys; its own subschema for a key wins (`inventory/kinds.py:1-5`).

Each kind has a `number` from the sequence `item_kinds_number_seq`
(`20261126T0218Z_inventory_class.sql`, step 1a). Its code is `K-<number>`, which
`GET /api/v1/codes/{code}` resolves (`codes/resolve.py` `PREFIXES`). An item has its own
code, `I-<number>` (#7169), which the same route resolves. A kind's number is never
reused. The kinds that existed before that migration keep their text ids and got numbers
in `created_at`, then `id` order. A kind made without an id, and a model kind that Go makes
for a new `compatible_with` code, get the id `k<number>`. The seeded kinds keep their text
ids. An id of `k` and digits must be `'k' || number` (`item_kinds_made_id_check`), so a person
cannot choose one (`KindInput`, 422; owner, 2026-11-27).

`tracking` says how a kind's items are counted:

| Tracking | Meaning | Stock |
|---|---|---|
| `lot` | counted stock | lots and moves |
| `unit` | one by one | units and moves |
| `filament` | spools with a gram ledger | none yet; items and offers of this kind are refused until phase 3 (Ruling 9, `FilamentItemRefused`, `inventory/stock.py`) |
| `none` | not counted | none; a receive or a count is 409 `step_not_applicable`, and a use is 409 `handler_not_built` (a service's consume step is `consume.service`, which K1 does not build; phase 2 answered `item_not_counted` to all three) (Ruling 1, spec §6.4) |

The migration seeds the built-in kinds (`20261124T0323Z_inventory_core.sql:189`): filament,
printer, part (with nozzle, hotend, belt, build plate, AMS part and hardware under it),
consumable (with desiccant, packaging and lubricant), equipment (with dry bin and tool),
printed part, assembly, service and spool part. A built-in kind cannot change its tracking, its class or its special type, and neither can a
kind with items (409 `kind_in_use`, Go `store/kinds.go`). Kinds are rows, never
a list in code (spec §4). A kind archives and restores; it never deletes.

An item's property schema is its kind's `attribute_schema`. So `GET` and `PUT
/entity-kinds/item/property-schema` answer 409 `schema_owned_by_item_kind` (R-P2-5, R-P2-6;
`backend/scadbuddy/entities/problems.py:124`).

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

Since #6949 K1 (Task 16) Python writes no stock row. scadbuddy-core (Go) writes every lot,
unit and move, and the orders reach it through Temporal workflows
(`backend/scadbuddy/workflows/order_stock.py`: `FulfilOrderItem` calls `inventory.Take` and
`inventory.GiveBack`, `RecostLot` calls `inventory.Recost`, `MergePrinter` calls
`inventory.Repoint`). `backend/tests/test_no_python_stock_writes.py` keeps it so. The rules
below are phase 2's, which Go ported; the Python names in them (`StockStore`, `take`,
`lock_items`, `count_item`, `_open_lots`) are phase 2's code. It lives now only in
`backend/tests/support/python_stock.py`, the tests' stand-in for Go; `inventory/stock.py`
keeps `estimate_unit_cost` and `PartsMoved`. The line numbers below are in that file unless
they name another.

A **move** (`stock_moves`) is the only thing that changes stock. Its `qty` is signed. Its
kinds are `receive`, `use`, `return`, `adjust`, `produce`, `transfer`, `sell`, `retire` and
`scrap`; phase 2 writes the first four and the unit ones. A move holds a lot or a unit, never
both (`stock_moves_one_holder`).

- **FIFO.** `StockStore.take` takes from the lots in receive order (`stock_lots_fifo`;
  `inventory/stock.py:108` `_fifo`). A move that names a lot takes that lot first. If the lot
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
  and checked). A second call writes nothing (`python_stock.py:427` `give_back`).
- **Count.** `count_item` zeroes the negative lots first. Then it takes the difference FIFO,
  or adds it to the newest lot. No lot keeps an offsetting negative balance (R-P2-15).
- **Re-cost.** `POST /inventory/lots/{id}/recost` ("Re-cost past uses") rewrites the cost
  of every `use`, `return` and `adjust` move of the lot to the lot's cost now. It answers
  each order item whose parts cost changed, with both figures (R-P2-31; `recost_lot`,
  `python_stock.py:305`). It is tier `write` (Ruling 8). The orders layer audits it as
  `item.parts_recosted`.
- **The open lock and the lock order.** Two callers that both find no lot must not both open
  one. Every lot insert (`receive_in`, `_open_lots`) first takes a transaction advisory lock
  on the item (`_open_lock`, `python_stock.py:666`). `lock_items` takes, for each item in
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
  `receive_in`, `python_stock.py:146`). `set_lot_cost` keeps both, for provenance.

The parts migration does the same for a foreign part (R-P2-22). A move of a `cost_unknown`
lot is `cost_unknown`, and so is a foreign lot's take (R-P2-19). A part's unit cost
averages the known lots only, and `cost_unknown` is set when a lot is unknown. The UI shows
"unknown", never 0 (R-P2-35). A person sets the cost on the lot page; that clears the flag
for later moves, and "Re-cost past uses" carries it to the past ones.

### Job cost's `parts` figure

An order item's parts are in `order_item_parts` (now by `item_id`); a model's rule gives
them to a new item (`model_part_rules`). When `accept_fulfilment` or `fulfil_items` makes
an item `fulfilled`, it takes each part from the lots (`parts.used`). `unfulfil_item` gives
each use back at its own cost (`parts.returned`). No cost entry is written (see
[orders-and-quotes.md](orders-and-quotes.md)).

Job cost reads the moves. `PartsMoved` (`inventory/stock.py:186`) is `-sum(amount_minor)`
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

### The `/parts` aliases are gone

The `/parts` row routes (`GET/POST /parts`, `PATCH /parts/{id}`, and its `stock`, `adjust` and
`moves`), the alias class and the agent's stock tool were removed (#6949, K1 5/7,
R-K1-17). The agent tool `part_list` keeps only its `order_id` form. To list every part, use
`inventory_item_list` with `kind=hardware` and with `kind=ams_part`. To receive stock, use
`inventory_lot_receive`. The parts kinds are `hardware` and `ams_part`, and the agent tool
`part_stock_add` is gone. Kept: `GET /orders/{id}/parts`, `PUT /orders/{id}/items/{item}/parts`,
the rule routes `GET`/`PUT /parts/rules/{slug}` and the tool `order_item_parts_set`.

Still to do: drop `parts_retired` and `part_stock_moves_retired` after a prod check (Ruling 6;
the pack data of a part with no supplier stays only there, R-P2-25). The `part_id` reads in
`PartStore` go with them.

### The migrations

`20261124T0323Z_inventory_core.sql` makes the tables below and seeds the kinds.
K1 adds `20261126T0218Z_inventory_class.sql` (the class columns, `item_kinds.number`,
`relation_types`, `relations`, `handler_config_history`, `inventory_operations` and
`inventory_k1_report`), `20261126T0253Z_inventory_service_caller.sql` and
`20261127T0555Z_service_callers_inventory_and_rotator.sql` (the caller `inventory`),
`20261126T0308Z_property_history_events.sql` (the history events; the subscriber is
`tools/go/core/internal/history`) and `20261127T0521Z_order_items_parts_workflow.sql`
(`order_items.parts_workflow_id`).
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

`agent/src/tools/inventory.ts` has 19 tools. Each answer but `inventory_operation_get`'s has a `ui_path` beside an id
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
| Operations | `inventory_operation_get` (reads a write that answered 202 "still working") |

`order_item_parts_set` sets an order item's parts by item.

## The UI

The sidebar entry is **Inventory**. The routes are in `frontend/src/App.tsx:149-151` and
[frontend.md](../architecture/frontend.md); the code is `frontend/src/pages/inventory/`.

- `/inventory` (`InventoryPage`): a kind tree on the left, the items on the right, with
  search, "Low stock only", "Show archived" and a New item form. The model part rules are
  on this page too.
- `/inventory/kinds` (`KindsPage`): the kind tree, with add, edit, archive and restore, and
  an editor for a kind's keys.
- `/inventory/items/:id` (`ItemPage`): the item's details, its Offers (`ItemOffers`, with
  "Add price"), its Stock (`ItemStock`: receive, use, count, units and their status, the
  moves table), and the Files, links and properties panel.
- An item's page shows its code, `I-<number>`, and has a "Print label" link. Each row of the
  kind tree has one too. Both open the label page with the item or kind chosen (#7169), as a
  saved spool type's page does for its type.
- `/inventory/units/:id` (`UnitPage`) and `/inventory/lots/:id` (`LotPage`): a lot's path
  opens its item's page at the lot's row, where its cost, "Re-cost past uses" and its
  result show.
- `/orders/parts` redirects to `/inventory?kind=hardware` (`PARTS_KIND`,
  `frontend/src/App.tsx:158`). The parts are the lot items under `hardware` and `ams_part`
  (`PART_KINDS`, `frontend/src/lib/inventoryLinks.ts`), which inventory K1 put under
  `consumable`.

The mock is `frontend/src/mocks/features/inventory.ts`.

## Code

- Backend: `backend/scadbuddy/inventory/` (`models.py`, `core_client.py`, `activity.py`,
  `kinds.py`, `offers.py`, `stock.py`, `problems.py`, `component.py`); routes `api/inventory.py`,
  `api/inventory_stock.py`, `api/inventory_operations.py`, `api/parts.py`. Order stock:
  `backend/scadbuddy/workflows/order_stock.py`.
- Go: `tools/go/core/internal/inventory/` (`model`, `handlers`, `store`, `flow`, `activities`,
  `routes`, `modeldata`), `internal/temporalx`, `internal/history`.
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
- Backend, order stock: `backend/tests/workflows/test_order_stock.py` (needs Temporal) replays
  the recorded histories in `backend/tests/fixtures/order_stock_histories/` (how to record
  them: `backend/CLAUDE.md`). `backend/tests/test_no_python_stock_writes.py` keeps Python
  from writing a stock table.
- Go: `go test ./...` in `tools/go/core` (needs `SCADBUDDY_TEST_DATABASE_URL`). The proof
  that the real model data resolves is `internal/inventory/modeldata`
  ([`docs/inventory-model/`](../inventory-model/README.md)).
- e2e: `frontend/e2e/inventory-core.spec.ts` with `E2E_INVENTORY_CORE=1` on the e2e stack's
  `core` service (`e2e-stack/CLAUDE.md`); `hot-path-e2e.yml` runs it on dispatch.
- Agent: `agent/test/inventory.test.ts`, `agent/test/inventoryLinks.test.ts`.
- Frontend: `frontend/src/pages/inventory/*.test.tsx`, `frontend/src/mocks/features/inventory.test.ts`.

## Orders and stock

Three Python workflows on the queue `library` cross to Go (`backend/scadbuddy/workflows/order_stock.py`;
R-K1-27 to R-K1-32): `FulfilOrderItem` (fulfil, accept and unfulfil: commit the order change,
then Go's `Take` or `GiveBack`, then record `parts.used` or `parts.returned`), `RecostLot`
(Go's `Recost`, then the cost events) and `MergePrinter` (Go's `Repoint`, then the merge). A
route starts the workflow first and waits 15 s; then it answers 202 and the item shows "stock
still settling". A refusal in a Python commit step fails the workflow, so the same id can start
again (R-K1-32). A closed stock workflow that holds its item is taken over: the waiting workflow's step restarts it, at most 3 times (`TAKEOVERS`, `backend/scadbuddy/orders/stock_activities.py`).
There is no outbox, no sweep and no manual retry.

## Limits and known issues

- Spool parts (#7278) wait for phase 3 (#7068). `spool_part` stays seeded (Ruling 7).
- Filament items, shopping lists, the `inventory_moves` view and spool cost in job cost
  are phase 3 (#7068). Install and value over life are phase 4 (#7069). Production is
  phase 5 (#7070). Selling from stock is phase 6 (#7071).
- `model_part_rules` become model-owned recipes in phase 5 (#7070). Phase 2 repoints the
  rules in place.
