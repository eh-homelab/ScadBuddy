# Filament management in ScadBuddy

Date: 2026-11-14. Status: draft for the owner's approval.

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
  (`spool_overlay_refresh_seconds`, `core/settings.py:662`).
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
- `colours`: manufacturer, name, RGBA, material, extra colours, effect. BB
  parity with `/inventory/colors`, including its sync from filamentcolors.

**Colour is RGBA everywhere (owner, 2026-11-14: transparent and translucent
filaments).** Every colour SB stores is `#RRGGBBAA`: types, instances, extra
colour stops, the colour catalog, the price book, tray fingerprints. Alpha `FF`
is opaque. Bambu writes translucent filaments with alpha `80`, and BB's tag
linking needs the exact RGBA, alpha included (the `filament-intake` skill's
traps table). A `#RRGGBB` value from an old row or a person reads as alpha
`FF`.

Today SB drops the alpha in several places; each changes:
- `native_spools.colour` is `^#[0-9A-Fa-f]{6}$` (`spools/native.py:57`).
- The tray fingerprint is `material|RRGGBB` (`spools/models.py:59-62`), so a
  clear and an opaque spool of one hue look alike.
- The price book keeps `#RRGGBB` (`orders/bambuddy.py:53-61`); open issue
  #6209 is the same gap and folds into this work.

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
- Locations: §5.6.
- Thresholds (low stock, humidity, drying presets) are settings with
  per-material and per-type overrides, never constants.

### 5.2 Spool type (a product)

`spool_types`:
- `id`, `code` (short, unique; it goes to BB as `material_number`)
- brand, material, subtype, category and effect (references to the §5.1
  catalogs), colour name, `rgba`, extra colours
- diameter, density, nozzle and bed temperature ranges
- label weight (g), `insert_type_id`
- category, low-stock threshold %, default cost per kg
- archived

Children:
- `spool_type_presets`: printer model, nozzle diameter, slicer filament id and
  name. BB parity with `SpoolFilamentPreset`.
- `spool_type_k_values`: printer, extruder, nozzle diameter, nozzle type, K,
  name, `cali_idx`, `setting_id`. BB parity with `SpoolKProfile`.
- `supplier_products`: supplier, SKU or article number, product URL, pack size
  (spools per order line), note.
- `price_points`: supplier product, price, currency, price per kg, date, source
  (`invoice` with the line id, or `manual`).
- `sku_settings`: lead time days, safety margin. BB parity with
  `FilamentSkuSettings`.

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

`native_spools` and `physical_spools` exist today. `physical_spools` already
gives an identity keyed by RFID
(`20261018T1744Z_physical_spools.sql`). The migration (our call):
- Each `physical_spools` row with at least one provider record becomes a
  `spools` row with the same id. Its fields come from the native record if one
  exists, else from the BB record.
- A `native_spools` row with no physical spool becomes a `spools` row with the
  native id.
- A physical spool that was only seen in a tray (no record) is not migrated as
  a spool. Its keys become a `tags` row with no binding.
- `tray_uuid` and `tag_uid` become `tags` rows and open bindings.
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
- The UI shows the anchor and the deltas that make the current value. `native_spool_uses` (`20261101T1849Z_native_spool_uses.sql`)
becomes `print_actual` events.

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
- Sensors: humidity and temperature per location, read from Home Assistant as
  BB does (`LocationHASensor`: entity id, alert above or below, notify) and from
  the AMS reports for AMS locations. Readings are kept as history.
- A move writes a `moved` event (from, to) in the spool's ledger. A scan of a
  location QR, then of a spool tag or label, moves the spool.
- Time spent outside dry storage counts toward "needs drying" for hygroscopic
  materials (§5.1 flag). That rule's hours are a material setting.

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

### 5.8 Purchasing

- `suppliers`: name, website, customer number, note. BB parity.
- `invoices`: supplier, number, date, currency, subtotal, shipping, tax, total,
  file (blob pointer), status (`draft`, `confirmed`), import run id.
- `invoice_lines`: invoice, supplier product, spool type, description,
  quantity, unit price, line tax, tax basis, colour code, line total,
  allocated cost (line plus its tax, plus its share of shipping; §9).
- Shopping list: BB parity with `/inventory/shopping-list`.

## 6. Capture from events

- **Tray changes.** SB already receives the LAN MQTT reports
  (`spools/seen.py`, `ReportTrays`). A change of tray contents writes
  `loaded` or `unloaded` at once, and an `ams_reading` with the remain %.
- **Prints.** SB's own runs write `print_started` (estimate) and `print_ended`
  (actual). For prints SB did not start, the print watcher's BB archive import
  writes `print_ended` with the archive's actual grams.
- **Scale.** BB's SpoolBuddy posts scale readings to BB
  (`/spoolbuddy/scale/reading`). With BB→SB sync of weights on, SB reads
  `last_scale_weight` and `last_weighed_at` changes and writes `weighed`.
- **Backstop.** The existing `SpoolRefresh` schedule stays as a poll that
  catches what the events missed.
- **AMS weight sync.** A button and an agent tool, "Sync weights from AMS".
  It writes `ams_reading` events for every loaded, unlocked spool. This is the
  SB equivalent of BB's `POST /inventory/sync-ams-weights`.

## 7. RFID

- **Bambu tag on another brand's spool (override binding).** SB binds the tag's
  `tray_uuid` to the spool with the override flag. When the tray loads:
  1. SB configures the slot with the spool type's preset and K
     (BB `POST /printers/{id}/slots/{ams}/{tray}/configure`, or the LAN path).
  2. The spool's weight lock is on, so the AMS remain % from the original
     Bambu spool never changes grams left.
  3. With BB sync on, SB writes the binding to BB (`link-tag`). A 409
     `tag_already_linked` names the BB spool that holds the tag; SB moves it.
  4. SB turns off BB's `auto_add_unknown_rfid` while override mode is on, so BB
     does not create its own spool for the tag.
- **NTAG tags.** SB reads scans and writes tags through BB's SpoolBuddy
  endpoints (`nfc/tag-scanned` as seen through BB, `nfc/write-tag`,
  `nfc/write-result`). The tag carries OpenSpool data with the SB spool id.
  A direct reader in SB is out of scope until the BB path proves too limited.
- **Labels.** Print labels with the spool number and a QR code of the spool id.
  BB parity with `/inventory/labels`.

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
- Each push is idempotent: it compares the hash of the fields it owns with the
  last pushed hash.
- Two-way conflicts: the newer change wins, per field. The owner reports no
  conflicts in practice; this rule is enough. BB has one `updated_at` per spool,
  so "newer" compares SB's event time with that value.
- **Consumption is counted once.** BB deducts `weight_used` itself when its
  print ends. SB records that print as `print_actual` with the BB archive id. A
  pulled BB weight change that matches a print SB already recorded (same
  archive, or the same SB run) is not written again. Any other pulled change is
  a `bb` absolute reading. When SB pushes weights, it pushes `weight_used` as
  label weight minus SB's grams left, so BB converges on SB's value.
- The sync runs in a worker and calls CRUD routes for SB data, with a service
  credential (§3).

## 9. Invoice import flow

The owner's existing skill is the source for this flow:
`.claude/skills/filament-intake/SKILL.md` (moved from eh-homelab/clusters in
#6222), with `scripts/intake.py` and `scripts/3dfp.mjs`. It does this work against BB today.
SB makes it native. Its rules, as SB rules:

- **Diff before create.** Before it proposes any spool, the session compares
  each line with the existing inventory. A spool with no cost that matches a
  line (often one an RFID tag auto-created) is that purchase: the line updates
  it. A spool that already has a cost belongs to an earlier purchase, so a
  matching line is a new spool. A qty-N line can be one update plus N-1 new
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

## 12. Phases

Each phase ships as its own PRs under one epic issue.

1. Data model (§5.1-5.6, without purchasing), editable catalogs, Filament
   section, spool edit page,
   migration of `native_spools` and `physical_spools`, Settings clean-up.
   Also in phase 1: a one-time **BB import** and a scheduled **BB→SB pull**
   (§8.1 matching), so SB's spools are complete and current from the first day.
   The print dialog, orders and agent tools switch to SB spools in the same
   phase; the provider overlay is removed. The price book editor moves to
   Filament → Types: cost per kg is on the type, and the price book view keeps
   the existing `filaments` ids so quotes and job cost keep working.
2. Capture from events (§6).
3. RFID: tags, bindings, overrides, NTAG through SpoolBuddy (§7).
4. Sync engine: SB→BB push and two-way (§8).
5. Purchasing and the invoice import flow (§5.8, §9), the `filament` skill.
6. AMS weight sync and scale input in the UI; location sensors; metrics and
   the Grafana panel (§5.7); parity sweep against §11.

## 13. Left to the builder

- Table and column names, and overrides as columns or jsonb.
- Whether the price book becomes a view or is migrated into types.
- How the spool edit page lays out its sections at phone width.
- The exact Temporal shape of the sync (one workflow per BB instance is the
  proposal).
- Agent tools for each new route, named like the existing
  `native_spool_*` tools.
