# Entity properties: arbitrary key-value pairs on everything — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any entity (a spool type and a spool first; items, lots and units later) carries
free key-value properties (lot number, GTIN, ASIN, MPN, manufacturer URL, …) that a person
or the agent can add, rename and remove, and that the filament search matches (#7026).

**Architecture:** A new `entity_properties` table hangs off the phase-1 `entities` registry,
as `entity_links` does, with one store (`entities/properties.py`), one route family
(`/api/v1/entities/{kind}:{ref}/properties`, plus per-kind reads under
`/api/v1/entity-kinds/{kind}/…`), one UI panel and one set of agent tools. Each entity kind
has an optional property schema (`entity_property_schemas`) that types and documents known
keys and never refuses an unknown one. The filament search (frontend and agent) reads every
property of a kind in one call and adds the values to its search documents.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, `jsonschema` (already a
dependency), React + TypeScript, TypeScript agent tools.

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.3a,
D17, §9 phase 1b. Issue #7026; epic #6949.

## Global Constraints

- Only the CRUD service touches SQL; agent tools and the frontend call CRUD routes.
- One new migration file, `$(date -u +%Y%m%dT%H%MZ)_entity_properties.sql`; edit no merged file.
- Key: `^[a-z][a-z0-9_]{0,63}$`. At most 200 keys per entity. No list of allowed keys in code.
- Value: a string (at most 2000 characters), a finite number, a boolean, or a list of at
  most 50 strings of at most 500 characters each. Anything else is 422 `bad_property_value`.
- A schema never restricts: a schema with `additionalProperties` (other than `true`),
  `required`, `propertyNames` or `patternProperties` at its top level, or one that is not a
  valid Draft 2020-12 schema of `"type": "object"`, is 422 `bad_property_schema`.
- A known key (in the kind schema's `properties`) is validated against its subschema
  (Draft 2020-12); a mismatch is 422 `bad_property_value` naming the key.
- `source`: `manual` for a person's or the agent's write. No import writes in this plan.
- Wire form of an entity: `kind:ref` (phase 1's `parsed`, `KINDS`). Every registered kind
  takes properties, the order kinds included; `print_archive` does not (422 `unknown_entity_kind`).
- Seeded schemas, verbatim in the migration:
  - `spool_type`: `mpn` string "Manufacturer part number"; `gtin` array of string
    "GTIN / EAN / UPC barcodes (spool and refill)"; `asin` array of string
    "Amazon ASINs as `marketplace:ASIN`, such as `us:B0CXYZ1234`"; `manufacturer_url`
    string "The manufacturer's product page"; `empty_spool_weight_g` number, minimum 0,
    "Weight of the empty spool in grams"; `td` number, minimum 0, "Transmission distance (HueForge TD)".
  - `spool`: `lot_number` string "Lot or batch number printed on the label".
- Agent tool risks: reads `read`; `entity_property_set` and `entity_property_schema_set`
  are `write`, the same risk as `filament_type_upsert` and `filament_spool_upsert`, which
  also take `properties` (ruling: properties are the record's own data, not an outward act).
- Load `frontend-design` before Task 3.

## Review Focus

1. A rename (old key `null`, new key set, in one PATCH) is atomic: a bad new value leaves
   the old key in place. Test in Task 1.
2. A list value with a number in it (`["123", 4]`) and a nested object are refused, not
   stored. Test in Task 1.
3. A printer merge keeps the gone printer's properties, and the kept printer's value wins a
   key both have (`repoint_entity`). Test in Task 1.
4. The filament search finds a type by one GTIN of its list and a spool by its lot number,
   and a failed property read leaves the search working without them. Tests in Tasks 3 and 4.
5. A schema PUT that would refuse keys is 422 and leaves the old schema. Test in Task 2.

---

### Task 1: Table, store, merge and sweep

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_entity_properties.sql`
- Create: `backend/scadbuddy/entities/properties.py`
- Modify: `backend/scadbuddy/entities/registry.py` (`repoint_entity`)
- Modify: `backend/scadbuddy/entities/component.py` (`properties` property)
- Test: `backend/tests/entities/test_properties.py`, `backend/tests/entities/test_sweep.py`

**Interfaces:**
- Produces:
  - `PropertyValue = str | float | int | bool | list[str]` (pydantic-validated).
  - `class EntityProperty(BaseModel)`: `key: str`, `value: PropertyValue`, `source: str`,
    `updated_at: datetime`, `updated_by: Actor`.
  - `class PropertyPatch(BaseModel)`: `set: dict[str, JsonValue]` (pydantic `JsonValue`;
    `None` removes), `extra="forbid"`, 1 to 200 entries. The shapes are checked by the store,
    not the model, so every bad value is one error, `BadPropertyValue`.
  - `class BadPropertyValue(ValueError)`: `.key: str`, `.reason: str`.
  - `class BadPropertySchema(ValueError)`: `.reason: str`.
  - `EntityPropertyStore(pool, entities)` with
    - `list(ref: EntityRef) -> list[EntityProperty]` (by key),
    - `apply(ref: EntityRef, patch: PropertyPatch, by: Actor) -> list[EntityProperty]`
      (one transaction; `EntityNotFound` for no owner row; `BadPropertyValue`; the whole
      patch or nothing; over 200 keys after it is `BadPropertyValue` with key `*`),
    - `of_kind(kind: str) -> dict[str, dict[str, PropertyValue]]` (ref to key to value),
    - `schema(kind: str) -> dict[str, Any]` (`{"type": "object", "properties": {}}` when no row),
    - `set_schema(kind: str, schema: dict[str, Any], by: Actor) -> dict[str, Any]`
      (`BadPropertySchema`),
    - `keys_in_use(kind: str) -> list[tuple[str, int]]` (key, count of entities; by key).
  - `EntitiesComponent.properties -> EntityPropertyStore`.

- [ ] **Step 1: Write the failing tests** in `tests/entities/test_properties.py`
  (`requires_postgres`, a spool type row made as `test_sweep.py` `_spool` makes a spool):
  - `test_set_list_and_remove`: apply `{"gtin": ["0123", "0456"], "td": 1.8}`, list gives
    both by key with `source == "manual"`; apply `{"td": None}` leaves only `gtin`.
  - `test_a_rename_is_one_patch_and_all_or_nothing`: with `mpn` set, apply
    `{"mpn": None, "part_no": "X"}` gives only `part_no`; with the seeded `spool_type`
    schema, apply `{"mpn": None, "empty_spool_weight_g": "heavy"}` raises
    `BadPropertyValue` (key `empty_spool_weight_g`) and `mpn` is still there.
  - `test_values_outside_the_four_shapes_are_refused`, parametrized over `{"a": 1}`
    (object), `["1", 2]`, `[["x"]]`, `float("nan")`, `"x" * 2001`, `["y"] * 51`, `["z" * 501]`: each raises `BadPropertyValue` naming key `a` and nothing
    is stored.
  - `test_a_bad_key_is_refused`, parametrized over `"GTIN"`, `"1abc"`, `"a" * 65`, `"a-b"`.
  - `test_unknown_keys_pass_a_schema_and_known_keys_are_typed`: on `spool_type`,
    `{"colour_family": "blue"}` is stored; `{"gtin": "0123"}` (not a list) is refused.
  - `test_set_schema_refuses_a_restricting_schema`, parametrized over
    `{"type": "object", "additionalProperties": False}`, `{"type": "object", "required": ["mpn"]}`,
    `{"type": "object", "propertyNames": {"maxLength": 3}}`,
    `{"type": "object", "patternProperties": {"^x": {}}}`, `{"type": "array"}`,
    `{"type": "object", "properties": {"a": {"type": "nope"}}}`: each raises
    `BadPropertySchema` and `schema("spool_type")` is unchanged.
  - `test_of_kind_and_keys_in_use`: two spool types with `gtin`, one with `mpn`: `of_kind`
    maps both refs; `keys_in_use` is `[("gtin", 2), ("mpn", 1)]`.
  - `test_the_seeded_schemas`: `schema("spool")["properties"]` has exactly `lot_number`;
    `schema("spool_type")["properties"]` has the six keys of Global Constraints.
  - `test_apply_on_a_missing_owner_raises_entity_not_found`.
  In `tests/entities/test_sweep.py`, extend `test_a_printer_merge_keeps_the_gone_printers_files_and_links`:
  the gone printer has properties `{"a": "gone", "b": "gone"}`; with `kept_has_files` the
  kept printer has `{"b": "kept"}`; after the merge the kept printer lists `a = gone` and
  `b = kept` (or `b = gone` when the kept printer had none). Add to
  `test_an_orphan_loses_its_files_holds_and_links_…`: the orphan's properties are gone.

- [ ] **Step 2: Run them to see them fail**
  Run: `cd backend && uv run pytest tests/entities/test_properties.py tests/entities/test_sweep.py -q`
  Expected: FAIL (`scadbuddy.entities.properties` does not exist).

- [ ] **Step 3: The migration.** `entity_properties (entity_id uuid NOT NULL REFERENCES
  entities (id) ON DELETE CASCADE, key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,63}$'),
  value jsonb NOT NULL CHECK (jsonb_typeof(value) IN ('string','number','boolean','array')),
  source text NOT NULL DEFAULT 'manual', updated_at timestamptz NOT NULL DEFAULT
  clock_timestamp(), updated_by jsonb NOT NULL, PRIMARY KEY (entity_id, key))`; an index on
  `(key)`. `entity_property_schemas (kind text PRIMARY KEY CHECK (kind ~ '^[a-z_]+$'), schema
  jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_by
  jsonb)`, seeded with the two schemas of Global Constraints; a seeded row's `updated_by`
  is NULL (no `Actor` kind is a migration), a person's write sets it. A comment at the top cites #7026 and spec §6.3a.

- [ ] **Step 4: Implement `entities/properties.py`.** Value checks in Python (the shapes and
  limits of Global Constraints), then the kind schema's subschema for a known key with
  `jsonschema.Draft202012Validator`. Schema checks: `Draft202012Validator.check_schema`,
  top-level `type == "object"`, none of the four restricting keywords. `apply` locks the
  entity row (`SELECT … FOR UPDATE`, as `links.py` `_insert` does), runs every removal and
  upsert (`INSERT … ON CONFLICT (entity_id, key) DO UPDATE`, `source = 'manual'`), counts
  keys, and raises before commit on any failure. `of_kind` joins `entities` on `kind`.

- [ ] **Step 5: `repoint_entity`** (`registry.py`): in the branch where both entities
  exist, move the gone entity's properties to the kept one, skipping keys the kept one has
  (`INSERT … SELECT … ON CONFLICT DO NOTHING`), before the gone entity is deleted. The
  rename branch needs nothing (properties follow `entity_id`).

- [ ] **Step 6: `EntitiesComponent.properties`**, as `links` is.

- [ ] **Step 7: Run the tests**, then `uv run ruff check`, `uv run ruff format --check` and
  `uv run mypy` on the changed files. Expected: PASS, clean.

- [ ] **Step 8: Commit** `feat(entities): key-value properties on any entity (#7026)`.

### Task 2: Routes

**Files:**
- Create: `backend/scadbuddy/api/entity_properties.py`
- Modify: `backend/scadbuddy/entities/problems.py` (`bad_property_value`, `bad_property_schema`)
- Test: `backend/tests/api/test_entity_properties.py`

**Interfaces:**
- Consumes: Task 1's store and models; phase 1's `parsed`, `unknown_kind`, `entity_not_found`,
  `KINDS`, `EntitiesDep`, `current_actor`.
- Produces (paths under `/api/v1`):
  - `GET /entities/{ref}/properties` → `EntityPropertyList {properties: list[EntityProperty]}`;
    404 `entity_not_found`, 422 `bad_entity_ref` / `unknown_entity_kind`.
  - `PATCH /entities/{ref}/properties` body `PropertyPatch` → `EntityPropertyList`; 404 as
    above; 422 `bad_property_value` (detail names the key and the reason).
  - `GET /entity-kinds/{kind}/properties` → `KindProperties {properties: dict[str, dict[str, PropertyValue]]}`
    (every entity of the kind that has any); 422 `unknown_entity_kind`.
  - `GET /entity-kinds/{kind}/property-schema` → `KindPropertySchema {kind, schema, keys_in_use: list[{key, count}]}`.
  - `PUT /entity-kinds/{kind}/property-schema` body `{schema: object}` → `KindPropertySchema`;
    422 `bad_property_schema`.

- [ ] **Step 1: Write the failing tests** with the app client the phase-1
  `tests/api/test_entity_links.py` uses: a round trip on `spool_type:<id>` (PATCH then GET);
  a rename; 422 `bad_property_value` with the key in `detail`; 404 on a missing spool;
  422 `unknown_entity_kind` on `print_archive:1` and on `/entity-kinds/nope/properties`;
  the kind read returns both spool types' maps; a restricting schema PUT is 422
  `bad_property_schema` and a GET after it returns the seeded schema; a good PUT then a
  PATCH of a now-typed key with a wrong type is 422.

- [ ] **Step 2: Run them to see them fail.**
  Run: `cd backend && uv run pytest tests/api/test_entity_properties.py -q` — FAIL (404 routes).

- [ ] **Step 3: Implement the routes** in the style of `api/entity_links.py` (store calls in
  `asyncio.to_thread`, `summary` and `description` on each route naming the rules of Global
  Constraints, since the agent and the API diff read them). No `from __future__ import annotations`.

- [ ] **Step 4: Run the tests and the linters.** Expected: PASS, clean. Also run
  `uv run pytest tests/auth -q`: the new GETs need no `SCRIPTLESS_GETS` entry (they are JSON
  reads), and the route-coverage tests there must still pass.

- [ ] **Step 5: Commit** `feat(entities): property routes (#7026)`.

### Task 3: Frontend panel and search

**Files:**
- Create: `frontend/src/components/entities/PropertiesPanel.tsx` (+ `.test.tsx`)
- Modify: `frontend/src/components/entities/api.ts`, `frontend/src/mocks/features/entities.ts`
- Modify: `frontend/src/pages/filament/SpoolPage.tsx`, `frontend/src/pages/filament/TypePage.tsx`
  (the panel beside `LinksPanel`)
- Modify: `frontend/src/lib/filamentSearch.ts` (+ test), `frontend/src/pages/filament/TypesTab.tsx`,
  `frontend/src/components/filament/spoolsQuery.ts`, `frontend/src/components/filament/FilamentQuickFind.tsx`

**Interfaces:**
- Consumes: Task 2's routes (types from the generated `schema.d.ts`; run `pnpm gen:api` first).
- Produces: `PropertiesPanel({ entity }: { entity: string })`; `entityApi.properties(entity)`,
  `entityApi.setProperties(entity, set)`, `entityApi.kindProperties(kind)`,
  `entityApi.propertySchema(kind)`; in `filamentSearch.ts`, `propertyTexts(values:
  Record<string, PropertyValue> | undefined): string[]` (strings as they are, list items,
  numbers and booleans as text), and a `properties?: string[]` field on the spool and type
  search extras that `spoolSearchDoc` / `typeSearchDoc` add to `texts`.

- [ ] **Step 1: Load `frontend-design`.** The panel is a compact key/value table: each row
  shows the key, the value (a list as chips) and the schema's description as a hint; edit
  in place; a key field with a `datalist` of the schema's keys and `keys_in_use`; a value
  type picker (text, number, yes/no, list) defaulted from the schema's `type` for a known
  key; rename is editing the key (one PATCH: old `null`, new value). A 422's `detail` shows
  under the row.

- [ ] **Step 2: Write the failing tests:** `PropertiesPanel.test.tsx` (lists the mock's
  properties; adds one and sends one PATCH; a rename sends `{old: null, new: value}`; a
  422 shows its detail); `filamentSearch.test.ts` (`propertyTexts` of
  `{gtin: ["0123","0456"], td: 1.8, ok: true}` is `["0123","0456","1.8","true"]`; a type
  whose properties hold GTIN `0456` matches the search `0456`). Mocks: `GET`/`PATCH`
  `/entities/:ref/properties`, `GET /entity-kinds/:kind/properties`, `GET`/`PUT`
  `/entity-kinds/:kind/property-schema`, with the seeded schemas.

- [ ] **Step 3: Implement**, then place the panel on both pages, and have the types tab,
  the spools query and the quick find load `kindProperties('spool_type')` /
  `kindProperties('spool')` beside the SKUs they load today; a failed read leaves the map
  empty (as `loadTypeSkus` failure does).

- [ ] **Step 4: Run** `pnpm test src/components/entities src/lib/filamentSearch.test.ts
  src/pages/filament` and `pnpm typecheck` and `pnpm lint`. Expected: PASS.

- [ ] **Step 5: Commit** `feat(filament): properties panel and property search (#7026)`.

### Task 4: Agent tools

**Files:**
- Modify: `agent/src/tools/entities.ts` (four tools), `agent/src/tools/filament.ts`
  (`properties` input on the two upserts; property values in the two list searches),
  `agent/src/tools/filamentSearch.ts` (`propertyTexts`, the extras field)
- Modify: `agent/src/sessions/touched.ts`, and the coverage list the phase-1 tools used
  (`coverage.ts` `NOT_A_TOOL` only if a route is not a tool)
- Test: `agent/test/entityTools.test.ts`, the filament tool tests that cover the upserts and
  list search (find them with `grep -l filament_type_upsert agent/test`)

**Interfaces:**
- Consumes: Task 2's routes.
- Produces tools:
  - `entity_property_list({entity})` read → the list.
  - `entity_property_set({entity, set: Record<string, value | null>})` write → the list.
  - `entity_property_schema_get({kind})` read → schema and keys in use.
  - `entity_property_schema_set({kind, schema})` write.
  - `filament_type_upsert` and `filament_spool_upsert` gain `properties?: Record<string,
    value | null>`: after the type or spool write succeeds, one PATCH on its entity; the
    result carries `properties`. If the PATCH fails, the error says the type or spool was
    saved and the properties were not, with the API's detail.
  - `filament_type_list` / `filament_spool_list` `q` also matches property values (one
    `GET /entity-kinds/{kind}/properties`; a failed read leaves them out, as the SKUs).

- [ ] **Step 1: Write the failing tests** (the msw/backend stub style of
  `entityTools.test.ts`): each new tool's route and risk; a rename through
  `entity_property_set`; an upsert with `properties` sends the filament write then the
  PATCH; an upsert whose PATCH answers 422 reports a saved type and the detail; a list
  search for a GTIN in a type's list finds it; a 500 on the property read still lists.

- [ ] **Step 2: Run them to see them fail.**
  Run: `cd agent && pnpm exec vitest run test/entityTools.test.ts <filament test files>` — FAIL.

- [ ] **Step 3: Implement.** Descriptions name the key rule, the value shapes, `null`
  removes, rename is one call, and the well-known keys as suggestions.

- [ ] **Step 4: Run** the tests, `pnpm typecheck`, `pnpm lint`, and the tool-coverage test
  (`pnpm exec vitest run test/coverage*.test.ts` or the file `coverage.ts` is tested by).
  Expected: PASS.

- [ ] **Step 5: Commit** `feat(agent): property tools and properties on the filament upserts (#7026)`.

### Task 5: Docs

**Files:**
- Modify: `docs/features/spools-and-filament.md` (properties on types and spools, search),
  `docs/architecture/data.md` (Entities row: `entity_properties`, `entity_property_schemas`,
  the new migration; recount the migration files if the page states a count),
  `backend/CLAUDE.md` (the `entities/` layout line), `docs/features/orders-and-quotes.md`
  only if it lists the entity route families.

- [ ] **Step 1: Edit**, each fact with its source (file and line, or #7026).
- [ ] **Step 2: Commit** `docs: entity properties (#7026)`.
