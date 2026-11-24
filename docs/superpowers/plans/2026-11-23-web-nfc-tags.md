# Spool Tags Through Web NFC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **UI work (Tasks 5, 6, 9 and the UI part of Task 10):** the implementer loads the
> `frontend-design:frontend-design` skill before writing any UI.

**Goal:** A phone page at `/filament/scan` reads and writes spool tags through Web NFC. Each write is backed up first and checked after. Phone, agent and AMS readings go through one ScadBuddy function. The SpoolBuddy path through Bambuddy is removed.

**Architecture:** `filament/tags.py` turns every reading (`TagRead`) into one lookup, one "seen" record and, when asked, one bind (`seen_on`, `read_on`). `filament/tag_formats.py` encodes and decodes OpenSpool and OpenTag3D NDEF records and picks the format to write. New CRUD routes under `/api/v1/filament/tags/…`, `/spools/{id}/tag-*` and `/nfc-jobs` hold backups and jobs in two new tables. The browser does all NFC I/O through `NDEFReader`. Agents queue jobs (`nfc_read`, `nfc_write`) that the open phone page runs.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, pytest; React 19, vitest, msw, Playwright; the agent's TypeScript tools (zod, vitest).

**Spec:** `docs/superpowers/specs/2026-11-23-web-nfc-tags-design.md` (approved; binding). Issue #7273, epic #6260. UX screens: https://claude.ai/artifact/Wj6S6C8Cuvi3sRsMgjeBEV (listed in "Screens" below, because an implementer cannot open the link).

## External facts this plan relies on

- **Web NFC** (https://w3c-cg.github.io/web-nfc/, moved from https://w3c.github.io/web-nfc/; MDN https://developer.mozilla.org/en-US/docs/Web/API/Web_NFC_API). Read 2026-11-23:
  - `NDEFReader.scan()`, `write()` and `makeReadOnly()` reject with `InvalidStateError` outside "the currently active top-level browsing context". The interfaces are `[SecureContext]`. They need the "nfc" permission.
  - `NDEFReadingEvent.serialNumber` "usually consists of 4 or 7 numbers, separated by `:`", "or empty string in case none is available".
  - An `NDEFRecord` has `recordType` (`empty`, `text`, `url`, `smart-poster`, `absolute-url`, `mime`, `unknown`, or an external or local type), `mediaType`, `id`, `encoding`, `lang` and `data` (a `DataView` of the payload).
  - `write()` takes `{overwrite, signal}`, and `overwrite` defaults to true. NFC is suspended while the page is not visible, and a pending write is aborted then.
  - `makeReadOnly()` exists and "cannot be reversed". This plan never calls it (spec anti-goal).
- **OpenSpool** (https://github.com/spuder/OpenSpool, README "OpenSpool Protocol" at `c060a06c`): one NDEF record of type `application/json`. The published sample is `{"protocol": "openspool", "version": "1.0", "type": "PLA", "color_hex": "FFAABB", "brand": "Generic", "min_temp": "220", "max_temp": "240"}`, with every value a string. The README asks for NTAG215 or NTAG216.
- **OpenTag3D** (https://github.com/queengooborg/OpenTag3D at `e14ee7ee`; `spec.md`, `_data/spec.json`, `assets/scripts/opentag3d.js`; the same JSON is served at https://opentag3d.info/spec.json):
  - The format is one NDEF record of MIME type `application/opentag3d`. The spec version is `2.004`, and the core address range is `0x00`–`0xDF`.
  - Integers are unsigned big-endian. Strings are UTF-8, NUL-padded to the field length (`encodeUtf8` in `opentag3d.js`). Temperatures are stored in °C divided by 5.
  - Missing bytes read as `0x00`. A reader rejects a newer major version and warns on a newer minor version.
  - The field table (id, start, length, type, scaling, required) is in Task 2, copied from `spec.json`.
- **NTAG216 usable memory** is 888 bytes, and NTAG215 has 504 bytes (OpenTag3D `spec.md`, "Hardware Standard").

## Screens (from the UX canvas)

1. **Scan ready.** A big "Scan" button that stays on once tapped, and a list of this session's reads.
2. **Known tag.** The spool card, then two camera buttons ("Filament label", "Spool label"). Then a gross-weight field that shows the empty-spool math, then "Open spool" and "Write tag".
3. **New tag.** The decoded data, the two photo buttons, "Create a spool from this tag", and a bind search.
4. **Not Chrome on Android.** The page URL with a "Copy" button.
5. **Write.** Three steps on one tap: read and back up, write, read back. There is no format choice.
6. **Result.** "Written and checked", or "The tag does not hold what was sent" with "Restore from the backup".
7. **Agent job banner.** The records, an expiry countdown, "Arm" and "Decline".

## Global Constraints

- Only the API process (the CRUD service) touches SQL (repo `CLAUDE.md`, "Database access"). The agent reaches jobs only through `/api/v1/filament/nfc-jobs…` routes.
- Migrations are new files only, named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` in `backend/scadbuddy/migrations/`, created with that command when the task runs. Never edit a merged file. The slugs below are fixed. The timestamps are not.
- Images are blobs with `{name, mediaType}` pointers, never base64 (#2479). Photos go through `entityApi.addAttachment` (multipart, `frontend/src/components/entities/api.ts:33`).
- A new setting has its label and help in `backend/scadbuddy/core/setting_docs.py`.
- Every new `/api/v1` route has an agent tool or a `NOT_A_TOOL` entry with a reason longer than 20 characters (`agent/src/tools/coverage.ts:8`, checked by `agent/test/coverage.test.ts:31-35`), in the same task that adds the route. Run `cd agent && pnpm gen:api` first.
- Every non-read agent tool has an extractor or a `TOUCHES_NOTHING` entry (`agent/src/sessions/touched.ts:703`, `agent/test/touched.test.ts:597-600`).
- Unit tests mock Web NFC with the `NDEFReader` stub of Task 5 (`frontend/src/test/ndefStub.ts`). No test reaches a real `NDEFReader`.
- Never call `makeReadOnly`. Never write without a backup, and never write without a UID (G7).
- Tag format names are fixed: `openspool` and `opentag3d`. The setting is `filament_tag_format`, its default is `openspool`, and it sits in Settings → Filament (the `filament` section of `frontend/src/pages/settings/fields.ts`).
- Backup reasons are fixed: `spool_write`, `agent_write` and `restore`. Job kinds are `read` and `write`. Job states are `pending`, `armed`, `done`, `failed` and `expired`.
- Photo roles are fixed: `filament_label` and `spool_label` (entity attachment `role`, `^[a-z_]{1,32}$`, `backend/scadbuddy/api/entity_attachments.py:126`).
- The phone page polls jobs every 2 s, and only while `document.visibilityState === 'visible'`.
- The tags store a record's `data_hex` in lower case. Two record lists are equal when their `(record_type, media_type, data_hex)` triples are equal in order.
- Backend tests use a Postgres in Docker on a unique port (see `CLAUDE.local.md`):
  `docker run -d --rm --name sbtest-7273 -p 55473:5432 -e POSTGRES_HOST_AUTH_METHOD=trust mirror.gcr.io/library/postgres:18 -c timezone=UTC`, then
  `docker exec sbtest-7273 createdb -U postgres scadbuddy_test`, then
  `export SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55473/scadbuddy_test`.
  Use at most `-n 4`. Remove the container by name (`docker rm -f sbtest-7273`) at the end. `tests/api` needs a `temporal` CLI on `PATH`; a skip is not a pass.
- Frontend and agent: run `pnpm gen:api` before any `pnpm exec vitest`. Never commit `backend/openapi.json` or any `schema.d.ts`.
- For uv, use the image's version: `uvx uv@0.13.9` (`Dockerfile:38`).
- Commit titles are conventional and end with `(#7273)`. Every commit body ends with exactly:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never amend, stash, force push or `git add -f`. Do not push.
- No `pkill -f` and no `killall`. Kill by pid only.

## Rulings (spec points this plan decides)

- **R1. "records `tag_seen` data"** means three new columns on `filament_tags`: `last_seen_at`, `last_seen_source` and `last_seen_records`. It is not a ledger event. The ledger is per spool, and an unbound tag has no spool. Its `kind` CHECK has no such kind, and the AMS reports about once a second.
- **R2. The AMS** goes through `tags.seen_on`. That is the lookup and "seen" core that `read_on` is built on. `seen_on` never makes a tag row for an AMS key that no tag holds, because the unknown-spool flow (#7143) owns those. `reconcile` calls it only for trays with keys, and `reconcile` already runs only when `trays_changed` says a tray changed (`capture.py:130`). So the once-a-second path gains no query when nothing changed.
- **R3. A write replaces the whole NDEF message** with one record of the picked format. Other records on the tag survive only in the backup. OpenTag3D readers take the first matching record (`spec.md`, "Reader Implementation Guidelines"), and an NTAG215 holds at most 471 bytes of OpenTag3D payload.
- **R4. `POST /spools/{id}/tag-written`** takes `{uid, backup_id, sent, read_back}`. The server cannot know what was sent without updating a backup row, and backup rows are append-only.
- **R5. A backup is valid for one write** when it names the same cleaned UID, is at most 10 minutes old, and has the reason the route expects. Otherwise the route answers 409 `backup_mismatch`.
- **R6. A restore is checked in the page.** It compares the read-back with the records the restore route answered. The page then posts the read-back to `POST /tags/reads`. There is no spool to bind, so no server check is needed.
- **R7. A job lives `ttl_s` seconds** (1 to 600, default 600, the spec's 10 minutes). The agent tools send their `timeout_s`, so a job never stays armable after its agent stopped waiting.
- **R8. `nfc_read` is a `write`-risk tool**, because it makes a job row (the `filament_type_profile_fetch` precedent, `agent/test/touched.test.ts:592`). `nfc_write` is `outward` with `approval: 'none'`, because the person's Arm-and-tap on the page is its approval (spec, Constraints).
- **R9. The removal also takes out** `RfidStatus.devices`, `BambuddyClient.spoolbuddy_devices`, `ws_token`, `_tag_scan`, `TagScan`, `SpoolBuddyDevice` and the `websockets` dependency. Each of them served only the SpoolBuddy scan or write (spec anti-goal: "Any Bambuddy or SpoolBuddy call for tags").
- **R10. A tag's backups and "Restore"** show on the scan page's tag view, not on the Tags tab. A restore is a write, and only the phone page can write.
- **R11. An OpenSpool write sets `data_origin` `openspool`**, and an OpenTag3D write sets `opentag3d` (Bambuddy's word, `filament/ntag.py:25`). Both also set `tag_encoded_at` to now, through `FilamentSpoolStore.set_origin` (`spools.py:1154`).
- **R12. "Create a spool from this tag"** uses a `draft` (a `SpoolCreate`) that the read answers. The read builds the draft by catalog lookup only (`catalog.find_by_key`, `catalog.py:329`). The page then calls `createSpool`, then binds through `POST /tags/reads` with `bind_spool_id`. With no material found, there is no draft, and the page links to `/filament/spools/new`.
- **R13. When a read's records decode as OpenSpool or OpenTag3D, the tag's stored `kind` changes** to `ntag_openspool` or `ntag_opentag`. A `bambu_mifare` tag never changes kind. A write can change a tag's format, and the kind must follow it.
- **R14. A phone read with no UID** answers the decoded data with `tag: null`, `spool: null` and `next: "none"`. It makes no tag row and binds nothing.
- **R15. The OpenTag3D mapping** is the table in Task 2. A required field with no value is written as 0 and named in `warnings`, and the page shows the warnings before the write.

## Review Focus

1. **A different tag answers the read-back** (the person moved the phone to another tag). Expected: nothing binds, and the page says "A different tag answered. Nothing was bound." Tests: Task 4 `test_a_read_back_from_another_uid_binds_nothing`, Task 6 `says a different tag answered and binds nothing`.
2. **The read-back does not come in the same tap.** Expected: after 3 s the page asks the person to lift the tag and tap it again. It never binds without a read-back, and the write's backup stays. Test: Task 6 `asks for a second tap when no read-back comes`.
3. **A Bambu MIFARE tag read by the phone** (a UID and no records). Expected: no format is claimed, `decoded` is null, the holder is found by UID when its stored key matches, and a write that the browser rejects shows its error and keeps the backup. Tests: Task 1 `test_a_read_with_no_records_claims_no_format`, Task 6 `shows the browser's write error and keeps the backup`.
4. **Long or non-ASCII names in fixed OpenTag3D fields.** Expected: UTF-8 is cut at a character boundary within the field length, never mid-character, and it decodes back without U+FFFD. Test: Task 2 `test_opentag3d_cuts_utf8_at_a_character_boundary`.
5. **A stale backup used for a write** (an old tab, or another tag's backup). Expected: 409 `backup_mismatch`, and no records are answered. Tests: Task 4 `test_tag_records_refuse_a_backup_of_another_uid`, `test_tag_records_refuse_a_backup_older_than_ten_minutes`.

---

## File Structure

Backend (`backend/scadbuddy/`):
- `filament/tags.py` (new): `TagRead`, `holder_by_keys`, `seen_on`, `read_on`, `read`, `same_records`, the backup and job store functions.
- `filament/tag_formats.py` (new): the OpenSpool and OpenTag3D encoders and decoders, `format_of`, `pick_format`, `facts_of`.
- `filament/models.py`: the wire models (`NdefRecord`, `TagReadIn`, `TagReadResult`, `DecodedSpool`, `TagBackupIn`, `TagBackup`, `TagRecordsIn`, `TagRecords`, `TagWrittenIn`, `NfcJobIn`, `NfcJob`, `NfcJobResultIn`). Also `Tag.last_seen_at` and `Tag.last_seen_source`, and the removal of the SpoolBuddy models (Task 10).
- `api/filament.py`: the new routes, and the removal of `/rfid/scan` and `/spools/{id}/ntag/*`.
- `filament/inventory.py`: `holder_on`'s tag half calls `tags.holder_by_keys`.
- `filament/capture.py`: `reconcile` calls `tags.seen_on`.
- `core/settings.py`, `core/setting_docs.py`, `library/settings_store.py`, `api/settings.py`: `filament_tag_format`.
- Migrations: `<ts>_filament_tag_seen.sql`, `<ts>_filament_tag_backups.sql`, `<ts>_filament_nfc_jobs.sql`, `<ts>_drop_filament_ntag_writes.sql`.
- Removed: `filament/ntag.py`, and the SpoolBuddy calls in `bambuddy/client.py` and `bambuddy/models.py`.

Frontend (`frontend/src/`):
- `lib/webNfc.ts` (new): the Web NFC types, `nfcSupport`, `NfcSession`, `toWire`, `toInit` and `sameRecords`.
- `lib/tagWrite.ts` (new): `writeWithBackup`, one tap of read and back up, write, read back.
- `test/ndefStub.ts` (new): the `NDEFReader` stub.
- `api/filament.ts`: the client calls for the new routes.
- `mocks/features/filament/tags.ts` (new): the mocks of the new routes.
- `pages/filament/ScanPage.tsx` (new), with `components/filament/scan/` (new): `KnownTag.tsx`, `NewTag.tsx`, `PhotoButtons.tsx`, `WeightField.tsx`, `WriteTag.tsx`, `TagBackups.tsx`, `NfcJobBanner.tsx`.
- `App.tsx`, `lib/filamentLinks.ts`: the `/filament/scan` route.
- Removed: `components/filament/NtagPanel.tsx` and its test, the scan button in `TagPanel.tsx`, and the SpoolBuddy handlers in `mocks/features/filament/rfid.ts`.

Agent (`agent/src/`):
- `tools/nfc.ts` (new): `nfc_read`, `nfc_write`. `tools/filament.ts`: `filament_tag_backups` (new). The four SpoolBuddy tools are removed.

---

### Task 1: `TagRead`, the one read function, and `POST /filament/tags/reads`

**Files:**
- Create: `backend/scadbuddy/filament/tags.py`
- Create: `backend/scadbuddy/migrations/<ts>_filament_tag_seen.sql`
- Modify: `backend/scadbuddy/filament/models.py:1526-1533` (`Tag`), plus a new section after `TagBind` (`models.py:1699`)
- Modify: `backend/scadbuddy/filament/inventory.py:281-301` (`holder_on`'s tag loop)
- Modify: `backend/scadbuddy/api/filament.py` (new route after `get_tag`, `api/filament.py:1707-1719`)
- Modify: `agent/src/tools/coverage.ts` (`NOT_A_TOOL`)
- Test: `backend/tests/filament/test_tags.py` (new), `backend/tests/api/test_filament_tags.py` (new)

**Interfaces:**
- Produces (`filament/models.py`):
  - `class NdefRecord(_In)`: `record_type: str` (1–255 chars, `^[A-Za-z0-9:._/+-]+$`), `media_type: str | None` (≤127), `id: str | None` (≤255), `encoding: str | None` (≤32), `lang: str | None` (≤35), and `data_hex: str` (`^([0-9a-fA-F]{2})*$`, at most `2 * MAX_RECORD_BYTES` characters, lower-cased by a validator).
  - `MAX_RECORD_BYTES: Final = 888` (NTAG216 usable memory) and `MAX_RECORDS: Final = 16`. A list of records whose `data_hex` bytes add up to more than 888 is a 422.
  - `TagSource = Literal["phone", "agent", "ams"]`.
  - `class TagReadIn(_In)`: `uid: str | None` (≤64), `tag_type: str | None` (≤32), `records: list[NdefRecord]` (≤16), `bind_spool_id: UUID | None = None`.
  - `class DecodedSpool(BaseModel)`: `format: Literal["openspool", "opentag3d"]`, `version: str`, and the optional fields `material`, `subtype`, `brand`, `colour_name`, `rgba`, `extra_colours`, `diameter_mm`, `density_g_cm3`, `nozzle_temp_min`, `nozzle_temp_max`, `bed_temp_min`, `bed_temp_max`, `drying_temp_c`, `drying_hours`, `label_weight_g`, `empty_weight_g` and `serial`. Also `warnings: list[str] = []`. (Task 2 fills it in. Task 1 only declares it.)
  - `class TagReadResult(BaseModel)`: `tag: TagRow | None`, `spool: Spool | None`, `decoded: DecodedSpool | None`, `draft: SpoolCreate | None`, `next: Literal["open_spool", "bind_or_create", "none"]`.
  - `Tag` gains `last_seen_at: datetime | None = None` and `last_seen_source: str | None = None`. `_TAG_COLUMNS` (`spools.py:68-70`) picks them up from `model_fields`.
- Produces (`filament/tags.py`):
  - `@dataclass(frozen=True) class TagRead`: `source: TagSource`, `uid: str | None`, `tray_uuid: str | None = None`, `tag_type: str | None = None`, `records: tuple[NdefRecord, ...] = ()`, `printer_id: UUID | None = None`, `ams_id: int | None = None`, `tray_id: int | None = None`.
  - `holder_by_keys(conn: Conn, tray_uuid: str | None, tag_uid: str | None) -> UUID | None`. This is the loop at `inventory.py:290-301`, moved here: an open binding of the tray UUID first, then of the UID, on an unarchived spool.
  - `kind_of(read: TagRead) -> m.TagKind`: `bambu_mifare` with a tray UUID, else the decoded format's kind (Task 2), else `other`.
  - `seen_on(conn: Conn, read: TagRead) -> tuple[UUID | None, UUID | None]` returns `(tag_id, holder_spool_id)` on `conn`'s transaction. It cleans the keys with `clean_tag_uid` and `clean_tray_uuid`. For `phone` and `agent` with a UID, it makes the tag (`FilamentSpoolStore.tag_on`, `spools.py:1203`). For `ams`, it only finds the tag (R2). It sets `last_seen_at = now()`, `last_seen_source` and `last_seen_records` (the records as JSON) on a found or made tag. It changes the kind as R13 says.
  - `read_on(conn: Conn, read: TagRead, *, actor: str, bind_spool_id: UUID | None = None, defaults: FilamentDefaults) -> TagReadResult`. It calls `seen_on`. With `bind_spool_id`, it binds through `FilamentSpoolStore.bind_on(conn, bind_spool_id, uid, None, kind_of(read), None, actor, marks_origin=True)` (`spools.py:1065`), which books `tag_bound`, and that spool becomes the holder. It reads the holder with `FilamentSpoolStore.read(conn, id, defaults=defaults)`. `next` is `open_spool` with a holder, `bind_or_create` with a tag and no holder, and `none` with no tag (R14). `bind_spool_id` with `source="ams"` raises `ValueError`.
  - `read(spools: FilamentSpoolStore, read: TagRead, *, actor: str, bind_spool_id: UUID | None = None, defaults: FilamentDefaults) -> TagReadResult`. This is `read_on` in one `spools.transaction()`.
  - `BindWithoutUidError(ValueError)` (`code = "no_uid"`): a `bind_spool_id` with no UID.
- Produces (route): `POST /api/v1/filament/tags/reads`, body `TagReadIn`, answer `TagReadResult` (200). It answers 404 for an unknown `bind_spool_id`, 422 `no_uid` for a bind with no UID, and 409 for a key clash (`TagKeysClashError`, as `bind_spool_tag` maps it, `api/filament.py:1427`). The actor is `actor(caller)` (`api/filament.py:1039`). After a bind it runs `_push_now` (`api/filament.py:1435`), as the bind route does.

- [ ] **Step 1: Write the migration** `<ts>_filament_tag_seen.sql`:

```sql
-- #7273 R1: when a tag was last read, by which source, and the NDEF records it held.
ALTER TABLE filament_tags
    ADD COLUMN last_seen_at timestamptz,
    ADD COLUMN last_seen_source text CHECK (last_seen_source IN ('phone', 'agent', 'ams')),
    ADD COLUMN last_seen_records jsonb;
```

- [ ] **Step 2: Write the failing store tests** in `backend/tests/filament/test_tags.py` (`pytestmark = pytest.mark.requires_postgres`; make spools as `tests/filament/test_rfid_binding.py:24-30` does):

```python
def test_a_phone_read_of_a_new_uid_makes_the_tag_and_marks_it_seen(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid="04:a2:b3:c4:d5:e6:f7"), actor="ann", defaults=DEFAULTS)
    assert result.tag is not None and result.tag.tag_uid == "04A2B3C4D5E6F7"
    assert result.tag.last_seen_source == "phone" and result.tag.last_seen_at is not None
    assert result.spool is None and result.next == "bind_or_create"

def test_a_read_of_a_bound_uid_names_its_spool(pg_pool):
    store.bind_tag(spool, tag_uid="04A2B3C4D5E6F7", kind="other", actor="ann")
    result = tags.read(store, TagRead(source="phone", uid="04a2b3c4d5e6f7"), actor="ann", defaults=DEFAULTS)
    assert result.spool is not None and result.spool.id == spool and result.next == "open_spool"

def test_a_read_with_no_uid_makes_no_tag(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid=""), actor="ann", defaults=DEFAULTS)
    assert (result.tag, result.spool, result.next) == (None, None, "none")
    assert _count(pg_pool, "filament_tags") == 0

def test_a_read_with_no_records_claims_no_format(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid="A1B2C3D4"), actor="ann", defaults=DEFAULTS)
    assert result.decoded is None and result.tag.kind == "other"

def test_a_read_with_bind_spool_id_binds_and_books_tag_bound(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid="04AA"), actor="ann", bind_spool_id=spool, defaults=DEFAULTS)
    assert result.spool.id == spool and result.next == "open_spool"
    assert [e.kind for e in store.events(spool)][-2:] == ["edited", "tag_bound"]  # marks_origin edits data_origin first? keep the order bind_on books

def test_an_ams_read_never_makes_a_tag(pg_pool):
    with store.transaction() as conn:
        assert tags.seen_on(conn, TagRead(source="ams", uid="AABB", tray_uuid="0123")) == (None, None)
    assert _count(pg_pool, "filament_tags") == 0

def test_an_ams_read_of_a_bound_tray_marks_it_seen(pg_pool):
    store.bind_tag(spool, tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    with store.transaction() as conn:
        tag_id, holder = tags.seen_on(conn, TagRead(source="ams", uid=None, tray_uuid=_TRAY))
    assert holder == spool and _tag(pg_pool, tag_id)["last_seen_source"] == "ams"

def test_the_inventory_finds_a_tray_holder_through_the_same_lookup(pg_pool):
    # FilamentInventory.holder_on (inventory.py:281) answers what tags.holder_by_keys answers
    ...
```

In `test_a_read_with_bind_spool_id_binds_and_books_tag_bound`, assert that the last event is `tag_bound`. Assert the full kind list exactly as `bind_on` books it. Read `bind_on`'s order of `tag_bound` and `set_origin`'s `edited` at `spools.py:1135-1149` and copy it into the test.

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tags.py -n 4 -q`
Expected: FAIL with `ModuleNotFoundError: scadbuddy.filament.tags`.

- [ ] **Step 4: Implement** `filament/tags.py` and the models above. Then replace the loop in `FilamentInventory.holder_on` (`inventory.py:290-301`) with one call to `tags.holder_by_keys(conn, seen.tray_uuid, seen.tag_uid)`. Keep `holder_on`'s `"tag"`/`"link"` answer and its docstring's rule. `tags.py` imports `FilamentSpoolStore` from `spools.py`. It must not import `inventory.py` (`inventory` imports `tags`).

- [ ] **Step 5: Run the store tests and the inventory and capture tests**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tags.py tests/filament/test_inventory_trays.py tests/filament/test_capture.py tests/filament/test_rfid_binding.py -n 4 -q`
Expected: PASS.

- [ ] **Step 6: Write the failing route tests** in `backend/tests/api/test_filament_tags.py`. Use a local `_spool(client)` that copies `tests/api/test_filament.py:182-186`.

```python
READS = "/api/v1/filament/tags/reads"

def test_a_phone_read_answers_the_tag_and_next(client):
    got = client.post(READS, json={"uid": "04:a2:b3:c4", "records": []})
    assert got.status_code == 200, got.text
    assert got.json()["next"] == "bind_or_create" and got.json()["tag"]["tag_uid"] == "04A2B3C4"

def test_a_read_binds_with_bind_spool_id(client):
    spool = _spool(client)
    got = client.post(READS, json={"uid": "04a2", "records": [], "bind_spool_id": spool["id"]})
    assert got.json()["spool"]["id"] == spool["id"] and got.json()["next"] == "open_spool"

def test_a_bind_without_a_uid_is_422_no_uid(client):
    got = client.post(READS, json={"uid": None, "records": [], "bind_spool_id": _spool(client)["id"]})
    assert got.status_code == 422 and got.json()["code"] == "no_uid"

def test_a_bind_to_an_unknown_spool_is_404(client):
    assert client.post(READS, json={"uid": "04a2", "records": [], "bind_spool_id": str(uuid4())}).status_code == 404

def test_records_past_888_bytes_are_refused(client):
    big = {"record_type": "mime", "media_type": "application/json", "data_hex": "00" * 889}
    assert client.post(READS, json={"uid": "04a2", "records": [big]}).status_code == 422
```

- [ ] **Step 7: Run them and see them fail** (404 on the route)

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py -q`
Expected: FAIL.

- [ ] **Step 8: Add the route** `read_tag` (summary "Record a tag reading"). Its description names the source (`phone`), what `next` means, and the codes. It runs `tags.read` in `asyncio.to_thread`. Pass `defaults` from `filament.spools`'s settings read, as `get_spool` does: find how `get_spool` gets its `FilamentDefaults` in `api/filament.py` and do the same.

- [ ] **Step 9: Add the coverage entry** in `agent/src/tools/coverage.ts`. `'POST /api/v1/filament/tags/reads'` gets the reason "The phone page's own Web NFC reading (#7273): the browser reads the tag. An agent reads a tag through nfc_read, which queues a job that the phone page runs."

- [ ] **Step 10: Run the route tests, the agent's coverage and lint**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py tests/api/test_filament.py -q && uv run --frozen ruff check . && uv run --frozen mypy`
Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/coverage.test.ts`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add backend/scadbuddy/filament/tags.py backend/scadbuddy/filament/models.py backend/scadbuddy/filament/inventory.py backend/scadbuddy/api/filament.py backend/scadbuddy/migrations/*_filament_tag_seen.sql backend/tests/filament/test_tags.py backend/tests/api/test_filament_tags.py agent/src/tools/coverage.ts
git commit -m "feat(filament): one read function for every tag reading (#7273)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The OpenSpool and OpenTag3D formats, the format choice and `filament_tag_format`

**Files:**
- Create: `backend/scadbuddy/filament/tag_formats.py`
- Modify: `backend/scadbuddy/filament/tags.py` (`read_on` fills `decoded` and `draft`, and `kind_of` uses `format_of`)
- Modify: `backend/scadbuddy/core/settings.py:798-815` (field) and `:1172-1180` (`"live"`), `backend/scadbuddy/core/setting_docs.py:427` (doc), `backend/scadbuddy/library/settings_store.py:900-909` (update field), `backend/scadbuddy/api/settings.py:315-322` (view field)
- Modify: `frontend/src/pages/settings/fields.ts:184-224` (a `select` in `filament`), `frontend/src/mocks/fixtures.ts:1091`
- Test: `backend/tests/filament/test_tag_formats.py` (new), `backend/tests/filament/test_tags.py`, `backend/tests/api/test_settings.py` (next to `test_filament_settings_round_trip`, `:1064`), `frontend/src/pages/settings/sections/filament.test.tsx`

**Interfaces:**
- Consumes: `NdefRecord`, `DecodedSpool`, `TagRead`, `read_on` (Task 1).
- Produces (`filament/tag_formats.py`):
  - `TagFormat = Literal["openspool", "opentag3d"]`, `OPENSPOOL_MEDIA: Final = "application/json"`, `OPENTAG3D_MEDIA: Final = "application/opentag3d"`, `OPENTAG3D_VERSION: Final = 2004`, `OPENTAG3D_SIZE: Final = 0xE0`.
  - `@dataclass(frozen=True) class SpoolFacts`: `material`, `subtype`, `brand`, `colour_name: str | None`, `rgba: str | None` (`#RRGGBBAA`), `extra_colours: tuple[str, ...]`, `diameter_mm`, `density_g_cm3: Decimal | None`, `nozzle_temp_min`, `nozzle_temp_max`, `bed_temp_min`, `bed_temp_max`, `drying_temp_c: int | None`, `drying_hours: Decimal | None`, `label_weight_g`, `empty_weight_g: int | None`, `code: str` (`codes.parse.format_code("S", number)`).
  - `facts_of(spool: m.Spool, empty_weight_g: int | None) -> SpoolFacts`. It reads `spool.effective` and `spool.names`.
  - `@dataclass(frozen=True) class EncodedTag`: `format: TagFormat`, `records: list[NdefRecord]`, `warnings: list[str]`.
  - `encode(fmt: TagFormat, facts: SpoolFacts) -> EncodedTag`. It always gives exactly one record (R3).
  - `format_of(records: Sequence[NdefRecord]) -> TagFormat | None`. It looks for the first `mime` record with media type `application/opentag3d`, or with `application/json` whose JSON object has `"protocol": "openspool"`. The first of these in record order wins.
  - `pick_format(records: Sequence[NdefRecord], default: TagFormat) -> TagFormat`. This is `format_of(records)`, else `default` (G2).
  - `decode(records: Sequence[NdefRecord]) -> m.DecodedSpool | None`.
  - `KIND_OF: Final[dict[TagFormat, m.TagKind]] = {"openspool": "ntag_openspool", "opentag3d": "ntag_opentag"}`.
  - `ORIGIN_OF: Final[dict[TagFormat, str]] = {"openspool": "openspool", "opentag3d": "opentag3d"}` (R11).
- Produces (`tags.py`): `read_on` sets `decoded = decode(read.records)`. With no holder, it sets `draft = draft_of(conn, decoded)`: a `SpoolCreate` with `material_id = find_by_key(conn, "materials", decoded.material)`, `brand_id` from `"brands"`, and `subtype_id` from `"subtypes"` with `material=` (`catalog.py:329`). The colour, temperatures, diameter, density, drying and label weight are copied over. There is no draft when no material is found (R12).
- Produces (setting): `Settings.filament_tag_format: Literal["openspool", "opentag3d"] = "openspool"`, applying `live`.

**OpenSpool mapping** (README sample; every value is written as a string):

| JSON key | from `SpoolFacts` | write rule | read rule |
| --- | --- | --- | --- |
| `protocol` | — | `"openspool"` | must be `"openspool"` |
| `version` | — | `"1.0"` | kept in `DecodedSpool.version` |
| `type` | `material` | the name; if missing, warn `type` and omit the key | `material` |
| `color_hex` | `rgba` | `RRGGBB`, upper case, alpha dropped; omit when missing | `rgba = "#" + hex + "FF"` |
| `brand` | `brand` | the name, else `"Generic"` (the README sample's value) | `brand` |
| `min_temp` | `nozzle_temp_min` | `str(int)`; omit when missing | `nozzle_temp_min` (string or number) |
| `max_temp` | `nozzle_temp_max` | `str(int)`; omit when missing | `nozzle_temp_max` |

Write the keys in that order, with `json.dumps(obj, separators=(",", ":"), ensure_ascii=False)`, as UTF-8.

**OpenTag3D mapping** (`spec.json` v2.004 core fields; big-endian; strings NUL-padded and UTF-8 cut at a character boundary; `°C/5` means the value divided by 5 and rounded half up). The payload is always `OPENTAG3D_SIZE` (224) bytes:

| id | start | len | type | from `SpoolFacts` (write) | to `DecodedSpool` (read) |
| --- | --- | --- | --- | --- | --- |
| `tag_version` | 0x00 | 2 | int ×0.001 | `2004` | `version` `"2.004"`; a major above 2 decodes to only `format`, `version` and the warning `"newer major version"`; a minor above 004 adds the warning `"newer minor version"` |
| `material` | 0x02 | 5 | utf8 | `material` (required) | `material` |
| `material_mod` | 0x07 | 5 | utf8 | `subtype` | `subtype` |
| `manufacturer` | 0x0C | 16 | utf8 | `brand`, else `"Generic"` (required) | `brand` |
| `color_name` | 0x1C | 32 | utf8 | `colour_name` | `colour_name` |
| `color_1` | 0x3C | 4 | rgba | `rgba` bytes (required) | `rgba` |
| `color_2..4` | 0x40, 0x44, 0x48 | 4 each | rgba | `extra_colours[0..2]`, else zeros | `extra_colours` (an all-zero entry left out) |
| `serial` | 0x4C | 32 | utf8 | `code` (`S-12`) | `serial` |
| `diameter` | 0x8C | 2 | int ×0.001 | `diameter_mm × 1000` (required) | `diameter_mm` |
| `print_temp` | 0x90 | 1 | int ×5 | the midpoint of the nozzle min and max, `°C/5` (required) | — |
| `min_print_temp` | 0x91 | 1 | int ×5 | `nozzle_temp_min`, `°C/5` | `nozzle_temp_min` (the value × 5) |
| `max_print_temp` | 0x92 | 1 | int ×5 | `nozzle_temp_max`, `°C/5` | `nozzle_temp_max` |
| `chamber_temp` | 0x93 | 1 | int ×5 | `0` (required; ScadBuddy keeps none) | — |
| `bed_temp` | 0x94 | 1 | int ×5 | the midpoint of the bed min and max (required) | — |
| `min_bed_temp` | 0x95 | 1 | int ×5 | `bed_temp_min` | `bed_temp_min` |
| `max_bed_temp` | 0x96 | 1 | int ×5 | `bed_temp_max` | `bed_temp_max` |
| `max_dry_temp` | 0x9A | 1 | int ×5 | `drying_temp_c` | `drying_temp_c` |
| `dry_time` | 0x9B | 1 | int (h) | `drying_hours`, rounded | `drying_hours` |
| `density` | 0x9C | 2 | int ×0.001 | `density_g_cm3 × 1000` (required) | `density_g_cm3` |
| `weight` | 0x9E | 2 | int (g) | `label_weight_g` (required) | `label_weight_g` |
| `empty_spool_weight` | 0xA0 | 2 | int (g) | `empty_weight_g` | `empty_weight_g` |

On read, with a min or max print temperature of 0 and a non-zero target, the decoder uses the target for both. Every field not in this table is written as 0. A required field with no value is written as 0, and its id goes into `warnings` (R15). A decoded value of 0 is `None`, except `chamber_temp`, which is never decoded.

- [ ] **Step 1: Write the failing format tests** in `backend/tests/filament/test_tag_formats.py` (no database):

```python
OPENSPOOL_SAMPLE = {"protocol": "openspool", "version": "1.0", "type": "PLA", "color_hex": "FFAABB",
                    "brand": "Generic", "min_temp": "220", "max_temp": "240"}  # github.com/spuder/OpenSpool README @ c060a06c

def _json_record(obj) -> NdefRecord:
    return NdefRecord(record_type="mime", media_type="application/json", data_hex=json.dumps(obj).encode().hex())

def test_openspool_decodes_the_published_sample():
    got = decode([_json_record(OPENSPOOL_SAMPLE)])
    assert (got.format, got.version, got.material, got.brand, got.rgba) == ("openspool", "1.0", "PLA", "Generic", "#FFAABBFF")
    assert (got.nozzle_temp_min, got.nozzle_temp_max) == (220, 240)

def test_openspool_encodes_back_to_the_sample_keys_and_strings():
    enc = encode("openspool", FACTS_PLA)  # FACTS_PLA: PLA, Generic, #FFAABBFF, 220..240
    assert enc.records[0].media_type == "application/json" and enc.records[0].record_type == "mime"
    assert json.loads(bytes.fromhex(enc.records[0].data_hex)) == OPENSPOOL_SAMPLE

def test_opentag3d_decodes_a_payload_built_from_spec_json_examples():
    payload = _ot3d_from_examples()  # each core field's examples[0] at its start, spec.json v2.004
    got = decode([NdefRecord(record_type="mime", media_type="application/opentag3d", data_hex=payload.hex())])
    assert got.format == "opentag3d" and got.version == "1.234"   # tag_version example 1234
    assert (got.material, got.subtype, got.brand) == ("PLA", "CF", "Example Brand")
    assert got.rgba == "#FFA64DFF" and got.diameter_mm == Decimal("1.750") and got.density_g_cm3 == Decimal("1.240")
    assert (got.nozzle_temp_min, got.nozzle_temp_max, got.label_weight_g, got.empty_weight_g) == (190, 225, 1000, 105)

def test_opentag3d_round_trips_a_spool():
    enc = encode("opentag3d", FACTS_FULL)
    assert len(bytes.fromhex(enc.records[0].data_hex)) == 0xE0 and enc.warnings == []
    got = decode(enc.records)
    assert (got.material, got.brand, got.rgba, got.label_weight_g, got.serial) == ("PETG", "Polar", "#112233FF", 1000, "S-12")

def test_opentag3d_names_each_missing_required_field():
    assert set(encode("opentag3d", FACTS_EMPTY).warnings) == {"material", "color_1", "diameter", "print_temp", "bed_temp", "density", "weight"}

def test_opentag3d_cuts_utf8_at_a_character_boundary():
    enc = encode("opentag3d", replace(FACTS_FULL, brand="Ünïcødé Filament Co"))  # 16-byte field
    got = decode(enc.records)
    assert "�" not in got.brand and len(got.brand.encode()) <= 16

def test_a_newer_major_version_is_not_read():
    payload = bytearray(_ot3d_from_examples()); payload[0:2] = (3000).to_bytes(2, "big")
    got = decode([NdefRecord(record_type="mime", media_type="application/opentag3d", data_hex=payload.hex())])
    assert got.material is None and "newer major version" in got.warnings

def test_pick_format_keeps_the_tags_format_and_else_takes_the_default():
    assert pick_format([_json_record(OPENSPOOL_SAMPLE)], "opentag3d") == "openspool"
    assert pick_format([ot3d_record], "openspool") == "opentag3d"
    assert pick_format([], "opentag3d") == "opentag3d"
    assert pick_format([NdefRecord(record_type="url", data_hex="00")], "openspool") == "openspool"
    assert pick_format([_json_record({"protocol": "other"})], "opentag3d") == "opentag3d"

def test_a_json_record_that_is_not_json_decodes_to_nothing():
    assert decode([NdefRecord(record_type="mime", media_type="application/json", data_hex="7b7b")]) is None
```

`_ot3d_from_examples` writes the `examples[0]` of each field in the mapping table at its start and length, as `spec.json` lists them. Put a link to `https://github.com/queengooborg/OpenTag3D/blob/e14ee7ee5ff34629eb41f9f80d4830eac073cb4a/_data/spec.json` above it. The examples are: tag_version 1234, material `PLA`, material_mod `CF`, manufacturer `Example Brand`, color_1 `[255,166,77,255]`, diameter 1750, min_print_temp 38, max_print_temp 45, density 1240, weight 1000, empty_spool_weight 105.

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tag_formats.py -q`
Expected: FAIL (no module).

- [ ] **Step 3: Implement** `filament/tag_formats.py`, using the two tables above. Hold the OpenTag3D layout in one table of `(id, start, length, kind, scale)` rows. Put the `spec.json` link and commit in a comment.

- [ ] **Step 4: Run the format tests**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tag_formats.py -q`
Expected: PASS.

- [ ] **Step 5: Add the read tests** in `tests/filament/test_tags.py`:

```python
def test_a_read_decodes_openspool_and_sets_the_tags_kind(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid="04AA", records=(_json_record(OPENSPOOL_SAMPLE),)), actor="ann", defaults=DEFAULTS)
    assert result.decoded.format == "openspool" and result.tag.kind == "ntag_openspool"

def test_a_new_tag_with_a_known_material_answers_a_draft(pg_pool):
    result = tags.read(store, TagRead(source="phone", uid="04AA", records=(_json_record(OPENSPOOL_SAMPLE),)), actor="ann", defaults=DEFAULTS)
    assert result.draft is not None and result.draft.material_id == _pla(pg_pool) and result.draft.nozzle_temp_min == 220

def test_a_new_tag_with_an_unknown_material_answers_no_draft(pg_pool):
    sample = OPENSPOOL_SAMPLE | {"type": "UNOBTAINIUM"}
    assert tags.read(store, TagRead(source="phone", uid="04AA", records=(_json_record(sample),)), actor="ann", defaults=DEFAULTS).draft is None

def test_a_bambu_tag_keeps_its_kind_whatever_its_records_say(pg_pool):
    store.bind_tag(spool, tag_uid="04AA", tray_uuid=_TRAY, kind="bambu_mifare", actor="ann")
    result = tags.read(store, TagRead(source="phone", uid="04AA", records=(_json_record(OPENSPOOL_SAMPLE),)), actor="ann", defaults=DEFAULTS)
    assert result.tag.kind == "bambu_mifare"
```

- [ ] **Step 6: Implement them** in `tags.py`, then run `tests/filament/test_tags.py` (PASS).

- [ ] **Step 7: Write the failing settings test** in `tests/api/test_settings.py`:

```python
def test_the_tag_format_setting_round_trips(client):
    body = client.get("/api/v1/settings").json()
    assert body["filament_tag_format"] == "openspool" and body["applies"]["filament_tag_format"] == "live"
    assert client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"}).status_code == 200
    assert client.get("/api/v1/settings").json()["filament_tag_format"] == "opentag3d"
    assert client.put("/api/v1/settings", json={"filament_tag_format": "tigertag"}).status_code == 422
```

Add the doc to `setting_docs.py`. Label: `"Tag format for new tags"`. Help: `"The format ScadBuddy writes to a blank tag, or to a tag that holds neither format: OpenSpool (JSON, for NTAG215 or NTAG216) or OpenTag3D. A tag that already holds OpenSpool or OpenTag3D keeps its format."`. Add the field wherever `filament_low_stock_pct` appears in `core/settings.py`, `library/settings_store.py` and `api/settings.py` (`grep -rn filament_low_stock_pct backend/scadbuddy`), using the `Literal` type that `storefront_mail_tls` uses (`core/settings.py:316`, `settings_store.py:947`).

- [ ] **Step 8: Add the frontend field** `{ name: 'filament_tag_format', section: 'filament', kind: 'select', choices: [{ value: 'openspool', label: 'OpenSpool' }, { value: 'opentag3d', label: 'OpenTag3D' }] }`, and `filament_tag_format: 'openspool'` in `mocks/fixtures.ts`. Add a test to `sections/filament.test.tsx`: `it('saves the tag format for new tags')`. It chooses OpenTag3D with `choose` (`src/test/select`), saves, and expects `mockSettings().filament_tag_format` to be `'opentag3d'`.

- [ ] **Step 9: Run every test touched**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tags.py tests/filament/test_tag_formats.py -n 4 -q && uv run --frozen pytest tests/api/test_settings.py -q && uv run --frozen ruff check . && uv run --frozen mypy`
Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/settings/sections/filament.test.tsx`
Expected: PASS.

- [ ] **Step 10: Commit** with the title `feat(filament): OpenSpool and OpenTag3D tag formats and the format setting (#7273)`.

---

### Task 3: The backups table and its routes; no write without a backup or a UID

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_tag_backups.sql`
- Modify: `backend/scadbuddy/filament/tags.py`, `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/api/filament.py`
- Modify: `agent/src/tools/filament.ts` (new tool `filament_tag_backups`), `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_tags.py`, `backend/tests/api/test_filament_tags.py`, `agent/test/filament.test.ts`

**Interfaces:**
- Consumes: `seen_on`, `TagRead`, `NdefRecord` (Task 1).
- Produces (models):
  - `BackupReason = Literal["spool_write", "agent_write", "restore"]`.
  - `class TagBackupIn(_In)`: `uid: str` (1–64), `records: list[NdefRecord]` (≤16), `reason: BackupReason`, `job_id: UUID | None = None`.
  - `class TagBackup(BaseModel)`: `id: UUID`, `tag_id: UUID`, `uid: str`, `records: list[NdefRecord]`, `reason: BackupReason`, `job_id: UUID | None`, `actor: str`, `at: datetime`.
- Produces (`tags.py`):
  - `BACKUP_TTL: Final = timedelta(minutes=10)` (R5).
  - `NoUidError(ValueError)` (`code = "no_uid"`).
  - `back_up(spools: FilamentSpoolStore, body: TagBackupIn, *, actor: str) -> TagBackup`. In one transaction it calls `seen_on(conn, TagRead(source="phone", uid=body.uid, records=tuple(body.records)))` (or `source="agent"` for `agent_write`), then inserts the row. A UID that cleans to `None` raises `NoUidError`.
  - `backups(spools: FilamentSpoolStore, tag_id: UUID) -> list[TagBackup] | None`, newest first. It answers `None` for no such tag.
  - `valid_backup(conn: Conn, backup_id: UUID, uid: str, reason: BackupReason, *, job_id: UUID | None = None) -> TagBackup`. It raises `BackupMismatchError(ValueError)` (`code = "backup_mismatch"`) unless the backup exists, `clean_tag_uid(uid)` equals its `uid`, `now() - at <= BACKUP_TTL`, `reason` matches, and (for `agent_write`) `job_id` matches.
- Produces (routes):
  - `POST /api/v1/filament/tags/backups`, body `TagBackupIn`, answer 201 `{"backup_id": UUID}` (`model TagBackupMade`). It answers 422 `no_uid`, and 409 on a key clash. An `agent_write` with no `job_id` is 422 (model validator). Task 8 adds the job check.
  - `GET /api/v1/filament/tags/{tag_id}/backups`, answer `list[TagBackup]`, 404 for no such tag.
- Produces (agent): tool `filament_tag_backups` (`risk: 'read'`, input `{ tag_id: uuid }`, route `GET /api/v1/filament/tags/{tag_id}/backups`).

- [ ] **Step 1: Write the migration** `<ts>_filament_tag_backups.sql`:

```sql
-- #7273 G7: what a tag held just before each write. One row per write, never updated or
-- deleted; a spool delete or an unbind keeps them. `job_id` names the agent's NFC job
-- (filament_nfc_jobs, a later migration) for an agent's write, as a plain uuid.
CREATE TABLE filament_tag_backups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tag_id uuid NOT NULL REFERENCES filament_tags (id),
    uid text NOT NULL,
    records jsonb NOT NULL,
    reason text NOT NULL CHECK (reason IN ('spool_write', 'agent_write', 'restore')),
    job_id uuid,
    actor text NOT NULL,
    at timestamptz NOT NULL DEFAULT now(),
    CHECK ((reason = 'agent_write') = (job_id IS NOT NULL))
);
CREATE INDEX filament_tag_backups_tag_at ON filament_tag_backups (tag_id, at DESC);
CREATE FUNCTION filament_tag_backups_keep() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'filament_tag_backups rows are never changed or deleted';
END $$;
CREATE TRIGGER filament_tag_backups_keep BEFORE UPDATE OR DELETE ON filament_tag_backups
    FOR EACH ROW EXECUTE FUNCTION filament_tag_backups_keep();
```

- [ ] **Step 2: Write the failing store tests** in `tests/filament/test_tags.py`:

```python
def test_a_backup_makes_the_tag_and_keeps_the_records(pg_pool):
    made = tags.back_up(store, TagBackupIn(uid="04:aa", records=[REC], reason="spool_write"), actor="ann")
    assert made.uid == "04AA" and made.records == [REC] and made.actor == "ann"
    assert [b.id for b in tags.backups(store, made.tag_id)] == [made.id]

def test_a_backup_with_no_uid_is_refused(pg_pool):
    with pytest.raises(tags.NoUidError):
        tags.back_up(store, TagBackupIn(uid="::", records=[], reason="spool_write"), actor="ann")

def test_backups_are_never_changed_or_deleted(pg_pool):
    made = tags.back_up(store, TagBackupIn(uid="04AA", records=[], reason="spool_write"), actor="ann")
    with pg_pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM filament_tag_backups WHERE id = %s", (made.id,))

def test_every_write_keeps_its_own_backup_newest_first(pg_pool):
    a = tags.back_up(store, TagBackupIn(uid="04AA", records=[], reason="spool_write"), actor="ann")
    b = tags.back_up(store, TagBackupIn(uid="04AA", records=[REC], reason="restore"), actor="ann")
    assert [x.id for x in tags.backups(store, a.tag_id)] == [b.id, a.id]

def test_unbinding_keeps_the_backups(pg_pool): ...  # back_up, bind, unbind_tag: backups still listed

def test_valid_backup_refuses_another_uid_an_old_backup_and_another_reason(pg_pool):
    made = tags.back_up(...uid="04AA", reason="spool_write"...)
    with store.transaction() as conn:
        assert tags.valid_backup(conn, made.id, "04:aa", "spool_write").id == made.id
        for uid, reason in (("04BB", "spool_write"), ("04AA", "restore")):
            with pytest.raises(tags.BackupMismatchError):
                tags.valid_backup(conn, made.id, uid, reason)
    # age: insert a row with at = now() - interval '11 minutes' through SQL, expect BackupMismatchError
```

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tags.py -n 4 -q`
Expected: FAIL.

- [ ] **Step 4: Implement** the store functions, then run the store tests again (PASS).

- [ ] **Step 5: Write the failing route tests** in `tests/api/test_filament_tags.py`: `test_a_backup_answers_its_id_and_lists`, `test_a_backup_with_no_uid_is_422_no_uid` (body `{"uid": "", …}` and `{"uid": "::", …}`), `test_backups_of_an_unknown_tag_are_404`, `test_an_agent_write_backup_needs_a_job_id` (422).

- [ ] **Step 6: Add the routes and the agent tool.** The tool's description: "List what a tag held before each write, newest first: its UID, its NDEF records (record type, media type, data as hex), why it was written (`spool_write`, `agent_write`, `restore`), who wrote it and when. Reads ScadBuddy only." Add `'POST /api/v1/filament/tags/backups'` to `NOT_A_TOOL` with the reason "The phone page backs a tag up right before it writes, in the same tap (#7273 G7). An agent's write is backed up by the page when it runs the nfc_write job." In `agent/test/filament.test.ts`, test that `filament_tag_backups` is `read` tier and not gated, and that it GETs the tag's path (msw, as `test/filament.test.ts:964-975` does).

- [ ] **Step 7: Run them**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py -q && uv run --frozen ruff check . && uv run --frozen mypy`
Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/coverage.test.ts test/filament.test.ts`
Expected: PASS.

- [ ] **Step 8: Commit** with the title `feat(filament): back up every tag before a write (#7273)`.

---

### Task 4: The write and verify routes, and restore

**Files:**
- Modify: `backend/scadbuddy/filament/tags.py`, `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/api/filament.py`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/filament/test_tags.py`, `backend/tests/api/test_filament_tags.py`

**Interfaces:**
- Consumes: `valid_backup`, `BackupMismatchError`, `back_up` (Task 3); `encode`, `pick_format`, `facts_of`, `format_of`, `KIND_OF`, `ORIGIN_OF` (Task 2); `read_on` (Task 1); `Settings.filament_tag_format`.
- Produces (models):
  - `class TagRecordsIn(_In)`: `uid: str`, `records: list[NdefRecord]` (what the tag holds now), `backup_id: UUID`.
  - `class TagRecords(BaseModel)`: `format: TagFormat | None` (`None` for a restore), `records: list[NdefRecord]`, `warnings: list[str] = []`.
  - `class TagWrittenIn(_In)`: `uid: str`, `backup_id: UUID`, `sent: list[NdefRecord]`, `read_back: list[NdefRecord]`.
- Produces (`tags.py`):
  - `records_for_spool(spools, spool_id, body: TagRecordsIn, *, default: TagFormat, empty_weight_g: Callable[[UUID], int | None]) -> TagRecords | None`. It answers `None` for no such spool. It checks `valid_backup(conn, body.backup_id, body.uid, "spool_write")`, then `encode(pick_format(body.records, default), facts_of(spool, empty_weight_g(spool.effective.insert_type_id)))`.
  - `same_records(a: Sequence[NdefRecord], b: Sequence[NdefRecord]) -> bool` (Global Constraints).
  - `ReadBackMismatchError(ValueError)` (`code = "read_back_mismatch"`), `UnknownFormatError(ValueError)` (`code = "unknown_format"`: `sent` is not a single OpenSpool or OpenTag3D record).
  - `written(spools, spool_id, body: TagWrittenIn, *, actor: str, defaults) -> TagReadResult | None`. In one transaction it runs `valid_backup(..., "spool_write")`, then `same_records(sent, read_back)`, then `format_of(sent)`. Then it calls `read_on(conn, TagRead(source="phone", uid=body.uid, records=tuple(read_back)), actor=actor, bind_spool_id=spool_id, defaults=defaults)`, then `FilamentSpoolStore.set_origin(conn, spool_id, actor, data_origin=ORIGIN_OF[fmt], tag_encoded_at=now)` (R11).
  - `restore_records(spools, tag_id, backup_id, body: TagRecordsIn) -> TagRecords | None`. It checks `valid_backup(conn, body.backup_id, body.uid, "restore")`, checks that the old backup's `tag_id` equals `tag_id` and that the fresh backup's tag is the same tag, and answers the old backup's records (`format` is `format_of(records)`).
- Produces (routes):
  - `POST /api/v1/filament/spools/{spool_id}/tag-records` → `TagRecords`. It answers 404 for no spool and 409 `backup_mismatch`. The default format comes from `store.load().filament_tag_format`. The empty weight is the insert type's `empty_weight_g` (`models.py:295-296`), read through `filament.catalog.get("insert-types", id)`.
  - `POST /api/v1/filament/spools/{spool_id}/tag-written` → `TagReadResult`. It answers 404, 409 `backup_mismatch`, 409 `read_back_mismatch` with detail `"the tag does not hold what was sent"`, and 422 `unknown_format`. After the bind it calls `_push_now`.
  - `POST /api/v1/filament/tags/{tag_id}/backups/{backup_id}/restore` → `TagRecords`. It answers 404 for no tag or backup, and 409 `backup_mismatch`.

- [ ] **Step 1: Write the failing route tests** in `tests/api/test_filament_tags.py`:

```python
def _backup(client, uid="04AA", records=(), reason="spool_write"):
    got = client.post("/api/v1/filament/tags/backups", json={"uid": uid, "records": list(records), "reason": reason})
    assert got.status_code == 201, got.text
    return got.json()["backup_id"]

def test_a_blank_tag_gets_the_default_format(client):
    spool = _spool(client)
    got = client.post(f"{SPOOLS}/{spool['id']}/tag-records", json={"uid": "04AA", "records": [], "backup_id": _backup(client)})
    assert got.status_code == 200 and got.json()["format"] == "openspool" and len(got.json()["records"]) == 1

def test_the_default_format_follows_the_setting(client):
    client.put("/api/v1/settings", json={"filament_tag_format": "opentag3d"})
    ... assert got.json()["format"] == "opentag3d"

def test_a_tag_keeps_its_own_format(client):
    ot3d = encode("opentag3d", FACTS_FULL).records[0].model_dump()
    ... records=[ot3d] with setting openspool ... assert got.json()["format"] == "opentag3d"

def test_tag_records_without_a_backup_are_refused(client):
    got = client.post(f"{SPOOLS}/{spool['id']}/tag-records", json={"uid": "04AA", "records": []})
    assert got.status_code == 422  # backup_id is required

def test_tag_records_refuse_a_backup_of_another_uid(client):
    got = client.post(..., json={"uid": "04BB", "records": [], "backup_id": _backup(client, uid="04AA")})
    assert got.status_code == 409 and got.json()["code"] == "backup_mismatch"

def test_tag_records_refuse_a_backup_older_than_ten_minutes(client, pg_conninfo):
    # The trigger refuses an UPDATE, so insert the old row directly with at = now() - interval '11 minutes'
    ... assert got.json()["code"] == "backup_mismatch"

def test_a_checked_write_binds_the_tag_and_marks_the_origin(client):
    backup = _backup(client)
    sent = client.post(f".../tag-records", ...).json()["records"]
    got = client.post(f"{SPOOLS}/{spool['id']}/tag-written", json={"uid": "04AA", "backup_id": backup, "sent": sent, "read_back": sent})
    assert got.status_code == 200 and got.json()["spool"]["id"] == spool["id"]
    detail = client.get(f"{SPOOLS}/{spool['id']}").json()
    assert detail["data_origin"] == "openspool" and detail["tag_encoded_at"] is not None
    assert got.json()["tag"]["kind"] == "ntag_openspool"

def test_a_read_back_that_differs_binds_nothing(client):
    ... read_back = [{**sent[0], "data_hex": sent[0]["data_hex"][:-2] + "00"}]
    assert got.status_code == 409 and got.json()["code"] == "read_back_mismatch"
    assert client.get(f"{SPOOLS}/{spool['id']}").json()["tag"] is None

def test_a_read_back_from_another_uid_binds_nothing(client):
    ... uid "04BB" with the backup of "04AA": 409 backup_mismatch, no binding

def test_a_restore_answers_the_old_records(client):
    old = _backup(client, records=[REC])
    tag_id = client.post("/api/v1/filament/tags/reads", json={"uid": "04AA", "records": []}).json()["tag"]["id"]
    fresh = _backup(client, records=[], reason="restore")
    got = client.post(f"/api/v1/filament/tags/{tag_id}/backups/{old}/restore", json={"uid": "04AA", "records": [], "backup_id": fresh})
    assert got.status_code == 200 and got.json()["records"] == [REC]

def test_a_restore_needs_its_own_fresh_backup(client):
    ... backup_id=old (reason spool_write): 409 backup_mismatch
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py -q`
Expected: FAIL.

- [ ] **Step 3: Implement** the store functions and the routes. Each route's description says what it refuses and why. For `tag-records`, add "Answers the records only. The browser writes them, and a write that skips this route has no backup."

- [ ] **Step 4: Add the three coverage entries** to `NOT_A_TOOL`. All three share one reason: "One step of the phone page's write in one tap (#7273 G7): read and back up, the records, the write, the read-back check. The browser holds the tag, so an agent writes through nfc_write, which the page runs."

- [ ] **Step 5: Run them**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py -q && uv run --frozen pytest tests/filament/test_tags.py -n 4 -q && uv run --frozen ruff check . && uv run --frozen mypy`
Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/coverage.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit** with the title `feat(filament): tag records, the read-back check and restore (#7273)`.

---

### Task 5: The `/filament/scan` page — read, the spool card, new tags, photos, weight, and the unsupported screen

Load `frontend-design:frontend-design` first. The page is used on a phone at about 390 px wide, one-handed. The Scan button is large and sits within thumb reach.

**Files:**
- Create: `frontend/src/lib/webNfc.ts`, `frontend/src/lib/webNfc.test.ts`, `frontend/src/test/ndefStub.ts`
- Create: `frontend/src/pages/filament/ScanPage.tsx`, `frontend/src/pages/filament/ScanPage.test.tsx`
- Create: `frontend/src/components/filament/scan/KnownTag.tsx`, `NewTag.tsx`, `PhotoButtons.tsx`, `WeightField.tsx` (each with a `.test.tsx`)
- Create: `frontend/src/mocks/features/filament/tags.ts`
- Modify: `frontend/src/api/filament.ts` (after `bindTag`, `:200-202`), `frontend/src/App.tsx:121-138`, `frontend/src/lib/filamentLinks.ts:33`, `frontend/e2e/page-titles.spec.ts` (`PAGES`)

**Interfaces:**
- Consumes: `POST /tags/reads` (Task 1); `decoded` and `draft` (Task 2); `createSpool` (`api/filament.ts:167`), `addSpoolEvent` (`:308`), `listCatalog('insert-types')`, `listSpools`, and `entityApi.addAttachment` (`components/entities/api.ts:33`).
- Produces (`lib/webNfc.ts`):
  - `type WireRecord = { record_type: string; media_type?: string | null; id?: string | null; encoding?: string | null; lang?: string | null; data_hex: string }` (the shape of `Schemas['NdefRecord']`; use that type).
  - `type Reading = { uid: string; records: WireRecord[] }`.
  - `nfcSupport(): 'ok' | 'no_api' | 'embedded'`. It answers `embedded` when `isEmbedded()` is true (`lib/embed.ts:7`), `no_api` when `!('NDEFReader' in window)`, and `ok` otherwise.
  - `toWire(record: NDEFRecord): WireRecord` (the bytes of `data` as lower-case hex; `data_hex: ''` when `data` is null). `toInit(record: WireRecord): NDEFRecordInit` (hex to `Uint8Array`, the other fields copied).
  - `sameRecords(a: WireRecord[], b: WireRecord[]): boolean` (Global Constraints).
  - `class NfcSession { start(): Promise<void>; stop(): void; readonly active: boolean; onReading(listener: (r: Reading) => void): () => void; nextReading(signal?: AbortSignal): Promise<Reading>; write(records: WireRecord[], signal?: AbortSignal): Promise<void> }`. There is one `NDEFReader` per session. `start` calls `scan()` once, and the reader stays on until `stop` (screen 1). `write` passes `{ overwrite: true, signal }`. It never calls `makeReadOnly`.
  - Local TypeScript declarations for `NDEFReader`, `NDEFReadingEvent`, `NDEFRecord` and `NDEFRecordInit`, taken from the Web NFC IDL (the DOM lib has none).
- Produces (`test/ndefStub.ts`): `installNdefStub(): NdefStub`, where `NdefStub = { tap(reading: { serialNumber: string; records: Array<{ recordType: string; mediaType?: string; data: Uint8Array }> }): void; writes: NDEFRecordInit[][]; failNextWrite(error: DOMException): void; failScan(error: DOMException): void; restore(): void }`. It sets `window.NDEFReader` to a class whose `scan()` resolves (or rejects once after `failScan`). Its `write()` records the records, or rejects once after `failNextWrite`. Its `addEventListener('reading', …)` and `onreading` get a `tap`.
- Produces (`api/filament.ts`): `postTagRead(body: Schemas['TagReadIn']) => Promise<Schemas['TagReadResult']>` and the type exports `TagReadResult`, `NdefRecord`, `DecodedSpool`.
- Produces (page): the route `filament/scan`, the link `filamentLinks.scan = '/filament/scan'`, and `<h1>Scan a tag</h1>`.

**Behaviour:**
- **Screen 4.** When `nfcSupport()` is not `ok`, the page shows "Open this page in Chrome on Android". For `embedded`, it also says "in its own tab, not inside Bambuddy". Below that is the page's own URL (`window.location.href`) in a read-only field with a "Copy" button (`navigator.clipboard.writeText`).
- **Screen 1.** "Scan" starts the session. The button then reads "Scanning — hold a tag to the phone" and stays on. A `NotAllowedError` from `scan()` shows "Allow NFC for this site in Chrome's settings, then tap Scan again." Each reading posts `postTagRead({ uid, tag_type: null, records })`. The answer goes at the top of "This session's reads": the time, the UID and the spool number or "New tag". Tapping a read opens it.
- **Screen 2** (`next === 'open_spool'`). `KnownTag` shows the spool card (number, colour swatch, material, brand, grams left). Then `PhotoButtons`, then `WeightField`. Then "Open spool" (a link to `filamentLinks.spool(id)`), and a disabled "Write tag" that Task 6 enables.
- **Screen 3** (`next === 'bind_or_create'`). `NewTag` shows the decoded fields that are set (format, material, brand, colour, temperatures, weight), then `PhotoButtons` in "held" mode. With a `draft`, "Create a spool from this tag" calls `createSpool(draft)`, then `postTagRead({ uid, records, bind_spool_id: made.id })`. Without one, a link "Make the spool by hand" goes to `filamentLinks.newSpool`. "Bind to a spool" is a search box over `listSpools()`, matching the number, material, brand or colour name. Choosing a spool posts `postTagRead({ uid, records, bind_spool_id })`. After either, the page shows Screen 2 for that read.
- **`next === 'none'`** (no UID). It shows the decoded data and "This tag gave no serial number, so ScadBuddy cannot keep it."
- **`PhotoButtons`** has two buttons, "Filament label" and "Spool label". Each is a `<label>` around a hidden `<input type="file" accept="image/*" capture="environment">`. A spool's photo goes to `entityApi.addAttachment(\`spool:${id}\`, file, { role })`. A filament-label photo also goes to `spool_type:${type_id}` when the spool has a `type_id` (G6). In "held" mode (new tag), files are kept in state, and shown as thumbnails through `URL.createObjectURL`. They are sent once a spool id exists. Each button shows "Saved" after the upload. A failed upload keeps the file and shows "Not saved. Try again."
- **`WeightField`** has a field labelled "Gross weight (g)". With the spool's effective `insert_type_id` and that insert type's `empty_weight_g`, it shows `gross − empty = net` ("1 243 g − 243 g empty spool = 1 000 g of filament"). Without them, the field is labelled "Filament weight (g)" and its value is the net. "Save weight" posts `addSpoolEvent(id, { kind: 'weighed', source: 'person', grams: net })` (G5). A net below 0 is refused with "The weight is less than the empty spool."
- Unmounting the page stops the session.

- [ ] **Step 1: Write the stub and the failing `webNfc` tests** (`lib/webNfc.test.ts`): `reports no_api without NDEFReader`, `reports embedded inside a frame` (mock `isEmbedded`), `turns a record into hex and back` (`toInit(toWire(r))` gives the same bytes), `compares records by type, media type and data`, `keeps one scan running and hands each tap to listeners`, `writes with overwrite and never makes a tag read-only` (expect the stub to have no `makeReadOnly` call, as a spy).

- [ ] **Step 2: Run them and see them fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/lib/webNfc.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** `lib/webNfc.ts`, then run its tests (PASS).

- [ ] **Step 4: Write the mocks** `mocks/features/filament/tags.ts` (export `handlers` and `reset`). `POST /api/v1/filament/tags/reads` resolves the UID against the seeded tags, which `index.ts` owns. Read them through an exported helper there rather than copying the seed. It answers `next` the way the backend does, and with `bind_spool_id` it binds through the same helper the `POST /spools/:id/tags` mock uses. A read whose records hold an OpenSpool JSON answers `decoded` with the format, the material and the brand, and a `draft` when the material name is a seeded material.

- [ ] **Step 5: Write the failing page and component tests** (`ScanPage.test.tsx`, with `installNdefStub()` in `beforeEach` and `restore()` in `afterEach`):

```tsx
it('asks for Chrome on Android with a copyable link when Web NFC is missing', ...)   // no stub: text + 'Copy' writes location.href
it('says to open it in its own tab inside a frame', ...)
it('starts one scan and lists each read', async () => { ...click 'Scan'; stub.tap({serialNumber: '04:a2:..', records: []}); expect list item 'New tag' })
it('opens a known tag on its spool card', ...)   // tap a seeded tag uid: shows 'Spool #<n>', 'Open spool' link to filamentLinks.spool(id)
it('creates a spool from a new tag's draft and binds it', ...)  // records = OpenSpool PLA; click 'Create a spool from this tag'; expect POST /spools then POST /tags/reads with bind_spool_id
it('binds a new tag to a searched spool', ...)
it('says why NFC was refused', ...)  // stub.failScan(new DOMException('', 'NotAllowedError'))
it('stops scanning when it unmounts', ...)
```

`PhotoButtons.test.tsx`: `saves a filament label on the spool and its type` (expect two uploads with role `filament_label` to `spool:<id>` and `spool_type:<type_id>`), `saves a spool label on the spool only`, `holds photos of a new tag until a spool exists`, `keeps a photo that failed to upload`. Check in each that the upload body is `multipart/form-data` with a `File`, never base64. Use `userEvent.upload` on the input.

`WeightField.test.tsx`: `subtracts the empty spool from the gross weight` (insert type 243 g, gross 1243 → posts `grams: 1000`, `source: 'person'`), `takes the filament weight when the empty spool is unknown`, `refuses a weight below the empty spool`.

- [ ] **Step 6: Run them and see them fail**

Run: `cd frontend && pnpm exec vitest run src/pages/filament/ScanPage.test.tsx src/components/filament/scan`
Expected: FAIL.

- [ ] **Step 7: Implement** the page, the components, the API calls, the route and the link. Add `'/filament/scan'` to `PAGES` in `e2e/page-titles.spec.ts`.

- [ ] **Step 8: Run the tests, lint, typecheck and the page-title e2e**

Run: `cd frontend && pnpm exec vitest run src/lib/webNfc.test.ts src/pages/filament/ScanPage.test.tsx src/components/filament/scan && pnpm lint && pnpm typecheck && pnpm exec playwright test e2e/page-titles.spec.ts`
Expected: PASS.

- [ ] **Step 9: Commit** with the title `feat(frontend): the /filament/scan phone page reads tags through Web NFC (#7273)`.

---

### Task 6: Write a spool's tag in one tap, the result screen, and restore

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/lib/tagWrite.ts`, `frontend/src/lib/tagWrite.test.ts`
- Create: `frontend/src/components/filament/scan/WriteTag.tsx`, `TagBackups.tsx` (each with a `.test.tsx`)
- Modify: `frontend/src/components/filament/scan/KnownTag.tsx` (enable "Write tag", and show `TagBackups`), `frontend/src/api/filament.ts`, `frontend/src/mocks/features/filament/tags.ts`, `frontend/src/components/filament/TagPanel.tsx` (a link "Read or write with a phone" to `filamentLinks.scan`)

**Interfaces:**
- Consumes: `NfcSession`, `sameRecords`, `toWire` (Task 5); `POST /tags/backups`, `GET /tags/{id}/backups` (Task 3); `tag-records`, `tag-written`, `restore` (Task 4).
- Produces (`api/filament.ts`): `createTagBackup(body) => Promise<{ backup_id: string }>`, `listTagBackups(tagId) => Promise<TagBackup[]>`, `spoolTagRecords(spoolId, body) => Promise<TagRecords>`, `spoolTagWritten(spoolId, body) => Promise<TagReadResult>`, `restoreTagBackup(tagId, backupId, body) => Promise<TagRecords>`.
- Produces (`lib/tagWrite.ts`):
  - `type WriteStep = 'backing_up' | 'writing' | 'reading_back' | 'tap_again'`.
  - `type WriteOutcome = { status: 'checked'; result: TagReadResult | null } | { status: 'mismatch'; backupId: string } | { status: 'other_tag'; backupId: string } | { status: 'failed'; message: string; backupId: string | null }`.
  - `writeWithBackup(options: { session: NfcSession; expectUid?: string; reason: 'spool_write' | 'agent_write' | 'restore'; jobId?: string; prepare: (now: Reading, backupId: string) => Promise<WireRecord[]>; confirm: (now: Reading, backupId: string, sent: WireRecord[], readBack: Reading) => Promise<TagReadResult | null>; onStep: (step: WriteStep) => void; signal?: AbortSignal }): Promise<WriteOutcome>`.
  - The order is fixed (G7). 1) `nextReading()`. An empty UID gives `failed` with "This tag gives no serial number, so it cannot be backed up. Nothing was written." 2) `createTagBackup`. On failure, give `failed` with `backupId: null` and write nothing. 3) `prepare`. 4) `session.write(sent)`. 5) `nextReading()` again. When none comes within 3 s, call `onStep('tap_again')` and keep waiting. 6) A different UID gives `other_tag`. Records that are not `sameRecords` give `mismatch`. Otherwise call `confirm` and give `checked`. A 409 `read_back_mismatch` from `confirm` gives `mismatch`.

**Behaviour:**
- **Screen 5.** "Write tag" opens `WriteTag` with three steps: "Read and back up", "Write", "Read back". Each step is ticked as `onStep` moves on. With `tap_again`, step 3 reads "Lift the tag and tap it again to check it." There is no format picker. After the records come back, the format and any `warnings` show in step 2 ("Written as OpenSpool"; "Missing on the spool: density, weight").
- **Screen 6, `checked`.** "Written and checked." The card reloads from `result`.
- **Screen 6, `mismatch`.** "The tag does not hold what was sent." It offers "Restore from the backup", which runs the restore flow with that `backupId`'s records.
- **Screen 6, `other_tag`.** "A different tag answered. Nothing was bound."
- **Screen 6, `failed`.** The message, plus "The backup is kept." when `backupId` is set.
- **`TagBackups`** is a disclosure "Backups (n)" on `KnownTag`. It lists `listTagBackups(tag.id)`: the time, the reason in words ("before a spool write", "before an agent write", "before a restore"), the actor, and the record count. Each row has "Restore". Restore runs `writeWithBackup({ reason: 'restore', prepare: (now, fresh) => restoreTagBackup(tag.id, row.id, { uid: now.uid, records: now.records, backup_id: fresh }).then(r => r.records), confirm: async (_n, _b, _s, back) => { await postTagRead({ uid: back.uid, records: back.records }); return null } })`. The page itself checks with `sameRecords` (R6).

- [ ] **Step 1: Write the failing `tagWrite` tests** (`lib/tagWrite.test.ts`, with the stub and msw):

```ts
it('backs up before it writes, then checks the read-back')            // order of calls: POST backups, POST tag-records, stub write, POST tag-written
it('writes nothing when the backup fails')                            // backups → 500: stub.writes is empty, outcome failed, backupId null
it('writes nothing to a tag with no serial number')                   // serialNumber '' → failed, no POST backups
it('says a different tag answered and binds nothing')                 // second tap other uid → other_tag, no POST tag-written
it('reports a read-back that differs as a mismatch')                  // second tap different data → mismatch
it('asks for a second tap when no read-back comes')                   // fake timers: 3 s → onStep('tap_again'); later tap → checked
it("shows the browser's write error and keeps the backup")            // stub.failNextWrite(new DOMException('x', 'NetworkError')) → failed with backupId
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd frontend && pnpm exec vitest run src/lib/tagWrite.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** `tagWrite.ts`, the API calls and the mocks. The mocks keep backups per tag in memory. `tag-records` answers one OpenSpool record built from the seeded spool. `tag-written` binds and answers `TagReadResult`. Then run the `tagWrite` tests (PASS).

- [ ] **Step 4: Write the failing component tests.** `WriteTag.test.tsx`: `walks the three steps on one tap and says written and checked`, `offers restore after a mismatch`, `shows the missing fields the format named`. `TagBackups.test.tsx`: `lists backups newest first with their reason`, `restores a backup with a fresh backup first` (expect POST backups with `reason: 'restore'` before the restore POST, then a stub write of the old records). `ScanPage.test.tsx`: `writes the tag of a known spool`.

- [ ] **Step 5: Implement**, then run

Run: `cd frontend && pnpm exec vitest run src/lib src/components/filament/scan src/pages/filament/ScanPage.test.tsx src/components/filament/TagPanel.test.tsx && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 6: Commit** with the title `feat(frontend): write a spool's tag with a backup and a read-back check (#7273)`.

---

### Task 7: AMS capture through the same read function

**Files:**
- Modify: `backend/scadbuddy/filament/capture.py:249-338` (`reconcile`)
- Test: `backend/tests/filament/test_capture.py`

**Interfaces:**
- Consumes: `tags.seen_on`, `TagRead` (Task 1). `SeenTray` has `tag_uid`, `tray_uuid`, `ams_id` and `tray_id` (`spools/models.py:59`).
- Produces: in `reconcile`, for each filled tray (`seen is not None`) whose `tray_uuid` or `tag_uid` is set, the holder comes from `tags.seen_on(conn, TagRead(source="ams", uid=seen.tag_uid, tray_uuid=seen.tray_uuid, printer_id=printer, ams_id=ams_id, tray_id=tray_id))`. That replaces the tag half of `FilamentInventory.holder_on`. When it finds no holder, `holder_on` runs as before for the placed-spool (`"link"`) half. `found_by` is `"tag"` for a holder from `seen_on`. `touched` (`capture.py:142-169`) still calls `holder_on`, which is lookup only, so taking the locks writes nothing.

- [ ] **Step 1: Write the failing tests** in `tests/filament/test_capture.py`. Use its helpers `_spool`, `_printer`, `_tray`, `_report` and `_reconcile` (`:39-84`).

```python
def test_a_tagged_tray_marks_its_tag_seen_by_the_ams(pg_pool):
    spool = _spool(pg_pool, tray_uuid=UUID_A)
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, UUID_A)))
    assert _tag_row(pg_pool, UUID_A)["last_seen_source"] == "ams"

def test_an_unknown_ams_tag_makes_no_tag_row(pg_pool):
    pid, _ = _printer(pg_pool)
    _reconcile(pg_pool, pid, _report(_tray(1, UUID_B)))
    assert _count_tags(pg_pool) == 0

def test_a_resent_report_does_not_touch_the_tag_again(pg_pool):
    # reconcile through PrinterReportStore.write twice with the same report: last_seen_at unchanged,
    # because trays_changed is false for the second (capture.py:130).
```

- [ ] **Step 2: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_capture.py -n 4 -q`
Expected: FAIL.

- [ ] **Step 3: Implement**, then run the capture, tray and inventory tests

Run: `cd backend && uv run --frozen pytest tests/filament/test_capture.py tests/filament/test_tray_reconcile.py tests/filament/test_inventory_trays.py tests/filament/test_ams_sync.py tests/filament/test_tags.py -n 4 -q && uv run --frozen ruff check . && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 4: Commit** with the title `feat(filament): AMS tag readings go through the one read function (#7273)`.

---

### Task 8: NFC jobs (backend) and the agent tools `nfc_read` and `nfc_write`

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_nfc_jobs.sql`
- Modify: `backend/scadbuddy/filament/tags.py` (the job functions), `backend/scadbuddy/filament/models.py`, `backend/scadbuddy/api/filament.py`
- Create: `agent/src/tools/nfc.ts`, `agent/test/nfc.test.ts`
- Modify: `agent/src/tools/index.ts:48-75` (register `nfcTools`), `agent/src/tools/coverage.ts`, `agent/src/sessions/touched.ts:703` (`TOUCHES_NOTHING`)
- Test: `backend/tests/filament/test_tags.py`, `backend/tests/api/test_filament_tags.py`

**Interfaces:**
- Consumes: `read_on`, `back_up`, `valid_backup`, `same_records` (Tasks 1, 3, 4).
- Produces (models):
  - `NfcJobKind = Literal["read", "write"]`, `NfcJobState = Literal["pending", "armed", "done", "failed", "expired"]`.
  - `class NfcJobIn(_In)`: `kind: NfcJobKind`, `records: list[NdefRecord] | None` (required for `write`, refused for `read`, 888 bytes at most), `ttl_s: int = Field(600, ge=1, le=600)` (R7).
  - `class NfcJob(BaseModel)`: `id`, `kind`, `records`, `created_by: str`, `state`, `result: dict[str, Any] | None`, `created_at`, `expires_at`, `armed_by: str | None`.
  - `class NfcJobResultIn(_In)`: `uid: str | None`, `records: list[NdefRecord] = []` (for a write, the read-back), `backup_id: UUID | None`, `error: str | None` (≤500). An `error` means a failed job and nothing else is read.
- Produces (`tags.py`):
  - `expire_jobs(conn) -> None`: `UPDATE … SET state = 'expired' WHERE state IN ('pending', 'armed') AND expires_at <= now()`. It runs first in every job function.
  - `create_job(spools, body: NfcJobIn, *, actor) -> NfcJob`, `pending_jobs(spools) -> list[NfcJob]` (pending, oldest first), `get_job(spools, job_id) -> NfcJob | None`.
  - `claim_job(spools, job_id, *, actor) -> NfcJob`. It moves the job from `pending` to `armed` and sets `armed_by`. Any other state raises `JobNotPendingError` (`code = "job_not_pending"`).
  - `finish_job(spools, job_id, body: NfcJobResultIn, *, actor, defaults) -> NfcJob`. Only an `armed` job can finish (`JobNotArmedError`, `code = "job_not_armed"`).
    - With `error`, the job goes to `failed` with `result = {"error": error}`.
    - For a `read`, it calls `read_on(conn, TagRead(source="agent", uid=body.uid, records=tuple(body.records)), actor=actor, defaults=defaults)`. The job goes to `done` with `result` = that `TagReadResult` as JSON, plus the read's `uid` and `records`.
    - For a `write`, it calls `valid_backup(conn, body.backup_id, body.uid, "agent_write", job_id=job_id)`. Then `same_records(job.records, body.records)` decides. If they match, the job goes to `done` with `result = {"uid", "records", "backup_id"}`, and it calls `seen_on(…source="agent"…)`. If not, it goes to `failed` with `result = {"error": "the tag does not hold what was sent", "uid", "records", "backup_id"}`.
  - `back_up` with `reason="agent_write"` now also checks that the job exists, is `write` and is `armed`. Otherwise it raises `JobNotArmedError`.
- Produces (routes, all under `/api/v1/filament`):
  - `POST /nfc-jobs` → 201 `NfcJob`.
  - `GET /nfc-jobs?state=pending` → `list[NfcJob]`. Only `pending` is accepted; anything else is 422.
  - `GET /nfc-jobs/{job_id}` → `NfcJob`, or 404.
  - `POST /nfc-jobs/{job_id}/claim` → `NfcJob`, 404, or 409 `job_not_pending`.
  - `POST /nfc-jobs/{job_id}/result` → `NfcJob`, 404, 409 `job_not_armed`, or 409 `backup_mismatch`.
- Produces (agent, `tools/nfc.ts`):
  - `nfc_read`: input `{ timeout_s: int 1..600 = 600 }`, `risk: 'write'` (R8), routes `POST /api/v1/filament/nfc-jobs` and `GET /api/v1/filament/nfc-jobs/{job_id}`, `waitsMs: ({timeout_s}) => timeout_s * 1000`. It creates `{kind:'read', ttl_s: timeout_s}`, then polls `GET` every `ctx.pollIntervalMs` until the state is `done`, `failed` or `expired`, `ctx.signal` aborts, or the deadline passes (the `activity_watch` loop, `tools/activity.ts:95-112`). It answers `json({ job_id, state, result })`, or `json({ job_id, state: 'expired' })`.
  - `nfc_write`: input `{ records: NdefRecord[] (1..16), timeout_s }`, `risk: 'outward'`, `approval: 'none'`, with a comment citing the spec's Constraints ("The person's arm-and-tap on the page is its approval"). It does the same as `nfc_read` with `kind:'write'`.
  - The descriptions say: "The user must have ScadBuddy's /filament/scan page open in Chrome on Android. It shows the job; the user taps Arm and holds a tag. Records are NDEF records with data as hex. The write replaces every record on the tag, after a backup the user can restore." `nfc_read` adds: "Any tag, not only a spool's."
  - Both go in `TOUCHES_NOTHING`, with the comment "#7273: a tag and an NFC job are no recorded resource kind".
  - `NOT_A_TOOL` gets `GET /nfc-jobs` (pending list), `/claim` and `/result`, with the reason: "The phone page's side of an agent's NFC job (#7273 G4): it lists the pending jobs, arms one when the person taps Arm, and posts what the tag held. The agent creates and waits on jobs with nfc_read and nfc_write."

- [ ] **Step 1: Write the migration** `<ts>_filament_nfc_jobs.sql`:

```sql
-- #7273 G4: a tag read or write an agent asked for, run by the phone page when a person arms it.
CREATE TABLE filament_nfc_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind text NOT NULL CHECK (kind IN ('read', 'write')),
    records jsonb,
    created_by text NOT NULL,
    armed_by text,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'armed', 'done', 'failed', 'expired')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    CHECK ((kind = 'write') = (records IS NOT NULL))
);
CREATE INDEX filament_nfc_jobs_open ON filament_nfc_jobs (created_at) WHERE state IN ('pending', 'armed');
```

- [ ] **Step 2: Write the failing store and route tests** (`test_tags.py`, `test_filament_tags.py`):

```python
def test_a_read_job_runs_through_the_read_function(client):
    job = client.post(JOBS, json={"kind": "read"}).json()
    assert job["state"] == "pending" and [j["id"] for j in client.get(JOBS, params={"state": "pending"}).json()] == [job["id"]]
    assert client.post(f"{JOBS}/{job['id']}/claim").json()["state"] == "armed"
    done = client.post(f"{JOBS}/{job['id']}/result", json={"uid": "04AA", "records": []}).json()
    assert done["state"] == "done" and done["result"]["next"] == "bind_or_create"

def test_a_write_job_needs_its_own_agent_write_backup(client): ...   # spool_write backup → 409 backup_mismatch
def test_a_write_job_is_done_only_when_the_read_back_matches(client): ...  # matching → done; other data → failed with the error text
def test_an_agent_write_backup_needs_an_armed_write_job(client): ...  # pending job → 409 job_not_armed
def test_a_job_expires_after_its_ttl(client, pg_conninfo): ...  # set expires_at in the past via SQL; GET → expired; claim → 409 job_not_pending
def test_a_job_is_claimed_once(client): ...  # second claim → 409 job_not_pending
def test_a_read_job_takes_no_records_and_a_write_job_needs_them(client): ...  # 422 each way
def test_a_failed_job_keeps_the_pages_error(client): ...
```

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tags.py -n 4 -q && uv run --frozen pytest tests/api/test_filament_tags.py -q`
Expected: FAIL.

- [ ] **Step 4: Implement** the migration, the store and the routes. Then run the backend tests (PASS).

- [ ] **Step 5: Write the failing agent tests** (`agent/test/nfc.test.ts`, msw as in `test/filament.test.ts`):

```ts
it.each([['nfc_read', 'write', false], ['nfc_write', 'outward', false]])('%s is %s tier, gated: %s', ...)
it('nfc_read creates a read job and answers its result once done')        // GET answers pending, then done
it('nfc_write sends the records and answers expired when the job expires')
it('nfc_write refuses records past 16')
it('stops waiting when the call is aborted')                             // ctx.signal
```

- [ ] **Step 6: Implement** `tools/nfc.ts`, then register it and add the coverage and touched entries.

- [ ] **Step 7: Run them**

Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/nfc.test.ts test/coverage.test.ts test/touched.test.ts && pnpm lint && pnpm typecheck`
Run: `cd backend && uv run --frozen ruff check . && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 8: Commit** with the title `feat(filament): NFC jobs and the agent tools nfc_read and nfc_write (#7273)`.

---

### Task 9: The agent job banner on the scan page

Load `frontend-design:frontend-design` first.

**Files:**
- Create: `frontend/src/components/filament/scan/NfcJobBanner.tsx`, `NfcJobBanner.test.tsx`
- Modify: `frontend/src/pages/filament/ScanPage.tsx`, `frontend/src/api/filament.ts`, `frontend/src/mocks/features/filament/tags.ts`

**Interfaces:**
- Consumes: the job routes (Task 8); `NfcSession`, `writeWithBackup` (Tasks 5 and 6).
- Produces (`api/filament.ts`): `listPendingNfcJobs() => Promise<NfcJob[]>`, `claimNfcJob(id)`, `postNfcJobResult(id, body)`.
- Produces (`NfcJobBanner`): props `{ session: NfcSession }`.
  - It polls `listPendingNfcJobs()` every 2000 ms, only while `document.visibilityState === 'visible'`. It reads once more on `visibilitychange` to visible, and stops the timer while hidden.
  - It shows the oldest job: "Agent wants to read a tag", or "Agent wants to write 2 records". Then come the records (type, media type, bytes), a countdown to `expires_at` (`m:ss`), and "Arm" and "Decline".
  - "Decline" posts `postNfcJobResult(id, { error: 'declined by the person' })`. That route needs an armed job, so Decline claims first, then posts the error.
  - "Arm" claims the job. Then, for a read, it shows "Hold a tag to the phone", takes `session.nextReading()`, and posts `{ uid, records }`. For a write, it runs `writeWithBackup({ reason: 'agent_write', jobId, prepare: async () => job.records, confirm: (_n, backupId, _s, back) => postNfcJobResult(id, { uid: back.uid, records: back.records, backup_id: backupId }).then(() => null) })`.
  - A `failed` outcome posts `{ error: message }`.
  - While a job runs, the page's own read handler ignores readings (one consumer at a time).
  - At 0:00 it shows "Expired" and the banner goes away on the next poll.

- [ ] **Step 1: Write the failing tests**: `shows a pending job with its records and countdown`, `polls every 2 s only while visible` (fake timers, set `document.visibilityState` through `Object.defineProperty`, count the GETs), `arms a read job and posts what the tag held`, `arms a write job, backs up with the job id, writes the records and posts the read-back`, `declines a job`, `posts the browser's error as a failed job`.

- [ ] **Step 2: Run them and see them fail**

Run: `cd frontend && pnpm exec vitest run src/components/filament/scan/NfcJobBanner.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement** the banner, the API calls and the mocks. The mocks keep jobs in memory; export `seedNfcJob` for the tests.

- [ ] **Step 4: Run**

Run: `cd frontend && pnpm exec vitest run src/components/filament/scan src/pages/filament/ScanPage.test.tsx && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit** with the title `feat(frontend): the scan page runs an agent's NFC jobs (#7273)`.

---

### Task 10: Remove the SpoolBuddy tag path

**Files:**
- Delete: `backend/scadbuddy/filament/ntag.py`, `backend/tests/filament/test_ntag.py`, `backend/tests/bambuddy/test_client_scan.py`
- Create: `backend/scadbuddy/migrations/<ts>_drop_filament_ntag_writes.sql`
- Modify: `backend/scadbuddy/api/filament.py` (module docstring lines 1-15, the import at `:65`, and the routes at `:2805-2909`); `backend/scadbuddy/filament/models.py` (`DeviceId`, `ScanRequest`, `ScanResult`, `NtagWrite`, `NtagCancel`, `NtagQueued`, `NtagConfirm` at `:1960-2030`, `RfidStatus.devices` at `:1950`, and the import at `:29`); `backend/scadbuddy/filament/rfid_mode.py:240-252` (no device list); `backend/scadbuddy/bambuddy/client.py` (`next_tag_scan` `:726-776`, `ws_token` `:679-720`, `_ws_url`, `_tag_scan` `:321-345`, the `_WS_*` constants `:315-320`, `queue_tag_write`, `cancel_tag_write`, `spoolbuddy_devices` `:2653-2693`, their `Operation` names `:191`, `:299-302`, and the `websockets` imports `:38-39`); `backend/scadbuddy/bambuddy/models.py` (`TagScan`, `SpoolBuddyDevice` at `:1696-1720`); `backend/pyproject.toml:46-47` (the `websockets` dependency) and `backend/uv.lock`
- Modify: `backend/tests/api/test_filament.py` (`:1394-1560`: the scan and ntag tests), `backend/tests/api/test_filament_sync_nudge.py:14,72`, `backend/tests/bambuddy/test_client_rfid.py` (the write, cancel and device tests)
- Modify: `agent/src/tools/filament.ts:1539-1645` (the 4 tools and `DEVICE_ID` `:37`), `agent/test/filament.test.ts:900-1010`, `agent/src/sessions/touched.ts:782-784`
- Delete: `frontend/src/components/filament/NtagPanel.tsx`, `NtagPanel.test.tsx`
- Modify: `frontend/src/components/filament/TagPanel.tsx` (drop `scanTag`, `SCAN_SECONDS` and the scan button; keep the bind form and the phone link from Task 6), `TagPanel.test.tsx`; `frontend/src/pages/filament/SpoolPage.tsx:51,859`; `frontend/src/api/filament.ts:203-289` (the scan and ntag calls, `SpoolBuddyDevice`); `frontend/src/mocks/features/filament/rfid.ts` (the device list and the scan and ntag handlers) and `index.ts:318-325` (their helpers); `frontend/src/pages/settings/sections/filament.tsx:857-860` (`devices`); `frontend/e2e/filament-rfid.spec.ts:24-62` (the NTAG and scan tests)
- Modify (docs this change makes false): `backend/CLAUDE.md` (the `ntag.py` sentence in the filament layout, and "and so do the NTAG write and cancel routes"); `frontend/CLAUDE.md:300-306` and `:346-348`; `docs/features/filament-parity.md:120-121` (point the rows at `/filament/scan` and `filament/tags.py`); and the `api/filament.py` module docstring.

**Interfaces:**
- Consumes: nothing new.
- Produces: no route under `/api/v1/filament/rfid/scan` or `/api/v1/filament/spools/{id}/ntag/*`; no Bambuddy `spoolbuddy.*` call; no `filament_ntag_writes` table.

- [ ] **Step 1: Write the migration** `<ts>_drop_filament_ntag_writes.sql`:

```sql
-- #7273: the SpoolBuddy NTAG write (filament/ntag.py) is gone; tags are written from the
-- phone page with a backup first (filament_tag_backups).
DROP TABLE filament_ntag_writes;
```

- [ ] **Step 2: Write the failing check tests**:
  - `backend/tests/api/test_filament_tags.py::test_the_spoolbuddy_routes_are_gone`: POST `/api/v1/filament/rfid/scan`, `/spools/{id}/ntag/write`, `/ntag/confirm` and `/ntag/cancel` each answer 404 or 405.
  - `backend/tests/filament/test_migrations.py::test_the_ntag_writes_table_is_dropped`: `to_regclass('filament_ntag_writes') IS NULL`.
  - `backend/tests/bambuddy/test_client_rfid.py::test_the_client_makes_no_spoolbuddy_call`: `not hasattr(BambuddyClient, name)` for `next_tag_scan`, `queue_tag_write`, `cancel_tag_write`, `spoolbuddy_devices` and `ws_token`.
  - `agent/test/filament.test.ts`: `it('has no SpoolBuddy tools')` expects none of `filament_rfid_scan` and `filament_spool_ntag_{write,confirm,cancel}` in `ALL_TOOLS`.

- [ ] **Step 3: Run them and see them fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament_tags.py::test_the_spoolbuddy_routes_are_gone tests/filament/test_migrations.py tests/bambuddy/test_client_rfid.py -q`
Expected: FAIL.

- [ ] **Step 4: Remove** everything in "Files" above, in the order backend, agent, frontend, docs. Find every leftover with `grep -rn "ntag\b\|ntag\.\|NtagPanel\|scanTag\|writeNtag\|confirmNtag\|cancelNtag\|rfid/scan\|ScanResult\|ScanRequest\|SpoolBuddyDevice\|next_tag_scan\|queue_tag_write\|cancel_tag_write\|spoolbuddy_devices\|ws_token\|filament_ntag_writes\|DEVICE_ID" backend/scadbuddy backend/tests agent/src agent/test frontend/src frontend/e2e backend/CLAUDE.md frontend/CLAUDE.md docs/features`. Only the merged migrations and the closed plans under `docs/superpowers/plans/` may still match. Then relock with `cd backend && uvx uv@0.13.9 lock`. `websockets` may stay in `uv.lock` as a dependency of another package; only the direct entry goes.

- [ ] **Step 5: Run every suite this touches**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest tests/filament tests/bambuddy -n 4 -q && uv run --frozen pytest tests/api/test_filament.py tests/api/test_filament_tags.py tests/api/test_filament_sync_nudge.py tests/api/test_settings.py -q`
Run: `cd agent && pnpm gen:api && pnpm exec vitest run test/filament.test.ts test/coverage.test.ts test/touched.test.ts && pnpm lint && pnpm typecheck`
Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/filament src/pages/filament src/pages/settings && pnpm lint && pnpm typecheck && pnpm exec playwright test e2e/filament-rfid.spec.ts e2e/page-titles.spec.ts`
Expected: PASS.

- [ ] **Step 6: Commit** with the title `refactor(filament): remove the SpoolBuddy tag scan and write (#7273)`.

---

## Final check (after Task 10)

- [ ] Run each package's full CI commands once: the backend's (`backend/CLAUDE.md` "Commands", `-n 4`), `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build`, and `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- [ ] `git status` shows no `openapi.json` and no `schema.d.ts`.
- [ ] Remove the test container: `docker rm -f sbtest-7273`.
