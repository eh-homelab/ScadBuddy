# Web Bambu Studio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plate, object and slicer control in the browser, at Bambu Studio's level, with a better UX: any setting at any level, a click-to-select editor, Bambu Studio's by-object rules, stable object ids, remembered choices, and agent tools at parity.

**Architecture:** A plate document (JSON blob beside each output) is the source of truth; the 3MF is written from it. One React component, `PlateEditor`, shows and edits it; the host page grants capabilities. Edits are an operation list that the server replays and saves as a new output. One rules module holds Bambu Studio's by-object rules for Arrange, the print check, the editor and the agent.

**Tech Stack:** Python 3.12, FastAPI, Temporal, pydantic (backend); React, three.js, `@react-three/fiber`, `@react-three/drei`, vitest, Playwright (frontend); TypeScript tools with zod (agent).

**Spec:** `docs/superpowers/specs/2026-11-14-web-bambu-studio-design.md`. Epic: #6272.

**Milestones (spec §12):** 1 = Tasks 2, 4a, 4b, 5, 6, 7 and Phase 4 (ORD-2026-2 prints by object, no editor). 2 = Tasks 1, 3, 8, 9, 10, 11, 13, 21. 3 = Tasks 12, 14, 15, 16, 17, 22.

## Global Constraints

- No slicer key or value is refused anywhere. A check returns warnings only. The slicer's refusal returns as a 422 that names the key (spec §7.1, #2998).
- Every capability of `PlateEditor` is off unless the host turns it on. Every save route checks the caller's role on the server (spec §4.1, §4.5).
- An edit never changes its source output. A save writes a new output (spec §4.5).
- Bambu Studio rules cite tag `v02.08.02.61` with file and line links (spec §0). A rule's code comment carries its link.
- H2C values: `extruder_clearance_max_radius` 96, `extruder_clearance_radius` 49, `extruder_clearance_height_to_rod` 47.4, `extruder_clearance_height_to_lid` 201, `extruder_clearance_dist_to_rod` 50, `wrapping_exclude_area` x 145–251, y 310–326.
- `obj_distance = 0.5 * extruder_clearance_max_radius - 0.1` (or `0.5 * MAX_OUTER_NOZZLE_RADIUS - 0.1` when every object is shorter than `nozzle_height`); in a by-object print `max(obj_distance + 0.5 * skirt, skirt)`.
- Images and meshes move as blobs and `{name, mediaType}` pointers, never base64 in JSON (repo rule #2479). The plate document is a blob, not a Postgres column.
- No new database client. New data that crosses services goes through a CRUD route with a service credential (root CLAUDE.md, #5295).
- Migrations: new file `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` only.
- Never commit `backend/openapi.json` or either `schema.d.ts`.
- PR titles are conventional commits; each PR body says `Part of #6272` and `Fixes #N` for its own issue.
- Write docs and UI copy in Simplified Technical English.

## Review Focus

1. **A 3MF from outside ScadBuddy** (a Bambu Studio project with modifiers, negative volumes, several instances of one object): the plate document reader must keep every volume type and instance, and a write must give it back to the slicer unchanged in meaning. Test owned by Task 3.
2. **An unknown or misspelled settings key** at object or part level: it is written, a warning names it, and the slice's refusal (if any) names it. Tests owned by Task 13 and Task 1.
3. **Two people or the agent and a person editing the same output**: each save makes its own new output from the same source; neither overwrites the other. Test owned by Task 8.
4. **A plate whose by-object order makes a tall object print before a later object in its Y band**: the rules flag the tall object, Arrange's by-object goal reorders it last. Tests owned by Task 2 and Task 6.
5. **A storefront visitor who calls the plate-edits route directly**: refused by role, whatever the browser's capabilities say. Test owned by Task 8 (the route) and Task 17 (`recolour goes to the storefront route, never plate-edits`).

---

## Phase 0: In flight (no new work here)

- #2998: generic `process_overrides`, the searchable settings list, `get_process_values`. PR in progress in another worktree. Tasks 13 and 14 build on it.
- #6218 / PR #6220: colour match on by default, remembered.

## Phase 1: Foundations (parallel)

### Task 1: Settings catalogue from Bambu Studio's source (issue #6273)

**Files:**
- Create: `tools/bbs-catalogue/generate.py` (reads the source at the pinned tag, writes JSON)
- Create: `backend/scadbuddy/slicing/catalogue.py` (loads the JSON, answers queries)
- Modify: `Dockerfile` (`slicer-dist` stage: fetch the three source files at `v${BAMBU_STUDIO_VERSION}`, run the generator, copy `settings-catalogue.json` next to `/opt/slicer-profiles`)
- Create: `backend/scadbuddy/api/settings_catalogue.py` (`GET /api/v1/print/settings-catalogue?q=&section=&scope=`)
- Create: `agent/src/tools/slicerSettings.ts` (`search_slicer_settings`)
- Test: `backend/tests/slicing/test_catalogue.py`, `agent/src/tools/slicerSettings.test.ts`; fixture: the three source files at the tag, under `backend/tests/fixtures/bbs-v02.08.02.61/` (LFS)

**Interfaces:**
- Produces: `SettingDef(key: str, label: str, tooltip: str, type: Literal['bool','int','float','percent','float_or_percent','string','enum','point','points','ints','floats','bools','strings'], unit: str | None, min: float | None, max: float | None, enum_values: list[str], enum_labels: list[str], mode: Literal['simple','advanced','develop'], page: str | None, group: str | None, order: int | None, wiki: str | None, scope: Literal['print','object','region','filament','machine'])`; `load_catalogue() -> dict[str, SettingDef]`; `search(q: str, section: str | None, scope: str | None) -> list[SettingDef]`; `check_value(key: str, value: object) -> str | None` (a warning, never an exception).

- [ ] **Step 1: Write the failing tests** — `test_print_sequence_def`: `cat['print_sequence'].type == 'enum'`, `enum_values == ['by layer', 'by object']`, `mode == 'simple'`. `test_no_sparse_layers_wiki`: `cat['wipe_tower_no_sparse_layers'].page == 'Others'`, `group == 'Prime tower'`, `wiki == 'parameter/prime-tower'`. `test_scope`: `cat['wall_loops'].scope == 'region'`, `cat['enable_prime_tower'].scope == 'print'`. `test_process_keys_count`: the set of keys with `scope in {'print','object','region'}` equals `s_Preset_print_options` at the tag (count fixed in the test). `test_check_value_warns_never_raises`: `check_value('wall_loops', -3)` returns a string; `check_value('no_such_key', 1)` returns `"unknown key"`-style text.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/slicing/test_catalogue.py -v` — expect FAIL (module missing).
- [ ] **Step 3: Implement the generator.** Parse `def = this->add("<key>", co<Type>);` blocks up to the next `def = this->add`, reading `label`, `tooltip`, `sidetext`, `min`, `max`, `enum_values.push_back`, `enum_labels.push_back`, `mode`. Read `TabPrint::build` for `add_options_page(L("<page>")`, `new_optgroup(L("<group>")`, `append_single_option_line("<key>", "<wiki>")` in order. Scope from `PrintObjectConfig`/`PrintRegionConfig`/`PrintConfig` class membership (the `PRINT_CONFIG_CLASS_DEFINE` blocks in `PrintConfig.hpp`; fetch it too).
- [ ] **Step 4: Run the tests** — expect PASS. Add the route and the tool (`risk: 'read'`, routes list); `cd agent && pnpm test src/tools/slicerSettings.test.ts`.
- [ ] **Step 5: Commit** `feat(print): a settings catalogue generated from Bambu Studio's source (#6273)`.

### Task 2: The rules module and the printers' clearance data (issue #6274)

**Files:**
- Modify: `backend/scadbuddy/render/plate_profiles.py` (`CLEARANCES` → `Clearances` dataclass per printer, generated from `/opt/slicer-profiles` machine JSON at import, with today's table as the fallback for tests without the tree)
- Create: `backend/scadbuddy/render/plate_rules.py`
- Create: `backend/tests/render/vectors/plate_rules/*.json` (shared with Task 12's TypeScript port)
- Test: `backend/tests/render/test_plate_rules.py`

**Interfaces:**
- Produces: `Clearances(max_radius: float, radius: float, height_to_rod: float, height_to_lid: float, dist_to_rod: float, nozzle_height: float, printable_height: float, bed_exclude: list[Polygon2], wrapping_exclude: list[Polygon2])`; `clearances_for(printer_model: str) -> Clearances | None`; `RuleObject(id: str, hull: list[tuple[float,float]], height: float)`; `RuleWarning(rule: Literal['hull_clearance','too_tall_rod','too_tall_lid','bed_exclude','wrapping_exclude'], objects: list[str], detail: str, source: str)`; `check_plate(objects: Sequence[RuleObject], *, sequence: Literal['by layer','by object'], clearances: Clearances, skirt_mm: float, wrapping_detection: bool) -> list[RuleWarning]`; `obj_distance(objects, clearances, skirt_mm, by_object: bool) -> float`.

- [ ] **Step 1: Write the failing tests** — `test_h2c_values`: `clearances_for('Bambu Lab H2C')` has `max_radius == 96`, `radius == 49`, `height_to_rod == 47.4`, `height_to_lid == 201`, `dist_to_rod == 50`, `wrapping_exclude == [[(145,310),(251,310),(251,326),(145,326)]]`. `test_hull_clearance_h2c`: two 20 mm squares 95 mm apart, both 60 mm tall, by object → one `hull_clearance` warning; 96 mm apart → none. `test_skirt_adds`: skirt 10 mm → `obj_distance == max(47.9 + 5, 10)`. `test_tall_before_overlapping_y`: object A 70 mm prints first, B prints after in an overlapping Y band (bands grown by 25 mm per side) → `too_tall_rod` on A; swap order → none. `test_last_object_uses_printable_height`. `test_exclusion_by_layer_only_for_wrapping`: an object in the wrapping area → `wrapping_exclude` in both modes (spec assumption 11). `test_menorah_layout`: the owner's 3-plate layout from #6246 → no warnings; all 10 candles on one plate → warnings.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/render/test_plate_rules.py -v` — expect FAIL.
- [ ] **Step 3: Implement** with shapely (already a backend dependency via trimesh? if not, add `shapely` to `backend/pyproject.toml`): `buffer(obj_distance, join_style='round')` for the grown hull. Each rule's docstring carries its `Print.cpp` link from spec §5.2. Write each test case also as a vector file (`{input, expected}`).
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `feat(render): Bambu Studio's by-object rules in one module (#6274)`.

### Task 3: The plate document (issue #6275)

**Files:**
- Create: `backend/scadbuddy/render/plate_document.py` (model, reader from a 3MF, writer to a 3MF)
- Modify: `backend/scadbuddy/workflows/outputs.py` (write the document blob beside each output it writes; `Layout` becomes a view of it)
- Modify: `backend/scadbuddy/render/objects3mf.py` (expose volume types and instances to the reader)
- Create: routes `GET /api/v1/outputs/{id}/plate-document` and `GET /api/v1/library/files/{id}/plate-document` in `backend/scadbuddy/api/outputs.py`
- Test: `backend/tests/render/test_plate_document.py`; fixtures: an Arrange output, a declared-objects output, a Bambu Studio project with a modifier and two instances (LFS)

**Interfaces:**
- Produces: `PlateDocument(version: Literal[1], plates: list[DocPlate], objects: dict[str, DocObject], parts: dict[str, DocPart], groups: dict[str, DocGroup], process_overrides: dict[str, Any])`; `DocPlate(id: str, name: str | None, objects: list[str])`; `DocObject(id: str, name: str, transform: Matrix34, parts: list[str], settings: dict[str, Any], group: str | None)`; `DocPart(id: str, mesh: BlobPointer, volume_type: Literal['part','negative','modifier','support_blocker','support_enforcer'], slot: int, transform: Matrix34, settings: dict[str, Any])`; `read_document(threemf: BinaryIO) -> PlateDocument`; `write_3mf(doc: PlateDocument, out: BinaryIO, *, project: ProjectSettings) -> None`; `document_of(output_id: str) -> PlateDocument` (reads the blob, or reads the 3MF once and stores the blob for an older output).
- Ids: `o1`, `o2`, … and `o1.p1`, …; stored in `model_settings.config` metadata `scadbuddy_id`.

- [ ] **Step 1: Write the failing tests** — `test_round_trip_ids`: read → write → read keeps every id, transform (to 1e-6), slot, volume type and setting. `test_bbs_project_modifier_kept` (Review Focus 1). `test_order_is_object_order`: the written `<build>` items follow `DocPlate.objects`. `test_old_output_reads`: an output with no blob gets one on first `document_of`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/render/test_plate_document.py -v` — expect FAIL.
- [ ] **Step 3: Implement.** Reuse `bambu3mf.py`'s writer pieces; do not add a second XML writer.
- [ ] **Step 4: Run** — expect PASS; also `uv run pytest tests/ -k "outputs or arrange" -n auto`.
- [ ] **Step 5: Commit** `feat(render): a plate document with stable object and part ids (#6275)`.

### Task 4a: Warn when a by-object print has one multi-colour object (issue #6245)

**Files:** Modify `backend/scadbuddy/bambuddy/clearance.py`; Test `backend/tests/bambuddy/test_clearance.py`.

- [ ] **Step 1: Test** `test_one_object_by_object_warns`: a plate with one object of several parts and `print_sequence='by object'` → a warning whose text says the plate is one object and names `echo(objects = N)`.
- [ ] **Step 2: Run, fail. Step 3: Implement. Step 4: Run, pass.**
- [ ] **Step 5: Commit** `fix(print): warn when a by-object plate holds one object (#6245)`.

### Task 4b: Split a template plate into objects (issue #6245)

**Files:** Modify `backend/scadbuddy/workflows/outputs.py`, `backend/scadbuddy/render/split.py`; Modify `docs/superpowers/specs/2026-10-13-declared-objects-design.md` §2 (one line pointing at the new rule); Test `backend/tests/render/test_template_objects.py`.

**Interfaces:** Produces `group_shells(parts: Sequence[ColourPart], gap_mm: float = GAP_MM) -> list[list[ColourPart]]`.

- [ ] **Step 1: Tests** — `test_twelve_candles_twelve_objects` (12 separate single-colour shells 10 mm apart → 12 objects); `test_letter_dot_one_object` (an "i" whose dot is 2 mm above → 1 object); `test_declared_objects_win` (with `echo(objects = 3)` → 3 objects whatever the shells).
- [ ] **Step 2: Run, fail. Step 3: Implement** (for every print, by layer too; spec §6.2) with trimesh `split(only_watertight=False)` per colour, then union-find over shells whose 2D hulls are closer than `gap_mm`. **Step 4: Run, pass.**
- [ ] **Step 5: Run the template checks** over `models/` (`models/CLAUDE.md`) and list in the PR body every template whose object count changed.
- [ ] **Step 6: Commit** `fix(render): a template plate is written as its objects (#6245)`.

## Phase 2: Arrange and checks

### Task 5: The print check uses the rules (issue #6246)

**Files:** Modify `backend/scadbuddy/bambuddy/clearance.py` (call `plate_rules.check_plate` with hulls from `objects3mf.plate_boxes` grown to hulls); Test `backend/tests/bambuddy/test_clearance.py`.

- [ ] **Step 1: Test** `test_h2c_reads_max_radius`: two objects 60 mm apart on the H2C, by object → warning (it passed with 49 mm before). **Steps 2–4:** fail, implement, pass.
- [ ] **Step 5: Commit** `fix(print): the by-object check reads extruder_clearance_max_radius and the Y band (#6246)`.

### Task 6: Arrange's by-object goal (issue #6246)

**Files:** Modify `backend/scadbuddy/workflows/arrange.py` (goal `by_object`), `backend/scadbuddy/workflows/models.py` (`PackRequest.sequence`, `skirt_mm`, `printer_model`), `agent/src/tools/arrange.ts` (`goal: 'by_object'`, `process_overrides`), `frontend/src/components/ArrangeDialog.tsx`; Test `backend/tests/test_arrange_by_object.py`.

**Interfaces:** Consumes `plate_rules.check_plate`, `obj_distance`, `clearances_for`.

- [ ] **Step 1: Tests** — `test_menorah_three_plates`: the ORD-2026-2 parts (10 candles 65.5–83.5 mm, flames, plug 27 mm, base 29 mm, cradle 54 mm) on the H2C → at most 5 tall candles a plate, `check_plate` empty on every plate. `test_tall_last`: on each plate the tallest object is last in `DocPlate.objects`. `test_keeps_out_of_exclusion`.
- [ ] **Steps 2–4:** fail, implement (pack with the grown hull's bounding box as the footprint; assign Y bands to objects taller than `height_to_rod`; open a new plate when no band is free), pass.
- [ ] **Step 5: Commit** `feat(arrange): lay out for print by object (#6246)`.

### Task 7: Arrange reserves the real prime tower (issue #3770)

As #3770's body says. Additional requirement from this epic: the tower model answers `has_tower(settings, filaments_used, timelapse, wrapping_detection) -> bool` by spec §5.3, and `plate_rules.check_plate` takes the tower's footprint as one more object. Commit `feat(arrange): reserve the real prime-tower footprint (#3770)`.

## Phase 3: The viewer and editor

### Task 8: The operations engine and the save route (issue #6276)

**Files:**
- Create: `backend/scadbuddy/render/plate_ops.py`
- Modify: `backend/scadbuddy/api/outputs.py` (`POST /api/v1/plate-edits` (body `source: {output_id} | {library_file_id}`, optional `order_item_id`), `POST /api/v1/plate-rules/check`)
- Test: `backend/tests/render/test_plate_ops.py`, `backend/tests/api/test_plate_edits.py`

**Interfaces:**
- Consumes: Task 3's `PlateDocument`, Task 2's `check_plate`.
- Produces: `PlateOp` (a pydantic discriminated union on `op`: `move{ids, delta}`, `rotate{ids, axis, degrees, about}`, `scale{ids, factors}`, `lay_on_face{id, face_normal}`, `drop_to_bed{ids}`, `to_plate{ids, plate}`, `delete{ids}`, `duplicate{id, count}`, `split_to_objects{id}`, `split_to_parts{id}`, `merge{ids}`, `set_filament{part, slot}`, `set_settings{id, values}`, `clear_settings{id, keys}`, `group{ids, name}`, `ungroup{id}`, `reorder{plate, objects}`, `arrange{plate, pinned}`); `apply_ops(doc: PlateDocument, ops: Sequence[PlateOp]) -> PlateDocument`; route body `{ops: list[PlateOp], print: PrintSettingsRef | None}` → `{output_id: str, warnings: list[RuleWarning]}`.

- [ ] **Step 1: Tests** — one per op (ids kept; new ids for new objects; `drop_to_bed` puts min z at 0; split and merge write new mesh blobs); `test_source_unchanged`; `test_library_file_source`; `test_order_item_revision_only_with_order_item_id`; `test_two_saves_two_outputs` (Review Focus 3); `test_storefront_role_refused` (403 for a storefront principal, Review Focus 5); `test_unknown_setting_saved_with_warning` (Review Focus 2).
- [ ] **Steps 2–4:** fail, implement, pass (`cd backend && uv run pytest tests/render/test_plate_ops.py tests/api/test_plate_edits.py -v`).
- [ ] **Step 5: Commit** `feat(api): plate edits as an operation list, saved as a new output (#6276)`.

### Task 9: `PlateEditor`: capabilities, selection, object list (issue #6277)

**Files:**
- Create: `frontend/src/components/plate/PlateEditor.tsx`, `capabilities.ts`, `ObjectList.tsx`, `useSelection.ts`, `PlateViewport.tsx`
- Modify: `frontend/src/components/prints/PrintFiles.tsx`, `frontend/src/components/library/LibraryFileDetails.tsx`, `frontend/src/pages/PrintDetailPage.tsx` (read-only hosts move to `PlateEditor` with `{select: 'both'}`)
- Test: `frontend/src/components/plate/PlateEditor.test.tsx`, `useSelection.test.ts`, `e2e/plate-editor.spec.ts`

**Interfaces:**
- Consumes: `GET /api/v1/outputs/{id}/plate-document`.
- Produces: `type Capabilities` (spec §4.1, verbatim); `<PlateEditor outputId={string} capabilities={Capabilities} onSave?={(outputId: string) => void} />`; `useSelection(): {selected: Set<string>, mode: 'object' | 'part', click(id, {shift, alt}), clear()}`; `pendingOps: PlateOp[]` with `undo()`, `redo()`.

- [ ] **Step 1: Tests** — `renders no control for a capability that is off` (with `{}` → no toolbar buttons, no settings panel); `click selects object, second click selects part`; `shift-click adds`; `list and viewport share selection`; `settings marker shows on an object with settings`.
- [ ] **Steps 2–4:** fail, implement, pass (`cd frontend && pnpm gen:api && pnpm exec vitest src/components/plate`).
- [ ] **Step 5: Commit** `feat(ui): one plate viewer with host capabilities and selection (#6277)`.

### Task 10: Transform tools (issue #6278)

**Files:** Create `frontend/src/components/plate/TransformTools.tsx`, `LayOnFace.tsx`; Modify `PlateEditor.tsx` (toolbar, save, undo/redo, keyboard: `M`, `R`, `S`, `F`, `Delete`, `Ctrl+Z`, `Ctrl+Shift+Z`); Modify `frontend/src/components/print/PreviewPane.tsx`, `ArrangedPlates.tsx` (back-office hosts get all capabilities); Test `TransformTools.test.tsx`, `e2e/plate-editor-transform.spec.ts`.

- [ ] **Step 1: Tests** — `move emits a move op with typed mm`; `rotate snaps to 15 degrees with shift`; `lay on face emits lay_on_face with the clicked normal`; `save posts pendingOps and calls onSave with the new id`; `undo removes the last op`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(ui): move, rotate, scale and lay on face on the plate (#6278)`.

### Task 11: Object and part operations (issue #6279)

**Files:** Create `frontend/src/components/plate/ObjectActions.tsx`; Modify `ObjectList.tsx` (context menu: delete, duplicate, split to objects, split to parts, merge, filament); Test `ObjectActions.test.tsx`.

- [ ] **Step 1: Tests** — one per action, each emitting its op; `filament menu lists the plate's slots with their spool colours`; `actions hidden when the capability is off`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(ui): delete, duplicate, split, merge and filament per part (#6279)`.

### Task 12: Assemblies and print order (issue #6280)

**Files:** Modify `ObjectList.tsx` (drag to reorder, group, ungroup); Create `frontend/src/lib/plateRules.ts` (TypeScript port of `plate_rules.check_plate`, run against `backend/tests/render/vectors/plate_rules/*.json`); Test `plateRules.test.ts`, `ObjectList.order.test.tsx`.

- [ ] **Step 1: Tests** — every vector file passes in TypeScript; `drag reorders and emits reorder`; `order change re-runs the rules and marks a too-tall object`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(ui): assemblies and print order in the object list (#6280)`.

### Task 13: Per-object and per-part settings, any key (issue #6253)

**Files:** Modify `backend/scadbuddy/render/bambu3mf.py` (`set_part_settings` → `set_settings(doc)` for objects and parts, any key), `backend/scadbuddy/bambuddy/part_settings.py` (no key list); Create `frontend/src/components/plate/SettingsPanel.tsx` (reuses #2998's settings list; shows the level that set each value, spec §7.4); Test `backend/tests/render/test_object_settings.py`, `SettingsPanel.test.tsx`.

**Interfaces:** Consumes Task 1's `search`, `SettingDef.scope`; Task 8's `set_settings` op.

- [ ] **Step 1: Tests** — `test_any_key_written` (`{"brim_type": "no_brim", "x_made_up": 1}` on `o2` → both in that object's metadata); `test_print_scope_key_warns` (`enable_prime_tower` on an object → warning "applies to the whole print only"); `test_precedence` (part beats object beats slot beats print); `panel shows which level set the value`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(print): slicer settings per object and per part, any key (#6253)`.

### Task 14: The settings list shows the catalogue (issue #6281)

**Files:** Modify the #2998 settings list component (path as #2998's PR names it); Test its test file.

- [ ] **Step 1: Tests** — `shows Bambu Studio label and section`; `filter by section`; `help link opens the wiki path`; `develop-mode key has a badge`; `unknown key still editable`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(print): labels, sections and help links in the settings list (#6281)`.

### Task 15: Live clearance halos (issue #3772)

As #3772's body says, on `PlateEditor` with `plateRules.ts` (Task 12) and the tower footprint (Task 7). Commit `feat(arrange): clearance halos while dragging (#3772)`.

### Task 16: Pin objects and pack around them (issue #2330)

As #2330's body says, through the `arrange{plate, pinned}` op (Task 8). Commit `feat(arrange): pinned placements, packer fills around (#2330)`.

### Task 17: Storefront and quote-link hosts (issue #6282)

**Files:** Modify `frontend/src/storefront/pages/CustomizePage.tsx`, `frontend/src/storefront/pages/OrderPage.tsx` (`PlateEditor` with `{select: 'parts', recolour: listing.allowsRecolour}`); Test their tests and `e2e/storefront-*.spec.ts`.

- [ ] **Step 1: Tests** — `customer can select a part and recolour from the allowed list`; `no transform, settings or delete controls render`; `recolour goes to the storefront route, never plate-edits`.
- [ ] **Steps 2–4.** **Step 5: Commit** `feat(storefront): the shared plate viewer with select and recolour (#6282)`.

## Phase 4: Remembered settings (#6219)

Each task is one PR, `Part of #6219`, ticking its boxes in #6219's body.

### Task 18: Dialog steps are remembered

Supports, prime tower, nozzle side per slot, filament preset overrides (kept across a nozzle change when the key still applies), profile switch-offs, rack position, spools always. **Files:** `backend/scadbuddy/library/settings_store.py` `ModelPrintChoices` (new optional fields, default "nothing remembered"), `frontend/src/lib/useRunPrint.ts`, the step components in `frontend/src/components/print/`. **Test:** one round-trip test per field in `backend/tests/api/test_settings_remembered.py` and one dialog test each ("opens with the remembered value"). Commit `feat(print): remember every dialog step (#6219)`.

### Task 19: Remembered on every print, and the dialog's other choices

Print options and copies saved on print (not only by Remember); scope selector opens on the last scope; project and library folder; plates to print; Arrange goal; bed type: the remembered choice wins over the latest archive (`bambuddy/choices.py:301-305`). Commit `feat(print): remember options, scope, folder, plates and goal on print (#6219)`.

### Task 20: Batch, quote and order-item prints write their choices

Queue all writes each file's choices and the bed type; the quote dialog writes the model's choices and support-slot filaments; the order-item print writes them. Commit `feat(orders): batch, quote and item prints remember their choices (#6219)`.

## Phase 5: Agent parity

### Task 21: Plate tools for the assistant (issue #6283)

**Files:** Create `agent/src/tools/plates.ts` (`get_plate_document`, `edit_plate`, `check_plate`); Modify `agent/src/tools/index.ts`; Test `agent/src/tools/plates.test.ts`, an eval in `agent/evals/` ("put the tall candles on their own plate and print by object").

**Interfaces:** Consumes Task 8's routes and `PlateOp` schema (the zod input mirrors it; a test compares the zod enum of `op` with the OpenAPI schema's discriminator values).

- [ ] **Step 1: Tests** — `edit_plate sends ops and returns output_id and warnings`; `edit_plate is not a pending action` (`risk: 'write'`); `op names match the API`.
- [ ] **Steps 2–4:** `cd agent && pnpm test src/tools/plates.test.ts`. **Step 5: Commit** `feat(agent): get, edit and check a plate with the UI's ids (#6283)`.

## Phase 6: Acceptance

### Task 22: ORD-2026-2 from end to end (issue #6284)

**Files:** Create `e2e-stack/tests/test_menorah_by_object.py` (real stack, native slicer); Modify `docs/` user guide page for the editor.

- [ ] **Step 1:** Render the menorah template, Arrange with `goal='by_object'` on the H2C, set `enable_prime_tower: false` and `skirt_loops: 0` through `process_overrides`, give one candle `wall_loops: 4` through `set_settings`, slice every plate.
- [ ] **Step 2:** Assert: no conflict warning from the slicer; each plate's slice time within 20 % of the owner's hand layout (5.5 h class, #6245); `o*` ids identical between the document and the sliced 3MF's metadata.
- [ ] **Step 3: Commit** `test(e2e): the menorah prints by object from Arrange to slice (#6284)`.

## Dependency order

```text
#6273 ─┬─> #6281 (also needs #2998)
          └─> #6253
#6274 ─┬─> #6246 ─> #2330
            ├─> #6276
            ├─> #3772
            └─> #6283
#6275 ─┬─> #6276 ─┬─> #6278 ─> #2330
          │             ├─> #6279 ─> #6280
          │             ├─> #6253
          │             └─> #6283
          └─> #6277 ─┬─> #6278, #6279, #3772, #6282, #6253
#3770 ─> #3772
#2998 ─> #6253, #6281
#6245, #6246, #6253, #6278, #6283 ─> #6284
#6218, #6219: no blockers
```
