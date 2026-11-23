# Filament settings: auto-add of unknown RFID tags, drying defaults

Issue #7156, a gap of the §11 parity sweep (#6270, `docs/features/filament-parity.md`) against
Bambuddy 1.2.6b1. Epic #6260. Spec `docs/superpowers/specs/2026-11-14-filament-management-design.md`
§5 ("Thresholds (low stock, humidity, drying presets) are settings with per-material and per-type
overrides, never constants") and §11 ("Settings: … `auto_add_unknown_rfid`, … drying presets").

## What happens today (origin/main 25bd69719, 2026-11-23)

- An AMS tray with an RFID tag that no SB spool holds: capture (`filament/capture.py`
  `reconcile`) ignores it. Only Bambuddy acts, when its own `auto_add_unknown_rfid` is on: it
  makes a BB spool, and SB's pull (`filament/bambuddy_pull.py`) imports it under
  `filament_sync.spools`. SB has no setting of its own. The RFID override mode
  (`filament/rfid_mode.py`) holds Bambuddy's value false and puts it back when it turns off.
- The unknown spool flow (#7143, design `docs/superpowers/specs/2026-11-23-unknown-spool-flow-design.md`,
  plan `docs/superpowers/plans/2026-11-23-unknown-spool-flow.md`, built in another session) adds
  SB's own path: capture makes and links a spool from an unknown tag (its Goal 2), and an
  untagged tray becomes a "Needs you" row that blocks SB's prints to the printer.
- Drying: a spool's drying defaults (`filament/spools.py` `_drying_defaults`) are its own, else
  its type's, else its subtype's, else its material's. Nothing comes after the material, so a
  spool of a material with no values gets no suggestion. Bambuddy keeps `drying_presets`, a JSON
  map per filament type in its settings (`backend/tests/bambuddy/recordings/openapi/rfid-routes.json`).

## Rulings

- **R-7156-1. SB gets its own setting `filament_auto_add_unknown_rfid`** (bool, on by default).
  It says whether SB lets a spool be made for an RFID tag it does not know, with no person. It
  governs SB, not Bambuddy's own toggle: SB enforces it on both paths that can make such a
  spool (Bambuddy's auto-add with SB's pull today, and #7143's capture once it is built). On
  holds nothing: SB leaves Bambuddy's auto-add as it is, so Bambuddy may add the spool and the
  pull brings it in, unless the RFID override mode holds it off. SB's own capture makes no
  spool from a tag on main today (`filament/capture.py` `reconcile` ignores an unknown tag).
  On is the default, as Bambuddy ships its own.
- **R-7156-2. Off means no system makes a spool from an unknown tag.** SB holds Bambuddy's
  `auto_add_unknown_rfid` false through the override mode's state-based reconcile (one saved
  value, read first, written only on a difference, put back when nothing holds it). #7143's
  capture must read the same setting and, when it is off, treat an RFID tray as untagged (a
  "Needs you" row the owner links). This PR does not touch #7143's code or branch; #7143's
  builder reads the rule here and on #7156. On, SB writes nothing to Bambuddy for this setting
  and leaves Bambuddy's value as it is (R-7156-1).
- **R-7156-3. The setting and the override mode are independent holds on one value.** Bambuddy's
  value is held false while `override_mode OR NOT filament_auto_add_unknown_rfid`. The saved
  value is kept until neither holds, then put back. Neither switch changes the other. The status
  (`GET /filament/rfid/status`) answers `auto_add_unknown_rfid` beside `override_mode`, and
  `applied` means "held, and Bambuddy reads false" whichever holds it.
- **R-7156-4. The setting is stored only** (no environment variable), as
  `filament_shopping_auto_add_empty`: the reconcile's SQL guards read it in the same statement
  as the mode, and #7143's capture reads it inside its savepoint. No row reads as on. A
  `PUT /settings` that changes it runs the reconcile once (lock tried for 5 s, as the mode's
  PUT); a failure never fails the save, and the refresh Schedule retries.
- **R-7156-5. Drying defaults are two live settings**, `filament_drying_temp_c` (°C, 1 to 150)
  and `filament_drying_hours` (0.1 to 999.9, R-7156-7). Both are empty by default: SB suggests
  no drying for a spool that nothing in the catalog sets, because one global value is wrong for some
  material (a TPU or PA dried at a PLA temperature), and a wrong suggestion can deform spools.
  They are the last level of the chain: spool, type, subtype, material, then the setting
  (`DryingDefaults.*_from` `setting`). Not a per-material map as Bambuddy's: SB's
  per-material values are the catalog's (spec §5), which override the setting.
- **R-7156-6. Where they apply.** Everywhere SB suggests a drying today, which is
  `_drying_defaults`: the spool page's "Record drying" form and its "from …" line
  (`SpoolDetail.drying_defaults`), a `dried` event that leaves out `temp_c` or `hours`
  (`POST /filament/spools/{id}/events`), and the agent's `filament_spool_get` and its drying
  tool (they read the same answer). The profile fill (`filament/profile_fill.py`) writes types,
  not suggestions, and is unchanged.
- **R-7156-7. One bound for the drying hours everywhere: 0.1 to 999.9.** The backend field is
  `ge=Decimal("0.1"), le=Decimal("999.9")` (`core/settings.py`, `library/settings_store.py`), the
  form's `values.ts` `DRYING` and the msw mock refuse the same values, and the mock names the side
  a value is out on. Decided in the review of #7206.

## Tasks

1. Backend, RFID: `StoredSettings`/`SettingsPatch` field, `SettingsView` field, `setting_docs`.
   `RfidModeStore` reads the setting in SQL (`state()` gains `auto_add_off`; `save_found` and
   `clear_saved` guard on the hold), `reconcile_mode`, `reconcile_mode_now` and `read_status`
   use the hold, `RfidStatus.auto_add_unknown_rfid`. `PUT /settings` reconciles when it changes.
   Tests: `tests/filament/test_rfid_mode.py` (hold by the setting; restore when both are off;
   the mode off keeps the hold), `tests/api/test_settings.py` (save, default, reconcile on save
   through respx).
2. Backend, drying: `Settings`, `SettingsPatch`, `RuntimeView` fields, `live`, docs;
   `FilamentDefaults` gains the two values (`component.py` reads them); `_drying_defaults` falls
   back to them. Tests: spool detail and a `dried` event with the setting.
3. Frontend: Settings → Filament gets the auto-add switch (stored, saved with "Save Filament")
   and the two drying fields (`fields.ts`); the override row's status line says when the setting
   holds Bambuddy's value; the spool page names "Settings" as a source. msw mocks and vitest.
4. Agent: the drying tools' descriptions name the setting level.
5. Docs: `docs/features/filament-parity.md` (two rows to MATCH), `spools-and-filament.md`,
   `settings-and-sign-in.md`.
