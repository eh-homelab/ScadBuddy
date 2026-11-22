# Printer and AMS Labels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Print a label for each printer, AMS unit and AMS slot. The label carries a typed code (`L-12`) and a QR of `<label base URL>/x/L-12`, which opens that place in ScadBuddy.

**Architecture:** Locations get an identity `number`. Each printer gets a root location, and its AMS places hang under it. A small `codes` module parses `<prefix>-<number>` and resolves it through a prefix registry (`L`, `S` now). The frontend adds `/x/:code`, a location page and a "Printers" mode on the labels page. Printing stays in the browser.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, pytest; React, react-router, vitest, msw; the `qrcode` npm package; the agent's TypeScript tools.

**Spec:** `docs/superpowers/specs/2026-11-22-printer-location-labels-design.md`

## Global Constraints

- Display form of a code: `<PREFIX>-<number>`, prefix upper case. Parse accepts any case, with or without the dash (`l12`, `L12`, `L-12`, ` L-12 `). Prefixes now: `L` (location), `S` (spool).
- QR payload of a location label: `<base>/x/L-<number>`, with `<base>` an https origin and no trailing slash.
- `filament_label_base_url`: an https origin (`core/urls.py` `https_origin`), at most 64 characters, live. Unset falls back to `public_url`, then to the page's own origin.
- The QR on a 50 x 25 mm label stays 19.8 mm square (`QR_MM`), level M, and its module is at least 0.5 mm: at most QR version 5 (37 modules), so the payload is at most 84 bytes.
- Numbers are never reused. An archived thing still resolves, with `archived` set.
- Spool labels keep `scadbuddy:spool:<id>` (plan 2026-11-17 Decision 12). Do not change them.
- New migration files only, named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.
- No new database client or cross-service grant (root CLAUDE.md "Database access").
- Generated API files (`backend/openapi.json`, `schema.d.ts`) are never committed.

## Review Focus

1. **A printer whose AMS unit places existed before this change** (parent null): after `ensure_printer_on`, the unit places sit under the printer's place, and their tray places stay under their units. Test in Task 2.
2. **Two calls make the same printer's place at once** (two label requests, or a label request racing capture): one place, both callers get its id. Test in Task 2.
3. **A code typed with spaces, lower case or no dash, or an unknown prefix**: the first resolves; an unknown prefix or a non-number is a 404 with `code_unknown`, never a 500. Test in Task 3.
4. **A long label base URL**: a value over 64 characters is a 422 on save, so no label QR goes past version 5. Test in Task 4. The frontend also asserts the module size for the longest base (Task 8).
5. **A printer place that is archived, or a printer merged into another**: the merge's `repoint_printer_locations` also handles the printer root (the kept printer's root stands; the merged one's is archived and its children move). Test in Task 2.

---

### Task 1: Location numbers and the printer kind

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_filament_location_numbers.sql`
- Modify: `backend/scadbuddy/filament/models.py` (`Location`)
- Test: `backend/tests/filament/test_location_numbers.py`

**Interfaces:**
- Produces: column `filament_locations.number integer GENERATED ALWAYS AS IDENTITY UNIQUE`; field `Location.number: int`; a seeded `filament_location_kinds` row named `Printer` (`dry_storage` false, `ams` false), inserted only when no unarchived kind of that name exists (`upper(name)`).

- [ ] **Step 1: Write the failing tests**

```python
def test_existing_and_new_locations_get_distinct_numbers(pg_pool): ...
    # create two places with LocationStore.create; assert both .number are ints and differ
def test_printer_kind_is_seeded(pg_pool): ...
    # CatalogStore(pool).list("location-kinds") has a LocationKind named "Printer", ams False, dry_storage False
def test_numbers_are_not_reused_after_archive(pg_pool): ...
    # archive a place, create another: new number > archived one's
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_location_numbers.py -v`
Expected: FAIL (`Location` has no `number`).

- [ ] **Step 3: Write the migration and add `number: int` to `Location`**

`ALTER TABLE ... ADD COLUMN number integer GENERATED ALWAYS AS IDENTITY UNIQUE` numbers the existing rows. `_COLUMNS` in `locations.py` reads the model's fields, so every select picks up `number`. Check `bb_sync_plan.py` and `bb_sync_models.py` for any place that builds a `Location` from a hand-written column list, and add `number` there.

- [ ] **Step 4: Run the filament tests**

Run: `cd backend && uv run --frozen pytest tests/filament -n auto -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(filament): a typed number for every location (#6955)`

---

### Task 2: A printer place with its AMS places under it

**Files:**
- Modify: `backend/scadbuddy/filament/locations.py` (`ensure_printer_on`, `ensure_ams_unit_on`, `ensure_ams_on`, `repoint_printer_locations`)
- Test: `backend/tests/filament/test_locations.py`

**Interfaces:**
- Consumes: the `Printer` kind (Task 1), `bambuddy/trays.py` `EXTERNAL`.
- Produces: `LocationStore.ensure_printer_on(conn: Conn, printer_id: UUID) -> UUID` (static) and `LocationStore.ensure_printer(self, printer_id: UUID) -> UUID`. The place is keyed by `printer_id` with `ams_id IS NULL AND tray_id IS NULL`, named as the printer, of the first unarchived kind named `Printer` (`NoPrinterKindError` when none, a `ValueError` like `NoAmsKindError`), `CatalogRefError` for an unknown printer.

- [ ] **Step 1: Write the failing tests**

```python
def test_printer_place_is_made_once_and_named_as_the_printer(pg_pool): ...
def test_two_threads_make_one_printer_place(pg_pool): ...   # ThreadPoolExecutor, as the existing race tests do
def test_unit_places_made_before_hang_under_the_printer(pg_pool): ...
    # make a unit place with parent NULL via ensure_ams_unit_on before the change's code path,
    # then ensure_printer; unit.parent_id == printer place; its tray's parent is still the unit
def test_new_unit_and_external_tray_hang_under_the_printer(pg_pool): ...
    # ensure_ams(printer, 0, 2) -> unit under printer; ensure_ams(printer, EXTERNAL, 0) -> parent is the printer place
def test_merge_keeps_the_kept_printers_place(pg_pool): ...
    # both printers have places; after repoint_printer_locations, merged's root is archived and
    # its unit places are under into's root
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_locations.py -k printer -v`
Expected: FAIL (`ensure_printer` not defined).

- [ ] **Step 3: Implement `ensure_printer_on`, then call it from `ensure_ams_unit_on` (set `parent_id` on insert, and on a found unit place whose `parent_id` is null) and from `ensure_ams_on` for the external holder**

The insert uses `ON CONFLICT ... DO NOTHING` then reads again, as `ensure_ams_unit_on` does. Add a partial unique index in a new migration for the root: `(printer_id) WHERE ams_id IS NULL AND tray_id IS NULL AND printer_id IS NOT NULL AND archived_at IS NULL`. In `repoint_printer_locations`, the join on `ams_id`/`tray_id` already matches the roots with `IS NOT DISTINCT FROM`; check that the `ams_id` comparison also uses `IS NOT DISTINCT FROM`, so the two roots (both `ams_id` null) clash and the merged root is archived.

- [ ] **Step 4: Run the filament tests**

Run: `cd backend && uv run --frozen pytest tests/filament -n auto -q`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(filament): each printer is a location, its AMS places under it (#6955)`

---

### Task 3: Codes and their resolver

**Files:**
- Create: `backend/scadbuddy/codes/__init__.py`, `backend/scadbuddy/codes/parse.py`, `backend/scadbuddy/codes/resolve.py`, `backend/scadbuddy/api/codes.py`
- Modify: the router registration in `backend/scadbuddy/main.py` (as `api/filament.py`'s router is registered)
- Test: `backend/tests/codes/test_parse.py`, `backend/tests/api/test_codes.py`

**Interfaces:**
- Produces:
  - `parse_code(text: str) -> Code | None`, `Code = NamedTuple(prefix: str, number: int)`; `format_code(prefix: str, number: int) -> str` gives `L-12`.
  - `PREFIXES: dict[str, Lookup]`, where `Lookup` is `(table, kind)`: `"L": ("filament_locations", "location")`, `"S": ("filament_spools", "spool")`. One SQL per lookup: `SELECT id, name, archived_at FROM <table> WHERE number = %s` (spools have no `name`: use `NULL`, and the route's `name` is null for a spool).
  - `resolve(conn: Conn, code: Code) -> Resolved | None`, `Resolved(BaseModel)`: `kind: Literal["location", "spool"]`, `id: UUID`, `code: str`, `name: str | None`, `archived: bool`.
  - Route `GET /api/v1/codes/{code}` → `Resolved`; 404 problem with `code` `code_unknown` for an unparsable code, an unknown prefix or no row. It runs on the shared `scadbuddy-rows` pool (`rows/component.py` `ROWS`).

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("text", ["L-12", "l12", "L12", " l-12 ", "L-012"])
def test_parse_accepts(text): assert parse_code(text) == Code("L", 12)
@pytest.mark.parametrize("text", ["", "L-", "-12", "L-1x", "12", "L--12", "L-0", "L-99999999999"])
def test_parse_refuses(text): assert parse_code(text) is None
def test_format(): assert format_code("S", 142) == "S-142"
# api: a location resolves (kind, id, code "L-<n>", name); a spool resolves; an archived
# location resolves with archived True; "Q-1", "L-999999", "nonsense" are 404 code_unknown
```

`L-0` and a number past `2**31 - 1` are refused, so the query never fails on an integer overflow.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/codes tests/api/test_codes.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement `parse.py`, `resolve.py` and the route**

Parse with one regex on the stripped, upper-cased text: `^([A-Z])-?([0-9]+)$`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run --frozen pytest tests/codes tests/api/test_codes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(codes): typed codes and their resolver (#6955)`

---

### Task 4: The label base URL setting

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (field, validator, `"live"` entry beside `filament_low_stock_pct`), `backend/scadbuddy/library/settings_store.py` (the patch and stored models, as `filament_low_stock_pct`), `backend/scadbuddy/api/settings.py` (the view), `backend/scadbuddy/core/setting_docs.py`, `frontend/src/pages/settings/fields.ts`, `frontend/src/pages/settings/sections/filament.tsx` (or the file that renders the Filament section's fields), `frontend/src/mocks/fixtures.ts`, `frontend/src/mocks/features/settings.ts`
- Test: `backend/tests/test_settings_label_base_url.py`, `frontend/src/pages/settings/sections/filament.test.tsx`

**Interfaces:**
- Produces: setting `filament_label_base_url: str | None = None`; the helper `label_base_url(settings_read) -> str | None` in `backend/scadbuddy/filament/labels.py`, giving the setting, else `public_url`, else `None`.
- Copy for `setting_docs.py`: label "Label address", help "The address that location labels open, such as https://scadbuddy.example.com. Keep it stable: a printed label keeps the address it was printed with. Empty uses ScadBuddy's public URL. At most 64 characters."

- [ ] **Step 1: Write the failing tests**

```python
def test_saves_an_https_origin(client): PUT {"filament_label_base_url": "https://sb.example.com"} -> 200, read back the same
def test_refuses_a_path_or_http(client): "https://sb.example.com/x" and "http://sb.example.com" -> 422
def test_refuses_over_64_characters(client): "https://" + "a" * 50 + ".example.com" -> 422
def test_falls_back_to_public_url(): label_base_url({"filament_label_base_url": None, "public_url": "https://p.example"}) == "https://p.example"
```

Frontend: the Filament section shows the "Label address" field and sends it on save.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/test_settings_label_base_url.py -v`; `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/settings/sections/filament.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Add the setting through each layer, as `filament_default_cost_per_kg` is added**

- [ ] **Step 4: Run the tests**

Same commands. Expected: PASS.

- [ ] **Step 5: Commit** — `feat(filament): the label address setting (#6955)`

---

### Task 5: The location page's read and the printer labels' read

**Files:**
- Create: `backend/scadbuddy/filament/labels.py` (with Task 4's helper)
- Modify: `backend/scadbuddy/filament/locations.py` (`detail`), `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/api/filament.py`
- Test: `backend/tests/filament/test_location_detail.py`, `backend/tests/api/test_filament_printer_labels.py`

**Interfaces:**
- Consumes: `ensure_printer_on` (Task 2), `format_code` (Task 3), `label_base_url` (Task 4).
- Produces:
  - `LocationDetail(Location)`: `code: str`, `kind_name: str`, `path: list[PathStep]` (root first, this place excluded; `PathStep`: `id`, `name`, `code`), `children: list[PathStep]` (unarchived, sort order then name), `spools: list[PlacedSpool]` (`id`, `number`, `code`, `material`, `colour_rgba`, `grams_left`; unarchived spools placed here).
  - `LocationStore.detail(self, location_id: UUID) -> LocationDetail | None`. Route `GET /filament/locations/{location_id}` → `LocationDetail`, 404 when none.
  - `PrinterLabel(BaseModel)`: `location_id: UUID`, `code: str`, `name: str`, `depth: int` (0 printer, 1 unit or external holder, 2 slot). `PrinterLabels(BaseModel)`: `base_url: str | None`, `labels: list[PrinterLabel]`.
  - Route `POST /filament/printer-labels`, body `{printer_ids: list[UUID] | None}` (null means every printer), → `PrinterLabels`: for each printer by name, `ensure_printer_on`, then its unarchived places in tree order (unit by `ams_id`, slot by `tray_id`, the external holder last). 422 for an unknown printer id. It is a POST because it may make the printer's place.

- [ ] **Step 1: Write the failing tests**

```python
def test_detail_of_a_slot(pg_pool): path is [printer, unit] by name, code "L-<n>", spools has the placed spool
def test_detail_of_a_printer_lists_its_units_as_children(pg_pool): ...
def test_detail_of_an_unknown_place_is_none(pg_pool): ...
def test_printer_labels_in_tree_order(client): printer (depth 0), AMS-A (1), slot 1..4 (2), External (1) last
def test_printer_labels_make_the_printer_place(client): no root before; one after; a second call makes no second
def test_printer_labels_carry_the_base_url(client): set filament_label_base_url; response base_url equals it
def test_printer_labels_unknown_printer_is_422(client): ...
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_location_detail.py tests/api/test_filament_printer_labels.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `detail`, the two routes and their models**

Each route's `description` states its 404/422 cases, as the existing location routes do.

- [ ] **Step 4: Run the tests, then the whole backend suite's lint and types**

Run: `cd backend && uv run --frozen pytest tests/filament tests/api/test_filament_printer_labels.py -n auto -q && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS, no findings.

- [ ] **Step 5: Commit** — `feat(filament): a location's page read and the printer labels read (#6955)`

---

### Task 6: Frontend API, mocks and the `/x/:code` page

**Files:**
- Modify: `frontend/src/api/filament.ts` (`resolveCode`, `getLocation`, `printerLabels`), `frontend/src/mocks/features/filament/index.ts` and `seed.ts` (numbers on seeded places, a printer place, the three routes), `frontend/src/App.tsx`
- Create: `frontend/src/pages/CodePage.tsx`
- Test: `frontend/src/pages/CodePage.test.tsx`

**Interfaces:**
- Consumes: the routes of Tasks 3 and 5 through the generated schema.
- Produces: `resolveCode(code: string): Promise<Resolved>`, `getLocation(id: string): Promise<LocationDetail>`, `printerLabels(printerIds: string[] | null): Promise<PrinterLabels>`; route `x/:code` → `CodePage`.

- [ ] **Step 1: Write the failing tests**

`CodePage` at `/x/L-3` replaces the history entry with `/filament/locations/<id>`; at `/x/S-1` with `/filament/spools/<id>`; at `/x/Q-9` shows "No such code: Q-9" and a field to type another code.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm exec vitest run src/pages/CodePage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement the client calls, the mocks and `CodePage`** (navigate with `replace: true`, so Back does not loop)

- [ ] **Step 4: Run the tests** — same command, PASS.

- [ ] **Step 5: Commit** — `feat(filament): open any code at /x/<code> (#6955)`

---

### Task 7: The location page

**Files:**
- Create: `frontend/src/pages/filament/LocationPage.tsx`
- Modify: `frontend/src/App.tsx` (route `filament/locations/:id`), `frontend/src/pages/filament/LocationsTab.tsx` (a row's name links to the page)
- Test: `frontend/src/pages/filament/LocationPage.test.tsx`

**Interfaces:**
- Consumes: `getLocation` (Task 6).

- [ ] **Step 1: Write the failing tests**

For a slot: the heading is the name, the code `L-<n>` is shown, the path's steps link to their pages, the placed spool links to its spool page with its grams left, and "Print label" links to `/filament/labels?locations=<id>`. For a printer place: its units are listed as links, and "Printer settings" links to Settings → Printers (there is no printer page). An unknown id shows "No such location". At 390 px wide nothing scrolls sideways.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm exec vitest run src/pages/filament/LocationPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement the page with the existing filament page's components** (`SpoolLink`, `RgbaSwatch`)

- [ ] **Step 4: Run the tests** — same command, PASS.

- [ ] **Step 5: Commit** — `feat(filament): a page for each location (#6955)`

---

### Task 8: Printer labels on the labels page

**Files:**
- Modify: `frontend/src/components/filament/labelSheet.ts` (`locationPayload`), `frontend/src/components/filament/Label.tsx` (a location variant: large code, name, QR), `frontend/src/pages/filament/LabelsPage.tsx`, `frontend/src/pages/filament/LocationsTab.tsx` ("Print printer labels")
- Test: `frontend/src/components/filament/labelSheet.test.ts`, `frontend/src/pages/filament/LabelsPage.test.tsx`

**Interfaces:**
- Consumes: `printerLabels` (Task 6).
- Produces: `locationPayload(base: string, code: string): string` → `${base}/x/${code}`, with `base` stripped of a trailing slash. The page's query: `?printers=<id>,<id>` or `?printers=all`, and `?locations=<id>` for one place (Task 7's link; read with `getLocation`).

- [ ] **Step 1: Write the failing tests**

```ts
it('builds the location payload', () => expect(locationPayload('https://sb.example.com/', 'L-12')).toBe('https://sb.example.com/x/L-12'))
it('keeps the module at least 0.5 mm for the longest base', async () => {
  // a 64-character base + '/x/L-9999999': QRCode.create(payload, {errorCorrectionLevel: 'M'}).modules.size <= 37,
  // and QR_MM / size >= 0.5
})
```

LabelsPage: `?printers=all` renders one label per `PrinterLabels.labels` entry, in order, each with its code and name and a QR; with `base_url` null the payload uses `window.location.origin`; the page names the base it uses ("Labels open https://…"); the layout choice (`roll`/`sheet`) applies as for spool labels; `?ids=` (spool labels) still works unchanged.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm exec vitest run src/components/filament/labelSheet.test.ts src/pages/filament/LabelsPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the frontend checks**

Run: `cd frontend && pnpm typecheck && pnpm exec vitest run src/components/filament src/pages/filament src/pages/CodePage.test.tsx && pnpm lint`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(filament): print labels for printers, AMS units and slots (#6955)`

---

### Task 9: Agent tools and notes

**Files:**
- Modify: `agent/src/tools/filament.ts` (`filament_code_resolve`, `filament_location_get`, `filament_printer_labels`, each `read`, beside `filament_location_list`), its tests in `agent/test/`, `backend/CLAUDE.md` (the `filament/` layout entry: `number`, the printer place, `labels.py`; a `codes/` entry), the spec's §5.6 line on QR labels in `docs/superpowers/specs/2026-11-14-filament-management-design.md` ("As built in #6955: …")

**Interfaces:**
- Consumes: the routes of Tasks 3 and 5.

- [ ] **Step 1: Write the failing tool tests**, following the existing `filament_location_list` test: each tool calls its route and returns the body; `filament_printer_labels` takes `printer_ids` (optional).

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/<the filament tools test file>`
Expected: FAIL.

- [ ] **Step 3: Implement the tools and write the notes**

`filament_printer_labels` is `read` even though its route may make a printer's place: the place is derived data the next capture would make anyway. State that in the tool's description.

- [ ] **Step 4: Run the agent checks**

Run: `cd agent && pnpm typecheck && pnpm exec vitest run test/<the filament tools test file>`
Expected: PASS.

- [ ] **Step 5: Commit** — `feat(agent): resolve codes and read places and printer labels (#6955)`
