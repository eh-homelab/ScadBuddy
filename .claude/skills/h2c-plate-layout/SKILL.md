---
name: h2c-plate-layout
description: Lay out or check a ScadBuddy single-plate 3MF for the Bambu H2C before it is sliced or printed: the dual-nozzle usable area (x 25..325 of a 330x320 bed), where the prime tower can stand and how big it is, the 96 mm "no sparse layers" keep-out that makes every full keychain plate fail with "relative position of the model and the prime tower", and why outputs from ScadBuddy's default pipeline arrive on a 256x256 plate with no tower. Use it for any "does it fit", "move the tower", "where does the prime tower go", "G-code in unprintable area", arrange, plate-fit or tower-collision question about a 3MF headed for the H2C, and before any multi-plate production print.
---

# Plate layout for the H2C

ScadBuddy renders a model on a generic 256x256 plate unless the output came through
an arranged, printer-aware pipeline. Those files carry `print_settings_id: ScadBuddy`,
a 256x256 `printable_area`, the prime tower wherever the default put it (often
`98, 5`, under the objects), and every build item translated to the plate centre
`128, 128`. ScadBuddy's send step relocates a tower only when a nozzle cannot reach it, it
stands on its plate's objects' box (#3770), or the file states "No sparse layers" and the tower
is inside that keep-out (#6735);
it keeps an author layout, one whose `print_settings_id` names the H2C process, exactly
as written. So for the H2C the layout is yours to make, and `scripts/layout_h2c.py` is
the tool: `report` measures a file, `move` rewrites it.

## The numbers that bind

| Thing | Value | Source |
|---|---|---|
| Bed | 330 x 320 mm | `render/plate_profiles.py` |
| Left nozzle reaches x 0..325, right nozzle x 25..330 | usable by both: **x 25..325**, y 0..320 | same |
| Prime tower | A square (the rib wall: on by default, and no H2C process preset turns it off) whose side is Bambu Studio's estimate: sqrt(45 mm³ x N / 0.2 mm x 1.5) + 8 / sqrt(2) mm, where N is the filaments plus a nozzle change per two, at least the depth for the height (5 mm up to 5 mm tall, 20 mm at 100 mm). On the H2C, for objects up to about 60 mm tall: 36 mm for 2 filaments, 41 for 3, 48 for 4, 65 for 8. With the rib wall off it is `prime_tower_width` wide and that volume deep. Plus a brim of 3 mm, or the auto brim for the tallest object (height / 100 x 8 mm, 8 mm from 100 mm up) when that is wider; 5 mm clearance to objects; 2 mm to the edge. A 133 mm plate with the tower at y 6 fails with -104, "G-code outside of the printable area" (#5545) | `render/plate.py` `tower_footprint`, `tower_brim`; Bambu Studio `Print.cpp` `Print::wipe_tower_data` (#6735) |
| Wrapping exclusion | x 145..251, y 310..326, tower only | `plate_profiles.py` |
| `no_sparse_layers` keep-out | objects taller than 4 mm must stay **96 mm** (less 0.2) from the tower hull, which is the tower grown by the spiral lift (2.4 mm) and padded by its brim and 2 x the nozzle; ScadBuddy keeps 99.8 mm from the tower with its brim (3.8 mm plus the same for objects up to 4 mm) | Bambu Studio `Print.cpp` `compacted_wipe_tower_clearance`, `extruder_clearance_max_radius` in the H2C profile; `render/plate.py` `tower_footprint` |

The last row is the one that bites. A 4.4 mm keychain is "tall", so with
`wipe_tower_no_sparse_layers` on, no object may stand within 96 mm of the tower. A
12-tag plate is 218 x 317 mm; there is no spot on a 300 x 320 area for a tower
96 mm from all of it. The slicer refuses with `relative position of the model and the
prime tower ... No sparse layers`. Turn sparse layers back on (`prime_tower:
{no_sparse_layers: false}` in the print call); for a plate that changes colour on nearly
every layer the tower is printed anyway, so nothing is lost.

## Procedure

1. `python3 -I scripts/layout_h2c.py report in.3mf`: prints the plate the file declares, each item's footprint after its transform, the group bounding box, the tower box, and the checks (inside the dual-nozzle area, tower clear of objects, 96 mm rule if sparse layers are off).
2. Choose a target. The layout that worked for 12-tag plates: objects centred at `136, 160` (x 27..245), tower at `260, 130` (x 260..308, y 130..178 for a four-filament tower; a tower whose brim passes x 323, the right nozzle's reach less 2 mm, is moved by ScadBuddy). For a 3-tag row the same centre leaves the tower room anywhere below y 120.
3. `python3 -I scripts/layout_h2c.py move in.3mf out.3mf --center 136,160 --tower 260,130`. It shifts every build item by one delta (relative positions kept), sets `wipe_tower_x/y`, writes the H2C process and printer preset names, the 330x320 area and 325 height, and re-reports the result. Add `--no-sparse` to have the report apply the 96 mm rule.
4. Upload the result to ScadBuddy's library (`POST /print/library/files`, multipart, `type=model/3mf`) and print from the library file. Read `wipe_tower_x/y` out of the sliced file if the exact spot matters.

## Print by object

From Bambu Studio `src/libslic3r/Print.cpp` (tag `v02.08.02.61`) and the H2C profile, read for ORD-2026-2 (#6246):

- Two object hulls need about 96 mm between them: each is grown by 0.5 x `extruder_clearance_max_radius` (96) - 0.1. The skirt adds to it.
- Objects print in object-list order, not by position (the sort by position is `#if 0`).
- Height limit: the last object gets the full printable height. Every other object gets 201 mm (`extruder_clearance_height_to_lid`), or 47.4 mm (`..._height_to_rod`) if a later object overlaps its Y band (its Y range +/- 25 mm, half of `extruder_clearance_dist_to_rod`). Put tall objects last, or in a Y band no later object overlaps.
- Only `bed_exclude_area` is checked. `wrapping_exclude_area` is not checked by object.
- A prime tower is still made when timelapse is on or there is more than one filament. Pass `prime_tower: {enabled: false}` in the print or quote call (it becomes `enable_prime_tower: "0"` in the process overrides) to stop it. Arrange takes the same `prime_tower`: with `enabled: false` it keeps no room for a tower, so more fits on a plate, and the file states the tower off (#3770). A print or quote of that file with no `prime_tower` of its own sends `enable_prime_tower: "0"`, since the slicer's process preset would otherwise put a tower back. A print with `prime_tower: {enabled: true}` gets room for the tower again, or is refused when the plate cannot fit it.

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
