# Inventory kinds and handlers: one base model for every thing (design)

Status: approved by the owner, 2026-11-25. Epic #6949. Proof: [`docs/inventory-model/`](../../inventory-model/README.md).

This design replaces how a kind is defined in
[`2026-11-21-inventory-and-purchasing-design.md`](2026-11-21-inventory-and-purchasing-design.md)
§6.4 (the `tracking` and `life_basis` columns, and the filament special case). Everything
else in that spec stands: suppliers, offers, invoices and their lines (§6.5, §6.7), the
parity table (§11, PR #7716). It also replaces the Python phase 3 plan in PR #7666: the
code is Go.

## 1. Intent

The owner's words, 2026-11-25:

- "Have different types of items in inventory where we can say it's a service or a physical
  thing. Then if a physical thing: consumable (filament) or depreciating (printer). Then if
  consumable: is it a lot or a unit."
- "If consumable, you can say it's a special type of consumable: Filament spool, which has
  its own logic in flows and types. Another consumable is a ream of paper, nothing special
  about it."
- "Once we're in a special type, different handlers kick in, like filament spools."
- "A service could be a warranty, with a start and end date", or "an hourly rate", or a
  "flat rate".
- "Relationships to things, think Jira's list of 'contains X', but strongly typed to be a
  specific instance and/or type."
- "Life should be handled by a handler. Nothing like that hard coded."
- "We should be in Rust or Go for this code."
- "Prove in markdown + yaml + mermaid that this design works."

Why: today filament is a second inventory with its own tables, routes, pages and tools
(`backend/scadbuddy/filament/`, about 60 modules), beside the generic one of phase 2
(`backend/scadbuddy/inventory/`). Every purchasing feature exists twice or only for
filament (spec §11). One base model, where filament is only a set of handlers, ends that.

## 2. Goals, non-goals, anti-goals

Goals:

- G1. One base model holds every thing the owner named: printer, nozzle, spool, spool
  insert, textured PEI plate, RFID labels, power drill, label printer, its service plan,
  a spool of labels, a ream of paper; and a warranty, an hourly service and a flat-rate
  service.
- G2. What a thing *is* is a few fixed answers (§3). What it *does* is handlers (§4).
  The base flow has no behaviour of its own for any step.
- G3. A special type is a named set of handlers. Filament spool is the first, printer the
  second.
- G4. Typed relationships between units, items and kinds, each with an optional handler
  (§5).
- G5. Go, in `scadbuddy-core` (#7667), behind the HTTP contract (§6).
- G6. The proof in `docs/inventory-model/` passes, and the Go tests load the same YAML.

Non-goals:

- Selling a service on the storefront (owner, 2026-11-25). Orders sell services (§4.6).
- Handlers that people write at run time. Handlers are Go code in the service. A kind,
  an item or a unit picks them and sets their parameters, as data.
- New filament behaviour. The filament handlers are today's code, moved.
- The UI design. The pages of spec §6.11 and the parity table stay; this design gives
  them one data model.

Anti-goals (a failure even if it works):

- Any `if kind == filament` (or `printer`, or a kind id) in the base flow or in a default
  handler.
- A class answer that a kind can change after its parent set it. A spool that becomes a
  service breaks every handler that trusted the class.
- Two sources of the same fact: life in a column and in a handler, a cost in two places.
  The handler cache (§4.7) is not a source: it is derived, and a rebuild must match it.

## 3. The class: what a thing is

A kind answers these questions. A child kind may answer one its parent left open. It may
never change an answer its parent gave. An item's kind must have answered all the
questions its nature needs.

| Key | Values | Asked when |
|---|---|---|
| `nature` | `physical`, `service` | always |
| `role` | `consumable`, `durable` | `nature = physical` |
| `counting` | `lot`, `unit` | `nature = physical`. `durable` is always `unit` |
| `billing` | `term`, `hourly`, `flat` | `nature = service` |

My call: the owner said "depreciating". I call that role `durable`: a thing you use again.
Whether its value falls, and how, is its life handler. A spool insert is durable and does
not lose value (`life.none`); a printer is durable and loses value by run hours.

The root kinds are `service`, `consumable` and `durable`. Every phase 2 built-in kind has
a place under them (`kinds.yaml`, with each kind's old `tracking` and `life_basis`).

A kind also has: `name`, `parent`, `uom`, a property schema (spec §6.3a, with `required`
from #7703), `special` (a special type, §4.4), and `steps` (handlers and parameters, §4).
The categories people know, `Filament > PLA > Matte` and `Paper`, are kinds in the same
tree. A category below the class roots changes no behaviour unless it sets `steps`.

## 4. Handlers: what a thing does

### 4.1 Steps

The base flow has ten steps. It does none of them itself: it finds the handler for the
step and calls it, in the caller's transaction.

| Step | What it does |
|---|---|
| receive | Make stock from an invoice line or by hand (a lot, units, or a service term) |
| consume | Book one use against a print, an order, a job or another unit |
| count | Set or measure what is left |
| move | Change where a lot or unit is |
| retire | End a lot or unit, with a reason |
| cost | Give a lot or unit its cost, and one use its cost at the time of use |
| life | Give a unit its used share, book value and end-of-life signals |
| bill | Give a service its charge |
| label | Say what a label and a scanned code show |
| sync | Exchange stock with an outside system |

The handler catalog is `handlers.yaml`. The handler `refuse` fills a step a class does not
have, with the problem `step_not_applicable` (an hourly service cannot be received).

### 4.2 How a step finds its handler

In this order, each later layer overriding the earlier one:

1. The class default (`handlers.yaml` `defaults`: one column per class).
2. The special type's handlers, if the kind has one.
3. The kinds, root to leaf (`steps.<step>.handler`).
4. The item.
5. The unit (parameters only).

A layer that names a new handler drops the parameters set for the old one. A layer that
sets only `params` merges them into the handler it has.

When a kind or an item is saved, every step must resolve to a handler that fills that
step, applies to the class (and special type), and has valid parameters. If not, the save
is refused with the step and the reason. So a broken combination never reaches stock.

### 4.3 Parameters

Each handler declares its parameter schema: the key, the type, whether it is required,
allowed values. Values are set at the kind, the item or the unit (§4.2). Examples:
`life.hours` takes `hours` and `source`; `bill.hourly` takes `cost_rate_minor`,
`sell_rate_minor`, `increment_minutes`, `minimum_minutes`; `count.remaining` takes `capacity`.

### 4.4 Special types

A special type is a named set of handlers. It says:

- `needs`: the class it requires (`filament_spool`: physical, consumable, unit).
- `handlers`: the steps it overrides.
- `requires_properties`: properties its items must have.
- `panels`: the UI panels the frontend shows for its units (a frontend registry keyed
  by panel id).
- `tools`: its agent tools.

A kind names at most one special type, and its children inherit it.

`filament_spool` is today's filament code as handlers: receive (`spools.py`, the invoice
confirm), consume grams (`ledger.py`, `print_uses.py`, `bambuddy_usage.py`), weigh
(`empty_weight.py`, `insert_weights.py`, #7681), move with AMS trays (`capture.py`,
`locations.py`), close (#7684), cost per kg (`cost_calc.py`), label (`labels.py`), and the
Bambuddy sync (`bb_sync_*.py`). Their tables stay (`filament_spools`,
`filament_spool_events` and the rest). The handler writes them in the base flow's
transaction.

`printer` is today's printer code as handlers: receive links the `printers` row, consume
books run hours and one use on every unit the printer contains, cost is the machine rate,
sync is the Bambuddy or LAN link (`backend/scadbuddy/printers/`).

### 4.5 Life

Life is a step like the others. Handlers: `life.none`, `life.months` (`months`,
`salvage_pct`), `life.hours` (`hours`, `source`: the unit's own run hours, the run hours of
the unit it is installed in, or manual), `life.uses` (`uses`), `life.term` (from the
instance's `starts_at` to `ends_at`, `warn_days`). A life handler answers a unit's state at
a time: used share, book value, and signals (a warning, the end). Its result is cached,
with a history (§4.7). This replaces `item_kinds.life_basis`, `items.life_hours`,
`items.life_months`, `stock_units.life_hours` and `stock_units.life_months`.

A life handler reads only the unit's own `unit_uses` rows (§6.3), never the state of the
unit it is in now. A print run books a row on the printer and, through it, on each unit
it contains at that moment. So a nozzle or a plate moved from printer A to printer B keeps
every hour and print from both, and A's later runs do not count on it. The proof replays
such a move and checks each life (`docs/inventory-model/README.md` §5.7).

### 4.6 Services

A service has no stock. A term service (warranty, plan) has instances: a unit with
`starts_at` and `ends_at`, made by `receive.service_term`, ended by `retire.term_end`, its
prepaid value falling by `life.term`. An hourly or flat service has no instances: it is
only used (`consume.service`) against an order item or a unit.

Owner rulings, 2026-11-25 (given to scadbuddy-53, relayed on PR #7784):

1. **A bill has a cost side and a sell side.** The cost is what the service costs the
   shop, and job cost takes it (`cost.bill`). The sell side is what the customer is
   quoted. `bill.hourly` takes `cost_rate_minor` and `sell_rate_minor`, `bill.flat` takes
   `cost_minor` and `sell_minor`; either side may be left out, and the rounding parameters
   apply to both. `bill.term`'s cost is the price paid for the term (its invoice line),
   and its optional `sell_minor` is the price when the shop sells the term on.
2. **An order sells a service through a `custom` order item that names it.** Order items
   are `model`, `custom`, `request` or `library_file` (`orders/models.py` `ItemKind`
   l.81). A `custom` item gets one nullable reference to a service item (`item:<id>`); no
   new item kind. Its quoted price defaults from the bill's sell side. Fulfilling it
   writes `consume.service` against the order item, and job cost takes the cost side. A
   custom item with `requires_printing: false` and no reference keeps working as today.
3. **The quote's `labour` line and time entries stay as they are** (`rate_minor_per_hour`,
   `orders/models.py` l.839). A `labour` service item is for work sold as a line of its
   own (an outside repair, a fixed service). How the shop's own labour line relates to
   labour items is left to a later phase.
4. **Selling a service on the storefront is out of scope** (§2).

### 4.7 Cached values, their history, and refresh (owner, 2026-11-25)

The histories here follow §4.8's rule: each change is an event on the stream
(`handler_value.changed`, `handler_config.changed`), published in the transaction that
changes the value, and the history subscriber inserts it into its history table.

The owner: "the system should cache the handler values and keep a history", and "allow
for a refresh".

- **Cache.** `handler_values` holds, per entity and step, the handler's current result
  (a life's used share, book value and signals; a roll's remaining count; a lot's unit
  cost), with the handler id and parameters that made it, when, and the last input it
  read (`inputs_through`, the newest `unit_uses` row). Pages, stock value, job cost and
  the machine rate read it. No read runs a handler.
- **When it is written.** In the transaction of the event that changes a handler's
  inputs: a use booked, a relationship added or removed, a parameter changed, a retire.
  A print run updates the printer and every unit it contains. Time-based values
  (`life.months`, `life.term`) change with no event; the daily schedule refreshes them.
- **History.** When a cached value changes, a row is appended to
  `handler_value_history` (at, reason, the value, the handler and parameters in force).
  A value that did not change writes nothing. Rows are never rewritten: a report of book
  value at a past date reads the history, so it shows what the shop knew then.
- **Parameter history.** Every change to a kind's, item's or unit's handler or
  parameters is appended to `handler_config_history` (old, new, who, when). It starts in
  K1, since K1 already lets people change `steps`. It explains a
  value row with the reason `params_changed`.
- **Manual readings** (owner, 2026-11-25: "allow for manual, like weight, in
  advanced"). The unit page's Advanced section takes a measured value by hand for a
  handler that measures something: hours run (`life.hours`), prints (`life.uses`), labels
  left (`count.remaining`), as `filament.weigh` takes grams. It is a reading row in
  `unit_uses` (value, who, when), a source like any other: the handler counts later uses
  on top of the newest reading, and the history shows the reason `manual`. A reading is
  never an edit of the cache, so a refresh or a rebuild keeps it.
- **Refresh.** `POST /api/v1/inventory/handler-values/refresh` (forwarded to Go's
  internal route, §6.1) with a scope: one unit, an item's units, a kind's units, or all.
  It recomputes each value and appends history only where a value changed, with the
  reason `refresh`; a repeat at the same time writes nothing. The unit, item and kind
  pages have a Refresh button, and the agent a tool. A release that changes a handler's
  formula runs a refresh of all, so the change is dated in the history.
- **The cache is derived.** The sources stay `unit_uses`, the stock moves, the
  relationships and the parameters. A rebuild from them must give the cached values; the
  Go tests check it after every replayed history, as `check.py` does.

### 4.8 Property history (owner, 2026-11-25)

The owner: "all property sets recorded and historied". Today `entity_properties` keeps
only the current value (`migrations/20261123T0017Z_entity_properties.sql`); a set
overwrites and a remove leaves no trace.

- New `entity_property_history`, append only: one row per set or remove, with
  `entity_id`, the entity's `kind` and `ref` (copied, so the row outlives the entity),
  `key`, `op` (`set`, `remove`), `old` and `new` values, `source`, `at` and `actor`. A set
  to the value already there writes nothing.
- It covers every entity's properties (items, units, lots, kinds, suppliers, orders and
  the rest of spec §6.1), not only inventory's. The first values of a new entity are
  recorded as sets from nothing. An entity merge (`entities/registry.py`) records each
  key it moves.
- A property schema change (`entity_property_schemas`, a kind's `schema`) is recorded the
  same way in `entity_property_schema_history`.
- **The event stream is the history** (owner, 2026-11-25: "assume event stream is the
  history"; "have a subscriber manage the history table by insert log every event
  change"). Each change is published as an event on the `events` log
  (`migrations/20260928T0630Z_events.sql`, `core/pg_events.py`; Go's `internal/bus`) in
  the same transaction as the write: `entity_property.changed` (entity id,
  `entity_kind`, ref, key, op, old, new, source, actor, at; `kind` is the envelope's own
  field) and `entity_property_schema.changed`. The history
  tables are not written by the writers. A **history subscriber** reads the stream from
  its own durable cursor and inserts one row per change event; the event's `seq` is the
  row's key, so a replay inserts nothing twice.
- **No writer can skip the event.** My call: a database trigger on `entity_properties`
  publishes the event (an `INSERT INTO events` and a `pg_notify`, as `internal/bus`
  `Publish` does) in the writer's transaction, so Python, Go and a cascade all publish.
  Every writer sets `SET LOCAL scadbuddy.actor` per transaction; the trigger takes that
  actor first, and the row's `updated_by` only when it is unset. So a merge, which copies
  `updated_by` from the old row (`entities/registry.py` l.135), is credited to the person
  who merged. A remove without an actor is refused.
- A delete of an entity (the sweep, a merge) cascades to its properties after the entity
  row is gone. A `BEFORE DELETE` trigger on `entities` publishes a remove event for each
  of its keys, with the entity's `kind` and `ref`, before the cascade.
- **The history events are kept forever.** On main the log is still pruned by age and
  count (`core/pg_events.py` l.79-84). Open PR #7819 (#7818) changes that: by default every
  event is kept forever (`SCADBUDDY_EVENT_LOG_RETENTION_SECONDS` and `_ROWS` default to 0),
  and `SCADBUDDY_EVENT_LOG_RETENTION_KINDS` sets retention per kind prefix (read on its
  branch by scadbuddy-09 and scadbuddy-34, 2026-11-25). The history kinds
  (`entity_property.`, `entity_property_schema.`, `handler_value.`, `handler_config.`)
  get no retention, so they stay. K1 depends on #7819. The stream itself is then the
  history; the history tables are the subscriber's indexed copy for reads by entity.
- **The subscriber's cursor.** No durable subscriber cursor exists on main or in an open
  PR (#7768 puts persisted cursors out of its scope). K1 adds `event_subscribers` (name,
  `seq`, updated). The history subscriber reads from its cursor after a restart, and its
  lag is a metric with an alert.
- The current properties must equal the history replayed (last row per key). The proof
  checks it; the Go tests do too.
- The item, unit and kind pages show a property's history, and the agent's
  `entity_property_*` tools can read it.

## 5. Relationships

A relationship joins two entities (spec §6.1: `unit:`, `item:`, `item_kind:` refs). Its
type says what may stand at each end (a unit, an item, a kind; and a class or special
type), how many `from` ends one `to` end may have, and which handler runs on add, on
remove, and on the daily schedule. A type with no handler is shown only. The types are in
`relations.yaml`:

| Type | From → to | Handler |
|---|---|---|
| `accepts` | a kind, item or unit → a kind or item, with `max` and an optional `where` on properties | none. `contains` reads it |
| `contains` | a unit → a unit, at most one container | `relation.contains`: refuses what no `accepts` allows, what passes its `max`, or a unit in another container; sets `in_service`, starts life; the container's consume books uses on it |
| `covers` | a term service's unit → a unit or item | `relation.covers`: the schedule warns `warn_days` before the end, and retires the term at the end |
| `compatible_with` | an item or kind → an item or kind | none. `contains` warns on a mismatch |
| `continues_from` | a unit → a unit | none (a respooled spool) |

**Container fields** (owner, 2026-11-25: "contains … shows a list or single item as being
within it; this is how we should model the container fields"). A container field is an
`accepts` rule. Its `max` decides how the page shows it: `max: 1` is a single field (the
roll in a label printer, the insert in a spool), a larger `max` or none is a list (an H2C
takes 2 nozzles). `contains` fills the field. A container's fields come from the rules on
its unit, its item and its kinds, so every H2C has the same fields without per-unit setup.

**Who adds types** (owner, 2026-11-25): a person can add a relationship type in the UI when
it has no handler (a show-only link such as "spare for"). A type with a handler comes with
Go code.

`contains` replaces `stock_units.installed_in_unit_id`. `compatible_with` replaces
`items.compatible_with text[]` (§8).

## 6. Go and Python until the Go move is done

### 6.1 The line (owner, 2026-11-25)

Go owns stock; Python calls it.

- `scadbuddy-core` (`tools/go/core`, epic #7667) holds the inventory and stock tables, the
  registry and every handler. It serves them as internal CRUD routes under
  `/api/v1/internal/crud/inventory/*`, which take a service credential of a new caller
  `inventory`, minted in process by the API (area `inventory`, and `inventory:read` for
  its GETs). The Go router mounts areas per caller: today `tools/go/core/main.go`
  `newRouter` mounts every area for the caller `agent`, and `internal/crud/crud.go`
  refuses a credential whose caller is not the path's, so the caller is added to
  `auth/service_auth.py` and the `service_credentials_caller_check` constraint
  (plan K1, R-K1-3).
- Python keeps the public `/api/v1/inventory/*` (and `/filament/*`) routes: the login
  checks, the OpenAPI export and so `schema.d.ts` stay as they are. Each route parses,
  forwards to its Go route, and maps the answer. The pydantic models stay the source of
  the public contract; a Go internal route takes and returns the same JSON. When #7667's
  phase 6 ports the login checks, the public routes move to Go unchanged and the Python
  forwarders go.
- Python code that writes stock tables outside `inventory/` (read on main `ed32aabf1`):
  `orders/lifecycle.py` (take l.5381, give back l.5424, re-cost l.5517),
  `entities/registry.py` (the merge's `UPDATE stock_moves`, l.132), `printers/store.py`
  (its merge re-points `stock_units.printer_id`, l.358) and `inventory/part_alias.py`
  (the `/parts` aliases, l.219-360). Later: the `PrintRun` workflow,
  `filament/bambuddy_usage.py` and the `FilamentBbSync` workflow. These become HTTP
  clients of the Go routes, each in the layer the plan names; until then a caller keeps
  its SQL and runs no handler. `orders/parts.py`, `orders/costs.py`, `orders/store.py` and
  `entities/sweep.py` only read stock tables and keep their reads.
- An order change and its stock write are two steps now, not one transaction. Every Go
  write takes an `Idempotency-Key`. The ledger is the existing `crud_requests`
  (`migrations/20261106T1836Z_crud_requests.sql`, `crud/idempotency.py`), kept 7 days for
  the caller `inventory` (24 hours for the others); a repeat returns the stored result.
  The caller starts its workflow first, and the workflow's first activity writes the
  caller's row (§6.1a "Nothing manual, no sweeps"); Go is called with a key derived from
  that row (the workflow `order_item:<id>:parts:<n>`, each `inventory.Take`
  `order_item:<id>:parts:<n>:<item>`), and Temporal retries a failed call. A refusal in
  the first activity rolls back and fails the workflow as non-retryable, so nothing was
  written and the same id may start again. A refusal by Go after the first activity has
  committed (a `Take` refused for a business reason) is an answer, not a failure: it is
  not retried, the workflow's last activity records it on the order
  (`parts.stock_refused`), the person sees it there, and nothing is rolled back.
- In Go, a handler runs in the base flow's transaction: every table a handler writes is
  in the same database, written by `scadbuddy-core`.

### 6.1a Crossing a boundary is a Temporal activity (owner, 2026-11-25)

The owner: "We should be using Temporal, not transactions on SQL, when we cross
boundaries", with Standalone Activities "used inline for maximum sharing", which "also
gives us the visibility into the Activities tab in SB".

- **Writes go through Standalone Activities.** A Standalone Activity is an activity
  started by a client with no workflow, retried by Temporal, listed by its id
  ([docs.temporal.io/standalone-activity](https://docs.temporal.io/standalone-activity),
  GA, read 2026-11-25). Every stock write that crosses from Python to Go is one: the
  Python caller starts the Go activity by its type name (`inventory.Receive`,
  `inventory.Take`, `inventory.GiveBack`, `inventory.Recost`, `inventory.Repoint`, …) on
  the task queue `inventory`. A public route starts it (`client.start_activity`) and
  waits up to 15 s for the result, then answers 202 ("A slow write" below); a workflow
  runs it as a step by the same type name. Reads stay plain HTTP on the internal routes.
- **Go runs the activities.** `scadbuddy-core` gets a Temporal client and a worker for
  the `inventory` queue. Each activity runs the base flow in its own database
  transaction, so the activity is the Go code itself, with no second HTTP hop. The same
  activity function serves a standalone start, a workflow's step (a flow, the
  invoice-import flow), and the Go tests: that is the sharing.
- **Idempotency.** The activity id is the idempotency key
  (`order_item:<id>:parts:<n>:<item>`, `entity_merge:<from>:<to>`, a route's
  `Idempotency-Key`). The start uses the conflict policy Use Existing (a repeat attaches
  to the running one) and the reuse policy Allow Duplicate Failed Only: a repeat after a
  success is refused and reads the stored result, and a repeat after a failure or a
  timeout runs again. An activity runs at least once, so the Go side also records the
  key with its result in `crud_requests` (§6.1) and answers a repeat from it.
- **A slow write** (owner, 2026-11-25). A public route waits up to 15 s. If the activity
  is still running, it answers `202 Accepted` with the activity id; the UI shows "still
  working" and follows it on the Activities tab. A repeat with the same key is never an
  error: while it runs it attaches and answers `202` again, and once it is done it
  answers the stored result. So repeated tries cause no extra work.
- **Nothing manual, no sweeps** (owner, 2026-11-25: "nothing manual here should happen
  for sweeps"). A change that crosses a boundary starts its workflow *before* it commits
  anything, and the workflow's own first activity commits the local change. So a failed
  start leaves nothing half done (the person gets an error, and nothing was written), and
  no state ever needs a sweep, a backstop, a repair loop or a person to settle it.
- **Orders.** Fulfilling an order item starts the workflow `FulfilOrderItem` with the
  item's key. Its steps: a Python activity that commits the order change; the Go activity
  `inventory.Take`; a Python activity that records `parts.used`. Temporal retries a step
  that fails; a refusal is an answer, not a failure. A refusal in the first activity ends
  the workflow as failed (a non-retryable error carrying the problem), with nothing
  written, so the same id can start again once the cause is fixed. A refusal of `Take`
  is recorded by the last activity as `parts.stock_refused`, which the order shows; the
  order change stays, and nothing is rolled back. If the workflow does not finish in 15 s, fulfil answers 202
  ("stock still settling") and the order settles by itself when the workflow ends. If the start itself fails, the fulfil call answers an error
  and nothing was written; a client that sends it again does its own idempotent retry,
  not a repair.
- **Merges.** An entity or printer merge is a workflow too: it first executes
  `inventory.Repoint` (move the stock rows from the old id to the kept one), then an
  activity runs the merge's own transaction and deletes the old row. Calling Go from inside the merge's locked transaction would make the two
  transactions wait on each other; this order avoids it. A stock row that lands on the
  old id between the two steps makes the delete fail on its foreign key, and the merge
  repeats the first step. The foreign key protects only `stock_moves.entity_id`.
  `stock_units.printer_id` is `ON DELETE SET NULL`, so a delete would clear it, not
  fail. So the printer merge locks the gone printer's row `FOR UPDATE`, then checks
  whether a stock row names it; if one does, it rolls back, and the workflow executes
  `inventory.Repoint` again (with a new key per round) and runs the merge again. There is
  no cap: only a stock write racing the merge repeats it. The workflow ends when the merge
  commits, or when the merge step refuses (one of the caller's checks fails): then it
  executes `inventory.Repoint` back, for exactly the rows it moved, and the run fails with
  that problem, so the same id can start again (owner, 2026-11-25; plan K1, R-K1-29,
  R-K1-32). A round repeats only
  after a new stock write named the gone printer, and the workflow waits between rounds
  (`workflow.sleep`, doubling from 1 s to 60 s), so a busy stock writer cannot spin it. The callers that need the
  kept row (`link_bambuddy`, `ensure_bambuddy_written`) wait on the workflow's result.
- **Visibility.** It is not automatic; every feature writes its own record row and the
  activity record follows it. Renders, print runs and the backend's operations write an
  `activity` row in the same transaction as their own row (`ActivityIndex.upsert_in`, for
  example `operations/store.py` l.174). The agent, another process, writes its
  `ai_operations` row and publishes `ai_operation.changed`; the API's activity component
  ingests it (`activity/component.py` l.61-86, `agent_data/operations.py`). Go does the
  same: an activity interceptor writes an `inventory_operations` row (id = activity id,
  kind, subject, status, start, end, the result's problem if any) in Go's transaction and
  publishes `inventory_operation.changed` on the events channel (Go's `internal/publish`
  already publishes there); the activity component ingests it into the Activities tab.
  No new service caller or route is needed. Temporal's `ListActivities` lists them too.
- **Versions.** Prod runs Temporal server 1.32.1 (eh-homelab/clusters
  `applications/scadbuddy/temporal-cluster-v2.yaml` l.65); Standalone Activities need
  1.32.0. The backend's Python SDK is 1.36.0 (`backend/uv.lock`); they need 1.33.0. The Go
  SDK needs 1.49.0; `scadbuddy-core` adds it (vendored, `tools/go/CLAUDE.md`). Go
  Temporal code to follow: eh-homelab/clusters `tier0/bootstrap/internal/` (`wf`,
  `activities`, `codec`, `progress`), on SDK v1.47.0. The e2e stack's dev server
  (`temporal server start-dev`) must run server 1.32.0 or newer, with Standalone
  Activities on.

### 6.2 Layout

```text
tools/go/core/internal/inventory/
  model/       class, kinds, items, lots, units, relationships (types only)
  handlers/    the registry, one interface per step, the default handlers, life, bill
  relations/   relationship types and their handlers
  special/filament/   the filament_spool handlers
  special/printer/    the printer handlers (phase K6)
  store/       every SQL statement
  routes/      /api/v1/internal/crud/inventory/* (reads)
  activities/  the Temporal activities for every write, and the worker (§6.1a)
  modeldata/   loads docs/inventory-model/*.yaml for the tests
```

A route handler holds no SQL and no rule beyond parsing and problem mapping (plan #7666
R-P3-54, kept). The package registers its areas as the Go core's domain packages do since
#7709: `inventory.Areas(pool, keys) map[string][]crud.Route`, returning `inventory` and
`inventory:read`, as `internal/ordercosts/areas.go` returns `session-order-costs`. The
layout is asked of the #7667 owner (comment on #7667, 2026-11-25).

### 6.3 Data

- `item_kinds`: add `nature`, `role`, `counting`, `billing`, `special_type`, `steps jsonb`.
  Drop `tracking` and `life_basis` after one release, with views for any reader left.
- `items`: add `steps jsonb`. `life_hours`, `life_months` move into it.
- `stock_units`: add `steps jsonb`, `starts_at`, `ends_at`; status gains `ended`.
  `life_hours`, `life_months` move into `steps`; `installed_in_unit_id` becomes a
  `contains` relationship.
- New `unit_uses`: one row per use of a durable unit (`unit_id`, `at`, `hours`, `uses`,
  `print_run_id` or `ref`, `via_unit_id` for a use booked through a container). It is the
  one store of uses and hours: `life.hours` and `life.uses` sum it up to a time, and a
  printer's run hours are its own rows. `printer_run_hours` (one counter per printer,
  `migrations/20261118T0738Z_printer_run_hours.sql`) stays as it is until K6, which back-
  fills one opening row per printer from it and then reads `unit_uses` only.
- New `relation_types` and `relations` (K1 creates them, since §8 turns
  `items.compatible_with` into relationships; their handlers come in K2), and the entity
  kind `item_kind` for a kind's end (`entities/registry.py` `KINDS`). `relations` has
  `type_id`, `from_entity`, `to_entity` as FKs to
  `entities`, `attrs jsonb` for `max` and `where`, `created_at`, `created_by`, `ended_at`).
- New `inventory_signals`: one row per signal raised, so the schedule never raises one
  twice.
- New `entity_property_history` and `entity_property_schema_history` (append only, written
  only by the history subscriber from the event stream), §4.8. The same subscriber writes
  `handler_value_history` and `handler_config_history` (§4.7).
- New `event_subscribers` (name, cursor `seq`, updated): each durable subscriber's place
  in the stream (§4.8).
- New `handler_values` (the cache, primary key `entity_id`, `step`),
  `handler_value_history` and `handler_config_history` (append only, written by the history
  subscriber), §4.7.
- `order_items`: one nullable `service_item_id` (a `custom` item that sells a service,
  §4.6 ruling 2). Orders stay Python's; fulfilling such an item calls Go's consume route.

Migrations stay files in `backend/scadbuddy/migrations/`, applied by the API at start,
until #7667 phase 7 moves them. The API and `scadbuddy-core` are the only SQL holders
(#5295, with #7667's exception).

### 6.4 The schedule and the notices

A Python Temporal schedule runs daily, as the housekeeping schedules do until #7667's
phase 7. Its activity calls `POST /api/v1/internal/crud/inventory/signals/run`. Go first
refreshes the time-based cached values (§4.7), then asks each relationship handler and
each life handler for its signals at that time, writes the new ones to
`inventory_signals`, and queues them as notices the same way sensor notices
are queued and sent (`filament/sensor_notify.py`, `workflows/sensor_notify.py`). The
filament sync stays the Python `FilamentBbSync` workflow; in K4 its writes go through the
Go routes, and `filament.bambuddy_sync` is the Go side of those writes.

## 7. The proof

`docs/inventory-model/` holds the handler catalog, the kind tree, the relationship types,
and examples of every thing named in §2 G1 with instances and relationships. Its
`check.py` resolves every item's handlers by §4.2, checks every handler's step, class and
parameters, every instance and every relationship (each container field's `max`
included), checks that five relationships the design must refuse are refused for their
stated reason (each entry's `reason` code is the rule that refuses it), and checks that the README's table is the resolved one. On 2026-11-25 it
passed (`uvx --with pyyaml python -I docs/inventory-model/check.py`): 33 kinds, 38
handlers, 16 items, 17 instances, 16 relationships, and a history of 16 events whose 6
unit lives, 21 value history rows, the parameter history, and 16 property history rows (8
of them against expected rows, and all of them replayed into the current properties) it
checks. Its 15 Mermaid diagrams rendered with
`@mermaid-js/mermaid-cli` 11.4.2.

The first Go task ports `check.py` to a Go test over the same YAML, so the proof becomes
the test of the real resolver.

## 8. Moving there

The migration places every phase 2 row by these rules, and reports each count:

- **Built-in kinds**: by `kinds.yaml`'s `was`. Those kinds, the roots and the four
  service kinds are seeded (`seed: true`). The other kinds in `kinds.yaml` are examples a
  person would add. `spool_part` keeps its id; its name becomes "Spool insert".
- **Kinds a person made** (`builtin = false`): `tracking = lot` → consumable, lot;
  `tracking = unit` → durable; `tracking = none` → service, flat; `tracking = filament`
  → under `filament`. Its `life_basis` becomes a `steps.life` handler (`hours` →
  `life.hours` with `source: own_run_hours` and no `hours`, so the kind is marked
  incomplete until a person sets them; `months` → `life.months` likewise).
- **Items on a root** (a root leaves its class open): items on `consumable` move to
  `general_consumable` (lot); items on `service` move to `flat_service`.
- **Lots of a kind that becomes durable** (`part`): split into units, one per piece, each
  with the lot's unit cost. On prod, the parts back-fill (#7457) found no parts
  (scadbuddy-09, 2026-11-24), so this is expected to touch no rows.
- **`items.compatible_with`** (printer model codes): each distinct code becomes a kind
  under `printer` named by the code (`H2C`), and each item gets a `compatible_with`
  relationship to it. A printer item takes its model kind. The list filter
  `?compatible=<model code>` keeps working: it reads the relationship to the kind of that
  name.
- **Spool types and spools**: items and units (plan #7666 R-P3-30, kept). The filament
  kinds come from `filament_materials` and `filament_subtypes` (R-P3-45, R-P3-67, kept).
- The Python `/inventory/*` routes of phase 2 become forwarders first (§6.1). Each
  `/filament/*` route's writes move when its handler does.

## 9. Phases

Each is an issue under #6949, with layers as stacked PRs.

- K1. The base in Go: the class, the registry, the default handlers, kinds, items, lots,
  units, moves, the internal routes, the Python forwarders for the phase 2 routes, the
  idempotency keys, and the migration of §8. The YAML test. It needs `scadbuddy-core`
  deployed with its CRUD port (#7702), not the front process: Python calls Go directly.
- K1 also adds the property history (§4.8): it touches only `entity_properties` and
  `entities`, so it can land first, on its own. K1 also creates the relationship tables
  and `handler_config_history` (§6.3, §4.7).
- K2. Life, services and relationships: `unit_uses`, the life handlers, the handler
  cache with its history and refresh, term instances, the relationship types and
  container fields, the schedule.
- K3. Purchasing in Go, with `filament.receive`: suppliers, offers, price points,
  invoices and the one line model (spec §6.5, §6.7). The filament receive handler comes
  in this phase so that a mixed invoice is confirmed in one Go transaction from the first
  day; the Python filament confirm becomes a forwarder in the same layer.
- K4. The other `filament_spool` handlers: consume, weigh, move, close, cost, label, sync.
  Their Python callers become clients (§6.1).
- K5. The UI: Filament moves into Inventory (spec §11 3b), with per special-type panels
  and container fields.
- K6. `printer` handlers, the run-hours back-fill, and the machine rate from units (spec
  §9 phase 4).

## 10. Approaches considered

| Approach | Trade-off | Verdict |
|---|---|---|
| A. A class plus handlers, special types as handler sets, in Go | One model; behaviour is data that picks code | Chosen |
| B. Keep `tracking` and add flags (consumable, has_life, is_service) | Small change, but every new need adds a flag and an `if` in the base flow; life stays hard-coded | Rejected: against "nothing hard coded" |
| C. A subtype table per kind of thing (printers, nozzles, papers…) | Strong types, but a table and code per kind; filament stays a second world | Rejected: against "spools are treated the same" |
| D. Handlers as flows written at run time | Most open; slow, hard to test, and stock writes outside a transaction | Rejected for now; a flow may call the steps' routes |
| E. Build in Python now, port later | Fastest start | Rejected by the owner: Go or Rust |

Rust: the Go move design keeps Rust for heavy compute. This is CRUD, so it is Go.

The line between Python and Go (§6.1) had three options: Go owns stock and Python calls
it (chosen by the owner); wait for #7667 to port the login checks and workflows (nothing
ships for weeks); or port the login checks for the inventory routes to Go now (pulls much
of #7667's phase 6 into this work).

## 11. My calls (the owner to check)

1. The role is named `durable`, not "depreciating" (§3).
2. A durable thing is always counted by unit.
3. Handlers are Go code; kinds, items and units only pick them and set parameters.
4. A special type may sit on any class, not only consumables (printer is durable).
5. `printed_part` and `assembly` are consumable lots (they are used up or sold).
6. A label roll is a consumable counted by unit, with a remaining count (`count.remaining`);
   NTAG stickers and paper are lots.
7. Each printer model is a kind under `printer` (`H2C`); container fields and
   compatibility hang off it (§8).
8. One `unit_uses` table holds every use and every run hour (§6.3).
9. The migration rules of §8 for kinds a person made and items on a root.
10. Idempotency keys live 7 days (§6.1).
11. `filament.receive` comes with purchasing in K3, not with the other filament handlers.
12. The cache is written in the event's transaction, and history only on a change (§4.7).
13. A past report reads the value history as it was then; a parameter change never
    rewrites old rows.
14. A manual reading replaces the counted measure from its time on; it does not change
    the life's length (`hours`), which is a parameter.
15. Every change is an event; a trigger publishes property events so no writer can skip
    one; a durable history subscriber writes the history tables; history kinds are kept
    forever under #7819's per-kind retention (§4.8).
16. Every Python-to-Go write is a Standalone Activity run by a Go worker; reads stay HTTP
    (§6.1a).

## 12. Left to the builder

- The Go interface signatures beyond those in the proof's class diagram.
- How the frontend registry maps panel ids to components.
- Index choices for `relations` and `unit_uses`.
- The Python client for the Go routes (hand-written or generated).

## 13. Questions for the owner

None open. The owner's answers of 2026-11-25 are in §5 (who adds relationship types,
container fields) and §6.1 (the line between Go and Python).
