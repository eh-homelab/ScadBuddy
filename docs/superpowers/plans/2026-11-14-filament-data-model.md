# Filament data model (#6261) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ScadBuddy holds its own filament inventory (catalogs, spool types,
spools, tags, ledger, locations), filled from Bambuddy on day one, and the
existing native-spool surface runs on it.

**Architecture:** New tables in the CRUD service's database, a `filament`
backend package with one synchronous psycopg store per concern, public routes
under `/api/v1/filament/*` with agent tools, and a BB→SB pull that runs in the
existing `SpoolRefresh` schedule. The old `native_spools` code becomes an
adapter over the new tables, so the print dialog, the overlay and the print
worker keep working unchanged until #6262 switches the readers.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync pools, `dict_row`),
pydantic v2, Temporal (Python SDK), Postgres 18, TypeScript agent tools (zod,
msw tests).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md`
(§3, §5.1-5.6, §8.1, §12 phase 1 row #6261). Epic #6260.

## Global Constraints

- Only the CRUD service has SQL access. A worker reaches new data through a
  `crud_router(...)` route with a service credential (`crud/guard.py`), never a
  new pool or grant (CLAUDE.md "Database access (#5295)").
- Pools come only from `core/pg_pool.py`; stores take a
  `ConnectionPool[Connection[DictRow]]` and are synchronous; async callers use
  `asyncio.to_thread`.
- Migrations: new files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`;
  never edit a merged file. Timestamps `timestamptz`; enums are `text` with
  `CHECK (... IN (...))`; soft delete is `archived_at` with partial unique
  indexes `WHERE archived_at IS NULL`.
- Colour is `#RRGGBBAA` everywhere (`CHECK (x ~ '^#[0-9A-F]{8}$')`, stored
  upper-case). Input `#RRGGBB` is accepted and stored with alpha `FF`.
- Nothing a person sees as a vocabulary is a code enum (spec §5.1). Code-only
  enums: event kinds, event sources, tag kinds, sync directions.
- Every new public `/api/v1` route gets an agent tool or a `NOT_A_TOOL` entry
  (`agent/src/tools/coverage.ts`, checked by `agent/test/coverage.test.ts`).
- Every route sets `summary=` and `description=`.
- `SpoolRefresh` changes go behind `workflow.patched(...)`
  (`tests/test_spool_refresh_replay.py`).
- Generated API files are never committed (`pnpm gen:api`).
- Commands: `cd backend && uv run --frozen pytest <files>` with
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`;
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`;
  `cd agent && pnpm test <file>` and `pnpm typecheck`.

## Decisions this plan makes (spec §13 left them to the builder)

- **Price book:** `filaments` stays the price book table and keeps its text
  ids. Each spool type has `filament_id text REFERENCES filaments(id)`. Creating
  or editing a type's identity or default cost upserts its `filaments` row by
  identity (`orders/store.py` `existing_filament`, `insert_filament`,
  `update_filament`). Quotes and job cost are untouched.
- **Overrides:** nullable columns on `spools` with the same names as the type's
  columns; `NULL` means "inherit". Presets and K values override per row in
  `spool_presets` / `spool_k_values`.
- **Material defaults:** a type's nullable value falls back to its subtype's,
  then its material's (spec §5.1). Resolution is one function, `effective()`.
- **Price book colour stays `#RRGGBB`, with its alpha beside it.** #6209
  landed in #6211 before this plan: `filaments.colour` stays `#RRGGBB` (quotes
  and the storefront read it) beside `alpha`, `extra_colours` and `effect`
  (`20261114T1607Z_filaments_alpha_effect.sql`). A type's book row takes the
  type's alpha, stops and effect, and the book's identity holds the alpha, so an
  opaque and a translucent type of one hue get two rows. This PR changes the
  spec's §12 note on #6209 to say so.
- **Native spools:** the tables `native_spools` and `native_spool_uses` are
  copied into the new tables and no longer written. `NativeSpoolStore` keeps its
  public signatures and reads and writes the new tables. Dropping the old tables
  is #6262's job.

## Review Focus

1. A tag that is already bound to another spool is bound to a new one: the old
   binding closes, both spools get a ledger event, and nothing is refused
   (Task 6).
2. A person or an old row sends `#rrggbb` (lower case, no alpha): it is stored
   as `#RRGGBBFF` and compares equal to the same colour sent as RGBA (Task 2).
3. The BB pull meets a BB spool whose tag an SB spool already holds: it links
   to that SB spool instead of creating a second one (Task 9).
4. A spool that has print events is deleted: the request is refused with 409
   and the spool stays; archive works (Task 6).
5. A catalog row is archived while types still reference it: the types keep
   reading it, and lists for new choices leave it out (Task 4).

---

## File structure

```
backend/scadbuddy/migrations/
  <ts>_filament_catalogs.sql          Task 1
  <ts>_filament_spools.sql            Task 1
  <ts>_filament_from_native.sql       Task 3
backend/scadbuddy/filament/
  __init__.py
  colour.py        RGBA parsing and comparison                  Task 2
  models.py        pydantic models for every table              Tasks 2, 4-8
  catalog.py       CatalogStore                                 Task 4
  types.py         SpoolTypeStore, effective()                  Task 5
  spools.py        SpoolStore (instances, tags, delete rule)    Task 6
  ledger.py        events, grams_left()                         Task 6
  locations.py     LocationStore                                Task 7
  bambuddy_pull.py BB→SB import and pull                        Task 9
  component.py     FILAMENT Key, Filament dataclass             Task 4
backend/scadbuddy/api/filament.py     public routes             Tasks 4-8
backend/scadbuddy/api/crud_print.py   pull routes for the worker Task 9
backend/scadbuddy/spools/native.py    adapter over new tables   Task 8
backend/scadbuddy/bambuddy/client.py, models.py                  Task 9
agent/src/tools/filament.ts, agent/test/filament.test.ts         Tasks 4-8
backend/tests/filament/                                          all tasks
```

---

### Task 1: Migrations for catalogs, types, spools, tags, ledger, locations

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_catalogs.sql`
- Create: `backend/scadbuddy/migrations/<ts+1>_filament_spools.sql`
- Test: `backend/tests/filament/test_migrations.py`

**Interfaces:**
- Produces the tables below; every later task reads them.

Catalog tables (`_filament_catalogs.sql`), each with `id uuid PK DEFAULT
gen_random_uuid()`, `name text NOT NULL`, `sort_order integer NOT NULL DEFAULT
0`, `archived_at timestamptz`, `created_at/updated_at timestamptz NOT NULL
DEFAULT now()`, and a unique index on `upper(name)` `WHERE archived_at IS NULL`
(scoped by parent where there is one):

| Table | Extra columns |
|---|---|
| `filament_materials` | `family text`, `density_g_cm3 numeric(6,3) CHECK > 0`, `nozzle_temp_min/max integer`, `bed_temp_min/max integer`, `drying_temp_c integer`, `drying_hours numeric(5,2)`, `needs_enclosure/abrasive/hygroscopic/flexible/hardened_nozzle boolean NOT NULL DEFAULT false`, `note text` |
| `filament_subtypes` | `material_id uuid REFERENCES filament_materials`, the same nullable value columns as materials |
| `filament_brands`, `filament_categories`, `filament_effects` | none |
| `filament_insert_materials` | none |
| `filament_insert_types` | `empty_weight_g integer NOT NULL CHECK > 0`, `insert_material_id uuid REFERENCES filament_insert_materials`, `note text` |
| `filament_colours` | `manufacturer text`, `rgba text NOT NULL CHECK (rgba ~ '^#[0-9A-F]{8}$')`, `material_id uuid REFERENCES filament_materials`, `extra_colours text[]`, `effect_id uuid REFERENCES filament_effects` |
| `filament_location_kinds` | `dry_storage boolean NOT NULL DEFAULT false`, `ams boolean NOT NULL DEFAULT false` |
| `filament_locations` | `kind_id uuid NOT NULL REFERENCES filament_location_kinds`, `parent_id uuid REFERENCES filament_locations`, `identifier text`, `capacity integer CHECK > 0`, `printer_id` (same type as `printers.id`) `REFERENCES printers ON DELETE CASCADE`, `ams_id integer`, `tray_id integer`, `note text`; unique `(printer_id, ams_id, tray_id) WHERE printer_id IS NOT NULL` |

Seed rows (same file): materials PLA, PETG, PETG-CF, ABS, ASA, TPU, PA, PC
(PETG, PA, TPU hygroscopic; PETG-CF, PA-CF abrasive and hardened_nozzle; ABS,
ASA, PC needs_enclosure; TPU flexible); subtypes Basic, Matte, Silk, HF,
Translucent, Dual Color, Tri Color, Plus; effects Sparkle, Wood, Marble, Glow,
Matte, Silk, Galaxy, Rainbow, Metal, Translucent, Gradient, Dual Color, Tri
Color, Multicolor (BB's list); insert materials Cardboard, Plastic, Bambu
reusable; location kinds Room, Shelf, Bin, Dry box (dry_storage), AMS (ams),
Drawer.

Spool tables (`_filament_spools.sql`):

| Table | Columns |
|---|---|
| `filament_spool_types` | `id uuid PK`, `code text NOT NULL` (unique `upper(code)` where unarchived), `brand_id`, `material_id NOT NULL`, `subtype_id`, `category_id`, `effect_id` (uuid FKs), `colour_name text`, `rgba text NOT NULL` (RGBA check), `extra_colours text[]`, `diameter_mm numeric(4,2) NOT NULL DEFAULT 1.75`, `density_g_cm3`, `nozzle_temp_min/max`, `bed_temp_min/max`, `label_weight_g integer NOT NULL CHECK > 0`, `insert_type_id uuid`, `low_stock_pct integer CHECK 0..100`, `cost_per_kg numeric(12,4) CHECK >= 0`, `filament_id text REFERENCES filaments(id)`, `note text`, `archived_at`, `created_at`, `updated_at` |
| `filament_spool_type_presets` | `type_id uuid NOT NULL REFERENCES filament_spool_types ON DELETE CASCADE`, `printer_model text NOT NULL`, `nozzle_diameter text NOT NULL`, `slicer_filament text NOT NULL`, `slicer_filament_name text`; PK `(type_id, printer_model, nozzle_diameter)` |
| `filament_spool_type_k_values` | `type_id`, `printer_id`, `extruder integer NOT NULL`, `nozzle_diameter text NOT NULL`, `nozzle_type text`, `k_value numeric(6,4) NOT NULL`, `name text`, `cali_idx integer`, `setting_id text`; PK `(type_id, printer_id, extruder, nozzle_diameter)` |
| `filament_spools` | `id uuid PK`, `number integer GENERATED ALWAYS AS IDENTITY UNIQUE`, `type_id uuid REFERENCES filament_spool_types` (nullable: "needs type"), every overridable type column as nullable (`brand_id`, `material_id`, `subtype_id`, `category_id`, `effect_id`, `colour_name`, `rgba`, `extra_colours`, `diameter_mm`, `density_g_cm3`, temps, `label_weight_g`, `insert_type_id`, `low_stock_pct`, `cost_per_kg`), `location_id uuid REFERENCES filament_locations`, `weight_locked boolean NOT NULL DEFAULT false`, `note text`, derived: `grams_left double precision`, `last_weighed_g`, `last_weighed_at`, `last_dried_at`, `last_dried_temp_c`, `last_dried_hours`, `last_used_at`; `archived_at`, `created_at`, `updated_at`. CHECK: `type_id IS NOT NULL OR material_id IS NOT NULL` |
| `filament_spool_presets`, `filament_spool_k_values` | as the type tables, keyed by `spool_id` |
| `filament_tags` | `id uuid PK`, `kind text NOT NULL CHECK IN ('bambu_mifare','ntag_openspool','ntag_opentag','other')`, `tag_uid text`, `tray_uuid text`, `raw_blob text`, `note text`, `created_at`; unique `tag_uid` and `tray_uuid` where not null; CHECK one of them not null |
| `filament_tag_bindings` | `id uuid PK`, `tag_id NOT NULL`, `spool_id NOT NULL REFERENCES filament_spools`, `override boolean NOT NULL DEFAULT false`, `bound_at timestamptz NOT NULL DEFAULT now()`, `unbound_at timestamptz`, `reason text`; unique `tag_id WHERE unbound_at IS NULL`; unique `spool_id WHERE unbound_at IS NULL` |
| `filament_spool_events` | `id uuid PK`, `spool_id NOT NULL REFERENCES filament_spools` (no cascade), `at timestamptz NOT NULL DEFAULT now()`, `kind text NOT NULL CHECK IN ('created','loaded','unloaded','moved','print_started','print_ended','weighed','ams_reading','dried','edited','tag_bound','tag_unbound','archived','restored','synced_from_bb')`, `source text CHECK IN ('ams_remain','scale','print_estimate','print_actual','person','bb')`, `printer_id`, `ams_id`, `tray_id`, `grams_before double precision`, `grams_after double precision`, `delta_g double precision`, `ref_kind text`, `ref_id text`, `actor text`, `data jsonb NOT NULL DEFAULT '{}'`; index `(spool_id, at)`; unique `(spool_id, ref_kind, ref_id, kind) WHERE ref_id IS NOT NULL` (idempotent recording) |
| `filament_bb_links` | `spool_id uuid PRIMARY KEY REFERENCES filament_spools`, `bb_spool_id integer NOT NULL UNIQUE`, `bb_updated_at timestamptz`, `pushed_hash text`, `linked_at` |

- [ ] **Step 1: Write the failing test** `test_the_filament_tables_and_seeds_exist(pg_pool)`: after `open_pg_pool`, `SELECT count(*) FROM filament_materials` is ≥ 8; a row in `filament_spool_types` with `rgba='#ff0000'` violates the check (lower case); `filament_spools` with neither `type_id` nor `material_id` violates the check; two open bindings of one tag raise `UniqueViolation`; two events with the same `(spool_id, ref_kind, ref_id, kind)` raise `UniqueViolation`.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament/test_migrations.py` — FAIL (no tables).
- [ ] **Step 3:** Write both migration files.
- [ ] **Step 4:** Run the test and `tests/test_pg_migrations.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): catalog, spool, tag and ledger tables (#6261)`.

### Task 2: Colour and models

**Files:**
- Create: `backend/scadbuddy/filament/__init__.py`, `colour.py`, `models.py`
- Test: `backend/tests/filament/test_colour.py`

**Interfaces:**
- Produces: `normalise_rgba(value: str) -> str` (accepts `RRGGBB`, `RRGGBBAA`, with or without `#`, any case; returns `#RRGGBBAA` upper; raises `ValueError` otherwise); `Rgba = Annotated[str, AfterValidator(normalise_rgba)]`; `is_translucent(rgba: str) -> bool` (alpha < `FF`).
- Produces pydantic models (`extra="forbid"` on inputs) named `<Thing>`, `<Thing>Create`, `<Thing>Patch` for: `Material`, `Subtype`, `Brand`, `Category`, `Effect`, `InsertMaterial`, `InsertType`, `Colour`, `LocationKind`, `Location`, `SpoolType`, `TypePreset`, `TypeKValue`, `Spool`, `Tag`, `TagBinding`, `SpoolEvent`. `Spool` output carries `effective: EffectiveValues` and `overridden: list[str]` (Task 5).

- [ ] **Step 1: Failing tests:** `normalise_rgba("ff8800") == "#FF8800FF"`; `normalise_rgba("#ff880080") == "#FF880080"`; `normalise_rgba("#FFF")` raises; `is_translucent("#FFFFFF80")` is true and `is_translucent("#FFFFFFFF")` false.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement `colour.py` and `models.py`.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 3: Copy native spools into the new tables

**Files:**
- Create: `backend/scadbuddy/migrations/<ts+2>_filament_from_native.sql`
- Test: `backend/tests/filament/test_from_native.py`

**Interfaces:**
- Consumes: Task 1 tables; old `native_spools`, `native_spool_uses`, `physical_spools`, `spool_record_links`.

Rules (spec §5.3, §5.5):
- Each `native_spools` row becomes a `filament_spools` row. The id is the
  physical spool's id when a `spool_record_links` row with provider `native`
  links them, else the native id. `material_id` comes from
  `filament_materials` by `upper(name)`, inserting the material if it is
  missing. `rgba` = `upper(colour) || 'FF'`. `label_weight_g`, `note`,
  `archived_at` copy; `type_id` is NULL.
- `grams_left` = `remaining_g`, and one `created` event with source `person`,
  `grams_after = remaining_g`.
- `tray_uuid` / `tag_uid` become a `filament_tags` row (kind `bambu_mifare`
  when `tray_uuid` is set, else `other`) and an open binding.
- A `physical_spools` row with no native record makes a tag only, no spool.
- Each `native_spool_uses` row becomes an event with `ref_kind='print_run'`,
  `ref_id = run_id || '/' || subtask_name || '/' || slot_id`: applied rows
  `kind='print_ended'`, `source='print_actual'`, `data={"basis":"sliced"}`;
  unapplied rows `kind='print_started'`, `source='print_estimate'`. `delta_g =
  -grams`.

- [ ] **Step 1: Failing test** (the `MIGRATIONS[:idx]` pattern of `tests/test_pg_migrations.py`): migrate to before the file; insert two native spools (one with `tray_uuid`, `#aabbcc`, 600 g left; one archived) and one applied and one unapplied use; migrate; assert two `filament_spools` rows with `rgba='#AABBCCFF'`, the tag and open binding, events `created`, `print_ended`/`print_actual` with `data->>'basis'='sliced'`, and `print_started`/`print_estimate`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Write the migration.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 4: Catalog store, component, routes and tools

**Files:**
- Create: `backend/scadbuddy/filament/catalog.py`, `component.py`, `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`, `agent/test/filament.test.ts`
- Modify: `agent/src/tools/index.ts` (spread `filamentTools` into `ALL_TOOLS`)
- Test: `backend/tests/filament/test_catalog.py`, `backend/tests/api/test_filament.py`

**Interfaces:**
- Produces: `CATALOGS: Mapping[str, CatalogSpec]` with keys `materials, subtypes, brands, categories, effects, insert-materials, insert-types, colours, location-kinds`; `CatalogStore(pool)` with `list(kind, *, include_archived=False) -> list[BaseModel]`, `create(kind, body) -> BaseModel`, `update(kind, id, patch) -> BaseModel | None`, `archive(kind, id, archived: bool) -> BaseModel | None`; `CatalogExistsError(ValueError)`.
- Produces: `@dataclass Filament(catalog: CatalogStore, types: SpoolTypeStore, spools: FilamentSpoolStore, locations: LocationStore)`, `FILAMENT: Key[Filament] = Key("filament")`, `FilamentDep` in `filament/component.py` (built from `core.settings_store.pool`).
- Produces routes on `APIRouter(prefix="/filament", tags=["filament"])`: `GET/POST /catalog/{kind}`, `PATCH /catalog/{kind}/{id}`; 404 for an unknown kind or id, 409 on `CatalogExistsError`.
- Produces agent tools `filament_catalog_list` (read), `filament_catalog_upsert` (write), with `routes:` naming each operation.

- [ ] **Step 1: Failing tests:** `test_each_catalog_lists_its_seeds(pg_pool)`; `test_an_archived_material_stays_readable_but_leaves_the_list` (Review Focus 5: archive a material a type uses; `list("materials")` omits it, `list(..., include_archived=True)` has it, and the type still reads its name); `test_a_second_material_with_the_same_name_is_refused` (409 through the API).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement the store, component, routes and tools. The catalog tool test (`agent/test/filament.test.ts`) follows `nativeSpools.test.ts` (msw `setupServer`, `runTool`).
- [ ] **Step 4:** Run backend tests, `cd agent && pnpm test filament coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit.

### Task 5: Spool types and effective values

**Files:**
- Create: `backend/scadbuddy/filament/types.py`
- Modify: `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`
- Test: `backend/tests/filament/test_types.py`

**Interfaces:**
- Produces: `SpoolTypeStore(pool)`: `list(*, include_archived=False)`, `get(id)`, `create(body: SpoolTypeCreate) -> SpoolType`, `update(id, patch) -> SpoolType | None`, `archive(id, archived)`, `set_presets(id, rows: list[TypePreset])`, `set_k_values(id, rows: list[TypeKValue])`, `match(*, code=None, brand, material, subtype, rgba, label_weight_g) -> SpoolType | None`.
- Produces: `effective(spool_row, type_row, subtype_row, material_row) -> EffectiveValues` — for each overridable field, the first non-null of spool, type, subtype, material; plus `overridden(spool_row) -> list[str]`.
- Price book: `create`/`update` call `orders/store.py` `existing_filament` then `insert_filament`/`update_filament` inside the same connection (`conn=`), and store the id in `filament_id`; a type with no `cost_per_kg` creates no price book row.
- Routes: `GET/POST /filament/types`, `GET/PATCH /filament/types/{id}`, `PUT /filament/types/{id}/presets`, `PUT /filament/types/{id}/k-values`. Tools: `filament_type_list`, `filament_type_get`, `filament_type_upsert`, `filament_type_set_presets`, `filament_type_set_k_values`.

- [ ] **Step 1: Failing tests:** `test_effective_takes_spool_then_type_then_subtype_then_material` (material density 1.24, type none, spool none → 1.24; type 1.27 → 1.27; spool 1.30 → 1.30 and `"density_g_cm3" in overridden`); `test_a_priced_type_writes_its_price_book_row` (create type with cost 19.99 → `orders.store.filament(type.filament_id).cost_per_kg == "19.9900"`; patch cost → the same row changes); `test_match_compares_alpha` (types `#FFFFFFFF` and `#FFFFFF80`; `match(rgba="#FFFFFF80", ...)` returns the translucent one); `test_rrggbb_input_matches_rgba` (Review Focus 2).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 6: Spools, tags, ledger and the grams-left rule

**Files:**
- Create: `backend/scadbuddy/filament/spools.py`, `ledger.py`
- Modify: `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`
- Test: `backend/tests/filament/test_spools.py`, `test_ledger.py`

**Interfaces:**
- Produces in `ledger.py`: `record(conn, spool_id, kind, *, source=None, delta_g=None, absolute_g=None, ref_kind=None, ref_id=None, actor=None, printer=None, data=None) -> SpoolEvent | None` (returns `None` when the idempotency index already holds the event); `grams_left(events: Sequence[SpoolEvent], *, weight_locked: bool, label_weight_g: int | None) -> float | None` (spec §5.5: newest absolute reading is the anchor — `scale`, `person`, `bb`, and `ams_remain` only when newer than the newest scale/person reading and not locked — then subtract later deltas; a `print_started` estimate whose run has a `print_ended` with the same `ref_id` is skipped); `refresh_derived(conn, spool_id) -> None` (recomputes `grams_left`, `last_*` columns).
- Produces `FilamentSpoolStore(pool)`: `list(*, include_archived=False, needs_type=None)`, `get(id) -> Spool | None`, `create(body, *, actor) -> Spool` (writes `created`), `update(id, patch, *, actor) -> Spool | None` (writes one `edited` event with `data={"changes": {field: [before, after]}}`), `archive(id, archived, *, actor)`, `delete(id) -> bool` (raises `SpoolInUseError` when any event has `kind IN ('print_started','print_ended')`), `bind_tag(spool_id, *, tag_uid=None, tray_uuid=None, kind, override=False, actor) -> TagBinding` (finds or creates the tag; closes any open binding of that tag or of that spool with `reason='rebound'`; writes `tag_unbound` on the old spool and `tag_bound` on the new), `unbind_tag(tag_id, *, actor)`, `events(spool_id) -> list[SpoolEvent]`, `add_event(spool_id, body, *, actor) -> SpoolEvent` (for a person's weighing or drying).
- Routes: `GET/POST /filament/spools`, `GET/PATCH/DELETE /filament/spools/{id}`, `POST /filament/spools/{id}/archive`, `POST /filament/spools/{id}/tags`, `DELETE /filament/tags/{tag_id}/binding`, `GET/POST /filament/spools/{id}/events`. 409 on `SpoolInUseError`. Tools: `filament_spool_list`, `filament_spool_get`, `filament_spool_upsert`, `filament_spool_archive`, `filament_spool_delete`, `filament_spool_bind_tag`, `filament_tag_unbind`, `filament_spool_events`, `filament_spool_add_event`.

- [ ] **Step 1: Failing tests:**
  - `test_grams_left_anchors_then_subtracts`: scale 800 at t1, print_actual −50 at t2 → 750; ams_reading 700 at t3 → 700; same with `weight_locked=True` → 750.
  - `test_an_estimate_is_replaced_by_its_actual`: print_started −40 (ref r1) then print_ended −35 (ref r1) after scale 500 → 465; estimate only → 460.
  - `test_rebinding_a_tag_moves_it` (Review Focus 1): bind tag T to A, bind T to B → A has no open binding, events `tag_unbound` on A and `tag_bound` on B, no error.
  - `test_a_spool_with_prints_is_not_deleted` (Review Focus 4): add a `print_ended` event; `DELETE` → 409 and the spool stays; archive → 200.
  - `test_an_edit_records_each_change`: patch `note` and `rgba` → one `edited` event whose `data.changes` has both fields with before and after.
  - `test_recording_the_same_print_twice_is_ignored`: `record(..., ref_kind="print_run", ref_id="x", kind="print_ended")` twice → second returns `None`, one row.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 7: Locations

**Files:**
- Create: `backend/scadbuddy/filament/locations.py`
- Modify: `backend/scadbuddy/api/filament.py`, `agent/src/tools/filament.ts`
- Test: `backend/tests/filament/test_locations.py`

**Interfaces:**
- Produces `LocationStore(pool)`: `tree() -> list[LocationNode]` (nested, with `spool_count`), `create`, `update` (refuses a parent cycle with `LocationCycleError`), `archive`, `ensure_ams(printer_id, ams_id, tray_id, *, label) -> Location` (idempotent, kind `AMS`), `move(spool_id, location_id, *, actor) -> SpoolEvent` (writes `moved` with `data={"from": old, "to": new}`).
- Routes: `GET/POST /filament/locations`, `PATCH /filament/locations/{id}`, `POST /filament/spools/{id}/move`. Tools: `filament_location_list`, `filament_location_upsert`, `filament_spool_move`.

- [ ] **Step 1: Failing tests:** `test_the_tree_nests_and_counts`; `test_a_parent_cycle_is_refused` (A→B then B→A → 409); `test_a_move_is_a_ledger_event`; `test_ensure_ams_is_idempotent`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 8: Native spools run on the new tables

**Files:**
- Modify: `backend/scadbuddy/spools/native.py` (`NativeSpoolStore` keeps every public signature listed in its class: `list`, `any`, `get`, `create`, `update`, `delete`, `record_uses`, `settle_uses`, `settle_uses_in`)
- Test: existing `backend/tests/spools/test_native.py`, `test_native_uses.py`, `tests/api/test_native_spools.py`, `tests/crud/test_crud_spools.py` must pass unchanged; add `backend/tests/filament/test_native_adapter.py`

**Interfaces:**
- Consumes: `FilamentSpoolStore`, `ledger.record`, `ledger.refresh_derived`.
- Mapping: `NativeSpool.colour` = first 7 characters of the effective `rgba` (the native API stays `#RRGGBB` until #6262); `remaining_g` = `grams_left`; `brand`/`material` = effective catalog names; `preset` = the spool's preset for the model `"*"` or NULL; `tray_uuid`/`tag_uid` = the open binding's tag. `record_uses` writes `print_started`/`print_estimate` events with the Task 3 `ref_id` form; `settle_uses_in` writes the matching `print_ended`/`print_actual` (`data.basis="sliced"`) and calls `refresh_derived`.
- A native create with an unknown material inserts it into `filament_materials`.

- [ ] **Step 1: Failing test** `test_a_native_spool_is_a_filament_spool(pg_pool)`: create through `NativeSpoolStore`, read through `FilamentSpoolStore.get` → same id, `rgba` ends `FF`, a `created` event; `record_uses` + `settle_uses` → `grams_left` drops by the grams and the events exist.
- [ ] **Step 2:** Run it and the four existing files — the new one FAILS.
- [ ] **Step 3:** Rewrite `NativeSpoolStore` internals.
- [ ] **Step 4:** Run all five — PASS.
- [ ] **Step 5:** Commit.

### Task 9: Bambuddy import and pull

**Files:**
- Modify: `backend/scadbuddy/bambuddy/models.py` (`Spool` gains `material_number: str | None`, `extra_colors: str | None`, `effect_type: str | None`, `core_weight_catalog_id: int | None`, `updated_at: datetime | None`, `last_scale_weight: int | None`, `last_weighed_at: datetime | None`, `last_dried_at: datetime | None`, `weight_locked: bool = False`)
- Create: `backend/scadbuddy/filament/bambuddy_pull.py`
- Modify: `backend/scadbuddy/spools/activities.py`, `backend/scadbuddy/workflows/spool_refresh.py`, `backend/scadbuddy/api/crud_print.py` (route only if the activity runs outside the API process; see Step 3), `backend/scadbuddy/api/filament.py`
- Test: `backend/tests/filament/test_bambuddy_pull.py`, `backend/tests/test_spool_refresh_replay.py`

**Interfaces:**
- Produces `pull(store: Filament, spools: Sequence[bambuddy.models.Spool], *, now) -> PullReport` (`created`, `linked`, `updated`, `needs_type` counts and lists of ids). Per BB spool, in order:
  1. A `filament_bb_links` row → update the SB spool's fields that BB changed since `bb_updated_at`, writing `synced_from_bb` (and a `bb`-sourced absolute reading when `label_weight - weight_used` changed).
  2. Else an SB spool holding the BB spool's `tray_uuid`/`tag_uid` through an open binding → link it (Review Focus 3).
  3. Else create an SB spool: type by `SpoolTypeStore.match(code=material_number, ...)`, else by identity (brand, material, subtype, RGBA, label weight), else `type_id NULL` with the BB values as overrides; bind its tags; `created` + `bb` reading of `label_weight - weight_used`; link.
- Catalog names from BB (`material`, `subtype`, `brand`, `category`, `effect_type`) resolve by `upper(name)` and are inserted when missing.
- Route: `POST /filament/bambuddy/import` (runs `pull` once, returns `PullReport`); tool `filament_bambuddy_import`.
- Schedule: a new activity `filament_bambuddy_pull` runs after the provider refreshes in `SpoolRefresh`, behind `workflow.patched("filament-bb-pull")`.

- [ ] **Step 1: Failing tests:** `test_a_new_bb_spool_becomes_an_sb_spool` (type matched by `material_number`); `test_an_unmatched_bb_spool_needs_a_type`; `test_a_bb_spool_whose_tag_sb_holds_is_linked_not_duplicated`; `test_a_second_pull_changes_nothing` (counts all zero); `test_a_bb_weight_change_is_a_bb_reading`; the replay test still passes with the patch.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. Check where `_run_library_worker` runs (`main.py:1759`): if it runs in the API process with `state.components`, the activity calls the store directly; if not, add `POST /api/v1/internal/crud/print/filament/bambuddy-pull` on `crud_router("print")` and a client method in `crud/print.py`, following `POST /spools/records`.
- [ ] **Step 4:** Run — PASS.
- [ ] **Step 5:** Commit.

### Task 10: Whole-branch check and PR

- [ ] Run `uv run --frozen pytest tests/filament tests/spools tests/api/test_native_spools.py tests/api/test_filament.py tests/crud/test_crud_spools.py tests/orders tests/test_pg_migrations.py tests/test_spool_refresh_replay.py tests/api/test_openapi.py tests/api/test_routes.py`, ruff, mypy; `cd agent && pnpm test && pnpm typecheck`.
- [ ] In `docs/superpowers/specs/2026-11-14-filament-management-design.md` §12, change the #6209 sentence to: "#6209 (the price book keeps alpha, colour stops and effect) landed in #6211 before #6261; #6261's types write the book's alpha, stops and effect from it."
- [ ] Open the PR: `feat(filament): spool types, instances, catalogs and ledger (#6261)`, body `Closes #6261`, the session marker, and the footer. Shepherd it.

## Decisions made while building (#6261)

The controller's rulings during the build, each with what it costs if wrong.
The spec's §5.5 and §8.2 carry the rules that changed the design.

Model and migration
- `Filament` gets each store in the task that builds it. Cost: none.
- Native preset → a spool preset row with `printer_model='*'`, `nozzle_diameter='*'`. Cost: none; #6262 changes native preset readers.
- `filament_spool_events.printer_id` has no FK, so the history outlives a deleted printer. Cost: a deleted printer shows as unknown.
- Seed PA-CF. Cost: none.
- Native name → `colour_name`; no material → `Unknown` (shown as no material by the native API). Cost: a person renames or types it in #6262's UI.
- A settled but unapplied native use → `print_ended`, delta 0, `basis = "not_applied"`. Cost: grams-left drift if wrong.
- Migrated spools take their physical spool id; `spool_record_links` and `spool_provider_records` are rewritten to it. An in-flight LAN run across the upgrade skips its use, and a remembered tray "no" asks once more. Cost: one transient loss.
- Archived native spools get a closed binding, reason `archived`, as every archive path does now. Cost: none.

Ledger and grams left
- A native print's estimate comes off at print start (spec §5.5 beats the plan's "tests unchanged"; three old tests changed). Cost: none expected.
- One source of truth: the native store never writes `native_spools` or `native_spool_uses`. Cost: none.
- Order is physical time: `print_ended` at the plate's start (no plate end is stored), a BB reading at its `updated_at`. Cost: the under/over-counts listed in spec §5.5, until #6263 records plate ends.
- The weight lock guards only estimates; a scale weighing (BB's included, net of core) always counts. Cost: a wrong scale reading wins on a locked spool.
- `ledger.record` locks the spool before its insert. Cost: none.

Spools, tags, locations
- A spool's location changes only through `move()`; `location_id` is refused on create and patch (422). Cost: an API client must call the move route.
- AMS tray locations are keyed by (printer, AMS, tray); their names never clash. Cost: two tray places may share a label.
- Every writer locks the tag before the spool; archive closes the open binding, restore binds nothing (native restore rebinds its last tag). Cost: a person rebinds a restored spool's tag by hand.
- Native delete: hard delete when no print ended on the spool, else 409. Cost: a used spool stays archived.
- Native lists include spools made through `/filament`; one inventory. Cost: none expected.
- A printer merge moves locations and K values: the kept printer's tray place and K value win, spools move with a `moved` event (source NULL, actor `system:printer-merge`), the gone place is archived. Cost: a `system` source needs a CHECK change later.
- Native routes and the import route record `actor(caller)`; system writers keep their names.

Price book
- A type's book row identity includes alpha (`20261115T0154Z`, which changes #6211's identity index); insert writes alpha, extra colours and effect; a found row is never rewritten. Cost: opaque and translucent of one hue are separate rows.
- Order import and item-print matching still compare RGB only: #6316.

Bambuddy pull
- The pull runs on the library worker in the API process (the CRUD service), so it calls the store directly and needs no CRUD route.
- Step 2 also links through an existing `spool_record_links` row for the BB record. A tag held by an archived SB spool is ignored (a new spool takes the tag); a record link to an archived spool stays as it is and is reported in `PullReport.archived`.
- A BB unlock clears the lock only when the newest lock change was a pull's; an SB lock wins.
- BB's last weighing → `weighed` (`bb_scale`), last drying → `dried` (`bb_dried`), each booked once. `extra_colors` is comma-separated hex (BB `schemas/spool.py`). `core_weight_catalog_id` is not mapped (#6262). Per-type sync settings are #6263.
- One malformed BB row is skipped and counted; the rest go on.

Unrelated flakes seen: #6313 (a lease test under xdist), a payments balance test under load.

Review follow-ups (#6342)
- The pull runs on every spool refresh while `bambuddy_url` is set, until #6262's `filament_bb_pull` setting gates it (#6265 adds the per-type settings). Every BB spool thus becomes a native provider record (priority 10) carrying BB's own values (#6342).
- Migrated settled uses (`20261114T1302Z`) keep `settled_at` as their time, unlike new `print_ended` events, which sit at the plate start. This cannot change grams left: every pulled reading is booked at BB's `updated_at`, which is after every migrated event (#6342).
- Deleting a Bambuddy-linked spool is refused (409, `SpoolBambuddyLinkedError`): the pull would make it again. Archive it. A spool with no link deletes as before; the rule is in `FilamentSpoolStore.delete`, so the native delete has it too (#6342). A link to a BB spool that is gone from Bambuddy is never removed yet, so such a spool cannot be deleted until #6265's sync engine owns the link's lifecycle; a partial BB listing must never unlink spools.
