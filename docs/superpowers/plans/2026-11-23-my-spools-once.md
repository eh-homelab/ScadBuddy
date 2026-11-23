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
- R-7228-3: The fetch removes no record. Records made by earlier fetches were deleted by
  hand on 2026-11-23. When several records exist, `export` keeps using the per-filament
  page, so 3DFP picks the record.
- R-7228-4: The test replica now shows "Add to My Spools" every time, as the real site
  does, so the old behaviour fails `test_add_clicks_add_to_my_spools_once`.
