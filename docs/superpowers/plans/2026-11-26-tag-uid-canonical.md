# Tag UID canonical form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A phone's 4-byte tag UID and the AMS's `<uid>00000100` name one tag, so a web scan of a Bambu spool finds its spool, and the duplicate rows web scans already made are merged.

**Architecture:** Every lookup, bind and write of a tag cleans its UID through `spools/models.py` `clean_tag_uid` first, so one canonical form there fixes them all with no SQL change. The canonical form of an 8-hex-digit (4-byte) UID is the AMS's: the UID followed by `00000100`. A migration rewrites the rows already stored: a short row whose long twin exists is merged into it, any other short row is re-keyed. The browser's two copies of the rule (`WriteTag.tsx`, the msw mocks) change with it, pinned by one vector file both sides read.

**Tech Stack:** Python 3.12 (psycopg, pytest), SQL migration (Postgres 18), TypeScript (vitest).

**Spec:** `docs/superpowers/specs/2026-11-26-bambu-tag-scan-design.md`, "Tag identity (G3), first". This plan is its first part. The multi-UID table (`filament_tag_uids`) and "same spool as…" ship with the native read, which fills the table from block 9 (spec updated in Task 3).

## Global Constraints

- The suffix is exactly `00000100`, appended to an 8-hex-digit UID after upper-casing. Measured: all 38 `bambu_mifare` rows on prod end `00000100`, and 6 of 6 phone and Spool Scanner UIDs match their AMS row in that form (spec, "Measured and sourced facts").
- A UID that is none or all zeros stays `None`; the zero check runs before the suffix is added.
- Any other length keeps today's rule: hex digits only, the last 16, upper case.
- New migration file only, named `$(date -u +%Y%m%dT%H%MZ)_filament_tag_uid_canonical.sql`; no merged file is edited (root CLAUDE.md "Migrations (#491)").
- No new route, no new Python module (#7667).
- Conventional-commit PR title, body `Fixes #<issue>` (the issue Task 3 files), sources cited (#259).

## Review Focus

- A UID of `00000000` (an empty read): must stay `None`, never `0000000000000100`. Vector in Task 1.
- A UID typed or read with separators or lower case (`7e:74:82:bb`): must clean to `7E7482BB00000100`. Vector in Task 1.
- A short row and its long twin both holding an open binding: the migration must not break the one-open-binding-per-tag index or drop a binding; it leaves both rows and raises a notice. Test in Task 2.
- A short row bound to a spool while its long twin is unbound: the binding moves to the long row and the spool still opens on a scan. Test in Task 2.
- Running the migration on a database with nothing to fix, or twice: no change. Test in Task 2.

---

### Task 1: The canonical UID rule, on both sides

**Files:**
- Create: `test-fixtures/filament/tag-uid-vectors.json`
- Modify: `backend/scadbuddy/spools/models.py:40-42` (`clean_tag_uid`)
- Create: `backend/tests/filament/test_tag_uid_vectors.py`
- Modify: `backend/tests/filament/test_tags.py` (one read test)
- Create: `frontend/src/lib/tagUid.ts`
- Modify: `frontend/src/components/filament/scan/WriteTag.tsx:231-232` (`cleanUid` goes; it imports `cleanTagUid`)
- Modify: `frontend/src/mocks/features/filament/index.ts:2023-2024` (`cleanTagUid`)
- Create: `frontend/src/lib/tagUid.test.ts`

**Interfaces:**
- Produces: `clean_tag_uid(value: str | None) -> str | None` with the rule above; `MIFARE_UID_SUFFIX: Final = "00000100"` in `spools/models.py`; the TS `cleanUid` and `cleanTagUid` with the same rule (`cleanTagUid` keeps returning `null` for none).

- [ ] **Step 1: Write the vector file**

`test-fixtures/filament/tag-uid-vectors.json`: `{"suffix": "00000100", "cases": [{"in": …, "out": …}]}` with these cases (out `null` for none):

| in | out |
|---|---|
| `"7E7482BB"` | `"7E7482BB00000100"` |
| `"7e:74:82:bb"` | `"7E7482BB00000100"` |
| `"7E7482BB00000100"` | `"7E7482BB00000100"` |
| `"53F64B94420001"` (NTAG, 7 bytes) | `"53F64B94420001"` |
| `"00000000"` | `null` |
| `""` | `null` |
| `null` | `null` |
| `"0123456789ABCDEF01"` (18 digits) | `"23456789ABCDEF01"` |

- [ ] **Step 2: Write the failing tests**

`test_tag_uid_vectors.py::test_clean_tag_uid_matches_the_vectors` reads the file (path as `test_label_fit_vectors.py` builds it) and asserts `clean_tag_uid(case["in"]) == case["out"]` for each case, and `MIFARE_UID_SUFFIX == vectors["suffix"]`.

`frontend/src/lib/tagUid.test.ts::cleanTagUid matches the vectors`: the same cases over `cleanTagUid(uid: string | null | undefined): string | null` from `frontend/src/lib/tagUid.ts`, the one browser copy of the rule, which `WriteTag.tsx` and the mocks import.

`test_tags.py::test_a_phone_uid_finds_the_ams_tag_and_its_spool`: a tag made by an AMS read (`TagRead(source="ams", …)` is find-only, so insert it as the existing tests make a bound Bambu tag: `tag_uid` `7E7482BB00000100`, `tray_uuid` `D3B5D4BCA6044374A9B52E9C2DC9AC6A`, bound to a spool). Then `read(…, TagRead(source="phone", uid="7E7482BB"), …)`. Assert `result.tag.id` is that tag, `result.spool.id` is that spool, `result.next == "open_spool"`, and `filament_tags` still has one row.

- [ ] **Step 3: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tag_uid_vectors.py tests/filament/test_tags.py -k "vectors or phone_uid" -v`
Expected: FAIL on `7E7482BB` (got `7E7482BB`). Run `cd frontend && pnpm exec vitest run src/lib/tagUid.test.ts` (after `pnpm gen:api`): FAIL likewise.

- [ ] **Step 4: Implement the rule**

`clean_tag_uid`: `hex = _hex(value)[-16:]`; return `None` when `_key(hex)` is `None`; when `len(hex) == 8` return `hex + MIFARE_UID_SUFFIX`; else `hex`. Docstring: the rule and its measurement (the spec's facts, one line). `frontend/src/lib/tagUid.ts` the same; `WriteTag.tsx` and `mocks/features/filament/index.ts` use it, and their comments say so.

- [ ] **Step 5: Run the tests to see them pass**

Same commands as Step 3. Expected: PASS. Then `uv run --frozen pytest tests/filament tests/api -k "tag or rfid or pull or capture or inventory" -n auto` and `pnpm exec vitest run src/components/filament`: PASS (no other test depended on a short UID staying short; if one did, its expectation changes to the long form, which is the point).

- [ ] **Step 6: Commit**

```bash
git add test-fixtures/filament/tag-uid-vectors.json backend/scadbuddy/spools/models.py backend/tests/filament frontend/src/lib/tagUid.ts frontend/src/components/filament/scan frontend/src/mocks/features/filament/index.ts
git commit -m "fix(filament): a 4-byte tag UID is the AMS's <uid>00000100 (#7905)"
```

### Task 2: The migration that merges and re-keys stored short UIDs

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_filament_tag_uid_canonical.sql`
- Create: `backend/tests/filament/test_tag_uid_migration.py`

**Interfaces:**
- Consumes: the canonical form of Task 1 (the SQL repeats it: `^[0-9A-F]{8}$` gets `|| '00000100'`).
- Produces: no short (8-digit) `tag_uid` left in `filament_tags`, except pairs it could not merge.

- [ ] **Step 1: Write the failing tests** (pattern: `tests/orders/storefront/test_slice_profile_migration.py`: `migrate(conn, MIGRATIONS[:before])`, insert rows, apply the file, assert)

- `test_a_short_row_with_a_long_twin_is_merged`: long `7E7482BB00000100` (kind `bambu_mifare`, tray set, unbound, `last_seen_at` null); short `7E7482BB` (kind `other`, `last_seen_at` set, source `phone`, one `filament_tag_backups` row, one `filament_bb_tag_pending` row). After: one row, the long one's id, kind `bambu_mifare`; its `last_seen_at`/`last_seen_source`/`last_seen_records` are the short row's (the newer); the backup's `tag_id` is the long row's; no pending row names the gone id.
- `test_a_bound_short_row_moves_its_binding_to_the_long_row`: short bound (open binding) to spool S, long unbound. After: the open binding is on the long row, spool S; the short row is gone.
- `test_two_open_bindings_are_left_alone`: both bound (to two spools). After: both rows unchanged, both bindings open.
- `test_a_short_row_with_no_twin_is_rekeyed`: short `DC999BE3` alone. After: same id, `tag_uid` `DC999BE300000100`.
- `test_running_it_again_changes_nothing`: apply the file twice over the first case's rows; the second run changes no row (compare a full `SELECT … ORDER BY id` before and after).

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_tag_uid_migration.py -v` (needs `SCADBUDDY_TEST_DATABASE_URL`, backend/CLAUDE.md "Commands")
Expected: FAIL: the migration file does not exist.

- [ ] **Step 3: Write the migration**

One `DO $$ … $$` block over `SELECT id, tag_uid FROM filament_tags WHERE tag_uid ~ '^[0-9A-F]{8}$' ORDER BY id FOR UPDATE`. For each short row, its twin is the row with `tag_uid = short || '00000100'`.

- Twin and both hold an open binding: `RAISE NOTICE` naming both ids; skip.
- Twin found, otherwise:
  1. Move the short row's bindings to the twin (open and closed: `UPDATE filament_tag_bindings SET tag_id = twin`).
  2. Move its backups to the twin.
  3. Delete its `filament_bb_tag_pending` row, and queue the twin when the short row had one (`INSERT … ON CONFLICT DO NOTHING`).
  4. Copy `last_seen_at`, `last_seen_source` and `last_seen_records` to the twin when the short row's `last_seen_at` is newer or the twin's is null; copy `raw_blob` and `note` when the twin's are null.
  5. Delete the short row. The twin keeps its kind and tray UUID.
- No twin: `UPDATE filament_tags SET tag_uid = tag_uid || '00000100'`.

Header comment: why (the measured facts, one line each), and that it is a no-op where no short UID is stored.

- [ ] **Step 4: Run the tests to see them pass**

Same command. Expected: PASS. Then `uv run --frozen pytest tests/test_pg_migrations.py -v`: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/migrations/*_filament_tag_uid_canonical.sql backend/tests/filament/test_tag_uid_migration.py
git commit -m "fix(filament): merge the tags web scans made under a short UID (#7905)"
```

### Task 3: Docs, issue and PR

**Files:**
- Modify: `docs/superpowers/specs/2026-11-26-bambu-tag-scan-design.md` ("Tag identity": this PR is the canonical form and the merge; `filament_tag_uids` and "same spool as…" ship with the native read)
- Modify: `docs/features/spools-and-filament.md` (the tag section: one line on the UID form and its measurement)
- Modify: `backend/CLAUDE.md` (the `tags.py` entry in "Layout": one clause, "a 4-byte UID is stored as the AMS's `<uid>00000100`, `clean_tag_uid`")

- [ ] **Step 1: Edit the three docs** (the facts above, nothing new)
- [ ] **Step 2: Run the checks CI runs on the touched areas**

`cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`; `cd frontend && pnpm lint && pnpm typecheck`; `npx markdownlint-cli2@0.20.0 <changed .md files>`. Expected: all clean.

- [ ] **Step 3: File the issue and open the PR**

Issue (sub-issue of #7171, linked from #7905): "A web scan of a Bambu tag makes a second tag row (short UID vs AMS `<uid>00000100`)", with the prod rows from the spec. Commit the docs, push `feat/7905-bambu-tag-scan`, open the PR: title `fix(filament): one tag for a phone's UID and the AMS's (#7905)`, body `Fixes #<issue>`, the measured facts with where they came from, and that the migration merges `a167982e…` into `becfa1bf…` on prod and re-keys `DC999BE3` and `9CC69CE3`. After the deploy, read prod's tags (`filament_tag_list include_unbound`) to confirm.
