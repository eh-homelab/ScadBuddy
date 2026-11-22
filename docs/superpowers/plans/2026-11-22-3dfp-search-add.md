# 3DFP search and add on the profile-fetch worker (#7025)

ScadBuddy's assistant finds a filament's 3dfilamentprofiles.com (3DFP) id, and adds a
filament 3DFP lacks, through the profile-fetch worker: the one process that reaches the
browser service, which holds the site's login (#6625, #6780; plan
`2026-11-18-profile-fetch-worker.md`). Before this, the invoice import looked ids up with a
web search, which 3DFP's Vercel bot check refuses.

## What was measured, and where

- The search page: one read-only search, `/filaments?q=SUNLU%20Silk%20Bronze`, on
  2026-11-22, in a copy of `3dfp.mjs`'s signed-in profile (the way `3dfp.mjs search` reads
  it). The page lists each filament twice: as a card (a breadcrumb of
  `/filaments/<brand>/<material>/<type>` links, the name linking `/filament/details/<id>`, the
  hex) and as a table row. The table's header names Brand, Material, Type, Color, RGB, TD,
  Retailer, Deal, Mfr Site and Actions; the Color cell links the filament, the RGB cell holds
  the hex. "Rows per page" is 25. A name can end in a weight, "BRONZE (250g)" (id 38537,
  one the peer script made). There is no weight column. The page's "Columns" control can hide
  columns.
- The add form: the script posted on #7025 by the owner on 2026-11-22, which made 3DFP
  entries 38533, 38535, 38536, 38537 and 38539. Its measured facts are in that comment.
- Nothing of this work ran against the live add form. The tests drive a local replica of
  both pages (`backend/tests/profile_fetch/site/search.html`, `add.html`).

## Rulings

- **R-7025-1 (the path).** The search and the create are the workflows `TdfpSearch` and
  `TdfpCreate` on the `profile-fetch` queue, each one activity (`TdfpActivities`) that runs
  one step on the browser service (`POST /search`, `POST /create`; JSON answers the worker
  checks against `SearchAnswer` and `CreateAnswer`, which forbid unknown fields). The API
  starts the workflow and awaits its outcome (`filament/tdfp.py` `TemporalTdfp`), so the
  worker calls no CRUD route for them and needs no new CRUD area, setting or secret. The
  public routes are `POST /api/v1/filament/tdfp/search` and
  `POST /api/v1/filament/tdfp/filaments`. Neither #6625 (the worker is deployed) nor #7026
  (the identifiers are inputs, R-7025-6) blocks this.
- **R-7025-2 (the query).** Only `?q=` filters (`3dfp.mjs`). The text is the brand, the
  material type and the colour, the form of the measured query. The material is not in the
  text (a name such as `PLA+/Pro` was not measured in it); it ranks the rows: those whose
  material matches first, then those whose type does, each in the site's order. The answer is
  the first page (at most 25). The cells are read by their header, not their position.
- **R-7025-3 (the weight).** A candidate's `nominal_weight_g` is read from a "(250g)" or
  "(1kg)" suffix of its name, else null: the list has no weight column.
- **R-7025-4 (the form).** Brand, material and material type are chosen through each
  combobox's hidden native `<select>` (the script: typing matched "Silk Dual" to "Kevlar"),
  matched exactly, then by case. The brand, which the script typed, is typed and its exact
  option clicked only when its select lacks it. A value the form does not offer is 422
  `option_missing`. A brand, material and type with no "Defaults found" within 5 s is
  refused, 422 `no_defaults`, before anything is filled further. Then "Next", `#color`,
  `#rgbText-0..3` (the button after a row adds the next), `#nominal_weight`.
- **R-7025-5 (a likely duplicate).** Before "Save Filament", the browser service reads the
  form's list after "Avoid creating duplicates": its text (2000 characters) and a card per
  `/filament/details/<id>` link after that heading. A card is a likely duplicate when its
  brand, material and type equal the ask's (case aside; a field the card does not show does
  not clear it), and its name or a colour is close: names with case, the weight suffix and
  every character but letters and digits left out are equal, one holds the other (4
  characters or more), or `difflib`'s ratio is at least 0.85; colours are within CIEDE2000 5.
  With no card read, the list's text stops the create when it holds the colour name. A likely
  duplicate saves nothing and answers `status: duplicate` with the list and why. An id the
  person judged another filament goes in `not_duplicates` and is skipped. The person read the
  list before each of the script's saves; this rule replaces that read
  (`profile_fetch/duplicates.py`).
- **R-7025-6 (the identifiers).** The create takes #7026's well-known keys `mpn`, `gtin`,
  `asin`, `manufacturer_url`, `empty_spool_weight_g` and `td`, and sends each to the form
  where it has a field: `mpn` to `#sku-0` (measured), the rest only to the one field whose
  label names it (not measured, so never a guess at another field). The answer lists `sent`
  and `not_sent`. ScadBuddy stores none of them and adds no column for them; writing them
  through the entities properties route comes after #7026 lands.
- **R-7025-7 (once only).** The create's activity is tried once (`maximum_attempts=1`). A
  failure after the "Save Filament" click is `not_created` (502): the filament may exist,
  so search before another create. A `timeout` or `browser` failure may also come after
  the click, so its reason says the same (`workflow.MAY_EXIST`). One workflow per caller,
  `Idempotency-Key` (428 without), type and ask (`create_workflow_id`, conflict
  `USE_EXISTING`, reuse `ALLOW_DUPLICATE_FAILED_ONLY`; R-7025-11, R-7025-12): the same
  request sent again answers the first outcome and never adds a second filament, and a 504
  (no outcome within the wait) is sent again with the same key to wait for it.
  (Amended by R-7025-11: the driver's own `timeout` and `browser` come only before the
  click; a Temporal timeout of the step is `step_timeout`, and that one carries the note.)
- **R-7025-8 (the type).** With `type_id`, a created id becomes the type's `tdfp_id`. A type
  that already has one is refused 409 `type_has_tdfp_id` before the create starts, unless
  this key and ask already started (a resend). A type that took another id meanwhile keeps it
  (`type_tdfp_id_set: false`).
- **R-7025-9 (tiers).** `filament_tdfp_search` is `read`: it changes nothing anywhere.
  `filament_tdfp_create` is `outward`, approved first: it writes 3DFP, a shared public
  database, as the repo marks the other tools that write outside ScadBuddy.

Rulings from the review of PR #7041 at 1c19d9bc1:

- **R-7025-10 (the search's wait).** The search is tried once (`SEARCH_RETRY`,
  `maximum_attempts=1`), not twice: it is a read, so a caller sends it again, and a second
  attempt would double how long the route holds the request. The route's waits come from
  the workflow's own constants: `workflow.step_wait` is every attempt's queue wait and run,
  the back-off between attempts and `WAIT_MARGIN` (30 s), so `SEARCH_WAIT` is 4 min 30 s
  and `CREATE_WAIT` 7 min 30 s. `test_the_routes_wait_out_every_attempt` pins each wait
  against its retry policy.
- **R-7025-11 (a failure before the save runs again).** A create that failed before "Save
  Filament" made nothing, so its outcome is not kept under the key. The driver raises
  `option_missing`, `no_defaults`, `login_failed`, `bot_check`, `timeout` and `browser`
  only before it clicks Save (after the click every failure is `not_created`), and
  `not_started` is a step no worker took. For those (`workflow.BEFORE_SAVE`) the workflow
  fails with the kind, the API answers it as before, and `ALLOW_DUPLICATE_FAILED_ONLY` lets
  the same key start the create again; `started` counts a failed run as not started, so a
  type's 409 check is not skipped for it. A failure the worker cannot place is kept as one
  that may have saved: a lost, malformed or unexpected answer of the browser service is
  `not_created` (`remote.RemoteSite.create`), a Temporal timeout of the step is
  `step_timeout`, and any other is `step_failed`; each completes the workflow, so the key
  answers it again, and its reason says the filament may exist.
- **R-7025-12 (the key and the type).** The type is part of the create's key: the route
  keys on caller, `Idempotency-Key` and `type_id` (`create_workflow_id`). The same key sent
  for another type is another create, so it never sets the first create's id on a type it
  was not made for. Chosen over a 409 because the key's owner meant another request, and a
  409 would need a stored link from key to type.
- **R-7025-13 (the duplicate list's text).** With no card read, the list's text stops the
  create only when it names the ask's colour as whole words, case aside, with any
  non-letter run between its words (`duplicates._names_whole`): "Tan" is not in "Standard".
  The text is the page from the "Avoid creating duplicates" heading, cut at the "Save
  Filament" button when that is within it, so the form's labels after the list are not read
  as the list. The site's container for the list was not measured (the peer's script reads
  `main.innerText`), so the cut is by that text, not a selector.

Rulings from the review of PR #7041 at 5c03d8850 (its non-blocking findings):

- **R-7025-14 (the type's id, set at once).** The route sets a type's `tdfp_id` with one
  conditional UPDATE (`SpoolTypeStore.set_tdfp_id`: `WHERE id = … AND (tdfp_id IS NULL OR
  tdfp_id = …)`) and answers `type_tdfp_id_set` from whether it matched a row. The read,
  check and write of before ran in separate transactions, so two creates for one type under
  different keys could both pass the check, and the second write replaced the first id. Now
  the row's lock orders them: the first id stays, and the second create answers
  `type_tdfp_id_set: false`. `tdfp_id` is no overridable or priced field, so the write needs
  none of `update`'s spool events or price-book link. `test_two_sets_of_a_types_tdfp_id_keep_the_first`
  races two sets with a `threading.Barrier`, as `test_invoices_race.py` does.
- **R-7025-15 (Temporal down).** An `RPCError` from the create's resend check
  (`TemporalTdfp.started`, other than NOT_FOUND), from the search, or from the create is
  read by `workflows/commands.py` `temporal_failure`, as every route reads one: unavailable
  or busy is 503 `https://scadbuddy.dev/problems/temporal-unavailable` with `Retry-After: 5`
  (`api/operations.py` `temporal_unavailable`); a refusal is 500 `temporal-refused`. The
  check's 503 says `may_have_started: false`, the create's `true` (its start may have reached
  Temporal, and the same key joins it), and the search's leaves it out.
- **R-7062-1 (a search has a slot of its own).** On prod a search answered 503
  `not_started` while a preset fetch ran: the worker's one activity slot
  (`max_concurrent_activities=1`, Decision 16) is held by a fetch's export for the whole
  fetch (about 10 min), and a search waits at most 1 min in the queue (#7062). The worker
  process now runs two Temporal workers (`worker.temporal_workers`). The first polls the
  profile-fetch queue for the three workflows and runs the export, import, failed and create
  activities, one at a time, as before. The second polls `workflow.search_queue` (the queue
  plus `-search`) for the search activity only, one at a time, on a `SiteHolder` and
  `RemoteSite` of its own. `TdfpSearch` schedules its activity there behind
  `workflow.patched("tdfp-search-own-queue-7062")`, since the worker is not versioned. A
  create stays on the fetch slot, so it never runs while an export activity runs. A search's
  `not_started` now says "busy with another search"; a create's still says "busy with a
  preset fetch". Chosen over (a) a priority for searches between a fetch's steps, which
  needs the same second slot to start the search at all, and (b) a second page or context
  on the signed-in browser, which runs two pages on one 3DFP login at once and has never
  been tried against the site. The deployment needs no change: the same process and pod,
  the new queue made by its first poll, no new env var or secret. The API's `no_worker`
  probe still reads the fetch queue, which the same process polls.
  `test_a_search_answers_while_a_preset_fetch_exports` holds a fake export and asks for a
  search: before this change it answered `not_started`.
- **R-7062-2 (the browser takes one step at a time).** The browser service still runs one
  step at a time (`BrowserService.lock`), and each step opens its own page path first, so a
  search takes its turn between an export's steps: it waits at most for the step in hand.
  Measured on the local replica (`tests/profile_fetch/site`) with the production
  `site.PACE` of 1.0, headless Chromium, three runs each: an export step held the browser
  10.3 s, a search 6.0 s (its 6 s settle). The live site adds its own load time. A worker
  that went away while its step still waited for the lock (its activity cancelled or timed
  out) now runs nothing and leaves the browser open; before, the step ran on an already
  closed connection, was cancelled at once, and the browser the export used was closed.
  A search that fails with a fresh-browser kind still closes the shared browser (DELETE
  `/session`), and the export's next step signs in again; that is as before.

## Timeouts

A search waits at most 1 min in the queue and runs at most 3 min (1 attempt, R-7025-10); a
create waits 2 min and runs 5 min (1 attempt). The routes wait every attempt's queue wait
and run plus 30 s (`workflow.step_wait`): 4 min 30 s for a search, 7 min 30 s for a
create, then 504 `no_outcome`. The worker runs one fetch or create activity at a time, so
a create waits behind a preset export; a search runs on a queue and slot of its own, so it
waits only behind another search (#7062, R-7062-1). Past its wait either is 503
`not_started`. Each workflow ends 15
min after its start. The routes answer 503 `no_worker` when Temporal says no worker polls
the queue.
