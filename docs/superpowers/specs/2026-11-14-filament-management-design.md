# Filament management in ScadBuddy

Date: 2026-11-14. Status: approved by the owner on 2026-11-14. Epic: #6260.

## 1. Intent

ScadBuddy (SB) becomes the system of record for filament. It must have every
spool feature that Bambuddy (BB) has, and more:

- spool types and spool instances
- RFID tags that you can re-bind
- a ledger of every event that changes a spool
- suppliers, price sheets and invoices
- a configurable sync with BB

Why:
- The values must be current at each event: spool loaded, before a print,
  after a print. Today SB polls BB every 5 minutes
  (`spool_overlay_refresh_seconds`, `backend/scadbuddy/core/settings.py:686`).
- SB must record what happened, not only the current value.
- The owner buys filament from several suppliers and wants to see cost and
  price history for each spool.
- The owner re-uses RFID tags.

This reverses an earlier decision. The own-store spec made replacing BB's
inventory a non-goal
(`docs/superpowers/specs/2026-10-16-own-store-slicer-rust-workers-design.md:52-58`),
and the orders spec left stock counts to BB "until a ScadBuddy filament
inventory exists" (`2026-10-18-orders-design.md` §1.2). This design is that
inventory.

## 2. Goals, non-goals, anti-goals

Goals:
- Every BB spool field and feature has an SB equivalent (§11 lists them).
- A first-class **Filament** section in the app. Settings keeps only
  configuration.
- Spool types (products) and spool instances (physical spools).
- An event ledger for each spool. Current values are derived from it.
- Capture driven by events: MQTT tray changes, print start and end.
- RFID: Bambu tags re-bound to any spool, with overrides. NTAG tags through
  BB's SpoolBuddy reader and writer.
- Suppliers, supplier products with URLs, dated price points, invoices
  (1 invoice to N spools).
- Invoice import from an uploaded or pasted PDF or image, done by an assistant
  flow.
- Sync with BB, configurable at system level for each data type: off,
  SB→BB, BB→SB, or two-way.
- An AMS weight sync and a scale input in SB.
- Every vocabulary (materials, subtypes, brands, categories, effects, insert
  types, location kinds) is data that a person edits. Nothing is hard-coded.
- Locations as a tree, with AMS slots as locations, sensors, and moves in the
  ledger.
- A full history for each spool, and metrics per spool and for the inventory.

Non-goals:
- Writing a Bambu tag that the AMS accepts. This is impossible: the tag carries
  a 2048-bit RSA signature and the key is Bambu's
  ([RFID-Tag-Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide)).
- Making a stock AMS read NTAG tags. It reads only Bambu-signed tags
  ([OpenSpool](https://github.com/spuder/OpenSpool)).
- A Spoolman-compatible API (approach C, §4). It can come later as a mode.
- Moving Parts (`/orders/parts`) into Filament.
- Purchase orders and stock reorder automation. The shopping list is in scope
  (BB parity); automatic ordering is not.

Anti-goals (the work fails even if it runs):
- BB's auto behaviour silently overwrites an SB override. Examples: a re-used
  tag's remain %, or `auto_add_unknown_rfid` creating a duplicate spool.
- A spool value with no source. Every number must trace to a ledger event.
- An import that creates spools twice after a retry.
- Data management left inside Settings.

## 3. Constraints

- **Database rule** (CLAUDE.md, #5295): only the CRUD service (the API process)
  has SQL access. Workers, the agent and flows reach this data through CRUD
  routes with a service credential. Every new cross-service route requires that
  credential from its first PR.
- **Bytes** go to the blob store (ContentStore), never `bytea`. Images and PDFs
  move as blobs and `{name, mediaType}` pointers, never base64 in text
  (`lint-base64-images.sh`, #2479).
- **Migrations** follow #491: one new timestamped file each, never edit a
  merged one.
- **Every tool works in every mode** (classic and durable agent).
- The BB API is the one measured on 2026-11-14: Bambuddy 1.2.6b1,
  `https://bambuddy.internal.nullreference.io/openapi.json`, 638 paths. The
  endpoints and schemas named below come from that spec.
- **The RFID routes were re-measured on 2026-11-17 against Bambuddy 1.3.1** (the spec's
  1.2.6b1 is out of date; the live instance moved). Method: read-only. `GET` requests and
  one listen-only WebSocket that sent nothing, with no API key, against
  `https://bambuddy.internal.nullreference.io`; Bambuddy's source read at tag
  [`v1.3.1`](https://github.com/maziggy/bambuddy/tree/v1.3.1/backend/app). The recording is
  `backend/tests/bambuddy/recordings/openapi/rfid-routes.json` (#6264).
  - `GET /openapi.json`: 200, `info.version` 1.3.1. `GET /api/v1/auth/status`:
    `{"auth_enabled": false, "requires_setup": false}`. Auth is off, so no key is needed and
    override mode can write the setting.
  - `GET /api/v1/settings/`: `auto_add_unknown_rfid` was `true` at that time.
  - `GET /api/v1/spoolbuddy/devices`: 200, `[]`. No SpoolBuddy device is registered, so the
    scan and write tests use recorded bodies.
  - `GET /api/v1/inventory/spools/by-tag?tray_uuid=<a live tray_uuid>`: 200, a
    `SpoolResponse` with `tag_type: "bambulab"` and `data_origin: "rfid_linked"`.
    `?tag_uid=00000000`: 404 `{"detail": "Spool not found"}`. No argument: 400
    `{"detail": "Provide tray_uuid and/or tag_uid"}`. The live inventory (106 spools) holds
    `data_origin` `rfid_linked` on 33, `rfid_auto` on 7 and none on 66.
  - `wss://.../api/v1/ws` with no token: handshake 101. In 15 s it sent 7 frames, all
    `printer_status` (keys `type`, `printer_id`, `data`). No `spoolbuddy_*` frame came,
    because no tag was scanned. The event fields are from the source (below).
  - `data_origin` values that Bambuddy writes (`backend/app`, v1.3.1): `rfid_auto`
    ([`services/spool_tag_matcher.py` line 265](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/services/spool_tag_matcher.py#L265)),
    `rfid_linked` ([same file, line 417](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/services/spool_tag_matcher.py#L417)),
    `nfc_link` ([`api/routes/inventory.py` line 2747](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/inventory.py#L2747)
    default, applied at lines 2856-2857) and `opentag3d`
    ([`api/routes/spoolbuddy.py` line 1149](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/spoolbuddy.py#L1149)).
  - **Not measurable without a write, so taken from the source and tested only with respx:**
    the 409 `tag_already_linked` body
    ([`services/tag_conflict.py` lines 24-40](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/services/tag_conflict.py#L24-L40),
    raised at `inventory.py` lines 2815 and 2839), the clear by `PATCH` with null
    (`inventory.py` lines 1700-1717, `model_dump(exclude_unset=True)`), the configure answer
    and the write-tag answer (`spoolbuddy.py` lines 826-957).
  - Scopes (Bambuddy's `Permission` in the route's dependency, mapped through
    [`_APIKEY_SCOPE_BY_PERMISSION`](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/core/auth.py#L90)
    to an API-key flag; the last column is ScadBuddy's `Scope`, `bambuddy/errors.py`):

    | Route | Bambuddy permission (line, v1.3.1) | Key flag | ScadBuddy `Scope` |
    |---|---|---|---|
    | `PATCH /inventory/spools/{id}/link-tag` | `INVENTORY_UPDATE` (`inventory.py` 2771) | `can_manage_inventory` | `MANAGE_INVENTORY` |
    | `GET /inventory/spools/by-tag` | `INVENTORY_READ` or `INVENTORY_UPDATE` (`inventory.py` 1560) | `can_read_status` or `can_manage_inventory` | `MANAGE_INVENTORY` (the plan's choice; either flag opens it) |
    | `PATCH /inventory/spools/{id}` (clear) | `INVENTORY_UPDATE` (`inventory.py` 1693) | `can_manage_inventory` | `MANAGE_INVENTORY` |
    | `GET /settings/` | `SETTINGS_READ` (`settings.py` 344) | `can_read_status` | `READ_STATUS` |
    | `PATCH /settings/` | `SETTINGS_UPDATE` (`settings.py` 552) | none: denied for every key (`core/auth.py` line 251) | `ADMIN_SETTINGS` |
    | `POST /printers/{id}/slots/{ams}/{tray}/configure` | `PRINTERS_CONTROL` (`printers.py` 2963) | `can_control_printer` | `CONTROL_PRINTER` |
    | `GET /spoolbuddy/devices` | `INVENTORY_READ` (`spoolbuddy.py` 280) | `can_read_status` | `READ_STATUS` |
    | `POST /spoolbuddy/nfc/write-tag` | `INVENTORY_UPDATE` (`spoolbuddy.py` 830) | `can_manage_inventory` | `MANAGE_INVENTORY` |
    | `POST /spoolbuddy/devices/{id}/cancel-write` | `INVENTORY_UPDATE` (`spoolbuddy.py` 1181) | `can_manage_inventory` | `MANAGE_INVENTORY` |
    | `POST /auth/ws-token` | `WEBSOCKET_CONNECT` (`api/routes/auth.py` 672) | `can_read_status` | `READ_STATUS` (needed only while auth is on) |

    `nfc/tag-scanned`, `nfc/tag-removed` and `nfc/write-result` are the SpoolBuddy daemon's
    routes (`INVENTORY_UPDATE`, `spoolbuddy.py` 582, 813, 963); ScadBuddy does not call them.

## 4. Approaches

- **A. SB is the system of record. A Temporal sync workflow keeps BB in step.**
  Chosen by the owner. It is the only approach that supports all four sync
  settings and gives SB control of RFID matching.
- **B. BB stays the owner. SB adds side tables keyed by BB spool id.** Cheapest,
  but SB→BB has no meaning and RFID overrides depend on BB's matching. Rejected.
- **C. SB serves a Spoolman API and BB runs in its Spoolman mode**
  (`spoolman_enabled`, `/api/v1/spoolman/*`). BB then pushes usage to SB with no
  sync code. But BB's own inventory goes idle, so the BB→SB and two-way settings
  mean nothing. Rejected as the base; possible later mode.

## 5. Data model

All tables are in the CRUD service's database. Names are proposals; the builder
can change them.

### 5.1 Catalogs: nothing hard-coded

**Rule (owner, 2026-11-14): every vocabulary a person sees is data, edited in
the Filament section, never an enum in code.** SB ships seed rows, and a person
can add, rename, archive and re-order every one. Code keeps only the concepts it
acts on: event kinds, event sources, tag kinds, and sync directions.

- `materials`: name (PLA, PETG, PETG-CF ...), family, default density,
  nozzle and bed temperature ranges, drying temperature and hours, flags
  (needs enclosure, abrasive, hygroscopic, flexible), the hardened-nozzle
  requirement, note. A spool type's material is a reference to this table, and
  the type's own values override the material's defaults (the same
  type → instance rule as §5.3, one level up).
- `material_subtypes`: name (Basic, Matte, Silk, HF ...), optional material,
  value overrides like a material's.
- `brands`, `categories`, `effects` (Sparkle, Wood, Marble ...): name, sort
  order, archived.
- `insert_types`: the empty spool. Name, empty weight (g), insert material (a
  reference to `insert_materials`: cardboard, plastic, Bambu reusable with
  refill ... all rows), note. BB's spool catalog (`/inventory/catalog`,
  `CatalogEntry {name, weight}`) is the BB side.
- Locations: §5.6.
- Thresholds (low stock, humidity, drying presets) are settings with
  per-material and per-type overrides, never constants.
- `colours`: manufacturer, name, RGBA, material, extra colours, effect. BB
  parity with `/inventory/colors`, including its sync from filamentcolors.

**Colour is RGBA everywhere (owner, 2026-11-14: transparent and translucent
filaments).** Every colour SB stores is `#RRGGBBAA`: types, instances, extra
colour stops, the colour catalog, the price book, tray fingerprints. Alpha `FF`
is opaque. Bambu writes translucent filaments with alpha `80`, and BB's tag
linking needs the exact RGBA, alpha included (the `filament-intake` skill's
traps table). A `#RRGGBB` value from an old row or a person reads as alpha
`FF`.

Before #6261, SB dropped the alpha in several places; each changes:
- `native_spools.colour` was `^#[0-9A-Fa-f]{6}$`; since #6261 native spools are
  RGBA spools. The native API showed the first 7 characters until #6262 removed it (the
  `/native-spools` routes are gone; the Filament section and the SB spool routes carry
  `#RRGGBBAA`).
- The tray fingerprint is `material|RRGGBB` (`spools/models.py:59-62`), so a
  clear and an opaque spool of one hue look alike.
- The price book kept `#RRGGBB` (`orders/bambuddy.py:63-64`, `_colour`). #6209 closed
  that gap in #6211, before #6261: `filaments.colour` stays `#RRGGBB` beside `alpha`,
  `extra_colours` and `effect` (`20261114T1607Z_filaments_alpha_effect.sql`), and a
  type's book row takes its alpha (#6261, `filament/types.py` `_link_price_book`).

Where alpha is used:
- Matching (print dialog nearest colour, BB type matching §8.1, tag
  fingerprints) compares alpha too. A translucent spool never matches an
  opaque one by colour alone.
- Swatches draw on a checkerboard, so a person sees the transparency. The
  colour picker has an opacity control and presets for clear (alpha `00`-`20`)
  and translucent (`80`).
- The 3D preview draws a translucent slot with the slot's opacity.
- "Translucent" and "Clear" stay effects or subtypes as data (§5.1); alpha
  is the value that matching and rendering read.
- What the 3MF sent to the slicer carries for a translucent filament is not
  measured. The builder measures whether Bambu Studio and the H2C read alpha
  in a 3MF's filament colour before writing it there; the OpenSCAD export's
  alpha byte is ignored today (CLAUDE.md, verified facts).

### 5.2 Spool type (a product)

`spool_types`:
- `id`, `code` (short, unique; it goes to BB as `material_number`)
- brand, material, subtype, category and effect (references to the §5.1
  catalogs), colour name, `rgba`, extra colours
- diameter, density, nozzle and bed temperature ranges
- label weight (g), `insert_type_id`
- low-stock threshold %, default cost per kg
- archived

Children:
- `spool_type_presets`: printer model, nozzle diameter, slicer filament id and
  name. BB parity with `SpoolFilamentPreset`.
- `spool_type_k_values`: printer, extruder, nozzle diameter, nozzle type, K,
  name, `cali_idx`, `setting_id`. BB parity with `SpoolKProfile`.

  As built in #6714 and #6720 (plan `docs/superpowers/plans/2026-11-20-filament-type-from-profile.md`,
  R-6714-9 to R-6714-21): a type also has `drying_temp_c` and `drying_hours`, and a spool may
  override both, as any value of its type. A spool's drying defaults are its own, else its
  type's, else its subtype's, else its material's; each drying may still change them. The
  slicer profile fill writes a type's empty print and drying values (the AMS HT's idle drying
  pair, which can be above an AMS 2 Pro's 65 °C), except a drying value the type's subtype or
  material already sets (only a refresh offers that one). It imports K values: its linked
  Bambuddy spools' while it has none, then each linked printer's K calibrations whose
  `filament_id` is a preset's `slicer_filament` (`GET /printers/{id}/kprofiles/`). Each K row
  says who set it (`source`: `person`, `profile`, `printer_calibration`, `profile_fetch`;
  rows from before are `person`). A calibration fills a key with no row, or replaces a
  `printer_calibration` row of its key that differs; it never replaces a person's, the
  fill's or the fetch's. A person's save tags each row it adds or changes `person`.
- `supplier_products`: supplier, SKU or article number, product URL, pack size
  (spools per order line), note.
- `price_points`: supplier product, price, currency, price per kg, date, source
  (`invoice` with the line id, or `manual`).
- `sku_settings`: lead time days, safety margin. BB parity with
  `FilamentSkuSettings`.

  As built in #6269 (plan `docs/superpowers/plans/2026-11-18-filament-history-metrics.md`,
  Decision 9): `sku_settings` is two columns of `spool_types`, `lead_time_days` and
  `safety_margin_days` (whole days, 0 to 365, null means the setting), with the settings
  `filament_lead_time_days` (7) and `filament_safety_margin_days` (3). A type's reorder day
  is its empty day minus both, and "reorder now" means that day is today or past. There is
  no separate table.

The existing price book (`filaments`, `20261022T1704Z_filaments.sql`) has the
same identity (material, subtype, brand, colour). It becomes a view over
`spool_types` and the newest price points, so quotes and job cost keep working.
The builder decides whether to migrate its rows into types or keep a view.

### 5.3 Spool instance (a physical spool)

`spools`:
- `id` (uuid, the fixed SB identity), short human number for labels
- `spool_type_id`, nullable. A spool with no type is in the **needs type**
  state: it has its own material and colour, it shows a badge, and it can still
  be picked for a print. A person, or the import, sets the type later. (Our
  call: blocking these spools would stop prints from a loaded tray.)
- `invoice_line_id` (nullable), cost per kg (from the line, else the type)
- overrides (builder's call: nullable columns or a jsonb)

**Rule (owner, 2026-11-14): every value is defined on the type and can be
overridden on the instance.** This includes colour, weights, insert type,
temperatures, cost, presets and K values. The effective value is the
instance's override if it is set, else the type's value. The UI shows which
values are overridden and can reset each one to the type.
`spool_presets` and `spool_k_values` hold the per-instance overrides of
`spool_type_presets` and `spool_type_k_values`.
- `location_id`, note
- archived, created

Derived from the ledger and stored for reads: grams left, last weighed weight
and time, last dried (time, temperature, hours), last used, weight lock, current
tray.

`native_spools` and `physical_spools` existed before #6261 (both were dropped in #6348, after #6262 stopped reading them). `physical_spools` already
gives an identity keyed by RFID
(`20261018T1744Z_physical_spools.sql`). The migration (our call; as built in
#6261, `20261114T1302Z_filament_from_native.sql`):
- A `physical_spools` row that a `native` record link names becomes a `spools`
  row with the same id. Its fields come from the native record.
- A `native_spools` row with no physical spool becomes a `spools` row with the
  native id.
- Any other physical spool (only a BB record, or only seen in a tray) is not
  migrated as a spool. Its keys become a `tags` row with no binding. For one with
  a BB record, the first BB pull (§8.1) makes a spool with a new id, links it to
  the BB spool and binds the tag to it.
- A native spool's `tray_uuid` and `tag_uid` become a `tags` row with an open
  binding, or, for an archived spool, a closed one (reason `archived`).
- Migrated spools have no type (needs type) until the BB import (§8.1) or a
  person sets one.

### 5.4 Tags

- `tags`: `id`, kind (`bambu_mifare`, `ntag_openspool`, `ntag_opentag`,
  `other`), `tag_uid`, `tray_uuid`, raw data blob (optional), note.
- `tag_bindings`: tag, spool, bound at, unbound at, reason, **override** flag.
  One open binding per tag and per spool at a time.

When a tag is re-bound, SB closes the old binding and writes a ledger event on
both spools.

### 5.5 Ledger

`spool_events`: spool, time, kind, source, printer, AMS and tray, grams before,
grams after, delta, reference (print run, BB archive, invoice line, scale
device, person), data jsonb.

Kinds: `created`, `loaded`, `unloaded`, `moved`, `print_started`,
`print_ended`, `weighed`, `ams_reading`, `dried`, `edited`, `tag_bound`,
`tag_unbound`, `archived`, `restored`, `synced_from_bb`.

Sources: `ams_remain`, `scale`, `print_estimate`, `print_actual` (BB archive
actuals or SB LAN accounting), `person`, `bb`.

Rule for grams left (our call): take the newest **absolute** reading as the
anchor, then subtract the **deltas** of every later event.
- Absolute readings: `scale`, `person` (a typed value), `ams_remain`
  (remain % × label weight), and `bb` (a BB weight copied in).
- Deltas: `print_actual`, and `print_estimate` while its print runs.
- When a print ends, its `print_actual` replaces its `print_estimate`. A print
  that fails or is cancelled keeps the actual grams if they are known, else the
  estimate is dropped.
- An `ams_remain` reading is an anchor only if it is newer than the newest
  `scale` or `person` reading and the spool is not locked. With the weight lock
  on, `ams_remain` is recorded but never used.
- The weight lock guards against estimates only: `ams_remain` and BB's own
  `label_weight - weight_used`. A scale weighing always counts, a BB one
  included (BB's `last_scale_weight` is gross, so SB books it net of the
  insert's empty weight, else BB's `core_weight`).
- Order is physical time (as built in #6261, changed in #6263). Each event
  carries when it happened: a print's `print_started` when the printer takes the
  plate, its `print_ended` at the report that shows the plate's timed end
  (`FINISH` or `FAILED`; the settle's clock time when no report showed it), a BB
  reading at the BB row's `updated_at`, a BB usage row at its `created_at`, a
  scale reading at its weighing time. An event with no time of its own gets the
  clock time, kept after the spool's newest event (the WSL clock can step
  back). Events of one time keep insert order (`seq`). Three rules (#6263):
  1. **At one instant, deltas come before readings.** A reading taken at the
     moment a plate ends holds that plate. BB's usage row and its spool's
     `updated_at` carry the same time, so a BB reading holds its own usage row.
  2. **An `ams_remain` reading taken while the printer prints is recorded, but
     is never an anchor.** The event carries `data.printing = true`. The AMS
     remain % moves in steps during a plate, so it does not tell the grams left
     after the plate.
  3. **One plate is counted once.** SB's `print_started` and `print_ended`
     events (`ref_kind = print_run`) and BB's usage row (`ref_kind = bb_usage`)
     are paired when they name the same spool and the same job (SB's
     `subtask_name`, which is BB's `print_name`). For a pair, SB's events are
     skipped and BB's grams count, because BB keeps a failed plate's partial
     grams and SB books 0 g for one. The pair is counted once, when it lies
     after the anchor. The ledger compares each anchor in its own clock: an
     anchor from BB (`bb_spool`, `bb_scale`, `bb_dried`) against BB's usage
     time, any other anchor (AMS, person) against SB's time of the plate (the
     end, else the start). So the skew between BB's clock and SB's cannot
     count a plate twice.
- The UI shows the anchor and the deltas that make the current value.

Known limits of the time rule (#6261 and #6263):
- Measured on the live BB on 2026-11-15 (§8.2): BB deducts `weight_used` in the
  write that ends the print, at the usage row's `created_at`, which equals the
  spool's `updated_at`. That is why a BB reading holds its own usage row
  (rule 1).
- A print BB did not deduct (BB was down at its end) and a later BB reading: SB's
  plate sits inside the BB anchor's time but not inside its value, so it is
  under-counted until the next scale or person reading.
- A plate that BB credits to another spool (the spool BB assigned to the tray,
  not the one SB printed from) counts on two spools: SB's record on SB's spool,
  and BB's usage row on the other. #6264 and #6265 fix this when they sync the
  tray assignment.
- A BB usage row with no SB record (a print SB did not start) is compared with
  an AMS or person anchor by raw time, so the measured skew (§8.2) decides a
  plate that ends within that skew of the reading.
- During a plate, the spool shows the anchor before the plate minus the
  estimate, because AMS readings taken while printing are no anchor (rule 2).
- The pull reads the newest 5,000 usage rows (#7002; Bambuddy's listing has no
  offset or date). More than 5,000 rows between two pulls are not all booked. They
  are inside the next BB reading's value. Prod wrote 471 rows in 60 days.
- A plate end that no report showed (the next plate began, the printer went
  `IDLE`) is booked at the settle's clock time, an upper bound.

`native_spool_uses` (`20261101T1849Z_native_spool_uses.sql`) held a LAN print's
sliced grams. Since #6261 the native store writes only the ledger, and the
migration `20261114T1302Z` copied the old rows: an applied use as `print_ended`
/ `print_actual` with `data.basis = "sliced"` (a LAN print has no archive
actual, so the sliced grams are the best actual known), a settled but unapplied
use as `print_ended` with delta 0 (`basis = "not_applied"`), and an unsettled
use as `print_started` / `print_estimate`, which the run's end then replaces.
#6262 removed the old tables' readers and writers, and #6348 dropped the tables
(`20261118T1235Z_drop_native_spool_tables.sql`).

### 5.6 Locations

BB has a flat list (`/inventory/locations`) plus location sensors
(`/location-ha-sensors`). SB goes further:

- `location_kinds`: data, like every vocabulary (room, shelf, bin, dry box,
  printer AMS, drawer ...). A kind can be marked **dry storage** and
  **AMS** (a location that is a printer's AMS unit).
- `locations`: name, kind, parent (a tree: room → rack → shelf → bin),
  identifier (printed as a QR label), capacity in spools (optional), note,
  archived.
- Each printer's AMS units and external spool holder are locations too, made
  automatically. A loaded spool's location is its AMS slot, so "where is it" is
  always one answer.
- As built in #6955: a location has a `number` and the code `L-<n>`, and its QR label
  opens `<base>/x/L-<n>` (spec `2026-11-22-printer-location-labels-design.md`). A printer has
  its own place (`printer_root`, a kind flagged `printer`), and its AMS units and external holder
  hang under it (`filament/locations.py` `ensure_printer_on`; migrations
  `20261122T0603Z_filament_location_numbers.sql` and `20261122T0700Z_filament_printer_place.sql`).
- Sensors: humidity and temperature per location, read from Home Assistant as
  BB does (`LocationHASensor`: entity id, alert above or below, notify) and from
  the AMS reports for AMS locations. Readings are kept as history.
  As built in #6268: a sensor is a row of `filament_location_sensors` (`filament/sensors.py`),
  its readings are in `filament_sensor_readings`. SB reads Home Assistant through
  Bambuddy's `/location-ha-sensors/entities`, so SB holds no HA token. The AMS sensors
  are made from the first report that shows a number, with an AMS-unit location as the
  parent of the unit's tray locations. The humidity is the report's `humidity_raw`, the
  percent; the report's `humidity` is Bambu's 1-5 level and is never stored as a percent. An alert is shown in the API and the UI. The
  plan's Decisions 4 to 7 have the details.
  As built in #6646: with `notify` on, a sensor sends one notification when it starts
  alerting and one when it stops, never one per reading. A reading outside the band opens
  the alert at once; it closes only once the readings have stayed inside for 10 minutes,
  so a value that flaps across a limit sends one start. The reading that opens or closes
  it queues the notification, with its message, under the sensor's row lock
  (`filament_sensor_notifications`). The print worker sends it at most once through
  Bambuddy's `POST /notifications/app-message`, a pass a minute, claiming and ending the
  rows through CRUD routes, so it reaches the channels the owner set up in Bambuddy and
  the CRUD service makes no Bambuddy call for it. Only a sensor's newest queued
  notification is sent, and none after `notify` is turned off. The sensor shows its
  newest notification: sent, waiting, or failed and why. Notification rows older than 30
  days are pruned, except each sensor's newest. The plan's Decisions 29 to 50 have the
  details.
- A move writes a `moved` event (from, to) in the spool's ledger. A scan of a
  location QR, then of a spool tag or label, moves the spool.
- Time spent outside dry storage counts toward "needs drying" for hygroscopic
  materials (§5.1 flag). That rule's hours are a material setting.
  As built in #6268: the setting is `max_hours_outside_dry` on the material (empty means no
  limit). `filament/dry_hours.py` counts the hours at read from the `moved` events since the
  last `dried` event, and spool reads carry `hours_outside_dry` and `needs_drying`. The
  plan's Decision 8 has the rule.

### 5.7 History and metrics

**History.** The ledger (§5.5) is the spool's history. Two additions:
- An `edited` event records each changed field with its value before and after,
  and who changed it (person, agent session, flow run, BB sync). So any value
  on a spool can be traced to who set it and when.
- The spool page shows a timeline: events, moves, prints (with links to the
  print and the order), drying, weighings, tag changes, and a chart of grams
  left over time with each anchor reading marked.

**Metrics per spool**, computed from the ledger:
- grams used, number of prints, grams per print, grams wasted (failed and
  cancelled prints)
- cost used and value left (grams × the spool's cost per kg)
- burn rate (grams per day over a window that is a setting) and forecast empty
  date
- days since opened, days since last use, hours outside dry storage
- the error of AMS % against scale readings, for this spool and its type

**Metrics for the inventory**, on a Filament → Metrics tab:
- stock in grams, spools and value, by material, brand, type, supplier and
  location
- consumption over time, by the same dimensions
- waste rate, by material, type and printer
- price per kg over time for each type and supplier; spend by supplier, per
  month
- days of stock left per type at the current burn rate; low stock and the
  reorder date from the type's lead time (BB parity with the Forecast view and
  `sku-settings`)

The same numbers go out as OpenTelemetry metrics with the existing tracing
setup, and a Filament panel joins the Grafana dashboards (`deploy/CLAUDE.md`).
Low-cardinality labels only: material, brand, location, not the spool id.

**As built in #6269** (plan `docs/superpowers/plans/2026-11-18-filament-history-metrics.md`;
the numbers are its decisions):
- Prometheus gauges, not an OTel meter (2): the backend has no meter provider, every metric
  is a `prometheus_client` series in `core/metrics.py`, and the dashboards read Prometheus.
  The gauges come from one inventory snapshot, refreshed at most every 60 s per process.
- Labels are catalog names (4): `material`, `brand` and `location_kind` are the catalog
  rows' `name`, because those tables have no code column. A rename of a material, brand or
  location kind starts a new series. A label has at most 20 values; the rest fold into
  `other`, and null is `none`. A material that crosses rank 20 makes a one-day step in
  "Used per day" and in `other`; the totals stay right. A used spool that changes material
  (a person types it, an invoice confirm types it, or its type's material changes) moves
  its past grams to the new material, because `metrics_store.py` labels every plate by the
  spool's current material: "Used per day" shows one negative bar and one false spike for a
  day, and "Waste rate" moves if any of those plates failed. A real value named `other` or
  `none` is exported as `other*` or `none*`.
- Consumption is a cumulative gauge (5): `scadbuddy_filament_used_grams{material,outcome}`
  is the grams of every plate booked; the panels use `delta` over the range, after a
  `max by` across API pods.
- A plate is counted by §5.5 rule 3 with no anchor (6), by `metrics.plates`: BB's usage row
  wins over SB's estimate, a `covered` plate is skipped (its grams are on another
  spool, so it is not a print either), and an open estimate counts 0 g until the print ends.
- Waste is the grams of failed and cancelled plates (7); an SB-only failed plate books 0 g.
- Burn rate (8) is the grams of plates that ended inside the window
  (`filament_burn_window_days`, default 30) over the window's days, or over the spool's age
  when it is younger, never under 1 day. A type's or material's burn is that of the union
  of its spools' plates, born at the oldest spool.
- A type edit fans out (11): a change to an overridable field, a preset or a K value writes
  one `edited` event on each unarchived spool that does not override it.
- The chart shows what SB believed then (13): each event stores `grams_before`; the value
  after an event is the next event's `grams_before`. Over 500 points thin out. The manual,
  scale and BB anchors, the plates and the last point stay; AMS anchors thin with the rest
  (`metrics.py` `series`, the `keep` line). An anchor mark uses the spool's current weight
  lock, not the lock at the reading's time, because the ledger does not record it.
- The AMS error (14) pairs each scale reading with the newest earlier AMS reading (not
  while printing, within 24 h, no plate that took grams between).

### 5.8 Purchasing

- `suppliers`: name, website, customer number, note. BB parity.
- `invoices`: supplier, number, date, currency, subtotal, shipping, tax, total,
  file (blob pointer), status (`draft`, `confirmed`), import run id.
- `invoice_lines`: invoice, supplier product, spool type, description,
  quantity, unit price, line tax, tax basis, colour code, line total,
  allocated cost (line plus its tax, plus its share of shipping; §9).
- Shopping list: BB parity with `/inventory/shopping-list`.

**As built (#6266).** The plan `docs/superpowers/plans/2026-11-17-filament-suppliers.md`
holds each decision and what it costs if wrong.

- **Price point basis.** A price point is the price of one pack, tax included, shipping
  excluded, in the invoice's currency. `price_per_kg` is derived on read from the pack size
  and the type's label weight, so a label-weight edit leaves no stale number. A price point
  never rewrites a type's `cost_per_kg`; "Use as default cost" is a person's click.
- **Quantity counts spools.** A line's `quantity` is the number of spools; `pack_size` only
  sizes the price point. A line may fill spools that exist (`update_spool_ids`).
- **Tax and shipping.** All lines carry `line_tax`, or none do and the invoice's `tax` is
  spread by line subtotal (basis `receipt-wide <rate>%`); a mix is a 422, and tax on lines
  that cost nothing is a 422. Shipping is spread by line total. Sums round by largest
  remainder at four places, so the lines add up to the invoice. Confirm needs the invoice's
  currency to be the shop's and the difference within 0.05.
- **Frozen confirmed invoices.** A confirmed invoice keeps only its note and file; its lines
  and header refuse edits and it cannot be deleted. A second confirm returns it and makes
  nothing.
- **Shopping list statuses.** An item is `pending` or `purchased`; a repeat keeps the first
  `purchased_at`. The list shows newest first, pending above purchased.
- **An empty spool adds its type (#6868).** A spool whose grams move from above 0 to 0, by
  any ledger write, or that is archived, adds a `pending` item for its type in the same
  transaction, unless one is already pending for that type; a spool with no type adds a text
  item. The item names the spool (`empty_spool_id`) and its note says when. The stored
  setting `filament_shopping_auto_add_empty` (Settings → Filament, on by default) turns it
  off. It orders nothing, so "stock reorder automation" above stays out of scope. The
  rulings are in `docs/superpowers/plans/2026-11-17-filament-suppliers.md`.
- **Draft bounds and archived suppliers.** A line's quantity is at most 1,000; a line may
  not fill more spools than its quantity, and its total must fit `numeric(12,4)`; each is a
  422. A draft for an archived supplier is a 422, and confirming an invoice whose supplier
  became archived is a 409.

## 6. Capture from events

- **Tray changes.** SB receives the LAN MQTT reports, and the CRUD service
  writes each one (`printers/reports.py` `advance_in`). A change of tray
  contents writes `loaded` or `unloaded` in that write, and an `ams_reading`
  with the remain % for an RFID tray (a tray without RFID keys gives no
  reading). The `SpoolRefresh` schedule runs a capture backstop that reconciles
  the stored reports against the ledger for what the write missed. Capture never
  fails a report write: a failure is logged and the write goes on.
- **Prints.** Only SB's native LAN runs write `print_started` (estimate, when
  the printer takes the plate). Their `print_ended` (actual) is booked in the
  write of the report that shows the plate's timed end (`FINISH` or `FAILED`,
  at that report's time); an end with no time (the next plate began, the printer
  went `IDLE`) is left to the follow's settle. A print sent through BB's queue
  gets no `print_started` and no estimate (owner's ruling, 2026-11-15): its
  grams come only from BB's usage rows. The scheduled BB pull books BB's usage
  rows (`GET /inventory/usage`), per spool, as `print_ended` events with
  `ref_kind = bb_usage` and the matched BB archive id (§8.2). It does not book the
  print watcher's archive import. An archive holds one total for the print, not
  grams per spool, and the watcher imports it while the print still runs, before
  any actual exists. A usage row is what BB deducted from each spool. When SB and
  BB both record one plate, BB's grams count (§5.5 rule 3). The booking runs
  inside the pull, so it obeys #6262's `filament_sync.spools` setting. The per-type
  sync settings are #6265's.
  The reasons are in `docs/superpowers/plans/2026-11-15-filament-capture.md`,
  "Decisions this plan makes", with the measurements of 2026-11-15 in §8.2.
- **Scale.** BB's SpoolBuddy posts scale readings to BB
  (`/spoolbuddy/scale/reading`). With BB→SB sync of weights on, SB reads
  `last_scale_weight` and `last_weighed_at` changes and writes `weighed`.
  As built in #6268: the setting is `filament_sync.weights` (`off` or `bb_to_sb`,
  on by default; #6265 widened it to the four values of §8). With `off`, the pull books no `bb_scale` weighing and no `bb` weight
  reading for a spool SB already tracks. A spool SB has never seen keeps its first `bb`
  reading, because it needs a start weight. A `bb_dried` event is not a weight and is
  still booked. The plan's Decision 3 has the reason.
- **Backstop.** The existing `SpoolRefresh` schedule stays as a poll that
  catches what the events missed.
- **AMS weight sync.** A button and an agent tool, "Sync weights from AMS".
  It writes `ams_reading` events for every loaded, unlocked spool. This is the
  SB equivalent of BB's `POST /inventory/sync-ams-weights`.
  As built in #6268: `POST /filament/spools/sync-ams-weights` (`capture.sync_ams_weights`),
  the Spools tab button "Sync weights from AMS" and the agent tool
  `filament_sync_ams_weights`. It reads the stored LAN reports, not BB. It books one
  `ams_reading` (`data.forced`) for the spool that holds each tray now, and skips a spool
  with a weight lock and a printer that prints. It answers the counts `booked`, `locked`,
  `printing`, `no_remain` and `stale`. Unlike BB, it does not release a weight lock.
  The plan's Decisions 1 and 2 have the reasons.

## 7. RFID

- **Bambu tag on another brand's spool (override binding).** SB binds the tag's
  `tray_uuid` to the spool with the override flag. When the tray loads:
  1. SB configures the slot with the spool type's preset and K
     (BB `POST /printers/{id}/slots/{ams}/{tray}/configure`, or the LAN path).
  2. The spool's weight lock is on, so the AMS remain % from the original
     Bambu spool never changes grams left.
  3. With BB sync on, SB writes the binding to BB (`link-tag`). A 409
     `tag_already_linked` names the BB spool that holds the tag; SB moves it.
  4. `auto_add_unknown_rfid` is one BB-wide setting, so SB turns it off for
     every tag while the system setting "RFID override mode" is on, whether or
     not an override binding exists yet. SB records the value it found. When a
     person turns override mode off, SB restores that value.
- **NTAG tags.** SB reads scans and writes tags through BB's SpoolBuddy
  endpoints (`nfc/tag-scanned` as seen through BB, `nfc/write-tag`,
  `nfc/write-result`). The tag carries OpenTag3D data of the linked Bambuddy spool, not
  OpenSpool data with the SB spool id: Bambuddy's write path encodes OpenTag3D v1 and v2
  from its own spool and has no field for another id
  ([`spoolbuddy.py` lines 826-957](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/spoolbuddy.py#L826-L957),
  v1.3.1). SB identifies the tag by its UID through its binding (kind `ntag_opentag`, §5.4).
  A direct reader in SB is out of scope until the BB path proves too limited.
  The flow has four calls. A scan listens on BB's `WS /api/v1/ws` for the frames
  `spoolbuddy_tag_matched` and `spoolbuddy_unknown_tag`
  ([`spoolbuddy.py` lines 667-690 and 788-797](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/spoolbuddy.py#L667-L690),
  v1.3.1); the socket takes a `?token=` from `POST /auth/ws-token` only while BB's auth is on
  ([`websocket.py` lines 101-135](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/websocket.py#L101-L135),
  v1.3.1). A write queues `nfc/write-tag` and binds nothing; confirm reads the BB spool and binds its
  new `tag_uid` when `data_origin` is `opentag3d`; cancel posts `devices/{id}/cancel-write`.
- **Labels.** Print labels with the spool number and a QR code of the spool id.
  BB parity with `/inventory/labels`.

  As built in #6264:
  - Override rules. `override` is valid only for a Bambu tag (kind `bambu_mifare`, a tray UUID),
    checked against the stored kind (422 otherwise). Binding with it locks the spool's weight, and
    unbinding never unlocks it; a move ("Move it here") sends `override: false`. Every binding
    change queues the tag in `filament_bb_tag_pending`; the push to Bambuddy (`link-tag`, a 409
    `tag_already_linked` moves it) runs only when `pushes(tags)` (`sb_to_bb` or `two_way`, #6265), SB first and
    Bambuddy second, comparing state before each write. Override mode is one row
    (`filament_rfid_state`), set by `PUT /filament/rfid/override-mode`; `GET /filament/rfid/status`
    reports it, with `blocked` when Bambuddy's auth refuses the setting write. The spool refresh
    Schedule runs the step `filament_rfid_reconcile` (mode, tag push, slot configure; each fails
    alone), and `POST /filament/rfid/reconcile` runs it once.
  - The scan and write calls: `POST /filament/rfid/scan` (a bounded listen, `timeout_s` 1 to 50),
    `POST /filament/spools/{id}/ntag/write`, `.../ntag/confirm` and `.../ntag/cancel`.
  - Labels: 50 x 25 mm, a QR code of the text `scadbuddy:spool:<spool id>` (no host), with a quiet
    zone of 4 modules, and the spool number, material, colour and brand. The page is
    `/filament/labels`; the layout (roll or sheet) is kept in `localStorage`.
  - As built in #6955: spool labels keep that payload. Place labels, and labels for a printer, its
    AMS units and slots, carry the URL `<base>/x/L-<number>` (at most 84 bytes, QR version 5 or
    less, so a 19.8 mm square QR keeps modules of 0.5 mm). `<base>` is the setting
    `filament_label_base_url`, else `public_url`, each reduced to an https origin
    (`filament/labels.py` `label_base_url`), else the page's own origin. A phone camera opens the
    URL; `/x/:code` resolves it (`backend/scadbuddy/codes/`). Modes `?locations=` and `?printers=`
    of the labels page print them.
  - As built in #7157 (plan `docs/superpowers/plans/2026-11-23-label-templates-pdf.md`): the
    size is a label template, the 50 x 25 mm roll above by default, or one of BB's templates or
    a custom size, kept in the setting `filament_label_template`. `POST /filament/labels` gives
    the same labels as a PDF.

## 8. Sync with BB

On only when BB integration is on. One system-level setting per data type:
`off`, `sb_to_bb`, `bb_to_sb`, `two_way`. Data types: types and instances,
weights, tags, suppliers and prices, locations, presets and K.

### 8.1 Mapping

| SB | BB |
|---|---|
| Spool type | No BB entity. Type fields are copied onto each BB spool. `material_number` carries the type code. A `filament-catalog` entry and an `sku-settings` row follow the type. |
| Insert type | Spool catalog entry (`core_weight_catalog_id`, `core_weight`) |
| Spool instance | One BB spool, 1:1, through `bb_spool_links` (SB spool, BB spool id, last pushed hash, last pulled `updated_at`) |
| Type presets, K values | Per-spool `filament-presets` and `k-profiles` on each instance |
| Supplier, supplier product, price | BB supplier; spool `suppliers` (`supplier_article_number`, `quoted_price_per_kg`, `is_purchase_source`) |
| Location | BB location |
| Tag binding | `tag_uid`, `tray_uuid` on the BB spool (`link-tag`) |
| Ledger | `weight_used`, `weight_locked`, `last_dried_at`, `last_scale_weight` |
| Invoice | SB only. BB receives the spool's `cost_per_kg`. |

A BB spool with no link is matched to an SB type in this order: by
`material_number`, then by brand, material, subtype, colour and label weight,
else SB proposes a new type for a person to confirm.

### 8.2 Engine

- A Temporal workflow per BB instance. SB events signal it, so a push leaves
  within seconds of the event. A pull runs on a schedule (BB has no webhooks
  for spools in the measured spec).
- Each push is idempotent: it compares the fields it owns with Bambuddy's current
  values; the hash of the last push only lets an event-driven pass skip an item (as
  built in #6265, Decision 7).
- Two-way conflicts: the newer change wins, per field. The owner reports no
  conflicts in practice; this rule is enough. BB has one `updated_at` per spool,
  so "newer" compares SB's event time with that value.
- **Consumption is counted once.** BB deducts `weight_used` itself when its
  print ends. SB records that print as `print_actual` with the BB archive id. A
  pulled BB weight change that matches a print SB already recorded (same
  archive, or the same SB run) is not written again. Any other pulled change is
  a `bb` absolute reading. When SB pushes weights, it pushes `weight_used` as
  label weight minus SB's grams left, so BB converges on SB's value.
  As built in #6263: the ledger counts a plate once by pairing a job's SB and
  BB records (§5.5 rule 3), not by matching an archive id. A pulled reading sits
  at the BB row's `updated_at`, and BB's usage row of the same write at its
  `created_at`, the same instant, with the delta first (§5.5 rule 1). SB's own
  plate pairs with BB's usage row by job. Each anchor is compared in its own
  clock: a BB anchor against BB's time, any other anchor against SB's time. So a
  plate inside a BB reading's value is not taken off again, and the skew
  between the clocks cannot count a plate twice (`filament/ledger.py`,
  `filament/bambuddy_pull.py`, `filament/bambuddy_usage.py`). #6261 ordered a
  native plate's `print_ended` at its plate's start; #6263 replaced that.

  Measured on 2026-11-15 (#6263), read-only, from the WSL host, against
  `https://bambuddy.internal.nullreference.io` (BB) and
  `https://scadbuddy.internal.nullreference.io` (SB), with `GET` requests only:
  - From the probe of about 05:50 UTC (`GET /api/v1/inventory/usage?limit=60`,
    `GET /api/v1/archives/?limit=40`, `GET /api/v1/inventory/spools`), reused
    and not measured again. A usage row's `created_at` equals its spool's
    `updated_at` to the microsecond (usage 457 and spool 31; usage 465 and
    spool 103), so BB deducts in the write that moves `updated_at`. BB keeps one
    usage row per spool per print, SB's own LAN prints included, and their
    `print_name` is SB's `subtask_name` (`scadbuddy-ac13ac3c-p1`). A failed
    print has usage rows with partial grams (usage 460: 23.4 g, `failed`).
  - The archive window, at about 06:22 UTC. `GET /api/v1/inventory/usage?limit=500`
    gave 435 rows; `GET /api/v1/archives/?limit=200` gave 200 archives, the
    oldest `completed_at` 2026-09-27. For each of the 406 usage rows not older
    than that archive, the script took the archive of the same `printer_id`
    whose `completed_at` was nearest, inside `[created_at − 60 s, created_at + 60 s]`.
    379 rows matched (138 archives); no row had a second archive in the window.
    `completed_at − created_at`: min 0.085 s, median 0.698 s, max 17.390 s,
    n 379; none was negative. By the row's status: `completed` n 274, median
    0.49 s, max 9.18 s; `cancelled` n 60, max 10.72 s; `failed` n 47, max
    17.39 s. All rows of one print share one `created_at`. 27 rows (12 prints:
    15 `failed`, 7 `cancelled`, 5 `completed`) matched no archive: for 19 the
    200 archives hold no archive of that name; for 5 the archive of that name
    was printed again, so its `completed_at` is a later run's; for 3 the
    nearest archive was more than 60 s away. Both times are BB's clock, so the
    skew below does not enter the window. The window to match an archive is
    therefore `[created_at, created_at + 17.4 s]` at the least; no archive
    ended before its usage rows.
  - Ruling (controller, 2026-11-15, from the measurement above): SB matches an
    archive to a usage row inside `[created_at, created_at + 30 s]`, the widest
    offset seen (17.4 s) plus a margin, and none before `created_at`. A row with
    no archive in the window keeps a null archive id; its grams are unaffected.
  - The clock skew, at about 06:30 UTC. 120 alternating requests, 0.25 s
    apart: `GET /api/v1/system/storage-usage` on BB and `GET /healthz` on SB,
    both answered by `uvicorn` (the `Server` header). Each request recorded
    the `Date` header and its send and receive times on the host's
    `time.monotonic()`, not the wall clock. One SB answer that took 36 s was
    dropped (n 119). Both servers drifted against the host's clock by the same
    −0.073 s/s, so the WSL clock is the one that moves. A least-squares fit of
    `Date − mid(send, receive)` with one common slope and one intercept per
    server gives BB − SB = +0.011 s (residual sd 0.52 s). Each BB sample
    against the mean of its two SB neighbours gives a mean of +0.019 s
    (n 58, sd 0.62 s, standard error 0.08 s). `Date` has 1 s steps, and
    uvicorn refreshes it about once a second (`Server.on_tick`, every 10
    ticks of 0.1 s, in [uvicorn 0.53.0 `server.py`](https://github.com/Kludex/uvicorn/blob/0.53.0/uvicorn/server.py),
    the version SB's `uv.lock` pins; BB's uvicorn version was not read), so one sample says little; the
    expected bias is the same on both servers and cancels in the difference.
    So the BB and SB clocks agree to within about 0.25 s. A second run of 120
    stopped at a 60 s read timeout and is not used.
- The sync runs in a worker and calls CRUD routes for SB data, with a service
  credential (§3).

**As built in #6265** (plan `docs/superpowers/plans/2026-11-18-filament-bb-sync.md`,
"Decisions this plan makes" and "Decisions made while building"; the decision numbers
below are the plan's):

- Worker (Decisions 1, 2, 18). The push is the workflow `FilamentBbSync` and its pass
  activity, on the `bambuddy` queue of the print worker (`scadbuddy-print`). SB's side
  goes through the API's CRUD area `filament-sync`; the worker holds no database secret
  (`backend/scadbuddy/workflows/filament_sync.py`, `filament/bb_sync_run.py`). A new
  workflow type on that queue needs a `Recreate` roll of `scadbuddy-print` (README, "A
  release that needs `Recreate`"). A Bambuddy key with auth on needs
  `can_manage_inventory`.
- Settings (Decisions 3, 4). `filament_sync` holds six data types (`spools`, `weights`,
  `tags`, `suppliers`, `locations`, `presets`), each `off`, `sb_to_bb`, `bb_to_sb` or
  `two_way`. `pulls(d)` is `bb_to_sb` or `two_way`; `pushes(d)` is `sb_to_bb` or `two_way`
  (`core/settings.py` `FilamentSync`). `spools`, `weights`, `tags` and `presets` default to
  `bb_to_sb`, `suppliers` and `locations` to `off`. A migration rewrote #6264's stored
  `tags` once: `off` became `bb_to_sb`, `sb_to_bb` became `two_way`
  (`migrations/20261118T1907Z_filament_sync_tags_values.sql`). A row with `spools` `off`
  is the exception: before #6265 it skipped the whole pull, so it still pulls nothing.
  Its `weights`, `presets` and `tags` became `off`, except a `tags` `sb_to_bb`, which
  stayed `sb_to_bb` (the push it was).
- Pull (Decision 5, then #6772, Decisions 172-186). BB to SB is still `SpoolRefresh`'s pull
  (`filament/bambuddy_pull.py`), gated per data type, and now also takes archive state,
  K values, suppliers and locations. Since #6772 its Bambuddy reads run on the print
  worker: the spool refresh's activity, and the import route, start `FilamentBbPull` on
  the `bambuddy` queue and wait for it. Its activity (`filament/bb_pull_run.py`) reads
  Bambuddy and sends the rows to the CRUD area `filament-pull`
  (`…/print/filament/pull`, `…/pull/usage`, `…/pull/ha-states`, `…/pull/trays`, and since
  #6907 `…/pull/colours`), which
  applies them (`filament/bb_pull_apply.py`). The CRUD service makes no Bambuddy call for
  the pull. The worker never names the actor the events are booked to: a person's import
  is kept by the API under the run's id (`filament_pull_actors`), else `bambuddy`.
  `SpoolRefresh`'s commands did not change; the push (`FilamentBbSync`) stays a
  workflow of its own.
- Tags (Decision 6). The tag queue and drain of #6264 (`filament_bb_tag_pending`,
  `rfid_push.drain_tags`) are kept and gated by `pushes(tags)`. The engine never sends
  `tag_uid` or `tray_uuid`; a new link queues the spool's open tags.
- Plan (Decisions 7, 8). The plan is state-based: each link keeps `pushed_fields` and the
  hash only lets an event-driven pass skip an item. A full pass (the timer, or "Sync
  now") compares with Bambuddy's current rows. Under `two_way` a field is pushed when SB's
  value differs and Bambuddy still holds what SB last saw there (`bb_seen`), or SB's change
  time (`bb_sync_plan.sb_changed_at`) is strictly after BB's `updated_at`; ties go to
  Bambuddy. Under `sb_to_bb` a differing field is always pushed. The pull uses the same
  `sb_changed_at`.
- Echo (Decision 9). SB's own write is stored as `bb_seen` and booked as one
  `synced_from_bb` event with `pushed: true` and no changes, so the pull does not read it
  as a Bambuddy change.
- Creates (Decision 10). A create of a spool, supplier, location or catalog entry first
  stores an intent in `filament_bb_creates`. A later plan adopts a matching Bambuddy row
  made after the intent started, in place of a second create; the pull skips a BB spool
  an intent can claim.
- Bounds (Decision 11). A pass makes at most 4 plan rounds and 200 writes, and the plan
  route returns at most `200 - written` writes. With work left, the next pass starts at
  once.
- Instance (Decision 12). The workflow id carries a hash of the normalised Bambuddy URL.
  `filament_bb_sync_state.instance` records the instance the links belong to. A pass for
  another instance plans nothing and reports `instance_changed`, and the sync stays
  stopped until the links are reset.
- Reset of the links (#6773, Decisions 165-170). `POST /filament/sync/reset-links` takes
  `confirm_instance`, which must be the recorded instance (409 `confirm_mismatch`), and
  answers 409 `same_instance` when the links belong to the configured Bambuddy and
  `nothing_to_reset` when no instance is recorded. In one transaction it deletes the spool
  links, the supplier, location, insert type and spool type links (their Bambuddy ids,
  hashes and seen copies), the create intents, the error rows, `filament_bb_ignored` and the
  recorded instance, answers the counts, then nudges a full pass. No SB spool, type,
  supplier or location is deleted. `GET` on the same path is its dry run. The status reports
  `instance_changed`; Settings → Filament then offers "Reset Bambuddy links" with a confirm
  dialog, and the agent has `filament_sync_reset_links` (outward). A plan or results call
  for another instance than the configured Bambuddy's does nothing. After a reset that
  deletes, a spool is linked again only by its RFID tag (the pull, step 2): Decision 10's
  adoption needs an intent, which the reset deletes, so an untagged spool is created again
  on the side its direction sends it to.
- Reset to the same Bambuddy (#6845, Decisions 193-199). Bambuddy has no instance id, so a
  new URL to the same Bambuddy is another instance. The dry run reads the configured
  Bambuddy's spools; when it holds every linked BB spool id, none older than its link's
  `bb_updated_at`, at least one exactly that, and at least half the links timed on both
  sides (`same_bambuddy`), the reset with `expect_kept: true` reads them again and, while
  that still holds, deletes nothing, records the configured instance and answers
  `kept: true`, so no spool is made twice. When it no longer holds, or the read fails, it
  is 409 `proof_changed` and deletes nothing. A dry run that cannot read Bambuddy answers
  `bambuddy_unreadable: true` and offers the delete; the delete (`expect_kept: false`)
  reads no Bambuddy. The pull (the Schedule's and the import) does nothing while the links
  belong to another instance.
- Lifecycle (Decision 13). A spool is created in Bambuddy only when it is unarchived and
  `pushes(spools)`. Archive and restore are pushed. A 404 on a linked spool, or on the
  probe of one a complete listing lacks, deletes the link; a partial listing never does.
  A linked supplier, location, insert type or filament entry a complete listing lacks is
  probed too; its 404 deletes the link, and the next round creates the row again under
  its direction (#6688, plan Decisions 187-192 and 200-205).
- Fields (Decisions 14, 15). The fields each data type carries are in Decision 14. Not
  pushed: SKU settings (#6270 owns them) and `last_scale_weight`. Supplier links and
  prices go to Bambuddy only, and are never pulled.
- Child types (Decision 16). Weights, presets, suppliers per spool and a spool's location
  act only on spools already linked.
- Pull widened (Decisions 138-152). The pull reads archived spools and takes BB's archive
  state, K values, suppliers and locations. It seeds a link's seen archive state (`bb_seen.archived_at`) on its first pull, and its
  seen location (`bb_seen.location_id`) on the first pull where BB's place is null or maps
  to an open SB location in no printer's place, with BB's value and moves nothing on that
  pull, so a link seen before #6265 is not archived or moved on the first pull (Decisions
  150, 152, 164 and 171). A seen location of a BB place SB has never mapped (in any
  state) counts as absent and is seeded again; one SB mapped once stays seen. It never
  moves a spool out of a printer place.
- SB writes that do not nudge (Decision 17). The API nudges the workflow after a write
  on `api/filament.py`, `api/filament_purchasing.py` and `api/tray_spools.py`, and after
  a settings save that changes `filament_sync`. Capture and `sync_ams_weights`
  (`filament/capture.py`), the Bambuddy import and the RFID scan (Decision 163) and the
  print worker's CRUD `filament` writes
  (`…/print-uses/settle`, `bb-to-sb`) do not. They reach Bambuddy on the timer only,
  against "SB events signal it" above.
- Status (Decision 19). `GET /filament/sync/status` and `POST /filament/sync/run` show and
  start a pass; the state is in `filament_bb_sync_state` and `filament_bb_push_errors`.

## 9. Invoice import flow

The owner's existing skill is the source for this flow:
`.claude/skills/filament-intake/SKILL.md` (moved from eh-homelab/clusters in
#6222), with `scripts/intake.py` and `scripts/3dfp.mjs`. It does this work against BB today.
SB makes it native. Its rules, as SB rules:

- **Diff before create.** Before it proposes any spool, the session compares
  each line with the existing inventory. A spool with no cost that matches a
  line (often one an RFID tag auto-created) is that purchase: the line updates
  it. A spool that has a cost but no invoice line (most spools the Bambuddy
  import made) may be this purchase too: the session offers it with its old and
  new cost per kg, and the line updates it only when the person says yes; the
  confirm replaces its cost (#6700). A spool on another invoice line is never
  filled. A qty-N line can be one update plus N-1 new
  spools. When it is ambiguous, the session asks the person and never guesses.
- **Cost per spool** = (line subtotal + that line's tax) ÷ quantity. Use the
  tax per line when the invoice prints it. When it prints only a total tax, the
  rate is total tax ÷ subtotal, applied per line, and the line records the tax
  basis (for example "receipt-wide 7.0%"). Shipping, if any, is spread by line
  value (our call; the skill has no rule for it). A bundle that splits into N
  spools is quantity N.
- **No SKU on the invoice:** the product name is the supplier product's SKU.
  Never borrow a SKU from another invoice.
- **Bambu lines:** the 5-digit colour code on the invoice (for example
  "Misty Blue (30108)") gives the exact RGBA from Bambu Studio's
  `filaments_color_codes.json`. Exact RGBA matters: tag linking needs an exact
  material and RGBA match, and translucents carry alpha `80`. A multi-colour
  silk's subtype follows the SKU: `A05-M*` is Dual Color, `A05-T*` is Tri
  Color.
- **Other brands:** the type gets its 3dfilamentprofiles.com (3DFP) id, and
  the 3DFP hex is its RGBA. The flow adds the filament to the 3DFP account,
  exports the profiles for every nozzle of each printer model (H2C: 0.2, 0.4,
  0.6, 0.8), imports them as local presets, and sets them as the type's presets
  for each nozzle.
- **Every spool gets a cost and a source.** Each spool links to its invoice
  line, which replaces the note the skill uses to show where a cost came from.
  A purchase with no invoice keeps the nearest known price and says so.

**3DFP needs a real browser.** The site sits behind a Vercel bot check: curl and
WebFetch get 429, and headless Chromium with its default user agent gets 403
(since 2026-11-01, per the skill). Export works only when logged in, one spool
at a time. So SB runs it in a **profile-fetch worker**: Chromium with a normal
Chrome user agent, a login from a secret (1Password item "3D Filament
Profiles"; the account email is passed separately). By the database rule (§3),
this worker holds no database secret. It returns the profile files as blobs to
the flow, and the flow imports them through CRUD routes.

Steps:

1. On Filament → Invoices, the person uploads or pastes a PDF or image. SB stores
   it in the blob store and creates a draft invoice.
2. SB starts an assistant session with an import prompt and the blob pointer.
   The session runs as a flow (spec `2026-10-01-durable-printing-agents-flows-design.md`
   §7), so a stopped import resumes.
3. The session reads the invoice: supplier, number, date, totals, and lines
   with tax and colour codes.
4. For each line it applies the diff rule above, then matches a supplier
   product and spool type, or proposes a new type with its colour (Bambu code
   table or 3DFP).
5. The session writes the draft through CRUD routes: the invoice, its lines,
   each line's proposed type and supplier product, and each line's updates of
   existing spools. The draft is visible on Filament → Invoices, where the
   person corrects it. The person can also correct it in the chat, and the
   session edits the same draft.
6. The person confirms with the button on the Invoices page, or in the chat
   through an agent tool that asks for approval. One CRUD call creates the
   invoice, the lines, the spools, the updates and the price points in one
   transaction, keyed by the import run id so a retry creates nothing twice.
7. For new non-Bambu types, the flow fetches the 3DFP profiles and sets the
   presets for each nozzle. K values come from the printers' K tables (BB
   `/printers/{id}/kprofiles/`) where one matches.
8. Check: every new type has a preset for each nozzle of each printer model, and
   no spool from the invoice is missing a cost.

A new plugin skill, `plugins/scadbuddy/skills/filament/SKILL.md`, covers import,
types, instances, tags and the ledger. It takes over the rules of
`.claude/skills/filament-intake`, which then points to it. The `print` skill says
"Bambuddy owns printers, spools, presets ..."; that changes to say SB owns
spools.

**As built in #6267** (plan `docs/superpowers/plans/2026-11-18-filament-invoice-import.md`,
Decisions 1-7). The profile-fetch worker (step 7) is
#6625, not part of #6267: until it lands, the session proposes a non-Bambu type
with its 3DFP id and hex and says the presets are still to fetch. The import is
an assistant session, not a flow (step 2): every session already runs as a
Temporal workflow, so a stopped import resumes. The invoice file reaches the
model in the session's first message, as an image or a PDF document block (step
2), and `POST /api/v1/ai/filament/invoice-import` starts it and sets the draft's
`import_run_id` to the session id. The colour table is read from the slicer
profiles by `GET /api/v1/filament/bambu-colour-codes`, and the diff rule has a
read, `GET /api/v1/filament/invoices/{id}/candidates`. The skill is
`plugins/scadbuddy/skills/filament/SKILL.md`.

**As built in #6625** (plan `docs/superpowers/plans/2026-11-18-profile-fetch-worker.md`,
Decisions 1, 9, 11, 12 and "Decisions made while building"). Step 7 is not a flow: a
Temporal workflow `ProfileFetch` runs on its own queue, `profile-fetch`, and a worker in its
own pod does the browser part only. The API imports the files into Bambuddy's local presets
(`POST /local-presets/import`) and sets the type's presets, because the worker holds no
database secret and no Bambuddy key. The nozzles are the remembered setting
`profile_fetch_nozzles`. The printer models are the models of SB's printers, and the 3DFP
printer option is the model string. Invoice confirm starts a fetch for each type it made that
has a `tdfp_id`; a route, two agent tools and a button start or read one by hand. Step 8's
check is the read `GET /filament/types/{id}/profile-fetch` (`gaps`: the printer models and
nozzles with no preset now). The browser's bot-check facts are the skill's, not measured
again by #6625 (the tests use a replica of the site).

## 10. UI

New top-level nav item **Filament** (`/filament`), between Library and Orders
(`frontend/src/components/AppShell.tsx:92-99`). Tabs:

- **Spools**: table, cards and forecast views; filters (active, archived, used,
  new, low stock, material, brand, supplier, insert type, location); columns
  picker; group by; bulk edit, archive, delete; CSV import and export; labels;
  summary tiles (total, consumed, by material, in printer, low stock).
- **Spool edit**: one page, not a modal, in sections:
  - Identity: number, type (pick or create), tags with bind and re-bind.
  - Weight: label, empty (insert type), measured, grams left with source, lock.
  - Colour and cost: overrides of the type, cost per kg, invoice link.
  - Printers: presets and K per printer and nozzle, from the type with
    overrides.
  - Storage: location, last dried.
  - History: the timeline and the grams-left chart (§5.7), and the spool's
    metrics.
- **Types**: the product list and editor, with suppliers, URLs and price history.
- **Tags**: every tag, its kind, current binding and history.
- **Suppliers**: list and editor, with their products.
- **Invoices**: list, upload or paste, draft review, confirmed invoices with
  their spools.
- **Locations**: the location tree, each location's spools, sensors and
  humidity history, QR labels.
- **Catalog**: materials, subtypes, brands, categories, effects, insert types,
  insert materials, location kinds, colours. Every list is editable (§5.1).
- **Metrics**: §5.7.
- **Shopping list**.
- **Ledger**: all events, filterable.

Settings loses "Spool inventory" (`pages/settings/sections/inventory.tsx`),
"ScadBuddy's spools" (`native-spools.tsx`) and the price book editor
(`components/orders/FilamentsEditor.tsx`). Settings gains a **Filament**
configuration section: the sync setting per data type, RFID override mode,
weight-sync policy, low-stock threshold, currency, default cost per kg.

The print dialog's `FilamentPicker` reads SB spools.

## 11. Parity list (BB 1.2.6b1)

Every item needs an SB equivalent:

- Spool fields: material, subtype, colour name, rgba, extra colours, effect,
  brand, label weight, core weight and catalog id, weight used and baseline,
  slicer filament and name, nozzle temperatures, note, tag uid, tray uuid, data
  origin, tag type, cost per kg, weight lock, last scale weight and time,
  category, low-stock %, material number, storage location, location id, added
  full, last used, last dried (time, temperature, hours), encode time,
  archived.
- Per spool: K-profiles, filament presets, suppliers, usage history, consumed
  counter reset.
- Operations: create, bulk create, copy, create from slot, edit, bulk edit,
  archive, restore, delete (each also in bulk; our call: delete is allowed only
  for a spool with no print events, else the spool is archived, so job cost and
  order actuals keep their source), CSV import and export, labels,
  assign and unassign to a tray, link tag, find by tag.
- Catalogs: spool catalog, colour catalog with filamentcolors sync, filament
  catalog with cost calculation, SKU settings, shopping list, supplier stats,
  material number stats.
- AMS: assignments, configure slot, slot presets, AMS labels, refresh and reset
  slot, load and unload, AMS history, sync weights from AMS.
- K-profiles on the printer: list, set, batch set, delete, notes.
- SpoolBuddy: NFC scan and write, scale reading and tare (through BB).
- Settings: default filament cost, currency, `auto_add_unknown_rfid`,
  low-stock threshold, drying presets.

Gap recorded by #6264, for #6270: BB's label templates (custom label sizes) and its
`/inventory/labels` PDF output were not matched. Matched by #7157 (§7): templates, a custom
size and `POST /filament/labels`.

## 12. Phases

Epic: [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260). Each sub-issue ships as its own PRs. "After" lists the issues
that block it (GitHub's blocked-by links hold the same graph).

| Phase | Issue | Builds | After |
|---|---|---|---|
| 1 | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261) | Data model: catalogs, types, instances, RGBA, tags, ledger, locations (§5.1-5.6); migration of `native_spools`, `physical_spools` and `native_spool_uses`; one-time BB import and scheduled BB→SB pull (§8.1); the price book linked to types: one `filaments` row per type identity, alpha included, named by `filament_spool_types.filament_id`, the `filaments` ids kept | - |
| 1 | [#6262](https://github.com/eh-homelab/ScadBuddy/issues/6262) | Filament section, spool edit page, Settings clean-up (§10); the print dialog, orders and agent tools read SB spools, and the provider overlay is removed | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261) |
| 2 | [#6263](https://github.com/eh-homelab/ScadBuddy/issues/6263) | Capture from tray changes and prints (§6) | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261) |
| 3 | [#6264](https://github.com/eh-homelab/ScadBuddy/issues/6264) | RFID: tags, bindings, overrides, NTAG through SpoolBuddy (§7) | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261) |
| 4 | [#6265](https://github.com/eh-homelab/ScadBuddy/issues/6265) | Sync engine: SB→BB push and two-way (§8) | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261), [#6264](https://github.com/eh-homelab/ScadBuddy/issues/6264) |
| 5 | [#6266](https://github.com/eh-homelab/ScadBuddy/issues/6266) | Suppliers, supplier products, price sheets, invoices (§5.8) | [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261) |
| 5 | [#6267](https://github.com/eh-homelab/ScadBuddy/issues/6267) | Invoice import flow, profile-fetch worker, `filament` plugin skill (§9) | [#6266](https://github.com/eh-homelab/ScadBuddy/issues/6266) |
| 6 | [#6268](https://github.com/eh-homelab/ScadBuddy/issues/6268) | AMS weight sync, scale input, location sensors (§6, §5.6) | [#6263](https://github.com/eh-homelab/ScadBuddy/issues/6263) |
| 6 | [#6269](https://github.com/eh-homelab/ScadBuddy/issues/6269) | History timeline, metrics, Grafana panel (§5.7) | [#6263](https://github.com/eh-homelab/ScadBuddy/issues/6263) |
| 6 | [#6270](https://github.com/eh-homelab/ScadBuddy/issues/6270) | Parity sweep against §11 | all of the above |

As built in #6264: `filament_sync.tags` took `off` (the default) and `sb_to_bb`. As built in #6265: every data type takes the four values of §8 (§8.2, "As built in #6265"), and #6264's stored `tags` values were rewritten once (`off` to `bb_to_sb`, `sb_to_bb` to `two_way`).

[#6209](https://github.com/eh-homelab/ScadBuddy/issues/6209) (the price book keeps alpha, colour stops and effect) landed in
[#6211](https://github.com/eh-homelab/ScadBuddy/pull/6211) before [#6261](https://github.com/eh-homelab/ScadBuddy/issues/6261); #6261's types write the book's alpha, stops and effect from it.

## 13. Left to the builder

- Table and column names, and overrides as columns or jsonb.
- Whether the price book becomes a view or is migrated into types.
- How the spool edit page lays out its sections at phone width.
- The exact Temporal shape of the sync (one workflow per BB instance is the
  proposal).
- Agent tools for each new route, named like the existing
  `native_spool_*` tools.
