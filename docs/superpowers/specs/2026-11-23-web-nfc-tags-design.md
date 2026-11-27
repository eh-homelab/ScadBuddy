# Spool tags through Web NFC (#7273)

Issue #7273, epic #6260. The owner made the decisions on 2026-11-23. They are recorded on #7273.

## Intent

The owner wants to read and write spool tags now, from an Android phone, with no SpoolBuddy and no Bambuddy in the path. ScadBuddy integrates with Bambuddy and does not build on it (owner, 2026-11-27), so ScadBuddy does not act on hardware through Bambuddy (#7276). The owner's words: "I prefer web for now... I want a single code path in terms of inputs." ScadBuddy agents must also be able to read and write any tag, for debugging and for work outside spools.

## Goals

- G1. A phone page in ScadBuddy reads a tag (its UID and its NDEF records), says which spool holds it, and offers the next step: open the spool, bind the tag to a spool, or start the unknown-spool flow (epic #7171).
- G2. The same page writes a spool's tag. The person never picks a format (owner, 2026-11-23): ScadBuddy picks it. A tag that already holds OpenSpool or OpenTag3D records keeps that format. A blank tag, or one with other data, gets the default format from a setting (`filament_tag_format`, default OpenSpool; Settings → Printing → Filament). After the write, the page reads the tag back to check it, and then binds the tag to the spool.
- G7. Every write is backed up first (owner, 2026-11-23). One tap does read → back up → write → read back:
  1. The page reads the tag's UID and records.
  2. It posts them to ScadBuddy.
  3. ScadBuddy stores them as a new row in `filament_tag_backups`, keyed by the physical tag (the `filament_tags` row for that UID). A tag seen for the first time is created with the kind its records decode to (`ntag_openspool`, `ntag_opentag`, else `other`), and a later read or the write's read-back corrects the kind when the format changes (plan R13).
  4. Only after ScadBuddy confirms the backup does the page write.
  If the backup fails, nothing is written. If the tag gives no UID, the write is refused, because the backup could not be tied to a physical tag. Backups are append-only, with one row per write, so every earlier version of a tag is kept. The tag's page lists them and offers "Restore", which is itself a write and so is backed up first. Agent writes (G4) use the same path.
- G3. **One input path.** Every tag reading, from the phone, an agent job or an AMS tray report, becomes one `TagRead` and goes through one ScadBuddy function. That function finds the holder, binds and books the ledger event. No source has its own matching or binding.
- G4. Agent tools: `nfc_read` and `nfc_write` (any NDEF records, not only a spool's). An agent queues a job. The phone page shows the job, the person arms it and taps a tag, and the page posts the result back. The agent waits for that result.
- G6. After a scan, the page takes quick photos with the phone camera (owner, 2026-11-23): one button for the filament label and one for the spool's label. Each tap opens the camera (`<input type="file" accept="image/*" capture="environment">`). Each photo is saved as an entity attachment (`entities/attachments.py`, #6950) on the spool (`spool:<id>`), and the filament-label photo is also saved on its spool type (`spool_type:<id>`) when the spool has one. Each attachment records which label it shows. The photos are blobs with `{name, mediaType}` pointers, never base64. On a new tag, the photos wait until the spool exists or the tag is bound.
- G5. After a scan, the page takes a typed weight, as a `person` reading on the spool (the spec's existing source). There is no scale hardware in this work.

## Non-goals

- A native Android app. The owner prefers the web for now.
- Reading the data on Bambu tags (MIFARE Classic, whose keys come from the UID: https://github.com/Bambu-Research-Group/RFID-Tag-Guide). Web NFC cannot authenticate sectors. The AMS still reads Bambu tags, and its readings enter through G3.
- Raw (non-NDEF) page or sector commands. Web NFC does not offer them.
- iOS, desktop browsers and a fixed reader.
- A connected scale. A later scale would post through the same reading path.

## Anti-goals

- A second path for a source. The phone, an agent and the AMS each feed `TagRead`, and nothing else.
- Any Bambuddy or SpoolBuddy call for tags.
- A write that a person did not arm, or one that locks a tag. `makeReadOnly` is never offered.

## Constraints

- Web NFC is `NDEFReader` (https://w3c.github.io/web-nfc/, https://developer.mozilla.org/en-US/docs/Web/API/Web_NFC_API). It works only in Chrome on Android, in a secure context, in a top-level frame (not inside Bambuddy's iframe), and only after the person grants permission. `scan()` gives `serialNumber` (the UID) and the NDEF records. `write()` writes records.
- The page reaches ScadBuddy at its own LAN host (the browser builds its own links from `window.location.origin`, as `frontend/src/lib/signIn.ts` does; the place labels in `filament/labels.py` are the exception, because they leave the browser). It calls the API as the signed-in user. There is no device credential and no device state.
- An agent's tag write is outward. The person's arm-and-tap on the page is its approval, so no pending action is needed. An agent's read needs no approval, because the tap is the person's own act.
- Only the CRUD service touches the database (repo CLAUDE.md, "Database access"). The agent reaches jobs through CRUD routes.

## Approaches

1. **Web NFC page** (picked by the owner). There is one input path, nothing to install, and it ships soonest. Its limits are the non-goals above.
2. A native Android app. It can read Bambu tags and send raw commands, but it is a second client to build and to sideload. The owner declined it for now.
3. Web NFC now and an app later. That means two code paths, and the owner wants one.

## Design

- **`TagRead`** (`filament/tags.py`) holds: `source` (`phone`, `agent`, `ams`), `uid`, `tray_uuid` (AMS only), `tag_type`, `records` (NDEF: record type, media type and the data as hex), the decoded spool fields (OpenSpool JSON, OpenTag3D), and the printer, AMS and tray for an AMS reading. `read(store, TagRead, actor)` finds the holder by tag (`FilamentInventory.holder_on`'s tag half), records `tag_seen` data, and returns `{tag, spool | null, decoded, next}`. The AMS capture path (`filament/capture.py`) uses the same lookup through this function, so it has no copy of its own.
- **Encoders** live in `filament/tag_formats.py`. They turn a spool into OpenSpool or OpenTag3D records, decode both formats, and pick the format to write from the tag's current records (G2). Their sources are https://github.com/spuder/OpenSpool and https://github.com/queengooborg/OpenTag3D. Each is tested against a published sample.
- **Routes** (under `/api/v1/filament/`, as the other filament routes are; the SpoolBuddy `/spools/{id}/ntag/*` routes are gone, see "Retire"):
  - `POST /tags/reads` takes one phone or agent reading.
  - `POST /tags/backups` takes `{uid, records}` read just before a write. It stores the backup and answers `{backup_id}`. A write without a `backup_id` from this route is refused.
  - `POST /spools/{id}/tag-records` takes `{uid, records, backup_id}` (the tag as just read) and answers the records to write, in the format G2 picks.
  - `GET /tags/{tag_id}/backups` lists the backups. `POST /tags/{tag_id}/backups/{id}/restore` answers that backup's records, to write the same way.
  - `POST /spools/{id}/tag-written` takes `{uid, backup_id, sent, read_back}` after a write. It checks that `read_back` equals `sent` and that `sent` equals what `tag-records` would answer for that spool, then binds the tag. The page carries `sent`, because backup rows are append-only and no row keeps what was sent (plan R4).
  - `nfc-jobs` handles create, list pending, claim, result, cancel and get. A job is `pending`, `armed`, `done`, `failed`, `expired` or `cancelled`. The agent cancels a job it stops waiting on. Arming a write job sends the SHA-256 of the records the page showed, and the API refuses a mismatch (plan ruling R22).
- **Jobs** live in a new `filament_nfc_jobs` table: `id`, `kind` (`read` or `write`), `records` (for a write), `created_by`, `state` (`pending`, `armed`, `done`, `failed`, `expired` or `cancelled`), `result`, `created_at`, and `expires_at` (10 minutes). A write job's `result` names the `backup_id` it took, and that backup row names the job (`job_id`). An open phone page polls for pending jobs every 2 s, and only while the page is visible. A write job takes a backup first, as G7 says.
- **Backups** live in a new `filament_tag_backups` table: `id`, `tag_id` (`filament_tags`, NOT NULL), `uid`, `records` (jsonb, as read: record type, media type and data as hex), `reason` (`spool_write`, `agent_write` or `restore`), `job_id` (null unless an agent write), `actor`, and `at`. Rows are never updated or deleted, and deleting a spool or unbinding the tag keeps them.
- **Page** `/filament/scan`. It has one big "Scan" button that starts `NDEFReader.scan()` after the person taps it. After a reading it shows the spool card or the bind and unknown-spool choices, the two photo buttons (G6) and a weight field. The screens are in the UX canvas https://claude.ai/artifact/Wj6S6C8Cuvi3sRsMgjeBEV. It also has a "Write tag" action on a spool, and a job banner ("Agent wants to write 2 records. Arm, then hold a tag."). Where Web NFC is missing, it says "Open this page in Chrome on Android" with the page's own URL. Load frontend-design.
- **Agent tools** `nfc_read(timeout_s)` and `nfc_write(records, timeout_s)` create a job and poll its result until the job ends or its time is up. A job still open then, or when the call is aborted, is cancelled. They return the job's state (`done`, `failed`, `expired` or `cancelled`) and its result; a failed write's result holds what the tag holds.
- **Retire** the SpoolBuddy path:
  - `filament/ntag.py`, and the `/spools/{id}/ntag/*` and `/rfid/scan` routes, with their tools and UI;
  - the Bambuddy client's tag-scan and tag-write calls;
  - `filament_ntag_writes`, dropped by a new migration.

### As built, 2026-11-25: the hand-held Scan mode

The owner asked for "a hand-held scan mode that is always scanning" that loads "anything, even
a prefixed tag" (2026-11-25). Plan `docs/superpowers/plans/2026-11-25-scan-mode.md`:

- The page moved to the top level, `/scan`, with a nav entry "Scan"; `/filament/scan` redirects.
- `POST /tags/reads` reads a typed code (#6955) or a `scadbuddy:spool:<id>` text out of the
  records when no spool holds the tag (`filament/tag_codes.py`): a `url` record whose path ends
  `/x/<code>`, or a `text` record that is a code. It answers `next` `open_code` and `resolved`
  in the `GET /codes/{code}` shape. A bound tag still opens its spool; a code that names no row
  is a new tag. The agent's read jobs answer the same result.
- The page shows a code's card (`CodeCard`, "Place L-4" / "Spool S-142", Open) and the switch
  "Open what I scan at once" (localStorage `scadbuddy.scan.openAtOnce`): on, each read that opens
  something navigates there, and the page starts the scan by itself when it opens.

## Left to the builder

- How the AMS capture calls `read` without slowing its once-a-second path. It may skip a tag whose holder has not changed.
- The exact OpenTag3D field mapping, from its spec.
- How each removal lands, in which tasks and in what order, with each task's tests.
