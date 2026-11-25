# A 3DFP fetch adds a My Spools record only once (#7228)

**Problem.** `profile_fetch/site.py` `ensure_in_my_spools` clicked "Add to My Spools"
whenever the button was on the filament's page. 3DFP shows that button every time, and
each click adds one more 1000 g record. Measured on 2026-11-23 in the owner's My Spools:
3DFP 22804 held 4 records for 1 spool, about one per fetch (details on #7228).

**Change.** `ensure_in_my_spools` opens `/my/spools/details/<id>` first. When that page
offers "Export slicer profile", the owner has a record, and the step adds nothing. Only
otherwise does it open `/filament/details/<id>` and click "Add to My Spools" once.

## Rulings

- R-7228-1: "The owner has a record" means the My Spools page offers "Export slicer
  profile". `export` already relies on this signal, and ruling 38 of
  `2026-11-18-profile-fetch-worker.md` reads its absence as "not in My Spools".
- R-7228-2: A 404, or no export button, on the My Spools page means no record. The step
  then adds one. An unknown filament is still `tdfp_not_found`, from the filament page.
  The real site's answer to `/my/spools/details/<id>` for a filament with no record was
  not measured (#7547): the driver has no access to the live site outside the owner's
  login, and a test may not write to it. The replica answers `<h1>No spools</h1>` with
  status 200 (`no_record_status` 404 for the 404 case). What the code does for each
  answer: a 404 adds a record at once and logs "has no My Spools record (404)". A page
  with no export button waits `RECORD_WAIT_MS` (5 s), adds a record, and logs that the
  record check timed out. A page slower than 5 s is the same case: the step adds a
  duplicate record and logs the same timeout line, so a later duplicate is easy to trace.
  Why this is safe: each answer ends in at most one added record, never a lost one. A
  duplicate costs one extra 1000 g record that the owner deletes by hand (R-7228-3), and
  `export` still works with several records. Any other answer (a redirect to `/login`,
  Vercel's check) is handled before the check, as on every other page.
- R-7228-3: The fetch removes no record. Records made by earlier fetches were deleted by
  hand on 2026-11-23. When several records exist, `export` keeps using the per-filament
  page, so 3DFP picks the record.
- R-7228-4: The test replica now shows "Add to My Spools" every time, as the real site
  does, so the old behaviour fails `test_add_clicks_add_to_my_spools_once`.
- R-7228-5: The check waits up to `RECORD_WAIT_MS` (5 s) for the export button, so a
  filament with a record goes on at once. One with no record waits the full 5 s, the
  old fixed settle: the page has no other mark to wait for. That happens once per
  filament.
- R-7228-6: The filament-intake skill's `3dfp.mjs add` reads the My Spools page first
  too, so the manual step makes no duplicate records either.
