# Spools and filament

## What it does

The **Filament** section is ScadBuddy's own filament inventory. It covers:

- spools and spool types, with tags, presets and K values;
- catalogs (brands, materials, colours and more);
- suppliers, price sheets, invoices and a shopping list;
- locations with humidity and temperature sensors;
- RFID tags, NTAG labels, and printed QR labels for spools, places, printers, AMS units and slots;
- a printer's K-profiles (pressure advance) over ScadBuddy's own LAN link: list, set, batch set,
  delete, and ScadBuddy's notes on them;
- a printer's AMS slots over ScadBuddy's own LAN link: configure a slot by hand, refresh and reset,
  load and unload; and ScadBuddy's own saved slicer preset per slot and label per AMS unit;
- a ledger of spool events, with history, metrics and a forecast;
- key-value properties on spools and spool types (#7026);
- unknown spools: a loaded tray that no spool holds is listed, blocks a print to its printer, and the owner links a spool to it (#7143).

The print dialog picks from these spools (ScadBuddy's own, "SB" spools). Tray changes and
finished prints are recorded against them. The inventory can sync with Bambuddy's in
either direction. A new spool type with a 3dfilamentprofiles.com id can have its slicer
presets fetched per printer model and nozzle. Filament profiles (#717) bundle print
settings for a filament. The design is the
[filament-management spec](../superpowers/specs/2026-11-14-filament-management-design.md).
The repo skill `.claude/skills/filament-intake` covers adding purchased spools.

## How it works

The spools and the ledger live in the `filament_*` tables, written by the API. The print
worker brings in Bambuddy's side:

- `FilamentBbSync` pushes to Bambuddy;
- `FilamentBbPull` reads from it;
- `SensorNotifyPass` sends location-sensor alerts through Bambuddy every minute.

On the API's `library` queue, `SpoolRefresh` runs on a Schedule. It refreshes the kept
presets, runs the pull, re-records any tray changes that were missed, and reconciles RFID
state.

A spool is made in three ways besides one at a time (#7150, plan
`docs/superpowers/plans/2026-11-23-spool-create-ops.md`):

- **Bulk create.** The spool form's "How many spools" (1 to 100) makes that many identical
  spools in one request, `POST /filament/spools/bulk`. Each gets its own number and its own
  `created` event. The place, presets and K values are written in the same transaction. The
  request needs an `Idempotency-Key`; the form sends one per opened form, so a retry or a
  second click makes no more spools. A bulk create never goes into a printer's tray.
- **Copy.** A spool's "Copy" button opens a new spool form filled from
  `GET /filament/spools/{id}/copy`. The copy keeps the type, the spool's own values, its
  note, presets, K values and shelf place. A preset row that a migration made stays such a
  row, so the copy's first type drops it as it would the source's (R-7150-11). The copy
  leaves out the number, tags, tray, weight history, weight lock and whether the source was
  added full (R-7150-10).
- **Create from slot.** "New spool from this tray" makes a spool from what the printer last
  reported in the tray over the LAN: its material, its `#RRGGBBAA` colour and a tagged
  tray's tag. The spool is placed in the tray, and the tray's previous spool moves to no
  place. ScadBuddy builds it from its own report; nothing reaches Bambuddy. The spool has no
  type yet. The route is `POST /printers/{id}/trays/{ams}/{tray}/new-spool` and needs an
  `Idempotency-Key`. The button is on the native print dialog's tray question and on an
  empty AMS tray's place page. The dialog asks about every unanswered native tray, one with
  no shelf candidate too; that question offers only this button and the skip (R-7150-9).

The agent's `filament_spool_upsert` takes `quantity` and `copy_of`, and `create_tray_spool`
makes a spool from a tray. A resend of a copy reads its source again: its key names the
draft it read, so an unchanged source makes nothing twice and a changed one makes a new set
(R-7150-8).

The Spools tab's bulk bar acts on the selected spools (#7151, plan
`docs/superpowers/plans/2026-11-23-spool-bulk-ops.md`):

- **Edit fields.** A dialog lists every field a spool's edit sets, except archived. Each
  ticked field is set on every selected spool; a ticked field left empty clears the spool's
  own value, so it takes its type's. It is one request, `POST /filament/spools/bulk-update`,
  and all or none: when one spool refuses, nothing changes and the reason names that spool.
  Each spool that changed gets its own `edited` event.
- **Archive and Restore.** One request each, `POST /filament/spools/bulk-archive` and
  `/bulk-restore`. The answer lists the spools changed, those already so and the ids that
  name no spool, and the bar reports it once.

An AMS tray's place page that holds a spool has "Take the spool out of this tray"
(`DELETE /printers/{id}/trays/{ams}/{tray}/spool`): the spool moves to no place.

The Spools and Tags tabs find a tag by its UID or tray UUID. A search token of at least 4 hex
digits matches a spool's open tag that holds it, with `:` and `-` left out, so `04:A1:B2`
finds tag `04A1B2…`. The agent's `filament_spool_list` `q` does the same. Its bulk tools are
`filament_spools_bulk_update` and `filament_spools_bulk_archive`.

Tray changes and print ends are recorded when a printer report is written
(`backend/scadbuddy/filament/capture.py`). They are not a workflow of their own.

A type's cost per kg can be worked out on its page (#7152, plan
[`2026-11-23-catalog-cost-stats.md`](../superpowers/plans/2026-11-23-catalog-cost-stats.md)).
The cost calculator sends a purchase's price, tax, shipping, spool count and net weight to
`GET /filament/cost-per-kg` (`backend/scadbuddy/filament/cost_calc.py`). The server does the
sum on exact decimals, in the shop's currency, and writes nothing. "Use as default cost" puts
the result in the type's cost field, and Save writes it. The Metrics tab's "By material
number" section reads `GET /filament/metrics/material-numbers`
(`MetricsStore.material_numbers`). A material number is a type's code, case folded, which
Bambuddy calls `material_number`; archived types of the same code count under it. Each row
gives the types, the spools in stock, their grams and value at cost, and the grams the
range's prints used, counted as the Consumption chart counts them. The agent reads both
with `filament_cost_per_kg` and `filament_material_number_stats`.

Filament presets are ScadBuddy's own (#7176, plan
[2026-11-23](../superpowers/plans/2026-11-23-scadbuddy-filament-presets.md)): the table
`filament_presets`, each document a blob with every version kept. A type or spool names one
by the ref `sb-<id>` in `slicer_filament`. The native slicer reads them from ScadBuddy's
store; Bambuddy knows each one only by its mirror's local preset id
(`EffectivePreset.bambuddy_id`), which its slice path and the spool push send. The print
dialog's tray-question answer sends no preset: it names the tray and the spool, and Bambuddy
configures the tray from the spool it already holds (below). The AMS slot configure sends none:
it goes over the LAN and names the preset through the native catalogue (`printers/ams_slots.py`
`slot_ids`). ScadBuddy owns the presets, and Bambuddy is at most a mirror target or
the source of an import (#7244, the owner's decision of 2026-11-23): ScadBuddy keeps no
copy of Bambuddy's local presets, and the native slicer, the print dialog and the profile
fill read none. Bambuddy's presets come in three ways only, each reading `GET
/local-presets` itself and writing only `filament_presets`: the scheduled pull's import
while `filament_sync.presets` is `bb_to_sb` or `two_way` (a binding that names a Bambuddy
local preset is moved to a ScadBuddy preset made from it, and a preset made in Bambuddy
takes a new version when Bambuddy edits it); the one-time import, whatever the direction
(`POST /filament/presets/bambuddy-import`, Settings → Printing → Filament's "Import presets
from Bambuddy now", agent `filament_bambuddy_presets_import`); and the API's start, which
adopts every binding that still names a Bambuddy local id (`adopt_at_start`), unless the
links are another Bambuddy's. A binding left naming one is an error the print dialog shows
(`backend/scadbuddy/filament/sb_presets.py`). While the stored setting
`filament_preset_mirror` is on (the default), each preset ScadBuddy owns is copied to
Bambuddy's local presets after a fetch and at the end of each scheduled pull, whatever
`filament_sync` says, rewritten in place under the same Bambuddy id when it changes; a
failed copy is the preset's `mirror_error`
(`backend/scadbuddy/filament/preset_mirror.py`). One mirror runs at a time, a preset's
Bambuddy id is kept with the Bambuddy it belongs to. That id is a Bambuddy link as the
filament sync's are: the write that stores it records its Bambuddy as the links' one when
none is recorded, so a site whose only links are presets has one too. After the Bambuddy
URL changes the mirror waits, as the filament sync does, until the links are reset
(`POST /filament/sync/reset-links`): a reset that finds the same Bambuddy (proved by the
spool links, or, with no spool linked, by the presets: each linked id listed by the new
URL's Bambuddy, none older than Bambuddy's `updated_at` as ScadBuddy last took it: the mirror's last
write, or for a preset imported from Bambuddy, its last import, #7343) moves the ids to the new URL, and
one that deletes the links drops them (the dry run's and the answer's `presets` count), so
each preset is created on the new Bambuddy (on the same Bambuddy, a second copy, which the
reset dialog says);
meanwhile no path hands Bambuddy an id of the old one. A create whose answer was lost is
found above the highest id listed before it, by its name and its document, not made
twice; when a newer version was written since, it is created again and its `mirror_error`
names the copy the lost create left (#7343); one preset's failure, a Bambuddy id another ScadBuddy preset holds included, is its
own `mirror_error` and never stops the rest.

The Presets view (#7204, plan `docs/superpowers/plans/2026-11-26-filament-presets-view.md`) is
read-only. Go core serves it, not Python: area `filament-presets` in
`tools/go/core/internal/filamentpresets`, with `GET /api/v1/filament/presets` (`?source=`,
`?type_id=`), `GET …/presets/{preset_id}` and `GET …/presets/{preset_id}/diff`
(`tools/go/core/README.md`). The area is on only when `SCADBUDDY_CORE_AREAS` names it
(`tools/go/core/main.go`). Until then the paths go on to Python, which has a handler for none
of them and answers a 404 with no `code`. The Presets tab and the preset page tell that apart
from a Go 404, which has a `code`, and show "Presets are not on yet on this server"
(`frontend/src/pages/filament/PresetsTab.tsx`, `PresetPage.tsx`). The agent tools say the same
in a `ToolError` that names the area (`agent/src/tools/filament.ts` `presetOk`). Go computes
the mirror state of each preset and stores none: `bambuddy_owned` for source `bambuddy`,
`failed` when `mirror_error` is set and `mirrored_sha256` differs from `sha256`, `pending` when
only the hashes differ, and else `sent`. A `sent` preset can still carry a `mirror_error`: it is
the note of a lost create (`tools/go/core/internal/filamentpresets/mirror.go`). Go strips the
print-host credential keys from every document it returns, as Python does. Python and Go keep
the same rule through shared vectors
(`backend/tests/fixtures/filament-preset-vectors.json`,
`backend/tests/filament/test_filament_preset_vectors.py`, `test-fixtures/filament/preset-wire.json`).
Every answer is `no-store`, and a document over 1 MiB is not read
(`tools/go/core/internal/filamentpresets/document.go`). Only a `manual` preset can be edited,
when the edit routes come: a `3dfp` preset would get a new version from the next fetch, and a
`bambuddy` preset from the next Bambuddy refresh, so each of them is copied first (plan R3). The
page and the three read tools `filament_preset_list`, `filament_preset_get` and
`filament_preset_diff` change nothing (`agent/src/tools/filament.ts`).

A fetch writes ScadBuddy only: each file is the type's `3dfp` preset of its key, a changed
one a new version with its changed keys in the run's notes, and the type's own nozzle range
wins over 3DFP's (#7142). Fetching presets is the `ProfileFetch` workflow on the
profile-fetch worker, and a 3DFP search or add is its `TdfpSearch` or `TdfpCreate` workflow (#7025, `filament/tdfp.py`)
([processes.md](../architecture/processes.md#profile-fetch-worker-and-browser)).

A printer's K-profiles (#7154, moved off Bambuddy by #7276, plan
`docs/superpowers/plans/2026-11-25-filament-hardware-off-bambuddy.md` Task 7) are read from
and written to the printer over ScadBuddy's own LAN link: MQTT `extrusion_cali_*` commands
(`backend/scadbuddy/printers/lan_commands.py`) run by the `LanCommand` workflow
(`backend/scadbuddy/workflows/lan_command.py`), routes
`backend/scadbuddy/api/printer_kprofiles.py`. They are shown on the printer's card in
Settings → Printers (closed until opened: each read asks the printer). The printer must be
linked over the LAN (409 `not_lan_linked`) and heard from lately (409 `printer_offline`).
Set, batch set and delete take an `Idempotency-Key`: a key sent again joins the run it
started or reads its outcome, so the command reaches the printer once; a write the printer
did not acknowledge answers `confirmed: false`. A printer slow to answer gets a 202 with the
run's id, followed at `GET /printers/{id}/commands/{command_id}`. The notes are ScadBuddy's
own (`backend/scadbuddy/printers/command_data.py`), under the keys Bambuddy's K-Profiles page
uses; nothing about them is sent to the printer or to Bambuddy.

A printer's AMS slots (#7153, moved off Bambuddy by #7276, same plan, Task 8) are read from
the printer's latest LAN report, and shown on its card in Settings → Printers ("AMS slots",
closed until opened). Each command (configure, reset, refresh, load, unload) is MQTT over the
same LAN link (`backend/scadbuddy/api/printer_slots.py`), sent under an `Idempotency-Key` as
above. Before it sends, ScadBuddy's own guard (`backend/scadbuddy/printers/ams_slots.py`
`guard`) reads that report and refuses with a 409 and a `code`: `printer_offline` (no fresh
report), `printer_busy` (a print is active), `ams_busy`, `no_slot`; load also refuses an empty
slot (`tray_empty`) or the one already feeding (`already_loaded`), and refresh refuses while
filament is loaded (`filament_loaded`). Configure takes any SB spool (its values and its type's
preset and K value for the slot's nozzle) or the values by hand. A slot's saved preset is
ScadBuddy's own (`printers/command_data.py`), and an AMS unit's label is the name of
ScadBuddy's place of the unit; neither is sent to the printer or to Bambuddy. A reset clears
the saved preset only when the printer confirms it in the route's own answer (P43); otherwise
the page says the preset was kept. The page asks before every printer write.

An override tag's slot is configured the same way: the RFID reconcile
(`backend/scadbuddy/filament/rfid_slots.py`) runs the configure's commands over the LAN link
and does not call Bambuddy.

One write to a printer still goes through Bambuddy. The print dialog's tray-question
answer (`BambuddyClient.assign_spool`, `POST /inventory/assignments`) places the spool at the
tray in ScadBuddy and writes Bambuddy's assignment for a linked spool, after which Bambuddy
configures the tray over MQTT. #7641 moves it. The K-profile read for the profile fill
(`BambuddyClient.k_profiles`, `filament/profile_fetch.py`) also stays on Bambuddy until
[#7604](https://github.com/eh-homelab/ScadBuddy/issues/7604).

### Unknown spools (#7143)

Spec: [unknown-spool-flow](../superpowers/specs/2026-11-23-unknown-spool-flow-design.md).
A tray that a printer reports as loaded, with no spool in it, is an unknown spool.

- **The row.** Capture writes a row in `filament_unknown_trays` for an untagged AMS tray that
  no spool holds. `seen_at` moves only when a reported field changes (D1): it is the version of
  the tray, not a heartbeat. The owner's resolve sends `seen_at` back, and a changed tray is
  refused as `tray_changed`. A tray that is empty, or that a spool holds, loses its row when its tray lock is free. A busy
  lock (D11) means a link or a resolve is settling the tray, so capture keeps the row and looks
  again on the next report. The
  external holder never gets a row (D2). `remain_pct` is stored, but it is null in practice for
  an untagged tray (D3).
- **A tray with an RFID key** gets a spool at once, with no question. Capture takes the spool type
  that `match_tray` finds, or makes a spool with a material and a colour only. The `created` event
  has `data.by` `rfid_capture`. The list shows these spools for 24 hours as `made_from_tags`
  (D5), and the page shows one toast for each new one.
- **Capture** (`backend/scadbuddy/filament/capture.py` `note_unknown`) runs after `reconcile` in
  the report write and in the backstop. It takes the tray's try-lock before it makes a spool or
  clears a row. A busy lock leaves the tray to the next report.
- **Routes** (Go, area `unknown-trays`, behind the person-auth gate of #8038):
  `GET /api/v1/filament/unknown-trays` lists the rows. Each row has the printer, the slot, the
  place, the reported material and colour, `seen_at` and up to 10 ranked `candidates`. The rank
  key is: spools with no place first, then spools of the reported material, then the nearest
  colour (a spool with no colour last), then the spool number; archived spools are left out.
  `POST …/{id}/dismiss` deletes a row (204, or 404 `tray_gone`). A dismiss is only for a slot
  that is no longer there: a spool that is still loaded comes back on the next report.
- **Resolve** links a spool to the tray, books the typed net weight as a `weighed` reading and
  deletes the row, in one transaction. It is a Temporal activity, `filament_resolve_unknown_tray`,
  on the `library` queue (G5), so the Python write that links a spool stays the only one. Only a
  spool with no reading, use or load yet takes the net weight as its label weight (D4). The lock
  order stays in Python (G9). **The Go route that starts the activity is not built yet.** It waits
  for a Temporal client in Go (`tools/go/core/internal/temporalx`). Until then, the dialog's and
  the tool's resolve get a 404 and cannot resolve a tray. The existing link paths still clear the
  row: the print dialog's tray question, the place page's "New spool from this tray" and the
  agent's `link_tray_spool`.
- **The block.** A print to a printer that has a row is refused with 422 `unknown_spool_loaded`.
  `prepare_run` and the print check do it (`PrintCheck.unknown_trays` names the trays), and so does
  `lan_print`. On Bambuddy's path, a tray that the inventory read gives an SB spool is not counted
  (D9). The print worker reads the block from a CRUD route of caller `print`. Both checks go on
  when that route is missing (G3), so the worker-side block works only once
  `SCADBUDDY_CORE_AREAS` names `unknown-trays`.
- **The owner is asked** in four places: the "Needs you" list (a source with the agent requests,
  the flow grants and the pairings, read every 15 s), Filament → Locations, the page of a place,
  and the print dialog's verdict. All four open one dialog. The assistant has two tools:
  `filament_unknown_trays` and `filament_unknown_tray_resolve`.

### Weighing breakdown (#7682)

A weighing sent with the scale's reading (`POST /filament/spools/{id}/events`, `kind`
`weighed`, `reading_g` beside `grams`, or alone) keeps what it was made of in the event's
`data`: `reading_g`, `empty_g` (what was taken off for the empty spool), `empty_source`
(`insert`, or `person` when the client took something else off than the spool's effective
empty weight), `empty_basis` and `empty_name` (the insert's, null for `person`), `expected_g`
(the grams left the ledger gave just before the weighing's time; null with no reading before)
and `drift_g` (`grams - expected_g`; negative means the scale found less filament than the
ledger expected). The effective empty weight is the spool's insert type's, else its type's
(`filament/empty_weight.py`); a reading sent alone on a spool with none is refused, 422
`no_empty_weight`, and one under the empty weight 422 `reading_below_empty`. The ledger's rules do not change: the weighing's filament anchors as
before. The spool carries the newest weighing's drift as `last_drift_g` and `drift_notice`
when it is at or over Settings → Filament → "Drift notice at, grams" (10 by default). The
spool page's Weight section says it in a sentence ("Weighed 1180 g on 25 Nov 2026: filament
980 g + spool 200 g (Plastic, placeholder). The ledger expected 992 g. The scale found 12 g
less than the prints accounted for."), the timeline says "reading 1180 g, spool 200 g, 12 g
less than expected", the heading and the spools list carry a Drift mark while it is a notice,
and an added-full spool whose starting amount (its `created` event) is not a reading says
its start gap ("Started at the label weight 1000 g. Bambuddy reported 979.3 g; 20.7 g
unexplained."); a later anchor (a sync, an AMS reading) follows prints and shows none. The spool page, the
phone page and the agent's `filament_spool_add_event` send `reading_g` whenever an empty
weight is known (the phone page sends it alone, so a decimal reading keeps the insert's
name). Design: `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md`
§5.1, §5.2, §5.5.

### A new spool's first weighing under the label weight (#7683)

When a spool that is new by #7280's rule (nothing consumed, grams left at the
label) reads under its label weight, the insert weight check
(`GET /filament/spools/{id}/insert-weight-offer`) answers `reason` `below_label` with
`shortfall_g` (the label less the reading), `resolutions` and `default`. Each resolution
names its filament: `tared` (the scale was tared on the empty spool, so the reading is the
filament), `short` (the spool holds less than its label: the reading less the empty weight,
and the spool stops being added full) and `label_wrong` (the same filament, and the spool's
own label weight becomes it, rounded; the type keeps its own). `short` and `label_wrong`
are offered only when the reading is at least the empty weight. The default is `tared`
within 10 % of the label, else `short`; a reading under the empty weight offers `tared`
alone, so its default is `tared`. The weigh form and the phone page show the three
with their numbers ("The reading is 100 g under the label weight. What is up?") with the
default preselected, and the weighing posts `resolution` with `reading_g` alone; the
agent's `filament_spool_add_event` answers the choices (`below_label`, nothing booked)
until it is called again with one. The store checks the choice again under the row locks
(a resolution the check no longer offers is 422 `no_below_label` with the check's
`reason`), books the side effect as an `edited` event before the weighing, and keeps
`resolution` and `shortfall_g` in the event's `data`; a `tared` weighing's `empty_source`
is `tared`, `empty_g` 0. A resolved weighing keeps `expected_g` and records no `drift_g`,
so it raises no Drift mark: the person just explained the gap. The timeline says "scale tared on the spool", "spool short by
100 g" or "label corrected", and the Weight section's sentence reads "filament 900 g (the
scale was tared on the spool)". Design: the same spec, §5.3.

### End of life: finished, respooled, damaged out (#7684)

Archive asks why the spool goes and offers one last weighing. The spool page's Archive
button opens a dialog: a reason (`finished`, `respooled`, `damaged`, `other`; the dialog
needs one, the API keeps it optional, and the sync's archives record `other`), and the
scale reading with the spool when its empty weight is known, else the filament left.
`GET /filament/spools/{id}/closing` previews, as the inputs settle, what the archive would
record, and says when the numbers disagree with the reason: a finished spool with more
than the setting `filament_weighing_finished_left_g` (5 g by default) still on it ("30 g of
filament are still on it."), or any other reason at or under it ("It reads empty; is it
finished?"), and what the archive would refuse: a reading with no empty weight known
(`no_empty_weight`), under it (`reading_below_empty`) or under the `final_grams` sent with
it (`reading_below_grams`, #7909); such a reading closes on nothing. The person can go on;
nothing refuses. `POST /filament/spools/{id}/archive`
takes `reason`, `final_reading_g` and `final_grams`: a final weighing is booked as a
`weighed` event just before the `archived` event, by the same rules as any weighing
(422 `no_empty_weight`, `reading_below_empty`; a restore takes neither).

The `archived` event's `data` keeps the lifetime reconciliation: `reason`, `started_g` and
`started_from` (`reading`, the spool's first scale or by-hand filament reading, or `label`,
the label weight of an added-full spool), `ledger_consumed_g` (the plates since that start,
never reset by a used-counter reset), `final_filament_g`, `scale_consumed_g` (start less
final filament, null without a final weighing) and `drift_g` (ledger less scale: negative
means the scale found less filament than the ledger expected). An archived spool's Weight
section says it in one sentence ("Finished on 25 Nov 2026: started with 1000 g (from the
label), the ledger used 508 g, the scale says 520 g were used; 12 g less than the ledger
expected." or "…the ledger used 508 g; not weighed at the end."), and the timeline shows
the reason on the archive.

Grams left at the end are waste (#7167). A person's archive books them as one
`non_print_waste` event, the zero reading, after the final weighing and before the
`archived` event. The waste is what is left after that weighing, so no gram is counted
twice. The spool then has 0 g left, and `non_print_waste_g` in the closing says how many
grams were written off. A `respooled` archive books none: the filament moves to the new
reel. The bulk archive, the Bambuddy pull's archive and an archive by a plain edit (PATCH) book
none. A restore leaves the spool at 0 g, so a second archive books no second waste and adds
no second shopping item. An `archived` event that carries waste counts as the archive of a
spool that was not empty, so the archive adds its one shopping item. The event is not a
plate, and it adds no shopping item of its own. The code is in
`backend/scadbuddy/filament/spools.py` (`archive_on`).

The metrics keep this waste apart from the print waste (#7167). `wasted_g` stays the grams of
the failed and cancelled plates. Each `non_print_waste` event adds its `data.waste_g`, raw, to
one other figure: `non_print_waste_g` on a spool's metrics, and on the inventory metrics a
total with rows by material and type (no printer: the waste has none) by the event's UTC day
in the range. No gram is in both figures, and no rate counts it. The Prometheus export has its
own series, `scadbuddy_filament_non_print_waste_grams`, by material. The reader is
`ledger.waste_events` in `backend/scadbuddy/filament/ledger.py`. The sums are in
`metrics.py` (`spool_metrics`), `metrics_store.py` (`inventory`, `snapshot`) and
`metrics_export.py`. The Metrics tab shows a "Non-print waste" block under "Waste" only when
the range has some (`frontend/src/pages/filament/MetricsTab.tsx`).

A dry box is a place of a dry-storage kind (`dry_storage` on the kind), with capacity 1, not
archived, and with no printer (#7167, `backend/scadbuddy/filament/dry_box.py` `BOX`). Capacity
was only a number before; for a dry box it is now enforced. A box holds one spool. A move into
a box that holds another spool is a 409 `box_full` naming it (`occupant_id`), and so are a
bulk create and a bulk restore. A change that makes a place a box while it holds two spools or
more (its kind, its capacity or its kind's `dry_storage` flag) is a 422 `box_has_many`. A
place of another kind, or with another capacity, keeps capacity as a number only. A box is a
place, so its label is the place label, `L-<n>`; the row of every place on the Locations tab
has a "Print label" link to it (`frontend/src/components/filament/LocationTree.tsx`).

A spool's home is the last dry box it sat in (#7167). When a spool that went into its tray
from a place that is a live dry box now comes out of the tray, capture puts it in no place,
makes that box its home and sets `home_asked_at`: ScadBuddy never assumes which spool goes into
the box, the unloaded spool included. A spool that went into the tray from a shelf goes back to
the shelf with no prompt, and its home stays the same.
`GET /filament/spools?awaiting_home=true` lists the asked spools. The person names the spool
(a tag scan, a code or a pick by hand), and `POST /filament/spools/{id}/move` with `answers`
(the unloaded spool) puts it in. With `if_occupied: replace`, the box's own spool is archived
at 0 g first and its grams are booked as non-print waste (`cause` `replaced`), in one
transaction. A box's spool that left the box before the replace locked it is not archived.
A replace names the box's spool the caller saw in `expected_occupant_id`: without it the move is
a 422 `expected_occupant_required`, and a box that holds another spool by then is a 409
`box_full` naming it, so no spool the caller did not name is archived.
Any move, an archive or a reload clears `home_asked_at`. A spool whose box was archived goes to
no place, and one whose box is no box now goes back to it, as before. The code is in
`backend/scadbuddy/filament/homes.py` and `capture.py` (`_put_back`).

Respooled offers "Archive and continue as a new spool": the copy flow opens with the final
filament as the new spool's grams and "Added full: No", and the save sends `continued_from`
with `copy_of`. The source must be archived (422 `continued_from_not_archived`) and not
yet continued by a spool that still exists (422 `continued_from_taken`, #7909), and the
request makes one spool. The new spool's `created` event keeps `continued_from`, the source's
newest `archived` event gets `continued_as`, and the timeline links the two ends. A spool
archived before #7684 has no closing: its page shows none (#7909). The agent's
`filament_spool_archive` takes the reason and the final reading, `filament_spool_closing` (a
read) previews the closing and its notices, and `filament_spool_upsert` takes `continued_from`. Design: the same spec, §5.4 and §5.5.

### Length and the density check (#7685)

A LAN print books its filament length beside its grams: the sliced plate's `used_m` per
filament (Bambu Studio writes it beside `used_g` in `Metadata/slice_info.config`; measured on
`backend/tests/printers/fixtures/rack-slice-info-h2c.config`) is read by
`slicing/slice_info.py` `plate_uses` with the grams, rides in `LanUploaded.length_by_slot`
and `SlotUse.length_m`, and is kept as `data.length_m` on the `print_started` event (absent
when the plate gave the slot no length, or when a slot merged onto the plate's first filament
has a source with grams but no length, #7932) and copied onto its `print_ended` (`0.0` on a
failed plate, as its grams are 0). Bambuddy's usage rows have no length and keep none, so a plate
Bambuddy also booked (the paired plate counts once, as the BB row) takes the length of
ScadBuddy's own end of the job. The spool's
history (`GET /filament/spools/{id}/history`) sums the metres of the plates that carry a
length over 0 m (`metrics.used_m`, null when none does: a spool whose lengthed plates all
failed has no metres, not 0 m), derives the density those plates' grams and metres
imply over the spool's effective diameter (`implied_density_g_cm3`; 1 m of 1.75 mm filament
at 1.24 g/cm³ is 2.98 g) and compares it with the type's (`density_g_cm3`): apart by more
than 5 % is `density_mismatch`. For a plate ScadBuddy alone booked, both numbers are the
slicer's, so the implied density is the slicer preset's density read back, not a
measurement: a mismatch means the preset and the type disagree about the filament. For a
plate Bambuddy also booked, the grams are Bambuddy's usage row's over the slicer's metres,
summed over the job's ends on the spool (one spool can feed several slots of a job). The spool page's Used tile adds the metres ("3 prints, 40 g
each, 11.7 m"), a Density tile shows the implied value with "The type says 1.24 g/cm³" or
the mismatch sentence ("The prints imply 1.31 g/cm³, 6% over the type's 1.24 g/cm³: check
the type's density or the label."), and the timeline shows a print's metres beside its grams
("−35 g, 11.7 m, measured"). A mismatch is a sentence, never a booking. The Weight section's
drift sentence then names a likely cause when one fits, read against the anchor the weighing
was compared with (the newest anchor before it), the first of: the starting amount (an
added-full spool anchored on a pull's figure whose drift is that figure's gap to the label:
the scale found the label weight), the type's density (the drift is what the slicer's grams
since that anchor over-counted at its preset's density, with a mismatch), or a placeholder
empty weight, for any drift, since the real empty weight is unknown ("Likely cause: the
spool's empty weight is a placeholder."). Nothing under a gram of drift, and nothing with
no fit; every cause waits for the history to load. Design: the same spec, §5.6.

### The phone page, the hand-held Scan mode (#7273)

`/scan` ("Scan" in the nav; `/filament/scan` redirects there) reads and writes tags with the
phone's own NFC reader, through Web NFC
(spec `docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md`; the Scan mode's plan
`docs/superpowers/plans/2026-11-25-scan-mode.md`). It works only in Chrome on
Android, in its own tab: Web NFC refuses a frame, so inside Bambuddy the page shows its address
to open. A tap posts the tag's UID and records to `POST /filament/tags/reads`, which opens
what the tag names, in this order: the spool that holds the tag (`next` `open_spool`; the
binding wins over anything the records say); else the thing a typed code in the records names,
a `url` record whose path ends `/x/<code>` (a printed label) or a `text` record such as `L-12`
or `S-142`, or a `scadbuddy:spool:<id>` text (`next` `open_code`, with `resolved` in the
`GET /codes/{code}` shape; `backend/scadbuddy/filament/tag_codes.py`); else it offers to bind
the tag or make a spool from what the tag says (OpenSpool or OpenTag3D). A code that names no
row is a new tag. A phone reads a Bambu tag's UID as 4 bytes (`7E7482BB`), and the AMS reports
the same tag as `7E7482BB00000100`, so ScadBuddy stores every 4-byte UID in the AMS's form
(`spools/models.py` `clean_tag_uid`, #8042; all 38 Bambu tags on prod end `00000100`, read
2026-11-26), and a phone's scan of a Bambu spool opens it. A 4-byte tag that is not a Bambu
tag (typed in, or another maker's) is stored and shown in that longer form too. The switch "Open what I scan at once" (remembered on the phone, localStorage
`scadbuddy.scan.openAtOnce`) opens the thing's page on each such read, and the page then starts
scanning by itself when it opens. "Write tag" on a spool backs the tag up first, writes the spool's
records in the tag's own format (else the `filament_tag_format` setting), reads the tag back,
and binds it only when the read-back equals what was sent. Every write keeps a backup, which
the page can write back. An agent reads or writes a tag through an NFC job (`nfc_read`,
`nfc_write`): the page shows the job and its records, and nothing runs until a person arms it
and holds a tag. A job waits `timeout_s` for a person to arm it; arming gives the run 60 seconds
(`ARM_SECONDS`) for the tap. Once the page has backed the tag up for a write (`write_begun`), the
job is never cancelled or expired: it ends with the write's result (its read-back, or the
page's error), or, when none comes within 10 minutes of the backup, `failed` with `result.code`
`no_read_back` (the tag may hold the new records, and its backup can restore it). The agent
waits for either.

### Properties (#7026)

Design: [inventory spec §6.3a](../superpowers/specs/2026-11-21-inventory-and-purchasing-design.md), D17.

A spool, a spool type and a catalog row (such as an insert type) can hold properties: free key-value pairs, such as a part number,
barcodes or a lot number. The store is generic and works for any entity, so it is not
specific to filament. A value is a string, a number, a boolean or a list of strings. A key
matches `[a-z][a-z0-9_]{0,63}`. Limits: 200 keys per entity, 2000 characters per string,
50 items per list, 500 characters per item (`backend/scadbuddy/entities/properties.py`).

- Each kind has a schema that documents and types the keys it knows. It never refuses a
  key. The schema is an allowlisted subset of JSON Schema Draft 2020-12 (`TOP_KEYWORDS`,
  `KEY_KEYWORDS`, `ITEM_KEYWORDS`; list items are strings). An `enum` or `const` must fit its
  key, and no bound may cross its pair or a cap. Anything else is 422 `bad_property_schema`, naming the keyword. A
  schema change rewrites and refuses no stored value; on the required kinds below, reads mark a
  value that no longer fits and the row's next checked save refuses it.
- Required keys (#7703, [spec §6.3b](../superpowers/specs/2026-11-21-inventory-and-purchasing-design.md)).
  The schema of a spool, a spool type or a catalog kind may list keys under `required`
  (`REQUIRED_KINDS`); any other kind's schema with `required` is 422 `bad_property_schema`.
  The row's own create and save routes (`POST`/`PATCH /filament/spools`,
  `POST /filament/spools/bulk`, `POST`/`PATCH /filament/types` and
  `POST`/`PATCH /filament/catalog/{kind}`) take `properties` beside the row, write both in
  one transaction, and refuse a row whose required keys have no value or whose defined keys
  hold a value that no longer fits: 422 `missing_required_properties`, with `missing` and
  `invalid`. An empty or blank string and an empty list are no value; `false` and `0` are.
  Not checked: a save that sends only `archived` or `sort_order`, bulk-update, events,
  syncs, imports, invoice confirm, a catalog row added on the way to another, and
  `PUT /filament/types/{id}/cost-per-kg`, which sets only the cost. Reads carry
  `missing_properties` and `invalid_properties` (keys only); an archived row is never
  marked. `PATCH /entities/{ref}/properties` refuses removing or emptying a required key
  that has a value (`backend/scadbuddy/entities/required.py`). Items and item kinds follow
  once the base-kind design settles.
- In the UI, a kind's key table has a Required column for those kinds. Ticking it shows how
  many live rows lack the key. The spool, type and catalog forms show the defined keys as
  fields, required ones first and marked. A refused save marks each missing or ill-fitting
  field and says "Fill the required properties first". The Spools, Types and catalog lists
  mark an incomplete row and filter to them ("Incomplete"). "Create type from this spool",
  the price book's "Make a type" and a new tag's "Create a spool from this tag" ask for the
  required keys before they make the row. "Reset to type", the bulk bar's type and linking
  a type to a price book row use the unchecked routes, so they work on an incomplete row
  (`frontend/src/components/entities/DefinedFields.tsx`, `RequiredKeysDialog.tsx`).
- `item` is the exception (#7067). An item's properties follow its item kind's
  `attribute_schema`, merged down the kind tree (a child kind's key wins;
  `backend/scadbuddy/inventory/kinds.py` `item_schema`). An item kind's schema is set on the
  kind, so `GET` and `PUT /entity-kinds/item/property-schema` both answer 409
  `schema_owned_by_item_kind` (`backend/scadbuddy/api/entity_properties.py`).
- The migration seeds the keys `mpn`, `gtin`, `asin`, `manufacturer_url`,
  `empty_spool_weight_g` and `td` for `spool_type`, and `lot_number` for `spool`
  (`backend/scadbuddy/migrations/20261123T0017Z_entity_properties.sql`).
- Routes: `GET`/`PATCH /entities/{ref}/properties` (a key set to `null` is removed; a
  patch applies whole or not at all), `GET /entity-kinds/{kind}/properties` and
  `GET`/`PUT /entity-kinds/{kind}/property-schema`, which answer 409
  `schema_owned_by_item_kind` for `item` (`backend/scadbuddy/api/entity_properties.py`).
- UI: the section "Files, links and properties" of the spool and type pages
  (`frontend/src/components/entities/PropertiesPanel.tsx`; the spool page's anchor
  `#files-and-links` stays), and the same three panels under a catalog row's edit form in the
  Catalog tab (`frontend/src/components/filament/CatalogList.tsx`). A kind's own keys are edited
  from the panel's "Edit the kind's keys" and the Catalog tab's "Property keys"
  (`frontend/src/components/entities/KindKeysForm.tsx`, #7656); an item's are its item kind's,
  on `/inventory/kinds`. A list value has one field per item. A rename or an add onto a key the entity
  already has asks first, since it replaces that key's value.
- Search: the Spools and Types tabs' search also matches property values, beside the hex
  match on a spool's tag UID and tray UUID (#7151)
  (`frontend/src/lib/filamentSearch.ts` `propertyTexts`, loaded by
  `frontend/src/components/entities/api.ts`). The agent's search matches them too
  (`agent/src/tools/filament.ts` loads them, `agent/src/tools/filamentSearch.ts` matches); both use the shared vectors
  `test-fixtures/filament/search-vectors.json`.
- Agent: `entity_property_list`, `entity_property_set`, `entity_property_schema_get` and
  `entity_property_schema_set` (`agent/src/tools/entities.ts`), and a `properties` field on
  `filament_type_upsert` and `filament_spool_upsert` (`agent/src/tools/filament.ts`). An
  upsert that changes only properties skips the row PATCH, so `updated_at` and the
  Bambuddy sync do not change.

## Code

- Routes:
  - `backend/scadbuddy/api/filament.py:265` and `backend/scadbuddy/api/filament_purchasing.py:46` (both under `/filament`);
  - `backend/scadbuddy/api/tray_spools.py:44` (a tray's spool link, and create from slot);
  - `backend/scadbuddy/api/filament_profiles.py:26`;
  - `backend/scadbuddy/api/printer_kprofiles.py:42` (a printer's K-profiles, under `/printers/{id}/kprofiles`);
  - `backend/scadbuddy/api/printer_slots.py:44` (a printer's AMS slots and AMS labels, under `/printers/{id}/ams` and `/printers/{id}/trays`).
- UI:
  - the **Filament** nav item (`frontend/src/components/AppShell.tsx:111`);
  - its routes (`frontend/src/App.tsx:121-138`);
  - `frontend/src/pages/filament/FilamentPage.tsx:45`, `SpoolPage.tsx:418`;
  - `frontend/src/components/filament/`;
  - Settings → Printing → Filament (`frontend/src/pages/settings/sections/filament.tsx:911`);
  - the print dialog's picker (`frontend/src/components/FilamentPicker.tsx:361`);
  - a printer's K-profiles on its card in Settings → Printers (`frontend/src/components/settings/PrinterKProfiles.tsx:83`).
- Core: `backend/scadbuddy/filament/`:
  - the SB spools a print reads: `inventory.py` (`read_sb_spools` at `:957`, `reconcile_trays` at `:453`);
  - `capture.py`, `ledger.py`, `catalog.py`, `types.py`, `locations.py`, `sensors.py`, `invoices.py`, `labels.py`;
  - a LAN print's grams and metres: `print_uses.py`, `prints.py`, read from the sliced plate by `backend/scadbuddy/slicing/slice_info.py` `plate_uses`;
  - the history and its metrics: `metrics.py` (`implied_density`), `metrics_store.py`; the drift's likely cause: `frontend/src/components/filament/driftCause.ts`;
  - `backend/scadbuddy/codes/` (code parsing and resolving);
  - `bb_sync_*.py` and `bambuddy_pull.py`;
  - `profile_fetch.py`, `sb_presets.py` (ScadBuddy's presets) and
    `backend/scadbuddy/slicing/own_presets.py` (their refs, as the native slicer reads them);
  - a printer's K-profiles: `backend/scadbuddy/printers/kprofiles.py` (the bodies and the LAN steps) and `backend/scadbuddy/api/printer_kprofiles.py` (over ScadBuddy's own LAN link, #7276); `BambuddyClient` has no printer K-profile write, slot command, slot preset or AMS label call any more (removed in #7276; it keeps the K-table read `k_profiles` that `profile_fetch.py` uses, until #7604, `put_spool_k_profiles` for the sync's spool K values, and `assign_spool` for the tray question, until #7641).
- Unknown spools (#7143):
  - `tools/go/core/internal/unknowntrays` (list, rank, dismiss, and the print worker's block read);
  - `backend/scadbuddy/filament/unknown_tray_rows.py` (the rows and the block text) and `unknown_resolve.py` (the resolve activity);
  - the capture step `note_unknown` in `backend/scadbuddy/filament/capture.py`, called from `backend/scadbuddy/printers/reports.py`, and the block in `backend/scadbuddy/bambuddy/print_run.py` and `backend/scadbuddy/printers/print_activities.py`;
  - the UI: `frontend/src/components/filament/UnknownSpoolDialog.tsx`, `useUnknownSpoolDialog.tsx` and `UnknownSpoolButton.tsx`, with `frontend/src/lib/unknownSpools.ts` and `frontend/src/api/unknownTrays.ts`;
  - the agent tools `filament_unknown_trays` and `filament_unknown_tray_resolve` in `agent/src/tools/filament.ts`.
- Workflows:
  - `backend/scadbuddy/workflows/spool_refresh.py:104`;
  - `filament_sync.py:68`, `filament_pull.py:43`, `sensor_notify.py:58`.
- Agent tools: `agent/src/tools/filament.ts`, `filamentPurchasing.ts`, `traySpools.ts`, `printerSlots.ts`,
  `printerKProfiles.ts`, `printerCommand.ts` (a printer command's 202, followed or read with `printer_command_get`).

## Data

- Tables: `filament_*` (`backend/scadbuddy/migrations/20261114T1300Z_filament_catalogs.sql`,
  `20261114T1301Z…` and later). These cover the catalogs, spool types, spools,
  `filament_spool_events`, tags and bindings, locations and sensors, suppliers and
  invoices, the Bambuddy sync state and links, `filament_profile_fetches`, and the
  presets `filament_presets` and `filament_preset_versions`
  (`20261123T0711Z_filament_presets.sql`; blobs held by `filament_preset`). The price
  book is `filaments`.
- `filament_unknown_trays` (#7143, `20261126T1936Z_filament_unknown_trays.sql`): one row for each
  `(printer_id, ams_id, tray_id)` while the tray is loaded and no spool holds it. It has the
  reported tag keys, material, colour and remain percent, `first_seen_at` and `seen_at`. The row
  goes with its printer (`ON DELETE CASCADE`). The API and `tools/go/core` read it (the
  one #5295 exception, #7667): core lists the rows and deletes one on dismiss, and serves the
  print worker's block read. The print worker gets the block from a CRUD route.
- The old spool overlay and native spool tables were dropped
  (`backend/scadbuddy/migrations/20261118T1235Z_drop_native_spool_tables.sql`).
- Settings: `spool_overlay_refresh_seconds` (the refresh Schedule), `filament_sync`,
  `filament_low_stock_pct`, `filament_default_cost_per_kg`, `profile_fetch_nozzles`,
  `filament_preset_mirror`,
  `filament_drying_temp_c` and `filament_drying_hours` (#7156: the drying a spool is offered
  when its spool, type, subtype and material set none; empty by default, so none is offered),
  `filament_auto_add_unknown_rfid` (#7156, stored only, on by default: on leaves Bambuddy's own
  auto-add of unknown RFID spools as it is, so Bambuddy may add one and the pull brings it in,
  unless the RFID override mode holds it off; off holds Bambuddy's auto-add off as the override
  mode does, so no spool is made for an unknown tag; the value Bambuddy had goes back only when
  neither holds. ScadBuddy's capture makes no spool from a tag itself),
  `filament_label_base_url` (an https origin of at most 64 characters, live; unset, labels use
  `public_url` reduced to its https origin, then the page's own origin),
  `filament_label_template` (#7157, stored only: the label template the labels page and the
  labels PDF use; the 50 x 25 mm roll label by default; a saved sheet grid by its
  `sheet_id`, #7216),
  `filament_label_output` (#7216, stored only: the labels file's format, `pdf` or `png`, the
  PNG's dpi, 203, 300 or 600, and the spool label's fields; a PDF at 300 dpi with the number,
  material, colour, brand and QR code by default).
- Bambuddy's spool fields (#7149, plan
  `docs/superpowers/plans/2026-11-23-spool-bb-fields.md`,
  migration `20261123T0609Z_filament_spool_bb_fields.sql`):
  - `data_origin` says how the spool's data came in, in Bambuddy's words (`rfid_auto`,
    `rfid_linked`, `nfc_link`, `opentag3d`), or `openspool`, ScadBuddy's own value for a tag
    the phone page wrote in OpenSpool (Bambuddy has none for that format). It is read only. The pull takes Bambuddy's
    value; a person's tag bind sets `rfid_linked` when the tag's stored kind is a Bambu
    tag, else `nfc_link`; a tag written from the phone page (#7273) sets `openspool` or
    `opentag3d`, the format it wrote.
    The push sends it when SB has one, and the tag push sends it to `link-tag`.
  - `added_full` (a person edits it) and `tag_encoded_at` (Bambuddy's `encode_time`, set
    by a tag written from the phone page) come from Bambuddy on the pull. Bambuddy takes no write of
    either, so they are never pushed. With the baseline below they are kept in the link's
    `bb_seen` (`PULL_SEEN_FIELDS`), and after a push the pull judges them against it, never
    against the push's echo (R-7149-8).
  - The consumed counter is a ledger event, `consumed_reset` (no grams; the grams left
    stay). The spool metrics' `consumed_g` counts the plates after the newest reset, and
    the spool page's "Used" tile shows it, with "Reset the used counter". Reset one spool
    with `POST /filament/spools/{id}/events` (`kind: consumed_reset`), or 1 to 500 spools
    with `POST /filament/spools/reset-consumed` (agent `filament_spool_add_event` and
    `filament_spools_reset_consumed`). Under `filament_sync.weights` the sync
    resets Bambuddy's counter of a linked spool once per SB reset made after the link
    (R-7149-11; op `reset_consumed`, `POST /inventory/spools/{id}/reset-consumed-counter`), and the pull books a Bambuddy
    reset (its `weight_used_baseline` moved) as a `consumed_reset` of source `bb`, which
    is never sent back. BB keeps no reset time, so that event is dated at the newest reading
    that still held the old baseline, or BB's `updated_at` when earlier (R-7149-9). No
    baseline column is kept.
- Codes (#6955): each place has a number and the code `L-<n>`, each spool `S-<n>`, and each
  item kind `K-<n>` (#6949; it opens the inventory list of the kind's items). A code
  opens at `/x/<code>` (`frontend/src/pages/CodePage.tsx`; route `GET /api/v1/codes/{code}`,
  `backend/scadbuddy/api/codes.py`). Any case works, with or without the dash. A number is
  never reused, and an archived thing still resolves.
- Printer places: a printer has its own place (`filament_locations.printer_root`, kind
  "Printer"). Its AMS units and external holder hang under it
  (`LocationStore.ensure_printer_on`, `backend/scadbuddy/filament/locations.py`). A printer
  label request makes the place when it is missing, and so does a tray capture whose printer
  has a unit place or external holder with no live parent (`LocationStore.hang_orphans_on`).
  A printer's places go when the printer is deleted (`PrinterStore.delete`,
  `backend/scadbuddy/printers/store.py`). First each spool in one moves to no place, with a
  `moved` event (#7143). Then each other place under one moves to the nearest place above
  the printer's place that is not the printer's (#8177). It moves to the top level when the
  printer has no unarchived place of its own, when nothing is above that place, or when the
  place above is archived. The delete is refused with 409, and changes nothing, when such a
  place would clash by name there (with a place already there, or with another place that
  moves there), or while a stock lot or unit points at one of the printer's places. The
  inventory has no move path with an event for those rows, so a person moves them first. The
  delete locks in the order a report write takes: the printer's report, its place lock, its
  spools, then its places (the order is in `PrinterStore.delete`). A delete that still meets
  a concurrent write answers 409 `printer_delete_conflict`, and the person tries again.
- Labels: a place's QR is `<base>/x/L-<n>`. The labels page (`/filament/labels`) prints places
  with `?locations=` and printers with `?printers=` (one printer's from its place page, every
  printer's from the Locations tab). The printer labels first make the place of each AMS unit,
  slot and external holder the printer's latest report lists; a printer with no such report is
  named on the page. Spool labels keep `scadbuddy:spool:<id>`. A place links to its own page,
  `/filament/locations/<id>`.
- Label templates and the PDF (#7157, plan
  `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`): the labels page offers SB's
  50 x 25 mm roll label, Bambuddy's six templates (two box labels, two AMS holder labels and
  the Avery 5160 and L7160 sheets) and a custom size, and saves the choice in
  `filament_label_template`. A sheet template prints on its own grid. "Download" asks
  `POST /api/v1/filament/labels` (`backend/scadbuddy/filament/label_pdf.py`, ReportLab) for
  the same labels at the same template and saves it through `downloadBlob`, so it works
  inside Bambuddy's frame. A place label's PDF needs the label address or an https public
  URL (409 `no_label_base` otherwise). The browser print stays.
  The agent reads codes with `filament_code_resolve`, `filament_location_get` and
  `filament_printer_labels`, and the label sizes with `filament_label_templates`. It saves
  and deletes saved sheets with `filament_label_sheet_upsert` and
  `filament_label_sheet_delete` (`write`, #7216 R-7216-14). The PDF route has no tool
  (R-7157-10).
- Labels as PNG (#7216, plan `docs/superpowers/plans/2026-11-25-label-outputs.md`): with
  `format` `png` (or the setting `filament_label_output`), `POST /api/v1/filament/labels`
  gives each page of the PDF as a greyscale PNG at the chosen dpi, drawn by PDFium
  (`backend/scadbuddy/filament/label_png.py`), so the PNG and the PDF never disagree. Each PNG
  carries its dpi, so label printer software prints it at its size. One page is `labels.png`;
  more pages are `labels.zip`, a stored ZIP of `spool-<number>.png` and `L-<n>.png` (one label
  a page) or `sheet-<n>.png` (a sheet). Past 400 megapixels in all, the route answers 422
  `png_too_large`: choose PDF, a lower dpi or fewer labels. The labels page has the choice
  beside the template: PDF or PNG, and for PNG the dpi (203, 300 or 600). It saves each change
  in `filament_label_output`, and "Download" names the file by the type of the answer.
- Labels print to a CUPS queue (#7170, design
  `docs/superpowers/specs/2026-11-27-cups-label-printing-design.md` R7 and R9). The labels page
  has a "Label printer" row beside "Print" and "Download". It reads the queues of the CUPS
  server in Settings, Filament, "Label print server" (`filament_label_server_url`; the
  cluster's `http://cups.cups.svc:8631` by default; only a person changes it) through core's
  `GET /api/v1/label-printing/queues`. It offers a queue only when CUPS gives the queue's size
  and that size is the PDF's page within 1 mm each way, in the same orientation. The page is
  the label for a roll with a one-label template, else the sheet's page (Letter or A4), so a
  sheet never goes to a 2 x 1 in queue. It picks the last queue used
  (`filament_label_queue`) when that queue fits, else the only queue that fits, else none. Each
  queue in the picker shows its state, and the picked queue shows its state and its CUPS message, such as "1.02 in labels are loaded; …". A
  Zebra ZPL queue also gets the print mode, the darkness and the speed
  (`filament_label_job_options`; "Printer default" sends nothing). "Print to label printer"
  asks for the labels' PDF, then sends it with
  `POST /api/v1/label-printing/queues/{queue}/jobs`, and says "Sent to" with the queue's `info` (its name when `info` is empty) and the job's id
  when CUPS accepts the job. A queue that is gone (`label_queue_unknown`) reads the list again.
  When the server does not answer, the row says so, and "Print" and "Download" still work
  (`frontend/src/pages/filament/LabelQueuePrint.tsx`, `frontend/src/api/labelPrinting.ts`;
  mocks `frontend/src/mocks/features/labelPrinting.ts`).
- The fields a spool label prints (#7216, R-7216-9 and R-7216-10): the body's `fields`, else
  the setting `filament_label_output`'s, from `number`, `material` (with its subtype),
  `colour` (its name), `hex` (the effective colour as `#RRGGBB`), `brand`, `location` (its
  place's name), `temps` (the nozzle range, or one value), `weight` (the label weight in g),
  `note` (its first line), `added` (the row's `created_at` day in UTC) and `qr`. The lines
  print top to bottom in that order, whatever order the request sends. At least one field is
  chosen (422 otherwise). The default is what labels printed before: `number`, `material`,
  `colour`, `brand` and `qr`. The text block may be the label's height less twice `PAD_MM`
  (1.5 mm); the last lines go until it fits, and a chosen number always stays. The fit counts
  line heights only (`backend/scadbuddy/filament/label_pdf.py` `NUMBER_MM`, `FIELD_MM`,
  `LEADING`, `BIG_LEADING`, `GAP_MM`), never glyph widths, so the browser label keeps the same
  lines in its own font. Without `qr`, the text column starts at the quiet zone and takes the
  QR code's width too. A place label ignores the fields: it prints its code, name and QR code.
  The labels page has a box for each field under "Spool label prints" (the last chosen box
  is off), draws its labels with the same fields and fit (`labelSheet.ts` `spoolLines`,
  `fitLines`), sends them with "Download", and saves each change in
  `filament_label_output`. A change of the fields keeps the format and dpi, and a change of
  the format or dpi keeps the fields. A tick changes one box only (R-7216-18): a tick made
  before the settings arrive is applied to the stored fields, not to the default the page
  showed.
- What the labels file prints (#7216, R-7216-1 to R-7216-4): the text is DejaVu Sans, embedded,
  so a name in Latin, Cyrillic, Greek, Armenian, Georgian, Hebrew, Arabic or Lao prints as
  itself, not as `?`. A character DejaVu lacks takes the first Noto font of a fixed chain that
  has it (Devanagari, Bengali, Tamil, Sinhala, Thai, Khmer, Myanmar and Ethiopic among them;
  `backend/scadbuddy/filament/label_fonts.py` `FALLBACKS`). A character no font in the image
  holds, CJK among them, prints as U+FFFD. Not done yet (#7707): Arabic prints unjoined,
  Indic scripts print with no shaping, and Hebrew and Arabic print in stored order, not right
  to left. When the server lacks DejaVu Sans, the route answers 503 `label_font_missing`,
  which names the missing file. The browser print keeps the browser's own fonts.
- Saved sheet grids (#7216, R-7216-11 to R-7216-13): a person names a sheet layout (the page,
  `a4` or `letter`; the label size; the columns, rows, margins and gaps) and saves it. The API
  keeps it in the table `label_sheets` (`backend/scadbuddy/filament/label_sheets.py`), and no
  other process reads it. A name is 1 to 100 characters after trim and NFC, and unique with
  case ignored (409 `label_sheet_name_taken`). An install keeps at most 100 sheets (409
  `too_many_label_sheets`). A label is 40 to 190 mm wide and 25 to 190 mm tall; the grid has
  1 to 20 columns and 1 to 40 rows and must fit its page with 0.01 mm of slack (422
  otherwise). `GET /api/v1/filament/label-templates` lists them as `saved`;
  `POST /api/v1/filament/label-sheets` saves one, and `PUT` and `DELETE
  /api/v1/filament/label-sheets/{id}` replace and delete it. These writes do not nudge the
  Bambuddy sync, which has nothing of them to push. A label choice names a saved sheet by
  its id (`{template: "saved", sheet_id}`), and `POST /api/v1/filament/labels` reads the row
  at each request, so an edit applies to the next print, PDF or PNG. A choice of a deleted
  sheet answers 409 `label_sheet_gone`.
  The labels page lists the saved sheets in the template picker under "Saved sheets". "New
  sheet…" opens the sheet editor (`frontend/src/components/filament/LabelSheetEditor.tsx`:
  the name, the page, the label width and height, the columns and rows, the top and left
  margins and the column and row gaps, in mm, with a drawing of the grid on its page). The
  server's 409 and 422 text is the only check the editor shows. A saved sheet becomes the
  label choice, `filament_label_template` `{template: "saved", sheet_id}`. For a saved sheet
  that is chosen, "Edit sheet…" changes it, and "Delete" deletes it after a confirm; the page
  then goes back to the 50 x 25 mm roll label. After each save or delete, the page reads the
  template list again. It also reads it again when its tab is shown again, and before "Print",
  which waits for that read and prints on the newest grid, so a sheet that the agent edits
  while the page is open prints as edited; a failed read prints with the grid the page has
  (R-7216-21). A saved sheet prints on its own grid, as a sheet template does. When
  the stored choice names a sheet that is not in the list, the page draws at 50 x 25 mm and
  says that the saved sheet was deleted; "Download" still sends the stored choice and shows
  the 409.

## Tests

- Backend:
  - `backend/tests/filament/`;
  - `backend/tests/api/test_filament*.py`, `test_tray_spools.py`, `test_print_filaments.py`;
  - the create operations (#7150): `test_filament_spool_create_ops.py`, and create from slot
    in `test_tray_spools.py`;
  - bulk edit, archive and restore (#7151): `test_filament_spool_bulk_ops.py`;
  - `backend/tests/test_spool_refresh_replay.py`;
  - `backend/tests/profile_fetch/`;
  - `backend/tests/api/test_printer_kprofiles.py`;
  - `backend/tests/api/test_printer_slots.py`.
- Unknown spools (#7143): `backend/tests/filament/test_capture_unknown.py`,
  `test_unknown_tray_rows.py`, `test_unknown_resolve.py`, `test_link_clears_unknown_row.py` and
  `test_unknown_tray_vectors.py`; `backend/tests/printers/test_unknown_spool_block.py`;
  `backend/tests/crud/test_crud_unknown_trays.py`; the Go tests in
  `tools/go/core/internal/unknowntrays/` and `tools/go/core/unknown_trays_test.go`; the dialog's
  tests in `frontend/src/components/filament/UnknownSpool*.test.tsx` and
  `frontend/src/lib/unknownSpools.test.ts`; the tools in `agent/test/filament.test.ts`. Python
  and Go share `backend/tests/fixtures/unknown-tray-vectors.json` (colour distance and labels)
  and `unknown-trays-block.json` (the block's JSON).
- Frontend: `frontend/src/components/settings/PrinterKProfiles.test.tsx` (mocks `frontend/src/mocks/features/printerKProfiles.ts`),
  `frontend/src/components/settings/PrinterAmsSlots.test.tsx` (mocks `frontend/src/mocks/features/printerAmsSlots.ts`),
  and the AMS unit place's rename in `frontend/src/pages/filament/LocationsTab.test.tsx`;
  the #7151 operations in `components/filament/BulkBar.test.tsx`, `pages/filament/LocationPage.test.tsx`,
  `pages/filament/SpoolsTab.search.test.tsx` and `pages/filament/TagsTab.test.tsx`.
- Agent: `agent/test/printerSlots.test.ts`, `agent/test/printerKProfiles.test.ts`.

## Limits and known issues

- Before core serves the area `unknown-trays`, the print block already holds (the API reads the rows
  itself), but the list and resolve routes answer 404. The print dialog's button then says to link
  the spool from the slot's page (Filament → Locations → the place: "New spool from this tray" or the
  tray question). The Needs-you list and the Locations and place callouts stay empty, because they
  cannot know the trays.
- The filament epic is [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260).
- K-profiles and slot commands need a printer linked over ScadBuddy's LAN link: a printer
  linked only through Bambuddy is refused 409 `not_lan_linked`
  (`backend/scadbuddy/printers/native.py` `lan_row`), and no Bambuddy scope applies. The
  assistant's K-profile tools are `printer_kprofile*` and `printer_command_get`
  (`agent/src/tools/printerKProfiles.ts`, `agent/src/tools/printerCommand.ts`,
  [#7162](https://github.com/eh-homelab/ScadBuddy/issues/7162)).
- A slot preset and an AMS label are ScadBuddy's own and reach neither the printer nor
  Bambuddy. The label is the unit's place name, so renaming that place on Filament →
  Locations renames the label.
- A slot command checks the printer's latest LAN report, then sends: a print started after
  that report is not caught by ScadBuddy's guard (`backend/scadbuddy/printers/ams_slots.py`
  `guard`), whose refusals are its own 409 codes (`printer_busy`, `ams_busy`, `no_slot`,
  `printer_offline` and the rest above).
- Unknown spools (#7143): the Go resolve route is not built yet (above). A print started in
  Bambuddy, or on the printer itself, is not blocked: only ScadBuddy's own prints check. A tray
  of the external holder (D2) gets no row, so it never blocks a print. A row whose unit the
  report no longer shows stays until that unit returns; deleting or merging the printer drops it.
- Parity with Bambuddy 1.2.6b1 (#6270) is not complete yet: [filament-parity.md](filament-parity.md)
  lists every §11 item, its evidence, and the gap issue for each miss.
- Filament profiles in Postgres, and their follow-ups, are still open:
  [#2800](https://github.com/eh-homelab/ScadBuddy/issues/2800),
  [#2801](https://github.com/eh-homelab/ScadBuddy/issues/2801),
  [#2802](https://github.com/eh-homelab/ScadBuddy/issues/2802).
