# Attempt media auto-import: the finish photo and timelapse follow the archive

**Date:** 2026-11-05
**Status:** approved by the owner 2026-11-05; reviewed against main 2026-11-23
**Builds on:** [orders design](2026-10-18-orders-design.md) §6.2 (attachments) and
§8.2 (archives); #4251, #4253, #5273; [inventory phase 1](2026-11-21-inventory-and-purchasing-design.md)
§6.2 (entity attachments, #6961).
**Plan:** [2026-11-23-attempt-media-auto-import.md](../plans/2026-11-23-attempt-media-auto-import.md)

## Tracking

Epic: [#7229](https://github.com/eh-homelab/ScadBuddy/issues/7229)

Stories:

| story | issue | title |
|---|---|---|
| 1 | [#7230](https://github.com/eh-homelab/ScadBuddy/issues/7230) | Spike: record Bambuddy's `timelapse/scan` replies |
| 2 | [#7231](https://github.com/eh-homelab/ScadBuddy/issues/7231) | Migration and model |
| 3 | [#7232](https://github.com/eh-homelab/ScadBuddy/issues/7232) | `copy_in` provenance, race, actor |
| 4 | [#7233](https://github.com/eh-homelab/ScadBuddy/issues/7233) | `mirror_media`, the `AttemptMedia` workflow, the three triggers |
| 5 | [#7234](https://github.com/eh-homelab/ScadBuddy/issues/7234) | The housekeeping sweep |
| 6 | [#7235](https://github.com/eh-homelab/ScadBuddy/issues/7235) | Scan the printer, behind a setting (dropped, §9) |
| 7 | [#7236](https://github.com/eh-homelab/ScadBuddy/issues/7236) | Read fields and the attempt row on page open |
| 8 | [#7237](https://github.com/eh-homelab/ScadBuddy/issues/7237) | Agent text and docs |

## Reviewed against main (2026-11-23)

The draft was written on 2026-11-05. Main moved since. This review read the code at
`98d17dd52` (origin/main, 2026-11-23) and changed the spec as follows:

- **Hand attempts copy their facts at once now** (#6134, #6146, #6169). `record_fulfilment`
  and `update_fulfilment` call `mirror_archives` through `_with_archive_facts`, with a 5 s
  read timeout (`ARCHIVE_READ_TIMEOUT`). F1 and the §4.1 table now say so. The import
  starts from `_with_archive_facts`, after the facts. The route never waits on a download.
- **Order attachments are rows of `entity_attachments`** (#6961 for #6950; the
  `order_attachments` view went in #6960). The two new columns, the unique index and the
  backfill now target `entity_attachments` (F6, §4.2).
- **The unique index includes `filename`.** The draft's index on
  `(subject, archive_id, archive_media)` refused a second finish photo of one archive, which
  the manual button must still allow (§1, "pick a different finish photo"). §4.2.
- **The retries are a Temporal workflow, and the sweep is a housekeeping sweep.** The repo
  forbids a periodic loop in the API ("A new periodic pass is an activity in its `SWEEPS`",
  `backend/CLAUDE.md` "Housekeeping"). The housekeeping Schedules run every 300 s or once a
  day, not once an hour. A per-attempt workflow (the `StagedFile` pattern, #6133) also keeps
  three API replicas from importing the same file three times. §4.6, F12.
- **The browser polls.** `order_item.changed` fires only on a status change, and the admin
  order page hears no order event (F13). §4.4.
- **The media proxy accepts an archive that an attempt claims** (#6406). The manual
  timelapse pull still needs a print link (F11). §4.3.
- **Bambuddy's queue can record a timelapse** (#5565 for #5352). §2 and F4.
- **`timelapse/scan` is not wrapped** (F5). The spike recorded its replies (§4.3a).
- **The timelapse scan is dropped** (owner's direction, 2026-11-23: ScadBuddy distances
  itself from Bambuddy). ScadBuddy never calls `timelapse/scan`. It imports only a
  timelapse that Bambuddy's archive already has. The spike's findings (§4.3a) stay as
  the evidence. §4.3, §4.4, §8.1, §9.
- **A pre-existing bug in the copy route** (F14): `ArchiveCopy.what` accepts `"file"` since
  #5679. The import's story narrows it.
- **#4721** is mostly shipped; §4.8 says how it relates.
- Every file:line reference was checked again. The owner's decisions (§8) are unchanged, except §8.1, which the owner superseded on 2026-11-23 (§9).

## 1. Intent

Today an attempt's Bambuddy archive is *offered*: once the print settles, the attempt
copies the archive's ids, times and grams (`orders/archives.py` `mirror_archives`), and
the attempt's row in the UI shows a **Copy finish photo** and a **Copy timelapse**
button (`ArchiveCopies.tsx`). Nobody presses them. ORD-2026-3 shipped with five
production attempts and nine cost-only attempts, every one with a finish photo in
Bambuddy and three with timelapses, and not one attached: the order page's attempts read
"no timelapse (pull it from the print's page first)" and the finish photo sat a click
away on fourteen rows.

The ask, in the user's words: *"make it so when an attempt is attached, the finish photo
and timelapse video are auto imported and attached."* Scope, confirmed: **all of the
above**, which means every way an attempt comes to name an archive:

1. an attempt recorded by hand with an `archive_id` (`POST …/fulfilments`, the
   `order_item_fulfil` tool, the UI's **Attach to order item** dialog on a print's page,
   `AttachToOrderItem.tsx`). Since #6134 the route copies the archive's facts at once,
   but not its media (F1);
2. the automatic path: a print run started by `order_item_print`, whose outcome
   `listen_for_prints` hears and whose archives `print_links` names;
3. the read: `GET …/fulfilments/{f}/archives` (`order_attempt_archives`), which
   already copies facts a late link brought. Today the UI fires it only when the
   row's **From Bambuddy…** button is pressed (`ArchiveCopies.tsx` starts with
   `read` null; `FulfilmentForm.tsx` mounts it and never loads it), so it is a click,
   not a page open, and the button keeps showing after an import because the page
   has no way to know (F6; seen on ORD-2026-3 after every attempt's media was copied).
   The owner has decided the row loads on page open (§4.4).

The buttons stay as a fallback for what the import could not do (a photo past the size
limit, a Bambuddy that was down), and as the way to pick a *different* finish photo.

## 2. Goals, non-goals, anti-goals

**Goals**

- An attempt that names an archive ends up with that archive's latest `finish_` photo
  as its `finished` attachment and, when the archive has one, its timelapse as its
  `timelapse` attachment, with no click.
- Media that lands late (Bambuddy links the archive after the outcome, the finish photo
  is written seconds after `completed`, a timelapse pulled off the printer later) is
  still imported, by the next trigger.
- Nothing is downloaded twice: a 300 MB timelapse is pulled once per attempt, and a
  repeated trigger costs one Bambuddy read, not a re-download.

**Non-goals**

- Promoting anything to Bambuddy. Nothing goes the other way (§8.2 stands).
- Copying every photo of the archive, or the sliced file, or the plate preview. One
  finish photo and one timelapse per archive.
- Pulling a timelapse the printer never recorded. A print queued with timelapse off
  has nothing to import. The design does not change how prints are queued: Bambuddy's
  queue and the LAN path both take a `timelapse` flag, off by default
  (`bambuddy/models.py:1678` `QueueItemCreate.timelapse`, `workflows/print_models.py:347`
  `LanPrintInput.timelapse`; #5565, #5323).
- The native/LAN print path's camera work (#3823). This is the Bambuddy seam only.
- Showing Bambuddy's files in place. Entity attachments can list a print archive's own
  files without a copy (`entities/attachments.py` `PRINT_ARCHIVE`, #6961). An order needs
  its own blob: Bambuddy prunes archives, and the order's file outlives them.

**Anti-goals**

- A finish photo a person chose by hand replaced by the archive's latest. The import
  adds; it never overwrites or deletes an attachment.
- A re-attached archive silently stealing another attempt's media. The `archive_claimed`
  rule (#5268) stands: an archive is one attempt's.
- A timelapse import that trips the **upload limit** and takes the whole attempt's
  copy with it. Each medium is its own unit of work.

## 3. Facts this rests on

Verified 2026-11-05; checked again on main `98d17dd52` on 2026-11-23 (file:line on that
commit). F6 and the §4.2 "today" text describe main before #7241, which added the
provenance columns and the partial unique index. F11 to F14 are new in the review.

| | fact | where |
|---|---|---|
| F1 | `mirror_archives` copies facts for both attempt kinds: a hand attempt's `archive_ids`, a run's `OrderStore.run_archives`. It runs on each `print.outcome` (`mirror_run`, via `listen_for_prints`' `on_outcome`), on each `GET …/archives`, and, for a hand attempt with archives, on `record_fulfilment` and `update_fulfilment` through `_with_archive_facts` (#6134). That route call waits at most `ARCHIVE_READ_TIMEOUT` (5 s) for the read (#6146); the write after the read is not timed (#6169). A failure or a timeout is logged and the attempt is answered as written. No path copies media. | `orders/archives.py:104-150`, `api/orders.py:3060-3131`, `orders/component.py:248-252`, `orders/printing.py:709-757` |
| F2 | `copy_in` is the one-click copy: `copy_check` → `integrations.archive` → `download_archive_media` into the claim store → sha256+role dedupe against the attempt's and the item's attachments → `lifecycle.add_attachment` with caption `Attempt <n>, Bambuddy archive <id>`. It downloads **before** it dedupes. | `orders/archives.py:180-296` (download 245-256, dedupe 260-267, caption 291) |
| F3 | The copy is the `order_attempt_copy` operation: `run_attempts=2`, `queue="library"`, `run_timeout=PIN_TIMEOUT`, size-capped by `media_upload_max_bytes` (413 past it). Started only from the HTTP route, with the author header as `by` (Ruling 12). The `library` queue's worker runs inside the API process. | `orders/operations.py:1091-1135,1206-1215`, `api/orders.py:3174-3222`, `main.py:1960-1969` |
| F4 | `ArchiveFacts` carries `finish_photos` (Bambuddy's order), `timelapse: bool`, `timelapse_name`. `timelapse` is true only once Bambuddy has the file: queued with timelapse on **and** processed, or pulled later through `POST /prints/{archive_id}/timelapse/pull` (`timelapse_pull` operation → Bambuddy `timelapse/select`, FTP from the printer, needs a filename from `printer-media`). | `orders/integrations.py:123-141`, `orders/bambuddy.py:81-104,128-130`, `bambuddy/operations.py:330-347,483`, `bambuddy/client.py:1552-1590`, `api/print_history.py:1674-1720` |
| F5 | Bambuddy also has `POST /archives/{id}/timelapse/scan`: "Scan printer for timelapse matching this archive and attach it", no parameters. Read off the **running** instance's `/openapi.json` on 2026-11-05. On main 2026-11-23: the route list recorded from Bambuddy 1.2.5.5 names it (`tests/bambuddy/recordings/openapi/routes.txt:78`); the vendored spec `tests/fixtures/bambuddy-openapi-1.2.6b1.json:636-640` carries only its operationId, no response schema; the spike recorded its replies later (§4.3a, merged in #7238); the client does not wrap it (no `archives.timelapse.scan` in the closed `Operation` set). Bambuddy's own policy on `printer-media` is "files are downloaded only after the user explicitly selects them in the UI". | live Bambuddy OpenAPI (2026-11-05), `bambuddy/client.py:178-232` |
| F6 | Order attachments are rows of `entity_attachments` since #6961 (the table `order_attachments` renamed, `entity_id` added; the compatibility view dropped in #6960). An attempt's file has the subject `order_item_fulfilment:<id>` and `attempt_item_id` set. The table has no column naming the archive a copy came from; the only link is the caption text. It has no unique index except its key. The frontend's "copied" state is a per-page `Set` that forgets on reload. | `migrations/20261122T0426Z_entities.sql:26-77`, `migrations/20261123T0651Z_drop_order_attachments_view.sql`, `orders/store.py:251-254,3086-3137`, `ArchiveCopies.tsx:16,41,64-65` |
| F7 | There is no system actor. Every write names the actor from the request (Ruling 12). `Actor.kind` is `browser`, `agent`, `provider`, `visitor` or `customer`. | spec §10 Ruling 12, `orders/actor.py`, `orders/models.py:164-176` |
| F8 | Every replica hears every bus event (the API runs three: eh-homelab/clusters `applications/scadbuddy/scadbuddy.yaml:120`, `replicas: 3`, read on clusters main `802ddba9` on 2026-11-23). `follow_print` writes under the order's lock and only what is still open. `mirror_run` is logged, never raised. `copy_in`'s dedupe is a read then an insert in separate transactions, and `entity_attachments` has no unique index, so two replicas can both insert. | `orders/printing.py:709-757`, `orders/archives.py:142-150,260-294` |
| F9 | `media_upload_max_bytes` defaults to 1 GiB; a timelapse past it is an edge case here, not the common one. | `core/config.py:113`, `core/settings.py:184` |
| F10 | `archive_facts()` keeps Bambuddy's `photos` order. Print history's `ArchiveDetail.finish_photo` takes the **first** `finish_`; `copy_in` defaults to the **last**. The names carry a timestamp (`finish_YYYYMMDD_HHMMSS_<hash>.jpg`). | `orders/bambuddy.py:94`, `bambuddy/models.py:440-443`, `orders/archives.py:236` |
| F11 | The media proxy's reads accept an archive that an order attempt claims (#6406: `require_known_archive`, `PrintLinkStore.known` reads `order_item_fulfilments.archive_ids`). The print detail and the manual timelapse pull do **not**: they need a print link (`_require_print`). So an attempt's archive that only the attempt names (for example one the agent named) has no ScadBuddy pull path; such an archive gets its timelapse only if Bambuddy has one. | `api/prints.py:90-97`, `bambuddy/print_links.py:568-574`, `api/print_history.py:1218-1225,1453,1706` |
| F12 | Periodic work runs as housekeeping sweeps on Temporal Schedules on the `library` queue, in the API process: the sweeps' Schedule (`asset_sweep_interval`, default 86400 s) and `PRUNE_SWEEPS` on a 300 s Schedule. "A new periodic pass is an activity in its `SWEEPS`, never a loop in the API." A new sweep goes behind its own `workflow.patched`. A per-object watcher is a workflow on the same queue: `StagedFile`, id `staged-file:<upload id>`, started with `USE_EXISTING` / `ALLOW_DUPLICATE`, AUTO_UPGRADE through `VersionedStagedFile` in `main.LIBRARY_WORKFLOWS`. | `backend/CLAUDE.md` "Housekeeping", `core/config.py:89`, `workflows/housekeeping.py:155-335`, `workflows/staged_file.py`, `orders/storefront/staged_files.py:95-136`, `main.py:1894` |
| F13 | `order_item.changed` fires once per status change of the item (`order_status_changes`), never for an attachment. The admin order page subscribes to no order event; only the customer's `OrderPage.tsx` and the flow triggers read order events. So a page learns of an import only by reading again. | `backend/CLAUDE.md` flow triggers "Order events", `frontend/src/components/orders/` (no subscriber) |
| F14 | `ArchiveCopy.what` is `ArchiveMedia`, which gained `"file"` for past-print quotes (#5679). `copy_in` maps only `finished_photo` and `timelapse` to a role (`ROLES`), so `POST …/copy` with `what: "file"` downloads the archive's 3MF under the timelapse's name and then fails on `ROLES["file"]`. The agent's tool allows only the two media. | `orders/integrations.py:181`, `api/orders.py:3134-3150`, `orders/archives.py:48,239-257`, `agent/src/tools/orders.ts:2308-2322` |

## 4. The design

All new writes are in the API process: its routes, and the activities of the `library`
queue's worker, which runs in the API (F3, F12). No new process reads or writes SQL, and
no CRUD route is needed (repo `CLAUDE.md` "Database access"). The agent only calls the
routes it calls today.

### 4.1 One step, three triggers, one workflow per attempt

`orders/archives.py` gains `mirror_media(lifecycle, integrations, blobs, claims,
attempt, facts, *, limit) -> list[Attachment]`: for each archive in `facts`, the
attachments the attempt is still missing, imported. `mirror_attempt(attempt_id)` is
`mirror_archives` then `mirror_media` for one attempt, logged and never raised (the shape
of `mirror_run`).

`mirror_attempt` runs only as the step of a workflow, `AttemptMedia`, one execution per
attempt, id `attempt-media:<fulfilment id>`, on the `library` queue (the API's worker),
AUTO_UPGRADE through `VersionedAttemptMedia` (the `StagedFile` pattern, F12). A trigger
starts it with `id_conflict_policy=USE_EXISTING` and `id_reuse_policy=ALLOW_DUPLICATE`:
a run already going is joined, and an ended one starts again. The start is one Temporal
call; it never waits on Bambuddy. A start that fails (Temporal down) is logged, and the
sweep (§4.6) catches the attempt.

| trigger | today | after |
|---|---|---|
| hand attempt with `archive_id` (`record_fulfilment`, `update_fulfilment` routes) | `_with_archive_facts` copies the facts in the request, the read timed at 5 s (F1); media wait for a click | after `_with_archive_facts`, the route starts `AttemptMedia` (retries on, §4.6). The request never waits on a download. The agent's `order_item_fulfil` is a separate TypeScript process that only POSTs the route (`agent/src/tools/orders.ts:2130-2150`); it changes its text, nothing else |
| `print.outcome` heard | `mirror_run` → `mirror_archives` | `mirror_run` → `mirror_archives`, then starts `AttemptMedia` (retries on). Every replica hears the outcome (F8); the workflow id makes it one run |
| `GET …/archives` | `attempt_archives` → `mirror_archives` | `attempt_archives` → `mirror_archives`, then starts `AttemptMedia` (one step, no retries) **only** when a medium is missing that the facts offer, and answers with what is attached now (§4.4). The read never waits on a download |

### 4.2 What "missing" means, without downloading

`mirror_media` must not re-download to find out it has the bytes already (F2 downloads
first). Two new nullable columns on `entity_attachments` (F6):

- `archive_id integer`: the Bambuddy archive the attachment was copied from;
- `archive_media text`, `CHECK (archive_media IN ('finished_photo', 'timelapse'))`, and
  both columns null or both set.

`copy_in` writes them for every copy, manual or automatic. `role` cannot carry this: a
person may edit it (`AttachmentPatch`). The file's name is already in `filename` (the
Bambuddy name through `clean_filename`, which a person cannot edit).

A **partial unique index** on `entity_attachments (subject, archive_id, archive_media,
filename) WHERE archive_id IS NOT NULL` stops a double insert (§4.6). It includes
`filename` so that a person can still copy a *different* finish photo of the same archive
with the button and `name`.

The **backfill** sets the two columns on the rows that exist: caption
`Attempt <n>, Bambuddy archive <id>` gives `archive_id`; `filename` gives the medium
(`finish_%` → `finished_photo`, `timelapse-%` → `timelapse`). It covers both subjects a
copy has had: `order_item_fulfilment:<id>` (since #5268) and `order_item:<id>` (before).
A row whose caption or name does not match stays null. Where two rows share one index
key, only the oldest is set, so the index can be built.

An archive's medium is **present** when an attachment of the attempt, or of its item,
carries that `(archive_id, archive_media)`, whatever its name. `mirror_media` then skips
it without a read of Bambuddy's bytes. The sha256+role dedupe in `copy_in` stays as the
second net (the same photo attached by hand under the same role is still found).

"Latest finish photo" means the `finish_` name that sorts last (the names carry a
timestamp, F10), which is `copy_in`'s default today and **not** print history's
(`finish_photo` takes the first). The import sorts rather than trusting Bambuddy's list
order. A person who wants an earlier frame uses the button with `name`; the import does
not revisit a photo it has imported when a newer `finish_` lands.

### 4.3 The timelapse that is not there yet

`facts.timelapse` is false for three different reasons. The import handles only the
first:

1. **The archive is too fresh.** Bambuddy processes the timelapse after `completed`.
   Nothing to do now; the next step imports it.
2. **It is on the printer, not in Bambuddy.** The print was queued with timelapse on but
   Bambuddy did not fetch it (#5323 and #5565 turn the flag on). ScadBuddy does not
   handle this case. Bambuddy scans the printer by itself when a print ends with
   timelapse on (§4.3a). If that scan misses, a person pulls the video by hand in
   Bambuddy, or from the print's page in ScadBuddy where the archive has a print link
   (F11). The next step then imports it.
3. **The printer never recorded one.** Nothing to import.

The import cannot tell case 2 from case 3, and it does not try. The UI says "no
timelapse in Bambuddy for this print" for both, once the archive is no longer fresh
(§4.4).

### 4.3a What the spike measured (#7230, PR #7238, 2026-11-23)

These findings led to the drop of the scan (§9). Measured on prod's Bambuddy 1.3.2
(auth off) with two scan calls. The replies are vendored as
`backend/tests/bambuddy/recordings/timelapse-scan-none.json` and
`timelapse-scan-exists.json` (merged in #7238), and the recordings' README section cites
the Bambuddy source lines. They stay as the evidence for the drop.

- **Replies.** Always 200 with a `status`: `not_found` (with `available_files`, the
  card's unclaimed videos; archive 282, 0.82 s), `exists` (a timelapse is already
  attached; archive 283, 0.17 s, no printer contact), `attached` (with `filename`;
  from the source only, see below). An unknown archive is 404.
- **Synchronous.** The route downloads and attaches before it replies: an
  `attached` reply means the next archive read has the timelapse.
- **A match deletes the printer's copy** of the video (`delete_archived_timelapse`).
  The spike therefore did not call the scan on the one archive that would have
  matched; there is no `attached` recording.
- **It already runs by itself.** Bambuddy scans when a print ends with timelapse on.
- **It can match loosely.** Videos claimed by other archives are excluded first. With a
  print-start baseline it attaches the one video new since the print started (two or
  more: nothing). Without one it tries, in order: the file's stem in the video name,
  the name's timestamp within ±4 h of `started_at`, the FTP mtime within 2 h of
  `completed_at`, then the card's only video if the print completed under 1 h ago.
  So a later print's lone unclaimed video can attach to this archive when this
  print's own video is gone, and the clock rules depend on the printer's clock (the
  H2C's is off by about 1 h 20 min). A wrong match also deletes that video from the
  card.

### 4.4 The read tells the truth

`ArchiveOffer` (one archive of the `GET …/archives` answer, `AttemptArchives.archives[]`)
gains `finished_attachment_id` and `timelapse_attachment_id` (nullable): what is attached
now, from the new columns. The read starts the import and answers at once (§4.1), so the answer
is "what is attached now", and the next read shows what the import did.

**The row loads on page open** (owner's decision, 2026-11-05): `ArchiveCopies.tsx`
calls `attemptArchives` when it mounts for an attempt that names an archive or a
run, and the **From Bambuddy…** button goes. It renders from the answer instead of
its page-local `Set` (F6): "Finish photo attached" / "Timelapse attached" are facts
after a reload, and the copy buttons show only for what is still missing. The "pull it
from the print's page first" text becomes one of two timelapse states, by the
archive's `completed_at` (already in `ArchiveOffer`; no new field):

- **"waiting for Bambuddy to process the timelapse"**: `completed_at` is null or less
  than 1 hour ago. Bambuddy writes the timelapse minutes after `completed` (§4.6); by
  that hour the workflow has stepped at +15 min and the sweep has checked once more.
- **"no timelapse in Bambuddy for this print"**: `completed_at` is 1 hour ago or more.
  This covers §4.3's cases 2 and 3. The sweep still checks for 14 days, so a timelapse
  pulled later is imported and the row then says "Timelapse attached".

The cost, accepted: one Bambuddy archive read per attempt per order page open (14 on
ORD-2026-3), each of which may start an import. No read reaches the printer.

No event tells the page that an import attached a file (F13). So the row **reads again**
while the answer offers a medium that is not attached yet: every 10 s, at most six times
after a mount or a change, then it stops and shows the buttons. `FulfilmentForm.tsx`
remounts the row after a hand attempt is recorded, which starts the same reads. The
too-large case has no flag of its own: the manual button stays and 413s, as today, and
at the 1 GiB default (F9) it is rare.

The attachments list on the item (`GET …/attachments`) is unchanged in shape; the
imported rows appear there as the manual copies do today, with the same caption.

### 4.5 Who did it

No system actor exists (F7) and this design does not add one. An automatic copy is
recorded `created_by` = **the attempt's `created_by`**: for a hand attempt that is the
person (or agent) who attached the print; for a run it is whoever started the print,
which the attempt already carries. The caption stays `Attempt <n>, Bambuddy archive
<id>`; the audit line (`attachment.added`) gains `source: archive` so a reader can tell
an import from an upload. (This is the order event's detail, not `EntityAttachment.source`,
which says whose bytes they are.) My call; §7 lists it.

### 4.6 Failure, retry, replicas

- Each medium is its own try. A timelapse past `media_upload_max_bytes` logs a warning
  with the size and the limit and does not stop the finish photo. The manual button for
  it stays and 413s, as today.
- Any other failure (Bambuddy down, archive gone) is logged and left for the next
  step. No row records a failed try except `media_checked_at` (below).
- **The retry is scheduled, not clicked.** The finish photo is written seconds after
  `completed` and the timelapse minutes after, so the `print.outcome` trigger alone
  misses both often. `AttemptMedia` started by an outcome or a hand attempt steps now,
  at **+2 min** and at **+15 min**, and ends early once nothing is missing. The timers
  are Temporal's, so a deploy does not lose them. A start from the read steps once.
- **The sweep** is a housekeeping sweep, `housekeeping_import_attempt_media`, on the
  300 s prune Schedule (`PRUNE_SWEEPS`, so it runs with the asset sweeps off too), behind
  its own patch (F12). It starts a one-step `AttemptMedia` for every attempt of the last
  14 days that names an archive, lacks a medium, and was last checked more than an hour
  ago. Each step stamps `media_checked_at` on the attempt, so each attempt costs at most
  one Bambuddy read an hour. **As built (#7453):** one pass starts at most `SWEEP_BATCH`
  (40) attempts, least lately checked first, as each start may wait `START_TIMEOUT` (5 s)
  inside the sweep's 5 min. A medium that an archive past `FRESH` does not offer (a printer
  with timelapse off) is recorded by the step in `order_attempt_media_not_offered`, which
  each step rewrites, and the sweep leaves it out; so such an archive no longer costs a read
  an hour for its 14 days. A timelapse Bambuddy offers later still comes in through the
  read of the attempt's archives. A settled run with no archive yet is still read each hour.
  The draft's `BusResync` trigger goes: the sweep catches a missed outcome within
  the hour, and a periodic pass belongs in the housekeeping sweeps, not in the API (F12).
- **Replicas.** Every replica hears `print.outcome` (F8); the workflow id makes their
  three starts one run, so one step imports, not three. The manual copy operation can
  still race a step. The **partial unique index** (§4.2) is the net. On a conflict the
  insert writes nothing: `add_attachment`'s transaction rolls back with its blob hold and
  its audit line, and `copy_in` re-reads and answers with the winner's attachment
  (`attachments.keep` is content-addressed, so the second keep of the same sha is a no-op,
  and the blob sweep's grace covers the moment in between). The cost of a race is one
  duplicate download, never a duplicate row.

### 4.7 Agent surface

- `order_item_fulfil` with `archive_id` returns after starting the import, not after it;
  the tool's text says the finish photo and timelapse are being imported and that
  `order_attempt_archives` shows them once attached.
- `order_attempt_archives` reports the two attachment ids per archive (§4.4), so an agent verifying an order can read "attached" without listing
  attachments. It stays `risk: 'read'`, as it already writes facts today; its text says
  that a read starts the import.
- `order_attempt_copy` stays, for a chosen `name` and for the fallback.

### 4.8 Relation to #4721

#4721 ("attachments on a print attempt, and 'Attach to order item' from a print", open)
asked for four things. Three shipped in #5268 (2026-11-03): the `order_item_fulfilment:<id>`
subject and its upload route, the archive copies landing on their attempt, and the
**Attach to order item** dialog on a print's page. The Files panel labels each file with
its attempt but does not group by attempt. This design depends on what #5268 shipped and
on nothing still open in #4721. The two are independent: the import fills the attempt's
files; the grouping only shows them. #4721 can be narrowed to the grouping, or closed.

## 5. Changes, by file

Backend
- Shipped in #7241: `scadbuddy/migrations/20261123T1649Z_attempt_media_provenance.sql`
  (the two `entity_attachments` columns and their checks, the backfill, the partial
  unique index (§4.2), and `order_item_fulfilments.media_checked_at`), the model fields
  below, `insert_attachment`'s `ArchiveCopyExistsError`, `archive_media_present` and
  `stamp_media_checked`.
- `orders/models.py`: `Attachment.archive_id`, `.archive_media`;
  `Fulfilment.media_checked_at`.
- `orders/store.py`: write/read the columns (`_ATTACHMENT`, `_FULFILMENT`,
  `insert_attachment` raises `ArchiveCopyExistsError` on the unique index);
  `archive_media_present(fulfilment_id, item_id)` (what is present, attempt and item); `attempts_missing_media(since, checked_before)` for the
  sweep; `stamp_media_checked`.
- `orders/archives.py`: `mirror_media`, `mirror_attempt`; `copy_in` writes the columns,
  takes `created_by` explicitly, and maps the insert conflict to the winner's row;
  `ArchiveOffer` gains the two ids;
  `attempt_archives` and `mirror_run` start `AttemptMedia`.
- `orders/lifecycle.py` `add_attachment`: accept `archive_id`, `archive_media` and
  `source`; the audit line carries `source`.
- `workflows/attempt_media.py` (new): `AttemptMedia`, `VersionedAttemptMedia`, the step
  activity `attempt_media_step`; registered in `main.LIBRARY_WORKFLOWS` and the library
  worker's activities. `orders/attempt_media.py` (new): the starter (`TemporalStarter`,
  as `staged_files.TemporalWatcher`).
- `workflows/housekeeping.py`: `ATTEMPT_MEDIA_SWEEP` in `SWEEPS` and `PRUNE_SWEEPS`,
  behind `ATTEMPT_MEDIA_PATCH`, with a replay history.
- `api/orders.py`: `_with_archive_facts` starts `AttemptMedia` after the facts;
  `ArchiveCopy.what` narrows to `finished_photo | timelapse` (F14).
- No new setting and no new Bambuddy call. The spike (§4.3a) called `timelapse/scan`
  live; its findings led to the drop (§9).

Frontend
- `ArchiveCopies.tsx`: loads on mount, renders from the answer's attachment ids and
  `completed_at`, re-reads while a medium is pending, new copy; the **From Bambuddy…** button goes.
- `FulfilmentForm.tsx`: the row remounts after a hand attempt with an archive is recorded.
- Mocks (`src/mocks/features/orders/`): the new answer fields.

Tests
- `backend/tests/orders/test_archives.py`: the `Archives` fake grows a too-large medium;
  cases: a hand attempt imports both; a heard outcome imports both; a late timelapse is
  imported on the next step; nothing is downloaded for a medium already recorded (the
  fake counts downloads); a too-large timelapse does not stop the photo; without
  Bambuddy nothing is asked; a race between two inserts gives one row.
- `backend/tests/test_pg_migrations.py`: the backfill, including two rows on one key.
- A workflow test for `AttemptMedia` (timers, early end, id reuse) and a replay test for
  the housekeeping patch.
- `frontend`: `ArchiveCopies.test.tsx` for the two timelapse states, the load on mount
  and the bounded re-read.

Docs
- Spec 2026-10-18 §6.2 and §8.2: "offered as one-click copies" becomes "imported;
  the copy route remains for a chosen photo and as the fallback". Ruling 12 gains the §4.5 convention for automatic writes.
- `backend/CLAUDE.md` orders section: the triggers, the workflow, the columns, and the trap that `GET …/archives` is a write (it starts an import): a reader
  expecting a pure read will be surprised by a 300 MB pull.
- `docs/features/orders-and-quotes.md`: one line on attempt media.

## 6. Left to the builder

- The exact wording of the two timelapse states.
- The re-read's interval and count in `ArchiveCopies.tsx`, within §4.4's bounds.

## 7. Calls I made (check these while reading)

1. **Latest finish photo only** (last by name), one per archive, never revisited once
   imported.
2. **No printer scan.** The import copies only a timelapse that Bambuddy's archive
   has. (Was: scan the printer behind a setting; dropped 2026-11-23, §9.)
3. **`created_by` is the attempt's creator**; no system actor. Audit says
   `source: archive`.
4. **Two columns on `entity_attachments`** (`archive_id`, `archive_media`) plus a
   partial unique index with `filename`, backfilled from captions and names, so "already
   imported" is a row read and not a download, a replica race is a duplicate download,
   never a duplicate row, and a second chosen photo is still allowed.
5. **The read starts and answers.** `GET …/fulfilments/{f}/archives` never waits on
   a download.
6. **One `AttemptMedia` workflow per attempt** steps now, at +2 and at +15 min; a
   housekeeping sweep every 300 s, throttled to once an hour per attempt by
   `media_checked_at`, over the last 14 days, is the retry. Not the page open. (Review
   2026-11-23: was an in-process delayed task and an hourly clock; see the review note.)
7. Manual buttons stay, shown only for what is missing.
8. **The page polls**, bounded, because no event reaches it (F13). (Review 2026-11-23.)

## 8. Decided by the owner (2026-11-05)

1. ~~**Scan the printer automatically: yes.** Default on, scoped to the attempt's one
   archive, capped as §4.3 says; the spike still records the scan's reply first, and
   a loose match comes back as a question before the switch ships on.~~
   Superseded 2026-11-23 by the owner's direction to distance ScadBuddy from Bambuddy
   (§9).
2. **Load the archive row on page open: yes.** The **From Bambuddy…** button goes;
   the row shows what is attached and offers only what is missing (§4.4).

## 9. Decided 2026-11-23: no scan

The owner's direction of 2026-11-23: ScadBuddy distances itself from Bambuddy. It owns
its records and adds no new read of, or dependency on, Bambuddy. So ScadBuddy never
calls `timelapse/scan`. The spike (§4.3a) gives the reasons:

- it is a new Bambuddy dependency;
- a match deletes the video from the printer's card;
- it can attach another print's video (clock-based rules; the H2C's clock is off by
  about 1 h 20 min);
- Bambuddy already scans by itself at print end.

The import of the finish photo and of a timelapse that Bambuddy's archive already has
stays: it is ScadBuddy's own copy of the record.
