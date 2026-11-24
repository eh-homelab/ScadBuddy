# Spool list operations: bulk edit, bulk archive and restore, unassign, find by tag (#7151)

Gap found by the §11 parity sweep (#6270, `docs/features/filament-parity.md`) against
Bambuddy (BB) 1.2.6b1. Spec: `docs/superpowers/specs/2026-11-14-filament-management-design.md`
§11. Epic #6260.

BB's shapes: `POST /inventory/spools/bulk-update` takes `{ids (1 to 500), update: SpoolUpdate}`
and answers `{updated, not_found}`; `bulk-archive`, `bulk-restore` and `bulk-delete` take
`{ids (1 to 500)}` and answer a count, the ids already so, and `not_found`
([`backend/app/api/routes/inventory.py`](https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/api/routes/inventory.py)).
The route names are in `backend/tests/fixtures/bambuddy-openapi-1.2.6b1.json`; that file
has no body schemas.

## Rulings

- **R-7151-1 Bulk edit is one route, all or none.** `POST /filament/spools/bulk-update`
  takes `{spool_ids (1 to 500), patch}`. `patch` is a `PATCH /filament/spools/{id}` body less
  `archived` (the archive routes do that), and it sets at least one field (422 otherwise).
  So every field a spool's PATCH edits can be set in bulk, the type included, as BB's
  `SpoolUpdate` does. One transaction, the spools locked in id order; each spool takes the
  patch through `update_on`, so each gets its own `edited` event of what changed on it, and
  none when nothing did. A spool the patch refuses (no type nor material left, a minimum
  temperature above its maximum) refuses the whole edit: a 422 that names its number. A row
  the patch names that does not exist is a 422. An id that names no spool is answered in
  `not_found`, as `reset-consumed` does. No `Idempotency-Key`: the write sets values, so a
  resend changes nothing and books no event.
- **R-7151-2 Bulk archive and restore are one route each.** `POST /filament/spools/bulk-archive`
  and `/bulk-restore` take `{spool_ids (1 to 500)}`. One transaction: the spools' open tags
  locked first, then the spools, each in id order, as every writer of bindings takes them.
  Each spool changed gets its own `archived` or `restored` event, and an archived one's open
  binding closes, as one archive does. Nothing is refused per spool, so the answer reports
  each spool once: `changed`, `unchanged` (already so) and `not_found`. The bulk bar sends one
  request and says once what it did.
- **R-7151-3 The agent gets the same, write tier.** `filament_spools_bulk_update` (the bulk
  edit) and `filament_spools_bulk_archive` (`archived: true` or `false`, one of the two
  routes) are `write`, as `filament_spool_upsert` and `filament_spool_archive` are. The three
  routes nudge the Bambuddy sync as every filament write does (the router's
  `nudge_after_write`); none is in `NOT_NUDGED`, since each changes spools the sync pushes.
- **R-7151-4 The bulk bar's "Edit fields".** A dialog lists every field of R-7151-1, each
  with a "Change" tick. Only ticked fields are sent. A ticked field left empty sends `null`,
  so the spool's own value is cleared and it takes its type's. "Weight locked" is yes or no;
  "Added full" is yes, no or not known. A refusal keeps the dialog open with the reason.
  "Set type" and "Move to" stay as they are.
- **R-7151-5 Unassign is on the tray's place page.** An AMS tray's place page that holds a
  spool has "Take the spool out of this tray", which sends the existing
  `DELETE /printers/{id}/trays/{ams}/{tray}/spool`. The spool moves to no place, and the page
  then offers "New spool from this tray" (R-7150-6).
- **R-7151-6 Find by tag.** The filament search (`lib/filamentSearch.ts` and the agent's copy)
  matches a spool's open tag: a token of at least 4 hex digits, with `:` and `-` left out, that
  a tag UID or tray UUID holds. So `04:A1:B2`, as a reader prints it, finds tag `04A1B2…`. The
  Spools tab and `filament_spool_list` `q` read the tags from `GET /filament/tags`; a failed
  read leaves them out. The Tags tab gets the same search box (`q=`), over each tag's keys and
  its spool's number. The shared vectors (`test-fixtures/filament/search-vectors.json`) gain
  a group.

## Tasks

1. Backend: `SpoolIds`, `SpoolBulkUpdate`, `SpoolBulkUpdated`, `SpoolsArchived`
   (`filament/models.py`); `FilamentSpoolStore.bulk_update`, `bulk_archive`
   (`filament/spools.py`); the three routes (`api/filament.py`); tests
   `tests/api/test_filament_spool_bulk_ops.py`.
2. Agent: the two tools, and the tag keys in `filament_spool_list`; tests.
3. Frontend: client calls, msw mocks, `BulkBar` and `BulkEditDialog`, the place page's
   unassign, the tag search on the Spools and Tags tabs; vitest tests.
4. Docs: `docs/features/spools-and-filament.md`; the parity rows.
