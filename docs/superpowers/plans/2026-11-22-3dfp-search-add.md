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
  the click, so its reason says the same (`workflow.MAY_EXIST`). One workflow per caller, `Idempotency-Key` (428 without)
  and ask (`create_workflow_id`, conflict `USE_EXISTING`, reuse `REJECT_DUPLICATE`): the same
  request sent again answers the first outcome and never adds a second filament, and a 504
  (no outcome within the wait) is sent again with the same key to wait for it.
- **R-7025-8 (the type).** With `type_id`, a created id becomes the type's `tdfp_id`. A type
  that already has one is refused 409 `type_has_tdfp_id` before the create starts, unless
  this key and ask already started (a resend). A type that took another id meanwhile keeps it
  (`type_tdfp_id_set: false`).
- **R-7025-9 (tiers).** `filament_tdfp_search` is `read`: it changes nothing anywhere.
  `filament_tdfp_create` is `outward`, approved first: it writes 3DFP, a shared public
  database, as the repo marks the other tools that write outside ScadBuddy.

## Timeouts

A search waits at most 1 min in the queue and runs at most 3 min (2 attempts); a create
waits 2 min and runs 5 min (1 attempt). The worker runs one activity at a time, so either
waits behind a preset export; past its wait it is 503 `not_started`. Each workflow ends 15
min after its start. The routes answer 503 `no_worker` when Temporal says no worker polls
the queue.
