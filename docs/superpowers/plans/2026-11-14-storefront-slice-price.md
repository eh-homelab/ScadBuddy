# Storefront Slice Price Implementation Plan (#6153)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A storefront visitor's render is priced by the back office's own slice-and-quote path. The visitor checks out and pays the firm lines at once. A line with no slice price shows no number, and the shop prices it later.

**Architecture:** The CRUD process saves the visitor's done render as a storefront output (hidden from the back office's lists) and slices it through `orders/quote.py`'s own slicer, `PreviewSlicer` (`orders/operations.py:175-317`), with its own routing: the native slicer, or Bambuddy's where the back office would use Bambuddy's. Support and process settings come from the template, as for any output (`OutputSource.print_settings`, `part_settings`, `bambuddy/print_source.py:797-806`). The plates are read with `read_plate` and priced with the same function a quote uses. Results go into a content-keyed variant cache that a batch (#6237) can fill too. At submit, the CRUD process recomputes each line's key, records a `storefront` quote for each firm line and accepts it as the customer. An all-firm order is accepted and asks for its balance. A mixed order asks for the firm part at once, as a deposit, and for the shop's lines later, as the balance.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Temporal (the existing slice workflows), Postgres migrations, React + TypeScript storefront, vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-11-13-storefront-customize-ux-design.md` §4, §5.4, §5.6, and the owner's decisions on #6153 of 2026-11-14:
- The slice price is firm, the page drops "confirmed after we check it", and checkout is self-service.
- One pricing path: the price uses quote_run's slice and pricing, plus the printer's machine rate.
- One price per variant, cached by content; a price-book or rate change makes a price stale.
- Checkout charges the cached price, looked up by key, and never a price from the browser.
- "Quoting should be no different than slicing and printing in back office": support and process come from the model, and the profile holds only the printer and a spool per material.
- "An order can mix and match firm prices and estimates. If unsure, don't show a price."
- "The page never shows grams or print time" (the last decision of 2026-11-14). The visitor reads only the price. The API may keep grams and print time for the shop.
- #6236 (renders may be kept forever) and #6237 (a batch pre-renders, pre-slices and pre-prices every colour combination) are noted only.

These decisions supersede the spec's §4 "A price shown as exact. It stays 'about'", §5.4's rough fallback, §5.6.1's process and support profile, and §5.6.2's "Never the Bambuddy path". PR A edits the spec to say so.

Base: origin/main `e76f876c7`. **#6166** (listing defaults) is merged (`6fc158e0d`). Assumes **#6178** (picks, `one_material`, `RenderLimits.job_of`, `OwnedRender.picks`, `slots_from_picks`, `customer_filaments`, `is_support`) merges first. Line numbers are on `e76f876c7`. Each PR re-checks them on its own base before it starts, because #6178 and other merges move them.

---

## Global Constraints

- **#5295.** Only the CRUD (API) process reads or writes the new tables and starts slices. No untrusted code and no slicer runs in a pod that holds a database secret. The native slice runs in the Rust worker, and Bambuddy's slice runs in Bambuddy. No new database client, and no grant to another role.
- **One slicing and pricing path.** The storefront slices through `PreviewSlicer.start` / `poll` / `fetch` (`orders/operations.py:183-317`), reads with `read_plate` (`orders/quote.py:734-771`), and prices with the function `price_quote` uses (`quote.py:871-916`), with the machine rate applied as `_priced` applies it (`quote.py:1147-1155`). No second formula, and no `estimated=True` (`estimate.py:179`).
- **The profile holds only what a visitor cannot choose:** the printer, and a spool per material. It holds no process, support, bed or nozzle override. The dialog choices come from the model's remembered `model_print_choices` (`library/settings_store.py:491`), else from the dialog's defaults for the output and printer (`bambuddy/choices.py:387` `choices_for_output`), exactly as the back-office dialog fills them. With neither, no price is shown.
- **The cache is keyed by content.** A key never holds a job id. Renders are pruned after 24 h (#6236 can switch that off), and the cache outlives them.
- **The same slice key never slices twice** while its row is `completed`. When the price book, a machine rate or a filament record changes, the price key changes. A new price then comes from the cached plates with no new slice.
- **The browser never sends a price.** The submit recomputes every key. No request body carries a price, a key or a source (`extra="forbid"`).
- **If unsure, show no price.** The visitor reads a number only from a completed slice priced with no error. The volume estimate never reaches a visitor.
- **Support stays internal** (spec §4). Support settings, the support filament and a support line never reach a visitor. `ESTIMATE_FIELDS` (`storefront/routes.py:190-192`) lists every field a visitor reads.
- Exact copy:
  - A firm price: `"$X for one"`. Nothing else: the page never shows grams or print time.
  - While the slice runs or waits: `"Working out the price…"`.
  - With no price (failed, unavailable, unsure): the existing `"We price this design by hand"` and `"Add it to your order, and we will send you the price before you pay."` (`Estimate.tsx:15-16`).
- Migrations: new files only, named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`.
- Generated API files are never committed. Run `pnpm gen:api` before frontend vitest.
- Docker is broken on the planning machine. Postgres and Temporal tests run in CI (`backend-pytest`).

## Review Focus

1. **The variant changes during a visit:** the profile, a template's `print_settings`, or the remembered dialog choices are edited. Expected: the slice key changes, the page shows "Working out the price…", and a draft line made before goes to `price_changed` at submit. Test: `test_submit_price_changed_after_profile_edit` (B2).
2. **The price book changes between add and submit.** Expected: 409 `price_changed`, new prices from the cached plates, and no slice. Test: `test_submit_reprices_from_cached_plates_without_slicing` (B2).
3. **The slice fails, or its execution is lost.** Expected: no number on the page, and the line is priced by the shop. Test: `test_failed_slice_shows_no_price_and_never_restarts` (A3).
4. **Two visitors with one variant at once, at capacity 1.** Expected: one slice and one cache row. Test: `test_two_reads_one_slice` (A3).
5. **A mixed cart with a pending purchase link** (#6200 gate, `_no_pending_purchase`, `lifecycle.py:2873-2886`). Expected: the firm quotes are recorded but not accepted, the submit sets no deposit and answers no `pay_url`, and no payment request is open. Test: `test_submit_with_pending_purchase_records_but_does_not_accept` (B2).

---

## Rulings (decisions this plan makes; the owner did not)

- **R1. PR order is C → A → B.** — A cannot slice without a profile. — Cost if wrong: none. C alone changes no visitor's view.
- **R2. A storefront spool per material is a spool id the shop picks from its own inventory** (Bambuddy's or native, as the print dialog's `filament_plan` names it, `bambuddy/filaments.py:229-242`). It is not a preset. — `SliceQuoteRequest.filament_plan` needs a spool per slot (`bambuddy/models.py:548-553`), and the dialog resolves each slot's filament preset from its spool. The spool only picks the preset: the slot's colour comes from the 3MF, and its price from the price-book filament the visitor picked. — Cost if wrong: if the shop runs out of that spool, the profile names a spool that is gone. The slice then fails and the line shows no price, which is the safe side. Task C2 refuses a missing spool at save time.
- **R3. The subject is an output, not a library copy.** The visitor's done render is saved by the same create-output operation the quote uses (`orders/operations.py:530-531`), under `storefront_output_id(model_sha256)` = `uuid5(NAMESPACE_URL, "scadbuddy:storefront_variant_output:<sha256>")`. — A library copy's `LibrarySource.print_settings` is `{}` (`bambuddy/print_source.py:1134-1136`), so it would lose the template's support and process settings. An output keeps them (`print_source.py:802-806`). — Cost if wrong: outputs grow. `OutputMeta` gains `storefront: bool = False`, which hides them from the back office's lists. They are swept with the cache rows (R13).
- **R4. `quote_run` is split, not called.** `quote_run` needs an order item (`quote.py:968-1058`). Its item-free core moves into `slice_and_price(run, output_id, request, subject, slot_filaments, *, quantity, currency) -> SlicedPrice`. That core is `slice_item` → `fetch` → `read_plate` → `rated_policy` → `price_plates`. `quote_run` / `_priced` call it, and the storefront calls it. — The one-cent equality test then follows from one shared function. — Cost if wrong: low. It is a refactor that the existing `test_quote.py` covers.
- **R5. A new quote source, `storefront`,** is added to `QuoteSource` and `MEASURED_SOURCES` (`orders/models.py:73-75`). It is not `slice`. — The cache keeps plates, not the sliced file, and `printing.py:358` / `:604` reuse a `slice` quote's file. The print slices again with the spools that are loaded. — Cost if wrong: any `source == "slice"` switch misses storefront quotes. Task B1 greps for them.
- **R6. Slices are recorded on read.** The price GET, the draft add and the prune tick poll the stored `PreviewStarted` list (`PreviewSlicer.poll`) and record the plates when every plate is done. — No workflow change. — Cost if wrong: a slice nobody reads before its blob is swept slices once more.
- **R7. Capacity is enforced on the CRUD side.** At most `storefront_slices_concurrent` (default 1) rows are `running`. A visitor's variant goes ahead of a `batch` variant (#6237). — No second Temporal task queue. — Cost if wrong: a large batch delays a visitor by at most one slice.
- **R8. The price key includes the quantity.** `price()` takes `quantity` (`orders/pricing.py:103-112`).
- **R9. The volume estimate is gone from the storefront.** `render_estimate` no longer calls the part analysis. PR A deletes the unused analysis code from `renders.py` (`_parts`, `_begin`, the executor, `renders.py:506-705`). The `storefront_job_parts` table stays until a later migration drops it. — "If unsure, don't show a price." — Cost if wrong: the shop loses a volume hint. `rough_estimate` stays for the back office's `GET /orders/estimate`.
- **R10. Mixed-order payment.** At submit, the firm lines' quotes are accepted. A mixed order's deposit is set to `max(policy deposit, totals.total_minor at submit)`, which is the firm lines plus shipping and their tax (`_set_deposit`, `lifecycle.py:1587-1635`). The shop's lines have no quote at submit, and `item_price` counts an item only through a quote (`totals.py:38-48`), so that total holds the firm part only. **Not while a purchase is pending (R14).** `_set_deposit` does not check `_no_pending_purchase`: the gate covers only `_accept_quote` (`lifecycle.py:4651`), `_accept_order` (`lifecycle.py:2840`) and a `balance` request (`lifecycle.py:1515-1516`). So the submit checks `pending_purchases` itself, and while it is not empty it sets no deposit. That opens a `deposit` request the customer pays at once. When the shop's lines are quoted and accepted, `_accepted_by_the_last` (`lifecycle.py:4591-4611`) accepts the order. `_open_balance` (`lifecycle.py:1755-1795`) then opens a `balance` request for what is not paid. The payments seam already supports this: request kinds are `deposit` and `balance` (`payments/models.py:13`), and a paid deposit counts in `totals.paid_minor`. — No new request kind. — Cost if wrong: a shop deposit policy higher than the firm total charges more up front, and that is the policy's own choice.
- **R11. An all-firm order always opens a balance request at acceptance,** deposit or not. Today `_open_balance` returns early with neither (`lifecycle.py:1764-1765`). One flag, `always_balance: bool = False`, is added to `_accepted_by_the_last`, `_accept_order` (`lifecycle.py:2834`) and `_open_balance`, and each passes it to the next (`_accept_order` calls `_open_balance` at `lifecycle.py:2870`). The submit sets it for an all-firm order. `_accept_order` also sets it when the order holds an accepted `storefront` quote. That covers an order whose firm quotes were held back by a pending purchase (R14): its first payment request opens at acceptance, for the whole order.
- **R12. The submit's answer gains `accepted` and `pay_url`,** read after the commit. They are not in the ledger's stored reply (`submit.py:7-9`). Pages are linked after the commit (`payments/service.py:356-363`).
- **R13. Cache rows are swept 90 days after their last use,** and the storefront outputs with them (`STOREFRONT_VARIANT_TTL_DAYS = 90`).
- **R14. A pending purchase link (#6200) in the cart:** the firm quotes are recorded at submit but not accepted. `_accept_quote` (`lifecycle.py:4642`) refuses `purchase_pending` through `_no_pending_purchase` (`lifecycle.py:4651`). The submit also sets no deposit and answers no `pay_url` (R10), so no payment request opens at submit. Only the shop's own deposit policy can open one (`_deposit_policy`, `lifecycle.py:985`), as on any order today, because a `deposit` request does not wait for purchases (`lifecycle.py:1509-1510`). Next: the shop prices the purchase and quotes its lines. Then the customer accepts the quotes on the order page, or the shop accepts for the customer. The last accept accepts the order (`_accepted_by_the_last`), and `_open_balance` with `always_balance` (R11) opens one `balance` request for what is not paid. — It keeps #6200's gate as it is.

---

## PR C — the shop slice profile

### Task C1: `StorefrontSliceProfile`

**Files:**
- Create: `backend/scadbuddy/orders/storefront/slice_profile.py`
- Modify: `backend/scadbuddy/library/settings_store.py:481-483` (StoredSettings), `:782-783` (SettingsPatch)
- Modify: `backend/scadbuddy/api/settings.py:136-140` (`storefront_slices_concurrent: int = 1`, 1..4)
- Test: `backend/tests/orders/storefront/test_slice_profile.py`

**Interfaces:**
```python
class StorefrontSliceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    printer: str                      # registry printer id; routes the slice and prices machine time
    spools: dict[str, int]            # material (casefolded) -> spool id
def profile_digest(profile: StorefrontSliceProfile) -> str
def slice_request(profile, slot_materials: Sequence[str], choices: PrintChoices) -> SliceQuoteRequest
    # filament_plan = one SlotChoice(slot_id=i+1, spool_id=spools[material]) per slot; printer=profile.printer;
    # path=None (the run's own route); no supports/prime_tower/print_sequence/part_settings (the template's)
```
- [ ] **Step 1: Failing tests:** `test_slice_request_one_spool_per_slot_by_material`, `test_material_is_casefolded`, `test_missing_material_raises` (`ProfileMissingMaterialError`), `test_profile_has_no_process_or_support_field` (`extra="forbid"` refuses `supports`).
- [ ] **Step 2:** `cd backend && uv run pytest tests/orders/storefront/test_slice_profile.py -v` gives FAIL.
- [ ] **Step 3:** Implement. **Step 4:** PASS. **Step 5:** Commit `feat(storefront): the shop slice profile, a printer and a spool per material (#6153)`.

### Task C2: checks at save and at publish

**Files:**
- Modify: `backend/scadbuddy/api/settings.py` (the PATCH validates the profile: the printer is in the registry, and each spool exists in the inventory the dialog reads)
- Modify: `backend/scadbuddy/orders/storefront/listings.py` (publish check, beside #6178's)
- Test: `backend/tests/orders/storefront/test_listings.py`, `backend/tests/api/test_settings.py`

- [ ] **Step 1: Failing tests:** `test_publish_refuses_material_without_spool` (422 `listing_material_unsliceable`, `materials=["PETG"]`; support materials skipped via `is_support`), `test_publish_without_profile_is_allowed` (shown with no price), `test_profile_with_unknown_spool_is_422`.
- [ ] **Step 2–4:** FAIL, then implement, then PASS. **Step 5:** Commit.

### Task C3: the "Storefront pricing" settings section

**Files:**
- Create: `frontend/src/pages/settings/sections/storefront-pricing.tsx`, `storefront-pricing.test.tsx`
- Modify: `frontend/src/mocks/features/settings.ts`, `frontend/src/mocks/fixtures.ts`

- The section has a printer select (registry printers), one spool select per material in the price book (from the spool inventory the print dialog lists; support materials under "Support (internal)"), and the concurrency number.
- [ ] **Step 1: Failing vitest:** `saves one spool per material`, `lists support materials apart`.
- [ ] **Step 2:** `cd frontend && pnpm gen:api && pnpm exec vitest run src/pages/settings/sections/storefront-pricing.test.tsx` gives FAIL.
- [ ] **Step 3–4:** Implement, then PASS. **Step 5:** Commit.

---

## PR A — the slice price on the page

### Task A1: `slice_and_price`, the shared core

**Files:**
- Modify: `backend/scadbuddy/orders/quote.py:871-916` (`price_quote`), `:968-1058` (`quote_run`), `:1110-1205` (`_priced`)
- Test: `backend/tests/orders/test_quote.py`

**Interfaces:**
```python
def price_plates(plates, filaments: Mapping[int, Filament], policy, *, currency, quantity, support: Collection[int] = ()) -> Priced
async def rated_policy(book, machine_rate, printer, printer_id) -> tuple[str, PricingPolicy, MachineRateBreakdown | None]
@dataclass(frozen=True)
class SlicedPrice:
    plates: list[Plate]; payloads: list[bytes]; priced: Priced; rate: MachineRateBreakdown | None
async def slice_and_price(run: QuoteRun, output_id: str, request: SliceQuoteRequest, subject: str,
                          slot_filaments: Mapping[int, Filament], *, quantity: int, currency: str,
                          support: Mapping[int, Filament] | None = None) -> SlicedPrice
async def price_sliced(plates, slot_filaments, *, book, machine_rate, printer, printer_id, quantity, currency, support=None) -> tuple[Priced, MachineRateBreakdown | None]
```
`price_quote` looks up the item's filaments, then calls `price_plates`. `_priced` gets its plates and payloads from `slice_and_price` (`keep_sliced` stays in `_priced`: a quote keeps its file, a variant does not).
- [ ] **Step 1: Failing tests:** `test_price_plates_equals_price_quote`, `test_quote_run_uses_slice_and_price` (a spy on `slice_and_price` is called once with the item's output and request).
- [ ] **Step 2–4:** FAIL, then refactor, then `uv run pytest tests/orders/test_quote.py -v` PASS, with every existing test unchanged. **Step 5:** Commit `refactor(orders): slice_and_price, the quote's slice and price without an item (#6153)`.

### Task A2: the variant cache and storefront outputs

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_storefront_variant_cache.sql`
- Create: `backend/scadbuddy/orders/storefront/variant_store.py`
- Modify: `backend/scadbuddy/library/outputs.py` (`OutputMeta.storefront: bool = False`; the listings the back office reads skip it)
- Test: `backend/tests/orders/storefront/test_variant_store.py`, `backend/tests/library/test_outputs.py`

Schema:
```sql
CREATE TABLE storefront_variant_slices (
  slice_key text PRIMARY KEY,  -- sha256(model_sha256, profile_digest, choices JSON, print_settings+part_settings digest, Profiles.version)
  model_sha256 text NOT NULL, output_id text NOT NULL,
  priority text NOT NULL CHECK (priority IN ('visitor','batch')),
  status text NOT NULL CHECK (status IN ('waiting','running','completed','failed')),
  started jsonb,               -- the PreviewStarted list, for PreviewSlicer.poll
  plates jsonb, failure text,
  created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
  last_used_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE storefront_variant_prices (
  price_key text PRIMARY KEY,  -- sha256(slice_key, filament id per slot, pricing digest, quantity, currency)
  slice_key text NOT NULL REFERENCES storefront_variant_slices ON DELETE CASCADE,
  quantity integer NOT NULL, currency text NOT NULL,
  unit_price_minor bigint NOT NULL, total_minor bigint NOT NULL, unit_cost_minor bigint NOT NULL,
  lines jsonb NOT NULL, print_seconds integer NOT NULL, grams numeric NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), last_used_at timestamptz NOT NULL DEFAULT now());
```
`VariantStore(pool)`:
- `claim(slice_key, model_sha256, output_id, priority, limit) -> VariantSlice`. It moves a row from `waiting` to `running` only below `limit`, and a `visitor` row goes before a `batch` row. It runs under `pg_advisory_xact_lock(hashtext('storefront_variant_slices'))`.
- `set_started`, `complete(slice_key, plates)`, `fail(slice_key, failure)`, `price(price_key)`, `put_price`, `touch(keys)`, `sweep(older_than) -> list[str]` (it returns the output ids to delete).
- [ ] **Step 1: Failing tests:** `test_claim_is_one_row_per_key`, `test_claim_waits_over_limit`, `test_visitor_goes_before_batch`, `test_completed_row_never_reruns`, `test_failed_row_is_not_restarted`, `test_sweep_takes_unused_rows_and_names_outputs`, `test_storefront_output_hidden_from_lists`.
- [ ] **Step 2–4:** FAIL, then implement, then PASS (CI). **Step 5:** Commit.

### Task A3: `VariantPrices`, one variant or a batch

**Files:**
- Create: `backend/scadbuddy/orders/storefront/variant_prices.py`
- Modify: `backend/scadbuddy/main.py:692-700` (the prune tick runs `collect()`, `start_waiting()` and `sweep()`)
- Test: `backend/tests/orders/storefront/test_variant_prices.py`

**Interfaces:**
```python
@dataclass(frozen=True)
class Variant:
    job_id: str                        # only to save the output from; never in a key
    model_3mf: Path
    slot_filaments: tuple[str, ...]    # price-book filament id per slot, from 0
    quantity: int = 1
@dataclass(frozen=True)
class VariantQuote:
    state: Literal["done", "running", "waiting", "failed", "unavailable"]
    price: VariantPrice | None; price_key: str | None; plates: list[Plate] | None
class VariantPrices:
    async def ensure(self, variants: Sequence[Variant], *, priority: Literal["visitor", "batch"] = "visitor") -> list[VariantQuote]
    async def collect(self) -> int
    async def start_waiting(self) -> int
    async def price_key_of(self, variant: Variant) -> str | None
```
`ensure` does this for each variant:
1. Hash the 3MF and read the template's `print_settings` / `part_settings` (`Catalogue`, as `OutputNaming` does).
2. Read the choices (`model_print_choices`, else `choices_for_output`). With none, the state is `unavailable`.
3. Build the request (`slice_request`, C1), then compute `slice_key` and `claim` it.
4. If the row is admitted: save the output (`create_output`, R3), then `PreviewSlicer.start(output_id, request, subject=f"storefront:{model_sha256}")`, then `set_started`.
5. If the row is completed: compute the price key, and on a miss call `price_sliced(...)` (A1) and `put_price`.

`collect` polls `running` rows (`PreviewSlicer.poll`), and when every plate is done it calls `fetch` and `read_plate`, then `complete`. A `QuoteError` or a slicer failure calls `fail`. #6237 calls `ensure(batch, priority="batch")`.
- [ ] **Step 1: Failing tests** (fake `PreviewSlicer` and `create_output`; the sliced fixture `test_quote.py` reads):
  - `test_ensure_saves_one_output_and_starts_one_slice`
  - `test_two_reads_one_slice`
  - `test_completed_variant_never_slices_again`
  - `test_price_book_change_reprices_from_cached_plates`
  - `test_failed_slice_shows_no_price_and_never_restarts`
  - `test_capacity_one_second_variant_waits_then_start_waiting_runs_it`
  - `test_batch_waits_behind_visitor`
  - `test_no_profile_or_no_choices_is_unavailable`
  - `test_template_print_settings_reach_the_slice` (the request's output source carries the template's `print_settings`, the support settings among them)
  - `test_storefront_price_equals_back_office_quote_to_the_cent` (**owner rule**): one render, the same filaments by slot, the same printer and spools, and a fake slicer that returns the same bytes. `quote_run` on an order item and `VariantPrices.ensure` + `collect` give equal `unit_price_minor`, `total_minor` and line amounts.
- [ ] **Step 2–4:** FAIL, then implement, then PASS (CI). **Step 5:** Commit `feat(storefront): price a variant through the back office's slice, cached by content (#6153)`.

### Task A4: the visitor's price answer

**Files:**
- Modify: `backend/scadbuddy/orders/estimate.py:76-92` (`StoreEstimate` is replaced by `StorePrice`), `:56-73` (`RoughEstimate.public` is removed)
- Modify: `backend/scadbuddy/orders/storefront/renders.py` (`render_estimate` is rewritten; the analysis code is deleted, R9)
- Modify: `backend/scadbuddy/api/crud_storefront.py:112`, `:403-409`
- Modify: `backend/scadbuddy/storefront/routes.py:190-198`, `backend/scadbuddy/tools/export_openapi.py:119`
- Test: `backend/tests/api/test_storefront_renders.py`, `backend/tests/storefront/test_edge_routes.py`

```python
class StorePrice(BaseModel):
    source: Literal["slice", "shop"]
    slice_state: Literal["done", "running", "waiting", "failed", "unavailable"]
    price_minor: Money | None = None       # set only when source == "slice"
    currency: str; minor_exponent: int
    grams: str | None = None; print_seconds: int | None = None   # for the shop; the page never shows them
```
`ESTIMATE_FIELDS = {"source","slice_state","price_minor","currency","minor_exponent","grams","print_seconds"}`. `render_estimate` builds a `Variant` with the slot filaments from `_filament_for(…, owned.picks)` (#6178) in slot order, and calls `ensure([variant])`. When the state is `done`, it answers `source: "slice"`. Otherwise it answers `source: "shop"` with no number.
- [ ] **Step 1: Failing tests:** `test_price_is_slice_when_done`, `test_running_slice_has_no_price`, `test_failed_slice_has_no_price`, `test_no_volume_estimate_reaches_a_visitor` (no `parts`, `unknown_parts` or `estimate` key), `test_slice_price_has_no_rate_or_cost`.
- The page reads only `source`, `slice_state`, `price_minor`, `currency` and `minor_exponent`. `grams` and `print_seconds` stay in the answer for the shop (owner, 2026-11-14).
- [ ] **Step 2–4:** FAIL, then implement, then PASS. **Step 5:** Commit.

### Task A5: the page

**Files:**
- Modify: `frontend/src/storefront/components/Estimate.tsx:10-27` (renamed `PriceLine`; the file name stays)
- Modify: `frontend/src/storefront/pages/CustomizePage.tsx:263-301` (keep reading while `slice_state` is `running`/`waiting`, up to `timing.limitMs`)
- Modify: `frontend/src/storefront/api/client.ts`, `frontend/src/mocks/features/storefrontPublic.ts`
- Create: `frontend/src/storefront/components/Estimate.test.tsx`
- Modify: `frontend/src/storefront/pages/CustomizePage.test.tsx`
- Modify: `docs/superpowers/specs/2026-11-13-storefront-customize-ux-design.md` §4, §5.4, §5.6 (the owner's decisions)

- [ ] **Step 1: Failing vitest:** `firm price shows "$4.20 for one" and no grams or print time`, `running shows "Working out the price…" and no number`, `failed shows "We price this design by hand" and no number`, `never shows "about" or "rough"`, `polls until slice_state is done`.
- [ ] **Step 2:** `pnpm gen:api && pnpm exec vitest run src/storefront` gives FAIL. **Step 3–4:** Implement, then PASS. **Step 5:** Commit.

---

## PR B — self-service checkout

### Task B1: the `storefront` quote source and the line's price key

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC>_storefront_quote_source.sql`. It re-adds the `quotes.source` CHECK (last in `20261107T2214Z_order_items_origin_archives.sql:12`) with `'storefront'`, and adds `order_items.storefront_price_key text`.
- Modify: `backend/scadbuddy/orders/models.py:73-75`, the `OrderItem` model; `backend/scadbuddy/orders/store.py`
- Modify: `backend/scadbuddy/orders/storefront/drafts.py:361-410`, `backend/scadbuddy/orders/storefront/wire.py:456-474` (`DraftItem.price: DraftPrice | None`, with `DraftPrice{source: "slice"|"shop", price_minor: int | None}`)
- Modify: `backend/scadbuddy/storefront/routes.py` (draft item fields, if filtered); `frontend/src/components/orders/QuoteEditor.tsx` (label a `storefront` quote "From the shop's slice")
- Modify, only if the grep shows a need: `backend/scadbuddy/orders/printing.py:358`, `:604`. The grep covers `source == "slice"` switches and every `MEASURED_SOURCES` user (`costs.py:269`, `:323`).
- Test: `backend/tests/orders/storefront/test_drafts.py`, `backend/tests/orders/test_costs.py`

- `add_listing_item` reads the visitor's render (`RenderLimits.job_of`, #6178), builds the `Variant` with the item's own slots (`resolved.colours`) and `quantity`, and calls `ensure`. When the state is `done`, it sets `storefront_price_key` in the add's transaction. Otherwise it leaves it null, and the shop prices the line.
- [ ] **Step 1: Failing tests:** `test_add_with_sliced_render_keeps_price_key`, `test_add_while_slicing_has_no_price`, `test_draft_view_shows_firm_and_shop_lines`, `test_add_body_cannot_name_a_price`, `test_storefront_quote_prices_job_cost_from_its_plates` (`test_costs.py`: `costs.py` reads `MEASURED_SOURCES` at `:269` (`_unit`, the job cost) and `:323` (`weights_of`), so a `storefront` quote prices the job cost and the slot weights from its plates, as a `slice` quote does).
- [ ] **Step 2–4:** FAIL, then implement, then PASS. **Step 5:** Commit.

### Task B2: the submit accepts firm lines and asks for payment

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py:901-994` (`submit_order`), `:1755-1795` (`_open_balance`), `:2834-2871` (`_accept_order`), `:4591-4611` (`_accepted_by_the_last`): each gains `always_balance: bool = False` (R11)
- Modify: `backend/scadbuddy/orders/storefront/submit.py:118-195`
- Modify: `backend/scadbuddy/orders/storefront/wire.py:578-584` (`SubmittedOrder.accepted: bool = False`, `pay_url: str | None = None`)
- Modify: `backend/scadbuddy/api/crud_storefront.py:497-504` (wrap the submit in `Payments.run_closing_linked`; run the `ensure_order_projects` hook as `order_page.py:817-820` does)
- Test: `backend/tests/orders/storefront/test_submit.py`, `backend/tests/api/test_orders_storefront.py`

**Interfaces:**
- `submit_order(..., firm: Mapping[str, FirmPrice] | None = None)`, where `FirmPrice{price_key, plates, lines, unit_cost_minor, unit_price_minor}`.
- After `stamp_submitted` (`lifecycle.py:986`), for each firm item: `store.insert_quote(source="storefront", …, revision_id=item.current_revision_id, created_by=customer)`. Then `_accept_quote(tx, order, item, quote, customer)`, unless a purchase is pending (R14).
- All firm: `_accepted_by_the_last(..., always_balance=True)`.
- Mixed, with no pending purchase: `_set_deposit(tx, order, customer, max(policy, totals.total_minor))` (R10). The shop's lines have no quote at submit, so `totals.total_minor` is the firm lines plus shipping and their tax (`totals.py:38-48`).
- With a pending purchase (`store.pending_purchases(tx, order.id)` not empty): no `_accept_quote`, no `_set_deposit` and no `pay_url` (R14).
- `submit_draft` recomputes each key with `VariantPrices.price_key_of`. On a mismatch, it updates or clears the item's key and raises 409 `price_changed` ("A price in your order changed. Check it, then send your order again.").

- [ ] **Step 1: Failing tests:**
  - `test_submit_all_firm_accepts_order_and_opens_balance` (order `accepted`; one open `balance` request of `total_minor`; `pay_url` set; quotes `storefront`, accepted by a `customer` actor)
  - `test_submit_mixed_cart_asks_firm_part_as_deposit` (firm items `approved`; shop items `draft`; one open `deposit` request of the firm total with shipping and tax; that amount equals the firm lines' quote totals plus shipping and tax, with no share of the shop's lines; `pay_url` set)
  - `test_mixed_order_balance_after_shop_lines_accepted` (deposit paid → shop quotes the rest → customer accepts → one `balance` request for the shop lines only)
  - `test_submit_reprices_from_cached_plates_without_slicing`
  - `test_submit_price_changed_after_profile_edit`
  - `test_submit_with_pending_purchase_records_but_does_not_accept` (no deposit policy; the `storefront` quotes exist and are not accepted; `deposit_minor` is None; no payment request is open; `pay_url` is None; after the purchase is priced and every quote is accepted, one `balance` request for the whole order)
  - `test_submit_never_reads_a_price_from_the_body`
  - `test_submit_replay_answers_pay_url_from_order`
  - `test_back_office_order_open_balance_unchanged`
- [ ] **Step 2–4:** FAIL, then implement, then PASS (CI). **Step 5:** Commit `feat(storefront): check out and pay the firm lines at the sliced price (#6153)`.

### Task B3: the visitor pays

**Files:**
- Modify: `frontend/src/storefront/pages/DraftPage.tsx:106-120`, `DraftPage.test.tsx`, `frontend/src/mocks/features/storefrontPublic.ts`
- Before submit, each line shows its firm price, or "We price this one by hand." After submit with a `pay_url`: "Pay {amount} now". For a mixed order the copy adds: "We send the price of the other items before you pay for them."
- [ ] **Step 1: Failing vitest:** `firm cart shows Pay now`, `mixed cart pays the firm part and says the rest comes later`, `price_changed reloads the draft and says so`.
- [ ] **Step 2–4:** FAIL, then implement, then PASS. **Step 5:** Commit.

### Task B4: the real stack and a measurement

**Files:** `e2e-stack/seed.sh` (seed a profile: the fake printer and a spool per material), plus a spec beside the hot-path ones (`e2e-stack/CLAUDE.md`). The spec renders a keychain listing, waits for `source: "slice"`, adds the line, submits, and asserts `pay_url`.
- [ ] Run as `e2e-stack/CLAUDE.md` says (CI `hot-path-e2e.yml`). Then measure the slice time of an H2C keychain, and record it on #6153 with the run id.

---

## Open risks

- **A firm price on a slice the shop never saw.** The template's support and process settings decide whether the slice is printable, as in the back office. Nobody sees the model before it is charged, so a template that slices cleanly but prints badly is charged at its firm price. A reprint is at the shop's cost.
- **Bambuddy's slicer and library now see public renders** when the back office's route for the profile printer is Bambuddy's (the owner's decision). Copies land in Bambuddy's inbox as a quote's do.
- **Slice failure modes:**
  - a spool removed from the inventory (refused at save; at slice time, a failed slice)
  - a printer that cannot be reached
  - a Bambu Studio bump (`Profiles.version` is in the key)
  - a Bambuddy slicer upgrade, which is not in the key, so a cached plate can outlive it
  - a lost execution
  Each ends with no price and the shop's check.
- **A stale price at checkout.** A price book, rate or filament edit gives 409 `price_changed`, and the price is redone from the cached plates. A machine-rate breakdown read live from Bambuddy (`machine_rate.py:527`) can move the pricing digest between reads. If that flaps, use the stored inputs (`live=False`) for storefront prices.
- **Remembered dialog choices are shared with the back office.** A person's next print of the model changes its `model_print_choices` and so the slice key. Visitors then wait for a new slice, and draft lines go to `price_changed`.
