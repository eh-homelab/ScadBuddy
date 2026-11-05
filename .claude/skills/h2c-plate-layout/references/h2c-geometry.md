# Where the H2C numbers come from

## Plate and extruders (ScadBuddy `render/plate_profiles.py`)

- Bed 330 x 320. Extruder areas: left `0..325`, right `25..330`. A multi-colour plate
  where either nozzle may print anything must keep every object inside `25..325`.
- Wrapping exclusion area `145..251 x 310..326`: only the prime tower is refused there.
- ScadBuddy's `render/plate.py`: `PRIME_TOWER_SIDE = 60`, `PRIME_TOWER_BRIM = 3`,
  `TOWER_CLEARANCE = 5`, `EDGE_MARGIN = 2`. `bambuddy/send.py` `towers_within_reach`
  relocates a tower only if a nozzle cannot reach it, and leaves an author layout
  (`print_settings_id` = a real process name such as `0.20mm Standard @BBL H2C`) alone.

## The "no sparse layers" rule (Bambu Studio `Print.cpp`)

With `wipe_tower_no_sparse_layers` the tower is printed only on layers that change
filament, so at other layers the head crosses the tower region at object height.
Bambu Studio therefore applies `compacted_wipe_tower_clearance`:

- Any object taller than `nozzle_height` (H2C profile: 4 mm) must be a full
  `extruder_clearance_max_radius` (H2C: 96 mm) away from the tower hull. The hull is the
  tower grown by the spiral z-hop reach plus brim and padding.
- Objects inside the tower's Y band (`dist_to_rod / 2` = 25 mm either side) are limited
  to `extruder_clearance_height_to_rod` (47.4 mm) of rise; elsewhere to
  `extruder_clearance_height_to_lid` (201 mm).

Profile values read from `h2c-04.json` / `fdm_machine_common.json`:
`extruder_clearance_max_radius 96`, `extruder_clearance_radius 49`,
`extruder_clearance_height_to_rod 47.4`, `extruder_clearance_height_to_lid 201`,
`extruder_clearance_dist_to_rod 50`, `nozzle_height 4`, `z_hop 0.4`.

A 4.4 mm keychain is "tall". With sparse layers allowed the only check is the ordinary
no-overlap one (tower box plus brim and clearance against the object footprints).

## Measured layouts

- Michael + Dana test plate (2 tags): objects 110 mm from the tower, passed with
  `no_sparse_layers: true`.
- 12-tag production plates (218 x 317 mm): objects centred `136, 160`, tower `260, 130`
  (263 puts the brim past the right nozzle's reach; ScadBuddy's send moved it); printed with sparse layers allowed.
- 3-tag plate: same centre; ScadBuddy's slice placed the tower at `106, 5`, clear of the
  row at y 120..200.
