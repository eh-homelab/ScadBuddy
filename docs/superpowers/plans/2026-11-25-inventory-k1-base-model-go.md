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
`/api/v1/inventory/*` keep their pydantic contract and forward to
`/api/v1/internal/crud/inventory/*` on Go's CRUD port with a service credential of a new
caller, `inventory`, which the API mints in process. The order lifecycle stops writing
stock in its own transaction: it records a stock call and settles it through Go.

**Tech Stack:** Go 1.26 (`tools/go/core`, pgx v5, `go.yaml.in/yaml/v3` for tests only),
Postgres 18 (triggers, PL/pgSQL), Python 3.12 (FastAPI, psycopg 3, httpx, the existing
`crud/client.py`), React + TypeScript (one panel), the agent's TypeScript tools.

**Spec:** `docs/superpowers/specs/2026-11-25-inventory-kinds-and-handlers-design.md` (§3,
§4.1–4.4, §4.8, §6, §8, §9 K1), its proof `docs/inventory-model/`, and
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
- Idempotency (§6.1): every Go write takes `Idempotency-Key` (1–200 characters); a repeat
  returns the stored status and body; a key with another route or body is 422
  `https://scadbuddy.dev/problems/crud/idempotency-key-reused`; a missing key is 428
  `https://scadbuddy.dev/problems/crud/idempotency-key-required`. Keys of caller
  `inventory` are kept 7 days.
- A route handler holds no SQL and no rule beyond parsing and problem mapping (§6.2,
  R-P3-54). Every SQL statement is in `internal/inventory/store/`.
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

1. **A write retried after a lost answer.** The same `Idempotency-Key` gives back the
   first status and body and writes nothing; the same key with another body is 422; two
   requests with one key at once run one write. Test: Task 9
   `TestARetryReplaysTheFirstAnswer`, `TestTwoRequestsWithOneKeyWriteOnce`.
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
5. **Go out of reach while a person saves.** A public inventory write answers 503
   `inventory_unavailable` within the client budget, never a 500; the person's retry with
   the same `Idempotency-Key` writes once. An order fulfilled while Go is out of reach
   commits; its stock call settles once on the next sweep. Tests: Task 13
   `test_go_out_of_reach_is_a_503`, Task 15 `test_a_pending_stock_call_settles_once`.

---

## Layers

| Layer | Branch | What |
|---|---|---|
| 1 | `feat/6949-k1-1-property-history` (from main) | The property history trigger, its readers, the panel and the tool (§4.8). Lands on its own |
| 2 | `feat/6949-k1-2-inventory-model` (from main) | The class, the catalog and the resolver in Go, and the YAML test |
| 3 | `feat/6949-k1-3-inventory-migration` (on 2) | The §8 migration, the `inventory` caller, the relation tables |
| 4 | `feat/6949-k1-4-inventory-core` (on 3) | The store, the default handlers, the base flow, idempotency, the internal routes |
| 5 | `feat/6949-k1-5-inventory-forwarders` (on 4) | The Python forwarders; the Python kind and item stores go |
| 6 | `feat/6949-k1-6-order-stock-calls` (on 5) | The order lifecycle settles its stock through Go |
| 7 | `feat/6949-k1-7-inventory-docs` (on 6) | Docs |

Layers 1 and 2 are independent. Layers 3, 4 and 5 deploy together (R-K1-18).

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
- The caller `inventory`, scope `crud.inventory` only (R-K1-3). The migration extends `service_credentials_caller_check` and `service_caller_suspensions_caller_check` with `'inventory'` (as `20261124T0222Z_camera_service_callers.sql` did).
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
- `func Write(ctx context.Context, pool *pgxpool.Pool, w WriteReq, do func(pgx.Tx) (status int, body any, err error)) (status int, body []byte, err error)` where `WriteReq{Caller, Key, Route string; Body []byte; Actor Actor}`. In one transaction: `set_config('scadbuddy.actor', actor, true)`; `pg_advisory_xact_lock(1396920913, hashtext(caller || E'\x1f' || key))` (`0x53435251`, `crud/idempotency.py` `LOCK_CLASS`); a stored reply → return it; the same key with another route or body digest → 422 `idempotency-key-reused`; else `do`, then insert the reply. An error from `do` records nothing.
- Digest (R-K1-2): SHA-256 hex of the body in `seal` canonical JSON. Only Go writes `inventory` rows, so the digest need not be Python's.
- `func EnsureEntity(ctx, tx pgx.Tx, kind, ref string) (uuid string, err error)`: the statement of `entities/shared/ensure_entity.sql`, the text Python's `EntityStore.ensure` runs (#7667 §5.1).
- Shared SQL (#7667 §5.1): each file holds one statement; a Go test and a Python test each compare their statement's text, whitespace-normalized, with it.

- [ ] **Step 1: Write the failing tests** (`idempotency_test.go`, `testdb.New`):
  - `TestARetryReplaysTheFirstAnswer`: a write that inserts one row, run twice with key `k` → one row, both answers byte-equal, one `crud_requests` row of caller `inventory`.
  - `TestAKeyWithAnotherBodyIsRefused` → 422, type `…/idempotency-key-reused`, nothing written.
  - `TestTwoRequestsWithOneKeyWriteOnce`: two goroutines, one key → one row, two equal answers.
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
- Errors keep phase 2's problem codes (`inventory/problems.py`): `kind_not_found`, `kind_exists`, `kind_in_use`, `kind_cycle`, `kind_archived`, `item_not_found`, `item_exists`, `bad_property_schema`.

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
- Create: `tools/go/core/internal/inventory/store/stock.go` (lots, units, moves), `tools/go/core/internal/inventory/handlers/steps.go` (the per-step interfaces), `tools/go/core/internal/inventory/handlers/receive.go`, `consume.go`, `count.go`, `move.go`, `retire.go`, `cost.go`, `refuse.go`, `tools/go/core/internal/inventory/flow/flow.go`
- Test: `tools/go/core/internal/inventory/flow/flow_test.go`, `tools/go/core/internal/inventory/store/stock_test.go`, `tools/go/core/internal/inventory/handlers/purity_test.go`

**Interfaces:**
- `handlers/steps.go` (README §8's class diagram; one interface per step):
  `Receiver{ Receive(ctx, tx pgx.Tx, in ReceiveIn) (ReceiveOut, error) }`,
  `Consumer{ Consume(ctx, tx, ConsumeIn) ([]wire.Move, error) }`,
  `Counter{ Count(ctx, tx, CountIn) ([]wire.Move, error) }`,
  `Mover{ Move(ctx, tx, MoveIn) error }`,
  `Retirer{ Retire(ctx, tx, RetireIn) (wire.Unit, error) }`,
  `Coster{ Estimate(ctx, q pgx.Tx, item string, qty *big.Rat) (unit *big.Rat, unknown bool, err error) }`.
  Each `In` carries `Item wire.Item`, `Choice handlers.Choice` (its params), `By store.Actor`, and the phase 2 input fields (`ReceiveLot`, `ReceiveUnits`, `UseInput`, `AdjustInput`, `CountInput`, `LotPatch.location_id`, `UnitPatch.location_id`, `UnitStatusInput`).
  `Lifer`, `Biller`, `Labeler`, `Syncer` are declared and implemented by nothing in K1 (seams for K2–K6).
- Implementations (R-K1-1: each ports its phase 2 path in `inventory/stock.py`, statement for statement, with its locks: `_open_lock` `pg_advisory_xact_lock(hashtext('stock_lots_open'), hashtext(item_id))`, lots `FOR UPDATE` in `(received_at, id)` order):
  `receive.lot` (`receive_in`), `receive.units` (`receive_units`), `consume.lot_fifo` (`take`, `use`), `count.lot` (`count_item`; `adjust_lot` is `count.lot` on one lot), `move.place` (the location of a lot or a unit; no stock move), `retire.default` (`set_unit_status`, every status), `cost.lot_fifo` (`estimate_unit_cost`, and the snapshot `take` writes), `cost.unit` (a unit's cost), `refuse` (409 `step_not_applicable` with the step and the class).
  Every other catalog handler has no implementation: `Registry.For` answers `ErrNotBuilt`, which is 409 `handler_not_built` (R-K1-7).
- `flow.Flow{Pool *pgxpool.Pool; Reg *handlers.Registry}` with `Receive`, `Consume`, `Count`, `Move`, `Retire`, `GiveBack`, `Estimate`, `SetLotCost`. Each: `store.Write` (Task 9); lock and read the item, its chain and the unit's steps; `Resolve`; a step whose choice has a `missing_param` problem → 409 `step_incomplete` (R-K1-8); `Reg.For(step, choice.Handler)`; call it in the transaction. `GiveBack` and `SetLotCost` keep phase 2's paths (`give_back`, `set_lot_cost`); they are not steps.
- `purity_test.go`: a test that parses `handlers/*.go` and `flow/*.go` with `go/parser` and fails on a string literal equal to a kind id of `kinds.yaml` or a special type id (§2 anti-goal). Literals inside `catalog.go` are allowed.

Phase 2's `tests/inventory/test_stock.py` cases are rewritten here, one Go test per Python test (`TestFifoTakesTheOldestLotFirst`, `TestANamedLotIsTakenFirstThenFifo`, `TestAForeignReceiveKeepsWhatWasEntered`, `TestACountSettlesANegativeLotFirst`, `TestBadInputIsRefusedNotA500`, `TestAShortfallGoesNegativeOnTheNewestLot`, `TestAnItemWithNoLotOpensACostUnknownLot`, `TestGiveBackReversesEachUseAtItsOwnCost`, `TestSetLotCostMovesNoPastMove`, `TestAWholeNumberUomRefusesAFraction`, `TestUnitStatusWritesItsMove`, `TestEstimateMatchesTake` (`566.666667` for 15 of 10 @ 500 and 10 @ 700), …). `test_stock.py` stays until its last Python caller goes (R-K1-17).

- [ ] **Step 1: Write the failing tests:** the ported cases, and:
  - `TestAServiceCannotBeReceived`: an item of `labour` (hourly) → `Receive` is 409 `step_not_applicable` `{step: receive}`; no row.
  - `TestADurableConsumeIsNotBuilt`: `Consume` on a `nozzle` item → 409 `handler_not_built` `{handler: consume.use}` (K2 builds it).
  - `TestAnIncompleteStepIsRefused`: a durable person kind with `life.hours` and no `hours` → its item's `Receive` works (receive is complete); a direct check of `step_incomplete` uses a kind whose `count` handler is `count.remaining` with no `capacity` → `Count` is 409 `step_incomplete` `{step: count, missing: [capacity]}`.
  - `TestAPlaceChangeWritesNoMove`: `Move` of a lot to a location → `location_id` set, no `stock_moves` row.
  - `TestTheBaseFlowNamesNoKind` (`purity_test.go`).
- [ ] **Step 2: Run** `cd tools/go/core && go test -race ./internal/inventory/...` — Expected: FAIL.
- [ ] **Step 3: Implement.** Register the K1 implementations in `handlers.Default() *Registry`.
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): the K1 default handlers and the inventory base flow (#6949, K1 4/7)`

### Task 12: The internal routes and the areas

**Files:**
- Create: `tools/go/core/internal/inventory/areas.go`, `tools/go/core/internal/inventory/routes/kinds.go`, `routes/items.go`, `routes/stock.go`, `routes/problems.go`, `tools/go/core/internal/inventory/routes/testdata/answers/*.json`
- Modify: `tools/go/core/main.go` (mount by caller), `tools/go/core/main_test.go`, `tools/go/core/README.md` (the area table and "Where it differs")
- Test: `tools/go/core/internal/inventory/routes/routes_test.go`, `tools/go/core/main_test.go`

**Interfaces:**
- `package inventory`: `const Caller = "inventory"`; `func Areas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]crud.Route` returns `inventory` (every route) and `inventory:read` (its GETs) (§6.2).
- `main.go`: `callerPackages = map[string]areaRoutes{inventory.Caller: inventory.Areas}`; `newRouter` mounts each area of a caller package with `router.Add(caller, routes...)`; an area name in two packages stops the start.
- Routes under `/api/v1/internal/crud/inventory` (paths as the public ones without `/inventory`, bodies and answers as the public JSON, R-K1-6):
  - Reads: `GET /kinds` (`?archived=`), `GET /kinds/{kind_id}`, `GET /kinds/{kind_id}/schema`, `GET /items` (Task 10 filters), `GET /items/{item_id}`, `GET /items/{item_id}/lots`, `GET /lots/{lot_id}`, `GET /items/{item_id}/units` (`?status=`), `GET /units/{unit_id}`, `GET /moves` (`?item_id=&lot_id=&unit_id=&order_item_id=&limit=`), `GET /items/{item_id}/estimate?qty=` (Task 15).
  - Writes (each takes `Idempotency-Key` and `X-ScadBuddy-Actor`, R-K1-5; body bound 64 KiB): `POST /kinds`, `PATCH /kinds/{kind_id}`, `POST /kinds/{kind_id}/archive`, `…/restore`, `POST /items`, `PATCH /items/{item_id}`, `POST /items/{item_id}/archive`, `…/restore`, `POST /items/{item_id}/lots` (receive), `PATCH /lots/{lot_id}` (cost, place, note), `POST /lots/{lot_id}/adjust` (count), `POST /items/{item_id}/use` (consume), `POST /items/{item_id}/count`, `POST /items/{item_id}/units` (receive), `PATCH /units/{unit_id}` (name, serial, place), `POST /units/{unit_id}/status` (retire).
  - `X-ScadBuddy-Actor` missing or not an `Actor` object → 422 `loc: ["header", "x-scadbuddy-actor"]`.
- Problems: `https://scadbuddy.dev/problems/inventory/<code>` with `code`, as `inventory/problems.py` writes them; new codes `step_not_applicable`, `step_incomplete`, `handler_not_built`, `steps_invalid`, `class_changed`, `class_open` (409 for the first three, 422 for the rest). A `steps_invalid` answer lists `problems: [{step, handler, reason}]`.
- `testdata/answers/<route>.json`: one recorded answer per route family (`ItemKind`, `KindList`, `Item`, `ItemList`, `Lot`, `LotList`, `Unit`, `UnitList`, `MoveList`, `KindSchema`), written by `go test ./internal/inventory/routes/ -update` and compared otherwise. Layer 5 validates them with the pydantic models.

Phase 2's `tests/api/test_inventory.py` and `test_inventory_stock.py` cases for kinds, items and stock are rewritten here (status, problem `code`, `errors[].loc`); offers stay in Python (R-K1-17).

- [ ] **Step 1: Write the failing tests:** the ported route cases, and:
  - `TestTheInventoryAreaAnswersOnlyItsCaller` (`main_test.go`): an `agent` credential → 403 `wrong_caller`; an `inventory` credential with `crud.inventory` → 200 and `X-ScadBuddy-Crud: 1`.
  - `TestAReadAreaServesNoWrite`: `SCADBUDDY_CORE_AREAS=inventory:read` → `POST /kinds` is forwarded to `Next` (a stub that answers 599).
  - `TestAWriteWithNoKeyIs428`.
  - `TestTheAnswersMatchTheirGoldenFiles`.
- [ ] **Step 2: Run** `cd tools/go/core && go test -race ./...` — Expected: FAIL.
- [ ] **Step 3: Implement.** A route handler parses, calls `flow` or `store`, and maps problems; nothing else.
- [ ] **Step 4: Run** `cd tools/go/core && go vet ./... && go test -race ./...` and `gofmt -l tools/go` — Expected: PASS, no file listed.
- [ ] **Step 5: Commit** `feat(core): the inventory internal CRUD routes, areas inventory and inventory:read (#6949, K1 4/7)`

---

## Layer 5 — `feat/6949-k1-5-inventory-forwarders` (on layer 4)

### Task 13: The client and the forwarders

**Files:**
- Create: `backend/scadbuddy/inventory/core_client.py`
- Modify: `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/inventory/component.py`, `backend/scadbuddy/inventory/models.py`, `backend/scadbuddy/inventory/problems.py`, `backend/scadbuddy/api/inventory.py` (kinds, items), `backend/scadbuddy/api/inventory_stock.py` (lots, units, moves; not `recost`), `frontend/src/lib/problems.ts`, `frontend/src/mocks/features/inventory.ts`
- Delete: `backend/scadbuddy/inventory/items.py`; from `inventory/kinds.py` the `KindStore` writes (keep `merged_schema`, `item_schema`, `keys_in_use`, which `entities/` reads); `backend/tests/inventory/test_kinds.py`, `test_items.py` (ported in Task 10)
- Test: `backend/tests/api/test_inventory.py`, `backend/tests/api/test_inventory_stock.py`, `backend/tests/inventory/test_core_client.py`, `backend/tests/inventory/test_go_answers.py`

**Interfaces:**
- `Settings.core_crud_url: str | None` (env `SCADBUDDY_CORE_CRUD_URL`, an `http://` URL such as `http://127.0.0.1:8082/api/v1`) (R-K1-4).
- `core_client.py`: `class InventoryCore` over `CrudClient(core_crud_url, "inventory", ServiceAuth(InProcessProvider("inventory", …)))`: `async def call(self, method: str, path: str, *, by: Actor, json: Any = None, params: Mapping[str, Any] | None = None, key: str | None = None) -> Any`. It sends `X-ScadBuddy-Actor` and, on a write, `Idempotency-Key` = `f"{by.kind}:{by.principal or ''}:{key}"` when the public request carried `key`, else `f"req:{uuid4()}"`. A Go problem is raised as `ApiError` with its status, type, detail and `code` unchanged. `CrudUnavailableError` and an unset `core_crud_url` are 503 `https://scadbuddy.dev/problems/inventory/inventory_unavailable`.
- Each public write route takes an optional `Idempotency-Key` header (1–200 characters).
- `models.py`: `ItemKind`, `KindInput`, `KindPatch`, `Item`, `ItemInput`, `InventoryItemPatch`, `Unit` gain Task 10's fields (`Class = Literal`s, `Steps = dict[str, StepConf]`, `incomplete_steps: list[str]`); the phase 2 fields stay (R-K1-6).
- `api/inventory.py`, `api/inventory_stock.py`: each kind, item, lot, unit and move route parses, calls `InventoryCore.call`, and validates the answer with its model. Offers and `recost` keep their current code.
- `frontend/src/lib/problems.ts`: a message for each new code. The mock returns the new fields.

- [ ] **Step 1: Write the failing tests** (the routes with an `httpx.MockTransport` that stands in for Go; it asserts the path, the method, the body, `Idempotency-Key` and `X-ScadBuddy-Actor`):
  - one round trip per family (kinds, items, lots, units, moves);
  - `test_a_go_problem_is_passed_through`: Go answers 409 `step_not_applicable` → the public answer is the same status, type and `code`;
  - `test_go_out_of_reach_is_a_503`: the transport raises `httpx.ConnectError` → 503 `inventory_unavailable` within the budget (a test clock), never 500;
  - `test_no_core_url_is_a_503`;
  - `test_a_persons_key_is_forwarded_with_its_actor`;
  - `test_go_answers_fit_the_public_models` (`test_go_answers.py`): each file in `tools/go/core/internal/inventory/routes/testdata/answers/` validates with its pydantic model, and `model_dump(mode="json")` equals the file.
- [ ] Add `tools/go/core/internal/inventory/routes/testdata/` to the `backend` lane's pattern in `.github/scripts/select-lanes.sh`, and a case to `select-lanes.test.sh` (root CLAUDE.md "A new cross-tree read").
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_inventory.py tests/api/test_inventory_stock.py tests/inventory -q` — Expected: FAIL.
- [ ] **Step 3: Implement**; delete the Python kind and item stores and their tests.
- [ ] **Step 4: Run** the tests, `uv run --frozen pytest tests/auth -q`, `bash .github/scripts/select-lanes.test.sh`, `cd frontend && pnpm gen:api && pnpm typecheck && pnpm lint && pnpm exec vitest run src/pages/inventory src/mocks/features/inventory.test.ts`, `cd agent && pnpm gen:api && pnpm typecheck && pnpm exec vitest run test/inventory.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): the public kind, item and stock routes forward to Go (#6949, K1 5/7)`

### Task 14: The real-stack run and the deployment change

**Files:**
- Modify: `e2e-stack/compose.yaml` (a `core` service built from `tools/go/core/Dockerfile`, `SCADBUDDY_CORE_AREAS=inventory,inventory:read`, `SCADBUDDY_CORE_CRUD_LISTEN=:8082`; the API gets `SCADBUDDY_CORE_CRUD_URL=http://core:8082/api/v1`), `e2e-stack/CLAUDE.md`
- Test: one spec in the real-stack e2e (`e2e-stack/`): make a kind, an item, receive a lot, use 1; the item reads `on_hand` one less.

- [ ] **Step 1: Write the failing spec** `inventory goes through Go` (the e2e-stack's own runner, `e2e-stack/CLAUDE.md`).
- [ ] **Step 2: Run** it — Expected: FAIL (503 `inventory_unavailable`).
- [ ] **Step 3: Add the service and the setting.**
- [ ] **Step 4: Run** it — Expected: PASS.
- [ ] **Step 5: Commit** `test(e2e-stack): run scadbuddy-core for the inventory routes (#6949, K1 5/7)`
- [ ] **Step 6 (outside this repo, before the deploy of layers 3–5):** an eh-homelab/clusters PR sets, in the API pod, `SCADBUDDY_CORE_CRUD_LISTEN=:8082` and `SCADBUDDY_CORE_AREAS` with `inventory,inventory:read` on the `scadbuddy-core` container (#7702), and `SCADBUDDY_CORE_CRUD_URL=http://127.0.0.1:8082/api/v1` on the API container. After the deploy, read `inventory_k1_report` on prod (`deploy/CLAUDE.md` "Prod checks") and post the counts on the K1 issue.

---

## Layer 6 — `feat/6949-k1-6-order-stock-calls` (on layer 5)

### Task 15: Order stock calls

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_order_stock_calls.sql`, `backend/scadbuddy/orders/stock_calls.py`
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`_lock_parts_of`, `_use_parts`, `_return_parts`, `parts_unit_cost`, `recost_lot`), `backend/scadbuddy/api/inventory_stock.py` (`recost`), `backend/scadbuddy/workflows/housekeeping.py` (a prune-schedule sweep), `tools/go/core/internal/inventory/routes/stock.go` (`POST /order-items/{order_item_id}/give-back`, `POST /lots/{lot_id}/recost`, `GET /items/{item_id}/estimate`), `tools/go/core/internal/inventory/flow/flow.go`
- Test: `backend/tests/orders/test_stock_calls.py`, `backend/tests/orders/test_parts_lifecycle.py`, `tools/go/core/internal/inventory/routes/orders_test.go`

**Interfaces:**
- SQL (R-K1-14): `order_stock_calls (id bigint identity PK, key text NOT NULL UNIQUE, order_id text NOT NULL, order_item_id text NOT NULL, item_id uuid NOT NULL, op text NOT NULL CHECK (op IN ('take','give_back')), qty numeric(20,6), actor jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), tries int NOT NULL DEFAULT 0, last_error text, done_at timestamptz, answer jsonb)`; index `(order_item_id, id) WHERE done_at IS NULL`.
- Key: `order_item:<order_item_id>:parts:<n>:<item_id>` (design §6.1, with the part item added). `n` is 1 + the count of the order item's earlier calls with that `op`, read under the order's lock.
- Go: `POST /order-items/{order_item_id}/give-back` body `{item_id}` (phase 2 `give_back`); `POST /lots/{lot_id}/recost` body `{orders: [order_id]}`: 409 `recost_orders_moved` and nothing written when a move of the lot names an order not in the list, else phase 2's `RecostResult`; `GET /items/{item_id}/estimate?qty=` (phase 2 `estimate_unit_cost`). `POST /items/{item_id}/use` takes `order_id` and `order_item_id` (phase 2 `take`'s keywords).
- `orders/stock_calls.py`: `record_take(conn, order, item, part_item_id, qty, by) -> int`, `record_give_back(conn, order, item, part_item_id, by) -> int` (in the transition's transaction, after `order_item_parts.used` is set); `settle(order_id: str) -> None` (after the commit: each pending call of the order in id order, through `CrudSyncClient` with its key; then, under the order's lock, the `parts.used` or `parts.returned` event with phase 2's detail and `done_at`; a call stops at the first failure of its order item, which `tries` and `last_error` record); a 4xx answer marks the call done with the problem as `answer` and writes `parts.stock_refused`.
- `lifecycle.py`: `_use_parts` and `_return_parts` record calls instead of `StockStore.take` and `give_back`; the transition calls `settle` after its commit; `_lock_parts_of` goes (each Go call is its own transaction); before the commit, `_use_parts` reads `GET …/estimate` for each part and raises `bad_quantity` as phase 2 does (a read takes no lock, so it cannot wait on the order's lock); `parts_unit_cost` reads the same route; `recost_lot` locks the orders, calls Go's recost with their ids inside its transaction, and retries on `recost_orders_moved` (phase 2 `_RECOST_TRIES`).
- Housekeeping: `ORDER_STOCK_CALLS_SWEEP = "prune_settle_order_stock_calls"` on the prune schedule (300 s, `PRUNE_INTERVAL`), behind its own workflow patch as each sweep there: `settle` for each order with a call pending for more than 60 s.

- [ ] **Step 1: Write the failing tests** (Go faked by `httpx.MockTransport` in Python; the Go routes tested in Go):
  - `test_fulfil_takes_stock_through_go`: fulfil an item with 2 of a part → one call `take` qty 2 with key `order_item:oi1:parts:1:<part>`, done; one `parts.used` event with the moves' amount.
  - `test_unfulfil_then_fulfil_uses_new_keys`: keys `…:parts:1:…` (take), `…:parts:1:…` (give_back), `…:parts:2:…` (take).
  - `test_a_pending_stock_call_settles_once`: the transport fails during the transition → the order is fulfilled, the call pending; the sweep settles it; a second sweep sends nothing; the fake saw one key twice and Go's replay gives one answer.
  - `test_a_refused_call_is_recorded`: Go answers 422 `bad_quantity` → `parts.stock_refused`, the call done.
  - `test_bad_quantity_refuses_before_the_commit`: the estimate says overflow → `LifecycleError("bad_quantity")`, the order not changed, no call.
  - Go: `TestRecostRefusesAnUnlockedOrder`, `TestGiveBackOfAnOrderItem`, `TestEstimateWritesNothing`.
  - The phase 2 cases of `test_parts_lifecycle.py` still pass with the fake.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/orders/test_stock_calls.py tests/orders/test_parts_lifecycle.py -q` and `cd tools/go/core && go test ./internal/inventory/routes/ -run 'Recost|GiveBack|Estimate'` — Expected: FAIL.
- [ ] **Step 3: Implement.** Remove from `inventory/stock.py` what only `lifecycle.py` called (`take`, `give_back`, `lock_items`, `estimate_unit_cost`, `recost_lot`) unless `part_alias.py` still calls it.
- [ ] **Step 4: Run** the tests, then `uv run --frozen pytest tests/orders tests/workflows -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): order parts take and give back stock through Go with idempotent calls (#6949, K1 6/7)`

---

## Layer 7 — `feat/6949-k1-7-inventory-docs` (on layer 6)

### Task 16: Docs

**Files:**
- Modify: `docs/features/inventory.md`, `docs/architecture/` (the page on processes and the page on data: the inventory tables now have Go as their writer), `backend/CLAUDE.md` (the `inventory/` layout), `tools/go/CLAUDE.md` (`core/` holds `internal/inventory`), `tools/go/core/README.md` (if Task 12 left a gap), `docs/inventory-model/README.md` (one line: the Go test is `internal/inventory/modeldata`), the design's status line ("K1 built")

- [ ] **Step 1:** Write each change; every fact names its file or its PR (#259).
- [ ] **Step 2: Run** `lychee --offline docs/features/inventory.md docs/inventory-model/README.md` if it is installed, else check each relative link by hand — Expected: no broken link.
- [ ] **Step 3: Commit** `docs: inventory K1, the base model in Go (#6949, K1 7/7)`. The PR body says `Fixes` the K1 issue.

---

## Seams K1 leaves for K2–K6

| Seam | K1 leaves | Phase |
|---|---|---|
| `unit_uses`, `consume.use`, manual readings | `Consumer` for durables is `handler_not_built`; `stock_units.life_*` columns stay unwritten | K2 |
| Life | `steps.life` is resolved and validated; `Lifer` has no implementation; `life_*` read fields are derived from `steps` | K2 |
| Handler cache and refresh | No `handler_values`; `handler_config_history` is written from K1 (R-K1-13) | K2 |
| Relationships | `relation_types` (five, seeded) and `relations` exist; only `compatible_with` rows; `installed_in_unit_id` stays a column; no `RelationHandler` | K2 |
| Term services, schedule, signals | `stock_units.starts_at`, `ends_at`, status `ended` exist; no route writes them | K2 |
| The YAML test | Relations and history replay of `check.py` | K2 |
| Purchasing | Offers, price points and `/parts` aliases stay Python SQL (R-K1-17) | K3 |
| Filament | `filament_spool` resolves; filament items are refused (`filament_item_refused`); `filament.*` are `handler_not_built` | K3, K4 |
| Printer | `printer` kind has `special_type` NULL; `printer.*` are `handler_not_built`; `printer_run_hours` untouched | K6 |
| Services on orders | No `order_items.service_item_id`; `consume.service` and `bill.*` not built | later phase |
| Merges | `entities/registry.py` and `printers/store.py` keep their stock `UPDATE`s (R-K1-15) | #7667 phase 4 |
| Drops | `item_kinds.tracking`, `life_basis`; `items.life_*`, `compatible_with`; `stock_units.life_*`; `inventory_k1_report` | K2, after one release |

## Rulings (R-K1-n, made in this plan; the owner may change them)

- **R-K1-1. Port as is.** Each K1 default handler ports the phase 2 code path it replaces,
  statement for statement, with its locks (#7667 §1.3: no new behaviour). Where
  `handlers.yaml` says more, the phase that needs it adds it. So `move.place` writes no
  `transfer` move (a one-row transfer would break `on_hand` = the sum of moves and
  `qty <> 0`), and a unit's status change keeps phase 2's moves.
- **R-K1-2. One idempotency ledger.** Go writes `crud_requests` under the caller
  `inventory`, with `crud/idempotency.py`'s lock class, and `prune` keeps those rows 7 days.
  No `idempotency_keys` table (design §6.3 names one; the ledger already exists).
- **R-K1-3. A caller `inventory`.** The Go router accepts a credential only for its path's
  caller, so the routes under `/internal/crud/inventory/` need a caller of that name. The
  API mints it in process (`InProcessProvider`), scope `crud.inventory`. A later caller (the
  print worker in K6) gets the area mounted under its own prefix.
- **R-K1-4. The address.** `SCADBUDDY_CORE_CRUD_URL`; unset, the inventory routes answer
  503 `inventory_unavailable`. There is no Python fallback: one writer per table.
- **R-K1-5. The actor** travels as `X-ScadBuddy-Actor` (an `Actor` as JSON), so the body is
  the public body. Go sets `scadbuddy.actor` in every write transaction.
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
- **R-K1-12. The history trigger.** A set's actor is `scadbuddy.actor` when it is set, else
  `updated_by` (so a merge's copies name the merge, not the first writer). A cascade is
  recorded by a `BEFORE DELETE` trigger on `entities`, because the entity row is gone when
  the property row's trigger runs. Existing rows get one back-filled `set` each, so the
  history replays to the current properties from the first day.
- **R-K1-13. Parameter history starts in K1**, because K1 lets people change steps.
- **R-K1-14. Order stock calls.** As §6.1 says: the order writes its own row first, then
  calls Go with a key from that row; the prune schedule's sweep (a Temporal activity)
  retries. The `parts.used` event is written when the call settles, not in the transition.
- **R-K1-15. Merges keep their SQL.** `entities/registry.py:132` and `printers/store.py:358`
  repoint a column inside a transaction that holds `entities` and `printers` rows
  `FOR UPDATE`. A Go call from inside it would wait on those rows (the foreign key check),
  and Python would wait on Go. They run no handler; #7667 phase 4 moves them with entities
  and printers.
- **R-K1-16. Readers keep their reads.** `orders/parts.py:421`, `orders/store.py:2661`
  (and so `orders/costs.py`), `entities/sweep.py:31`, `entities/registry.py:203` read stock
  tables; reads are not writes (§6.1). They move with #7667 phase 5, or in K2 where they need
  a cached value.
- **R-K1-17. Offers and the `/parts` aliases stay Python SQL** until K3 moves purchasing
  (or the aliases are deleted). They run no handler. `inventory/stock.py` keeps what
  `part_alias.py` calls.
- **R-K1-18. Layers 3, 4 and 5 deploy together.** Between their merges, Python's phase 2
  writes still work on the new schema (the class columns are nullable), but the prod pin
  moves only when layer 5 is on main and the clusters change of Task 14 Step 6 is ready.
- **R-K1-19. The YAML test in K1** covers the catalog, the kinds, the items, the instances
  and the README table, and the property history through the real trigger. `check.py`'s
  relation checks and its replay of lives and cached values come in K2.
- **R-K1-20. A YAML package for tests only:** `go.yaml.in/yaml/v3`, imported by
  `modeldata` alone.
- **R-K1-21. Python tests fake Go.** The forwarder and lifecycle tests use
  `httpx.MockTransport`; the route behaviour is tested in Go; the recorded Go answers are
  validated by the pydantic models, so "the same JSON" (§6.1) is checked in both trees.

## Self-review

- **Spec coverage.** §3 class: Task 5, Task 7. §4.1–4.3 steps, resolution, parameters:
  Task 5, Task 11. §4.4 special types: Task 5 (catalog), R-K1-7. §4.8: Tasks 1–3, 6. §6.1:
  Tasks 8, 9, 12, 13, 15. §6.2 layout: Tasks 5, 9–12. §6.3 data for K1: Task 7 (the K2 tables
  are seams). §7 proof as a test: Task 6. §8: Task 7. §9 K1 needs the CRUD port: Task 14.
  §4.5–4.7, §5's handlers, §6.4: K2 (seams table).
- **Type names.** `model.Class`, `model.Chain`, `model.Steps`, `handlers.Catalog`,
  `handlers.Resolution`, `handlers.Problem`, `handlers.Registry`, `store.Write`,
  `store.Actor`, `flow.Flow`, `inventory.Areas`, `inventory.Caller`, `InventoryCore.call`,
  `stock_calls.settle` are used with one spelling in every task.
- **Proportion.** The plan gives signatures, tests and values; no task writes a body that
  its tests determine.
