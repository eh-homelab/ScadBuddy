# Spool fields Bambuddy keeps: data origin, added full, encode time, consumed counter (#7149)

> **UI work (Task 5):** the implementer loads `frontend-design:frontend-design` before
> writing any UI.

**Goal:** ScadBuddy (SB) keeps four Bambuddy (BB) spool fields it dropped (`data_origin`,
`added_full`, `encode_time`, `weight_used_baseline`), and gets an equivalent of BB's
consumed-counter reset. Found by the §11 parity sweep (#6270). Epic #6260.

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` §5 (the
ledger), §8 (the sync) and §11. The pull and push are those of
`2026-11-18-filament-bb-sync.md` (#6265); this plan adds fields to them and one write.

## What BB holds (read from its source, never from the live Bambuddy)

| Field | BB column | BB writes it through | Source |
| --- | --- | --- | --- |
| `data_origin` | `String(20)` | `SpoolCreate`, `SpoolUpdate`, `LinkTagRequest` (default `nfc_link`), its tag matcher (`rfid_auto`, `rfid_linked`) and a SpoolBuddy write (`opentag3d`) | [`models/spool.py` L88](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/models/spool.py#L88), [`schemas/spool.py` L142, L212, L302](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/schemas/spool.py#L142); `data_origin_values` in `backend/tests/bambuddy/recordings/openapi/rfid-routes.json` |
| `added_full` | `bool \| None`, "(unused)" | nothing: only `SpoolResponse` has it | [`models/spool.py` L50](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/models/spool.py#L50), [`schemas/spool.py` L293](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/schemas/spool.py#L293) |
| `encode_time` | `DateTime` | only a SpoolBuddy write result | [`models/spool.py` L85](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/models/spool.py#L85), `docs/superpowers/plans/2026-11-17-filament-rfid.md` (`spoolbuddy.py` L960-1190) |
| `weight_used_baseline` | `Float`, default 0 | `SpoolCreate`, and the two reset routes, which set it to `weight_used` | [`routes/inventory.py` L1811-1870](https://github.com/maziggy/bambuddy/blob/v1.3.1/backend/app/api/routes/inventory.py#L1811) |

BB's "Total consumed" is `weight_used - weight_used_baseline`. A reset leaves
`weight_used`, the remainder and the weight lock alone. `SpoolUpdate` takes none of
`added_full`, `encode_time` or `weight_used_baseline` (`SpoolUpdate` in
`backend/tests/bambuddy/recordings/openapi/sync-routes.json`). The recorded rows
(`backend/tests/bambuddy/recordings/inventory-spools.json`, BB 1.2.6b1) hold
`data_origin` `rfid_auto` or `null`, `added_full` and `encode_time` `null`, and a
baseline of `0.0`.

## Rulings

Each ruling is also a comment on #7149.

- **R-7149-1 — `data_origin` is provenance, kept per spool and read-only.** A new column
  `filament_spools.data_origin` (`^[a-z0-9_]{1,20}$`, BB's 20 characters). The API shows
  it and takes no value for it: a person cannot claim a tag was written by a machine.
  Three writers only. The BB pull takes BB's value (newer change wins; a BB `null` never
  clears SB's; a value outside the pattern is not taken). A person's new tag binding in
  SB sets BB's own value for that link: `rfid_linked` for a Bambu tag, else `nfc_link`
  (amended by R-7149-10). A confirmed NTAG write sets
  `opentag3d`, as BB does. Each SB write is one `edited` event. The `spools` push sends
  it on create and update only when SB has one, so SB never clears BB's value. The tag
  push (`rfid_push.py`) sends the spool's value to `link-tag`, else the bind's value
  for the tag's kind (R-7149-10); before, it always sent `nfc_link`. The pull still reads `opentag3d` to pick the tag kind.
- **R-7149-2 — `added_full` is kept and editable, and syncs BB to SB only.** A new
  nullable boolean column. A person sets it on create and patch (`null` means not known).
  The pull takes BB's value. SB never pushes it: BB takes no write of it (table above).
- **R-7149-3 — `encode_time` is kept as `tag_encoded_at`, and syncs BB to SB only.** A
  new nullable `timestamptz` column, not editable. A confirmed NTAG write sets it to the
  `encode_time` of the BB record SB read to confirm it, else to the confirm time, with
  `data_origin` `opentag3d`. The pull takes BB's value. SB never pushes it: BB takes no
  write of it. BB sets its own on the same SpoolBuddy write, so both sides hold it.
  `filament_ntag_writes.queued_at` stays a queue row only.
- **R-7149-4 — SB's consumed counter is a ledger event.** A new event kind
  `consumed_reset`: no grams, no anchor, `grams_left` unchanged. The spool metrics gain
  `consumed_g` (the plate grams after the newest reset; all of them with no reset) and
  `consumed_since` (that reset's time). `used_g`, waste and cost stay lifetime figures.
  The spool page's "Used" tile shows `consumed_g`, with the reset date and the lifetime
  total beside it. A person resets one spool with `POST /spools/{id}/events`
  (`kind: consumed_reset`), or several with `POST /spools/reset-consumed` and an explicit
  list of 1 to 500 ids. There is no "reset all", for the reason BB gives (a slip would
  wipe the whole inventory's counters).
- **R-7149-5 — An SB reset reaches BB as its own write.** The sync plan adds the op
  `reset_consumed` (`POST /inventory/spools/{id}/reset-consumed-counter`, Manage
  Inventory) for a linked, unarchived spool whose newest SB reset (an event with a
  source other than `bb`) has not been pushed. The hash of that event's id goes in
  `filament_bb_links.pushed_fields.consumed_reset`, so each reset is sent once. It
  pushes under the `weights` data type. A new spool takes it on the pass after its create.
  SB never calls BB's bulk route: one write per spool keeps one result per write.
- **R-7149-6 — A BB reset reaches SB's ledger.** The pull reads `weight_used_baseline`
  under `weights`. The first reading of it only records it. A later change books a
  `consumed_reset` event with source `bb`, once per BB change (its ref), dated by
  R-7149-9. The push echo of SB's own reset holds the new baseline, so that reset does not
  come back. A `bb` reset is never pushed back.
- **R-7149-7 — No baseline column.** SB's ledger stays the truth. BB's baseline lives
  only in the pull's reading (`data.bb.consumed_baseline`) and the link's `bb_seen`.
- **R-7149-8 — The pull-only fields are judged against `bb_seen`, never a push's echo**
  (the review of #7181 at a500e3a83). After a push, the newest reading is the echo, which
  holds BB's whole answer. A BB reset or a BB change of `added_full` made before that
  push would read as already seen, and be lost (the R-T5-5 hazard). So `added_full`,
  `encode_time` and `weight_used_baseline` are `PULL_SEEN_FIELDS`
  (`bb_sync_models.py`): `seen_row` keeps them in the link's `bb_seen`, the pull's
  `_write_seen` takes BB's value of each while its type pulls (only BB writes them), and
  `apply_results` keeps the old seen value through a push's answer, except the baseline
  in the answer of SB's own `reset_consumed`. After an echo the pull judges them, and
  the reset, against `bb_seen`, as it judges `SEEN_FIELDS`. They are never pushed and
  never in a plan's compare.
- **R-7149-9 — A BB reset is dated at the last reading that held the old baseline, or
  earlier.** BB keeps no reset time, and its `updated_at` moves with any later edit, so a
  reset dated at it can be too late and drop plates from `consumed_g`. The event is dated
  at the earlier of BB's `updated_at` and the newest `synced_from_bb` reading whose
  `consumed_baseline` is the old value. A plate between that reading and the real reset
  then counts as used since the reset: the counter may hold a plate too many, never one
  too few.
- **R-7149-10 — A person's bind of a Bambu tag sets `rfid_linked`** (amends R-7149-1).
  BB writes `rfid_linked` when it adopts a Bambu tag onto a matching spool
  (`spool_tag_matcher.py` L417), and `nfc_link` for `link-tag`'s default
  (`inventory.py` L2747), as `data_origin_values` in
  `backend/tests/bambuddy/recordings/openapi/rfid-routes.json` records from BB v1.3.1. So
  a bind of a `bambu_mifare` tag sets `rfid_linked`, and any other kind `nfc_link`
  (`rfid_push.link_origin`).
- **Upgrade (review nit 2 of #7181).** A reading made before this change lacks the new
  snapshot keys. The pull fills each missing key with BB's value when it says nothing
  new: the baseline, a null, or a value SB holds already (`bambuddy_pull._seeded`). So
  the first pull after the deploy books no `synced_from_bb` event and no reset for them.

## Tasks

1. Migration: the three columns, and `consumed_reset` in the event kinds.
2. Model and API: `Spool` and `SpoolCreate`/`SpoolPatch`, the reset event and bulk
   route, metrics `consumed_g`/`consumed_since`, NTAG confirm, tag bind origin.
3. Pull: `bambuddy_pull.snapshot` and its tables; the reset event.
4. Push: `data_origin` in `desired_and_unmapped`; the `reset_consumed` op in the plan,
   the run, the results and the client; `rfid_push` origin.
5. Spool page: the origin, encode time and added-full fields, the counter and its reset;
   msw mocks and vitest tests.
6. Docs: `docs/features/spools-and-filament.md`, and `docs/features/filament-parity.md`
   when it is on main.
7. Agent: `filament_spool_add_event` takes `kind: consumed_reset`, and
   `filament_spools_reset_consumed` is the bulk route (`write`).
