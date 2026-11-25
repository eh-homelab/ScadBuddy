# Inventory K1: the base model in Go — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every inventory kind has a class (§3), every step resolves to a handler (§4.2),
and `scadbuddy-core` owns kinds, items, lots, units and moves behind internal CRUD routes
that the Python public routes forward to. Every property set and remove is recorded
(§4.8). Epic #6949, phase K1 (§9).

**Architecture:** The property history is a database trigger on `entity_properties`, so
no writer can skip it (layer 1, on its own). A new Go package tree
`tools/go/core/internal/inventory/` holds the class, the handler catalog and resolver, the
K1 default handlers, the base flow, the SQL and the routes, as design §6.2 lays it out.
The catalog is Go code; a Go test loads `docs/inventory-model/*.yaml` and checks that the
catalog, the defaults, the special types and every example's resolution equal the proof.
One migration places every phase 2 row by §8. The Python routes under
`/api/v1/inventory/*` keep their pydantic contract. For a read they call
`/api/v1/internal/crud/inventory/*` on Go's CRUD port with a service credential of a new
caller, `inventory`, which the API mints in process. For a write they run a Temporal
Standalone Activity (`inventory.<Name>`) on the task queue `inventory`, which a worker
inside `scadbuddy-core` runs (§6.1a). The activity id is the idempotency key. The order
lifecycle, the printer merge and the `/parts` aliases stop writing stock: the lifecycle and
the merge start activities, and the aliases are deleted. After K1 no Python code writes a
stock table.

**Tech Stack:** Go 1.26 (`tools/go/core`, pgx v5, `go.temporal.io/sdk` v1.49.0 or newer,
`go.yaml.in/yaml/v3` for tests only), Postgres 18 (triggers, PL/pgSQL), Temporal server
1.32.1, Python 3.12 (FastAPI, psycopg 3, httpx, `temporalio` 1.36.0, the existing
`crud/client.py`), React + TypeScript (one panel), the agent's TypeScript tools.

**Spec:** `docs/superpowers/specs/2026-11-25-inventory-kinds-and-handlers-design.md` at
`c51dba825` (§3, §4.1–4.4, §4.8, §6.1, §6.1a, §6.2, §6.3, §8, §9 K1), its proof
`docs/inventory-model/`, and
`docs/superpowers/specs/2026-11-25-go-service-migration-design.md` (#7667: §4 ports and
areas, §5 what a ported route keeps, §5.1 shared SQL, §5.2 how we check). Executors read
both specs.

## What exists on main today (verified at `ed32aabf1`, 2026-11-25)

- **Go core.** `tools/go/core/main.go` `newRouter` mounts every area for the caller
  `agent` (`router.Add("agent", routes...)`). `crud.Router.refusal`
  (`internal/crud/crud.go:175`) refuses a credential whose caller is not the path's
  (`wrong_caller`) or lacks `crud.<caller>` (`out_of_scope`). A domain package exports
  `Areas(pool *pgxpool.Pool, keys *seal.Keys) map[string][]crud.Route`
  (`internal/ordercosts/areas.go`, #7709). `internal/testdb` migrates a template database
  from `backend/scadbuddy/migrations/` and the agent's. The module has no YAML package
  (`tools/go/core/go.mod`). `crud.Body` reads pydantic-shaped 422s (`internal/crud/crud.go`).
  `internal/seal/canonical.go` writes canonical JSON.
- **Callers.** `ServiceCaller` (`backend/scadbuddy/auth/service_auth.py:25-35`), `CrudCaller`
  (`crud/wire.py`), `CRUD_CLASSES` (`auth/routes.py:224-234`), `SCOPES`
  (`auth/service_credentials.py:102-111`) and the CHECK
  `service_credentials_caller_check` (`migrations/20261124T0222Z_camera_service_callers.sql`)
  list nine callers. `InProcessProvider(caller, store)` mints a credential in the API's own
  process (`auth/service_credentials.py:582`; used by `flows/component.py:118`).
  `CrudClient`/`CrudSyncClient` (`crud/client.py`) retry with one `Idempotency-Key`.
- **Idempotency.** `crud_requests (caller, key, route, body_sha256, status, body,
  created_at)` (`migrations/20261106T1836Z_crud_requests.sql:5`) and
  `crud/idempotency.py` (lock class `0x53435251`, `RETENTION = 24 h` l.49, `prune` l.182,
  run by `main.py:1765`).
- **Phase 2 inventory.** Tables `item_kinds`, `items`, `supplier_offers`,
  `offer_price_points`, `stock_lots`, `stock_units`, `stock_moves`, 20 built-in kinds
  (`migrations/20261124T0323Z_inventory_core.sql`); parts became `hardware` items
  (`20261124T0918Z_parts_to_items.sql`). Stores `inventory/kinds.py`, `items.py`,
  `offers.py`, `stock.py`, `part_alias.py`; routes `api/inventory.py` (kinds, items,
  offers) and `api/inventory_stock.py` (lots, units, moves, recost). Public models
  `inventory/models.py` (`ItemKind` l.49, `KindInput` l.64, `Item` l.101, `ItemInput`
  l.146) carry `tracking`, `life_basis`, `life_hours`, `life_months`, `compatible_with`.
  A place change writes no move (`stock.py` `update_lot` l.411-433, `update_unit`).
- **Stock writers outside `inventory/`.** `orders/lifecycle.py` through `StockStore`:
  `take` (l.5381), `give_back` (l.5424), `recost_lot` (l.5517), `lock_items` (l.5364),
  `estimate_unit_cost` (l.5475). `entities/registry.py:132` (`UPDATE stock_moves` in a
  merge). `printers/store.py:358` (`PrinterReference("stock_units", "printer_id")`).
  `inventory/part_alias.py` (the `/parts` aliases: items, offers, price points, lots).
  Readers only: `orders/parts.py:421`, `orders/store.py:2661` (`parts_moves`, which
  `orders/costs.py` uses), `entities/sweep.py:31`, `entities/registry.py:203`.
- **Properties.** `entity_properties` and `entity_property_schemas`
  (`migrations/20261123T0017Z_entity_properties.sql`). Writers: `entities/properties.py`
  `apply_on` (insert or update l.517, delete l.512), `set_schema` (l.561),
  `entities/registry.py` `repoint_entity` (copy l.135, `DELETE FROM entities` l.140, which
  cascades), `entities/sweep.py:41` (`DELETE FROM entities`). `KINDS` has no `item_kind`
  (`entities/registry.py:37-58`). `Actor.kind` is `browser`, `agent`, `provider`,
  `visitor` or `customer` (`orders/models.py:164-176`).
- **The `/parts` aliases.** `api/parts.py` serves `GET`/`POST /parts`, `PATCH
  /parts/{part_id}`, `POST …/stock`, `POST …/adjust` and `GET …/moves` through
  `inventory/part_alias.py`; the same file also serves `GET`/`PUT /parts/rules/{slug}` and
  `GET /orders/{order_id}/parts`, `PUT /orders/{order_id}/items/{item_id}/parts`, which are
  not aliases (they read and write `model_part_rules` and `order_item_parts`). Alias
  callers: the agent tools `part_list` (its no-`order_id` form) and `part_stock_add`
  (`agent/src/tools/orders.ts:1572-1633`), `agent/src/sessions/touched.ts:334`, five
  entries of `agent/src/tools/coverage.ts:136-151`, `agent/test/orders.test.ts:353-406`,
  `backend/tests/api/test_parts.py`, `docs/features/inventory.md:194`. The frontend calls no
  alias (`frontend/src/mocks/features/orders/parts.ts:16`). No plugin skill names them
  (`git grep part_list plugins` finds nothing).
- **Old problem codes and their readers.** `item_not_counted` and `wrong_tracking`:
  `inventory/problems.py:96-104`, `api/inventory_stock.py:8,46`,
  `agent/src/tools/inventory.ts:424-425,492`, `frontend/src/mocks/features/inventory.ts:462-464`,
  `backend/tests/api/test_inventory_stock.py:132-137,172`,
  `backend/tests/inventory/test_stock.py:653-686`, `docs/features/inventory.md:43`.
  `frontend/src/lib/problems.ts` names neither.
- **Printer merge.** `PrinterStore._merge(conn, gone, kept)` (`printers/store.py:603`) runs
  inside its callers' transactions (l.667, l.949); it repoints every
  `PRINTER_REFERENCES` row, including `stock_units.printer_id` (l.358, `ON DELETE SET
  NULL`), and `repoint_entity` moves `stock_moves.entity_id` (`entities/registry.py:132`, a
  plain FK, so it refuses the delete of a named entity).
- **Temporal.** The Python client connects with `pydantic_data_converter` (JSON payloads)
  and no TLS (`workflows/client.py:89-98`); settings `temporal_address`,
  `temporal_namespace` (`core/settings.py:648-649`). `temporalio` 1.36.0's
  `Client.start_activity` and `execute_activity` take `id`, `task_queue`,
  `schedule_to_close_timeout`, `start_to_close_timeout`, `id_reuse_policy`,
  `id_conflict_policy`, `retry_policy`, `summary` (read in the installed package,
  `temporalio/client/_client.py`); a start whose id is taken raises
  `temporalio.exceptions.ActivityAlreadyStartedError`, and `get_activity_handle(id)` reads
  its result. Go SDK v1.49.0: `client.StartActivityOptions` (`ID`, `TaskQueue`,
  `ScheduleToCloseTimeout`, `ScheduleToStartTimeout`, `StartToCloseTimeout`,
  `HeartbeatTimeout`, `ActivityIDConflictPolicy`, `ActivityIDReusePolicy` of
  `enumspb.ActivityIdConflictPolicy`/`ActivityIdReusePolicy`, `RetryPolicy`,
  `TypedSearchAttributes`), `Client.ExecuteActivity(ctx, options, activity, args...)
  (ActivityHandle, error)`, `GetActivityHandle`; a reused id is the server's
  `ActivityExecutionAlreadyStarted` (read in the module source,
  `internal/internal_activity_client.go:30-90`, `client/client.go:965-1634`, from
  https://proxy.golang.org/go.temporal.io/sdk/@v/v1.49.0.zip). Go patterns to follow
  (eh-homelab/clusters `tier0/bootstrap/internal/`, SDK v1.47.0): an `Activities` struct
  that holds the dependencies; each activity registered by name, never the whole struct
  (`wf/register_test.go`); a worker interceptor on `interceptor.WorkerInterceptorBase`
  and `ActivityInboundInterceptorBase` (`progress/interceptor.go`).
- **The activity record, the agent's pattern (§6.1a "Visibility").** The agent writes its
  own `ai_operations` row and publishes `ai_operation.changed`
  (`core/events.py:329-336` `AgentOperationEvent`, `operation_id: str`). The API's
  activity component listens on the bus and calls `ActivityIndex.ingest_agent_operation`
  (`activity/component.py:61-86`), which reads the row and writes an `ActivityRow` with
  `upsert_in` (`activity/index.py:402-440`: `id = "agent_operation:<id>"`, `kind =
  "agent.<kind>"`, `source = "ai_operations"`). `core/events.py` decodes a NOTIFY with one
  pydantic discriminated union (`Event`, l.453-487, `Field(discriminator="kind")`;
  `decode_event` l.506); every event model has `extra="forbid"` and the fields `id` (a
  UUID's 32 hex digits) and `at` (`BaseEvent`, l.80-84). A kind that is not in the union
  does not decode, so the new event needs a model there. A feature declares its activity
  kinds in its own `activity.py` as `ACTIVITY_KINDS` (`activity/kinds.py:15-16, 47-76`;
  for example `entities/activity.py`). Go publishes on `scadbuddy_events` with
  `pg_notify` and writes the payload as Python's `json.dumps(ensure_ascii=True)` does
  (`tools/go/core/internal/publish/publish.go`, `EventsChannel`, `ASCII`).
- **A 202 in the frontend.** `command<T>(path, init, operations, key, onAccepted)`
  (`frontend/src/api/client.ts:913-946`) sends one `Idempotency-Key`, takes a 202
  `OperationAccepted` (`operations/store.py:34-56`: `id`, `kind`, `subject`, `status`,
  `result`, `error`, `created_at`, `finished_at`, `repeated`), calls `onAccepted`, and
  follows `GET <operations>/{id}` to the result or the problem. The inventory pages call
  `request` directly today (`frontend/src/pages/inventory/api.ts:56-96`).
- **CI.** `select-go.sh` selects the `go` job from `tools/go/`, `vendor/`, the migrations
  and its own wiring; not `docs/` (`.github/scripts/select-go.sh:5`). `e2e-stack` runs no
  `scadbuddy-core`.

## Global Constraints

- Database access (#5295, #7667 §1): the API and `scadbuddy-core` are the only SQL holders.
  No new database client, role or grant. No untrusted code in `scadbuddy-core`.
- Migrations (#491): new files only, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in
  `backend/scadbuddy/migrations/`, later than every file on main when made. Never edit a
  merged one. The API applies them; Go never migrates (#7667 §4).
- Generated API files are never committed: `backend/openapi.json`, both `schema.d.ts`
  (root CLAUDE.md "Generated API files"). The Go client is regenerated by `freshness`.
- Anti-goals (§2): no `if kind == …`, no kind id, no special type id in the base flow or a
  default handler. A child kind never changes a class answer its parent gave. No fact in
  two places.
- Class values (§3): `nature` ∈ {`physical`, `service`}; `role` ∈ {`consumable`,
  `durable`} (physical only); `counting` ∈ {`lot`, `unit`} (physical only; `durable` is
  always `unit`); `billing` ∈ {`term`, `hourly`, `flat`} (service only).
- Steps, in this order: `receive`, `consume`, `count`, `move`, `retire`, `cost`, `life`,
  `bill`, `label`, `sync` (`handlers.yaml` `steps`). Resolution order (§4.2): class default,
  special type, kinds root to leaf, item, unit (parameters only). A layer that names a new
  handler drops the old parameters; a layer that sets only `params` merges them.
- Parameter types (`check.py` `TYPES`): `integer`, `number`, `string`, `boolean`, `list`;
  a boolean is never an `integer` or a `number`; `enum` limits a value.
- Crossing the line (§6.1a, owner 2026-11-25): every Python-to-Go write is a Temporal
  Standalone Activity on the task queue `inventory`, run by the worker in
  `scadbuddy-core`. Never an outbox table, a sweep, or a SQL transaction that spans both
  processes. Reads stay HTTP. Every caller starts the activity (`start_activity`) and waits on its handle up to 15 s;
  a public write then answers 202 (R-K1-24).
- Idempotency (§6.1, §6.1a): the activity id is the idempotency key. Every start sets
  `id_conflict_policy = USE_EXISTING` and `id_reuse_policy = ALLOW_DUPLICATE_FAILED_ONLY`
  (design §6.1a at `f791b4581`): a repeat while it runs attaches; a repeat after a failure
  or a timeout runs it again; a repeat after a success raises
  `ActivityAlreadyStartedError`, and the caller reads the stored result
  (`get_activity_handle(id).result()`). The activity also records the key with its answer
  in `crud_requests` (caller `inventory`, kept 7 days), because an activity runs at least
  once; a repeat with another input is the problem
  `https://scadbuddy.dev/problems/crud/idempotency-key-reused` (422).
- A slow write (owner, 2026-11-25): a public route waits up to 15 s; then it answers
  `202 Accepted` with `OperationAccepted` (the activity id). A repeat with the same key is
  never an error: while the activity runs it answers 202 again, and once it is done it
  answers the stored result.
- A refusal is an answer, not a failure: an activity returns `{status, body}` for every
  problem it decides (4xx), so Temporal does not retry it and a repeat of the key gets the
  same refusal. Only an infrastructure error (the database out of reach, a crash) fails
  the activity, and Temporal retries it.
- Each Go activity is one function, registered by name (`inventory.<Name>`), shared by a
  standalone start, a workflow step and the tests (§6.1a).
- A route handler or an activity holds no SQL and no rule beyond parsing and problem
  mapping (§6.2, R-P3-54). Every SQL statement is in `internal/inventory/store/`.
- What a ported route keeps (#7667 §5): CRUD headers on matched routes only, problem JSON,
  pydantic-shaped 422 `errors[].loc`, the body bound (413/411), `X-ScadBuddy-Crud: 1`.
  Each difference from Python goes in `tools/go/core/README.md` "Where it differs".
- Money and quantities as phase 2: `numeric(20,6)` minor units in the shop currency
  (`orders_currency`, default `USD`), unrounded; on the wire as plain decimal strings
  (`format(value.normalize(), "f")`). Go does decimal arithmetic in SQL or with `math/big`;
  no decimal dependency.
- Each layer runs, for the trees it touches: `cd tools/go/core && go vet ./... && go test
  -race ./...` (with `SCADBUDDY_TEST_DATABASE_URL`); `gofmt -l tools/go` prints nothing;
  `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . &&
  uv run --frozen mypy`; `cd frontend && pnpm lint && pnpm typecheck`; `cd agent && pnpm
  lint && pnpm typecheck`.
- Commits: conventional titles ending `(#6949, K1 n/7)`. PR bodies: `Part of #6949`. Every
  external fact links its source; every measured fact says where and how (#259).
- Write docs and comments in Simplified Technical English (ASD-STE100).

## Review Focus

1. **A write retried after a lost answer, or run twice by Temporal.** A second start with
   the same id attaches to the running activity or reads its result; an activity that
   runs twice (at least once) writes once, because `crud_requests` answers the second run;
   the same key with another input is 422; a refusal repeated gives the same refusal.
   Tests: Task 9 `TestARetryReplaysTheFirstAnswer`, `TestTwoRequestsWithOneKeyWriteOnce`;
   Task 12 `TestAnActivityRunTwiceWritesOnce`; Task 13
   `test_a_taken_id_reads_the_existing_result`.
2. **A property that goes with its entity.** The orphan sweep and a printer merge delete
   an `entities` row; the cascade removes its properties. Each remove is recorded with the
   entity's kind and ref and the sweep's or the merge's actor; a direct remove with no
   `scadbuddy.actor` is refused and changes nothing. Test: Task 1
   `test_a_cascade_records_each_key_with_the_deleting_actor`, Task 2
   `test_the_sweep_records_what_it_removes`.
3. **Phase 2 rows the rules did not foresee.** A kind a person made under `part` (now
   durable) with `tracking = lot`; a lot kind with `life_basis = hours`; an item with
   `life_hours` on a kind whose life handler is `life.months`; a lot of a `part` item with
   a fractional `on_hand`. The migration never aborts; it places each row and reports it.
   Test: Task 7 `test_every_phase_2_combination_is_placed`.
4. **A kind edit that would change its items' class.** A `PATCH` that moves a kind with
   items to a parent of another class, or a child that sets a key its parent set to
   another value, is refused (422 `class_changed`, the key named) and writes nothing.
   Test: Task 10 `TestAParentChangeThatChangesAClassIsRefused`.
5. **Go's worker slow or down while a person saves.** The activity waits on its queue.
   The public route stops waiting after 15 s and answers 202 with the activity id, never a
   500; the page shows "Still working" and follows it to its result. A second press with
   the same key answers 202 again, never an error, and starts nothing new. When the worker
   comes back the activity runs once. An activity that failed or timed out runs again on
   the same key, and `crud_requests` keeps it from writing twice. Temporal itself out of
   reach is 503 `inventory_unavailable`, and nothing was started. An order fulfil whose
   `inventory.Take` cannot start or does not finish in time answers an error that is safe
   to retry, and the retry takes stock once. Tests: Task 13
   `test_a_slow_write_answers_202_and_a_repeat_202_again`,
   `test_a_done_write_answers_the_stored_result`, `test_a_failed_activity_runs_again`,
   `test_temporal_out_of_reach_is_a_503`; `StillWorking.test.tsx`; Task 16
   `test_a_failed_start_fails_the_fulfil`, `test_a_fulfil_retry_takes_stock_once`.

---

## Layers

| Layer | Branch | What |
|---|---|---|
| 1 | `feat/6949-k1-1-property-history` (from main) | The property history trigger, its readers, the panel and the tool (§4.8). Lands on its own |
| 2 | `feat/6949-k1-2-inventory-model` (from main) | The class, the catalog and the resolver in Go, and the YAML test |
| 3 | `feat/6949-k1-3-inventory-migration` (on 2) | The §8 migration, `inventory_operations`, the caller `inventory`, the relation tables |
| 4 | `feat/6949-k1-4-inventory-core` (on 3) | The store, the default handlers, the base flow, idempotency, the Temporal worker and activities, the read routes |
| 5 | `feat/6949-k1-5-inventory-forwarders` (on 4) | The Python forwarders (reads over HTTP, writes as activities); the kind and item stores and the `/parts` aliases go; the new problem codes |
| 6 | `feat/6949-k1-6-order-stock-activities` (on 5) | The order lifecycle and the printer merge start activities; Python's stock writes go |
| 7 | `feat/6949-k1-7-inventory-docs` (on 6) | Docs |

Layers 1 and 2 are independent. Layers 3 to 6 deploy together (R-K1-18).

---

## Layer 1 — `feat/6949-k1-1-property-history`

### Task 1: The history tables and triggers

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_entity_property_history.sql`
- Test: `backend/tests/entities/test_property_history.py`, and one case in `backend/tests/test_pg_migrations.py`

**Interfaces:**
- Produces (SQL):
  - `entity_property_history (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, entity_id
    uuid NOT NULL` (no FK: the row outlives the entity)`, kind text NOT NULL, ref text NOT
    NULL, key text NOT NULL, op text NOT NULL CHECK (op IN ('set','remove')), old jsonb, new
    jsonb, source text, at timestamptz NOT NULL DEFAULT clock_timestamp(), actor jsonb NOT
    NULL)`; index `(entity_id, key, id)`; index `(kind, ref)`.
  - `entity_property_schema_history (id bigint identity PK, owner text NOT NULL` (`kind:<entity kind>` for `entity_property_schemas`, `item_kind:<id>` for `item_kinds.attribute_schema`)`, old jsonb, new jsonb NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp(), actor jsonb NOT NULL)`; index `(owner, id)`.
  - `scadbuddy_actor() RETURNS jsonb`: `current_setting('scadbuddy.actor', true)::jsonb`, NULL when unset or empty.
  - Trigger `entity_properties_history` AFTER INSERT OR UPDATE OR DELETE FOR EACH ROW
    (R-K1-12): INSERT → `set` from NULL; UPDATE → `set` only when `OLD.value IS DISTINCT
    FROM NEW.value`; DELETE → `remove`, actor `scadbuddy_actor()`, and `RAISE EXCEPTION
    'a property remove needs scadbuddy.actor' USING ERRCODE = 'P0001'` when it is NULL. A
    set's actor is `coalesce(scadbuddy_actor(), NEW.updated_by)`. `kind` and `ref` are read
    from `entities`; when the row is gone (a cascade), the trigger writes nothing.
  - Trigger `entities_property_history` BEFORE DELETE ON `entities` FOR EACH ROW: one
    `remove` row per property of the entity, with the entity's kind and ref, and the same
    actor rule and refusal (only when the entity has a property).
  - Triggers on `entity_property_schemas` (INSERT, UPDATE when the schema changes) and on
    `item_kinds` (UPDATE OF `attribute_schema` when it changes; INSERT): one schema history
    row each, actor `coalesce(scadbuddy_actor(), NEW.updated_by,
    '{"kind":"provider","principal":"migration"}')` (`item_kinds` has no `updated_by`).
  - Back-fill, in the same file: one `set` row per existing `entity_properties` row (`old`
    NULL, `at` = `updated_at`, actor `updated_by`), in `(entity_id, key)` order; one schema
    row per existing schema (actor as above).

- [ ] **Step 1: Write the failing tests** (`test_property_history.py`, the `pool` fixture of `tests/entities/conftest.py`; each write sets the actor with `SELECT set_config('scadbuddy.actor', %s, true)`):
  - `test_a_new_key_is_a_set_from_nothing`: set `colour_name = "Gold"` on `item:<id>` → one row `op='set', old=None, new="Gold", kind='item', ref=<id>`.
  - `test_a_set_to_the_same_value_writes_nothing`: set `"Silk Gold"` twice → one row for the second write, not two (the proof's 2027-01-21 event).
  - `test_a_change_keeps_the_old_value`: `"Gold"` → `"Silk Gold"` gives `old="Gold", new="Silk Gold"`.
  - `test_a_remove_needs_an_actor`: `DELETE FROM entity_properties` with no `scadbuddy.actor` raises `psycopg.errors.RaiseException` and the row is still there.
  - `test_a_remove_records_the_old_value_and_actor`: remove `material` with actor `{"kind":"browser"}` → `op='remove', old="hardened_steel", new=None, actor={"kind":"browser"}`.
  - `test_a_cascade_records_each_key_with_the_deleting_actor`: an entity with keys `a`, `b`; `DELETE FROM entities` with actor `{"kind":"provider","principal":"entities_sweep"}` → two `remove` rows with the entity's kind and ref and that actor; with no actor the delete raises and both keys stay.
  - `test_the_history_replays_to_the_current_properties`: 20 random sets and removes on three entities; for each entity, the last row per key (a `remove` drops it) equals `entity_properties`.
  - `test_a_schema_change_is_recorded`: `entity_property_schemas` `spool` set twice to the same schema → one row; `UPDATE item_kinds SET attribute_schema = …` → one row with `owner='item_kind:<id>'`.
- [ ] Add `test_property_history_backfills_existing_rows` to `test_pg_migrations.py`: migrate to the file before this one, insert two properties, migrate the rest → two `set` rows with `at` = each row's `updated_at`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/entities/test_property_history.py tests/test_pg_migrations.py -k "history" -q` — Expected: FAIL (no table).
- [ ] **Step 3: Write the migration** as the Interfaces say. A top comment cites #6949 and design §4.8.
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): every property set and remove is recorded with history (#6949, K1 1/7)`

### Task 2: Every Python writer sets the actor; the history routes

**Files:**
- Create: `backend/scadbuddy/entities/actor.py`
- Modify: `backend/scadbuddy/entities/properties.py` (`apply_on`, `set_schema`), `backend/scadbuddy/entities/registry.py` (`repoint_entity`), `backend/scadbuddy/entities/sweep.py`, `backend/scadbuddy/inventory/kinds.py` (each `item_kinds` write), `backend/scadbuddy/api/entity_properties.py`, `backend/scadbuddy/printers/store.py` (the merge that calls `repoint_entity`)
- Test: `backend/tests/entities/test_property_history.py`, `backend/tests/api/test_entity_properties.py`, `backend/tests/entities/test_sweep.py`

**Interfaces:**
- Produces `entities/actor.py`: `set_actor(conn: Conn, by: Actor) -> None` (`SELECT set_config('scadbuddy.actor', %s, true)` with `by.model_dump_json()`); `SWEEP_ACTOR = Actor(kind="provider", principal="entities_sweep")`.
- Produces `EntityPropertyStore.history(ref: EntityRef, key: str | None, limit: int) -> list[PropertyChange]` and `schema_history(owner: str, limit: int) -> list[SchemaChange]`, newest first. `PropertyChange {key, op, old, new, source, at, actor}`; `SchemaChange {old, new, at, actor}`.
- Produces routes: `GET /api/v1/entities/{ref}/properties/history?key=&limit=` → `{changes: PropertyChange[]}` (`limit` 1–500, default 100); `GET /api/v1/entity-kinds/{kind}/property-schema/history?limit=` → `{changes: SchemaChange[]}` (for `item` it is 409 `schema_owned_by_item_kind`, as R-P2-5); `GET /api/v1/inventory/kinds/{kind_id}/schema/history` is added in layer 5 with the forwarders (the kind's own `attribute_schema`, owner `item_kind:<id>`).
- Every transaction that deletes `entity_properties` or `entities` calls `set_actor` first: `apply_on` (with `by`), `repoint_entity` (gains a `by: Actor` parameter; the printer merge passes its actor), the sweep (`SWEEP_ACTOR`).

- [ ] **Step 1: Write the failing tests:**
  - `test_apply_on_records_a_remove_with_its_actor` (store): a patch `{"material": None}` by `{"kind":"agent","principal":"p"}` → one `remove` row with that actor.
  - `test_a_merge_records_each_key_it_moves`: entity `printer:a` with `x=1`, `printer:b` with `x=2`, `y=3`; `repoint_entity(conn, "printer", "b", "a", by)` → `printer:a` gets `set y` (actor `by`); `printer:b` gets `remove x`, `remove y` (actor `by`).
  - `test_the_sweep_records_what_it_removes` (`test_sweep.py`): an orphan with one key → one `remove` row, actor `SWEEP_ACTOR`.
  - `test_history_route_lists_newest_first` (API): two sets and a remove of `colour_name` → three changes, newest first; `?key=other` → none; `?limit=0` → 422.
  - `test_schema_history_route`: two `PUT /entity-kinds/spool/property-schema` → two changes; `item` → 409.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/entities tests/api/test_entity_properties.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** the Interfaces. Search for every `DELETE FROM entities` and `DELETE FROM entity_properties` (`git grep -n -E "DELETE FROM (entities|entity_properties)" backend/scadbuddy`) and check that each has `set_actor` in its transaction.
- [ ] **Step 4: Run** the tests, then `uv run --frozen pytest tests/printers tests/inventory -n auto -q` (merges and kind writes still pass) — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): writers name the actor; property and schema history routes (#6949, K1 1/7)`

### Task 3: The history in the panel and the agent's tool

**Files:**
- Modify: `frontend/src/components/entities/PropertiesPanel.tsx`, `frontend/src/components/entities/api.ts` (`entityApi`), `frontend/src/mocks/features/entities.ts`, `agent/src/tools/entities.ts`, `agent/src/tools/coverage.ts` if the route needs an entry
- Test: `frontend/src/components/entities/PropertiesPanel.test.tsx`, `frontend/src/mocks/features/entities.test.ts`, `agent/test/entityTools.test.ts`

**Interfaces:**
- Consumes: Task 2's routes.
- Produces: `entityApi.propertyHistory(ref: string, key?: string): Promise<PropertyChange[]>`. In `PropertiesPanel`, each key row has a "History" button that opens a list: date, `old → new` (or "removed"), and who (`actor.principal ?? actor.kind`).
- Produces the agent tool `entity_property_history` (risk `read`, input `{ref: string, key?: string, limit?: number}`), next to `entity_property_list`.

- [ ] **Step 1: Write the failing tests:** `shows a key's history newest first` (two changes from the mock; the second row reads `Gold → Silk Gold`); `shows a remove as removed`; the agent tool's test calls `GET /entities/item:x/properties/history?key=colour_name` and returns the changes as `untrusted_data`.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/entities/PropertiesPanel.test.tsx` and `cd agent && pnpm exec vitest run test/entityTools.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.** Load the `frontend-design` skill before the panel change.
- [ ] **Step 4: Run** the two commands, then `cd frontend && pnpm lint && pnpm typecheck` and `cd agent && pnpm lint && pnpm typecheck && pnpm exec vitest run test/coverage.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): a property's history in the panel and the agent (#6949, K1 1/7)`

---

## Layer 2 — `feat/6949-k1-2-inventory-model`

### Task 4: `modeldata` loads the proof

**Files:**
- Create: `tools/go/core/internal/inventory/modeldata/modeldata.go`
- Modify: `tools/go/core/go.mod`, `go.work.sum` if it changes, `vendor/` (`go work vendor`), `.github/scripts/select-go.sh`, `.github/scripts/select-go.test.sh`
- Test: `tools/go/core/internal/inventory/modeldata/modeldata_test.go`

**Interfaces:**
- Produces `modeldata.Dir() string` (the repo's `docs/inventory-model`, found from `runtime.Caller` as `testdb.build` finds `backend/scadbuddy`) and `modeldata.Load(dir string) (*Model, error)`.
- `Model{Steps []string; Handlers []Handler; Defaults map[string]Column; Special map[string]Special; Kinds []Kind; Items []Item; Instances []Instance; Relations []map[string]any; History []map[string]any; ExpectPropertyHistory map[string][]PropertyRow; Readme string}`. `Handler{ID string; Steps []string; Applies map[string]string; Params map[string]Param}`; `Param{Type string; Required bool; Enum []string}`; `Column{Match map[string]string; Handlers map[string]string}`; `Special{Needs map[string]string; Handlers map[string]string; RequiresProperties, Panels, Tools []string}`; `Kind{ID, Name, Parent, Special, UOM string; Class map[string]string; Steps map[string]StepConf; Schema map[string]any; Seed bool; Was map[string]any}`; `StepConf{Handler string; Params map[string]any}`; `Item{ID, Name, Kind string; Properties map[string]any; Steps map[string]StepConf}`; `Instance{ID, Item, Type string; StartsAt, EndsAt string}`; `PropertyRow{Key, At, Op string; Old, New any; Actor string}`.
- Dependency (R-K1-20): `go.yaml.in/yaml/v3`, the newest v3 tag that `go list -m -versions go.yaml.in/yaml/v3` shows (source: https://github.com/yaml/go-yaml). Only `modeldata` imports it.
- `select-go.sh` also selects `^docs/inventory-model/`; `select-go.test.sh` gets the case `docs/inventory-model/kinds.yaml → true`.

- [ ] **Step 1: Write the failing test** `TestTheProofLoads`: `Load(Dir())` gives 10 steps, 38 handlers, 6 default columns, 2 special types, 33 kinds, 16 items, 17 instances, 16 relations, 16 history events (the counts `check.py` printed on 2026-11-25, `docs/inventory-model/README.md`), and `Readme` holds `<!-- matrix:start -->`.
- [ ] **Step 2: Run** `cd tools/go/core && go test ./internal/inventory/modeldata/ -run TestTheProofLoads` — Expected: FAIL.
- [ ] **Step 3: Implement**; `go get go.yaml.in/yaml/v3@<tag>` in `tools/go/core`, then `go work vendor` at the root.
- [ ] **Step 4: Run** the test and `bash .github/scripts/select-go.test.sh` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): load the inventory model's YAML for the tests (#6949, K1 2/7)`

### Task 5: The class, the catalog and the resolver

**Files:**
- Create: `tools/go/core/internal/inventory/model/class.go`, `tools/go/core/internal/inventory/model/kind.go`, `tools/go/core/internal/inventory/handlers/catalog.go`, `tools/go/core/internal/inventory/handlers/resolve.go`, `tools/go/core/internal/inventory/handlers/registry.go`
- Test: `tools/go/core/internal/inventory/model/class_test.go`, `tools/go/core/internal/inventory/handlers/resolve_test.go`

**Interfaces:**
- `package model`:
  - `type Class struct{ Nature, Role, Counting, Billing string }` ("" is open). `func (c Class) Narrow(child Class) (Class, error)` returns `*ClassChanged{Key, From, To string}` when the child changes a set key. `func (c Class) Complete() bool` (`physical` needs role and counting; `service` needs billing). `func (c Class) Check() error` (values in range; `durable` with `counting = lot` is `ErrDurableLot`; a physical key on a service is an error).
  - `type StepConf struct{ Handler string \`json:"handler,omitempty"\`; Params map[string]any \`json:"params,omitempty"\` }`; `type Steps map[string]StepConf`.
  - `type Kind struct{ ID, Name, ParentID, Special, UOM string; Class Class; Steps Steps; Schema json.RawMessage; Saleable, Builtin bool; ArchivedAt *time.Time }`. `type Chain []Kind` (root first). `func (ch Chain) Class() (Class, string, error)`: narrows root to leaf; a second special type is `ErrSecondSpecial`.
- `package handlers`:
  - `type Step string`; `var Steps = []Step{"receive", …, "sync"}` (Global Constraints order).
  - `type Param struct{ Type string; Required bool; Enum []string }`; `type Spec struct{ ID string; Steps []Step; Applies map[string]string; Params map[string]Param }` (`Applies` keys: the four class keys and `special`).
  - `type Column struct{ Name string; Match model.Class; Handlers map[Step]string }`; `type SpecialType struct{ ID string; Needs model.Class; Handlers map[Step]string; RequiresProperties, Panels, Tools []string }`.
  - `type Catalog struct{ Specs map[string]Spec; Columns []Column; Special map[string]SpecialType }`; `func Builtin() *Catalog`: every handler, column and special type of `handlers.yaml`, written as Go literals.
  - `type Choice struct{ Handler string; Params map[string]any }`; `type Resolution struct{ Class model.Class; Special string; Steps map[Step]Choice }`.
  - `type Problem struct{ Step Step; Handler, Reason string; Missing []string }`. Reasons: `unknown_handler`, `wrong_step`, `not_applicable`, `unknown_param`, `wrong_type`, `not_in_enum`, `missing_param`, `class_open`, `special_needs`.
  - `func (c *Catalog) Resolve(chain model.Chain, item, unit model.Steps) (Resolution, []Problem, error)`: §4.2 as `check.py` `resolve` and `apply_steps` do; `error` for a class that does not resolve (`*model.ClassChanged`, `ErrDurableLot`, `ErrSecondSpecial`, `class_open`); `[]Problem` for each step. A `unit` layer that names a handler is the problem `unit_names_handler`.
  - `func Blocking(ps []Problem) []Problem` (every problem but `missing_param`) and `func Incomplete(ps []Problem) []Problem` (only `missing_param`) (R-K1-8).
  - `registry.go`: `type Registry struct{ Catalog *Catalog }`; `func (r *Registry) Implement(id string, impl any)`; `func (r *Registry) For(step Step, id string) (any, error)` returns `ErrNotBuilt{Handler}` when no implementation is registered (R-K1-7). The per-step interfaces are declared in Task 11.

- [ ] **Step 1: Write the failing tests:**
  - `TestAChildMayAnswerWhatItsParentLeftOpen`: `{physical, consumable}` narrowed by `{counting: unit}` → `{physical, consumable, unit}`.
  - `TestAChildMayNotChangeAnAnswer`: `{physical, consumable}` narrowed by `{role: durable}` → `*ClassChanged{Key: "role", From: "consumable", To: "durable"}`.
  - `TestADurableIsCountedByUnit`: `{physical, durable, lot}.Check()` → `ErrDurableLot`.
  - `TestResolveFollowsTheLayers`: the chain `durable → part → nozzle` with the item steps `life: {params: {hours: 1500}}` resolves `life` to `life.hours` with `{hours: 1500, source: container_run_hours, warn_pct: 90}`.
  - `TestANewHandlerDropsTheOldParams`: kind `life: {handler: life.months, params: {months: 60}}`, item `life: {handler: life.hours, params: {hours: 10, source: manual}}` → params `{hours: 10, source: manual}` only.
  - `TestResolveProblems`, table-driven, one row each: an unknown handler; `count.lot` on `life`; `bill.hourly` on a physical class; a parameter `hourz`; `hours: "ten"`; `hours: true` (a boolean is no number); `source: "solar"` (not in enum); `life.hours` with no `hours` (`missing_param`, and `Blocking` is empty); a unit layer that names a handler.
  - `TestASpecialTypeNeedsItsClass`: `filament_spool` on `{physical, durable, unit}` → `special_needs`.
- [ ] **Step 2: Run** `cd tools/go/core && go test ./internal/inventory/model/ ./internal/inventory/handlers/` — Expected: FAIL.
- [ ] **Step 3: Implement.** No branch in `Resolve` names a kind, a handler or a special type.
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): the inventory class, handler catalog and resolver (#6949, K1 2/7)`

### Task 6: The YAML test

**Files:**
- Create: `tools/go/core/internal/inventory/modeldata/proof_test.go`, `tools/go/core/internal/inventory/modeldata/convert.go` (YAML kinds and items to `model.Chain` and `model.Steps`)
- Test: `tools/go/core/internal/inventory/modeldata/proof_test.go`, `tools/go/core/internal/inventory/modeldata/property_history_test.go`

**Interfaces:**
- Consumes: Task 4 `Load`, Task 5 `Builtin`, `Resolve`; Task 1's triggers (through `testdb.New`).
- Produces `func Chain(m *Model, kindID string) (model.Chain, error)` and `func Matrix(m *Model, cat *handlers.Catalog) (string, error)` (the README table, as `check.py` builds it: `—` for `refuse`, the class `nature/role/counting` or `nature/billing`, `+ <special>`).

K1 ports `check.py` up to the README table (R-K1-19). Its relation checks and its history replay of lives and cached values come with K2, which builds those handlers.

- [ ] **Step 1: Write the failing tests** (`proof_test.go`):
  - `TestTheCatalogIsTheProof`: `Builtin()`'s specs, columns and special types equal `handlers.yaml`'s (id, steps, applies, params; column matches and handlers; needs, handlers, requires_properties, panels, tools).
  - `TestEveryKindHasAClass`: each of the 33 kinds' `Chain(...).Class()` has no error.
  - `TestEveryExampleItemResolves`: each of the 16 items resolves with no problem; its `requires_properties` and its kinds' `schema` `required` keys are among its properties (as `check.py` `resolve`).
  - `TestEveryInstanceFitsItsClass`: a lot only for `counting: lot`; a service instance only for `billing: term`, with `starts_at` and `ends_at`.
  - `TestTheReadmeTableIsTheResolvedOne`: `Matrix(...)` equals the text between `<!-- matrix:start -->` and `<!-- matrix:end -->`.
- [ ] Write `TestPropertyHistoryReplaysTheProof` (`property_history_test.go`, `testdb.New`): for `pla_plus_gold` and `nozzle_hs_04`, insert an `entities` row (`kind='item'`, a new uuid ref); set each initial property in key order with actor `{"kind":"provider","principal":"import"}`; then apply the `set_property` and `remove_property` events of `examples.yaml` `history` with actor `{"kind":"browser"}`. The history rows, read in id order as `(key, op, old, new, actor name)` with `import` and `person` for the two actors, equal `expect_property_history` (`at` is not compared). The last row per key equals `entity_properties`.
- [ ] **Step 2: Run** `cd tools/go/core && go test ./internal/inventory/modeldata/` — Expected: FAIL.
- [ ] **Step 3: Implement** `convert.go` and `Matrix`.
- [ ] **Step 4: Run** the same, then `uvx --with pyyaml python -I docs/inventory-model/check.py` (the proof still passes) — Expected: PASS, and `ok: 33 kinds, 38 handlers, …`.
- [ ] **Step 5: Commit** `test(core): the inventory proof is the resolver's test (#6949, K1 2/7)`

---

## Layer 3 — `feat/6949-k1-3-inventory-migration` (on layer 2)

### Task 7: The §8 migration

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_inventory_class.sql`
- Modify: `backend/scadbuddy/entities/registry.py` (`KINDS` gets `_kind("item_kind", "item_kinds")`; `orphans` skips an entity a `relations` row names), `backend/scadbuddy/entities/sweep.py` (the same skip under the lock)
- Test: `backend/tests/test_pg_migrations.py`, `tools/go/core/internal/inventory/modeldata/seed_test.go`, `backend/tests/entities/test_sweep.py`

**Interfaces:**
- Schema (design §6.3):
  - `item_kinds` ADD `nature text CHECK (nature IN ('physical','service'))`, `role text CHECK (role IN ('consumable','durable'))`, `counting text CHECK (counting IN ('lot','unit'))`, `billing text CHECK (billing IN ('term','hourly','flat'))`, `special_type text CHECK (special_type ~ '^[a-z][a-z0-9_]{0,39}$')`, `steps jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(steps) = 'object')`. All class columns are nullable: a kind leaves a key open (§3).
  - `items` ADD `steps jsonb NOT NULL DEFAULT '{}'`; `stock_units` ADD `steps jsonb NOT NULL DEFAULT '{}'`, `starts_at timestamptz`, `ends_at timestamptz`; `stock_units.status` CHECK gains `ended`.
  - `relation_types (id text PK CHECK (id ~ '^[a-z][a-z0-9_]{0,39}$'), label text NOT NULL, inverse_label text NOT NULL, from_rule jsonb NOT NULL, to_rule jsonb NOT NULL, max_from_per_to int, handler text, attrs text[] NOT NULL DEFAULT '{}', builtin boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now())`, seeded with the five types of `relations.yaml` (`builtin = true`).
  - `relations (id uuid PK DEFAULT gen_random_uuid(), type_id text NOT NULL REFERENCES relation_types, from_entity uuid NOT NULL REFERENCES entities, to_entity uuid NOT NULL REFERENCES entities, attrs jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT clock_timestamp(), created_by jsonb NOT NULL, ended_at timestamptz, CHECK (from_entity <> to_entity))`; unique `(type_id, from_entity, to_entity) WHERE ended_at IS NULL`; index `(to_entity) WHERE ended_at IS NULL`.
  - `handler_config_history (id bigint identity PK, owner text NOT NULL` (`item_kind:<id>`, `item:<uuid>`, `unit:<uuid>`)`, step text NOT NULL, handler text, old jsonb, new jsonb, at timestamptz NOT NULL DEFAULT clock_timestamp(), actor jsonb NOT NULL)` (R-K1-13).
  - `inventory_k1_report (rule text PRIMARY KEY, count int NOT NULL, detail jsonb NOT NULL DEFAULT '[]')`, one row per rule below (R-K1-9). K2 drops it.
  - `inventory_operations (id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 300)` (the activity id)`, kind text NOT NULL` (the activity name, `inventory.Take`)`, subject text NOT NULL DEFAULT ''` (`item:<id>`, `lot:<id>`, `unit:<id>`, `item_kind:<id>`, `printer:<id>`)`, status text NOT NULL CHECK (status IN ('running','succeeded','failed')), attempt int NOT NULL DEFAULT 1, actor jsonb, problem jsonb, last_error text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz)`; index `(status, updated_at)`. Only Go writes it (R-K1-22). The result body stays in `crud_requests` (7 days); the row keeps the problem.
- Data, in this order (each step writes its report row; the actor of every row is `{"kind":"provider","principal":"inventory_k1_migration"}`):
  1. `seeded_kinds`: insert `durable`, `general_consumable`, `warranty`, `service_plan`, `labour`, `flat_service`; set name, parent, class, special type and steps of every built-in kind from `kinds.yaml` (`spool_part` keeps its id and becomes "Spool insert"; `ams_part` and `hardware` move under `consumable` with `counting = lot`). `printer` gets no `special_type` (R-K1-7).
  2. `person_kinds`: each `builtin = false` kind gets the class of its `tracking` (`lot` → consumable, lot; `unit` → durable; `none` → service, flat; `filament` → under `filament`), setting only the keys its parent leaves open. A kind whose rule conflicts with its parent's class, and a person root kind, move under the root of their class (`consumable`, `durable`, `service`). Detail: each moved kind with its old parent.
  3. `kind_life`: `life_basis = hours` → `steps.life = {handler: life.hours, params: {source: own_run_hours}}`; `months` → `{handler: life.months, params: {}}`; only on a kind whose class is durable. A non-durable kind's basis is dropped and named in the detail.
  4. `items_on_roots`: items on `consumable` move to `general_consumable`; items on `service` to `flat_service`.
  5. `item_life`, `unit_life`: `items.life_hours` → `steps.life.params.hours` when the item's resolved life handler is `life.hours`; else `{handler: life.hours, params: {hours, source: own_run_hours}}` on a durable item; else dropped and named. The same for `life_months` and `life.months`. `stock_units.life_*` → `steps.life.params` only where the unit's resolved handler takes the key; else dropped and named. The columns stay, unwritten, until K2.
  6. `part_lots`: each lot of an item whose kind is now durable with `on_hand > 0` gets an `adjust` move of `-floor(on_hand)` and `floor(on_hand)` units at the lot's `unit_cost_minor` (`origin = 'opening'`, one `receive` move each). A fraction left is named in the detail. Expected: 0 rows on prod (§8, scadbuddy-09, 2026-11-24).
  7. `model_codes`: each distinct trimmed `items.compatible_with` code becomes a kind under `printer`: id `'printer_' || left(regexp_replace(lower(code), '[^a-z0-9]+', '_', 'g'), 32)`, name the code as the oldest item spells it; codes that give one id share it. An `entities` row `('item_kind', id)` for each such kind and `('item', id)` for each item, and one `compatible_with` relation per item and code. An item of kind `printer` with exactly one code takes that model kind; one with more stays and is named.
  8. `tracking_derived`: `tracking` and `life_basis` are rewritten from the class and `steps.life` by R-K1-6, so the SQL readers stay right.
- `registry.py`: `item_kind` is an entity kind on `item_kinds` (text ids). `orphans` and the sweep skip an entity that a `relations` row names (as R-P2-4 does for moves).

- [ ] **Step 1: Write the failing tests** (`test_pg_migrations.py`, with a `_before_inventory_class` helper like `_before_parts`):
  - `test_inventory_class_places_the_builtin_kinds`: after the migration, `printer` is `(durable parent, physical, durable, unit, special NULL, steps.life.handler = 'life.hours')`; `spool_part` is named "Spool insert"; `hardware` is under `consumable` with `counting = 'lot'`; the six new kinds exist.
  - `test_every_phase_2_combination_is_placed`: before the migration, one person kind for each `tracking` (4) × `life_basis` (3) × parent (`NULL`, `part`, `consumable`, `equipment`), one item on each, with `life_hours` 100 on every third; after it, every kind's chain resolves to a complete or open class with no conflict (a SQL check: no child sets a key its parent set to another value), every report row exists, and the report's moved and dropped details name the kinds the rules say.
  - `test_items_on_roots_move`: an item on `consumable` is on `general_consumable`; one on `service` is on `flat_service`.
  - `test_part_lots_become_units`: a `part` item with a lot of 2.5 at 300 → 2 units at 300, an `adjust` of −2, the lot's `on_hand` 0.5, and the detail names the 0.5.
  - `test_model_codes_become_kinds`: items with `{"H2C"}`, `{"h2c", "X1C"}` → kinds `printer_h2c` (name `H2C`) and `printer_x1c`; three `compatible_with` relations; a `printer` item with `{"H2C"}` is now on `printer_h2c`.
  - `test_item_life_moves_into_steps`: a `nozzle` item with `life_hours = 1500` has `steps = {"life": {"params": {"hours": 1500}}}`; a `tool` item (months) with `life_hours = 50` gets `{"life": {"handler": "life.hours", "params": {"hours": 50, "source": "own_run_hours"}}}`.
- [ ] Write `TestSeededKindsMatchTheProof` (`seed_test.go`, `testdb.New`): every kind of `kinds.yaml` that is a root, has `seed: true` or has `was.tracking` is in `item_kinds` with the same parent, class, uom, name and steps; `special_type` equals `kinds.yaml`'s except `printer`, which is NULL until K6.
- [ ] Write `test_an_entity_a_relation_names_survives_the_sweep` (`test_sweep.py`).
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_pg_migrations.py -k inventory_class tests/entities/test_sweep.py -q` and `cd tools/go/core && go test ./internal/inventory/modeldata/ -run Seeded` — Expected: FAIL.
- [ ] **Step 3: Write the migration** and the registry change. The migration takes `LOCK TABLE item_kinds, items, stock_lots, stock_units IN EXCLUSIVE MODE` first (as `parts_to_items` does, R-P2-20).
- [ ] **Step 4: Run** the commands, then `uv run --frozen pytest tests/inventory tests/api/test_inventory.py tests/api/test_inventory_stock.py -n auto -q` (phase 2 still works on the new schema) — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): kinds get a class; phase 2 rows placed by the K1 rules (#6949, K1 3/7)`

### Task 8: The `inventory` service caller

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_inventory_service_caller.sql`
- Modify: `backend/scadbuddy/auth/service_auth.py` (`ServiceCaller`), `backend/scadbuddy/auth/service_credentials.py` (`SCOPES`), `backend/scadbuddy/crud/wire.py` (`CrudCaller`), `backend/scadbuddy/auth/routes.py` (`CRUD_CLASSES`), `backend/scadbuddy/crud/idempotency.py` (`prune`)
- Test: `backend/tests/auth/test_service_credentials.py`, `backend/tests/crud/test_idempotency.py`

**Interfaces:**
- The caller `inventory`, scope `crud.inventory` only, minted in process by the API (R-K1-3). The migration extends `service_credentials_caller_check` and `service_caller_suspensions_caller_check` with `'inventory'` (as `20261124T0222Z_camera_service_callers.sql` did). No other caller (R-K1-22).
- `idempotency.py`: `RETENTION_BY_CALLER: Final = {"inventory": timedelta(days=7)}`; `prune` deletes a row older than its caller's retention, else `RETENTION` (R-K1-2).

- [ ] **Step 1: Write the failing tests:** `test_the_api_mints_an_inventory_credential_in_process` (`InProcessProvider("inventory", …).token()` resolves with scopes `{"crud.inventory"}`); `test_prune_keeps_inventory_keys_seven_days` (rows of `inventory` 2 days and 8 days old, and of `print` 2 days old → only the `inventory` row of 2 days stays).
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/auth/test_service_credentials.py tests/crud/test_idempotency.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `uv run --frozen pytest tests/auth -q` (route coverage) — Expected: PASS.
- [ ] **Step 5: Commit** `feat(auth): the inventory service caller (#6949, K1 3/7)`

---

## Layer 4 — `feat/6949-k1-4-inventory-core` (on layer 3)

### Task 9: The store: transactions, actor, idempotency, shared SQL

**Files:**
- Create: `tools/go/core/internal/inventory/store/tx.go`, `tools/go/core/internal/inventory/store/idempotency.go`, `tools/go/core/internal/inventory/store/entities.go`, `backend/scadbuddy/entities/shared/ensure_entity.sql`, `backend/scadbuddy/crud/shared/crud_requests_select.sql`, `backend/scadbuddy/crud/shared/crud_requests_insert.sql`
- Modify: `backend/scadbuddy/entities/registry.py` (`ensure` reads its statement from the shared file), `backend/scadbuddy/crud/idempotency.py` (`stored`, `record` read theirs)
- Test: `tools/go/core/internal/inventory/store/idempotency_test.go`, `backend/tests/crud/test_shared_sql.py`

**Interfaces:**
- `type Actor = json.RawMessage` (an `Actor` JSON object, checked by `ReadActor`).
- `type Answer struct{ Status int \`json:"status"\`; Body json.RawMessage \`json:"body"\` }`: what every write gives, a success or a problem.
- `func Write(ctx context.Context, pool *pgxpool.Pool, w WriteReq, do func(pgx.Tx) (status int, body any, err error)) (Answer, error)` where `WriteReq{Caller, Key, Route string; Input []byte; Actor Actor}`. In one transaction: `set_config('scadbuddy.actor', actor, true)`; `pg_advisory_xact_lock(1396920913, hashtext(caller || E'\x1f' || key))` (`0x53435251`, `crud/idempotency.py` `LOCK_CLASS`); a stored answer → return it; the same key with another route or input digest → `Answer{422, idempotency-key-reused problem}`; else `do`, then insert the answer. A problem that `do` decides is an `Answer` and is recorded; an `error` from `do` records nothing and is returned (the activity fails, Temporal retries).
- Digest (R-K1-2): SHA-256 hex of the input in `seal` canonical JSON. Only Go writes `inventory` rows, so the digest need not be Python's.
- `func EnsureEntity(ctx, tx pgx.Tx, kind, ref string) (uuid string, err error)`: the statement of `entities/shared/ensure_entity.sql`, the text Python's `EntityStore.ensure` runs (#7667 §5.1).
- Shared SQL (#7667 §5.1): each file holds one statement; a Go test and a Python test each compare their statement's text, whitespace-normalized, with it.

- [ ] **Step 1: Write the failing tests** (`idempotency_test.go`, `testdb.New`):
  - `TestARetryReplaysTheFirstAnswer`: a write that inserts one row, run twice with key `k` → one row, both answers byte-equal, one `crud_requests` row of caller `inventory`.
  - `TestAKeyWithAnotherInputIsRefused` → `Answer.Status == 422`, type `…/idempotency-key-reused`, nothing written.
  - `TestTwoRequestsWithOneKeyWriteOnce`: two goroutines, one key → one row, two equal answers.
  - `TestARefusalIsRecorded`: `do` answers 409 → recorded; a repeat gets the 409 and `do` does not run.
  - `TestAFailedWriteRecordsNothing`: `do` returns an error → no `crud_requests` row; a retry runs `do` again.
  - `TestTheActorIsSetInTheTransaction`: `do` reads `current_setting('scadbuddy.actor')` and gets the actor.
  - `TestSharedStatementsMatch` (Go) and `test_shared_statements_match` (Python).
- [ ] **Step 2: Run** `cd tools/go/core && go test ./internal/inventory/store/` and `cd backend && uv run --frozen pytest tests/crud/test_shared_sql.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same, then `uv run --frozen pytest tests/crud tests/entities -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): inventory transactions with idempotency keys and the actor (#6949, K1 4/7)`

### Task 10: Kinds and items

**Files:**
- Create: `tools/go/core/internal/inventory/store/kinds.go`, `tools/go/core/internal/inventory/store/items.go`, `tools/go/core/internal/inventory/store/relations.go`, `tools/go/core/internal/inventory/store/config_history.go`, `tools/go/core/internal/inventory/wire/wire.go`
- Test: `tools/go/core/internal/inventory/store/kinds_test.go`, `tools/go/core/internal/inventory/store/items_test.go`

**Interfaces:**
- `package wire`: the JSON of `inventory/models.py`, field for field: `ItemKind` (phase 2 fields, plus `nature`, `role`, `counting`, `billing` (`string|null`), `special_type`, `steps`, `incomplete_steps: string[]`); `KindInput`, `KindPatch` (phase 2 fields plus the class keys, `special_type` and `steps`; `tracking` and `life_basis` still taken, R-K1-6); `Item` (phase 2 fields plus `steps`, `incomplete_steps`, `class: {nature, role, counting, billing}`, `special_type`); `ItemInput`, `InventoryItemPatch` (plus `steps`). Readers derive `tracking`, `life_basis`, `life_hours`, `life_months` (R-K1-6) and `compatible_with` (the model kinds' names, R-K1-11).
- `store.Kinds`: `List(ctx, q, archived bool)`, `Get`, `Create(ctx, tx, in wire.KindInput, by Actor)`, `Update(ctx, tx, id, patch, by)`, `Archive`, `Restore`, `Schema(ctx, q, id) (merged, own json.RawMessage, keysInUse [][2]any, err)`. Each save: build the chain, `Resolve` with no item; `Blocking` problems → `*Refused{Status: 422, Code: "steps_invalid", Problems}`; a class conflict → 422 `class_changed` (key, from, to); a parent change that changes the class of a kind with items (its own or its descendants') → 422 `class_changed`; writes `handler_config_history` for each step whose handler or params changed; rewrites `tracking`, `life_basis`.
- `store.Items`: `List(ctx, q, f ItemFilter)` (phase 2 filters `kind`, `descendants`, `q`, `low`, `archived`, `compatible` (a model kind by name, through `relations`), `limit` 1–5000 default 1000), `Get`, `Create`, `Update`, `Archive`, `Restore`. A save resolves with the item's steps: `Blocking` → 422 `steps_invalid`; an open class → 422 `class_open`; an item whose resolved special type is `filament_spool` → 409 `filament_item_refused` (phase 2 Ruling 9, until K3). `compatible_with` codes become `compatible_with` relations to model kinds, made under `printer` when missing (the §8 id rule).
- Errors keep phase 2's problem codes where the concept stays (`inventory/problems.py`): `kind_not_found`, `kind_exists`, `kind_in_use`, `kind_cycle`, `kind_archived`, `item_not_found`, `item_exists`, `bad_property_schema`, `filament_item_refused`.

Phase 2's `tests/inventory/test_kinds.py` and `test_items.py` cases are rewritten here, one Go test per Python test of the same name in CamelCase (#7667 §5.2 step 4); layer 5 deletes the Python ones.

- [ ] **Step 1: Write the failing tests:** the ported cases, and:
  - `TestAKindWithLifeHoursAndNoHoursIsIncomplete`: a durable kind with `steps.life = {handler: life.hours, params: {source: manual}}` saves; `incomplete_steps == ["life"]`.
  - `TestAKindWithAWrongHandlerIsRefused`: `steps.count = {handler: count.lot}` on a durable kind → 422 `steps_invalid`, problem `{step: count, handler: count.lot, reason: not_applicable}`, no row.
  - `TestAParentChangeThatChangesAClassIsRefused`: a consumable kind with one item, `PATCH parent_id = durable` → 422 `class_changed` `{key: role, from: consumable, to: durable}`.
  - `TestTrackingIsDerived`: a kind made with `{nature: service, billing: flat}` reads `tracking = none`; a `KindInput` with only `tracking: unit` under `durable` saves as durable; `life_basis` reads `hours` for `life.hours`.
  - `TestCompatibleWithBecomesRelations`: an item made with `compatible_with: ["H2C"]` reads it back; `List(compatible: "H2C")` finds it; a new code `P1S` makes `printer_p1s`.
  - `TestAStepChangeIsInTheConfigHistory`: an item's `steps.life.params.hours` 1500 → 2000 → one row, `old` and `new` as the resolved params, `owner = item:<id>`.
- [ ] **Step 2: Run** `cd tools/go/core && go test ./internal/inventory/store/ -run 'Kind|Item|Compatible|Tracking|Config'` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): inventory kinds and items with class and steps (#6949, K1 4/7)`

### Task 11: The default handlers and the base flow

**Files:**
- Create: `tools/go/core/internal/inventory/store/stock.go` (lots, units, moves, repoint), `tools/go/core/internal/inventory/handlers/steps.go` (the per-step interfaces), `tools/go/core/internal/inventory/handlers/receive.go`, `consume.go`, `count.go`, `move.go`, `retire.go`, `cost.go`, `refuse.go`, `tools/go/core/internal/inventory/flow/flow.go`
- Test: `tools/go/core/internal/inventory/flow/flow_test.go`, `tools/go/core/internal/inventory/store/stock_test.go`, `tools/go/core/internal/inventory/handlers/purity_test.go`

**Interfaces:**
- `handlers/steps.go` (README §8's class diagram; one interface per step):
  `Receiver{ Receive(ctx, tx pgx.Tx, in ReceiveIn) (ReceiveOut, error) }`,
  `Consumer{ Consume(ctx, tx, ConsumeIn) ([]wire.Move, error) }`,
  `Counter{ Count(ctx, tx, CountIn) ([]wire.Move, error) }`,
  `Mover{ Move(ctx, tx, MoveIn) error }`,
  `Retirer{ Retire(ctx, tx, RetireIn) (wire.Unit, error) }`,
  `Coster{ Estimate(ctx, q pgx.Tx, item string, qty *big.Rat) (unit *big.Rat, unknown bool, err error) }`.
  Each `In` carries `Item wire.Item`, `Choice handlers.Choice` (its params), `By store.Actor`, and the phase 2 input fields (`ReceiveLot`, `ReceiveUnits`, `UseInput` with `order_id` and `order_item_id`, `AdjustInput`, `CountInput`, `LotPatch.location_id`, `UnitPatch.location_id`, `UnitStatusInput`).
  `Lifer`, `Biller`, `Labeler`, `Syncer` are declared and implemented by nothing in K1 (seams for K2–K6).
- Implementations (R-K1-1: each ports its phase 2 path in `inventory/stock.py`, statement for statement, with its locks: `_open_lock` `pg_advisory_xact_lock(hashtext('stock_lots_open'), hashtext(item_id))`, lots `FOR UPDATE` in `(received_at, id)` order):
  `receive.lot` (`receive_in`), `receive.units` (`receive_units`), `consume.lot_fifo` (`take`, `use`), `count.lot` (`count_item`; `adjust_lot` is `count.lot` on one lot), `move.place` (the location of a lot or a unit; no stock move, `handlers.yaml` at `c51dba825`), `retire.default` (`set_unit_status`, every status), `cost.lot_fifo` (`estimate_unit_cost`, and the snapshot `take` writes), `cost.unit` (a unit's cost), `refuse` (409 `step_not_applicable` with the step and the class).
  Every other catalog handler has no implementation: `Registry.For` answers `ErrNotBuilt`, which is 409 `handler_not_built` (R-K1-7).
- `flow.Flow{Pool *pgxpool.Pool; Reg *handlers.Registry}` with `Receive`, `Consume`, `Count`, `Move`, `Retire`, `GiveBack`, `Recost`, `Repoint`, `Estimate`, `SetLotCost`. Each write: `store.Write` (Task 9); lock and read the item, its chain and the unit's steps; `Resolve`; a step whose choice has a `missing_param` problem → 409 `step_incomplete` (R-K1-8); `Reg.For(step, choice.Handler)`; call it in the transaction. `GiveBack`, `Recost` and `SetLotCost` keep phase 2's paths (`give_back`, `recost_lot`, `set_lot_cost`); they are not steps. `Recost` takes the ids of the orders its caller locked and answers 409 `recost_orders_moved`, writing nothing, when a move of the lot names another order (phase 2's `_OrdersMoved`, now checked under the lot's lock). `Repoint(kind, gone, kept)` moves `stock_moves.entity_id` from the entity `kind:gone` to `kind:kept`, and for `kind = printer` also `stock_units.printer_id`; a repeat finds nothing to move.
- A lots route on a unit-counted item, or the reverse, is 409 `wrong_counting` (it replaces phase 2's `wrong_tracking`, R-K1-23).
- `purity_test.go`: a test that parses `handlers/*.go` and `flow/*.go` with `go/parser` and fails on a string literal equal to a kind id of `kinds.yaml` or a special type id (§2 anti-goal). Literals inside `catalog.go` are allowed.

Phase 2's `tests/inventory/test_stock.py` cases are rewritten here, one Go test per Python test (`TestFifoTakesTheOldestLotFirst`, `TestANamedLotIsTakenFirstThenFifo`, `TestAForeignReceiveKeepsWhatWasEntered`, `TestACountSettlesANegativeLotFirst`, `TestBadInputIsRefusedNotA500`, `TestAShortfallGoesNegativeOnTheNewestLot`, `TestAnItemWithNoLotOpensACostUnknownLot`, `TestGiveBackReversesEachUseAtItsOwnCost`, `TestSetLotCostMovesNoPastMove`, `TestRecostResnapshotsAndNamesTheOrderItems`, `TestAWholeNumberUomRefusesAFraction`, `TestUnitStatusWritesItsMove`, `TestEstimateMatchesTake` (`566.666667` for 15 of 10 @ 500 and 10 @ 700), …). Layer 6 deletes `test_stock.py` with the Python writes.

- [ ] **Step 1: Write the failing tests:** the ported cases, and:
  - `TestAServiceCannotBeReceived`: an item of `labour` (hourly) → `Receive` is 409 `step_not_applicable` `{step: receive}`; no row.
  - `TestADurableConsumeIsNotBuilt`: `Consume` on a `nozzle` item → 409 `handler_not_built` `{handler: consume.use}` (K2 builds it).
  - `TestAnIncompleteStepIsRefused`: a kind whose `count` handler is `count.remaining` with no `capacity` → `Count` is 409 `step_incomplete` `{step: count, missing: [capacity]}`; its `Receive` works.
  - `TestAPlaceChangeWritesNoMove`: `Move` of a lot to a location → `location_id` set, no `stock_moves` row.
  - `TestRecostRefusesAnUnlockedOrder`: moves of orders `o1` and `o2`, `Recost(lot, ["o1"])` → 409 `recost_orders_moved`, no move changed.
  - `TestRepointMovesStockRowsOnce`: a move naming `printer:a` and a unit with `printer_id = a` → both name `b`; a second `Repoint` changes nothing.
  - `TestTheBaseFlowNamesNoKind` (`purity_test.go`).
- [ ] **Step 2: Run** `cd tools/go/core && go test -race ./internal/inventory/...` — Expected: FAIL.
- [ ] **Step 3: Implement.** Register the K1 implementations in `handlers.Default() *Registry`.
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): the K1 default handlers and the inventory base flow (#6949, K1 4/7)`

### Task 12: The Temporal worker, the activities, the operation rows, and the read routes

**Files:**
- Create: `tools/go/core/internal/inventory/activities/activities.go`, `activities/register.go`, `activities/record.go` (the interceptor), `tools/go/core/internal/inventory/store/operations.go`, `tools/go/core/internal/temporalx/temporalx.go` (client and worker options from the environment), `tools/go/core/internal/inventory/areas.go`, `tools/go/core/internal/inventory/routes/reads.go`, `routes/problems.go`, `tools/go/core/internal/inventory/activities/testdata/answers/*.json`
- Modify: `tools/go/core/internal/publish/publish.go` (an exported `Notify` for a bus event), `tools/go/core/main.go` (the Temporal client, the worker, mount by caller), `tools/go/core/go.mod`, `vendor/` (`go work vendor`), `tools/go/core/main_test.go`, `tools/go/core/README.md` (variables, the area table, the activities, "Where it differs")
- Test: `tools/go/core/internal/inventory/activities/activities_test.go`, `activities/register_test.go`, `activities/record_test.go`, `tools/go/core/internal/inventory/store/operations_test.go`, `tools/go/core/internal/publish/notify_test.go`, `tools/go/core/internal/inventory/routes/reads_test.go`, `tools/go/core/main_test.go`

**Interfaces:**
- Dependency: `go.temporal.io/sdk` v1.49.0 or newer (§6.1a; Standalone Activities on the client side arrived by v1.49.0, `internal/internal_activity_client.go`), and the `go.temporal.io/api` it requires (v1.63.5 for v1.49.0). Vendored by `go work vendor` (`tools/go/CLAUDE.md`).
- Environment (`temporalx`): `SCADBUDDY_TEMPORAL_ADDRESS` (required when `SCADBUDDY_CORE_AREAS` names `inventory`; the start stops without it), `SCADBUDDY_TEMPORAL_NAMESPACE` (default `scadbuddy`, as `core/config.py:39` `DEFAULT_TEMPORAL_NAMESPACE`), `SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY` (default `inventory`). No TLS: the Python client uses none (`workflows/client.py`). Payloads use the SDK's default JSON converter, which reads the Python `pydantic_data_converter`'s `json/plain` payloads.
- `package activities`: `type Activities struct{ Flow *flow.Flow; Kinds *store.Kinds; Items *store.Items }` (the clusters `Activities` pattern). Every method has the shape `func (a *Activities) X(ctx context.Context, in Input) (store.Answer, error)` with `Input{ Key string; Actor json.RawMessage; Params map[string]string; Body json.RawMessage }` (`Params` are the path parameters; `Body` is the public body). Names, registered one by one with `activity.RegisterOptions{Name: …}` in `Register(w worker.Registry, a *Activities)`:
  `inventory.KindCreate`, `inventory.KindUpdate`, `inventory.KindArchive`, `inventory.KindRestore`, `inventory.ItemCreate`, `inventory.ItemUpdate`, `inventory.ItemArchive`, `inventory.ItemRestore`, `inventory.ReceiveLot`, `inventory.ReceiveUnits`, `inventory.LotUpdate` (cost, place, note), `inventory.LotAdjust`, `inventory.Take` (the use route and an order's take), `inventory.Count`, `inventory.UnitUpdate` (name, serial, place), `inventory.UnitStatus`, `inventory.GiveBack`, `inventory.Recost`, `inventory.Repoint`.
  `Input.Key` is the activity id; each method calls `store.Write` with `Caller "inventory"`, `Key`, `Route` = the activity name. A problem is returned as an `Answer`; only an infrastructure error is returned as `error` (Global Constraints).
- Worker: `worker.New(client, queue, worker.Options{Interceptors: []interceptor.WorkerInterceptor{activities.Record(pool)}})`, started by `main.go` beside the HTTP servers and stopped with them.
- The operation rows (R-K1-22, design §6.1a "Visibility" at `f791b4581`; the agent's `ai_operations` pattern):
  - `publish.Notify(ctx, q pgx.Tx, kind string, fields map[string]any) error`: `SELECT pg_notify('scadbuddy_events', $1)` with the payload `{"id": <uuid hex, 32>, "at": <RFC 3339 UTC>, "kind": kind, …fields}` written by `ASCII`, so `core/events.py` `decode_event` reads it. In the caller's transaction, so it is sent at the commit.
  - `store.StartOperation(ctx, pool, op Operation)`: in a short transaction, upsert `inventory_operations` (`status = 'running'`, `attempt`, `actor`, `subject`, `updated_at`) and `Notify(…, "inventory_operation.changed", {"operation_id": id})`.
  - `store.FinishOperation(ctx, tx, id string, a Answer)`: in the write's own transaction (`store.Write` calls it, through `OperationFrom(ctx)`, after `do` and after a replay): `status` `succeeded`, or `failed` with `problem` when `a.Status >= 400`; `finished_at`; the same `Notify`. So the row's end commits with the stock write, or not at all.
  - `store.NoteError(ctx, pool, id string, attempt int, err error)`: an attempt that failed (an infrastructure error) keeps `status = 'running'` and sets `last_error`, `attempt`; Temporal retries it.
  - `store.Operation(ctx, q, id) (wire.Operation, bool, error)`: the row as `operations/store.py` `Operation` (`id`, `kind`, `subject`, `status`, `result` from `crud_requests` when `succeeded`, `error` = `problem` when `failed`, `created_at`, `finished_at`). A `running` row older than 60 s is checked with Temporal (`client.GetActivityHandle(…).Describe`): a closed, failed or timed-out activity marks the row `failed` with the problem `inventory_activity_failed` (its last error as `detail`) and notifies.
  - `activities.Record(pool) interceptor.WorkerInterceptor` (the clusters `progress` pattern, `interceptor.WorkerInterceptorBase`, `ActivityInboundInterceptorBase`): before each attempt `StartOperation` (id = `activity.GetInfo(ctx).ActivityID`, kind = the activity type, subject = the first entity the input names), puts the recorder in `ctx` for `store.Write`, and calls `Next`; on an `error`, `NoteError`. A failure of the record is logged and counted; it never fails the activity.
- `package inventory`: `const Caller = "inventory"`; `func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route` returns `inventory:read` (every read route). Area `inventory` is the reads plus the worker: `main.go` starts the worker only when `SCADBUDDY_CORE_AREAS` names `inventory`.
- `main.go`: `callerPackages = map[string]areaRoutes{inventory.Caller: inventory.Areas}`; `newRouter` mounts each area of a caller package with `router.Add(caller, routes...)`; an area name in two packages stops the start.
- Read routes under `/api/v1/internal/crud/inventory` (paths as the public ones without `/inventory`, answers as the public JSON, R-K1-6): `GET /operations/one?id=` (the id is a query value, since a key holds `:` and the router leaves an escaped path to `Next`, `internal/crud/crud.go` `match`), `GET /kinds` (`?archived=`), `GET /kinds/{kind_id}`, `GET /kinds/{kind_id}/schema`, `GET /items` (Task 10 filters), `GET /items/{item_id}`, `GET /items/{item_id}/lots`, `GET /lots/{lot_id}`, `GET /items/{item_id}/units` (`?status=`), `GET /units/{unit_id}`, `GET /moves` (`?item_id=&lot_id=&unit_id=&order_item_id=&limit=`), `GET /items/{item_id}/estimate?qty=`. There is no write route.
- Problems: `https://scadbuddy.dev/problems/inventory/<code>` with `code`, as `inventory/problems.py` writes them; new codes `step_not_applicable`, `step_incomplete`, `handler_not_built`, `wrong_counting`, `recost_orders_moved` (409), `steps_invalid`, `class_changed`, `class_open` (422). A `steps_invalid` answer lists `problems: [{step, handler, reason}]`.
- `testdata/answers/<name>.json`: one recorded `Answer.Body` per activity and per read family (`ItemKind`, `KindList`, `Item`, `ItemList`, `Lot`, `LotList`, `Unit`, `UnitList`, `MoveList`, `KindSchema`, `RecostResult`), written by `go test ./internal/inventory/... -update` and compared otherwise. Layer 5 validates them with the pydantic models.

Phase 2's `tests/api/test_inventory.py` and `test_inventory_stock.py` cases for kinds, items and stock are rewritten here against the activities (status, problem `code`) and the read routes (`errors[].loc` of a query); offers and the `/parts` rules stay in Python.

- [ ] **Step 1: Write the failing tests:** the ported cases, and:
  - `TestEveryActivityIsRegisteredByName` (`register_test.go`, as clusters `TestRegisterActivities_DoesNotPanic`, with `client.NewLazyClient`): `Register` does not panic, and the registered names are exactly the 19 above.
  - `TestAnActivityRunTwiceWritesOnce` (`testsuite.TestActivityEnvironment`, `testdb.New`): `inventory.Take` with one `Key` run twice → one move, two equal answers.
  - `TestARefusalIsAnAnswerNotAFailure`: `inventory.ReceiveLot` on a `labour` item → `Answer{409, step_not_applicable}`, `err == nil`.
  - `TestADatabaseOutOfReachFailsTheActivity`: a closed pool → a non-nil `error` (Temporal retries).
  - `TestTheRecordWritesStartAndEnd` (`record_test.go`, `testdb.New`, a `LISTEN scadbuddy_events` connection): one activity → a `running` row and one notice, then `succeeded` and a second notice, one `id`; a 409 answer → `failed` with `problem.code`; the end row and the stock write commit together (a forced rollback of the write leaves the row `running`).
  - `TestAFailedAttemptStaysRunning`: an attempt that returns an error → `running`, `attempt = 1`, `last_error` set; the next attempt succeeds → `succeeded`, `attempt = 2`.
  - `TestAStaleRunningRowIsClosedFromTemporal` (a fake `Describe` that answers timed out) → `failed`, `inventory_activity_failed`.
  - `TestTheNoticeIsAnEventPythonReads` (`notify_test.go`): the payload has exactly `id`, `at`, `kind`, `operation_id`; it is written to `testdata/inventory_operation_changed.json`, which Task 13 decodes with `decode_event`.
  - `TestTheInventoryAreaAnswersOnlyItsCaller` (`main_test.go`): an `agent` credential → 403 `wrong_caller`; an `inventory` credential with `crud.inventory` → 200 and `X-ScadBuddy-Crud: 1`.
  - `TestInventoryNeedsTemporal`: `SCADBUDDY_CORE_AREAS=inventory` without `SCADBUDDY_TEMPORAL_ADDRESS` → `run` returns an error naming the variable.
  - `TestTheAnswersMatchTheirGoldenFiles`.
- [ ] **Step 2: Run** `cd tools/go/core && go test -race ./...` — Expected: FAIL.
- [ ] **Step 3: Implement.** `go get go.temporal.io/sdk@<v1.49.0 or newer>` in `tools/go/core`, then `go work vendor` at the root.
- [ ] **Step 4: Run** `cd tools/go/core && go vet ./... && go test -race ./...` and `gofmt -l tools/go` — Expected: PASS, no file listed.
- [ ] **Step 5: Commit** `feat(core): inventory writes as Temporal activities on queue inventory, reads as CRUD routes (#6949, K1 4/7)`

---

## Layer 5 — `feat/6949-k1-5-inventory-forwarders` (on layer 4)

### Task 13: The clients, the forwarders, the 202, and the Activities tab

**Files:**
- Create: `backend/scadbuddy/inventory/core_client.py`, `backend/scadbuddy/inventory/activity.py`, `backend/scadbuddy/api/inventory_operations.py`, `frontend/src/pages/inventory/StillWorking.tsx`
- Modify: `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/core/events.py` (`InventoryOperationEvent`), `backend/scadbuddy/activity/component.py` (its ingest), `backend/scadbuddy/activity/index.py` (`ingest_inventory_operation`), `backend/scadbuddy/inventory/component.py`, `backend/scadbuddy/inventory/models.py`, `backend/scadbuddy/inventory/problems.py`, `backend/scadbuddy/api/inventory.py` (kinds, items), `backend/scadbuddy/api/inventory_stock.py` (lots, units, moves; `recost` moves in Task 16), `backend/scadbuddy/main.py` (the operations router), `frontend/src/pages/inventory/api.ts` (writes through `command`), the pages that call its writes (`ItemStock.tsx`, `LotPage.tsx`, `UnitPage.tsx`, `ItemPage.tsx`, `KindsPage.tsx`), `frontend/src/lib/problems.ts`, `frontend/src/mocks/features/inventory.ts`
- Delete: `backend/scadbuddy/inventory/items.py`; from `inventory/kinds.py` the `KindStore` writes (keep `merged_schema`, `item_schema`, `keys_in_use`, which `entities/` reads); `backend/tests/inventory/test_kinds.py`, `test_items.py` (ported in Task 10)
- Test: `backend/tests/api/test_inventory.py`, `backend/tests/api/test_inventory_stock.py`, `backend/tests/inventory/test_core_client.py`, `backend/tests/inventory/test_go_answers.py`, `backend/tests/activity/test_inventory_operations.py`, `frontend/src/pages/inventory/StillWorking.test.tsx`, `frontend/src/pages/inventory/ItemStock.test.tsx` (new)

**Interfaces:**
- `Settings.core_crud_url: str | None` (env `SCADBUDDY_CORE_CRUD_URL`, an `http://` URL such as `http://127.0.0.1:8082/api/v1`) for reads (R-K1-4); `Settings.temporal_task_queue_inventory: str = "inventory"`.
- `core_client.py`, `class InventoryCore`:
  - `async def read(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any` over `CrudClient(core_crud_url, "inventory", ServiceAuth(InProcessProvider("inventory", …)))`. `CrudUnavailableError` or no `core_crud_url` → 503 `inventory_unavailable`.
  - `async def write(self, name: str, *, key: str, by: Actor, subject: str, params: Mapping[str, str] | None = None, body: Any = None, wait: float = 15.0) -> Done | OperationAccepted` (R-K1-24):
    1. `handle = await client.start_activity(f"inventory.{name}", {"key", "actor", "params", "body"}, id=key, task_queue=settings.temporal_task_queue_inventory, schedule_to_close_timeout=timedelta(hours=24), start_to_close_timeout=timedelta(seconds=30), id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING, id_reuse_policy=ActivityIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY, retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30)), summary=f"{name} {subject}")`. A start while the activity runs attaches (Use Existing). `ActivityAlreadyStartedError` (it succeeded before) → `handle = client.get_activity_handle(key)`, `repeated = True`.
    2. `answer = await asyncio.wait_for(asyncio.shield(handle.result()), wait)`; `{status, body}`: a status ≥ 400 is raised as `ApiError` with the problem's status, type, detail and `code` unchanged; else `Done(body)`.
    3. The wait ends → `OperationAccepted(id=key, kind=f"inventory.{name}", subject=subject, status="running", created_at=…, repeated=…)`; the route answers it with status 202. The activity still runs.
    4. A start that cannot reach Temporal → 503 `inventory_unavailable`; nothing was started, and a retry with the same key is safe.
  - `def write_blocking(self, …) -> Done | OperationAccepted`: the same for synchronous callers (the printer merge, Task 16), through `asyncio.run_coroutine_threadsafe` on the app's loop.
  - Key: `f"{by.kind}:{by.principal or ''}:{idempotency_key}"` when the public request carried `Idempotency-Key`, else `f"req:{uuid4()}"`.
- Each public write route takes an optional `Idempotency-Key` header (1–200 characters) and declares a `202` answer of `OperationAccepted` beside its own.
- `api/inventory_operations.py`: `GET /api/v1/inventory/operations/{operation_id}` → `Operation` (`operations/store.py:34`), read from Go's `GET /operations/one?id=` (Task 12); 404 `operation_not_found`.
- `api/inventory.py`, `api/inventory_stock.py`: each kind, item, lot, unit and move route parses, calls `InventoryCore.read` or `write` with the activity of Task 12, and answers the model, or 202. Offers keep their current code (K3).
- `models.py`: `ItemKind`, `KindInput`, `KindPatch`, `Item`, `ItemInput`, `InventoryItemPatch`, `Unit` gain Task 10's fields (`Class = Literal`s, `Steps = dict[str, StepConf]`, `incomplete_steps: list[str]`); the phase 2 fields stay (R-K1-6).
- The Activities tab (R-K1-22, the agent's pattern):
  - `core/events.py`: `class InventoryOperationEvent(BaseEvent)`, `kind: Literal["inventory_operation.changed"] = "inventory_operation.changed"`, `operation_id: str`, added to the `Event` union. It goes to no WebSocket topic, as `AgentOperationEvent`.
  - `ActivityIndex.ingest_inventory_operation(operation_id: str) -> None` and `ingest_inventory_operation_in(conn, operation_id)`: read the `inventory_operations` row and `upsert_in` an `ActivityRow` (`id = f"inventory_operation:{id}"`, `kind = row.kind` (the activity name, `inventory.Take`), `subject`, `status`, `title` from the kind's label and the subject, `source = "inventory_operations"`, `source_id = id`, `created_at`, `finished_at`, `detail = {"problem", "attempt", "last_error"}`).
  - `activity/component.py` `_run`: `on_event` also takes `InventoryOperationEvent` and schedules that ingest, as it does `AgentOperationEvent`.
  - `inventory/activity.py`: `ACTIVITY_KINDS`, one `ActivityKind` per activity of Task 12 (`inventory.KindCreate` … `inventory.Repoint`, the 19 names of Task 12, labels such as "Receive a lot", "Take stock", "Re-cost a lot"), each with a link to the subject's page.
- Frontend (owner, 2026-11-25: "still working" on a 202):
  - `pages/inventory/api.ts`: each write calls `command<T>(path, init, '/inventory/operations', key, onAccepted)` (`api/client.ts:913`), not `request`, so it sends an `Idempotency-Key`, takes a 202 and follows `GET /inventory/operations/{id}` to the result or the problem. Each write takes an optional `onAccepted`.
  - `StillWorking.tsx`: `<StillWorking operation={OperationAccepted | null} />`, a status line "Still working. This can take a moment; you can leave this page." with a link to the operation on the Activities tab (`/activity?q=<id>`, as the Activities page filters). `ItemStock`, `LotPage`, `UnitPage`, `ItemPage` and `KindsPage` keep the accepted operation in state from `onAccepted`, show `StillWorking` while the command runs, and clear it when it settles. A second press while it runs reuses the first press's key (`command`'s `key` argument), so it attaches.
- Problem codes (R-K1-23, owner 2026-11-25: the new codes now, with no mapping): `inventory/problems.py` drops `item_not_counted` and `wrong_tracking` and passes Go's codes through. Every reader of an old code changes in this task: `api/inventory_stock.py:8,46` (docstrings and `codes(...)`), `agent/src/tools/inventory.ts:424-425,492` (descriptions: `step_not_applicable`, `wrong_counting`), `frontend/src/mocks/features/inventory.ts:462-464` (`Refused(409, 'step_not_applicable', …)`, `'wrong_counting'`), `frontend/src/lib/problems.ts` (a message for each new code), the backend tests that assert them (rewritten in Go in Task 12; `test_inventory_stock.py:132-137,172` become forwarder pass-through tests), `docs/features/inventory.md:43` (Task 17).
- The agent's inventory tools call the public routes, which may answer 202; `agent/src/tools/inventory.ts` treats a 202 as "still running, operation <id>" in its answer text and does not repeat the call.

- [ ] **Step 1: Write the failing tests** (reads through an `httpx.MockTransport` that stands in for Go; writes through a fake Temporal client that records `start_activity`'s name, input and options, and whose handle answers what the test gives):
  - one round trip per family (kinds, items, lots, units, moves): the read path, and the write's activity name, `id`, `task_queue == "inventory"`, `id_conflict_policy == USE_EXISTING`, `id_reuse_policy == ALLOW_DUPLICATE_FAILED_ONLY`, and input `actor`;
  - `test_a_go_problem_is_passed_through`: the activity answers `{status: 409, body: {code: step_not_applicable, …}}` → the public answer is 409 with that type and `code`;
  - `test_a_slow_write_answers_202_and_a_repeat_202_again`: the handle does not answer within the wait (a test wait of 0.1 s) → 202 `OperationAccepted{id: key, status: running}`; the same `Idempotency-Key` again → one more start with the same id, 202 again, no error;
  - `test_a_done_write_answers_the_stored_result`: `start_activity` raises `ActivityAlreadyStartedError` → the route answers the handle's result with its own status;
  - `test_a_failed_activity_runs_again`: the fake records that a start after a failed run is accepted (Allow Duplicate Failed Only) and the route answers the new run's result;
  - `test_temporal_out_of_reach_is_a_503` (`inventory_unavailable`); `test_go_out_of_reach_is_a_503` (a read; the transport raises `httpx.ConnectError`); `test_no_core_url_is_a_503`;
  - `test_a_persons_key_is_scoped_to_the_actor`: two actors with one `Idempotency-Key` → two ids;
  - `test_the_operation_route_reads_go`: `GET /api/v1/inventory/operations/browser%3A%3Ak` → Go's `GET /operations/one?id=browser::k`, answered as `Operation`;
  - `test_go_answers_fit_the_public_models` (`test_go_answers.py`): each file in `tools/go/core/internal/inventory/activities/testdata/answers/` validates with its pydantic model, and `model_dump(mode="json")` equals the file; `tools/go/core/internal/publish/testdata/inventory_operation_changed.json` decodes with `decode_event` to an `InventoryOperationEvent`.
  - `test_inventory_operations.py`: an `inventory_operations` row, then the event on the bus → one `activity` row `inventory_operation:<id>` with the row's kind, subject and status; the row moved to `succeeded` and the event again → the same activity row, `succeeded`, `finished_at` set; every kind of `inventory/activity.py` is in the registry.
  - `StillWorking.test.tsx`: `shows still working with a link to the activity` (an `OperationAccepted` → the text and `href` `/activity?q=op-1`); `ItemStock.test.tsx`: `a slow receive shows still working and then the lot` (the mock answers 202, then the operation `running`, then `succeeded` with the lot → "Still working" shows, then goes, and the lot is listed); `a second press reuses the key` (two presses → two requests with one `Idempotency-Key`).
- [ ] Add `tools/go/core/internal/inventory/activities/testdata/` and `tools/go/core/internal/publish/testdata/` to the `backend` lane's pattern in `.github/scripts/select-lanes.sh`, and a case to `select-lanes.test.sh` (root CLAUDE.md "A new cross-tree read").
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_inventory.py tests/api/test_inventory_stock.py tests/inventory tests/activity -q` and `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/inventory` — Expected: FAIL.
- [ ] **Step 3: Implement**; delete the Python kind and item stores and their tests. Load the `frontend-design` skill before `StillWorking.tsx`.
- [ ] **Step 4: Run** the tests, `uv run --frozen pytest tests/auth -q`, `bash .github/scripts/select-lanes.test.sh`, `cd frontend && pnpm typecheck && pnpm lint && pnpm exec vitest run src/pages/inventory src/mocks/features/inventory.test.ts`, `cd agent && pnpm gen:api && pnpm typecheck && pnpm exec vitest run test/inventory.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): public inventory reads go to Go's routes and writes to its activities, 202 when slow (#6949, K1 5/7)`

### Task 14: The `/parts` aliases go

**Files:**
- Delete: `backend/scadbuddy/inventory/part_alias.py`; from `backend/scadbuddy/api/parts.py` the alias routes (`GET`/`POST /parts`, `PATCH /parts/{part_id}`, `POST …/stock`, `POST …/adjust`, `GET …/moves`) and their models; from `backend/tests/api/test_parts.py` their tests
- Modify: `backend/scadbuddy/inventory/component.py` (no `parts`), `agent/src/tools/orders.ts` (`part_list`, `part_stock_add`), `agent/src/sessions/touched.ts:334`, `agent/src/tools/coverage.ts:136-151`, `agent/test/orders.test.ts`
- Keep: `GET`/`PUT /parts/rules/{slug}`, `GET /orders/{order_id}/parts`, `PUT /orders/{order_id}/items/{item_id}/parts` and the tool `order_item_parts_set` (they are order and rule routes, not aliases)
- Test: `backend/tests/api/test_parts.py`, `agent/test/orders.test.ts`, `agent/test/coverage.test.ts`

**Interfaces (owner 2026-11-25, R-K1-17):**
- The tool `part_stock_add` is deleted; `inventory_lot_receive` does its work.
- The tool `part_list` keeps only its `order_id` form: `order_id` becomes required, its route list is `['GET /api/v1/orders/{order_id}/parts']`, and its description drops "Alias of inventory_item_list". The list of every part is `inventory_item_list` with `kind=part` and `descendants=true`.
- `coverage.ts`: the entries for the deleted routes go; the `/parts/rules/{slug}` entries stay.

- [ ] **Step 1: Write the failing tests:** `test_the_part_aliases_are_gone` (`GET /api/v1/parts` and `POST /api/v1/parts/x/stock` → 404); `test_rules_and_order_parts_still_work` (the kept cases of `test_parts.py` pass); agent: `part_stock_add` is not a tool; `part_list` without `order_id` is refused by its schema; `coverage.test.ts` lists no `/api/v1/parts` alias route.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_parts.py -q` and `cd agent && pnpm exec vitest run test/orders.test.ts test/coverage.test.ts` — Expected: FAIL.
- [ ] **Step 3: Delete and change** as listed. `git grep -n -E "part_alias|part_stock_add|/api/v1/parts['\"\`]|/parts/\{part_id\}" backend agent frontend plugins docs/features` finds nothing after it except the kept rule routes.
- [ ] **Step 4: Run** the tests, `cd agent && pnpm lint && pnpm typecheck`, `uv run --frozen pytest tests/auth -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory)!: remove the /parts alias routes and part_stock_add (#6949, K1 5/7)`

### Task 15: The real-stack run and the deployment change

**Files:**
- Modify: `e2e-stack/compose.yaml` (a `core` service built from `tools/go/core/Dockerfile`, `SCADBUDDY_CORE_AREAS=inventory,inventory:read`, `SCADBUDDY_CORE_CRUD_LISTEN=:8082`, `SCADBUDDY_TEMPORAL_ADDRESS=temporal:7233`, `SCADBUDDY_TEMPORAL_NAMESPACE=default`, `SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY=inventory`; the `app` service gets `SCADBUDDY_CORE_CRUD_URL=http://core:8082/api/v1`), `e2e-stack/CLAUDE.md`
- Test: one spec in the real-stack e2e (`e2e-stack/`): make a kind, an item, receive a lot, use 1; the item reads `on_hand` one less; the Activities tab lists `inventory.ReceiveLot` and `inventory.Take` as succeeded (through the `inventory_operation.changed` event).

- [ ] **Pre-flight:** check that the `temporal` image in `e2e-stack/compose.yaml` runs server 1.32.0 or newer and that Standalone Activities need no dynamic-config switch there (https://docs.temporal.io/standalone-activity); if one is needed, set it in the compose file and in the Step 6 note.
- [ ] **Step 1: Write the failing spec** `inventory goes through Go` (the e2e-stack's own runner, `e2e-stack/CLAUDE.md`).
- [ ] **Step 2: Run** it — Expected: FAIL (503 `inventory_unavailable`).
- [ ] **Step 3: Add the service and the settings.**
- [ ] **Step 4: Run** it — Expected: PASS.
- [ ] **Step 5: Commit** `test(e2e-stack): run scadbuddy-core and its inventory worker (#6949, K1 5/7)`
- [ ] **Step 6 (outside this repo, before the deploy of layers 3–6):** an eh-homelab/clusters PR. In the API pod: on the `scadbuddy-core` container, `SCADBUDDY_CORE_CRUD_LISTEN=:8082`, `SCADBUDDY_CORE_AREAS` with `inventory,inventory:read` (#7702), `SCADBUDDY_TEMPORAL_ADDRESS` and `SCADBUDDY_TEMPORAL_NAMESPACE` as the API container has them (no mTLS: the Python client uses none), `SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY=inventory` (no service token: the container calls no CRUD route, R-K1-22); on the API container, `SCADBUDDY_CORE_CRUD_URL=http://127.0.0.1:8082/api/v1`. A NetworkPolicy egress rule from the API pod to the Temporal frontend (7233) if the pod's policy does not already allow it (the API container already reaches it; check whether the rule is per pod or per container, which a NetworkPolicy is not). After the deploy, read `inventory_k1_report` on prod (`deploy/CLAUDE.md` "Prod checks") and post the counts on the K1 issue.

---

## Layer 6 — `feat/6949-k1-6-order-stock-activities` (on layer 5)

### Task 16: Orders and the printer merge start activities; Python's stock writes go

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`_lock_parts_of`, `_use_parts`, `_return_parts`, `parts_unit_cost`, `recost_lot`), `backend/scadbuddy/api/inventory_stock.py` (`recost`), `backend/scadbuddy/printers/store.py` (`_merge` and its two callers, l.667 and l.949), `backend/scadbuddy/entities/registry.py` (`repoint_entity` loses its `UPDATE stock_moves`)
- Delete: from `backend/scadbuddy/inventory/stock.py` every write (`StockStore` and its helpers); keep `parts_moves`, `parts_moved`, `PartsMoved` (readers, R-K1-16); `backend/tests/inventory/test_stock.py` (ported in Task 11)
- Test: `backend/tests/orders/test_stock_activities.py`, `backend/tests/orders/test_parts_lifecycle.py`, `backend/tests/printers/test_merge_stock.py` (new, beside `test_merge_filament.py`), `backend/tests/test_no_python_stock_writes.py`

**Interfaces:**
- Keys (§6.1a): `order_item:<order_item_id>:parts:<n>:<item_id>` for `inventory.Take` and `inventory.GiveBack`, where `n` is the item's fulfilment cycle: 1 + the count of its earlier `parts.returned` events, read under the order's lock (so a retry of one fulfil reads the same `n`, and the take after an unfulfil gets a new one). `lot:<lot_id>:recost:<request key>` for `inventory.Recost`. `entity_merge:printer:<gone>:<kept>` for `inventory.Repoint`.
- `lifecycle.py`, fulfil (R-K1-14):
  1. In the order's transaction: the transition as today; `order_item_parts.used` set; before the commit, `GET …/items/{item_id}/estimate?qty=` per part refuses `bad_quantity` as phase 2 did (a read takes no lock). Commit.
  2. Then `InventoryCore.write("Take", key=…, body={qty, order_id, order_item_id})` per part item, in `item_id` order, awaited inline. The answers' moves give the `parts.used` event, written in a short transaction under the order's lock (phase 2's detail).
  3. A start that fails (Temporal out of reach), or a `write` that answers `OperationAccepted` (the 15 s wait ended) → `LifecycleError("stock_pending", …)` (503, safe to retry with the same key): the order stays fulfilled, `parts.used` is not written, and the person retries the fulfil. A fulfil of an already-fulfilled item whose `parts.used` event is missing runs step 2 again with the same `n`, so the same keys: a running activity is attached to (Use Existing), a succeeded one answers its stored result (`ActivityAlreadyStartedError`, then the handle), and a failed one runs again (Allow Duplicate Failed Only), whose Go write finds the key in `crud_requests` if the first attempt committed. No path takes the stock twice (owner 2026-11-25). A refusal answer → `parts.stock_refused` event with the problem.
  Unfulfil does the same with `inventory.GiveBack` and `parts.returned`. `_lock_parts_of` goes: each activity is its own transaction.
- `parts_unit_cost` reads the estimate route. `recost_lot` locks the orders (its transaction), executes `inventory.Recost` with `body={orders: [ids]}` inside it (Go waits on no row Python holds: it locks the lot and its moves, never an order), writes the events, commits; `recost_orders_moved` → the whole recost again (phase 2 `_RECOST_TRIES`).
- Printer merge (owner 2026-11-25, R-K1-15): `_merge(conn, gone, kept)` first locks the `gone` printer row `FOR UPDATE`, then reads whether a stock row names it (`stock_units.printer_id = gone`, or a `stock_moves.entity_id` of the entity `printer:gone`). If one does, it raises `NeedsRepoint(gone, kept)`; the caller's transaction rolls back; the store calls `InventoryCore.write_blocking("Repoint", key=f"entity_merge:printer:{gone}:{kept}", body={kind: "printer", gone, kept})` with no transaction open, and runs the whole method again (at most 3 times, then `MergeBusy`, 409). A `write_blocking` that answers `OperationAccepted` stops the merge with 503 `stock_pending`; a retry uses the same key. With no stock row, the merge goes on as today without the stock references. A stock write that names `gone` after the check waits on the row lock and then fails on its foreign key when the printer is gone (`stock_moves.entity_id`), or would set `printer_id` NULL (`ON DELETE SET NULL`): the lock makes Go's insert wait, and its retry after the commit finds the printer gone and is refused. `PRINTER_REFERENCES` loses `stock_units`; `repoint_entity` loses its `UPDATE stock_moves`.
- `test_no_python_stock_writes.py`: `git grep`-style scan of `backend/scadbuddy/**/*.py` (not `migrations/`) for `INSERT INTO|UPDATE|DELETE FROM` followed by `items`, `item_kinds`, `stock_lots`, `stock_units`, `stock_moves`; the only allowed hits are `inventory/offers.py` (`supplier_offers`, `offer_price_points` are not stock tables, K3) — so the test fails on any stock-table write.

- [ ] **Step 1: Write the failing tests** (the fake Temporal client of Task 13):
  - `test_fulfil_takes_stock_through_an_activity`: fulfil an item with 2 of a part → one `inventory.Take`, id `order_item:oi1:parts:1:<part>`, qty `"2"`; one `parts.used` event with the answer's amount.
  - `test_unfulfil_then_fulfil_uses_new_keys`: `…:parts:1:…` (Take), `…:parts:1:…` (GiveBack), `…:parts:2:…` (Take).
  - `test_a_failed_start_fails_the_fulfil`: `start_activity` raises `RPCError` → 503 `stock_pending`; the item is fulfilled; no `parts.used`. The same with a handle that does not answer within the wait.
  - `test_a_fulfil_retry_takes_stock_once`: after each of those, the retry starts the same id (`…:parts:1:…`) once more: with the first activity still running the fake attaches, with it done the fake raises `ActivityAlreadyStartedError` and the handle answers; in each case `parts.used` is written once and the fake counts one Take run.
  - `test_a_refused_take_is_recorded`: the answer is 422 `bad_quantity` → `parts.stock_refused`.
  - `test_bad_quantity_refuses_before_the_commit`: the estimate route says overflow → `LifecycleError("bad_quantity")`, the order not changed, no activity.
  - `test_recost_retries_when_another_order_moved`: the first answer is 409 `recost_orders_moved`, the second a result → two activities with two keys, events once.
  - `test_a_merge_repoints_stock_first`: a unit with `printer_id = gone` → one `inventory.Repoint` with id `entity_merge:printer:gone:kept`, then the merge commits; with no stock row, no activity.
  - `test_no_python_stock_writes`.
  - The phase 2 cases of `test_parts_lifecycle.py` still pass with the fake.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/orders/test_stock_activities.py tests/orders/test_parts_lifecycle.py tests/printers tests/test_no_python_stock_writes.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement**; delete the Python stock writes and `test_stock.py`.
- [ ] **Step 4: Run** the tests, then `uv run --frozen pytest tests/orders tests/printers tests/entities -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): order parts, re-costs and printer merges write stock through Go's activities (#6949, K1 6/7)`

---

## Layer 7 — `feat/6949-k1-7-inventory-docs` (on layer 6)

### Task 17: Docs

**Files:**
- Modify: `docs/features/inventory.md` (the class, the new problem codes in place of `item_not_counted` l.43, the `/parts` aliases gone l.194, the Activities tab rows), `docs/architecture/` (the page on processes: `scadbuddy-core` runs a Temporal worker on queue `inventory`; the page on data: Go writes the inventory tables; the page on Temporal workflows: the standalone activities), `backend/CLAUDE.md` (the `inventory/` layout), `tools/go/CLAUDE.md` (`core/` holds `internal/inventory` and `internal/temporalx`; the Temporal SDK is vendored), `tools/go/core/README.md` (if Task 12 left a gap), `docs/inventory-model/README.md` (one line: the Go test is `internal/inventory/modeldata`), the design's status line ("K1 built")

- [ ] **Step 1:** Write each change; every fact names its file or its PR (#259).
- [ ] **Step 2: Run** `lychee --offline docs/features/inventory.md docs/inventory-model/README.md` if it is installed, else check each relative link by hand — Expected: no broken link.
- [ ] **Step 3: Commit** `docs: inventory K1, the base model in Go (#6949, K1 7/7)`. The PR body says `Fixes` the K1 issue.

---

## Seams K1 leaves for K2–K6

| Seam | K1 leaves | Phase |
|---|---|---|
| `unit_uses`, `consume.use`, manual readings | `Consumer` for durables is `handler_not_built`; `stock_units.life_*` columns stay unwritten | K2 |
| Life | `steps.life` is resolved and validated; `Lifer` has no implementation; `life_*` read fields are derived from `steps` | K2 |
| Handler cache and refresh | No `handler_values`; `handler_config_history` is written from K1 | K2 |
| Relationships | `relation_types` (five, seeded) and `relations` exist; only `compatible_with` rows; `installed_in_unit_id` stays a column; no `RelationHandler` | K2 |
| Term services, schedule, signals | `stock_units.starts_at`, `ends_at`, status `ended` exist; no activity writes them. The daily schedule can call the same activity functions as workflow steps (§6.1a) | K2 |
| The YAML test | Relations and history replay of `check.py` | K2 |
| Purchasing | Offers and price points stay Python SQL (not stock tables) | K3 |
| Filament | `filament_spool` resolves; filament items are refused (`filament_item_refused`); `filament.*` are `handler_not_built`; `inventory.ReceiveUnits` is the activity `filament.receive` will extend | K3, K4 |
| Printer | `printer` kind has `special_type` NULL; `printer.*` are `handler_not_built`; `printer_run_hours` untouched; `PrintRun` will call `inventory.Take`-style activities as workflow steps | K6 |
| Services on orders | No `order_items.service_item_id`; `consume.service` and `bill.*` not built | later phase |
| Drops | `item_kinds.tracking`, `life_basis`; `items.life_*`, `compatible_with`; `stock_units.life_*`; `inventory_k1_report` | K2, after one release |

## Owner answers (2026-11-25, through the coordinator)

1. Crossing the Python-Go line is a Temporal Standalone Activity (§6.1a), never an outbox, a
   sweep, or a cross-process SQL transaction. This replaced the first draft's
   `order_stock_calls` outbox.
2. Merges do not keep their SQL: they execute `inventory.Repoint` first (R-K1-15).
3. The `/parts` aliases are deleted in K1, with every caller moved (Task 14). After K1 no
   Python code writes a stock table (Task 16's test).
4. The new problem codes are used now, with no mapping (R-K1-23).
5. Round 3: no caller `core` and no `/internal/crud/core/activities` route. Go writes
   `inventory_operations` and publishes `inventory_operation.changed`, as the agent does
   (R-K1-22).
6. Round 3: a slow write answers 202 after 15 s, never 503 `inventory_pending`; the reuse
   policy is `ALLOW_DUPLICATE_FAILED_ONLY`; a repeat with the same key is never an error;
   order fulfil never takes stock twice (R-K1-24).

## Rulings (R-K1-n, made in this plan; the owner may change them)

- **R-K1-1. Port as is.** Each K1 default handler ports the phase 2 code path it replaces,
  statement for statement, with its locks (#7667 §1.3: no new behaviour). Where
  `handlers.yaml` says more, the phase that needs it adds it. A unit's status change keeps
  phase 2's moves; `move.place` writes no move (now in `handlers.yaml`).
- **R-K1-2. One idempotency ledger.** Go writes `crud_requests` under the caller
  `inventory`, with `crud/idempotency.py`'s lock class, and `prune` keeps those rows 7 days
  (design §6.1 at `c51dba825`). The activity id and the ledger key are the same string.
- **R-K1-3. A caller `inventory`** for the read routes (`/internal/crud/inventory/`), minted
  in process by the API (`InProcessProvider`), scope `crud.inventory`. A later caller (the
  print worker in K6) gets the area mounted under its own prefix.
- **R-K1-4. The read address** is `SCADBUDDY_CORE_CRUD_URL`; unset, a read answers 503
  `inventory_unavailable`. There is no Python fallback: one writer per table.
- **R-K1-5. The actor** travels in the activity input (`actor`), so `body` is the public
  body. Go sets `scadbuddy.actor` in every write transaction.
- **R-K1-6. The public contract grows; nothing is removed.** Kinds and items gain the class
  keys, `special_type`, `steps` and `incomplete_steps`. `tracking`, `life_basis`,
  `life_hours`, `life_months` and `compatible_with` stay, read from the new data and still
  taken as input for one release (`tracking` by the §8 rules, `life_*` into `steps.life`).
  Go keeps writing `item_kinds.tracking` and `life_basis` while SQL readers remain.
  Derivation: `tracking` = `filament` when the special type is `filament_spool`, `none` for
  a service, else `counting` (`lot` when open); `life_basis` = `hours` for `life.hours`,
  `months` for `life.months`, else `none`.
- **R-K1-7. A full catalog, a partial registry.** Every handler of `handlers.yaml` is in the
  Go catalog; K1 implements the defaults it needs. Calling an unbuilt handler is 409
  `handler_not_built`. `printer`'s special type stays NULL until K6 builds its handlers, so
  printer items keep phase 2's behaviour; filament items stay refused until K3.
- **R-K1-8. Incomplete is not invalid.** A save refuses every resolution problem except a
  missing required parameter. That one marks the kind or item incomplete
  (`incomplete_steps`), as §8 needs for migrated kinds, and a call of that step is 409
  `step_incomplete`. So a broken combination still never reaches stock (§4.2).
- **R-K1-9. The migration's rules beyond §8.** A person kind whose rule conflicts with its
  new parent's class, and a person root kind, move under the root of their class. A
  `life_basis` on a non-durable kind is dropped. Each placement is counted in
  `inventory_k1_report`.
- **R-K1-10. Lots of a kind that became durable** become whole units with an `adjust` move;
  a fraction stays on the lot and is reported.
- **R-K1-11. Model codes.** Kind id `printer_<slug>` (lower case, runs of other characters
  as `_`, at most 32), name the code. `item_kind` becomes an entity kind so a relation can
  name a kind. An item write with a new code makes its model kind, by the same rule.
  `?compatible=` reads the relations.
- **R-K1-12. The history trigger** follows §4.8 at `c51dba825`: the actor from
  `scadbuddy.actor` first, `updated_by` only when it is unset; cascades recorded by a
  `BEFORE DELETE` trigger on `entities`. Existing rows get one back-filled `set` each, so
  the history replays to the current properties from the first day.
- **R-K1-13. Parameter history starts in K1** (§4.7 at `c51dba825`).
- **R-K1-14 (replaces the first draft's outbox). Orders.** Fulfil commits the order change,
  then executes `inventory.Take` per part inline with the key
  `order_item:<id>:parts:<n>:<item>`. A failed start, or no answer in 15 s, fails the call (503 `stock_pending`, safe to retry
  with the same key); the order stays fulfilled without `parts.used`, and a retry of the
  fulfil starts the same ids, which attach to, read or (after a failure) re-run the first
  activities; `crud_requests` stops a second take. Unfulfil does the same with
  `inventory.GiveBack`. A re-cost executes `inventory.Recost` inside its order-locked
  transaction; Go checks the locked orders under the lot's lock (`recost_orders_moved`).
- **R-K1-15 (replaces "merges keep their SQL"). Printer merge.** The merge locks the gone
  printer, checks for stock rows naming it, and if any, rolls back, executes
  `inventory.Repoint` with no transaction open, and runs again. Go's later insert naming the
  gone printer waits on the lock and then fails on the foreign key (or would be nulled by
  `ON DELETE SET NULL` on `stock_units.printer_id`, which the check under the lock covers).
  `entities/registry.py` `repoint_entity` loses its stock `UPDATE`: the printer merge is
  its only caller (`git grep repoint_entity`).
- **R-K1-16. Readers keep their reads.** `orders/parts.py:421`, `orders/store.py:2661`
  (and so `orders/costs.py`), `entities/sweep.py:31`, `entities/registry.py:203` read stock
  tables; reads are not writes (§6.1). They move with #7667 phase 5, or in K2 where they need
  a cached value.
- **R-K1-17. Offers stay Python SQL until K3; the `/parts` aliases are deleted in K1**
  (owner). `part_list` keeps its order form; `part_stock_add` goes.
- **R-K1-18. Layers 3 to 6 deploy together.** Between their merges, Python's phase 2
  writes still work on the new schema (the class columns are nullable), but the prod pin
  moves only when layer 6 is on main and the clusters change of Task 15 Step 6 is ready.
- **R-K1-19. The YAML test in K1** covers the catalog, the kinds, the items, the instances
  and the README table, and the property history through the real trigger. `check.py`'s
  relation checks and its replay of lives and cached values come in K2.
- **R-K1-20. A YAML package for tests only:** `go.yaml.in/yaml/v3`, imported by
  `modeldata` alone.
- **R-K1-21. Python tests fake Go and Temporal.** The forwarder, lifecycle and merge tests
  use `httpx.MockTransport` for reads and a fake Temporal client for writes; the Go side is
  tested with `testsuite.TestActivityEnvironment` and `testdb`; the real path runs in
  `e2e-stack` (Task 15). The recorded Go answers are validated by the pydantic models.
- **R-K1-22. The activity record follows the agent's pattern** (owner 2026-11-25; the
  first draft's caller `core` and its CRUD route are withdrawn). Go's interceptor writes an
  `inventory_operations` row in Go's own transaction (id = activity id, kind, subject,
  status, start, end, problem; Task 12), then sends `inventory_operation.changed` on
  `scadbuddy_events` with `internal/publish` in `core/events.py`'s format. Python's
  `InventoryOperationEvent` parses it, `ActivityIndex.ingest_inventory_operation` writes the
  `activity` row, and `inventory/activity.py` registers the kinds (Task 13), as
  `ai_operations` and `AgentOperationEvent` do. No new caller, no new database client: the
  table is in the database Go's write already uses.
- **R-K1-23. The new problem codes.** `item_not_counted` becomes `step_not_applicable`;
  `wrong_tracking` becomes `wrong_counting` (the class has no `tracking`); a consume of a
  durable is `handler_not_built`. `unit_installed` and `filament_item_refused` stay.
- **R-K1-24. Slow writes and repeats** (owner 2026-11-25). A public write waits up to 15 s
  for the activity, then answers 202 with `OperationAccepted` (id = activity id); the
  frontend shows `StillWorking` and follows `GET /api/v1/inventory/operations/{id}` (Task 13).
  The activity has `schedule_to_close_timeout` 24 h and `start_to_close_timeout` 30 s. The
  policies are `USE_EXISTING` and `ALLOW_DUPLICATE_FAILED_ONLY`: a repeat while it runs
  attaches (202 again), a repeat after a success answers the stored result, a repeat after a
  failure or a timeout runs again, and Go's `crud_requests` check stops a second commit. A
  repeat with the same key is never an error. Order fulfil and the printer merge (Task 16)
  answer 503 `stock_pending`, safe to retry with the same key, when the start fails or the
  wait ends. A running row older than 60 s is checked with Temporal's Describe (Task 12).
- **R-K1-25. Design wording for the review findings** (the coordinator puts it in the
  design, or it stays here):
  - Finding 2 (§6.1a "Merges"): "The foreign key protects only `stock_moves.entity_id`.
    `stock_units.printer_id` is `ON DELETE SET NULL`, so a delete would not fail; it would
    clear the reference. So the merge locks the gone printer's row `FOR UPDATE`, then checks
    whether a stock row names it. If one does, it executes `inventory.Repoint` first."
  - Finding 3 (§6.1a "Merges"): "The printer merge runs inside its callers' transactions.
    So the merge rolls back, executes `inventory.Repoint` with no transaction open, and
    runs again, at most 3 times."
  - Finding 5 (§6.1a "Versions"): "The e2e stack's dev server (`temporal server
    start-dev`) must run Temporal server 1.32.0 or newer, with Standalone Activities on."

## Self-review

- **Spec coverage.** §3 class: Task 5, Task 7. §4.1–4.3 steps, resolution, parameters:
  Task 5, Task 11. §4.4 special types: Task 5 (catalog), R-K1-7. §4.7 parameter history:
  Tasks 7, 10. §4.8: Tasks 1–3, 6. §6.1: Tasks 8, 9, 13, 16. §6.1a: Task 12 (worker,
  activities, interceptor), Task 13 (starts and policies), Task 16 (orders, merge), Task 15
  (deployment). §6.2 layout: Tasks 5, 9–12. §6.3 data for K1: Task 7. §7 proof as a test:
  Task 6. §8: Task 7. §9 K1 needs the CRUD port: Task 15. §4.5–4.7's cache, §5's handlers,
  §6.4: K2 (seams table).
- **Type names.** `model.Class`, `model.Chain`, `model.Steps`, `handlers.Catalog`,
  `handlers.Resolution`, `handlers.Problem`, `handlers.Registry`, `store.Write`,
  `store.Answer`, `store.Actor`, `flow.Flow`, `activities.Activities`,
  `activities.Register`, `activities.Record`, `inventory.Areas`, `inventory.Caller`,
  `publish.Notify`, `store.StartOperation`, `store.FinishOperation`, `store.NoteError`,
  `store.Operation`, `InventoryOperationEvent`, `ingest_inventory_operation`,
  `OperationAccepted`, `StillWorking`,
  `InventoryCore.read`, `InventoryCore.write`, `InventoryCore.write_blocking` are used with
  one spelling in every task. The 19 activity names in Task 12 are the ones Tasks 13 and
  16 start.
- **No new caller.** Only the `inventory` caller exists; Go's activity record is a row in
  its own transaction plus an event (R-K1-22).
- **No outbox, no sweep.** No task adds a table of pending calls or a housekeeping step;
  every write that crosses the line is an activity start.
- **Proportion.** The plan gives signatures, tests and values; no task writes a body that
  its tests determine.
