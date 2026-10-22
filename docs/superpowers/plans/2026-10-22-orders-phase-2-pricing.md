# Orders Phase 2: Pricing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A line item priced from the sliced file's own facts (#4250, epic #4248): the shop's price book (its filaments with a cost per kilogram, and a pricing policy of machine rate, setup, labour, markup and rounding), a `slice` quote the `order_item_quote` operation computes from the plates' grams and seconds that `slicing/slice_info.py` `read_sliced` reads out of the sliced 3MF, kept with the sliced file it was read from, and a rough estimate from a render's part volumes before anything is sliced. The accepted quote is a snapshot: a later change to the price book never moves it. Nothing here prints (§5.4 is phase 3, #4251), and nothing here matches a filament record to Bambuddy's spools (§8.3 is phase 5, #4253).

**Architecture:** The price book is one new table, `filaments` (`orders/pricebook.py` over `OrderStore`'s pool), plus six stored settings for the policy and the estimate factor (`core/settings.py`, `library/settings_store.py`, `api/settings.py`, the Settings page's Orders section, as `orders_currency` landed in phase 1). The arithmetic is one pure module, `orders/pricing.py`, on `Decimal`. The slice quote is an `OperationKind` on the `library` queue, `orders/operations.py` `OPERATION_KINDS` (Ruling 1): its check refuses from the rows alone, its run renders when the item has no matching output, slices through the print dialog's own background slice (`bambuddy/preview.py` `start_preview`, Ruling 2), follows the slice, keeps the sliced bytes as a blob the quote holds a ref on (Ruling 3), reads each plate with `read_sliced`, prices, records the quote through the lifecycle and publishes `order_item.changed`. The route is `POST /orders/{id}/items/{item}/quote` through `api/operations.py` `run_operation` (202, then the quote); the manual quote of phase 1 stays at `POST …/quotes`. The frontend gets the price book on the Settings page, "Quote from a slice…" on the order page through the print picker in a quote mode, and the estimate in the add-to-order dialog; the agent gets the price book, the quote in both modes and the estimate.

**Tech Stack:** Python 3.12, psycopg 3, FastAPI, pydantic 2, Temporal (the `library` queue's worker, in the API process); React 19, Vite, msw, vitest, Playwright; TypeScript on the Claude Agent SDK.

**Spec:** `docs/superpowers/specs/2026-10-18-orders-design.md` §3.3 (`colours[].filament_id`), §3.4, §3.6, §4's rows for the quote and the price book, §5.1, §5.2, §5.3, §7.3's price book and quote tools, §11 phase 2. Not §5.4 (phase 3).

## Global Constraints

- Everything is Postgres, in new migration files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md "Migrations"). Phase 1's `order_item_quotes` already carries every §3.4 column (phase 1 plan Ruling 1), so this phase adds only `filaments`.
- Money is integer minor units with the order's `currency`; rates and factors are decimal strings parsed with `Decimal`; nothing is a float (§3.6). `read_sliced`'s grams are floats as the slicer wrote them and are converted to `Decimal` through `str()` once, at the line.
- A quote is a fact about one sliced file: the row carries the file's sha256 and the key it is kept under, and nothing recomputes a recorded quote (§3.4, §5.2's last paragraph).
- The price book in force is copied into each quote's lines (§5.1): a line carries the rate it was priced at, never a reference to the book.
- A slice quote is recorded only once the slice has finished and every plate and slot read: an unknown plate or a slot at 0 g fails the operation and records no quote (§5.2 step 3).
- The run writes nothing of its own before the sliced file's blob, its ref and the quote row, in that order (Ruling 3): the render it may submit is an ordinary render job, the slices ordinary background slices, each its own record. The check's one write is the item's filaments, and only when the body changes them (Ruling 6).
- No body carries an author, a principal or a status (`extra="forbid"`; spec Ruling 12). `created_by` comes from `core/authorship.py` `current_author()` as phase 1's routes read it.
- Bus events carry ids only; the quote publishes `order_item.changed` (`core/events.py` `OrderItemChanged`) through the lifecycle, as the manual quote does.
- Every `/api/v1` operation this phase adds has an agent tool or a `coverage.ts` entry, or `agent/test/coverage.test.ts` fails. The generated API files are never committed.
- Frontend mocks go under `src/mocks/features/orders/`, each file exporting `handlers` (and `reset`).
- Every measured or external fact in a docstring or doc cites its source (#259).

## Rulings

Where the spec leaves a choice open, or phase 2 needs something another phase owns, decided here:

1. **The slice quote is an `OperationKind`, not a workflow of its own.** Spec §5.2 asks for "the `order_item_quote` operation, on the `library` queue, as activities of one `OrderQuote` workflow (the same shape as `Arrange`)". `Arrange` is a `render_jobs` row of kind `arrange` run by `TemplatePipeline` (`workflows/pipelines.py:936` `_arrange`), a shape built for a render's cache, projection and Jobs page, none of which a quote has. The §4.2 command shape already gives an operation everything the quote needs: the record (`operations`), the 202 and the follow (`GET /operations/{id}`), the `Idempotency-Key` so a re-sent request joins the running one, the activity row (`operations/activity.py` `row_of`, kind `operation_kind_key("order_item_quote")`) and a run that heartbeats (`workflows/operation_activities.py:99` `_heartbeating`). So `order_item_quote` is a kind of `orders/operations.py`'s `OPERATION_KINDS` (`queue="library"`, `run_attempts=1`, `run_timeout=QUOTE_TIMEOUT` of 30 minutes, `answers_running=True` so the route answers 202 as soon as the record is written, `where="the item's quotes"`), declared as `operations/kinds.py:34-70` `OperationKind` declares them. Its subject is `order_item:<id>` (§5.4's subject, so phase 3's print and this quote are the same subject's work). Task 10 corrects §5.2's wording.
2. **The quote slices exactly what the print dialog would print, with the dialog's own choices.** Spec §5.2 step 2 says "slice the output's 3MF with the item's colours mapped to the chosen filaments' slicer profiles". On the Bambuddy slicer a slice needs a spool per slot: `PrintRunRequest.filament_plan` (`bambuddy/print_run.py:192`, "a slot with no spool has no filament preset, which the resolver reports as an error"), and the resolver's preset catalogue comes from those spools. Rather than a second resolver, the quote request carries the print dialog's own body, `printer_id`, `filament_plan` and `choices` (`PrintRunRequest`'s fields; `choices.filament_overrides` names a preset per slot where the spool's is not wanted, `bambuddy/resolver.py:105`), plus `filaments: {slot: filament_id}` naming the price-book filament of each slot, and the run calls `start_preview_plates`, a new function of `bambuddy/preview.py` beside `:95` `start_preview` (the call `POST /outputs/{id}/preview-slice` makes, `api/printing.py:1042`), with `subject="order_item:<id>"`. `start_preview` slices one plate only, `planned.plates[0]` ("the first plate when every plate prints", `preview.py:108-109`, `:134`), which is right for the dialog's toolpath and wrong for a price: a quote is every plate the item prints. `start_preview_plates` runs the same `prepare_run` and `plan_run` with `all_plates=True` forced onto the request (so `plan_run` plans every plate the output lays out, `print_run.py:1484-1486`; the body's `plate_id`, `plate_ids` and `all_plates` are ignored and the response says so), then starts one slice per `PlatePlan` of `planned.plates` through the per-plate code `start_preview` already has for its first (Bambuddy: `slice_request(plate.plan, plate.plate_id)` behind `reusable_slice` and `record_preview`, `preview.py:155-173`, one `PreviewSlice` row per job and subject; native: `_start_native(..., plate.plate_id, ...)`, `preview.py:177`, whose `native_preset_key` carries the plate, so one subject holds one `native_slices` row per plate), and answers a `PreviewStarted` per plate, in plate order. `start_preview` itself is unchanged and is refactored only so the two share that per-plate code. So whatever the dialog's route can slice on the stack, the quote can, on either slicer (`start_preview_plates` branches on `slicer_backend` as `start_preview` does). The price-book filament and the spool are two facts in phase 2: the spool is what sliced, the filament is what priced, and nothing checks that the spool is of that filament (that check is phase 5's, once §8.3 imports spools as filament records keyed by `bambuddy/farm.py:187` `_filament_key`). `filaments` is written onto the item's `colours[].filament_id` (§3.3) by the check, before the run, so the item says what it was priced with. Task 10 corrects §5.2 step 2.
3. **The sliced file is kept in the directory-shaped blob store, under a key of its own, held by a `blob_refs` row.** `StoreBundle.content` is `None` on the local backend (`store/factory.py:36-39`), so a content-addressed put is not available everywhere; `StoreBundle.blobs` (the `BlobStore` Protocol, `store/__init__.py:24`: `dir_for(key)`, `publish(key, scope=)`, `exists`, `remove`) is, on every backend. The run writes the bytes as `sliced.3mf` under key `quote-<sha256>` (`dir_for`, then `publish` with the template's `BlobScope`), adds `blob_refs (key, holder_kind="order_item_quote", holder_id=<quote id>)` through `store/refs.py` `BlobRefs.add`, and only then inserts the quote row, in that order: the quote id is made first (`uuid.uuid4().hex`; `blob_refs` has no foreign key to its holder), so `sweep_blobs` (`store/__init__.py:85`, which removes every unreferenced key past its grace) never sees the key unreferenced once a row names it, and a crash between any two steps leaves at most a blob with no ref (the sweep's) or a ref with no row, never a row whose bytes can go. A ref with no row (a crash between the ref and the row; the kind has `run_attempts=1`, so nothing retries) is reaped by `housekeeping_reap_quote_holds`, a sweep on the prune Schedule (`workflows/housekeeping.py` `PRUNE_SWEEPS`, behind `workflow.patched("housekeeping-reap-quote-holds")` as each later sweep is) shaped like the output-hold reaper (`REAP_SWEEP`, `main.py:591`, #1007): every `order_item_quote` holder with no `order_item_quotes` row whose `blob_refs.created_at` is older than `QUOTE_TIMEOUT` is dropped, the grace being the run that may still be about to insert its row. A run that fails after the add drops its own holder before it raises. `sliced_blob_key` holds the key, `sliced_sha256` the digest. The refs follow §3.4: recording a quote drops the holder row of the item's older unaccepted quote (`BlobRefs.drop_holder`), accepting one drops every other quote's, cancelling the item or closing the order drops them all; the rows stay. Two quotes of the same bytes share the key (`add` is `ON CONFLICT DO NOTHING`), and the sweep frees it once the last holder is gone. The Bambuddy path's bytes come from `bambuddy/client.py:1049` `download_library_file(sliced file id)`; the native path's from the `native_slices` row's `sliced_blob_key` (`slicing/store.py:28` `NativeSlice`, read with `NativeSliceStore.get(slice_id, subject)` :222) through the content store, which the native slicer requires. The spec's "kept as a `library` blob" names the kind; the store's directory key is how it is kept.
4. **The price book is a `filaments` table and six stored settings.** `filaments` (`id`, `material`, `subtype`, `brand`, `colour_name`, `colour` (`#RRGGBB`, `NOT NULL`: every filament has a colour, and the unique index needs one), `cost_per_kg` (`numeric(12,4)`), `density_g_cm3` (`numeric(6,3)`, nullable), `archived_at`, `created_at`, `updated_at`), unique on `(material, subtype, brand, colour)` upper-cased, the identity `_filament_key` groups spools by. A record prices and never slices: the slicer's filament preset per slot comes from the spool the dialog's `filament_plan` names (or `choices.filament_overrides`, Ruling 2), on either slicer, so the record carries no preset. The policy is `pricing_machine_rate_per_hour`, `pricing_setup_fee`, `pricing_labour_per_item`, `pricing_markup_percent` (decimal strings, default `"0"`), `pricing_rounding` (`to_minor_unit` | `to_major`, default `to_minor_unit`) and `rough_estimate_factor` (decimal string, default `"1.15"`), each a stored setting as `orders_currency` is (phase 1 Ruling 8; `core/settings.py:156`, `library/settings_store.py:565`, `api/settings.py:117`), `APPLIES` `live` (read at each quote). `GET /orders/price-book` answers the currency, the policy and the unarchived filaments in one body; `PUT /orders/price-book` takes the policy; `POST /orders/price-book/filaments`, `PATCH …/{id}`, `POST …/{id}/archive` keep the records. Spec §5.1 says "a Pricing tab of the Orders page" and the issue says a Pricing tab of Settings: it is the Settings page's existing Orders section (`frontend/src/pages/settings/fields.ts:11`), which already holds `orders_currency`, gaining the policy fields and a filaments editor. Task 10 corrects §5.1.
5. **The arithmetic is `orders/pricing.py`, pure, on `Decimal`.** `price(plates, filaments, policy, *, currency, quantity) -> Priced`: per slot one `filament` line (`grams × cost_per_kg / 1000`, grams summed over plates), one `machine_time` line (`print_seconds` summed over plates ÷ 3600 × rate), one `setup` line per plate (Ruling 2 slices every plate, so there is one per plate the item prints), one `labour` line, one `markup` line (the percentage of the sum of the others); each line's `amount_minor` is its `Decimal` amount quantised to the currency's minor unit with `ROUND_HALF_UP` ([Python `decimal`](https://docs.python.org/3/library/decimal.html#rounding-modes)) once; `unit_cost_minor` is the sum of the filament and machine-time lines, `unit_price_minor` the sum of all lines; with `to_major` the unit price is rounded up to the next whole currency unit (`ROUND_CEILING` on the major amount) and the difference is one more line, `kind="rounding"` (a new `QuoteLineKind`, `orders/models.py:45`; `amount_minor` the difference, 0 when there is none, so the lines always add to `unit_price_minor`); `total_minor = unit_price_minor × quantity`. The minor unit's exponent is 2 unless the currency is in `CURRENCY_EXPONENTS` (the zero-decimal codes `BIF CLP DJF GNF ISK JPY KMF KRW PYG RWF UGX UYI VND VUV XAF XOF XPF` and the three-decimal codes `BHD IQD JOD KWD LYD OMR TND`, from [ISO 4217's current codes list](https://en.wikipedia.org/wiki/ISO_4217#Active_codes)), the same table `frontend/src/lib/money.ts` derives from `Intl.NumberFormat`'s `maximumFractionDigits`. `quantity` on a `filament` line is the grams as an integer only when whole; the line's `quantity` field is phase 1's integer, so the grams go on the label (`"PLA Basic, 42.3 g"`) and `unit_minor` is the price of a gram's thousandth rounded at the line: Task 2's test fixes the examples.
6. **The item's filaments are the item's, and changing them drops the quote.** `PATCH /orders/{id}/items/{item}` takes `filaments: {slot: filament_id | null}` and writes `colours[].filament_id`; it is allowed while the order is `draft` or `quoted` and, like a reference or quantity change (phase 1 Ruling 1), drops the accepted quote and returns the item to `draft` (§5.2: "changing the reference, the colours or the quantity makes the next quote a new row"). The write repins only when it changes something: `update_item` compares the body's slots with the item's `colours[].filament_id` and, when they are equal, writes nothing and drops nothing (today `update_item` treats any non-`None` field as a repin, `orders/lifecycle.py:583`; `filaments` is the one field that may arrive unchanged on every request, so it is compared first). A quote request's `filaments` goes through the same write in the check, so a quote whose filaments are the item's own leaves the item, its status and its accepted quote as they were, and one that changes them starts from an unpinned item.
7. **The render step reuses the output route's own writer.** When the item has no `output_id`, or the output is gone, or its `meta.json` `slug`, `model_version` or `inputs` differ from the reference (`library/outputs.py:131` `OutputMeta`), the run submits the reference's render with `RenderService.submit(slug, params, model_version=commit, inputs=inputs)` (`render/submit.py:210`; the commit is a resolved revision as `api/jobs.py:314` `_resolve_version` gives one), waits for the job by reading `render.store.read(job_id)` every two seconds under the heartbeat until its `state` is settled (`render/job_models.py:19` `JobState`: `done`, else the quote fails naming the job), then writes the output by calling `library/output_operations.py:88` `create_run` with `{slug, job_id, index: 0}` from inside its own run (the same process and queue; a kind's run is a plain coroutine, and the output row and blob refs it writes are the ones `POST /outputs` writes), and sets the item's `output_id`. A matching output is reused as it is.
8. **A quote request is refused from the rows alone, in the check.** 409 `order_not_open` unless the order is `draft` or `quoted` (the lifecycle's `_open_for_items`), 409 `item_not_open` for a settled item, 422 `filaments` when a slot of the item's `colours` has no filament after the body's are applied, or names an archived one, 422 `price_book` when the policy has no `machine_rate_per_hour` and no filament cost would price anything (the book is empty), 404 for an unknown order or item. The check's only write is the item's filaments, through Ruling 6's `update_item`, which changes nothing when they are the item's own. The dialog's own refusals (no spool for a slot, a preset Bambuddy lacks) come from `start_preview` in the run and are recorded as the operation's failure, as a pin's clone failure is (`library/operations.py`'s module docstring), with the `ApiError`'s detail as the failure text. On hold a quote is allowed: it advances nothing (phase 1 Ruling 14).
9. **The rough estimate is a read, never a record.** `GET /orders/estimate?output_id=&filament_id=` answers `{grams, filament_minor, currency, factor, parts}` from the output's geometry (`api/outputs.py:1134`'s `outputs.geometry(output_id, plate)` for plate 1, `render/geometry.py:207` `PartGeometry.volume_mm3`, only for a part that is checked and closed; a part with no volume is listed as unknown and the estimate says so), `density_g_cm3` of the filament (404 when it has none; §5.3 "gives none") and `rough_estimate_factor`: `grams = Σ volume_mm3 / 1000 × density × factor`, `filament_minor = grams × cost_per_kg / 1000` rounded as a line is. Nothing is stored; the response is labelled an estimate. The agent's `order_estimate` is `read`; the browser shows it in `AddToOrderDialog` beside a filament picker.
10. **Accepting a slice quote checks nothing about its file in phase 2.** Phase 1's `accept_quote` (quantity must match) stands; the digest check against what prints is §5.4's, phase 3. A `slice` quote whose file the sweep could have freed cannot exist while the quote is unaccepted and newest, or accepted (Ruling 3), so phase 3 finds the bytes.
11. **The agent's `order_item_quote` takes both modes.** `{order_id, item_id, manual?: {unit_price_minor, label?}, slice?: {printer_id?, filament_plan, choices, filaments}}`, exactly one of the two; `manual` posts `POST …/quotes` as today, `slice` posts `POST …/quote` through `agent/src/tools/command.ts:228` `command` (the idempotency key, the 202 follow, the running answer past the follow window), as the agent's other command tools do. The tier stays `write` (a quote records a price; accepting it is the outward step, phase 1 Ruling 14). `price_book_get` is `read`, `price_book_set` (the policy, a filament's create, patch and archive) `write`, `order_estimate` `read`.
12. **Tests slice with the fakes the print path has.** The Bambuddy path over the `respx` recordings `tests/api/test_print.py` uses (`slicer-presets.json`, the slice job and library file routes) with a sliced 3MF from `tests/support/sliced3mf.py` served as the download; the native path with `tests/support/fake_slicer.py`'s `slice_plate`. The pricing module is tested on literal plates, with no slice.

## Spec corrections

- §5.2's "as activities of one `OrderQuote` workflow (the same shape as `Arrange`)" becomes "as the run of the `order_item_quote` operation (`operations/kinds.py`), which gives it its record, its follow and its activity row" (Ruling 1). Task 10 edits §5.2.
- §5.2 step 2's "with the item's colours mapped to the chosen filaments' slicer profiles" becomes "with the print dialog's own choices (`printer_id`, `filament_plan`, `choices`), the slice `POST /outputs/{id}/preview-slice` makes, and the price-book filament named per slot; a spool and a filament record are matched only from phase 5 (§8.3)" (Ruling 2). Task 10 edits §5.2.
- §5.1's "edited on a Pricing tab of the Orders page" becomes "edited in the Settings page's Orders section" (Ruling 4). Task 10 edits §5.1.
- §3.4's "kept as a `library` blob" is kept as a directory blob of the blob store under `quote-<sha256>`, one per sliced plate (Ruling 3); the ref's name `order_item_quote:<id>` is the `(holder_kind, holder_id)` of `blob_refs`, taken before the row. Task 10 adds the keys and the order to §3.4.
- §3.6's line kinds gain `rounding`, the `to_major` difference (Ruling 5), so the lines of every quote add to its unit price. Task 10 edits §3.6.
- §5.2 step 2's one slice becomes one per plate the item prints (Ruling 2): a quote is the whole output's price. Task 10 edits §5.2.

## Review Focus

1. **Rounding happens once per line, and the totals add rounded lines**, the `to_major` difference being a `rounding` line of its own (Task 2 `test_each_line_rounds_once_and_totals_add_lines`, `test_to_major_rounds_the_unit_price_up_as_a_rounding_line`, `test_zero_decimal_and_three_decimal_currencies`).
2. **A later change to the price book never moves a recorded quote** (Task 4 `test_a_recorded_quote_keeps_its_rates`).
3. **An unknown plate, or a slot at 0 g, records no quote** and the operation fails naming the plate or slot, on any plate of the item, not only the first (Task 5 `test_an_unknown_plate_fails_the_quote`, `test_a_slot_at_zero_grams_fails_the_quote`, the failing plate being the second of two).
4. **An item never holds more than two quote refs:** its accepted quote's and its newest unaccepted one's; recording, accepting, cancelling and closing drop the right ones and the rows stay (Task 3 `test_quote_refs_follow_the_rules`).
5. **A re-sent quote request joins the running operation** rather than slicing twice (Task 5 `test_a_resent_quote_request_joins_the_run`, the same `Idempotency-Key` twice while the fake slice is still running).
6. **A quote request's filaments go onto the item and drop its accepted quote**, as a reference change does, and the item's own filaments sent again drop nothing (Task 3 `test_filaments_change_drops_the_quote`, `test_the_same_filaments_drop_nothing`).
7. **A rendered-on-demand quote sets the item's `output_id`** and reuses a matching output on the next quote (Task 5 `test_a_quote_renders_when_the_item_has_no_output`, `test_a_quote_reuses_a_matching_output`).
8. **The estimate is never a quote:** `GET /orders/estimate` writes nothing and the item's quotes are unchanged (Task 7 `test_the_estimate_records_nothing`).
9. **A multi-plate item is quoted on every plate:** a two-plate output gets both plates' grams and seconds and two `setup` lines, on both slicers (Task 5 `test_a_two_plate_item_is_quoted_on_both_plates`).
10. **The sliced file's ref is taken before its row**, so a crash between them leaves a ref the reaper drops, never a row whose bytes the blob sweep could free (Task 4 `test_the_ref_is_added_before_the_row`, `test_a_failed_run_drops_its_own_holder`; Task 3 `test_the_reaper_drops_a_holder_with_no_row`).

---

### Task 1: The price book: `filaments`, the policy settings and their routes

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filaments.sql`
- Create: `backend/scadbuddy/orders/pricebook.py`
- Modify: `backend/scadbuddy/orders/models.py` (`Filament`, `FilamentInput`, `FilamentPatch`, `PricingPolicy`, `PriceBook`)
- Modify: `backend/scadbuddy/orders/store.py` (the `filaments` CRUD beside the customers')
- Modify: `backend/scadbuddy/core/settings.py` (the six fields after `orders_currency` :156; `APPLIES` :640, each `live`), `backend/scadbuddy/library/settings_store.py` (`StoredSettings`, `SettingsPatch`, the descriptions, as `orders_currency` :565), `backend/scadbuddy/api/settings.py` (the view :117 and the patch)
- Modify: `backend/scadbuddy/api/orders.py` (`GET /orders/price-book`, `PUT /orders/price-book`, `POST /orders/price-book/filaments`, `PATCH …/{id}`, `POST …/{id}/archive`)
- Test: `backend/tests/orders/test_pricebook.py` (`requires_postgres`), `backend/tests/api/test_price_book.py`, `backend/tests/api/test_settings.py` (the six settings' defaults and validation)

**Interfaces:**
- Produces: the `filaments` table of Ruling 4 (`cost_per_kg numeric(12,4) NOT NULL CHECK (cost_per_kg >= 0)`, `density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0)`, `colour text NOT NULL CHECK (colour ~ '^#[0-9A-Fa-f]{6}$')`, a unique index on `(upper(material), upper(coalesce(subtype,'')), upper(coalesce(brand,'')), upper(colour))`, every column of it non-null or coalesced, so two records can never both pass it); `Filament` (the row, decimals as strings on the wire), `FilamentInput`/`FilamentPatch` (`extra="forbid"`; `cost_per_kg` and `density_g_cm3` as `Decimal`-validated strings), `PricingPolicy` (`machine_rate_per_hour`, `setup_fee`, `labour_per_item`, `markup_percent`: non-negative decimal strings; `rounding: Literal["to_minor_unit", "to_major"]`), `PriceBook` (`currency`, `policy`, `rough_estimate_factor`, `filaments`); `PriceBookReader.snapshot() -> PriceBook` in `pricebook.py` (the settings snapshot's six fields plus the store's unarchived filaments, read in one place so the quote and the routes agree); `OrderStore.insert_filament/update_filament/archive_filament/filament/filaments(include_archived)`. A duplicate identity is 409 `filament_exists` with the existing id. Settings: `Settings.pricing_machine_rate_per_hour: str = "0"` (validated as a non-negative `Decimal`), likewise `pricing_setup_fee`, `pricing_labour_per_item`, `pricing_markup_percent`; `pricing_rounding: Literal[...] = "to_minor_unit"`; `rough_estimate_factor: str = "1.15"` (a positive `Decimal`).

- [ ] **Step 1: Write the failing tests**: `test_a_filament_round_trips_with_decimals_as_strings`, `test_a_duplicate_identity_is_409` (same material, subtype, brand and colour in another case), `test_archiving_hides_a_filament_from_the_book`, `test_the_price_book_reads_policy_and_filaments_together`; in `test_settings.py` a `PATCH` of `pricing_markup_percent: "-1"` is 422 and `"12.5"` is saved.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders/test_pricebook.py tests/api/test_price_book.py tests/api/test_settings.py` (the `tests/api/` paths together, CLAUDE.md) → PASS; `ruff`, `mypy`.
- [ ] **Step 5: Commit** `feat(orders): the price book: filaments, the pricing policy settings and their routes (#4250)`

---

### Task 2: The arithmetic

**Files:**
- Create: `backend/scadbuddy/orders/pricing.py`
- Test: `backend/tests/orders/test_pricing.py` (no Postgres)

**Interfaces:**
- Produces: `CURRENCY_EXPONENTS: Mapping[str, int]` and `minor_exponent(currency) -> int` (Ruling 5); `class Plate(BaseModel)`: `index: int`, `print_seconds: int`, `filaments: list[PlateFilament]` (`slot`, `used_g: Decimal`), `blob_key: str | None = None`, `sha256: str | None = None` (the plate's own sliced file, Task 4; None on the literal plates the arithmetic is tested on), the shape stored in the quote's `plates`, one per plate sliced; `price(plates, filaments: Mapping[int, Filament], policy: PricingPolicy, *, currency, quantity) -> Priced` (`lines: list[QuoteLine]`, `unit_cost_minor`, `unit_price_minor`, `total_minor`; under `to_major` the last line is the `rounding` line, Ruling 5), raising `PricingError(slot=…)` for a slot with no filament and `PricingError(plate=…)` for a plate with no seconds; `estimate_grams(volumes_mm3, density_g_cm3, factor) -> Decimal` for Task 7.

- [ ] **Step 1: Write the failing tests**: the Review Focus 1 cases with literal numbers (two plates, two slots, `markup_percent` `"15"` (a percentage, not a factor: `rough_estimate_factor` `"1.15"` is the estimate's, Ruling 9), `USD`, `JPY`, `KWD`; two `setup` lines for the two plates); `test_a_slot_with_no_filament_is_an_error`; `test_the_lines_carry_the_rates_they_were_priced_at` (the label and `unit_minor` of each line).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders/test_pricing.py` → PASS; `mypy`.
- [ ] **Step 5: Commit** `feat(orders): quote pricing on Decimal, one rounding per line (#4250)`

---

### Task 3: Quote refs, the item's filaments and the slice quote's record

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py` (`record_slice_quote`, the refs in `record_manual_quote`, `accept_quote`, `cancel_item`, `cancel_order`, `close_order`; `update_item` gains `filaments`; `reap_quote_holds`)
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (`QUOTE_HOLDS_SWEEP`, its patch, in `SWEEPS` and `PRUNE_SWEEPS`), `backend/scadbuddy/main.py` (the activity), `backend/tests/test_housekeeping.py` and `test_housekeeping_replay.py` (the sweep runs behind its patch, and the recorded histories still replay, as the others are tested)
- Modify: `backend/scadbuddy/orders/store.py` (`insert_quote` takes `plates`, `sliced_sha256`, `sliced_blob_key`; `set_item_filaments`)
- Modify: `backend/scadbuddy/orders/component.py` (`OrdersComponent` gains `refs: BlobRefs`, the core's `store.refs`, so the lifecycle can drop holders)
- Modify: `backend/scadbuddy/api/orders.py` (`PATCH …/items/{item}` body gains `filaments`)
- Test: `backend/tests/orders/test_lifecycle.py`, `backend/tests/api/test_orders.py`

**Interfaces:**
- Produces: `Lifecycle.record_slice_quote(order_id, item_id, priced: Priced, plates, *, sliced_sha256, sliced_blob_key, by) -> Quote` (as `record_manual_quote`: the order open for items, the item not settled; the item `quoted` from `draft`; the audit row `quote.recorded`; then `refs.drop_holder("order_item_quote", <older unaccepted quote id>)` for the item's previous newest unaccepted quote, after commit); `accept_quote` drops every other quote's holder after commit; `cancel_item`, `cancel_order` and `close_order` drop every holder of the affected items' quotes after commit; `update_item(..., filaments: Mapping[int, str | None] | None)` writes `colours[].filament_id` and counts as a repin only when it changes a slot (Ruling 6); `QUOTE_HOLDER = "order_item_quote"`; `reap_quote_holds(refs, store, *, grace=QUOTE_TIMEOUT) -> int` in `orders/lifecycle.py`, Ruling 3's reaper, registered as `housekeeping_reap_quote_holds` in `main.py` beside `reap_output_holds` (`:591`) and added to `PRUNE_SWEEPS` behind its patch.
- Holder drops run after the transaction commits (the rows are the record; a ref dropped before a rollback would lose bytes a quote still names), through `BlobRefs` on the autocommit pool it owns.

- [ ] **Step 1: Write the failing tests**: Review Focus 4 and 6; `test_a_slice_quote_is_recorded_with_its_file` (the row's `plates`, `sliced_sha256`, `sliced_blob_key`, `source="slice"`); `test_a_patch_with_filaments_sets_the_slots` (API; an unknown slot is 422); `test_the_reaper_drops_a_holder_with_no_row` (a holder younger than the grace stays, an older one with no row goes, one with a row stays whatever its age).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders` then `uv run --frozen pytest tests/api/test_orders.py tests/api/test_orders_lifecycle.py` → PASS; `mypy`.
- [ ] **Step 5: Commit** `feat(orders): slice quotes keep their file by a blob ref; an item's filaments (#4250)`

---

### Task 4: The `order_item_quote` operation

**Files:**
- Create: `backend/scadbuddy/orders/operations.py` (`OPERATION_KINDS`, found like every feature's), `backend/scadbuddy/orders/quote.py` (the run's steps, each a function the tests call on its own)
- Create: `backend/scadbuddy/orders/activity.py` (`ACTIVITY_KINDS` with the `order.quote` kind's label and facets `order`, `item`, as `render/activity.py:45` declares one) — if `operation_kind_key` already labels an operation kind, this is the label only
- Modify: `backend/scadbuddy/bambuddy/preview.py` (`start_preview_plates`, Ruling 2, and the per-plate helper it shares with `start_preview`), `backend/tests/api/test_print_preview.py` and `test_print_preview_native.py` (a `start_preview` that still slices the first plate only, unchanged)
- Modify: `backend/scadbuddy/api/orders.py` (`POST /orders/{id}/items/{item}/quote`, through `run_operation` with `kind=ORDER_ITEM_QUOTE`, `subject=f"order_item:{item_id}"`, the body `SliceQuoteRequest`)
- Test: `backend/tests/orders/test_quote.py` (the steps on fakes: a `read_sliced` of `tests/support/sliced3mf.py`'s file, a fake `start_preview`), `backend/tests/api/test_order_quote.py` (Task 5 fills it)

**Interfaces:**
- Produces: `SliceQuoteRequest` (`extra="forbid"`): `printer_id: int | None`, `filament_plan: FilamentPlan`, `choices: PrintChoices`, `filaments: dict[int, str]`; `quote_check(request) -> checked` (Ruling 8: the rows' refusals, and `filaments` written onto the item through `update_item`), `quote_run(request, checked) -> Quote.model_dump(mode="json")` with the steps of `quote.py`: `ensure_output` (Ruling 7), `slice_item` (`start_preview_plates` with `subject="order_item:<id>"`, then `follow_slice` for each plate's `PreviewStarted`: Bambuddy `read_preview` every two seconds until `status == "completed"` or a failure; native `NativeSliceStore.get` until `completed`/`failed`; each under the run's heartbeat; a failure on any plate fails the quote with the slicer's own text and the plate), `fetch_sliced` (Ruling 3's bytes and digest), `keep_sliced` (the blob, then the ref, before the row, Ruling 3: the quote id is made first, `uuid.uuid4().hex`, `refs.add(key, QUOTE_HOLDER, quote_id)` follows `publish`, and `insert_quote` takes that id last; a failure anywhere after the add drops the holder before the run raises, and a crash leaves the reaper a ref with no row), `read_plates` (`read_sliced(payload, plate_id)` for every plate `start_preview_plates` sliced, one sliced file per plate on Bambuddy and one `native_slices` row per plate on native, each fetched and kept by `fetch_sliced` and `keep_sliced` in turn, so a multi-plate quote names one key per plate: `sliced_blob_key` and `sliced_sha256` hold the first plate's and each `Plate` of `plates` carries its own `blob_key` and `sha256`, every one held by the quote's holder; the quote's `plates` record each plate's index, seconds and grams; an every-field-None read, or a slot at 0 g, on any plate raises `QuoteError` naming it), `price_quote` (Task 2 on the Task 1 snapshot), `record` (Task 3). `QUOTE_TIMEOUT = timedelta(minutes=30)`.
- The run reads its services from the state as every kind does (`operations/kinds.py`: "a kind reads the state when it runs, never a route dependency"): the orders component, the Bambuddy client through `client_for(stored settings)`, the uploads store, the print library, the native starter and store, the render service, the blob store and refs.

- [ ] **Step 1: Write the failing tests** for the steps: `test_read_plates_prices_each_slot_from_the_file` (two plates, `sliced3mf.py`'s `plates` with two entries), `test_an_unknown_plate_fails_the_quote`, `test_a_slot_at_zero_grams_fails_the_quote`, `test_ensure_output_reuses_a_matching_output` (a fake outputs store), `test_the_check_refuses_a_settled_item`, `test_the_check_needs_a_filament_per_slot`, `test_the_check_writes_nothing_for_the_items_own_filaments`, `test_the_ref_is_added_before_the_row` (a fake `insert_quote` that raises finds the ref already there, and then dropped), `test_a_failed_run_drops_its_own_holder`; in `test_print_preview.py`, `test_start_preview_plates_slices_every_plate` (a two-plate copy starts two jobs under one subject).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders/test_quote.py` → PASS; `mypy`; `cd frontend && pnpm gen:api` exports.
- [ ] **Step 5: Commit** `feat(orders): the order_item_quote operation: render if needed, slice, read, price, record (#4250)`

---

### Task 5: The quote end to end

**Files:**
- Test: `backend/tests/api/test_order_quote.py` (`requires_postgres`, the api `client`; Bambuddy through `respx` as `tests/api/test_print.py` mocks the slicer's presets, slice job and library file, the sliced download answering `tests/support/sliced3mf.py`'s bytes; the native path through `tests/support/fake_slicer.py` as `tests/api/test_print_preview_native.py` runs one)
- Modify: `backend/tests/api/conftest.py` only if a fixture the two paths share is missing (the fake output and item helpers of `test_orders_lifecycle.py` are reused)

- [ ] **Step 1: Write the tests**: the happy path on Bambuddy (an item from an output, a filament per slot, `POST …/quote` → 202 → `GET /operations/{id}` → the quote with two `filament` lines, a `machine_time` line, `plates`, `sliced_sha256`; `blob_refs` holds the key; a second quote drops the first's holder), the same on native, Review Focus 2, 3, 5, 7 and 9 (`test_a_two_plate_item_is_quoted_on_both_plates`: an output of two plates, a sliced 3MF per plate with different grams and seconds, the quote's `plates` carrying both, two `setup` lines, the `machine_time` line over both plates' seconds, on Bambuddy and on native), and `test_a_quote_on_a_held_order_is_allowed`, `test_a_failed_slice_records_a_failed_operation_and_no_quote`.
- [ ] **Step 2: Run** `uv run --frozen pytest tests/api/test_order_quote.py` → PASS (the kind's run lands on the in-process `library` worker the api `settings` fixture gives every test).
- [ ] **Step 3: Refresh** `backend/.test_durations` only if the shard split complains (CLAUDE.md).
- [ ] **Step 4: Commit** `test(orders): the slice quote end to end on both slicers (#4250)`

---

### Task 6: Agent tools and coverage

**Files:**
- Modify: `agent/src/tools/orders.ts` (`order_item_quote` widened, Ruling 11; `price_book_get`, `price_book_set`, `order_estimate`)
- Modify: `agent/src/tools/coverage.ts` only if a route is deliberately without a tool (none planned)
- Test: `agent/test/orders.test.ts`, `agent/test/coverage.test.ts`

- [ ] **Step 1: Write the failing tests**: `order_item_quote` in `slice` mode sends the idempotency key, follows a 202 to the quote, and answers "still running" past the follow window; `price_book_set` patches a filament; `order_estimate` reads.
- [ ] **Step 2: Run to verify they fail**: `pnpm test -- orders coverage`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` in `agent/` → PASS.
- [ ] **Step 5: Commit** `feat(agent): price book, slice quotes and the rough estimate (#4250)`

---

### Task 7: The rough estimate

**Files:**
- Modify: `backend/scadbuddy/api/orders.py` (`GET /orders/estimate`, Ruling 9)
- Modify: `backend/scadbuddy/orders/pricing.py` (`estimate_grams`, Task 2)
- Test: `backend/tests/api/test_order_estimate.py`

- [ ] **Step 1: Write the failing tests**: an output with two closed parts and a filament with a density answers grams and a filament price; a filament without density is 404; a part with no volume is listed as unknown; Review Focus 8.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the module → PASS; `mypy`.
- [ ] **Step 5: Commit** `feat(orders): the rough estimate from a render's part volumes (#4250)`

---

### Task 8: Client, types and mocks

**Files:**
- Modify: `frontend/src/api/client.ts` (`api.priceBook`, `api.setPriceBook`, `api.createFilament`, `api.updateFilament`, `api.archiveFilament`, `api.quoteItem` through `command()` as the browser's other commands go, `api.orderEstimate`, `updateOrderItem` with `filaments`), `frontend/src/api/types.ts` (`Filament`, `PricingPolicy`, `PriceBook`, `SliceQuoteRequest`, `OrderEstimate`; `QuoteLine['kind']` gains `'rounding'`)
- Modify: `frontend/src/lib/money.ts` (`minorExponent(currency)` beside the formatting, the same table as Ruling 5's, with a test that the two agree on the listed codes)
- Create: `frontend/src/mocks/features/orders/priceBook.ts` (the book in the shared `state.ts`; the quote route answering 202 with an operation the mock settles into a `slice` quote with two filament lines after a tick, so the page's follow is testable; the estimate)
- Test: `frontend/src/mocks/features/orders/priceBook.test.ts`, `frontend/src/lib/money.test.ts`

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Implement.** `pnpm gen:api` first.
- [ ] **Step 3: Run** `pnpm typecheck && pnpm test -- orders money` → PASS.
- [ ] **Step 4: Commit** `feat(frontend): price book, slice quote and estimate client, types and mocks (#4250)`

---

### Task 9: The pages

**Files:**
- Modify: `frontend/src/pages/settings/fields.ts` (the five policy fields and the factor in the `orders` section: `kind: 'text'` with the decimal help, `pricing_rounding` as a select), `frontend/src/pages/SettingsPage.tsx` (a `FilamentsEditor` under the Orders section)
- Create: `frontend/src/components/orders/FilamentsEditor.tsx` (a `DataTable` of the book's filaments with add, edit and archive)
- Modify: `frontend/src/components/orders/QuoteEditor.tsx` ("Quote from a slice…" beside the manual price while editable; it opens `PrintPicker` with a `quote` prop), `frontend/src/components/PrintPicker.tsx` (`quote?: { orderId, itemId, filaments }`: in quote mode the Print button reads "Quote", the submit posts `api.quoteItem` with the picker's `PrintRunRequest` and the filaments, no queue, no project; a filament picker per slot from the price book beside the spool), `frontend/src/components/orders/OrderItemsTable.tsx` (a quote's lines shown with their kinds; a `slice` quote names its file's digest), `frontend/src/components/orders/AddToOrderDialog.tsx` (the estimate, Ruling 9, when a filament is picked)
- Modify: `frontend/e2e/orders.spec.ts` (the slice quote over the mocks: open the picker from an item, choose a filament, Quote, the quote appears with its lines; the estimate in Add to order)
- Test: the components' `*.test.tsx`, `pages/SettingsPage.sections.test.tsx` (the Orders section shows the policy fields and the filaments)

- [ ] **Step 1: Write the failing tests and the e2e cases.**
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Run** `pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/orders.spec.ts e2e/settings.spec.ts` → PASS; the `--list` counts still add up.
- [ ] **Step 4: Commit** `feat(frontend): the price book in Settings, Quote from a slice, the estimate (#4250)`

---

### Task 10: Docs

**Files:**
- Modify: `CLAUDE.md` ("Layout", the orders entry: `pricebook.py`, `pricing.py`, `quote.py`, `operations.py`'s kind on the `library` queue, the quote's blob keys and holder, the `housekeeping_reap_quote_holds` sweep, `start_preview_plates`, the estimate route)
- Modify: `docs/superpowers/specs/2026-10-18-orders-design.md` (§5.2: the operation's run in place of the workflow, Ruling 1; step 2's dialog choices and the per-slot filament, Ruling 2; §5.1: the Settings page's Orders section, Ruling 4; §3.4: the blob key and the holder, one key per plate, the ref before the row and the reaper, Ruling 3; §3.6: the `rounding` line kind, Ruling 5; §4: the rows for `POST …/items/{item}/quote`, `GET /orders/estimate`, the price book routes, `PATCH …/items/{item}`'s `filaments`; §7.3: `order_item_quote`'s two modes and `order_estimate`, Ruling 11)
- Modify: `README.md` "Features" (the orders line gains "priced from the slice")

- [ ] **Step 1: Write** the edits.
- [ ] **Step 2: Commit** `docs(orders): CLAUDE.md layout and the spec's §3.4, §4, §5 and §7.3 for phase 2 (#4250)`

---

## Self-review

- Spec coverage. §3.4 (the slice columns, the refs): Tasks 3, 4, Ruling 3. §3.6: Task 2, Ruling 5. §5.1: Task 1, Ruling 4, Task 9. §5.2: Tasks 4, 5, Rulings 1, 2, 7, 8. §5.3: Task 7, Ruling 9. §4's rows: Tasks 1, 3, 4, 7. §7.3: Task 6, Ruling 11. §11 phase 2's "slices through the Bambuddy slicer until #3822's remaining parts put the native one in `PrintRunWorkflow`": Ruling 2 slices through `start_preview_plates`, which branches to the native slicer as `start_preview` does, so both paths work now and the reader is `read_sliced` either way, once per plate.
- Review round 1 (PR #4492): a quote slices every plate, never the dialog's first plate alone (Ruling 2, Review Focus 9); the sliced file's ref is taken before its row and a stray ref is reaped (Ruling 3, Review Focus 10); unchanged filaments repin nothing (Rulings 6 and 8); the `to_major` difference is a `rounding` line (Ruling 5); `colour` is `NOT NULL` (Ruling 4, Task 1); the markup example is a percentage (Task 2); `orders/activity.py` is created (Task 4).
- Dependencies between tasks: Task 1 before 2 (the policy's shape), 4 and 7. Task 2 before 4 and 7. Task 3 before 4. Task 4 before 5, 6 and 8 (the schema). Task 8 before 9. Task 10 last.
- Types used across tasks: `Filament`, `PricingPolicy`, `PriceBook` (Task 1) in Tasks 2, 4, 6, 8; `Plate`, `Priced`, `PricingError` (Task 2) in Tasks 3, 4; `QUOTE_HOLDER` and `record_slice_quote` (Task 3) in Task 4; `SliceQuoteRequest` (Task 4) in Tasks 6, 8, 9.
- What phase 3 and 5 will touch again, on purpose: `accept_quote` gains no check here (Ruling 10); phase 3's `order_item_print` reads the accepted quote's `sliced_blob_key` and takes a ref of its own; phase 5 imports Bambuddy's spools as `filaments` rows by `_filament_key` and then checks a quote's spool against its filament (Ruling 2).
