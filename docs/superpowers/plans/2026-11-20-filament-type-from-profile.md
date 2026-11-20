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
    `drying_temp_c` and `drying_hours`), so they are skipped and no column is added;
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
  here.
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
  once even when the re-read finds no backwards pair; a second race skips what is left; a type the re-read finds gone is not retried, and every wanted field is skipped with "the type was deleted meanwhile", #6787; a
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
