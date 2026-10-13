# Filament profiles: per-filament process changes in the print dialog (#717)

Status: first slice shipped in #2804; the override's effect measured against the live
Bambuddy (#2803), 2026-10-13. Follow-ups: #2799 (filament-setting channel), #2800
(storage, Settings editing, more built-ins), #2801 (media), #2802 (orientation check),
#2803 (this measurement).

## 1. What a profile is

A named, cited set of process changes that the print dialog suggests when a chosen spool
matches it, such as Silk PLA's slower outer wall
(`backend/scadbuddy/bambuddy/filament_profiles.py`). It is offered, never applied
silently. The dialog shows each change against the value it replaces, with its sources,
and sends the ids the user left on (`PrintRunRequest.filament_profiles`).

The one built-in is **Silk PLA** (`silk-pla`). It matches a PLA spool whose name says
silk and sets `outer_wall_speed` to 50 mm/s. Its 235 °C nozzle advice is a note only:
filament settings have no verified channel yet (#2799).

## 2. How a change reaches the slicer

Each change goes out as `SliceRequest.process_overrides`. Bambuddy writes those into the
process JSON it slices with (`services/process_overrides.py`), after the preset.

The change is written in the shape the preset stores the setting. On the H2C,
`outer_wall_speed` is one entry per extruder variant (`0.20mm Standard @BBL H2C`:
`["200", "500", "500", "200", "500"]`), so ScadBuddy first reads the preset's values
(`GET /slicer/preset-values`) and sends the value once per entry.

A profile that cannot apply is skipped, never refused. That covers unreadable values, a
preset without the setting, or no resolved process. Its overrides are not sent in a
guessed shape, and a `profile-not-applied` warning says so in Checks and in the run's
warnings. The print then uses the preset as it is.

## 3. Measured: the override reaches the slice (#2803, 2026-10-13)

Two slices of the same file, through Bambuddy's own slice dialog, which sends the same
`process_overrides` field ScadBuddy does. The run was slice-only: nothing was queued or
printed, and both outputs were deleted and purged from the trash afterwards.

- **Bambuddy:** the live instance.
- **Source:** library file 188, `fmtest-auto.3mf` (62 KB, one plate).
- **Presets:** `Bambu Lab H2C 0.4 nozzle`, `0.20mm Standard @BBL H2C`, and
  `Bambu PLA Basic @BBL H2C` in both slots, on the Textured PEI Plate.
- **Plain slice:** no overrides, job 21, output file 3618.
- **Silk slice:** job 22, output file 3619. Its request body carried
  `"process_overrides": {"outer_wall_speed": ["50", "50", "50", "50", "50"]}`, which is
  exactly what `overrides()` builds for this preset.

Read from each output's `Metadata/project_settings.config` and `Metadata/plate_1.gcode`.
In the G-code, outer-wall moves are the extrusions under `; FEATURE: Outer wall`.

| | Plain (3618) | Silk profile (3619) |
|---|---|---|
| `outer_wall_speed` | `200, 500, 500, 200, 500` | `50, 50, 50, 50, 50` |
| `inner_wall_speed` (control) | `300, 600, 600, 300, 600` | `300, 600, 600, 300, 600` |
| Outer-wall extrusion moves | 5928 | 5928 |
| … at F12000 (200 mm/s) | 5743 | 0 |
| … at F3000 (50 mm/s) | 90 | 5833 |
| … at F1800 (30 mm/s, slowdowns) | 95 | 95 |
| Fastest outer-wall feed | 200 mm/s | 50 mm/s |
| Inner-wall moves at F18000 (300 mm/s) | 5673 | 5673 |
| Estimated time (job / G-code header) | 1345 s / 22m 25s | 1505 s / 25m 5s |
| Filament | 7.63 g | 7.63 g |

What this shows:

- **The setting is applied as a vector.** All five extruder variants are 50, so sending
  the value once per entry keeps the preset's shape.
- **The outer wall really slows.** No outer-wall move is faster than 50 mm/s, while the
  plain slice ran them at the 200 mm/s left-extruder value. The 30 mm/s moves are the
  slicer's own slowdowns and are the same in both.
- **Nothing else changes.** Inner walls, move counts and filament are identical.
- **The cost is time.** On this small part the estimate rose by 2m 40s, about 12%.

## 4. Nozzle temperature: no channel yet (#2799, 2026-10-13)

Silk's 235 °C stays a note, because no tested route sets one slot's nozzle temperature
for a slice.

- **The slice dialog has none.** Bambuddy's settings panel lists process options only.
  Searching "temperature" with Expert on finds only the ooze-prevention variation and
  the prime-tower cooldown.
- **The slice API has no filament field.** Its `SliceRequest`
  (`backend/app/schemas/slicer.py`) carries filament presets and colours, and two kinds
  of override. `design_overrides` carries only the source 3MF's process keys (index 0
  of `different_settings_to_system`). `process_overrides` goes into the process JSON.
- **One route is untested.** `apply_process_overrides` merges any lowercase key into
  the process JSON, so a `nozzle_temperature` override would reach the slicer's
  `--load-settings` process file. Whether the slicer honours a filament key there, or
  the filament preset wins, has not been measured. The dialog cannot send the key, and
  a direct slice request was not permitted in this session.
- **Plain baseline,** measured on file 188 with H2C 0.4, `0.20mm Standard @BBL H2C`
  and 2× `Bambu PLA Basic @BBL H2C`:
  - `nozzle_temperature` and `nozzle_temperature_initial_layer` are both `["220"]`
    ×6, which is six entries for two filaments, per variant like the speeds.
  - The G-code sets `M104 S220` four times.

  A test of the untested route must therefore send six entries, not one per slot.

Until that is measured, or Bambuddy gains a filament override, the temperature is set
in the spool's filament preset, as the note says.
