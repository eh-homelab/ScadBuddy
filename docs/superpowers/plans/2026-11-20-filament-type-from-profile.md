# Fill spool types from their slicer profiles (#6714)

Goal: one resolver reads a spool type's slicer filament profile and fills the type's
empty print values from it (nozzle and bed temperatures, density, diameter), and imports
Bambuddy spool K values into a type that has none. A route for one type, a bulk route,
an agent tool, a button on the type page and a bulk action on the types list. A type is
filled the same way after it is made.

## What was measured (2026-11-20)

Read from the live Bambuddy and the API image's profile tree with GET requests only, by
`kubectl --context primary -n bambuddy exec deploy/scadbuddy -c scadbuddy -- python3 -c …`
inside the pod (the key was read from the pod's environment and never printed).

- `GET /api/v1/local-presets/`: 76 filament presets, all 3DFP imports. A row has
  `compatible_printers` (JSON text), `filament_cost`, `filament_density`,
  `nozzle_temp_min/max`, `pressure_advance`, `inherits`. One preset per nozzle:
  id 2 `Cookiecad PETG Magic Dark Magic (3DFP 7JdoWkaDB) @H2C` is
  `["Bambu Lab H2C 0.4 nozzle"]`, id 1 `… @H2C 0.2n`, id 3 `… @H2C 0.6n`.
- `GET /api/v1/local-presets/2` `setting` (inherits `Generic PETG @BBL H2C 0.4 nozzle`):
  `nozzle_temperature_range_low ["220"]`, `nozzle_temperature_range_high ["240"]`,
  `filament_density ["1.275"]`, `filament_diameter ["1.75"]`, `filament_cost ["30"]`,
  `cool_plate_temp ["0"]`, `eng_plate_temp ["70"]`, `hot_plate_temp ["80"]`,
  `textured_plate_temp ["80"]`, `supertack_plate_temp ["70"]`,
  `filament_dev_ams_drying_temperature ["65","65","55","55"]`,
  `filament_dev_ams_drying_time ["12","12","12","12"]`,
  `filament_dev_chamber_drying_time ["12.0"]`, `filament_dev_chamber_drying_bed_temperature ["80"]`.
- The image's tree (Bambu Studio 02.08.02.61, `/opt/slicer-profiles`): `GFA05` names 30
  presets. `Bambu PLA Silk @BBL H2C` is for the H2C 0.6 and 0.8 nozzles; `Bambu PLA Silk
  @BBL H2C 0.4 nozzle` is for `["Bambu Lab H2C 0.4 nozzle"]`. Flattened, the 0.4 one has
  `nozzle_temperature_range_low ["190"]`, `_high ["240"]`, `filament_density ["1.32"]`,
  `filament_diameter ["1.75"]`, `filament_cost ["21.99"]`, `cool_plate_temp ["35"]`,
  `eng_plate_temp ["55"]`, `hot_plate_temp ["55"]`, `textured_plate_temp ["55"]`,
  `supertack_plate_temp ["0"]`, no `textured_cool_plate_temp`, no `pressure_advance`, and
  the same drying keys (`["45"…]`, `["12"…]`).
- `BBL/machine/Bambu Lab H2C.json`: `"not_support_bed_type": "Cool Plate;Smooth PEI
  Plate / High Temp Plate"`, `"default_bed_type": "Textured PEI Plate"`. So the H2C's
  plates are Cool Plate (SuperTack), Engineering Plate and Textured PEI Plate:
  `supertack_plate_temp`, `eng_plate_temp`, `textured_plate_temp`. A plate temperature of
  `0` means the filament is not for that plate.
- `GET /api/v1/inventory/spools`: 106 spools, none with `k_profiles`. The OpenAPI
  `SpoolKProfileResponse` has `printer_id`, `extruder`, `nozzle_diameter`, `nozzle_type`,
  `k_value`, `name`, `cali_idx`, `setting_id`.
- Printer K calibrations: Bambuddy exposes them, `GET /api/v1/printers/{id}/kprofiles/
  ?nozzle_diameter=0.4` (`KProfilesResponse`: `profiles` of `slot_id`, `extruder_id`,
  `nozzle_id`, `nozzle_diameter`, `filament_id`, `name`, `k_value`, `n_coef`, `ams_id`,
  `tray_id`, `setting_id`). Printer 1 (`3DP-31B-598`, H2C) answered 0 profiles at 0.4.

## Decisions

Posted on #6714.

- R-6714-1: preset choice: the type's preset for printer model H2C, nozzle 0.4; else any
  0.4 mm preset; else the first one.
- R-6714-2: the source order is the same as `orders/density.py`: Bambuddy's local preset
  when the id is numeric, else the system profile (by `filament_id`, then `setting_id`),
  with `inherits` resolved. A failed or missing read leaves the fields alone and reports
  the reason. A system `filament_id` names one preset per printer; the one whose
  `compatible_printers` holds `Bambu Lab <model> <nozzle> nozzle` is read, else its
  `@base`. A local preset's `setting` is read over its `inherits` preset in the tree,
  when the tree has it.
- R-6714-3: mapping:
  - `nozzle_temp_min/max` from `nozzle_temperature_range_low/high`;
  - `bed_temp_min/max`: the min and max of `supertack_plate_temp`, `eng_plate_temp` and
    `textured_plate_temp` (the H2C's plates, measured above), values above 0 only;
  - `density_g_cm3` from `filament_density` (0 is none);
  - `diameter_mm` from `filament_diameter`. The column is not null (default 1.75), so it
    is never empty: only a refresh changes it;
  - drying: the type model has no drying fields (only materials and subtypes have
    `drying_temp_c` and `drying_hours`), so they are skipped and no column is added
    (superseded by R-6714-9, #6720);
  - `filament_cost` is reported as a hint (`cost_hint`), never written.
  List values take their first element; empty or `nil` values are skipped. A pair whose
  filled value would put a minimum above its maximum is not filled, and says why.
- R-6714-4: only empty type fields are filled. The bulk and single routes return per
  type `{filled: {field: value}, source: {preset_id, name, origin}}`. `refresh: true`
  returns the diff (with `previous`) without writing, and `apply` with the confirmed
  fields and values writes them, after checking that the profile still gives them.
- R-6714-5: K values: Bambuddy spool `k_profiles` of the BB spools linked to the type's
  spools become the type's K values when it has none (one row per printer, extruder and
  nozzle, the table's key).
  A BB printer is the ScadBuddy printer linked to it; a K profile of an unlinked printer
  is skipped. Bambuddy does expose printer K calibrations (above), but printer 1 had
  none on 2026-11-20 and they name a `filament_id`, not a spool; they are not imported
  here (#6720 imports them, R-6714-12).
- R-6714-6: a type is filled after it is made: `POST /filament/types` (makeTypeFromBook,
  the type page), the first `PUT /filament/types/{id}/presets` of a type with none
  (makeTypeFromSpool and the type page set presets after the type), and an invoice
  confirm, for each type a line proposed. In the backend, after the write. The Bambuddy
  pull (`filament/bambuddy_pull.py`) makes no type (it types a spool by an existing one,
  or leaves it needing one), so it has no hook; a type made from such a spool later is
  filled by the hooks above.
- R-6714-7: the routes are writes; the agent tool is on the write tier, with a
  `touched.ts` entry (`filament_type_fill_from_profile`, one tool for one type or all).
  The type page has "Fill from slicer profile", which shows the diff and asks to confirm
  each row (and the K values, sent as `apply` with `k_values: true`); the types list has
  "Fill from slicer profiles", which fills empty fields and lists what it did per type.
- R-6714-a (supersedes the create and invoice-confirm parts of R-6714-6): `POST
  /filament/types` and an invoice confirm no longer fill, because a type made has no
  preset and nothing to read. A type is filled when its first presets arrive: in
  `PUT /filament/types/{id}/presets` for a type with none, and in the 3DFP profile-fetch
  import (`profile_fetch.py`, after its commit) for a type with none. Empty fields only.
- R-6714-b: the fill after a write runs after the commit. A fill that raises is logged
  (`fill_quietly`, label `error` = the exception's class name) and never fails the request.
- R-6714-c: `fill_k_values` checks that the type has no K values and writes them in one
  transaction, under the row lock `set_k_values` takes (`_replace_k_values`).
- R-6714-d: `read_profile` spells the nozzle as the presets do (`canonical_nozzle`:
  `0.40` is `0.4`) when it names the printer, so `0.40` reads the H2C 0.4 mm preset.
- R-6714-e (#6747): the bulk route's fill catches `TypeTempError` for each type in
  `TypeFiller._one` (a person set one end of a range between the read and the row lock)
  and records the pair that is now backwards in `skipped` with the reason (#6762: it
  re-reads the row, drops only that pair and writes the other fields once more, and retries
  once even when the re-read finds no backwards pair; a second race skips what is left; a type the re-read finds gone is not retried, and every wanted field is skipped with "the type was deleted meanwhile", and the K-value read and write stop too, not only the field fill, on a flag and not on that text, #6787, #6800; a
  field a person set between the read and the lock counts as filled by them and is not in
  `skipped`, as on the path with no race). One type's race never answers 500, and the types after it are
  still filled.
- R-6714-f (#6747): `fill_quietly` takes the filler as a callable, so the profile-fetch
  import's `state.components.get(FILAMENT)` lookup runs inside its guard: nothing after the
  commit fails the import.
- R-6714-g (#6747): `TypeFiller.apply` with K values compares the K values it would write
  with the ones the refresh showed (`FillRequest.shown_k_values`), as it compares fields.
  When they differ it refuses (`ProfileChangedError`, 409) before anything is written.
- R-6714-h (#6747): R-6714-5 no longer says "and nozzle type": the K values are one row
  per printer, extruder and nozzle, the table's key.
- R-6714-i (review of #6750): `FillRequest.shown_k_values` defaults to `None`, and a
  `model_validator` answers 422 when `k_values` is true and it was not sent; `[]` stays
  valid and says the refresh showed none. The agent tool's input has the matching
  `.refine`.
- R-6714-j (review of #6750): route tests send `shown_k_values` through
  `POST /filament/types/{id}/fill-from-profile`: a match writes (200), a difference is 409,
  and with `k_values: false` they are ignored.
- R-6714-k (review of #6750): the frontend mock of the fill answers 409 when the sent
  `shown_k_values` differ from its own (`setMockBambuddyKValues`), and a
  `ProfileFillDialog` test shows the error.
- R-6714-8: tests use respx and fixture profile JSON; they never call a live service.

### #6720: drying values and printer K calibrations

Measured on 2026-11-21, read only:

- The API image's tree (`/opt/slicer-profiles`, Bambu Studio 02.08.02.61), read in the
  `scadbuddy` API pod with `kubectl -n bambuddy exec deploy/scadbuddy -c scadbuddy -- python3`
  over `scadbuddy.slicing.profiles.Profiles` (the slicer pod's image has no
  `/opt/slicer-profiles`). `BBL/filament` names 8 drying keys:
  `filament_dev_ams_drying_temperature` (170 files), `filament_dev_ams_drying_time` (163),
  `filament_dev_ams_drying_heat_distortion_temperature`, `filament_dev_ams_drying_ams_limitations`,
  `filament_dev_chamber_drying_bed_temperature`, `filament_dev_chamber_drying_time`,
  `filament_dev_drying_cooling_temperature` and `filament_dev_drying_softening_temperature`.
  `fdm_filament_common` sets every one (temperature `["40.0"×4]`, time `["8.0"×4]`). Of the 68
  `* @BBL H2C 0.4 nozzle` presets, 66 flatten (two support presets name a parent the tree
  lacks) and all 66 carry the AMS temperature and time, four values each: 22 PLA-like
  `["45"×4]`/`["12"×4]`, 10 `["65","85","65","85"]`/`["12"×4]`, 8 PETG-like
  `["65","65","55","55"]`/`["12"×4]`, 6 TPU-like `["65","75","45","45"]`/`["12","18","12","18"]`,
  5 ABS-like `["65","80","65","80"]`/`["12","8.0","12","8.0"]`, and smaller groups. Examples:
  `Generic PETG @BBL H2C 0.4 nozzle` (`GFG99`) `["65","65","55","55"]`, `["12"×4]`, chamber bed
  `["80"]`, chamber time `["12.0"]`; `Bambu PLA Silk @BBL H2C 0.4 nozzle` (`GFA05`) `["45"×4]`,
  `["12"×4]`; `Bambu TPU 95A HF @BBL H2C 0.4 nozzle` (`GFU00`) `["65","75","45","45"]`,
  `["12","18","12","18"]`. Bambuddy local preset 2 (above) has the PETG values.
- What the four values are: Bambu Studio's `DevUtilBackend.cpp`
  ([`AMS_DRY_IDX_*`](https://github.com/bambulab/BambuStudio/blob/9cfdde3be60b20fe3ef4e3ec9b23ce8d55476616/src/slic3r/GUI/DeviceCore/DevUtilBackend.cpp#L66-L75)):
  `[0]` N3F idle, `[1]` N3S idle, `[2]` N3F printing, `[3]` N3S printing (and a 6-value layout
  for a newer AMS this tree does not use); `DevDefs.h`
  ([`N3F`, `N3S`](https://github.com/bambulab/BambuStudio/blob/1f57f20db62a13903315df8ac6c4f2388bbe357f/src/slic3r/GUI/DeviceCore/DevDefs.h#L84-L85))
  says N3F is the AMS 2 Pro and N3S the AMS HT. Bambu Studio reads none of them when the
  first temperature or time is 0 (`HasUsableAmsDryingValues`, same file).
- Bambuddy's K tables: `GET /api/v1/printers/` then `GET /api/v1/printers/1/kprofiles/
  ?nozzle_diameter=` 0.2, 0.4, 0.6 and 0.8, from inside the API pod with its key (never
  printed): printer 1 (H2C) answered 0 profiles at every nozzle.

Decisions:

- R-6714-9: drying: a type has `drying_temp_c` and `drying_hours` (migration
  `20261121T0840Z_filament_type_drying.sql`, the materials' column types). The fill maps them
  from `filament_dev_ams_drying_temperature` and `_time`, the second value (the AMS HT idle;
  a list of one value gives that one; 0 is none, hours above 0 and below 1000, rounded to
  0.01). The AMS HT's pair is the filament's own: the AMS 2 Pro's is capped at 65 °C (ABS
  65 vs 80, TPU 65 vs 75). The chamber drying keys (the H2C's heated-bed drying) and the
  cooling, softening and heat-distortion temperatures are not mapped: the type has one
  drying pair. Empty fields only, as every other field (R-6714-4).
- R-6714-10: a spool's drying defaults (`SpoolDetail.drying_defaults`, `spools.py`
  `_drying_defaults`): temperature and hours each from its type, else its subtype, else its
  material (the spool's own subtype and material override first, else the type's), with
  `temp_from` and `hours_from` naming the level. A `dried` event that leaves out `temp_c` or
  `hours` records that default; one that sends `null` records none. The spool page fills its
  drying form with them and says where they came from; a person changes them for one
  drying, and the form shows the defaults again after it records.
- R-6714-11 (overturned by R-6714-18): a spool does not override the drying values: the drying action is the
  per-spool choice (the owner's "every setting a remembered choice": the type remembers it,
  each drying may change it). So the drying fields are not `OVERRIDABLE`, a type's drying
  edit writes no `edited` event on its spools, and the spool page has no "Reset to type" for
  them.
- R-6714-12: printer K calibrations: for each of the type's presets, each ScadBuddy printer
  linked to Bambuddy whose `model` is the preset's printer model is asked once per fill for
  its K table at the preset's nozzle (`GET /printers/{id}/kprofiles/?nozzle_diameter=`,
  scope `READ_STATUS`, never a write). A calibration whose `filament_id` is the preset's
  `slicer_filament` becomes the type's row for that printer, its `extruder_id`, the preset's
  nozzle (spelled as the preset spells it, `canonical_nozzle`), with `nozzle_type` its
  `nozzle_id`, `name`, `cali_idx` its `slot_id` and `setting_id`. A Bambu system id (`GF…`)
  matches too: the printer keys a calibration by the filament id, so every type of that
  preset shares it (unlike the profile fetch, which skips `GF…` because it replaces rows,
  #6625). A local preset's numeric id never matches a `filament_id`, so such a type takes
  none. A K table that cannot be read is said in `k_reason`; the rest of the fill goes on.
- R-6714-13 (its "every existing row counts as a person's" overturned by R-6714-19): never a
  person's K value: a calibration fills only a key (printer, extruder,
  nozzle, `0.40` being `0.4`) the type has no row for, checked under the type's row lock in
  the write (`SpoolTypeStore.add_k_values`). Since no row says who set it, every existing row
  counts as a person's; a person who wants the printer's value deletes the row and fills
  again.
- R-6714-14: the table holds one row per printer, extruder and nozzle (R-6714-h), so
  calibrations of the filament for one extruder that differ in K or nozzle type are not
  guessed between: none is imported and `k_reason` names the count. Equal ones give one row.
- R-6714-15: order: a type with no K values takes its linked spools' first (R-6714-5,
  `fill_k_values`, all or none); the calibrations then fill the keys those leave
  (`add_k_values`). Both are in the refresh's `k_values`, and a fill after the type has rows
  still adds calibrations for keys it lacks.
- R-6714-16: an apply with `k_values` compares the spools' and the printers' rows together
  with `shown_k_values` (R-6714-g); a calibration changed since the refresh is a 409. The
  "the type now has K values of its own" reason is given only when nothing would be imported.
- R-6714-17: the type tools: `filament_type_upsert` takes `drying_temp_c` and `drying_hours`,
  `filament_type_fill_from_profile`'s `apply` takes both, and `filament_spool_add_event`
  leaves out `temp_c` and `hours` for the defaults (`null` records none). The type page has a
  Drying section; the fill dialog lists the drying rows and says its K values come from the
  spools and printers.

Decisions of the PR #6856 review (2026-11-21; the controller's rulings A and B, and the review's
findings):

- R-6714-18 (ruling A, overturns R-6714-11; the owner's rule: every value can be overridden on
  the instance): a spool has its own nullable `drying_temp_c` and `drying_hours`
  (a new migration, `20261121T1007Z_filament_spool_drying_k_source.sql`, since #6856 merged the first). They are `OVERRIDABLE`
  (`EffectiveValues`, `_INHERITED`): the effective value is the spool's, else the type's, else
  the subtype's, else the material's, and `drying_defaults` names the level (`temp_from` and
  `hours_from` take `spool` too). So a type's drying edit writes an `edited` event on each spool
  that does not override it, the spool page has "Default drying temperature (°C)" and "Default
  drying hours" with "Reset to type" in Storage, beside the drying form, and the agent's
  `filament_spool_upsert` and `filament_spool_get` say so. The CSV import keeps its columns.
- R-6714-19 (ruling B, overturns R-6714-13's "every existing row counts as a person's"): each
  type K row has a `source`: `person`, `profile` (the fill's import from the linked Bambuddy
  spools), `printer_calibration` or `profile_fetch` (the 3DFP fetch's `merge_k_values`, #6625).
  Rows made before the column are `person`. A printer calibration fills a key with no row, or
  replaces the `printer_calibration` row of its printer, extruder and nozzle when it differs (K,
  nozzle type, name, slot or setting); it never replaces a `person`, `profile` or
  `profile_fetch` row (`add_k_values`, under the type's row lock). The fetch's overwrite of any
  row of its keys is unchanged (#6857 decides it). A person's save (`PUT …/k-values`) tags each
  row it adds or changes `person` and keeps the source of a row sent as it was, so saving other
  rows does not turn a calibration into a person's. A write never takes `source` from a body:
  `TypeKValueCreate` has none, so a PUT sends rows without it (a `shown_k_values` row may carry
  it, and it is dropped: R-6714-22). The type
  page shows it beside a row's name ("Printer calibration", "From linked Bambuddy spools",
  "From 3DFP"; nothing for a person's).
- R-6714-20 (finding 4): the type comes first in a spool's drying defaults, so a fill that
  wrote a type's empty drying field would hide a value the type's subtype or material sets. An
  empty-field fill (the bulk fill, a type's fill, the fill after its first presets) leaves such
  a field empty and says why in `skipped`; a per-type refresh still offers it, and an apply
  writes it.
- R-6714-21 (findings 1 to 3): the K table is read with `BambuddyClient.k_profile_rows`: a
  body that is not JSON, or has no list of profiles, is a 502 `BAD_BODY_PROBLEM` (never a
  `JSONDecodeError` that answers 500; `k_profiles`, the fetch's read, goes through it too), and
  the fill validates the rows one by one, a row of another shape skipped and counted in
  `k_reason`. The spool page's drying line says a slicer profile gives the AMS HT's values and
  to lower them in an AMS 2 Pro (65 °C at most), since `DRYING_AT` stays the AMS HT's pair.

Decisions of the PR #6859 fix round (2026-11-21):

- R-6714-22: a `shown_k_values` row is `ShownKValue`: `TypeKValueCreate`'s fields, plus an
  optional `type_id` and `source` that are accepted and dropped (excluded from its dump), so a
  client that echoes the refresh's `k_values` as it got them is not refused with 422. The
  shown rows are only compared with what Bambuddy gives now (`_k_key`, which reads neither);
  the rows written are the server's own import, tagged by the server (`profile` or
  `printer_calibration`), so a client's `source` never reaches a stored row. A `PUT
  …/k-values` stays strict (`TypeKValueCreate`, `extra="forbid"`): a `source` there is a 422,
  never silently dropped, because a PUT is a person's write and the ruling keeps the body
  free of anything the server owns. One comparison of a K row's values, `types.py`
  `k_identity`, serves the fill's preview (`_printer_k`), `add_k_values` and `set_k_values`.
  The type page shows no source on a draft row whose values differ from its saved row (its
  save makes it the person's), and shows it again when the edit is undone.

## Tasks

1. `orders/density.py`: the table keeps each preset's `compatible_printers` and its
   profiles, and names the preset for a printer and nozzle.
2. `filament/profile_fill.py`: the preset choice, the mapping, the reads (local preset,
   tree), the fill, the refresh and the apply, the K value import.
3. Routes: `POST /filament/types/{id}/fill-from-profile`,
   `POST /filament/types/fill-from-profile`; the fill after a type's first presets (R-6714-a).
4. Frontend: the API client, the type page button and dialog, the types list bulk
   action, MSW mocks.
5. Agent: `filament_type_fill_from_profile` tool, write tier, `touched.ts`.
