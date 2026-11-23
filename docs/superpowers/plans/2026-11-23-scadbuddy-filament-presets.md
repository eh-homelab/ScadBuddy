# ScadBuddy-owned filament presets (#7176) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Filament presets flow `3DFP => ScadBuddy's store => a ScadBuddy preset id`.
ScadBuddy is the record. Bambuddy gets a mirror only when a setting enables it, and a preset
made in Bambuddy is brought into ScadBuddy by the pull and refreshed when Bambuddy edits it
(the owner's decision and addition on #7176, 2026-11-23).

**Architecture:** A new table `filament_presets` holds each preset's current document as a
blob (`blob_refs` holder `filament_preset`), with every version in
`filament_preset_versions`. A preset is named in a binding row
(`filament_spool_type_presets`, `filament_spool_presets`) by the ref `sb-<id>` in
`slicer_filament`; a generated column `preset_id` makes that ref a foreign key. The native
slicer reads `sb-<id>` refs from ScadBuddy's store (the API's Postgres and blob store; the
print worker through a CRUD route), never from Bambuddy. Every place that hands a preset to
Bambuddy (Bambuddy's slice path, the spool push, the AMS slot configure) sends the preset's
Bambuddy id instead (`EffectivePreset.bambuddy_id`, `for_bambuddy()`).

**Spec and earlier plans:** #7176 and its comment; #7174 (superseded: its `PUT` is the
mirror's update); `docs/superpowers/plans/2026-11-18-profile-fetch-worker.md` (Decision 22:
Bambuddy's import never overwrites a same-name preset);
`docs/superpowers/specs/2026-11-14-filament-management-design.md`.

## Measurement (issue step 5): which Bambuddy paths still need presets in Bambuddy

Read 2026-11-23, code at maziggy/bambuddy `main` (GitHub code search and contents API) and
prod with GET only (Bambuddy 1.3.2 at https://bambuddy.internal.nullreference.io, and
ScadBuddy's `get_settings`):

1. **AMS slot settings (needs Bambuddy presets).** Bambuddy turns a spool's
   `slicer_filament` into the printer's `tray_info_idx` and `setting_id` in
   `backend/app/services/slicer_filament_resolver.py`, which reads a local preset id from
   its own `LocalPreset` table, and `services/slot_preset_writer.py` keys the slot card by
   `local_<id>`. Three paths call it: a manual assign (`routes/inventory.py`
   `apply_spool_to_slot_via_mqtt`), the RFID auto-assign (`services/spool_tag_matcher.py`)
   and the Spoolman sync. ScadBuddy's tray answer writes Bambuddy's assignment for a linked
   spool, and `filament/rfid_slots.py` configures override slots through Bambuddy. A
   preset Bambuddy does not hold reaches the printer as a generic material.
2. **Bambuddy's spool records (needs Bambuddy presets).** Prod's `filament_sync.presets` is
   `sb_to_bb`: the push writes each linked spool's `slicer_filament` into Bambuddy. Prod's
   Bambuddy has 128 spools; 54 name a local preset (41 distinct of its 172 filament local
   presets), and 74 name a cloud or BBL id.
3. **Bambuddy's own slice path (needs Bambuddy presets when used).** Prod slices natively
   (`slicer_backend` `native`), but a request with `path: bambuddy` (the calibrations the
   LAN start cannot honour) slices in Bambuddy, which resolves a filament ref only among its
   own presets.
4. **The pull (reads Bambuddy presets).** It copies a linked spool's `slicer_filament`, a
   Bambuddy id when it names a local preset.

**Mirror default: on.** Paths 1 and 2 are in use on prod and need the preset in Bambuddy.
Without a Bambuddy URL the mirror does nothing.

## Decisions

1. **The ref.** A ScadBuddy preset is `sb-<id>` in `slicer_filament`: text, so the type and
   spool APIs, the frontend's preset table and the agent's tools keep their shape, and never
   digits, so no reader takes it for a Bambuddy local id (`resolver._is_local_id`). The
   generated column `preset_id` (`bigint`, `sb-` refs only) references `filament_presets`,
   so a binding never names a preset that does not exist.
2. **Versions.** A changed document is a new version of the same preset (same id); the
   preset row holds the current one. Each version's blob is held (`holder_id`
   `<id>:<version>`), so an old version stays readable. No route deletes a preset.
3. **Identity of a fetched preset.** One `3dfp` preset per type, printer model and nozzle
   (a partial unique index). A re-fetch puts a new version on it when the cleaned document
   differs, and its notes list the keys that changed; the same content changes nothing.
4. **Owners.** `3dfp` and `manual` presets are ScadBuddy's: Bambuddy gets a mirror, and an
   edit in Bambuddy is never read back. `bambuddy` presets are Bambuddy's: each refresh
   brings in a new version when Bambuddy's `updated_at` moved.
5. **Reading Bambuddy.** The pull's reads of Bambuddy's presets are the kept copy
   `held_local_presets` (#4645): its refresh already lists `/local-presets/` and reads
   `/local-presets/{id}` only when `updated_at` moved, on the spool refresh Schedule, in the
   API. The adoption pass runs after it in the same activity (`held_presets_refresh`), so the
   Schedule's workflow commands do not change.
6. **Adoption (the migration of existing rows, and the pull's step 2).** Each binding row
   whose `slicer_filament` is a Bambuddy local id that the kept copy holds becomes an `sb-`
   ref: to the ScadBuddy preset with that Bambuddy id, or to a new one made from the kept
   document. A new one is `3dfp` (with the run, the type and the key) when a fetch of the
   binding's type bound that id at that key and no `3dfp` preset has the key yet; otherwise
   `bambuddy`. Its Bambuddy id is kept (the mirror's, or the origin's). The bytes go to the
   blob store, so this is code, not SQL; it runs as part of the deployed refresh. A row whose
   preset the copy lacks keeps its Bambuddy id, and the native slicer reads it as before.
7. **The pull (comment steps 1 and 4).** `_bb_presets` writes `sb-<id>` when a ScadBuddy
   preset has the Bambuddy id Bambuddy sent, else the id (the next refresh adopts it). A row
   still holds "what Bambuddy sent last" when it holds that id or the `sb-` ref of the
   preset that has it.
8. **Bambuddy-facing reads.** `EffectivePreset.for_bambuddy()` is the ref Bambuddy knows (from `bambuddy_id`):
   the preset's Bambuddy id for an `sb-` ref (none when it has none), else `slicer_filament`
   as it is. The resolver on Bambuddy's catalogue, the spool push and the slot configure use
   it.
9. **The native slicer.** `Profiles.own` (API: `filament/sb_presets.py`
   `FilamentPresetStore`; print worker: `crud/print.py` `RemoteFilamentPresets` over
   `GET …/print/own-presets` and `…/documents` (area `own-presets`)) gives rows and documents. The catalogue
   offers each as a `local` choice with id `sb-<id>`, and hides the kept copy's row of a
   Bambuddy id a ScadBuddy preset has, so one preset is listed once. A slice flattens the
   document over the image's tree as it does a kept one, and keys the file by its digest.
10. **Retiring `held_local_presets`.** Not in this work: it still serves Bambuddy's printer
    and process local presets on a native run, and it is the pull's reader (Decision 5).
11. **The mirror (PR 2).** Setting `filament_preset_mirror` (bool, default on, a toggle in
    Settings, #6219). After each write and on each refresh, a ScadBuddy-owned preset whose
    `mirrored_sha256` differs from its `sha256` is sent: `PUT /local-presets/{id}` when it
    has a Bambuddy id, else `POST /local-presets/`; the id is kept. A 404 on `PUT` creates it
    again. A failure is `mirror_error` on the preset and never fails the write.
12. **The fetch (PR 2).** Writes ScadBuddy only: no zip import. K values still come from the
    printers' K tables through Bambuddy, as before.
13. **#7142 (PR 2).** The fetch writes the type's own `nozzle_temp_min`/`max` into
    `nozzle_temperature_range_low/high` and clamps `nozzle_temperature` and
    `nozzle_temperature_initial_layer` into that range, each change a note: the document is
    ScadBuddy's now, so this falls out of the fetch's write.

## PR 1: the store, adoption, the native read, Bambuddy-facing refs

- [ ] Migration `filament_presets`, `filament_preset_versions`, the generated `preset_id`
      columns.
- [ ] `filament/sb_presets.py`: refs, `FilamentPresetStore` (create, version, rows,
      documents, by Bambuddy id), `adopt_held` (Decision 6) and `refresh_bambuddy` (Decision
      4). Tests first (`tests/filament/test_sb_presets.py`).
- [ ] `held_presets_activity` runs adoption after the refresh.
- [ ] `Profiles.own`, catalogue, `slicing/presets.py`, resolver native mapping; CRUD route and
      `RemoteFilamentPresets`; wiring in the API and the print worker.
- [ ] `EffectivePreset.bambuddy_id` and `for_bambuddy()`; resolver Bambuddy path, `bb_sync_plan`,
      `rfid_slots`, `profile_fill` reads an `sb-` preset's document.
- [ ] `_bb_presets` (Decision 7).
- [ ] Docs: backend CLAUDE.md, `docs/features/spools-and-filament.md`.

## PR 2: the fetch writes ScadBuddy, the mirror, #7142

- [ ] `BambuddyClient.update_local_preset` (`PUT`).
- [ ] Setting `filament_preset_mirror` (settings, docs, frontend field).
- [ ] `filament/preset_mirror.py`: mirror one, mirror pending; after the fetch, in the refresh.
- [ ] `profile_fetch.import_run` writes ScadBuddy presets (Decisions 3, 12, 13); tests.
- [ ] Close #7174 (superseded) and #7142 when it lands.
