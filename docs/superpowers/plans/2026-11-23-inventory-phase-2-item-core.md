# Inventory phase 2: the item core (kinds, items, offers, lots, units, moves) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every non-filament thing the shop buys or uses is an inventory item of a kind. It
has per-supplier offers, stock in lots or units, and moves that hold a cost snapshot. The
`parts` model moves onto it. Order parts and model part rules point at items. Job cost reads
part cost from the moves (#7067, epic #6949).

**Architecture:** A new backend package `scadbuddy/inventory/` holds the kinds, items,
offers and stock stores over the shared rows pool. Only the API process runs them (#5295).
Items, lots and units are entity kinds (`item`, `lot`, `unit`) of the phase-1 registry, so
links, attachments and properties (#7026) work on them with no new table. An item's
property schema is its kind's `attribute_schema`, merged down the kind tree. The orders
lifecycle takes and gives back stock through the inventory stock store, inside its own
transactions. `orders/costs.py` gets a `parts` cost part, read from `stock_moves`. A second
migration moves `parts`, `part_stock_moves`, `order_item_parts` and `model_part_rules` onto
items. The old `/parts` routes stay as thin aliases until the UI and agent layers move.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, `jsonschema` (Draft 2020-12),
React + TypeScript (frontend), TypeScript agent tools (Claude Agent SDK).

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.3a,
§6.4, §6.5, §6.6, D2, D11, D12, D13, D14, D16, D17, §9 phase 2, §10. Also
`docs/superpowers/specs/2026-11-23-spool-parts-design.md` (#7278: it needs the kind
`spool_part` and the `saleable` columns from this phase; it is not built here). The owner's
scope addition of 2026-11-23 on #7067 adds `saleable`.

## What exists on main today (verified at `95c4a2064`, 2026-11-23)

- The filament purchasing PRs landed. #6266 merged as `33fcad744` (#6613, 2026-11-18).
  #6267 landed as `e2170b3c5` (#6641, 2026-11-18) and `f5fb1f9a8` (#6648). #6700 merged as
  `2b33b0858` (#6716, 2026-11-20). The four PRs that spec §5 names (#6892, #6915, #6878,
  #6903) are merged too (`gh pr view`).
- `filament_suppliers`, `filament_supplier_products` (unique `(supplier_id, upper(sku))`
  among the unarchived), `filament_invoices`, `filament_invoice_lines`,
  `filament_price_points` (`price numeric(12,4)`, CHECK `(source = 'invoice') =
  (invoice_line_id IS NOT NULL)`) and `filament_shopping_items` exist
  (`backend/scadbuddy/migrations/20261117T2042Z_filament_suppliers.sql:8-142`).
  `SuppliersStore` takes a `shop_currency` callable (`backend/scadbuddy/filament/suppliers.py:127`).
- The Bambuddy supplier sync reads only `filament_supplier_products` and the spool's
  invoice (`backend/scadbuddy/filament/bb_sync_plan.py:347-366`, `_supplier_links`
  l.522-540). So a supplier with only `supplier_offers` rows never reaches Bambuddy (D16)
  if phase 2 leaves that code alone.
- Parts: `parts`, `part_stock_moves`, `model_part_rules`, `order_item_parts`
  (`backend/scadbuddy/migrations/20261107T0015Z_parts_inventory.sql:6-66`). `parts.id` is
  `text DEFAULT gen_random_uuid()::text`; each part has its own `currency`; `unit_cost_minor`
  is a weighted average in minor units (`orders/parts.py:296` `average`). All SQL on these
  tables is in `backend/scadbuddy/orders/parts.py` (23 statements; nothing else names them).
- Order use: `OrderLifecycle._use_parts` writes one `material` cost entry per part
  (`source = "part:<item>:<part>"`) only when the part's currency is the order's and the
  amount is above 0, then a `use` move (`orders/lifecycle.py:5315-5358`). `_return_parts`
  deletes that entry and writes a `return` move (l.5360-5402). A new item copies its
  model's rule (`rule_parts`, l.3934-3946); a duplicate copies the original's parts
  (l.3828-3841). The unit-cost estimate reads `PartStore.unit_cost_of_item`
  (`api/orders.py:1170`, `orders/parts.py:384`).
- Job cost: `CostPart = Literal["prints", "labour"]` (`orders/costs.py:86`); the cost
  entries are `other_minor` (`Margin`, l.164-180; `_item_cost` l.527-600). Part cost
  reaches job cost today only as those `part:` entries.
- Routes `/api/v1/parts…`, `/parts/rules/{slug}`, `/orders/{order_id}/parts`,
  `/orders/{order_id}/items/{item_id}/parts` (`backend/scadbuddy/api/parts.py:63-250`).
  Agent tools `part_list` (read), `part_stock_add` (write), `order_item_parts_set` (write)
  (`agent/src/tools/orders.ts:1570-1640`); the other parts routes are `coverage.ts`
  entries (l.89-111). UI: `/orders/parts` (`frontend/src/App.tsx:142`), `PartsPage.tsx`,
  `components/orders/ItemParts.tsx`, `components/orders/PartRules.tsx`, the Orders tab
  (`components/orders/OrdersTabs.tsx:25`), mocks `mocks/features/orders/parts.ts`.
- Entities: `KINDS` registers `part` on `parts` (`backend/scadbuddy/entities/registry.py:37-52`);
  `repoint_entity` (l.85-110) moves files and links. No owner store calls a `drop_entity`;
  the housekeeping sweep removes orphans (`entities/sweep.py`).
- Properties (#7026) are NOT on main. The stack is open: #7079 (store,
  `feat/7026-1-properties-store`), #7085 (routes), #7091 (UI), #7100 (agent), #7101 (docs).
  This plan uses its API by name: `EntityPropertyStore.list/apply/of_kind/schema/set_schema/
  keys_in_use`, `check_schema`, `_schema_in(conn, kind)`, `PropertyPatch`,
  `BadPropertyValue`, `BadPropertySchema` (`backend/scadbuddy/entities/properties.py` in
  #7079); routes `GET|PATCH /entities/{ref}/properties`, `GET /entity-kinds/{kind}/properties`,
  `GET|PUT /entity-kinds/{kind}/property-schema` (#7085); `PropertiesPanel({entity, title})`
  and `entityApi.propertySchema(kind)` (#7091, `frontend/src/components/entities/`); tools
  `entity_property_list|set|schema_get|schema_set` (#7100, `agent/src/tools/entities.ts`).

## Global Constraints

- Only the API process holds SQL. Agent tools and the frontend call CRUD routes (root
  CLAUDE.md "Database access (#5295)"). No new database client, role or grant.
- Migrations: new files only, `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in
  `backend/scadbuddy/migrations/`, later than every file on main when made. Never edit a
  merged one (root CLAUDE.md "Migrations (#491)").
- Phase 2 changes no filament table's schema and no filament code path
  (`filament/*.py`, `bb_sync_*`) (spec §6.5 "Phase 2 changes no filament table"). New
  foreign keys TO `filament_suppliers`, `filament_locations` and `filament_invoice_lines`
  are allowed.
- Kinds and attribute schemas are rows; no allowlist of kinds or keys in code (spec §4).
  A schema documents and never restricts: every `attribute_schema` write goes through
  #7079's `check_schema` (422 `bad_property_schema`).
- Kind id: `^[a-z][a-z0-9_]{0,39}$`. `tracking` in `lot`, `unit`, `filament`, `none`.
  A `none` kind is not counted: its items take offers and prices, never lots, units or
  moves; every stock call on such an item is 409 `item_not_counted` (Ruling 1).
  `life_basis` in `none`, `hours`, `months`.
- Money: stock costs are in the shop currency (`orders_currency`), in minor units.
  `unit_cost_minor numeric(20,6)`, `amount_minor numeric(20,6)`, unrounded; job cost
  rounds an item's sum once (as `orders/parts.py:39` `UNIT_COST` keeps a fraction today).
  Quantities `numeric(20,6)`; an `each` item takes whole numbers only (422 `bad_quantity`).
- A move's cost is a snapshot (D14). A later lot cost change moves no past move except
  through "Re-cost past uses".
- FIFO by `(received_at, id)` unless the caller names a lot (D2). Stock may go negative; a
  shortfall takes the newest lot's cost and sets `needs_review` (spec §6.6).
- Offers are for non-filament items only until phase 3 (D12). SKU unique per
  `(supplier_id, upper(sku), item_id)` among the unarchived (D13).
- Nothing in phase 2 sends an item, offer or price to Bambuddy (D16).
- Entity wire form `kind:ref`; new kinds `item`, `lot`, `unit` (uuid ids).
- Agent tool risks: reads `read`; every inventory write `write` (as `part_stock_add`,
  `orders.ts:1609`); `inventory_lot_recost` is `write` too (Ruling 8).
- Each layer: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format
  --check . && uv run --frozen mypy`; `cd frontend && pnpm lint && pnpm typecheck`;
  `cd agent && pnpm lint && pnpm typecheck` for the trees it touches.
- Conventional-commit titles with `(#7067, N/6)`; PR body `Part of #7067` (the last layer:
  `Fixes #7067`). Every fact in docs and comments cites its source (#259).
- Load `frontend-design` before Task 10.

## Pre-flight (cross-layer contracts; check before Task 1)

1. **Properties stack.** Start layer 1 after #7079..#7101 merge. If they are still open,
   cut layer 1 from `feat/7026-5-properties-docs` and rebase onto main when they merge.
   Layer 1 depends on `check_schema` and `_schema_in`; layer 2 on the property routes;
   layer 4 on `PropertiesPanel`; layer 5 on `entity_property_*`.
2. **Item schema hook (owned by Task 2, used by Tasks 5 and 10).** `properties.py`
   resolves a schema per entity, not per entity kind:
   `schema_for(conn, ref: EntityRef) -> dict[str, Any]`. For `item` it is
   `inventory.kinds.item_schema(conn, item_id)`; for every other kind it is today's
   `_schema_in(conn, ref.kind)`. `apply` calls it. `set_schema("item", …)` raises
   `SchemaOwnedByKind` (409 `schema_owned_by_item_kind`).
3. **Ids.** `items.id`, `stock_lots.id`, `stock_units.id`, `stock_moves.id`,
   `supplier_offers.id`, `offer_price_points.id` are `uuid`. A migrated item keeps its
   part's id (`parts.id::uuid`), so `part_id` values in the alias routes, activity
   details and `entities.ref` stay valid. `part_item_map (part_id text PK, item_id uuid)`
   records the mapping for every part.
4. **Stock store in order transactions.** Every stock write takes a `conn` and runs in
   the caller's transaction: `StockStore.take(conn, …)`, `StockStore.give_back(conn, …)`.
   The orders lifecycle never opens a second transaction for stock.
5. **Job cost.** After layer 3, part cost is `Margin.parts_minor` (from `stock_moves`),
   and no new `part:` cost entry is written. The parts migration converts the old ones
   (Task 6), so no order counts a part twice.
6. **Prod measures before layer 3 merges** (`deploy/CLAUDE.md` "Prod checks", read only;
   these were not measured for this plan). Record the answers in the layer-3 PR body:
   - `SELECT count(*) FROM parts WHERE id !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'` (expect 0);
   - `SELECT currency, count(*) FROM parts GROUP BY 1` and `SELECT value FROM settings WHERE name = 'orders_currency'`, and whether the API pod sets the currency from its environment;
   - `SELECT count(*) FROM order_cost_entries WHERE source LIKE 'part:%'`; counts of `parts`, `part_stock_moves`, `model_part_rules`, `order_item_parts` (used > 0), `entities WHERE kind = 'part'`;
   - `SELECT DISTINCT supplier FROM parts`.
   If any part's currency differs from the shop's, stop and ask the owner before merge.
7. **Coverage.** Each new route is a tool or a `coverage.ts` entry, or
   `agent/test/coverage.test.ts` fails (`agent/CLAUDE.md` l.393-396). Layer 2 adds
   temporary entries naming the layer-5 tools (the pattern of #7085); layer 5 drops them.
8. **#7278 contract.** #7278 waits for phase 3 (#7068) as well (Ruling 7); phase 2 adds
   no `filament_spools` column. Seeded kind `spool_part` (`tracking = unit`, `saleable = false`,
   schema with `empty_weight_g` and `role`); `stock_units.installed_in_unit_id` exists;
   moves accept `use` and `return` with `entity_id`. Phase 2 builds no install route.

## Review Focus

1. A use larger than all stock on hand: FIFO empties every lot, the rest goes to the
   newest lot (on hand below zero) at its cost, with `needs_review`. An item with no lot at
   all opens a zero-cost `adjustment` lot with `cost_unknown`, and job cost marks `parts`
   unknown. Tests in Task 4 and Task 8.
2. Unfulfil after a lot's cost was edited: the return gives back exactly what each use
   took, at the use's own `unit_cost_minor`, to the same lot, not at today's cost. Test in
   Task 4 and Task 7.
3. An order item fulfilled before the migration and unfulfilled after it: the converted
   history returns the right count and cost, and the item's `parts_minor` ends at 0. Test
   in Task 6 and Task 7.
4. A property on an item whose kind's parent types the key (`hardware` < `part`): a wrong
   type is 422 `bad_property_value`; an unknown key is stored. A kind schema that would
   restrict keys is 422 and leaves the old one. Test in Task 2 and Task 5.
5. Two offers with one SKU at one supplier for two items (a bundle) are allowed; the same
   SKU twice for one item is 409 `offer_exists`; a supplier with only non-filament offers
   produces no Bambuddy supplier link. Test in Task 3.

---

## Layer 1 — `feat/7067-1-inventory-store` (from main, or Pre-flight 1)

### Task 1: The core migration

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_inventory_core.sql`
- Test: `backend/tests/inventory/__init__.py`, `backend/tests/inventory/test_schema.py`

Tables (one file; a top comment cites #7067 and spec §6.4–6.6):
- `item_kinds (id text PK CHECK (id ~ '^[a-z][a-z0-9_]{0,39}$'), name text NOT NULL
  (1..100), parent_id text REFERENCES item_kinds (id), tracking text NOT NULL CHECK IN
  ('lot','unit','filament','none'), uom text NOT NULL DEFAULT 'each' CHECK (uom ~ '^[a-z]{1,8}$'),
  attribute_schema jsonb NOT NULL DEFAULT '{"type":"object","properties":{}}', life_basis
  text NOT NULL DEFAULT 'none' CHECK IN ('none','hours','months'), saleable boolean NOT
  NULL DEFAULT false, builtin boolean NOT NULL DEFAULT false, archived_at timestamptz,
  created_at, updated_at, CHECK (parent_id IS DISTINCT FROM id))`.
- `items (id uuid PK DEFAULT gen_random_uuid(), kind_id text NOT NULL REFERENCES
  item_kinds (id), name text NOT NULL (1..200), manufacturer text NOT NULL DEFAULT '',
  compatible_with text[] NOT NULL DEFAULT '{}', low_stock numeric(20,6) NOT NULL DEFAULT 0
  CHECK (>= 0), life_hours numeric(12,2) CHECK (> 0), life_months integer CHECK (> 0),
  saleable boolean, source text NOT NULL DEFAULT 'manual', source_ref text,
  edited_fields text[] NOT NULL DEFAULT '{}', note text NOT NULL DEFAULT '' (<= 4000),
  archived_at, created_at, updated_at)`; unique index `(kind_id, lower(name)) WHERE
  archived_at IS NULL`; unique `(source, source_ref) WHERE source_ref IS NOT NULL`.
- `supplier_offers (id uuid PK, supplier_id uuid NOT NULL REFERENCES filament_suppliers
  (id), item_id uuid NOT NULL REFERENCES items (id), sku text NOT NULL (1..100), url text
  CHECK (url ~* '^https?://'), pack_size numeric(20,6) NOT NULL DEFAULT 1 CHECK (> 0),
  pack_uom text NOT NULL DEFAULT 'each', note text, archived_at, created_at, updated_at)`;
  unique index `(supplier_id, upper(sku), item_id) WHERE archived_at IS NULL`; index
  `(item_id)`.
- `offer_price_points (id uuid PK, offer_id uuid NOT NULL REFERENCES supplier_offers (id),
  price numeric(12,4) NOT NULL CHECK (>= 0), currency text NOT NULL CHECK (~
  '^[A-Z]{3}$'), on_date date NOT NULL, source text NOT NULL CHECK IN
  ('invoice','manual'), invoice_line_id uuid REFERENCES filament_invoice_lines (id),
  created_at, CONSTRAINT offer_price_points_source_line CHECK ((source = 'invoice') =
  (invoice_line_id IS NOT NULL)))`; index `(offer_id, on_date DESC)`. (Ruling 4: created
  now, in spec §6.5's final shape; phase 3 moves `filament_price_points` into it.)
- `stock_lots (id uuid PK, item_id uuid NOT NULL REFERENCES items (id), location_id uuid
  REFERENCES filament_locations (id), received_qty numeric(20,6) NOT NULL, on_hand
  numeric(20,6) NOT NULL, unit_cost_minor numeric(20,6) NOT NULL CHECK (>= 0),
  cost_unknown boolean NOT NULL DEFAULT false, cost_currency text, origin text NOT NULL
  CHECK IN ('invoice_line','production_run','opening','adjustment','manual'),
  invoice_line_id uuid REFERENCES filament_invoice_lines (id), production_run_id uuid,
  note text NOT NULL DEFAULT '', received_at timestamptz NOT NULL DEFAULT
  clock_timestamp(), created_by jsonb NOT NULL)`; index `(item_id, received_at, id)`.
  `origin = 'manual'` is a receive with no invoice yet (today's "Add stock"); spec §6.4
  lists the other four. `cost_currency` is set only on opening lots from parts.
- `stock_units (id uuid PK, item_id uuid NOT NULL REFERENCES items (id), serial text,
  name text NOT NULL DEFAULT '', location_id uuid REFERENCES filament_locations (id),
  status text NOT NULL DEFAULT 'in_stock' CHECK IN
  ('in_stock','in_service','retired','scrapped','sold'), installed_in_unit_id uuid
  REFERENCES stock_units (id) CHECK (installed_in_unit_id IS DISTINCT FROM id),
  cost_minor numeric(20,6) NOT NULL CHECK (>= 0), cost_unknown boolean NOT NULL DEFAULT
  false, origin text NOT NULL (as lots), invoice_line_id, production_run_id,
  in_service_at, retired_at, life_hours, life_months, printer_id uuid REFERENCES printers
  (id), location_ref_id uuid REFERENCES filament_locations (id), received_at, created_by
  jsonb NOT NULL)`; unique `(item_id, serial) WHERE serial IS NOT NULL`.
- `stock_moves (id uuid PK, at timestamptz NOT NULL DEFAULT clock_timestamp(), item_id
  uuid NOT NULL REFERENCES items (id), lot_id uuid REFERENCES stock_lots (id), unit_id
  uuid REFERENCES stock_units (id), kind text NOT NULL CHECK IN
  ('receive','use','return','adjust','produce','transfer','sell','retire','scrap'), qty
  numeric(20,6) NOT NULL CHECK (qty <> 0), unit_cost_minor numeric(20,6) NOT NULL,
  amount_minor numeric(20,6) NOT NULL, cost_unknown boolean NOT NULL DEFAULT false,
  needs_review boolean NOT NULL DEFAULT false, reverses_move_id uuid UNIQUE REFERENCES
  stock_moves (id), reason text NOT NULL DEFAULT '', order_id text, order_item_id text,
  production_run_id uuid, print_run_id text, entity_id uuid REFERENCES entities (id),
  actor jsonb NOT NULL, CHECK ((lot_id IS NULL) <> (unit_id IS NULL)), CHECK
  (amount_minor = qty * unit_cost_minor))`; indexes `(item_id, at)`, `(order_item_id)
  WHERE order_item_id IS NOT NULL`, `(lot_id)`. No FK on `order_id`/`order_item_id`
  (a move outlives a deleted draft, as `part_stock_moves` l.39-42).
- Seed rows of `item_kinds` (`builtin = true`), per Ruling 1: see Task 1 test.
  `spool_part` gets `attribute_schema` `{"type":"object","properties":{"empty_weight_g":
  {"type":"integer","exclusiveMinimum":0,"description":"Weight of the empty part in
  grams"},"role":{"type":"string","enum":["shell","addon"],"description":"A reusable
  shell, or an addon such as an AMS adaptor"}}}` (spool-parts spec §5).

- [ ] **Step 1: Write the failing tests** (`requires_postgres`, the `pg_conninfo` /
  `pg_pool` fixtures `tests/entities/conftest.py` uses):
  - `test_the_builtin_kinds_are_seeded`: ids, parents, tracking, uom, life_basis and
    saleable equal this table (Ruling 1):

    | id | parent | tracking | uom | life_basis | saleable |
    |---|---|---|---|---|---|
    | filament | — | filament | g | none | false |
    | printer | — | unit | each | hours | false |
    | part | — | lot | each | none | false |
    | nozzle, hotend | part | unit | each | hours | false |
    | belt | part | unit | each | hours | false |
    | build_plate | part | unit | each | months | false |
    | ams_part, hardware | part | lot | each | none | false |
    | consumable | — | lot | each | none | false |
    | desiccant | consumable | lot | g | none | false |
    | packaging, lubricant | consumable | lot | each / ml | none | false |
    | equipment | — | unit | each | months | false |
    | dry_bin, tool | equipment | unit | each | months | false |
    | printed_part, assembly | — | lot | each | none | true |
    | service | — | none | each | none | false |
    | spool_part | — | unit | each | none | false |

  - `test_spool_part_schema_passes_check_schema`: `check_schema(row.attribute_schema)` raises nothing for every seeded kind.
  - `test_a_move_needs_one_lot_or_one_unit` and `test_a_move_amount_is_qty_times_cost`: the CHECKs refuse bad rows.
  - `test_an_offer_sku_is_unique_per_item_not_per_supplier`: two items, one SKU, one supplier: both insert; a second row for the same item raises `UniqueViolation`; an archived one does not count.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_schema.py -q` — Expected: FAIL (tables missing).
- [ ] **Step 3: Write the migration.**
- [ ] **Step 4: Run** the same command and `uv run --frozen pytest tests/test_pg_migrations.py -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): kinds, items, offers, lots, units and moves tables (#7067, 1/6)`

### Task 2: Kinds and items store; items take properties

**Files:**
- Create: `backend/scadbuddy/inventory/__init__.py`, `inventory/models.py`,
  `inventory/kinds.py`, `inventory/items.py`, `inventory/component.py`
- Modify: `backend/scadbuddy/entities/registry.py` (`KINDS`: `item` on `items`, `lot` on
  `stock_lots`, `unit` on `stock_units`, all `uuid`), `backend/scadbuddy/entities/properties.py`
  (`schema_for`, `SchemaOwnedByKind`), `backend/scadbuddy/main.py` (register the component
  as `ENTITIES` is)
- Test: `backend/tests/inventory/test_kinds.py`, `test_items.py`,
  `backend/tests/entities/test_properties.py` (two cases)

**Interfaces:**
- Consumes: #7079 `check_schema`, `BadPropertySchema`, `_schema_in`; `EntityStore`.
- Produces (`inventory/models.py`):
  - `Tracking = Literal["lot", "unit", "filament", "none"]`, `LifeBasis = Literal["none", "hours", "months"]`.
  - `ItemKind(BaseModel)`: `id, name, parent_id: str | None, tracking, uom, attribute_schema: dict, life_basis, saleable: bool, builtin: bool, archived_at, item_count: int`.
  - `KindInput` (`id`, `name`, `parent_id`, `tracking`, `uom`, `attribute_schema`, `life_basis`, `saleable`), `KindPatch` (all optional but `id`), `extra="forbid"`.
  - `Item(BaseModel)`: `id: UUID, kind_id, kind_path: list[str]` (root first), `tracking, uom, name, manufacturer, compatible_with: list[str], low_stock: Decimal, life_hours, life_months, saleable: bool | None, effective_saleable: bool, source, source_ref, edited_fields, note, archived_at, on_hand: Decimal` (lots' sum, or units `in_stock` count), `stock_value_minor: Decimal` (on-hand lots × cost, plus in-stock units' cost), `cost_unknown: bool`, `low: bool` (`low_stock > 0 and on_hand <= low_stock`).
  - `ItemInput`, `ItemPatch` (`extra="forbid"`).
- Produces (`inventory/kinds.py`): `class KindStore(pool)`: `list(include_archived=False) -> list[ItemKind]`, `get(id)`, `create(body, by)`, `update(id, patch, by)`, `archive(id)`, `restore(id)`; `merged_schema(conn, kind_id) -> dict` (ancestors' `properties` then the kind's, a child's key wins); `item_schema(conn, item_id: str) -> dict` (the item's kind's merged schema; the empty object schema for a bad id). Errors: `KindNotFound`, `KindExists`, `KindCycle`, `KindInUse` (tracking change on a kind with items, or on a builtin), `BadPropertySchema` (re-raised from `check_schema`).
- Produces (`inventory/items.py`): `class ItemStore(pool, entities)`: `list(*, kind: str | None, descendants: bool = True, q: str | None, low: bool = False, include_archived: bool = False, compatible: str | None) -> list[Item]`; `get(id) -> Item | None`; `create(body, by) -> Item`; `update(id, patch, by) -> Item` (adds each changed field to `edited_fields` when `source != 'manual'`); `archive(id)`, `restore(id)`. Errors: `ItemNotFound`, `ItemExists`, `FilamentItemRefused` (a kind with `tracking = filament`, Ruling 9), `KindArchived`. `q` matches name, manufacturer and any property value of the item (`EXISTS` over `entity_properties` joined to `entities` with `kind = 'item'`, `value::text ILIKE`).
- Produces (`entities/properties.py`): `schema_for(conn, ref: EntityRef) -> dict[str, Any]`; `apply` uses it; `class SchemaOwnedByKind(ValueError)` raised by `set_schema("item", …)`.
- Produces (`inventory/component.py`): `INVENTORY` component key; `InventoryComponent.kinds`, `.items`, `.offers`, `.stock`; `InventoryDep`.

- [ ] **Step 1: Write the failing tests**
  - `test_kinds.py`: create `fastener` under `part`; `merged_schema("fastener")` has `part`'s keys plus its own; a child key overrides the parent's subschema; `update(parent_id=<own descendant>)` raises `KindCycle`; a restricting schema (`additionalProperties: false`) raises `BadPropertySchema` and the stored schema is unchanged; changing `tracking` of `nozzle` raises `KindInUse`; `archive` of a kind with unarchived items raises `KindInUse`.
  - `test_items.py`: an item of `service` (`tracking = none`) has `on_hand == 0`, `stock_value_minor == 0` and `low is False` whatever its `low_stock`; create in `hardware`, `get` gives `kind_path == ["part", "hardware"]`, `effective_saleable is False`; with `saleable=True` it is True; `list(kind="part")` includes it, `list(kind="part", descendants=False)` does not; a second item named the same in any case in the kind raises `ItemExists`; create in `filament` raises `FilamentItemRefused`; with `source="bambu_store"`, `update(name=…)` gives `edited_fields == ["name"]`; `list(q="0451")` finds an item whose property `mpn` is `"X-0451"`.
  - `test_properties.py`: `test_an_items_schema_is_its_kinds`: set `part`'s schema to type `mpn` as string; `apply(item:<id>, {"mpn": 5})` raises `BadPropertyValue("mpn", …)`; `{"colour": "red"}` is stored; `set_schema("item", …)` raises `SchemaOwnedByKind`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory tests/entities/test_properties.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** the models, stores, component and the registry rows. `apply` reads `schema_for` before its transaction, as it reads `_schema_in` today.
- [ ] **Step 4: Run** the same command plus `tests/entities -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): kinds and items; an item's properties follow its kind (#7067, 1/6)`

### Task 3: Offers and price points

**Files:**
- Create: `backend/scadbuddy/inventory/offers.py`
- Test: `backend/tests/inventory/test_offers.py`, `backend/tests/filament/test_bb_sync_plan.py` (one case; find the module that tests `_supplier_links` with `grep -l _supplier_links backend/tests`)

**Interfaces:**
- Produces: `Offer(BaseModel)`: `id, supplier_id, supplier_name, item_id, item_name, sku, url, pack_size: Decimal, pack_uom, note, archived_at, latest_price: PricePoint | None`; `PricePoint`: `id, price: Decimal, currency, on_date, source, invoice_line_id`; `OfferInput`, `OfferPatch`, `PriceInput (price, on_date, currency defaults to the shop's)`. `class OfferStore(pool, shop_currency: Callable[[], str])`: `for_item(item_id)`, `for_supplier(supplier_id)`, `by_sku(supplier_id, sku) -> list[Offer]` (every item a bundle SKU covers, D13), `create`, `update`, `archive`, `add_price(offer_id, body) -> PricePoint`, `prices(offer_id)`. Errors `OfferExists`, `OfferNotFound`, `SupplierNotFound`, `FilamentItemRefused` (an offer for an item of a `filament` kind, D12).

- [ ] **Step 1: Write the failing tests**: Review Focus 5 (bundle SKU allowed, `OfferExists` for a repeat on one item, case-insensitive); `by_sku` returns both bundle items; an offer for an archived supplier raises `SupplierNotFound`; `add_price` with `source` absent is `manual`; a price in a currency other than the shop's is 422-bound `BadCurrency` (spec §3: stock is in the shop currency). In the BB sync test: a supplier with only a `supplier_offers` row is in no spool's supplier links.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_offers.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the same plus the BB sync test file — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): supplier offers with purchase links and prices (#7067, 1/6)`

### Task 4: Lots, units and moves (FIFO, snapshots, reversal, re-cost)

**Files:**
- Create: `backend/scadbuddy/inventory/stock.py`
- Test: `backend/tests/inventory/test_stock.py`

**Interfaces:**
- Produces (`inventory/models.py`): `Lot`, `Unit`, `Move` (every column of Task 1, decimals serialized as strings as `InventoryPart._decimal` does, `orders/parts.py:119`); `ReceiveLot (qty, total_cost_minor | unit_cost_minor, location_id, note, received_at?)`; `ReceiveUnits (count 1..100, cost_minor each, serials: list[str] | None, location_id, name)`; `UseInput (qty > 0, lot_id?, entity: str | None ("kind:ref"), reason)`; `AdjustInput (on_hand, note required)`; `UnitStatusInput (status, entity?, note)`; `RecostResult (moves: int, orders: list[RecostedItem{order_id, order_item_id, before_minor, after_minor}])`.
- Produces `class StockStore(pool, entities)`:
  - `receive_lot(item_id, body, by) -> Lot` (one `receive` move); `receive_units(item_id, body, by) -> list[Unit]` (one `receive` move each).
  - `take(conn, item_id, qty: Decimal, by, *, lot_id=None, order_id=None, order_item_id=None, entity_id=None, reason="", kind="use") -> list[Move]` — FIFO; Review Focus 1 rules; locks the item's lots `FOR UPDATE` in `(received_at, id)` order.
  - `give_back(conn, *, order_item_id: str, item_id: UUID, by) -> list[Move]` — one `return` move per unreversed `use` move of that order item and item, same lot, same `unit_cost_minor`, `reverses_move_id` set.
  - `use(item_id, body, by) -> list[Move]` (`take` in its own transaction; `entity` resolved with `EntityStore.ensure`).
  - `adjust_lot(lot_id, body, by) -> Lot`; `count_item(item_id, on_hand, note, by) -> list[Move]` (a lower count takes FIFO as `adjust`; a higher one adds to the newest lot at its cost).
  - `set_unit_status(unit_id, body, by) -> Unit` — moves: `in_service` → `use` (−1), `sold` → `sell`, `retired` → `retire`, `scrapped` → `scrap`, back to `in_stock` → `return` (+1); sets `in_service_at` / `retired_at`. A unit with `installed_in_unit_id` set is refused (`UnitInstalled`; #7278 owns install and remove).
  - `set_lot_cost(lot_id, unit_cost_minor, by) -> Lot` (clears `cost_unknown`; no move changes, D14).
  - `recost_lot(conn, lot_id) -> RecostResult` — every `use`/`return`/`adjust` move of the lot re-snapshotted at the lot's cost now; per order item, the parts sum before and after.
  - `lots(item_id)`, `units(item_id, status?)`, `moves(*, item_id=None, lot_id=None, unit_id=None, order_item_id=None, limit=200)`, `estimate_unit_cost(conn, item_id, qty) -> tuple[Decimal, bool]` (what `take` would charge per unit now, and whether any part is cost-unknown; reads, writes nothing).
  - Errors: `LotNotFound`, `UnitNotFound`, `WrongTracking` (a lot call on a unit item and the reverse), `ItemNotCounted` (any lot, unit or move call on an item of a `none` kind, Ruling 1; `take` raises it too, so an order part of such an item is refused at `set_item_parts`), `BadQuantity`, `UnitInstalled`.

- [ ] **Step 1: Write the failing tests** (`test_stock.py`):
  - `test_fifo_takes_the_oldest_lot_first`: lots 10 @ 5.00 (day 1) and 10 @ 7.00 (day 2); take 15 → moves `-10 @ 500`, `-5 @ 700`; on hand 0 and 5.
  - `test_a_named_lot_is_taken_first`.
  - `test_a_shortfall_goes_negative_on_the_newest_lot`: take 25 of the above → third move `-5 @ 700`, `needs_review`, newest lot `-5`.
  - `test_an_item_with_no_lot_opens_a_cost_unknown_lot`: one `adjustment` lot, cost 0, `cost_unknown`; the move has `cost_unknown`.
  - `test_give_back_reverses_each_use_at_its_own_cost`: take 15, `set_lot_cost` of lot 2 to 9.00, `give_back` → `+10 @ 500`, `+5 @ 700`; a second `give_back` writes nothing.
  - `test_set_lot_cost_moves_no_past_move` then `test_recost_resnapshots_and_names_the_order_items`: one use for order item `oi1` (`-5 @ 700`), recost at 900 → `RecostResult.orders == [RecostedItem("o1", "oi1", 3500, 4500)]`.
  - `test_a_whole_number_uom_refuses_a_fraction`: `each`, qty `1.5` → `BadQuantity`; `ml` takes it.
  - `test_unit_status_writes_its_move`: receive 2 units at 3000; `in_service` → `use` move −1 @ 3000 and `in_service_at` set; `retired` then → `retire`.
  - `test_lot_calls_on_a_unit_item_are_refused` (`WrongTracking`).
  - `test_a_not_counted_item_takes_no_stock`: an item of `service`: `receive_lot`, `receive_units`, `take`, `use`, `count_item` each raise `ItemNotCounted` and no row is written.
  - `test_estimate_matches_take`: `estimate_unit_cost(item, 15)` is `(Decimal("566.666667"), False)` for the two lots above, and nothing is written.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/inventory/test_stock.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement** `stock.py`. Keep `lots.on_hand` equal to the sum of its moves in each write's transaction.
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/inventory -n auto -q`, then ruff and mypy — Expected: PASS, clean.
- [ ] **Step 5: Commit** `feat(inventory): lots, units and moves with FIFO and cost snapshots (#7067, 1/6)`

## Layer 2 — `feat/7067-2-inventory-routes` (on layer 1)

### Task 5: CRUD routes

**Files:**
- Create: `backend/scadbuddy/api/inventory.py` (kinds, items, offers),
  `backend/scadbuddy/api/inventory_stock.py` (lots, units, moves),
  `backend/scadbuddy/inventory/problems.py`
- Modify: `backend/scadbuddy/api/entity_properties.py` (`GET /entities/{ref}/property-schema`;
  `PUT /entity-kinds/item/property-schema` → 409), `backend/scadbuddy/main.py` (routers),
  `agent/src/tools/coverage.ts` (temporary entries, Pre-flight 7)
- Test: `backend/tests/api/test_inventory.py`, `backend/tests/api/test_inventory_stock.py`,
  `backend/tests/api/test_entity_properties.py` (two cases)

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces (all under `/api/v1`, tag `inventory`, store calls in `asyncio.to_thread`, a
  `summary` and `description` on each route; no `from __future__ import annotations`):
  - Kinds: `GET /inventory/kinds` → `{kinds: ItemKind[]}` (`?archived=true`); `POST /inventory/kinds`; `PATCH /inventory/kinds/{kind_id}`; `POST /inventory/kinds/{kind_id}/archive|restore`; `GET /inventory/kinds/{kind_id}/schema` → `{kind_id, schema (merged), own_schema, keys_in_use}`.
  - Items: `GET /inventory/items?kind=&descendants=&q=&low=&archived=&compatible=` → `{items: Item[]}`; `POST /inventory/items`; `GET|PATCH /inventory/items/{item_id}`; `POST /inventory/items/{item_id}/archive|restore`.
  - Offers: `GET /inventory/items/{item_id}/offers`; `GET /inventory/suppliers/{supplier_id}/offers`; `POST /inventory/offers`; `PATCH /inventory/offers/{offer_id}`; `POST /inventory/offers/{offer_id}/archive`; `GET|POST /inventory/offers/{offer_id}/prices`.
  - Stock: `GET|POST /inventory/items/{item_id}/lots`; `GET|POST /inventory/items/{item_id}/units`; `GET|PATCH /inventory/lots/{lot_id}` (PATCH: `unit_cost_minor`, `location_id`, `note`); `POST /inventory/lots/{lot_id}/adjust`; `POST /inventory/items/{item_id}/use`; `POST /inventory/items/{item_id}/count`; `GET|PATCH /inventory/units/{unit_id}` (name, serial, location); `POST /inventory/units/{unit_id}/status`; `GET /inventory/moves?item_id=&lot_id=&unit_id=&order_item_id=&limit=`.
  - `POST /inventory/lots/{lot_id}/recost` is added in Task 7 (it writes order events).
  - `GET /entities/{ref}/property-schema` → `KindPropertySchema` of the entity's effective schema (`schema_for`), for every kind.
  - Problems (`inventory/problems.py`): 404 `kind_not_found`, `item_not_found`, `offer_not_found`, `lot_not_found`, `unit_not_found`, `supplier_not_found`; 409 `kind_exists`, `kind_in_use`, `item_exists`, `offer_exists`, `wrong_tracking`, `item_not_counted`, `unit_installed`, `filament_item_refused`, `schema_owned_by_item_kind`; 422 `kind_cycle`, `bad_quantity`, `bad_property_schema`, `bad_currency`.

- [ ] **Step 1: Write the failing tests** with the app `client` fixture of `tests/api/test_entity_links.py`: one round trip per family (kinds, items, offers, lots, units, moves); each problem code above at least once; `GET /entities/item:<id>/property-schema` returns the merged kind schema; `PUT /entity-kinds/item/property-schema` → 409 `schema_owned_by_item_kind`; `PATCH /entities/item:<id>/properties` with a value the kind types wrong → 422 `bad_property_value`; `POST /inventory/kinds` with `"attribute_schema": {"type":"object","required":["mpn"]}` → 422 `bad_property_schema` and `GET` shows no kind.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/api/test_inventory.py tests/api/test_inventory_stock.py tests/api/test_entity_properties.py -q` — Expected: FAIL (404).
- [ ] **Step 3: Implement** the routes and problems; add the coverage entries (reason: "Inventory phase 2 (#7067, spec §6.4–6.6); its `inventory_*` tools follow in layer 5 of the same stack, which drops these entries").
- [ ] **Step 4: Run** the tests, `uv run --frozen pytest tests/auth -q` (route coverage), and `cd agent && pnpm exec vitest run test/coverage.test.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): kind, item, offer and stock routes (#7067, 2/6)`

## Layer 3 — `feat/7067-3-parts-to-items` (on layer 2; Pre-flight 6 first)

### Task 6: Migrate parts onto items

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_parts_to_items.sql`
- Test: `backend/tests/test_pg_migrations.py` (`test_the_parts_migration_*`, the pattern of
  `test_the_projection_migration_backfills_inputs_of_existing_rows`, l.168-183)

The migration, in one file, in this order:
1. `part_item_map`: `item_id = parts.id::uuid` when it is a uuid's text, else
   `gen_random_uuid()`.
2. `items` from `parts`: kind `hardware` (Ruling 2), name, `low_stock`, `source = 'manual'`,
   `created_at`/`updated_at` kept.
3. Suppliers (Ruling 5): for each distinct non-empty `parts.supplier`, the unarchived
   `filament_suppliers` row with that `upper(name)`, else a new row (name only). One
   `supplier_offers` row per part with a supplier: `sku` = `'PART-' || left(item_id::text, 8)`
   (parts have no SKU; a person edits it), `url` = `parts.link` or NULL, `pack_size` =
   `parts.pack_size`. One `offer_price_points` row per offer: `price` =
   `pack_price_minor / 10^exponent` in `parts.currency`, `on_date` = `parts.updated_at::date`,
   `source = 'manual'` (Ruling 4). The exponent comes from the part's currency as
   `orders/currency.py` `minor_exponent` gives it; write the three-letter exceptions as a
   SQL `CASE` and cite that file.
4. A part with a link and no supplier: one `entity_links` row, `kind = 'purchase'`,
   `source = 'parts_migration'`, on its item's entity (made here).
5. One opening lot per part: `origin = 'opening'`, `received_qty` = sum of its `add`
   deltas, `on_hand = parts.on_hand`, `unit_cost_minor = parts.unit_cost_minor`,
   `cost_currency = parts.currency`, `cost_unknown = (parts.currency <> shop)` where `shop`
   is `coalesce((SELECT value #>> '{}' FROM settings WHERE name = 'orders_currency'), 'USD')`
   (D11; Pre-flight 6 checks prod has no other case), `received_at = parts.created_at`,
   `created_by` = the system actor `{"kind":"provider","principal":"parts_migration"}`.
6. Every `part_stock_moves` row becomes a `stock_moves` row on that lot: `add` →
   `receive`, `adjust` → `adjust`, `use` → `use`, `return` → `return`; `qty = delta`,
   `unit_cost_minor` = the use's `unit_cost_after_minor` (a `use`'s cost, `orders/parts.py:397`
   `used_at`), `amount_minor = qty × unit_cost_minor`, `cost_unknown = (use or return and
   amount_minor IS NULL and unit cost > 0)`, `reason = note`, order ids, `actor =
   created_by`, `at = created_at`. A `return` gets `reverses_move_id` = the newest
   unreversed `use` of the same order item and part.
7. Delete every `order_cost_entries` row whose `source` is `part:<order item>:<part>` and
   whose order item has a converted `use` move (Pre-flight 5).
8. `order_item_parts`: add `item_id uuid REFERENCES items (id)`, fill from the map, `SET NOT
   NULL`, drop `part_id`, new PK `(order_item_id, item_id)`, index `(item_id)`.
9. `model_part_rules.lines`: rewrite every `part_id` (line and `when`) to its item id with
   `jsonb_set` over the array; the key becomes `item_id`.
10. `entities` of kind `part`: `kind = 'item'`, `ref` = the item id; their
    `entity_attachments.subject` likewise.
11. Rename `parts` → `parts_retired`, `part_stock_moves` → `part_stock_moves_retired` (Ruling 6; a follow-up issue drops them after a prod check).

- [ ] **Step 1: Write the failing tests** (apply files before this one, seed, apply it):
  - `test_the_parts_migration_keeps_stock_cost_and_history`: a part (USD, shop USD) with
    an add of 10 @ 599, a use of 3 for `oi1` (entry `part:oi1:<p>` 1797) → one item with the
    part's id; one opening lot `on_hand = 7`, `unit_cost_minor = 599`; two moves; no
    `part:` cost entry; `order_item_parts.item_id` set.
  - `test_a_part_in_another_currency_opens_a_cost_unknown_lot` (EUR part, shop USD).
  - `test_a_returned_use_is_linked_to_its_use`.
  - `test_rules_and_entities_point_at_items`: a rule with `when` and a link on `part:<p>` →
    `lines[0].item_id`, `lines[0].when[0].item_id`; the link lists under `item:<p>`.
  - `test_suppliers_are_matched_by_name_or_made`: `"bambu lab"` matches an existing
    "Bambu Lab" row; `"Amazon"` makes one; `""` makes none and the link becomes a
    `purchase` entity link.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/test_pg_migrations.py -k parts_migration -q` — Expected: FAIL.
- [ ] **Step 3: Write the migration.**
- [ ] **Step 4: Run** the same — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): parts become items with opening lots (#7067, 3/6)`

### Task 7: Orders use items; rules and item parts repoint; re-cost

**Files:**
- Modify: `backend/scadbuddy/orders/parts.py` (keep only the models and the
  order-item-parts and rule reads/writes, over `order_item_parts.item_id` and
  `model_part_rules`; `ItemPart.item_id` with `validation_alias=AliasChoices("item_id",
  "part_id")`; `PartRuleLine.item_id`, `PartRuleWhen.item_id` likewise; drop `PartStore`'s
  stock and part writes), `backend/scadbuddy/orders/lifecycle.py` (`self.stock`;
  `_use_parts` l.5315 → `self.stock.take(conn, item_id, per_unit × quantity, by,
  order_id=…, order_item_id=…)` per row, no cost entry; `_return_parts` l.5360 →
  `self.stock.give_back(conn, …)` per row; `set_item_parts` checks items exist and are
  `lot`-tracked (`LifecycleError("part_not_found")` kept as the code); new
  `recost_lot(lot_id, by) -> RecostResult` writing one `item.parts_recosted` event per
  order with each item's before and after), `backend/scadbuddy/api/orders.py:1170`
  (`unit_cost` estimate from `estimate_unit_cost` per item part × `per_unit`),
  `backend/scadbuddy/api/inventory_stock.py` (`POST /inventory/lots/{lot_id}/recost`),
  `backend/scadbuddy/orders/component.py` (pass the stock store)
- Test: `backend/tests/orders/test_parts_lifecycle.py`, `backend/tests/orders/test_parts.py`,
  `backend/tests/api/test_inventory_stock.py` (recost)

**Interfaces:**
- Consumes: `StockStore.take`, `give_back`, `recost_lot`, `estimate_unit_cost` (Task 4).
- Produces: `OrderLifecycle.recost_lot(lot_id: UUID, by: Actor) -> RecostResult`;
  `ItemPartRow(item_id, per_unit, used)`; `PartNeed` and `OrderParts` keep their fields with
  `item_id` beside `part_id` (same value) for one release.

- [ ] **Step 1: Write the failing tests**: fulfil an item with 2 × hook per unit, quantity 3
  → one `use` move of −6 at the FIFO cost with `order_item_id`, no `part:` cost entry,
  `order_item_parts.used == 6`; unfulfil → reversing `return` moves at the use's cost even
  after a lot cost change (Review Focus 2); an item converted by Task 6 (seed through the
  migration as Task 6 does) unfulfils to `parts_minor == 0` (Review Focus 3); a new item of
  a slug whose rule has a `when` colour gets the colour's item; `PUT
  /orders/{id}/items/{item}/parts` with `part_id` keys still works; `POST
  /inventory/lots/{id}/recost` writes `item.parts_recosted` on the affected order with
  before and after; the unit-cost route's `parts_unit_minor` equals the FIFO estimate.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/orders/test_parts_lifecycle.py tests/orders/test_parts.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/orders tests/inventory -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): order parts take stock from inventory lots (#7067, 3/6)`

### Task 8: Job cost reads part cost from moves

**Files:**
- Modify: `backend/scadbuddy/orders/costs.py` (`CostPart` adds `"parts"`; `Margin.parts_minor:
  Money = 0`; `_item_cost` adds it to `cost_minor`; `_order_costs` sums it; the module
  docstring gains a **Parts** paragraph citing spec §6.6 and #7067), the cost reader
  (`_Rows` or `_read`, l.300 / l.670: one query per order, `SELECT order_item_id,
  sum(-amount_minor), bool_or(cost_unknown) FROM stock_moves WHERE order_id = %s AND kind IN
  ('use','return') GROUP BY 1`)
- Modify: `backend/scadbuddy/orders/report.py` (or whatever builds `orders_profit_report`;
  `grep -n other_minor backend/scadbuddy/orders`) so the report carries `parts_minor`
- Test: `backend/tests/orders/test_costs.py`, `backend/tests/api/test_orders_job_cost.py`

**Interfaces:**
- Produces: `Margin.parts_minor`; `"parts"` in `ItemCost.unknown` when a move is
  `cost_unknown` or the order's currency is not the shop's (the existing rule for today's
  settings, docstring l.38-39).

- [ ] **Step 1: Write the failing tests**: an item with a use of 6 @ 599.5 → `parts_minor
  == 3597`, `cost_minor` includes it, `other_minor` excludes it; after a give-back both 0;
  a cost-unknown move → `"parts" in unknown` and margin state `unknown`; an order in EUR
  with a shop in USD → `"parts" in unknown`; the order sums its items' `parts_minor`.
- [ ] **Step 2: Run** `cd backend && uv run --frozen pytest tests/orders/test_costs.py -q` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd backend && uv run --frozen pytest tests/orders tests/api/test_orders_job_cost.py -n auto -q` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): job cost reads part cost from stock moves (#7067, 3/6)`

### Task 9: `/parts` aliases and the stop-gap UI change

**Files:**
- Modify: `backend/scadbuddy/api/parts.py` (each route keeps its path, method and
  response model; it reads and writes items: `InventoryPart` from `Item` + its newest
  offer + its latest price point; `unit_cost_minor` = on-hand lots' weighted average for
  display; `POST /parts` creates an item of kind `hardware` + offer + price + a `manual`
  lot when `on_hand > 0`; `POST /parts/{id}/stock` receives `packs × pack_size` at
  `packs × pack price` (the offer's latest price point, or the body's `pack_price_minor`,
  which also writes a new `manual` price point: Add stock keeps prefilling the cost, Ruling 4); `POST /parts/{id}/adjust` → `count_item`; `GET /parts/{id}/moves`
  maps `Move` to `StockMove`; each route's description says "Alias of /inventory/…; goes
  after one release", spec §6.11)
- Modify: `frontend/src/pages/PartsPage.tsx` (the panels' entity `part:<id>` → `item:<id>`)
- Test: `backend/tests/api/test_parts.py` (existing; it must pass unchanged but for
  `part_id` values that are now uuids), `frontend/src/pages/PartsPage.test.tsx`

- [ ] **Step 1: Run** `cd backend && uv run --frozen pytest tests/api/test_parts.py -q` — Expected: FAIL (tables gone).
- [ ] **Step 2: Implement** the aliases; change the two entity strings.
- [ ] **Step 3: Run** the same and `cd frontend && pnpm exec vitest run src/pages/PartsPage.test.tsx` (after `pnpm gen:api`) — Expected: PASS.
- [ ] **Step 4: Run** the whole backend suite `cd backend && uv run --frozen pytest -n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `refactor(orders): the parts routes are aliases of inventory (#7067, 3/6)`

## Layer 4 — `feat/7067-4-inventory-ui` (on layer 3)

### Task 10: The Inventory area

Load `frontend-design` first.

**Files:**
- Create: `frontend/src/pages/inventory/InventoryPage.tsx` (kind tree on the side, items
  table: name, kind, on hand, low, value, offers count; search; low filter; "New item"),
  `ItemPage.tsx` (header with kind path and saleable; `PropertiesPanel`, offers with
  purchase links and latest price and "Add price", `LinksPanel`, `AttachmentsPanel`, lots or
  units by tracking (none for a `none` kind: offers only) with "Receive" (cost prefilled
  from the newest offer's latest price × qty / pack size, Ruling 4), "Use", "Count", lot cost edit and "Re-cost past uses",
  moves), `UnitPage.tsx` (status with its actions, location, cost, the three panels),
  `KindsPage.tsx` (tree, add, edit name, parent, uom, life basis, saleable, archive; the
  schema as a key table: key, type, description; refusals show the 422 detail),
  `pages/inventory/api.ts`, `frontend/src/lib/inventoryLinks.ts` (+ test),
  `frontend/src/mocks/features/inventory.ts` (register it as the other features are)
- Create: `test-fixtures/inventory/ui-paths.json` (item, unit, lot, kind, supplier offer)
- Modify: `frontend/src/App.tsx` (`inventory`, `inventory/items/:id`, `inventory/units/:id`,
  `inventory/lots/:id` → its item page with `#lot-<id>`, `inventory/kinds`),
  `frontend/src/components/AppShell.tsx` (an Inventory entry beside Filament, l.111),
  `frontend/src/components/entities/PropertiesPanel.tsx` (schema from `GET
  /entities/{ref}/property-schema`)
- Test: `InventoryPage.test.tsx`, `ItemPage.test.tsx`, `UnitPage.test.tsx`,
  `KindsPage.test.tsx`, `PropertiesPanel.test.tsx`, `lib/inventoryLinks.test.ts`,
  `e2e/page-titles.spec.ts` (new paths)

**Interfaces:**
- Consumes: Task 5 and Task 7 routes through the generated client (`pnpm gen:api`).
- Produces: `inventoryPaths.item(id)`, `.unit(id)`, `.lot(id)`, `.kinds`,
  `.offer(itemId, offerId)` (anchor `offer-<id>`), checked against the fixture by both
  `frontend/src/lib/inventoryLinks.test.ts` and (Task 12) the agent.

- [ ] **Step 1: Write the failing tests**: the list filters by a kind and its children and
  by `q`; the item page of a `lot` item shows lots and receives one (POST body `qty`,
  `total_cost_minor`); of a `unit` item shows units and changes one's status; lot cost
  edit then "Re-cost past uses" shows the returned orders with links; an offer's URL renders
  `target="_blank" rel="noopener noreferrer"`; the panel on an item shows the kind's
  description as a hint; a kind schema PUT that restricts keys shows `bad_property_schema`.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/inventory src/lib/inventoryLinks.test.ts src/components/entities` — Expected: FAIL.
- [ ] **Step 3: Implement** the pages, mocks, routes and nav entry.
- [ ] **Step 4: Run** the same plus `pnpm typecheck && pnpm lint` and `pnpm exec playwright test e2e/page-titles.spec.ts` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(inventory): the Inventory area: items, units, kinds (#7067, 4/6)`

### Task 11: Order parts and suppliers move to items

**Files:**
- Modify: `frontend/src/components/orders/ItemParts.tsx`, `PartRules.tsx` (pick items of
  `lot` tracking from `/inventory/items`, send `item_id`), `frontend/src/pages/OrderPage.tsx`
  (part names link to `inventoryPaths.item`), `frontend/src/pages/filament/SupplierPage.tsx`
  (an "Other offers" section from `GET /inventory/suppliers/{id}/offers`), `frontend/src/App.tsx`
  (`orders/parts` → `<Navigate to="/inventory?kind=part" replace />`, spec §6.11),
  `components/orders/OrdersTabs.tsx` (the Parts tab links to Inventory), `api/client.ts`
  (drop the `parts*` calls), `mocks/features/orders/parts.ts` (serve item ids)
- Delete: `frontend/src/pages/PartsPage.tsx`, `PartsPage.test.tsx`
- Test: `ItemParts.test.tsx`, `PartRules` tests, `SupplierPage.test.tsx`, `OrdersPage.test.tsx`

- [ ] **Step 1: Write the failing tests**: `ItemParts` sends `{parts: [{item_id, per_unit}]}`;
  `/orders/parts` lands on `/inventory?kind=part`; the supplier page lists a bearing offer
  with its link.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/orders src/pages/filament/SupplierPage.test.tsx src/pages/OrdersPage.test.tsx` — Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `cd frontend && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(orders): order parts and model rules pick inventory items (#7067, 4/6)`

## Layer 5 — `feat/7067-5-inventory-agent` (on layer 4)

### Task 12: `inventory_*` tools and the part aliases

**Files:**
- Create: `agent/src/tools/inventory.ts`, `agent/src/tools/inventoryLinks.ts`
- Modify: `agent/src/tools/index.ts` (register), `agent/src/tools/coverage.ts` (drop the
  layer-2 entries and the `/parts` entries l.89-111; keep reasons only for routes with no
  tool), `agent/src/sessions/touched.ts` (every new write tool, as l.334-357),
  `agent/src/tools/entities.ts` (`uiPath` for `item`, `unit`, `lot`),
  `agent/src/tools/orders.ts` (`part_list`, `part_stock_add`: descriptions say "Alias of
  inventory_item_list / inventory_lot_receive; goes after one release"; they call the
  inventory routes; `order_item_parts_set` takes `item_id`, and `part_id` as an alias)
- Modify: `plugins/scadbuddy/` skills that name the part tools, if any (`grep -rn part_list
  plugins` found none on 2026-11-23), and `plugin.json`'s version only if a skill changes
- Test: `agent/test/inventory.test.ts`, `agent/test/inventoryLinks.test.ts` (reads
  `test-fixtures/inventory/ui-paths.json`), `agent/test/orders.test.ts` (aliases),
  `agent/test/coverage.test.ts`

**Interfaces:**
- Produces tools (route; risk): `inventory_kind_list` (GET kinds; read),
  `inventory_kind_upsert` (POST/PATCH kinds; write), `inventory_item_list` (GET items, `q`
  matches properties server-side; read), `inventory_item_get` (item + offers + lots or
  units + last 20 moves; read), `inventory_item_upsert` (POST/PATCH item, optional
  `properties` applied by one `PATCH /entities/item:<id>/properties` as #7100's filament
  upserts do; write), `inventory_item_archive` (write), `inventory_offer_list` (read),
  `inventory_offer_upsert` (write), `inventory_offer_price_add` (write),
  `inventory_lot_receive` (write), `inventory_lot_update` (cost, location, note; write),
  `inventory_lot_recost` (write, Ruling 8), `inventory_unit_receive` (write),
  `inventory_unit_status` (write), `inventory_move_list` (read), `inventory_move_use`
  (write), `inventory_move_count` (write). Every result names `entity_ui_path` (the #6878
  pattern) and is wrapped as untrusted data as the other tools are.

- [ ] **Step 1: Write the failing tests**: each tool's route and risk (the table style of
  `agent/test/entityTools.test.ts` in #7100); `inventory_item_upsert` with `properties` sends the item
  write then the PATCH, and a 422 on the PATCH reports a saved item and the detail;
  `part_list` still answers parts in its old shape from `/inventory/items?kind=part`;
  `order_item_parts_set` with `part_id` sends `item_id`; `uiPath("item:<id>")` matches the
  fixture.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/inventory.test.ts test/inventoryLinks.test.ts test/orders.test.ts test/coverage.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement.** Descriptions state FIFO, snapshots (D14), "Re-cost past uses"
  changes past order costs, and the `kind:ref` form.
- [ ] **Step 4: Run** `cd agent && pnpm lint && pnpm typecheck && pnpm test` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): inventory tools; the part tools become aliases (#7067, 5/6)`

## Layer 6 — `feat/7067-6-inventory-docs` (on layer 5)

### Task 13: Docs

**Files:**
- Create: `docs/features/inventory.md` (one page per feature, `docs/features/README.md` lists it)
- Modify: `docs/features/orders-and-quotes.md` (parts are inventory items; job cost's
  `parts` part), `docs/architecture/data.md` (the eight new tables, both migrations, the
  retired tables), `docs/architecture/frontend.md` (Inventory routes; `/orders/parts`
  redirect), `backend/CLAUDE.md` (the `inventory/` layout line), `agent/CLAUDE.md` (the
  tools line, if it lists tool families), the spec's §9 phase 2 line (a "done in" note
  with the PRs) and any deviation below that the owner accepted.

- [ ] **Step 1: Edit**, each fact with its source (file:line, PR or issue).
- [ ] **Step 2: Run** `grep -rn "orders/parts\|part_stock_moves\|PartStore" docs backend/CLAUDE.md frontend/CLAUDE.md agent/CLAUDE.md` — Expected: only historical specs and plans.
- [ ] **Step 3: Commit** `docs: inventory item core (#7067, 6/6)`

## Deferred from §6.4–6.6 (and why)

- `filament_shopping_items.item_id` (§6.5), `cost_per_kg_at` on spool events and the
  `inventory_moves` view (§6.6), the filament precedence in job cost (D7): they change
  filament tables, which phase 2 may not (§6.5). Phase 3 (#7068).
- `model_part_rules` → model-owned recipes (§6.8): phase 5 (#7070). Phase 2 repoints the
  rules in place.
- Install, value over life and printers as units (§6.4): phase 4 (#7069); the columns exist.
- `saleable` is stored and shown; refusing to sell is phase 6 (#7071) and #7278.
- #7278 (spool parts) waits for phase 3 (#7068), where spools get `filament_spools.unit_id`
  (Ruling 7).

## Rulings (owner, 2026-11-23)

Answers to the plan's open questions, given through the coordinator. "Owner" marks the
owner's own ruling; "Controller" marks a proposed default the coordinator accepted. Every
task above follows them.

1. **Owner.** Kinds gain a fourth tracking, `none` ("not counted"): the kind has items and
   offers and prices, but never lots, units or moves; any stock call or move on such an
   item is refused (409 `item_not_counted`). `service` is a `none` kind. The rest of the
   built-in kinds table (Task 1) stands as proposed. Spec §6.4 carries the amendment
   (this branch).
2. **Controller.** Migrated parts become items of kind `hardware`.
3. **Owner.** Job cost gets a `parts` figure read from `stock_moves`. The migration
   converts `part_stock_moves` and deletes the `part:` cost entries it replaces.
4. **Owner.** Pack prices are kept: `offer_price_points` is created now, in spec §6.5's
   final shape, and each part's pack price moves there. Add stock keeps prefilling the cost.
5. **Controller.** A part with no supplier name gets no supplier and no offer; its link
   becomes a `purchase` entity link.
6. **Controller.** `parts` and `part_stock_moves` are renamed `*_retired`; a follow-up drops
   them after a prod check.
7. **Owner.** #7278 waits for phase 3 (#7068); phase 2 adds no `filament_spools` column.
   `spool_part` stays seeded.
8. **Controller.** `inventory_lot_recost` is tier `write`.
9. **Controller.** Items and offers of a `filament` kind are refused until phase 3.
10. **Controller.** The quote estimate prices parts at what a FIFO take would cost now.
