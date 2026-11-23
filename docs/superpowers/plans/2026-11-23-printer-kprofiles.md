# A printer's K-profiles through Bambuddy (#7154)

ScadBuddy (SB) shows and manages a printer's K-profiles (pressure advance) through
Bambuddy (BB), as BB does: list, set one, batch set, delete, and notes. Before this, SB read
BB's K table only to fill a filament type's K values (`filament/profile_fill.py`,
`profile_fetch.py`). Gap found by the §11 parity sweep (#6270; spec
`docs/superpowers/specs/2026-11-14-filament-management-design.md` §11, line "K-profiles on
the printer: list, set, batch set, delete, notes"). Epic #6260.

## What BB does, and where it was read

The OpenAPI fixture `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json` lists the seven
routes with no bodies, and no recording holds one. So the bodies were read from BB's source
at tag `v1.3.1` (commit `d3be761f`), the version prod runs
(`backend/tests/bambuddy/recordings/README.md`, `rfid-routes.json`). No call reached a live
Bambuddy.

- Routes: [`backend/app/api/routes/kprofiles.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/kprofiles.py).
  `GET /printers/{id}/kprofiles/?nozzle_diameter=` (default `0.4`) answers
  `KProfilesResponse`. `POST /printers/{id}/kprofiles/` takes one `KProfileCreate`: a
  `slot_id` of 0 adds, above 0 edits (in place on a dual-nozzle printer, else a delete
  then an add, read back). `POST …/batch` takes a list of `KProfileCreate`, sends them in
  one MQTT command with the first row's `nozzle_diameter`, and refuses an empty list (400).
  `DELETE /printers/{id}/kprofiles/` takes a `KProfileDelete` body. Each write answers
  `{success, confirmed, message}`; `confirmed` is false when the printer sent no
  acknowledgement in time. `GET …/notes` answers `{notes: {setting_id: note}}`; `PUT
  …/notes` takes `{setting_id, note}` and deletes the note when `note` is blank;
  `DELETE …/notes/{setting_id}` answers `{success, message}`. Notes live in BB's database,
  not on the printer. A printer BB is not connected to is a 400; a printer that does not
  answer the read is a 504; a refused write is a 500 naming the printer's reason.
- Bodies: [`backend/app/schemas/kprofile.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/schemas/kprofile.py).
  `KProfile` and `KProfileCreate`: `slot_id`, `extruder_id`, `nozzle_id`, `nozzle_diameter`,
  `filament_id`, `name`, `k_value` (a string, e.g. `"0.020000"`), `n_coef`, `ams_id`,
  `tray_id`, `setting_id`. `KProfileDelete`: `slot_id`, `extruder_id`, `nozzle_id`,
  `nozzle_diameter`, `filament_id`, `setting_id`.
- Timing: the printer's acknowledgement is awaited up to 6 s (`await_cali_ack`) and a read
  up to 5 s, three tries (`get_kprofiles`), in
  [`backend/app/services/bambu_mqtt.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/services/bambu_mqtt.py)
  lines 7542 and 7636. An edit on a single-nozzle printer waits for two acknowledgements
  and one read, about 28 s at worst.
- Permissions: `KPROFILES_READ` maps to the API key's `can_read_status`, and
  `KPROFILES_CREATE`, `KPROFILES_UPDATE` and `KPROFILES_DELETE` are in
  `_APIKEY_DENIED_PERMISSIONS`
  ([`backend/app/core/auth.py`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py)
  lines 114 and 305-307). So no BB API key can write a K-profile or a note while BB's auth
  is on; with BB's auth off the key is not checked.
- BB's own page: [`frontend/src/components/KProfilesView.tsx`](https://github.com/maziggy/bambuddy/blob/v1.3.1/frontend/src/components/KProfilesView.tsx).
  It sends `k_value` with six decimals, builds `nozzle_id` as the flow type and the
  diameter (`HS00-0.4` standard, `HH00-0.4` high flow), adds a new profile to both
  extruders of a dual-nozzle printer with the batch route, and keys a note by the
  profile's `setting_id`, else `slot_<slot>_<filament>_<extruder>`, else
  `name_<name>_<filament>`.

## Rulings

Each is posted on #7154.

- **R-7154-1 (the source of the bodies).** The bodies are BB's source at `v1.3.1`, cited
  above, because the repo holds none. The 1.2.6b1 fixture's routes are the same seven.
- **R-7154-2 (which printer).** SB's routes are keyed by SB's registry printer id
  (`/printers/{printer_id}/kprofiles…`), never by a BB printer id from the caller. The
  printer must be a physical row linked to a BB printer: an unknown id is a 404, a BB
  virtual printer or an unlinked row a 409. The BB id is read from the row.
- **R-7154-3 (BB's scope).** The read takes `Read Status`. Every write (set, batch,
  delete, note set, note delete) takes a new admin-only scope, `Scope.ADMIN_KPROFILES`
  ("K-profiles (admin only)"), as `ADMIN_SETTINGS` and `ADMIN_FILAMENTS` do: BB's 403 is
  SB's 409 scope problem that says no BB API key can change a printer's K-profiles while
  BB's auth is on.
- **R-7154-4 (SB's guard).** The writes go as SB's existing printer writes through BB do
  (`assign_tray_spool`, the virtual printers): operations on the `bambuddy` queue, so the
  print worker makes the call, each route needs an `Idempotency-Key` (428 without), and
  each write is recorded with its author. The routes are of class `api`, so in `enforce` a
  `read` access token cannot send them (`auth/routes.py` `SAFE_METHODS`).
- **R-7154-5 (no repeat).** BB dedupes no K-profile write: a second add makes a second
  profile. So set, batch and delete run once (`run_attempts` 1); SB never retries them,
  and a write whose answer was lost says to check the list before sending it again. A
  note's set and delete are upserts and run up to 3 times.
- **R-7154-6 (what SB accepts).** Bodies forbid unknown fields. `k_value` is a decimal from
  0 to under 10 with at most 6 places, sent with 6 places as BB's page does.
  `nozzle_diameter` is one of `0.2`, `0.4`, `0.6`, `0.8`. `nozzle_id` is two capitals, two
  digits, a dash and a diameter (`HS00-0.4`), or as the printer reported it: a flow code
  alone or empty (review of #7173). `filament_id` is 1-64 of `[A-Za-z0-9_.+-]`;
  `setting_id` is 0-64 of the same, so an empty one from the list goes back. A set takes
  the row's `n_coef`, `ams_id` and `tray_id`, so an edit sends what the printer reported. `name` is 1-64 printable characters. `extruder_id` is 0 or 1,
  `slot_id` at least 0. A batch is 1-16 rows of one `nozzle_diameter` (BB sends the
  first row's for all). A note is at most 2,000 characters; a note key 1-256 printable
  characters with no `/`.
- **R-7154-7 (the answer).** A write answers BB's `{success, confirmed, message}`
  unchanged. `confirmed: false` is shown as "sent, the printer did not confirm it", never
  as saved. Writes take a 60 s timeout (an edit can take about 28 s on BB).
- **R-7154-8 (notes stay in BB).** SB keeps no copy of the K table or the notes: no
  table, no migration. BB's note keys are used as BB's page makes them, so a note made in
  either UI shows in both.
- **R-7154-9 (the page).** The K table is on the printer's card in Settings → Printers,
  behind a "K-profiles" disclosure that reads BB only when opened (each read asks the
  printer over MQTT). It has the nozzle choice, the table (name, filament, extruder, flow,
  K, note), edit, add (to both extruders, through the batch route, on a printer BB says
  has two nozzles), delete after a confirm, and the note per row.
- **R-7154-10 (the assistant).** No agent tool in this change. The new routes are in the
  agent's `NOT_A_TOOL` list with the reason, and a follow-up issue tracks the tools.

## Tasks

1. Client: the six calls, the K row's three missing fields, `Scope.ADMIN_KPROFILES`
   (`bambuddy/client.py`, `models.py`, `errors.py`). respx tests.
2. Operations and routes: `bambuddy/kprofiles.py` (the request models and the five kinds),
   `api/printer_kprofiles.py` (two reads, five writes). respx tests through the API.
3. Frontend: `components/settings/PrinterKProfiles.tsx` on the printer's card, the client
   calls, msw mocks `mocks/features/printerKProfiles.ts`, vitest.
4. Agent: `NOT_A_TOOL` entries. Docs: `docs/features/spools-and-filament.md`.
