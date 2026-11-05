---
name: h2c-plate-layout
description: Lay out or check a ScadBuddy single-plate 3MF for the Bambu H2C before it is sliced or printed: the dual-nozzle usable area (x 25..325 of a 330x320 bed), where the 60 mm prime tower can stand, the 96 mm "no sparse layers" keep-out that makes every full keychain plate fail with "relative position of the model and the prime tower", and why outputs from ScadBuddy's default pipeline arrive on a 256x256 plate with no tower. Use it for any "does it fit", "move the tower", "where does the prime tower go", "G-code in unprintable area", arrange, plate-fit or tower-collision question about a 3MF headed for the H2C, and before any multi-plate production print.
---

# Plate layout for the H2C

ScadBuddy renders a model on a generic 256x256 plate unless the output came through
an arranged, printer-aware pipeline. Those files carry `print_settings_id: ScadBuddy`,
a 256x256 `printable_area`, the prime tower wherever the default put it (often
`98, 5`, under the objects), and every build item translated to the plate centre
`128, 128`. ScadBuddy's send step relocates a tower only when a nozzle cannot reach it;
it keeps an author layout, one whose `print_settings_id` names the H2C process, exactly
as written. So for the H2C the layout is yours to make, and `scripts/layout_h2c.py` is
the tool: `report` measures a file, `move` rewrites it.

## The numbers that bind

| Thing | Value | Source |
|---|---|---|
| Bed | 330 x 320 mm | `render/plate_profiles.py` |
| Left nozzle reaches x 0..325, right nozzle x 25..330 | usable by both: **x 25..325**, y 0..320 | same |
| Prime tower | 60 mm square plus a 3 mm brim; 5 mm clearance to objects; 2 mm to the edge | `render/plate.py` |
| Wrapping exclusion | x 145..251, y 310..326, tower only | `plate_profiles.py` |
| `no_sparse_layers` keep-out | objects taller than 4 mm must stay **96 mm** from the tower hull | Bambu Studio `Print.cpp`, `extruder_clearance_max_radius` in the H2C profile |

The last row is the one that bites. A 4.4 mm keychain is "tall", so with
`wipe_tower_no_sparse_layers` on, no object may stand within 96 mm of the tower. A
12-tag plate is 218 x 317 mm; there is no spot on a 300 x 320 area for a 66 mm tower
96 mm from all of it. The slicer refuses with `relative position of the model and the
prime tower ... No sparse layers`. Turn sparse layers back on (`prime_tower:
{no_sparse_layers: false}` in the print call); for a plate that changes colour on nearly
every layer the tower is printed anyway, so nothing is lost.

## Procedure

1. `python3 -I scripts/layout_h2c.py report in.3mf`: prints the plate the file declares, each item's footprint after its transform, the group bounding box, the tower box, and the checks (inside the dual-nozzle area, tower clear of objects, 96 mm rule if sparse layers are off).
2. Choose a target. The layout that worked for 12-tag plates: objects centred at `136, 160` (x 27..245), tower at `260, 130` (x 260..320, y 130..190; at 263 the brim passes the right nozzle's reach and ScadBuddy moves it). For a 3-tag row the same centre leaves the tower room anywhere below y 120.
3. `python3 -I scripts/layout_h2c.py move in.3mf out.3mf --center 136,160 --tower 260,130`. It shifts every build item by one delta (relative positions kept), sets `wipe_tower_x/y`, writes the H2C process and printer preset names, the 330x320 area and 325 height, and re-reports the result. Add `--no-sparse` to have the report apply the 96 mm rule.
4. Upload the result to ScadBuddy's library (`POST /print/library/files`, multipart, `type=model/3mf`) and print from the library file. Read `wipe_tower_x/y` out of the sliced file if the exact spot matters.

## Why the meshes centre the way they do

Default-pipeline files hold one object per colour group, all origin-centred, each
item's translation being the plate centre. Arranged-pipeline files hold one object per
part with min-corner translations. `report` computes real footprints from the vertices
times the item matrix, so both shapes measure correctly; `move` applies a delta rather
than an absolute position for the same reason.

## See also

`references/h2c-geometry.md` for the derivation of the keep-out rule from Bambu
Studio's source and the H2C profile, and `h2c-print-from-scadbuddy` for the print call
this feeds.
