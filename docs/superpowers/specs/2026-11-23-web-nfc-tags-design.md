# Spool tags through Web NFC (#7273)

Issue #7273, epic #6260. The owner made the decisions on 2026-11-23. They are recorded on #7273.

## Intent

The owner wants to read and write spool tags now, from an Android phone, with no SpoolBuddy and no Bambuddy in the path. ScadBuddy is moving away from Bambuddy, which stays a sync target only (#7276). The owner's words: "I prefer web for now... I want a single code path in terms of inputs." ScadBuddy agents must also be able to read and write any tag, for debugging and for work outside spools.

## Goals

- G1. A phone page in ScadBuddy reads a tag (its UID and its NDEF records), says which spool holds it, and offers the next step: open the spool, bind the tag to a spool, or start the unknown-spool flow (epic #7171).
- G2. The same page writes a spool's tag: OpenSpool (default) or OpenTag3D NDEF records. Then it reads the tag back to check the write, and binds the tag to the spool.
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
- The page reaches ScadBuddy at its own LAN host (memory: UI URLs come from the Host header). It calls the API as the signed-in user. There is no device credential and no device state.
- An agent's tag write is outward. The person's arm-and-tap on the page is its approval, so no pending action is needed. An agent's read needs no approval, because the tap is the person's own act.
- Only the CRUD service touches the database (repo CLAUDE.md, "Database access"). The agent reaches jobs through CRUD routes.

## Approaches

1. **Web NFC page** (picked by the owner). There is one input path, nothing to install, and it ships soonest. Its limits are the non-goals above.
2. A native Android app. It can read Bambu tags and send raw commands, but it is a second client to build and to sideload. The owner declined it for now.
3. Web NFC now and an app later. That means two code paths, and the owner wants one.

## Design

- **`TagRead`** (`filament/tags.py`) holds: `source` (`phone`, `agent`, `ams`), `uid`, `tray_uuid` (AMS only), `tag_type`, `records` (NDEF: record type, media type and the data as hex), the decoded spool fields (OpenSpool JSON, OpenTag3D), and the printer, AMS and tray for an AMS reading. `read(store, TagRead, actor)` finds the holder by tag (`FilamentInventory.holder_on`'s tag half), records `tag_seen` data, and returns `{tag, spool | null, decoded, next}`. The AMS capture path (`filament/capture.py`) uses the same lookup through this function, so it has no copy of its own.
- **Encoders** live in `filament/tag_formats.py`. They turn a spool into OpenSpool or OpenTag3D records, and decode both formats. Their sources are https://github.com/spuder/OpenSpool and https://github.com/queengooborg/OpenTag3D. Each is tested against a published sample.
- **Routes** (`/api/v1/filament/tags/…`):
  - `POST reads` takes one phone or agent reading.
  - `GET /spools/{id}/tag-records?format=` returns the records to write.
  - `POST /spools/{id}/tag-written` takes the read-back after a write. It checks that the read-back records equal the records sent, then binds the tag.
  - `nfc-jobs` handles create, list pending, claim, result and get.
- **Jobs** live in a new `filament_nfc_jobs` table: `id`, `kind` (`read` or `write`), `records` (for a write), `created_by`, `state` (`pending`, `armed`, `done`, `failed` or `expired`), `result`, `created_at`, and `expires_at` (10 minutes). An open phone page polls for pending jobs every 2 s, and only while the page is visible.
- **Page** `/filament/scan`. It has one big "Scan" button that starts `NDEFReader.scan()` after the person taps it. After a reading it shows the spool card or the bind and unknown-spool choices, the two photo buttons (G6) and a weight field. The screens are in the UX canvas https://claude.ai/artifact/Wj6S6C8Cuvi3sRsMgjeBEV. It also has a "Write tag" action on a spool, and a job banner ("Agent wants to write 2 records. Arm, then hold a tag."). Where Web NFC is missing, it says "Open this page in Chrome on Android" with the page's own URL. Load frontend-design.
- **Agent tools** `nfc_read(timeout_s)` and `nfc_write(records, timeout_s)` create a job and poll its result. They return the result, or `expired`.
- **Retire** the SpoolBuddy path:
  - `filament/ntag.py`, and the `/spools/{id}/ntag/*` and `/rfid/scan` routes, with their tools and UI;
  - the Bambuddy client's tag-scan and tag-write calls;
  - `filament_ntag_writes`, dropped by a new migration.

## Left to the builder

- How the AMS capture calls `read` without slowing its once-a-second path. It may skip a tag whose holder has not changed.
- The exact OpenTag3D field mapping, from its spec.
- How each removal lands, in which tasks and in what order, with each task's tests.
