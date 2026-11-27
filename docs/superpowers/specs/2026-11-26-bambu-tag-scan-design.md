# Bambu tags from the phone, and scans in the assistant (#7905)

Issue #7905, epic #7171. This design changes two non-goals of
`2026-11-23-web-nfc-tags-design.md` (#7273): reading the data on Bambu tags is now a goal, and
ScadBuddy gets an app.

## Intent

During an invoice import, one line can hide which spools were bought. A Bambu "mystery box"
(`MB-FL-…`) is one line with no colour codes. The owner wants to identify those spools by
tapping each spool's tag with a phone, not by loading each one into an AMS slot, and to hand
what a scan read to the assistant (issue #7905).

The owner's decisions (2026-11-26):

- "We need to support scanning bambu tags". The closed-source Android app Spool Scanner
  (`com.sveas.spoolscanner`) reads them on the owner's phone ("Spoolscanner app works fine").
- "We will need a scadbuddy app, should be xplat eventually, Android first. I'm thinking .net?"
- "I'd prefer server side decoding."
- "Every tag scan should be recorded in the tag history."

## Measured and sourced facts

- Bambu spool tags are MIFARE Classic 1K. Each sector's key A is derived from the UID:
  HKDF-SHA256, input the UID bytes, salt `9A 75 9C F2 C4 F7 CA FF 22 2C B9 76 9B 41 BC 96`,
  info `RFID-A\0`, 16 keys of 6 bytes, one per sector
  ([deriveKeys.py](https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/main/deriveKeys.py)).
- Block layout ([BambuLabRfid.md](https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/main/BambuLabRfid.md)),
  numbers little-endian, every 4th block a key trailer: block 1 holds the variant id (bytes
  0-7, `A50-K0`) and the material id (bytes 8-15, `GFA50`, the AMS's `tray_info_idx`); block 2
  the filament type; block 4 the detailed type (`PLA Basic`); block 5 the colour RGBA (bytes
  0-3), the spool weight in grams (bytes 4-5) and the diameter (float, byte 8); block 6 drying
  and nozzle temperatures; block 9 the tray UID (the AMS's `tray_uuid`); block 12 the
  production date as ASCII `<year>_<month>_<day>_<hour>_<minute>`; block 16 a second colour.
  Sectors 10-15 hold an RSA signature.
- Web NFC reads a Bambu tag's UID but none of its blocks. Measured on prod on 2026-11-25:
  the owner's phone scan of a Bambu spool made tag `a167982e…` with `tag_uid` `7E7482BB`,
  kind `other`, no records (`filament_tag_list`, read 2026-11-26). Web NFC cannot authenticate
  a MIFARE Classic sector (#7273 Non-goals; https://w3c.github.io/web-nfc/).
- **The phone's UID and the AMS's `tag_uid` differ in form.** The same physical tag is
  `becfa1bf…`, `tag_uid` `7E7482BB00000100`, tray `D3B5D4BC…`, from the AMS. Every one of the
  38 `bambu_mifare` rows on prod ends `00000100`: the AMS reports the 4-byte UID and then
  `00000100`. `read` compares strings, so the phone's read made a second tag row instead of
  finding the first (read 2026-11-26). Android's own NFC API gives the same 4-byte form: the
  owner's Spool Scanner export (2026-11-26, 24 Bambu spools scanned 2026-11-23 and 24) holds
  UIDs such as `6EC39097`, and 5 of them are prod AMS tags as `<uid>00000100` (spools 43, 36,
  81, 42 and 10). With `7E7482BB`, 6 of 6 match the rule. The other 19 are on no SB tag (a
  second side, or a spool no AMS has seen). Spool Scanner keeps `alternateUids` per spool,
  the same idea as G3.
- A Bambu spool carries two tags, one on each side, with different UIDs and the same tray UID
  in block 9. `filament_tags` allows one row per `tray_uuid` and one `tag_uid` per row
  (`20261114T1301Z_filament_spools.sql`), so a spool's second side has no row to match.
- Android reads MIFARE Classic through `android.nfc.tech.MifareClassic`, only on phones whose
  NFC controller supports it ([gamersi/rfid-bbl-scanner-poc](https://github.com/gamersi/rfid-bbl-scanner-poc):
  "Many Samsung devices support it; Pixels typically do not"). Spool Scanner 1.4 is a .NET
  MAUI app that uses that API (`android/nfc/tech/MifareClassic` in its
  `libaot-SpoolScan.dll.so`; XAPK from APKPure, sha256 `a639dcd6…63ba1`, inspected statically
  on 2026-11-26). Its source is not public.
- The LAN host `scadbuddy.internal.nullreference.io` runs with auth mode `off`: every caller
  there is the anonymous browser principal (`docs/security/trust-boundaries.md`). No sign-in
  page is involved, so a WebView on that host needs no identity provider.

## Goals

- G1. The phone reads a Bambu tag (its UID and every block it can authenticate) and ScadBuddy
  decodes it **on the server**: material, detailed type, `fila_id` and variant, colour,
  weight, diameter, temperatures, production date and tray UID. It finds the spool that
  holds the tag, or offers a spool draft from the decoded fields, as an OpenSpool read does.
- G2. **One input path** (#7273 G3 stays): a Bambu read is one more `TagRead` through
  `POST /api/v1/filament/tags/reads` (and an NFC job's result) into `filament/tags.py`
  `read`. Nothing else decodes or binds.
- G3. **A tag is found by any of its UIDs.** A phone UID matches the AMS's form, and a tag
  can hold several UIDs (a spool's two sides). A native read adds a side's UID to the tag
  its block 9 names. A UID-only read (Web NFC) can be linked to a tag by hand ("same spool
  as…").
- G4. **Every scan is in the tag's history.** Each phone or agent read, and each time an AMS
  first sees a tag in a tray, is a row: when, source, who, the UID, what was read (raw blocks
  or NDEF records), the decoded fields and the printer place for an AMS read. The tag page
  lists them, and the scan page lists the latest scans.
- G5. **The ScadBuddy app**, .NET MAUI, Android first, cross-platform later. It reads and
  writes tags natively and posts through the same routes as the page.
- G6. The assistant's composer gets **Scan a tag**. It queues a read job (the `nfc-jobs`
  routes `nfc_read` uses), the person taps the tag, and the job's result becomes a scan on
  the message. A message can carry several (one per spool in a box). The agent gets them as
  untrusted data, the same as tool output.
- G7. Agent `nfc_read` gets Bambu reads with no change to its code: the job's result is the
  read's answer. Its description stops saying "Chrome on Android" and "NDEF" only.

## Non-goals

- Writing or cloning a Bambu tag. The signature makes a written tag useless to a Bambu
  printer.
- Checking the RSA signature. Bambu's public key is not in the sources above.
- iOS in the first release. Core NFC's support for MIFARE Classic is not checked yet; check it
  before the iOS work is planned.
- The AMS-slot source of Scan a tag (#7905 lists it). ScadBuddy has no native route that reads
  a tray from the LAN report, and new routes go in Go (#7667). A follow-up issue tracks it.
- Showing the scans again in the transcript after the message is sent. A follow-up issue
  tracks it.
- App store publishing. The APK is sideloaded.

## Anti-goals

- A second input path, or a decoder outside the server.
- A native bridge that any page can call.
- A scan whose contents the agent treats as instructions.
- A second tag row for one physical tag.

## Constraints

- New backend routes go in Go (#7667). This design adds no route path: it adds fields to the
  existing read bodies and answers, and two tables. As built (#8148, "As built: the route and
  the split" below), core's area `filament-tag-reads` answers the read path in Go in front of
  Python.
- Only the API holds SQL (root CLAUDE.md). The app reaches ScadBuddy as the page does, on the
  LAN host.
- Bytes travel as hex in JSON only where NDEF records already do (`data_hex`): at most 64
  blocks of 16 bytes. No images in JSON (#2479).
- CI runs on `ubuntu-latest` only. A MAUI Android build runs there with the .NET SDK and the
  `maui-android` workload.

## Approaches

1. **A MAUI app whose screens are the web page** (picked by the owner, 2026-11-26: "Web for
   now"). A MAUI app hosts `/scan` in a plain `WebView` and
   gives it a native NFC service through a bridge. One UI; iOS later is a second NFC service,
   not new screens.
2. **A MAUI app with native screens.** A real app look, offline use. Two UIs to keep in step
   with the web page.
3. A USB reader on the desktop through WebUSB. No app, but hardware, desk only, and OS
   drivers often hold the device. Rejected: the owner wants an app.

## Design

### Tag identity (G3), first

- A new table `filament_tag_uids (uid text PRIMARY KEY, tag_id uuid NOT NULL REFERENCES
  filament_tags ON DELETE CASCADE, source text, added_at, added_by)`. Every UID of a tag is a
  row; `filament_tags.tag_uid` stays as the tag's first UID so that reads and Bambuddy sync
  keep working.
- `clean_tag_uid` gives one form for a MIFARE UID: a 4-byte UID (8 hex digits) and the AMS's
  16-digit form ending `00000100` are the same key. The lookup (`_find`, `holder_by_keys`,
  `FilamentSpoolStore.tag_on`) goes through `filament_tag_uids`.
- A native read with block 9 adds its UID to the tag of that tray UID, or makes the tag.
- "Same spool as…" on a tag page links a UID-only tag to another tag: its UIDs and its history
  move to the other tag, and the empty row goes. Both write a history row.
- A migration fills `filament_tag_uids` from `filament_tags.tag_uid` and merges the prod rows
  that are one tag in two forms (today `a167982e…` into `becfa1bf…`), guarded by id and value
  as backend/CLAUDE.md asks.
- This part ships first, on its own (the owner, 2026-11-26: "Ahead of the rest"): it fixes
  today's duplicate rows from web scans.

### Scan history (G4)

- A new append-only table `filament_tag_reads`: `id`, `tag_id`, `at`, `source` (`phone`,
  `app`, `agent`, `ams`), `actor`, `uid`, `tray_uuid`, `records` (jsonb, NDEF), `blocks`
  (jsonb, MIFARE, key trailers never stored), `decoded` (jsonb), `printer_id`, `ams_id`,
  `tray_id`, `job_id`. Rows are never updated; a tag merge moves them. As built (#8148):
  `dump_key` (the `tag_dump` blob) stands for `blocks`, and `image_key`, `image_media_type`
  and `location_*` keep the scan's photo and position.
- The phone's reads write a row; the AMS capture writes one when a tag appears in a tray
  (`capture.py`'s `loaded`), not on every report. As built (#8148), core writes the phone's
  rows; the agent's NFC jobs and the AMS write none yet.
- Routes: `GET /filament/tags/{id}/reads` (paged, newest first) and `GET /filament/tag-reads`
  (the latest scans across tags, for the scan page). Agent tool `filament_tag_reads` (`read`).

### Server-side decode (G1)

The owner changed this part on 2026-11-26 ("prefer go code", "The tag data raw should be
stored in the blob store", "implement 100% of the rfid tag guide"; decisions on #7905).

- The decode is Go: `tools/go/core/internal/bambutag` decodes every field the
  [RFID Tag Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide) documents. It gives
  each undocumented byte range as hex and the RSA signature (blocks 40-63) as hex, unverified.
  It accepts a partial read; a field whose block is missing is absent, never zero. Its JSON
  carries `decoder_version` and `complete`.
- Every MIFARE read is stored raw through the blob API (#7961, kind `tag_dump`): the data
  blocks and the UID, never the key trailers. A partial read is stored too, with its missing
  sectors marked. Each row of `filament_tag_reads` points at its dump, and `blocks` there is
  replaced by that pointer. The tag's `raw_blob` is written only from a complete read.
- `decoded` holds the decoder's JSON with its version, so a later decoder can decode the
  stored dumps again without a re-scan.
- The route is Go, behind the shared person auth (#8053). The Python notes below give the
  behaviour it keeps.
- `TagReadIn` and `NfcJobResultIn` get `tag_type` and `mifare_blocks: [{index, data_hex}]`
  (64 at most, 16 bytes each). As built (#8148): `TagReadIn` only; the NFC jobs come later.
- A tag is untrusted: strings and numbers are bounded before use, as `tag_formats.py` does.
- The read (as built, core's, not `read_on`) decodes a Bambu read, takes block 9 as the read's `tray_uuid`, and gives
  `decoded` and `draft` as for an NDEF read: the material, brand and subtype found in the
  catalog by `fila_id` and type name, the colour from block 5 (and its Bambu colour code,
  `ColourCodes`), the label weight from block 5. The answer gets `bambu`, the decoded fields.
  The tag's kind is `bambu_mifare`.
- Tested against the published dumps.

### As built: the route and the split (#8148)

The decision of #8148 (2026-11-27). The route keeps its path, `POST /api/v1/filament/tags/reads`
(G2), and its answer is a superset of `TagReadResult`. Core's area `filament-tag-reads`
(`tools/go/core/internal/tagreads`) stands in front of Python on that path. Each table has one
writer:

- Core writes the tag identity and the history: `filament_tags` (a new Bambu tag, its tray UUID,
  kind `bambu_mifare`, `raw_blob` from a complete read), `filament_tag_uids`,
  `filament_tag_reads`, the `blob_refs` holds of the dump and the photo, and the fold of a UID-only
  tag into its tray's tag ("Same spool as…": its UIDs, bindings, reads, the ledger's copies of its
  id and its Bambuddy queue row move, then the row goes; 409 `tags_both_bound` when both tags hold
  an open binding, 409 `tag_has_backups` when the UID-only tag has backups, 409 `tag_keys_clash`
  when the UID names a tag of another tray).
- Python keeps the rest of a read: the last-seen mark, the bind (`bind_spool_id`), the answer's
  `tag`, `spool`, `resolved` and `next`. Core hands it each MIFARE read as a UID-only read (no
  blocks, no records), and each NDEF read as it is. Core writes no spool, binding or ledger event.
- A MIFARE read (`mifare_blocks`): core checks and decodes it, stores the dump (the UID and the
  data blocks, JSON, format `scadbuddy.mifare_classic_1k.v1`, kind `tag_dump`), records it, then
  asks Python, and adds `bambu` (the decoder's JSON), `tag_read_id`, `dump_key` and, when no spool
  holds the tag, a `draft` (material by the filament type, subtype by the rest of the detailed
  type, brand Bambu Lab, colour, weight, diameter, nozzle and drying values). The Bambu colour
  code is not in the draft yet. A key trailer in the body is dropped, never stored.
- An NDEF read: Python answers first; core then writes its history row (records, source
  `phone`) on the tag Python names and adds `tag_read_id`. A read with `bind_spool_id` is the page
  binding a tag it just read, so it writes no second row.
- A read with `tap: false` is the page's lookup of a tag it read before (after a move, a
  restore's confirm). It writes no history row and no hold, and a MIFARE one sets no `raw_blob`.
- Core's own refusals of a MIFARE read come before it records anything: 422 `mifare_invalid`
  (the body's shape, a 7-byte UID say) and 422 `mifare_unreadable` (the decode). On those, and on
  503 `mifare_needs_core`, the page posts the read again as a UID-only read.
- Every read may carry `image` (`{key, media_type}`, a committed `scan_image` blob, 422
  `image_not_found` otherwise) and `location` (`{lat, lon, accuracy_m, at}`, `at` within a day of
  now), the owner's decisions of 2026-11-27. Core keeps them on the history row; Python checks
  their shape only.
- `tagreads.Reads.RecordMifare(ctx, body, actor, source)` records a MIFARE read from another Go
  route (#8157, a scan job's result) with the same body shape, without Python's part.
- With the area off, Python answers a MIFARE read 503 `mifare_needs_core` and writes no history.
- Both sides find a UID through `filament_tag_uids`. A trigger keeps each tag's `tag_uid` there.

Left for later PRs: history rows for the agent's NFC jobs and the AMS, the read history routes
and the agent tool, the hex view, and the Bambu colour code in the draft.

### The app (G5)

- .NET MAUI, `app/` in this repo, Android first, min SDK 29.
- It opens the LAN host's `/scan` page (the URL set once in the app). Navigation stays on that
  origin; another origin opens in the browser.
- The bridge is offered only to that origin. As built (`app/ScadBuddy/Resources/Raw/nfc-polyfill.js`),
  it installs Web NFC's `NDEFReader` (`scan()`, `write()`), backed by Android's NFC, so the page
  runs unchanged; `window.ScadBuddyNfc` is `{native, lastMifare, close}`:
  - A tap answers `reading` as Chrome does, except that a 4-byte MIFARE Classic UID's
    `serialNumber` is the AMS's 8-byte form (`…:00:00:01:00`); the server canonicalises both. For a MIFARE Classic tag, the app derives the 16
    keys, authenticates each sector with key A, and reads its blocks; a sector that refuses is
    left out, and key trailers are never sent. Those blocks go to `window.ScadBuddyNfc.lastMifare`
    and a `scadbuddy-mifare` window event as `{uid, blocks, refusedSectors, error?}`.
  - `write()` writes NDEF records (never to a MIFARE Classic tag), and the page reads them back,
    so its backup-first write (#7273 G7) runs as it does with Web NFC.
- `lib/webNfc.ts` gets a reader seam: `nfcSupport()` answers `native` when the bridge exists,
  and `NfcSession` uses it then, Web NFC otherwise. A MIFARE read posts the raw blocks; the
  screen shows the server's answer. No decoder in the page.
- The release APK is signed with a key kept in 1Password and in the repo's Actions secrets.
  As built, `ci.yml`'s `android` job (lane `android`) builds it on `ubuntu-latest` for changes
  under `app/`; attaching the APK to a release on an `app-v*` tag is not built yet.

### The app's telemetry

The app sends traces, metrics and logs as `tools/go/bambuddy-fs` sends its own (its README,
"Telemetry"). The code is `app/ScadBuddy/Telemetry.cs`.

- The packages are `OpenTelemetry` and `OpenTelemetry.Exporter.OpenTelemetryProtocol` 1.21.0
  ([NuGet](https://www.nuget.org/packages/OpenTelemetry/1.21.0)), pinned in `ScadBuddy.csproj`.
- The export is OTLP/HTTP protobuf to `<base>/v1/traces`, `/v1/metrics` and `/v1/logs`. The
  default base is bambuddy-fs's: `https://otlp.internal.nullreference.io`
  (eh-homelab/clusters#2221). That collector takes no credentials, so the APK holds no secret.
- The base URL is the Preferences key `scadbuddy.otel.endpoint`. The menu item "Telemetry" sets
  it. An empty value turns telemetry off. Off, the app builds no provider and makes no network
  call. A change takes effect after a restart. Android gives an app no environment, so the
  `OTEL_*` variables do not apply.
- The resource is `service.name=scadbuddy-app`, `service.version` (the app's version), and
  `service.instance.id`, a random id that the app keeps for each install. No device name is sent.
- The sampler is parent-based always-on. Metrics are cumulative and export each 60 s, because
  Prometheus refuses delta.
- The app flushes all three signals, within 5 s and off the main thread, each time it leaves
  the screen (Android `OnStop`). Android gives an app no clean exit.
- Spans: `nfc.read` for each tag read, with `nfc.kind`, `nfc.techs`, `nfc.ndef.records`,
  `nfc.outcome`, and for MIFARE Classic `nfc.mifare.blocks`, `nfc.mifare.refused_sectors` and
  `nfc.mifare.resumed`. A failed read has the error status, with the failing sector and the
  exception type. The span never holds the UID, block data or Android's error message.
- Metrics: `scadbuddy_app.nfc.reads` and `scadbuddy_app.nfc.read.duration` (by `kind` and
  `outcome`: `complete`, `partial` or `error`), and `scadbuddy_app.nfc.resumed_reads`.
- Logs: `Microsoft.Extensions.Logging` goes to OTLP. The bridge logs each read on the category
  `ScadBuddy.Nfc`. Logcat keeps its `ScadBuddyNfc` lines, with the UID and the block counts, never block data.
- A scan link's result (a tag read or a photo) is the app's own HTTP POST, to the link's
  result URL (`NfcBridge.SendJob`, `MainPage.Scan`); it propagates no `traceparent`. The page in the
  WebView keeps its own browser tracing (`frontend/src/lib/tracing.ts`). The app does not
  instrument the page.

### The scan view

The owner asked for it on 2026-11-26 ("I'll be able to see raw hex view and decoded fill in
UI?"). The `/scan` page shows it after a tap, and a tag's page shows it for each stored read.

- A decoded panel: every field with its unit, colour swatches for the colours. A field from a
  missing sector says "not read".
- A raw hex view: 16 sectors by 3 data blocks, 16 bytes a row. Hovering a byte range names
  its field and highlights it in the decoded panel. Undocumented ranges say "unknown". The
  signature sectors (10-15) are folded and say "unverified". A partial read shows its
  missing sectors as gaps. A copy button copies the dump.
- The read history opens each read's own dump and fields, so two taps can be compared.
- The decoded fields come from `decoded`; the hex comes from the read's blob, through the API.

### Scan a tag (G6)

- The composer's attach menu gets "Scan a tag". It posts `POST /api/v1/filament/nfc-jobs`
  (`kind: read`), shows a chip "Tap the tag on your phone", and polls the job until it ends. A
  done job's chip shows the material, colour and spool number; a failed or expired one says
  why and can be tried again. Chips can be removed. Several are allowed.
- The `user.message` frame gets `scans: [{job_id}]` (16 at most). Before the turn starts, the
  agent reads each job through its backend client and refuses one that is not a `done` `read`
  job. With auth mode `off` every job is the anonymous principal's, so ownership is not
  checked (the LAN host is the trust boundary, as for every other route there). The agent
  adds a `<tag_scans>` block after the page context, with the sentence that marks the page
  context as data, not instructions.

## Calls the assistant made (the owner may change them)

- A Bambu read gives a spool draft, as an NDEF read does, so a mystery-box spool can be made
  from its scan.
- The app loads the LAN host, where auth is off and no sign-in page is needed. Off the LAN
  (the SSO host) is later work: identity providers often refuse sign-in inside a WebView.
- Scan jobs are not bound to a chat; any finished read job id can be named.
- An AMS read is a history row when the tag appears in a tray, not on every report.

## Left to the builder

- The exact `bambu` field names, and how a missing sector shows on the screen.
- The PR order after the identity fix: decode and history, then the app and the page seam,
  then Scan a tag, each with its tests.
