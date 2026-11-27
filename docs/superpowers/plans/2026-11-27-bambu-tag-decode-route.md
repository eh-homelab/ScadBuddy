# Bambu tag reads: decode in Go, tag identity, scan history (#8148)

Part of #7905. Spec: `docs/superpowers/specs/2026-11-26-bambu-tag-scan-design.md`, sections
"Tag identity (G3)", "Scan history (G4)", "Server-side decode (G1)" and "As built: the route and
the split (#8148)". The split decision is in that last section and on #8148.

## Tasks

1. Migration `20261127T0409Z_filament_tag_uids_reads.sql`: `filament_tag_uids` (filled from
   `filament_tags.tag_uid`, kept by a trigger) and the append-only `filament_tag_reads` (dump key,
   decoded JSON, photo, position). No named rows: prod holds no tag in two UID forms
   (`filament_tag_list`, read 2026-11-27).
2. Python finds a tag by any UID: `filament/tags.py` `_find` and `holder_by_keys`,
   `filament/spools.py` `_tag` (which adds a UID it finds by tray), `filament/inventory.py`
   `_Keys` and `_no_tag_clash`, `filament/bambuddy_pull.py`. Tests
   `tests/filament/test_tag_uids.py`.
3. `TagReadIn` takes `mifare_blocks`, `image` and `location`; Python answers a MIFARE read 503
   `mifare_needs_core`. `TagReadResult` documents `bambu`, `tag_read_id` and `dump_key`, which
   core sets.
4. `internal/blobstore` `Blobs.Put`: a blob stored by a route of core, with the commit's checks.
5. `internal/tagreads`, area `filament-tag-reads`: the MIFARE read (check, decode, dump, identity,
   fold, history, Python's UID-only read, the answer's extra fields and draft), the NDEF read's
   history row, the photo and the position, and `RecordMifare` for #8157. Tests on real Postgres
   with a fake S3 and a fake Python, and the owner's two-tag dump as the fixture.
6. The Scan page posts the app's blocks (`lib/nativeMifare.ts` `takeMifare`) and shows the
   `bambu` fields as text (`components/filament/scan/BambuFields.tsx`).

## After merge

Prod needs `filament-tag-reads` in `SCADBUDDY_CORE_AREAS` (eh-homelab/clusters). Until then a
MIFARE read is 503 `mifare_needs_core`, and no read writes a history row.
