---
name: filament
description: Manage ScadBuddy's own filament inventory - spool types, spools, tags, the ledger, suppliers and invoices - and import a purchase invoice (a PDF or an image) into it. Use when asked to import, read or confirm a filament invoice, to add purchased filament, to cost a spool, to look up a Bambu colour code, to match an invoice line to a spool that already exists, or to explain why a spool has no cost. ScadBuddy owns spools; Bambuddy only holds a copy.
---

# Filament inventory and invoice import

ScadBuddy (SB) owns the spool inventory. Bambuddy (BB) keeps a copy. The setting
`filament_sync` gives each of six data types (`spools`, `weights`, `tags`, `suppliers`,
`locations`, `presets`) one direction: `off`, `sb_to_bb`, `bb_to_sb` or `two_way`. SB
writes BB only for a type set to `sb_to_bb` or `two_way`. The sync pass never sends
`tags`: tags reach BB through the spool refresh's tag drain, so `tags` alone set to push
gives `nothing_pushes`. The defaults pull `spools`,
`weights`, `tags` and `presets` and leave `suppliers` and `locations` off
(`backend/scadbuddy/core/settings.py` `FilamentSync`; plan
`docs/superpowers/plans/2026-11-18-filament-bb-sync.md`, Decision 3). Override mode, AMS
slot setup write BB whatever `filament_sync` says.

## Sync with Bambuddy

- `filament_sync_status` reads how the sync stands: the directions, the last pass, its
  error, and each item whose last write to BB failed or was held back. Use it to explain
  why a spool is not in BB. An error `name_taken` means another BB row has the item's name,
  so SB never sent it. `skipped` says why no pass runs: `no_bambuddy` or `nothing_pushes`.
  `last_ok_at` is the last pass with no error, a skipped pass too, so do not report it as
  the last sync (plan Decision 19; `agent/src/tools/filament.ts`).
- `filament_sync_run` asks for a full pass now. It writes BB, so it needs approval. It
  answers `started: true` at once; read `filament_sync_status` afterwards. A 409
  `no_bambuddy` or `nothing_pushes` means there is nothing to run: say so, and do not
  change `filament_sync` for the person.
- `instance_changed: true` in `filament_sync_status` means the links belong to another
  Bambuddy than the one set up now (its URL changed), so the sync and the pull are stopped.
  Call `filament_sync_reset_links` with `expect_kept: true` first: it reads the configured
  Bambuddy's spools, and when it holds every linked spool as ScadBuddy last saw it (the
  same Bambuddy at a new address), the reset deletes nothing and answers `kept: true`: the
  links now belong to it (plan Decisions 193-199, #6845). When it cannot prove that
  (Bambuddy could not be read, or it is another one), it answers 409 `proof_changed` and
  deletes nothing. The dry run (`GET /filament/sync/reset-links`) says `changed_since_pull:
  true` when Bambuddy holds every linked spool, at least half the links have a time on both
  sides, and every one ScadBuddy could compare (a time on both sides) was edited there since the
  last pull: then say that is why there is no keep, because ScadBuddy cannot prove it is
  the same Bambuddy. Only then call it with `expect_kept: false`, which reads no Bambuddy and
  deletes the links, intents and error rows of that old instance (`kept: false`). ScadBuddy's
  filament presets' Bambuddy ids are links too (`presets` in the counts, #7176): when they are
  the only links nothing can prove the same Bambuddy, so the reset deletes them, and the
  preset mirror then creates each preset on the new Bambuddy. Either
  way it asks for a full pass, and it never deletes an SB spool, type, supplier or
  location. It writes, so it needs approval. Pass `confirm_instance` as the status's
  `instance`; a 409 `confirm_mismatch`, `same_instance`, `nothing_to_reset` or
  `proof_changed` deleted nothing.
  Before you ask for the delete, tell the person that when the Bambuddy is another one, a spool is
  linked again only by its RFID tag, so an untagged spool can end up twice (plan Decisions
  165-170).
- Child data types (weights, presets, suppliers per spool, a spool's location) move only
  on spools that are linked to a BB spool already (plan Decision 16).
- A spool deleted in BB comes back under `sb_to_bb` or `two_way` with `spools` set to
  push (plan Decision 13).

Sources, relative to the root of
[eh-homelab/ScadBuddy](https://github.com/eh-homelab/ScadBuddy):

- `docs/superpowers/specs/2026-11-14-filament-management-design.md` ("filament
  spec"): §5.2 types, §5.3 spools, §5.4 tags, §5.5 ledger, §5.8 purchasing, §9
  the invoice import.
- `docs/superpowers/plans/2026-11-18-filament-invoice-import.md` (#6267): the
  decisions of the import, and "Decisions made while building".
- `docs/superpowers/plans/2026-11-18-profile-fetch-worker.md` (#6625): the 3DFP
  profile-fetch worker, for "Profiles".
- `docs/superpowers/plans/2026-11-22-3dfp-search-add.md` (#7025): the 3DFP search
  and add, for "Find or add the 3DFP id", and its rulings R-7025-1 to R-7025-13.
- `.claude/skills/filament-intake/SKILL.md`: the owner's rules for receipts. It
  is the source of the diff, cost and colour-code rules below. It still drives
  the BB-era scripts (see "Profiles").
- Bambu Studio's colour table, `filaments_color_codes.json`, from the
  [BambuStudio](https://github.com/bambulab/BambuStudio) profiles. SB reads its
  copy at `BBL/filament/filaments_color_codes.json` under `slicer_profiles_dir`
  (`backend/scadbuddy/filament/colour_codes.py`).
- `backend/openapi.json` for every route named below (all under `/api/v1`).

## The data

- A **type** is a product: brand, material, subtype, colour (`#RRGGBBAA`),
  presets per printer model and nozzle, default cost per kg (filament spec §5.2).
- A **spool** is a physical spool. Every value is set on its type, and the spool
  can override it. A spool with no type is "needs type" (filament spec §5.3).
- A **tag** is an RFID or NFC identity bound to one spool at a time (filament
  spec §5.4).
- The **ledger** (`spool_events`) is the only source of grams left (filament
  spec §5.5). Never write grams left on a spool by hand.
- An **invoice** has lines. A line names a supplier product and a type. It
  creates spools, or costs spools that already exist (`update_spool_ids`).
  Confirming an invoice does that in one transaction (filament spec §5.8).

## Import an invoice

The person presses "Import with the assistant" on Filament, Invoices, on an invoice draft that has a
file. The agent route `POST /api/v1/ai/filament/invoice-import` starts this
session. The session's first message holds the invoice file, as an image or as a
PDF document block, and the draft's id. The route refuses a confirmed invoice
(409), an invoice with no file (422), and an invoice that a live session already
imports (409 with that session's id). The draft's `import_run_id` is the session
id. A session that is idle is waiting for the person; it is not finished.

Read the file in the first message. Do not look for it with a tool.

Tools, in the order you use them. A candidate spool has no invoice line. An
uncosted one has an empty own cost too; a costed one has its own cost:

1. `filament_invoice_get`: the draft as it stands.
2. `filament_bambu_colour` with the five-digit `code` of a Bambu SKU: `name`,
   `rgba`, `fila_type`, `fila_id`. A 404 means the table does not have the code.
   Look the colour up in the catalog (step 3) before you ask the person. A 503
   means this install has no table. Never invent an RGBA.
3. `filament_catalog_list` with kind `colours` and `q`, for a line that makes a
   new type: the maker and the colour name, for example `polymaker galaxy black`
   (every word must match a row's name, maker, `material_name` or `#RRGGBBAA`;
   `GET /api/v1/filament/catalog/{kind}` in `backend/scadbuddy/api/filament.py`,
   #6907). For a Bambu Lab line, only when step 2 gave no colour. When exactly
   one row fits the line by maker, name and material, take its `rgba` as it is
   (a translucent row keeps its alpha) and its `extra_colours`. Ask the person
   only when no row fits or more than one does (#6912).
4. `filament_invoice_candidates`: for each line, the spools whose own cost
   is empty and which match the line's type (or proposed type) by material and
   RGBA exactly, and the spools that have a cost already. A costed spool names
   its `invoice_line_id`, if it has one. Archived (used-up) spools are listed
   too, with `archived: true`. A line may fill an archived spool: the spool
   takes its cost and invoice line, and stays archived. A spent spool was still
   bought, so fill it when the invoice is that purchase.
5. `filament_invoice_upsert`: write the draft (the header, the lines, each
   line's type and `update_spool_ids`). Edit the draft you were given. Never make
   a second invoice.
6. `filament_invoice_confirm`: only after the person says yes to a summary of what
   will be created and updated. It needs approval.

### Rules

1. **Diff before you create.** Call `filament_invoice_candidates` for every
   line first.
   - A spool with no cost that matches a line is that purchase. The line
     updates it. An RFID tag often created that spool (`data_origin:
     rfid_auto` in BB).
   - Offer uncosted candidates first, then costed ones.
   - A costed spool with no `invoice_line_id` may be this purchase. The
     Bambuddy import gave most spools a cost. Offer to link it to the line.
     Show the person its old cost per kg and the new one. Put it in
     `update_spool_ids` only when the person says yes. The confirm replaces its
     cost and writes an `edited` event with both costs (#6700).
   - A spool that has an `invoice_line_id` belongs to another invoice. Never
     offer it. The confirm refuses it (409).
   - A line of quantity N that covers one uncosted spool and new ones is one
     update plus N-1 new spools.
   - When it is ambiguous, ask the person. Do not guess between "the same
     spool" and "a second spool".
   - "Uncosted" means the spool's own cost per kg is empty and it has no invoice
     line. A cost that only the type gives is a default, not a purchase.
2. **Cost per spool** is (line subtotal + that line's tax + that line's share of
   shipping) ÷ quantity.
   - Shipping is shared over the lines by value (`allocate` and `unit_cost` in
     `backend/scadbuddy/filament/invoice_money.py`).
   - The confirm computes the cost. Show the person the same number.
   - Use the tax that the invoice prints for each line.
   - When the invoice prints only a total tax, the rate is total tax ÷ subtotal.
     Apply it to each line. The line records its tax basis, for example
     "receipt-wide 7.0%".
   - A bundle that splits into N spools is quantity N.
3. **No SKU on the invoice:** use the product name as the SKU. Never borrow a SKU
   from another invoice.
4. **Bambu lines:** the colour code on the invoice (for example "Misty Blue
   (30108)") gives the exact RGBA through `filament_bambu_colour`. Tag linking
   needs an exact material and RGBA match, and a translucent colour keeps alpha
   `80`. Take the RGBA from the table, never from a swatch.
   - `GFA17` (PLA Translucent) is missing from BB's `/cloud/builtin-filaments`
     list. It is a real filament. A missing entry there does not make the line
     wrong.
   - A multi-colour silk's subtype follows the SKU: `A05-M*` is Dual Color and
     `A05-T*` is Tri Color.
5. **Other brands:** propose a new type with its 3dfilamentprofiles.com (3DFP)
   id set as `tdfp_id`. Find the id with `filament_tdfp_search`, never with a
   web search (see "Find or add the 3DFP id"). Its RGBA is the catalog row's
   (tool step 3); use the 3DFP hex only when no catalog row fits. Confirm starts the preset fetch
   for each new type that has a `tdfp_id` (see "Profiles"). Without a `tdfp_id`
   no fetch starts.
6. **Never confirm for the person.** Show the totals, the spools to create, and
   the spools to update. Wait for a yes.
7. If the person corrects a line in the chat or on the page, edit the same draft.
   Read it again with `filament_invoice_get` first. The person may have changed
   it on the page.

### Check at the end

- The invoice's lines add up to its total, within the confirm tolerance. Confirm
  refuses a larger difference (409).
- No spool from the invoice is missing a cost.
- Every new type has a colour.
- Every new type has a preset for each nozzle of each printer model (filament
  spec §9, step 8). Read `filament_type_profile_fetch` for each new type after
  the confirm (see "Profiles"), and report each gap to the person.

## Find or add the 3DFP id

The profile-fetch worker is the one process with 3DFP's browser and login, so it
also searches 3DFP and adds a filament that 3DFP lacks (#7025; plan
`docs/superpowers/plans/2026-11-22-3dfp-search-add.md`). Use these tools, not a web
search or a fetch of the site: 3DFP's bot check refuses those.

1. **Search.** `filament_tdfp_search` with the `brand`, and the `material`,
   `material_type` and `colour` you know. It answers the site's first page:
   each candidate's `tdfp_id`, brand, material, material type, colour `name`,
   `hexes` and `nominal_weight_g`. The site filters only by its text (the brand,
   the type and the colour); the material only ranks the candidates. Check a
   candidate's brand, material, type and colour before you take its id. A name
   that ends in a weight, as "BRONZE (250g)", is that spool size only (R-7025-3).
2. **No match is not proof.** 3DFP's text index misses some brands
   (`.claude/skills/filament-intake/SKILL.md`, Common mistakes). Search again with
   fewer words (the brand and the colour) before you decide that 3DFP lacks it.
3. **Add only with the person.** When no candidate is the filament, offer
   `filament_tdfp_create`. 3DFP is a shared public database, so the tool asks for
   approval first. Send the brand, material and material type as 3DFP names them
   (a candidate of the same brand shows them), the colour name, every hex, the
   nominal weight, and the identifiers you have: `mpn` (the SKU), `gtin`, `asin`,
   `manufacturer_url`, `empty_spool_weight_g`, `td`. ScadBuddy stores none of
   them (R-7025-6). Send `type_id` to set the new id on a type that has none.
4. **Read the answer.**
   - `created`: the new `tdfp_id`, set on `type_id` when sent
     (`type_tdfp_id_set`). `not_sent` names the identifiers the form had no
     field for.
   - `duplicate`: nothing was saved. 3DFP's own duplicate list held a filament
     of the same brand, material and type with a close name or colour (R-7025-5).
     Show the person `likely` and `duplicates`. Take that id when it is the
     filament. Call again with its id in `not_duplicates` only when the person
     says it is another filament.
   - 422 `no_defaults`: 3DFP has no defaults for that brand, material and type,
     so the worker does not add it (R-7025-4). 422 `option_missing`: the form
     offers no such brand, material or type. Tell the person; do not guess
     another name.
   - 502 `not_created`: Save Filament did not open the new filament's page. The
     filament may exist. Search again before any second create (R-7025-7). Treat
     any 502 whose detail says the filament may exist the same way. A failure
     before the save (422, 503 `not_started`, another 502) made nothing; the same
     call may be sent again (R-7025-11).
   - 503 `no_worker` or `not_started`: the worker is not running, or it is busy
     with a preset fetch. Try again later.

## Profiles

A worker fetches a type's slicer presets from 3DFP. 3DFP sits behind a Vercel bot
check, so the worker drives a real Chromium. It exports one Bambu Studio preset per
printer model that SB's printers have and per nozzle in the setting
`profile_fetch_nozzles` (default 0.2, 0.4, 0.6, 0.8). SB keeps each one as the
type's own SB preset of its key (`sb-<id>`, #7176), sets them as the type's presets
for those keys, copies them to BB's local presets while the setting
`filament_preset_mirror` is on, and sets K values
from the printers' K tables where one matches exactly. An old K value of a fetched
key that the new preset does not match is removed, and the run's `notes` name it.
The fetch writes and removes only K values an earlier fetch set (`source`
`profile_fetch`): a K value a person, the fill or a printer calibration set stays, and
the `notes` name each one it kept (README "Profile-fetch worker").
The fetched keys replace the type's presets for those keys. The type's other presets stay. A spool's own presets
still win.

1. **Set the id.** The fetch needs the type's `tdfp_id`: a positive integer below
   2^31. Set it with `filament_type_upsert`, or in the invoice line's proposed
   type. A type without it gets 422 `no_tdfp_id`.
2. **Confirm starts the fetch.** The invoice confirm starts one for each new type
   that has a `tdfp_id`. To fetch by hand (an older type, or after a failure), use
   `filament_type_fetch_profiles`. It writes BB's local presets, so it asks for
   approval first. A start while a fetch runs joins it (`joined: true`).
3. **Read the result.** Call `filament_type_profile_fetch` with the type's `id`. It
   answers the last `run` (`status`: `running`, `imported`, `partial` or `failed`;
   `missing`, each key with its reason; `notes`; `error`) and `gaps`. How long a run
   takes was not measured on the real site. The README gives the bounds: 20 min per
   export attempt, and a queued export may wait up to 6 h because the worker exports
   one run at a time. Read again, and do not start a second fetch.
4. **Report the gaps.** `gaps` is every printer model and nozzle that the type has
   no preset for now. It can list keys even after an `imported` run, for example
   when a printer model was added after it. Name each gap and its reason to the
   person. Do not hide a `partial` run, and do not set a preset by hand to
   hide a gap.
5. **Know the refusals.** 422 `no_tdfp_id` (set the id), 422 `no_printer_models`
   (no printer has a model: tell the person to set one in Settings → Printing →
   Printers), 503 (the fetch could not be started: say so, and try again later).
   A run that no worker serves ends `failed` with "no profile-fetch worker picked
   the fetch up". Tell the person; the worker is a Deployment they run. A run
   still open after 25 h ends `failed` with "the fetch did not end within 25 h;
   start it again": offer to start it again. A run
   whose export waited 6 h in the queue ends `failed` with "no profile-fetch
   worker started the fetch within 6 h: none is running, or it is busy with other
   fetches". Tell the person to check both: whether the worker's Deployment runs,
   and whether other fetches (an invoice's many types) kept it busy. Do not start
   the fetch again until they know which.
6. **Versions and the copy in BB.** Each key is the type's `3dfp` SB preset (#7176,
   plan `docs/superpowers/plans/2026-11-23-scadbuddy-filament-presets.md`). A fetch
   whose file changed makes a new version of the same preset, and the run's `notes`
   name the keys that changed; the same file changes nothing. A BB preset of the
   same name never stops a fetch. The copy in BB is the mirror's: it rewrites the
   same BB preset in place, so the id BB's spools and AMS slots name stays. A copy
   BB refused is the preset's `mirror_error` and never fails the run; the next
   refresh of the kept presets sends it again. BB refuses every copy while its auth
   is on (an API key cannot write presets). Tell the person; never write a preset to
   BB by hand.

The fetch only reads 3DFP and writes SB's presets; the mirror copies them to BB. Never start a
fetch to test a change, and never write a preset to BB by hand to fill a gap.
For the BB-era scripts (`intake.py`, `3dfp.mjs`), use
`.claude/skills/filament-intake/SKILL.md`.

## Where a fact comes from

- The 3DFP search page's layout (a table whose header names Brand, Material,
  Type, Color and RGB, a first page of 25 rows, a "(250g)" weight in the name)
  was read in one read-only search for "SUNLU Silk Bronze" on 2026-11-22. The add
  form's fields, "Defaults found", the duplicate list and the page Save Filament
  opens come from the script posted on
  [#7025](https://github.com/eh-homelab/ScadBuddy/issues/7025), run on 2026-11-22.
  The worker's tests drive a local replica of both pages, never 3DFP.

- The 3DFP bot check, its 429 and 403 answers, and the `GFA17` gap are stated
  in `.claude/skills/filament-intake/SKILL.md`, which gives a date only for the 403
  (since 2026-11-01) and no method for any of them. Neither #6267 nor #6625 measured them against the real site: the worker's
  tests drive a local replica, never 3DFP. Check one before you rely on it.
- The worker's behaviour (what it exports, the run statuses, the gaps, the
  binding rule for preset names) is in `docs/superpowers/plans/2026-11-18-profile-fetch-worker.md`
  ("Decisions this plan makes" and "Decisions made while building", #6625) and
  the code it describes: `backend/scadbuddy/filament/profile_fetch.py`,
  `backend/scadbuddy/profile_fetch/`. The routes are
  `POST` and `GET /api/v1/filament/types/{type_id}/profile-fetch`.
- The candidate rule ("uncosted", and a costed spool with no invoice line) and
  the 404 and 503 answers of the colour route are SB's code and its tests:
  `backend/tests/filament/test_invoice_candidates.py`,
  `backend/tests/filament/test_invoice_confirm.py` and
  `backend/tests/filament/test_colour_codes.py`.
