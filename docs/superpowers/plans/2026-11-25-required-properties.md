# Defined and required properties Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A kind's defined keys are fields of each row's form, a key can be required, and a row is
checked when it is created or saved through its own route (#7703).

**Architecture:** One module, `backend/scadbuddy/entities/required.py`, decides completeness
(`missing`, `invalid`) from a schema and a row's values. The create and save routes of spools,
spool types, catalog rows and items take an optional `properties` patch, write the row and its
properties in one transaction, and call the check before commit. Reads add
`missing_properties`/`invalid_properties`. The frontend renders defined keys as form fields,
the key table gets a Required box, and lists get an Incomplete mark and filter.

**Tech Stack:** FastAPI, psycopg 3, jsonschema (Draft 2020-12), React + vitest + msw, the
agent's zod tools.

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.3b (with
§6.3a, §6.4, D18, phase 1c).


**Status (2026-11-25):** PR 1 ships Tasks 1–5 (spools, spool types, catalogs). Task 6
(items and item kinds) waits for the inventory session's base-kind design (epic #6949,
its comment on #7703); materials and subtypes later become item kinds, so their checks
move to the kind schema then.
## Global Constraints

- Kinds that check (`REQUIRED_KINDS`): `spool`, `spool_type`, `material`, `subtype`, `brand`,
  `category`, `effect`, `insert_material`, `insert_type`, `colour`, `location_kind`, and
  `item` (through its item kind's `attribute_schema`). Any other kind's schema with `required`
  is 422 `bad_property_schema`.
- Refusal: 422, problem code `missing_required_properties`, body fields `missing: list[str]`
  and `invalid: list[{key, reason}]`, every key at once. A value the caller sends that does
  not fit stays 422 `bad_property_value`.
- No value: a missing key, a string that is empty or only whitespace, an empty list. `false`
  and `0` are values. Undefined keys never make a row incomplete.
- Exempt: a `PATCH` that sends only `archived`, `sort_order` or both; every route not named in
  the spec table (moves, weighings, bulk update, CSV import, invoice confirm, syncs, imports,
  `/parts`). `create_on`/`update_on` used by those stay unchecked.
- Archived rows never read incomplete.
- Properties sent on a checked create or save are `source = manual`.
- `PATCH /entities/{ref}/properties` refuses removing or emptying a required key that has a
  value (422 `missing_required_properties`, that key in `missing`); missing keys do not refuse it.
- A kind without `required` behaves exactly as today.
- Migrations: none (completeness is computed on read).

## Review Focus

- A required key whose stored value no longer fits after its subschema changed: the row reads
  `invalid`, and its next form save is refused naming it (Task 3 test).
- Archiving or restoring an incomplete spool or type through `PATCH {archived}` still works
  (Tasks 3 and 4 tests).
- A bulk create of N spools with a required key missing writes no spool (Task 3 test).
- An item kind requiring a key it only inherits; then the ancestor drops that key: the
  ancestor's update is refused naming the child (Task 6 test).
- A spool form save that changes only the name on a spool whose required key is set by a
  sync passes; one whose key is empty is refused (Task 3 test).

---

### Task 1: Schema `required` and completeness

**Files:**
- Create: `backend/scadbuddy/entities/required.py`
- Modify: `backend/scadbuddy/entities/properties.py` (`TOP_KEYWORDS`, `check_schema`, `set_schema`)
- Test: `backend/tests/entities/test_required.py`

**Interfaces:**
- Produces:
  - `REQUIRED_KINDS: frozenset[str]` (Global Constraints list).
  - `check_schema(schema, *, allow_required: bool = False) -> None`: `required` is a list of
    distinct strings, each in `properties`, else `BadPropertySchema`; with `allow_required`
    false, any `required` is `BadPropertySchema("kind does not check required keys yet")`.
  - `@dataclass(frozen=True) class Completeness: missing: tuple[str, ...]; invalid: tuple[tuple[str, str], ...]` with `.complete -> bool`.
  - `completeness(schema: dict, values: Mapping[str, PropertyValue]) -> Completeness`
    (missing in `required` order; invalid sorted by key, reason from `best_match`).
  - `class MissingRequiredProperties(ValueError)` with `.missing`, `.invalid`.
  - `def required_problem(exc) -> ApiError` (in `entities/problems.py`): 422, code
    `missing_required_properties`, extras `missing`, `invalid` (list of `{key, reason}`).

- [ ] **Step 1: Write failing tests** in `test_required.py`:
  `test_required_must_name_defined_keys` (`{"required":["x"]}` with no `x` → `BadPropertySchema`),
  `test_required_refused_for_a_kind_that_does_not_check` (`check_schema(.., allow_required=False)`),
  `test_completeness_no_value_rules` (schema requires `a,b,c,d,e`; values `{"a":"", "b":"  ", "c":[], "d":False, "e":0}` → missing `("a","b","c")`),
  `test_completeness_reports_ill_fitting_stored_value` (`{"n":{"type":"number"}}`, values `{"n":"x"}` → invalid `(("n", …),)`),
  `test_set_schema_required_by_kind` (`set_schema("spool_type", …required)` ok; `set_schema("supplier", …required)` → `BadPropertySchema`).
- [ ] **Step 2:** `uv run pytest tests/entities/test_required.py -n0` → FAIL (import error).
- [ ] **Step 3:** Implement. `set_schema` passes `allow_required=kind in REQUIRED_KINDS`.
- [ ] **Step 4:** Run the file plus `tests/entities tests/api/test_entity_properties.py` → PASS.
- [ ] **Step 5:** Commit `feat(entities): a schema may require keys (spec 6.3b)`.

### Task 2: In-transaction property writes and the row check

**Files:**
- Modify: `backend/scadbuddy/entities/properties.py` (`EntityPropertyStore`)
- Modify: `backend/scadbuddy/entities/required.py`
- Modify: `backend/scadbuddy/api/entity_properties.py` (map `MissingRequiredProperties`)
- Test: `backend/tests/entities/test_required.py`, `backend/tests/api/test_entity_properties.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `EntityPropertyStore.apply_on(conn, ref, patch: PropertyPatch, by: Actor) -> list[EntityProperty]` — today's `apply` body on the caller's connection; `apply` opens a transaction and calls it.
  - `check_row(conn, ref: EntityRef) -> None` in `required.py`: reads `schema_for(conn, ref)` and the stored values, raises `MissingRequiredProperties` unless complete.
  - `completeness_of_kind(conn, kind: str, refs: Iterable[str]) -> dict[str, Completeness]` (one schema read and one properties query; items group by kind_id via `merged_schema`).
  - `PropertyPatch` keeps its shape; `apply`/`apply_on` refuse a patch that removes or empties a required key the entity has a value for.

- [ ] **Step 1: Failing tests:** `test_check_row_names_every_missing_key`,
  `test_patch_refuses_removing_a_required_value` (API: schema requires `mpn`; set `mpn`; `PATCH {mpn: null}` → 422 `missing_required_properties`, `missing == ["mpn"]`),
  `test_patch_may_fill_one_required_key_at_a_time` (requires `a`,`b`; `PATCH {a:"1"}` → 200),
  `test_completeness_of_kind_batches` (two spool types, one complete).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `tests/entities tests/api/test_entity_properties.py` → PASS.
- [ ] **Step 5:** Commit.

### Task 3: Spools

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolCreate`, `SpoolBulkCreate`, `SpoolPatch` gain `properties: dict[str, JsonValue] | None = None`; `Spool` gains `missing_properties: list[str] = []`, `invalid_properties: list[str] = []`)
- Modify: `backend/scadbuddy/filament/spools.py` (`create`, `update`, list/get/detail fill the two lists)
- Modify: `backend/scadbuddy/api/filament.py` (`create_spool`, `bulk_create_spools`, `patch_spool`)
- Test: `backend/tests/api/test_filament_required_properties.py` (new)

**Interfaces:**
- Consumes: Task 2 `apply_on`, `check_row`, `completeness_of_kind`, `required_problem`.
- Produces: `FilamentSpoolStore.create(body, *, actor, checked: bool = False)`, `bulk_on(conn, body, *, actor, checked: bool = False)`, `update(spool_id, patch, *, actor, checked: bool = False)`; the three routes pass `checked=True`. `create_on`/`update_on` stay unchecked. `bulk-update` drops `properties` from its patch (422 if sent).

- [ ] **Step 1: Failing tests** (schema `spool` requires `lot_number`):
  `test_create_without_required_is_refused_and_writes_nothing` (count before == after),
  `test_create_with_properties_is_one_write` (`properties: {lot_number: "L1"}` → 201, properties listed),
  `test_bulk_of_three_without_required_writes_none`,
  `test_patch_name_only_refused_when_incomplete_and_passes_when_complete`,
  `test_patch_archived_only_is_never_checked`,
  `test_bulk_update_and_moves_are_not_checked`,
  `test_list_marks_incomplete_but_not_archived` (`missing_properties == ["lot_number"]`; archived row `[]`),
  `test_invalid_stored_value_is_reported_and_blocks_a_form_save`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement: write the row, `apply_on` the patch with the row's ref, then `check_row`, all in the row's transaction; exempt a patch whose set fields ⊆ `{"archived","sort_order"}`.
- [ ] **Step 4:** Run the new file plus `tests/api/test_filament_spools*.py tests/filament` → PASS.
- [ ] **Step 5:** Commit.

### Task 4: Spool types, with a cost-only route

**Files:**
- Modify: `filament/models.py` (`SpoolTypeCreate`, `SpoolTypePatch` gain `properties`; `SpoolType` gains the two lists), `filament/types.py`, `api/filament.py` (`create_type`, `patch_type`; new `PUT /types/{type_id}/cost-per-kg` body `{cost_per_kg}` → `types.update(..., checked=False)`)
- Test: `backend/tests/api/test_filament_required_properties.py`

**Interfaces:**
- Produces: `SpoolTypeStore.create(body, *, checked=False)`, `update(type_id, patch, *, actor, checked=False)`. `create_on` (invoice confirm, proposals) unchecked. Route `PUT /filament/types/{type_id}/cost-per-kg` answers `SpoolType`.

- [ ] **Step 1: Failing tests** mirroring Task 3 for `spool_type` requiring `mpn`, plus `test_cost_route_is_not_checked` and `test_invoice_confirm_makes_an_incomplete_type`.
- [ ] **Step 2–4:** Run → FAIL; implement; run with `tests/api/test_filament_types*.py tests/filament/test_invoices*.py` → PASS.
- [ ] **Step 5:** Commit.

### Task 5: Catalog rows

**Files:**
- Modify: `filament/catalog.py` (`create(kind, body, *, properties=None, by=None, checked=False)`, `update(...)` same), `api/filament.py` (`create_catalog_row`, `patch_catalog_row`: pop `properties` from the raw body before `_body`; add the two lists to every row the routes and `list` answer)
- Test: `backend/tests/api/test_filament_required_properties.py`

**Interfaces:**
- Produces: catalog row JSON carries `missing_properties`, `invalid_properties`. Entity kind from `KIND_OF` map `catalog kind → entity kind` defined in `entities/registry.py` as `CATALOG_ENTITY_KINDS: dict[str, str]` (reuse in the frontend test only by value).
- Rows added by `resolve_or_add` (on the way to another row) stay unchecked.

- [ ] **Step 1: Failing tests:** `insert_type` requiring `part_number`: create refused / created with `properties`; `PATCH {sort_order}` and `{archived}` exempt; `PATCH {name}` refused while incomplete; `test_resolve_or_add_material_is_unchecked`.
- [ ] **Step 2–4:** FAIL; implement; run with `tests/api/test_filament_catalog*.py tests/filament/test_catalog*.py` → PASS.
- [ ] **Step 5:** Commit.

### Task 6: Items and item kinds

**Files:**
- Modify: `inventory/models.py` (`ItemInput`, `InventoryItemPatch` gain `properties`; `Item` gains the two lists), `inventory/items.py`, `inventory/kinds.py`, `api/inventory.py`
- Test: `backend/tests/api/test_inventory_required_properties.py` (new)

**Interfaces:**
- Produces: `merged_schema` also returns `required` (union down the tree, ancestor first, no duplicates). `KindStore.create/update` validate the kind's own `required` against its merged `properties` and, on any update or reparent, every descendant's; failure → `BadPropertySchema("kind '<id>' would require '<key>', which it no longer defines")`. `ItemStore.create(body, by, *, checked=False)`, `update(...)` likewise; routes pass `checked=True`; the Bambu import and `/parts` aliases do not.

- [ ] **Step 1: Failing tests:** `test_child_requires_inherited_key`, `test_ancestor_dropping_a_required_key_is_refused_naming_child`, `test_reparent_that_drops_a_required_key_is_refused`, `test_item_create_refused_until_complete`, `test_parts_alias_is_not_checked`.
- [ ] **Step 2–4:** FAIL; implement; run with `tests/inventory tests/api/test_inventory*.py` → PASS.
- [ ] **Step 5:** Commit; open PR 1 (backend, Tasks 1–6). Docs: `docs/features/spools-and-filament.md` "Properties" and `docs/features/inventory.md` get the rule in a paragraph each, in the same PR.

### Task 7: Agent tools

**Files:**
- Modify: `agent/src/tools/filament.ts` (`filament_type_upsert`, `filament_spool_upsert`, `filament_catalog_upsert`), `agent/src/tools/inventory.ts` (`inventory_item_upsert`, `inventory_kind_upsert` description), `agent/src/tools/entities.ts` (`entity_property_schema_set` description, `PROPERTY_RULES`)
- Test: `agent/test/filamentTools*.test.ts`, `agent/test/inventoryTools*.test.ts`, `agent/test/entityTools.test.ts`

**Interfaces:**
- Upserts send `properties` in the row body (one request); an edit with only `properties` keeps the `PATCH /entities/{ref}/properties` path. A 422 `missing_required_properties` becomes a `ToolError` whose summary lists `missing` and `invalid` keys.

- [ ] **Step 1: Failing tests:** `sends properties with the row on create` (one POST, body has `properties`), `an edit with only properties uses the property patch`, `names each missing key on a refusal`, `catalog upsert takes properties`.
- [ ] **Step 2–4:** `pnpm exec vitest run test/<files>` FAIL; implement; PASS; `pnpm typecheck`.
- [ ] **Step 5:** Commit; PR 2.

### Task 8: Key table Required box

**Files:**
- Modify: `frontend/src/components/entities/propertyKeys.ts` (`KeyRow.required: boolean`; `rowsOf` reads `schema.required`; `schemaOf` writes `required` when any row is required, deletes it otherwise), `PropertyKeyTable.tsx` (a checkbox column, `aria-label="Key N is required"`), `KindKeysForm.tsx` (before save, for each newly required key show "N rows lack it" from `entityApi.kindProperties(kind)`; the item kinds form shows it from `keys_in_use`), `frontend/src/mocks/features/entities.ts` (schema `required` check and `missing_required_properties` refusals mirroring Global Constraints)
- Test: `PropertiesPanel.test.tsx`, `KindsPage.test.tsx`

- [ ] **Step 1: Failing tests:** `marks a key required and saves required`, `says how many rows lack a key before it is saved as required`, `a kind that does not check has no Required box` (supplier).
- [ ] **Step 2–4:** FAIL; implement (Required column only when `REQUIRED_KINDS.has(kind)`, a frontend copy of the backend set in `components/entities/api.ts`); PASS.
- [ ] **Step 5:** Commit.

### Task 9: Defined keys as form fields

**Files:**
- Create: `frontend/src/components/entities/DefinedFields.tsx`
- Modify: `pages/filament/SpoolPage.tsx` (form at :963/:1088), `pages/filament/TypePage.tsx`, `components/filament/CatalogList.tsx`, `pages/inventory/ItemPage.tsx`, `components/entities/PropertiesPanel.tsx` (prop `hideKeys?: string[]`), `api/filament.ts`, `pages/inventory/api.ts` (bodies carry `properties`)
- Test: `components/entities/DefinedFields.test.tsx`, `pages/filament/SpoolPage.test.tsx`, `TypePage.test.tsx`, `CatalogTab.test.tsx`, `pages/inventory/ItemPage.test.tsx`

**Interfaces:**
- Produces: `DefinedFields({ schema, values, onChange, errors })` where `values: Record<string, PropertyValue | undefined>`, `errors: { missing: string[]; invalid: {key: string; reason: string}[] }`; order: required keys first, then by key; control by type (`enum` → select, boolean → checkbox, array → item list, number → number input); label `title ?? key`, `*` when required. `propertiesPatch(schema, before, draft) -> Record<string, PropertyValue | null>`: an emptied optional field → `null`, an untouched one omitted.
- The form's Save sends `properties` with the row; a 422 `missing_required_properties` marks each field.

- [ ] **Step 1: Failing tests:** `renders required first then by key`, `an emptied optional field removes its key`, `a refused save marks each missing field and keeps the draft`, and per page one `saves the row and its defined properties with one request`.
- [ ] **Step 2–4:** FAIL; implement; PASS.
- [ ] **Step 5:** Commit.

### Task 10: Incomplete mark and filter

**Files:**
- Modify: `pages/filament/SpoolsTab.tsx`, `TypesTab.tsx`, `components/filament/CatalogList.tsx`, `pages/inventory/InventoryPage.tsx`; create `components/entities/IncompleteMark.tsx` (`Missing required: a, b` text, nothing when both lists are empty)
- Test: the matching `*.test.tsx`

- [ ] **Step 1: Failing tests:** each list shows "Missing required: lot_number" and an `Incomplete` filter (checkbox, `aria-label="Only incomplete"`) narrows to those rows.
- [ ] **Step 2–4:** FAIL; implement; PASS.
- [ ] **Step 5:** Commit.

### Task 11: One-click creates and one-field actions

**Files:**
- Modify: `components/filament/makeType.ts` (`linkTypeToBook` → `PUT …/cost-per-kg`; `createTypeFromSpool` final step → `bulkUpdateSpools([id], …)`), `components/filament/BulkBar.tsx` ("Set type" → `bulkUpdateSpools`), `pages/filament/SpoolPage.tsx` ("Reset to type" → `bulkUpdateSpools`), `FilamentsEditor.tsx` and the "Create type from this spool" button (open a dialog with `DefinedFields` for `spool_type` when its schema has `required`, else go straight on), `components/filament/scan/NewTag.tsx` (`DefinedFields` for `spool`)
- Test: `makeType.test.ts`, `BulkBar.test.tsx`, `SpoolPage.test.tsx`, `FilamentsEditor*.test.tsx`, `scan/NewTag.test.tsx`

- [ ] **Step 1: Failing tests:** `set type works on an incomplete spool`, `reset to type works on an incomplete spool`, `link type to book uses the cost route`, `create type from spool asks for the required keys and sends them`, `a new tag spool shows the required fields`.
- [ ] **Step 2–4:** FAIL; implement; PASS; `pnpm typecheck && pnpm lint`.
- [ ] **Step 5:** Commit; docs in `docs/features/spools-and-filament.md` "Properties" (UI paragraph); PR 3.
