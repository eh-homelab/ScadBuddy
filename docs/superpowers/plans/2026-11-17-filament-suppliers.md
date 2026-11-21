# Filament suppliers, price sheets and invoices (#6266) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **UI work (Tasks 9-13 touch the frontend):** the implementer loads `frontend-design:frontend-design` before writing any UI. Pass this line on to every UI subagent too.

**Goal:** A person records who sells them filament, what each product costs over time, and what each purchase cost, and every spool bought on an invoice carries its real cost and a link back to the invoice.

**Architecture:** New tables in the CRUD service's database and three synchronous psycopg stores in `backend/scadbuddy/filament/` (`SuppliersStore`, `InvoiceStore`, `ShoppingStore`), public routes in one new `api/filament_purchasing.py` (auto-discovered by `main.py`'s `pkgutil` walk), and agent tools. An invoice is a draft that a person edits, then confirms in one transaction that creates the types, supplier products, spools and price points it names. The invoice file is a blob (`attachment-<sha256>`, the order attachments' helpers) held through `blob_refs`. The frontend gains the Suppliers, Invoices and Shopping list tabs and a price panel on the Types page.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3 (sync pools, `dict_row`), pydantic v2, Postgres 18, `Decimal` money; React 19, Vite, shadcn/ui, msw, vitest, Playwright; TypeScript agent tools (zod, msw).

**Spec:** `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§3, §5.2 supplier products and price points, §5.8, §9 steps 1, 5, 6 and the cost rules, §10 tabs, §11 parity, §12 row #6266, §13). Plans that bind this one: `2026-11-14-filament-data-model.md` and `2026-11-15-filament-ui.md`, with their "Decisions made while building". Epic #6260. Next issue, kept out of this plan: #6267 (see "What #6267 relies on").

## Global Constraints

- **Database access (#5295):** only the CRUD service has SQL. Every new route is a public `/api/v1/filament/*` route that the agent's tools reach with the person's own credential. No new pool, grant, worker or cross-service table read. No new `crud_router` area: #6267's profile-fetch worker adds its own, with a service credential from its first PR.
- **Migrations (#491):** new files only, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, later than `20261117T0907Z_filament_events_order.sql`. Never edit a merged file. Timestamps `timestamptz`; enums are `text` with `CHECK (... IN (...))`; soft delete is `archived_at` with partial unique indexes `WHERE archived_at IS NULL`.
- **Money is a decimal string in the shop's currency, never a float.** Columns are `numeric(12,4)`; pydantic fields are `Decimal` (`Cost`-style bounds, `ge=0, lt=100_000_000`), which the API writes as strings; the TypeScript side keeps strings and does arithmetic only through `lib/decimal.ts` (Task 9). The shop's currency is the setting `orders_currency`; its minor-unit exponent is `orders/currency.py` `minor_exponent(code)`, sent as `minor_exponent` so the browser never asks `Intl` for digits (`lib/money.ts` comment).
- **Files are blobs and pointers, never base64** (`lint-base64-images.sh`). An invoice file is `{name, media_type, size}` on the wire, a blob key in the row, a `blob_refs` hold (holder kind `filament_invoice`).
- **Links a person typed** (`website`, `product_url`) are `http` or `https` only; the UI renders them as anchors with `rel="noopener noreferrer"`.
- **Every route** sets `summary=` and `description=`. Every new public `/api/v1` operation gets an agent tool or a `NOT_A_TOOL` entry with a reason (`agent/src/tools/coverage.ts`, `agent/test/coverage.test.ts`). Every tool works in classic and durable mode.
- **Ledger:** a spool made or costed by an invoice carries `ref_kind='invoice_line'`, `ref_id=<line id>` on its event (`ledger.record`'s idempotency index), actor `invoice:<invoice id>`. `FilamentSpoolStore.create_on` and `update_on` take optional `ref_kind` and `ref_id` keyword arguments and pass them to `ledger.record` (Task 6).
- **Every frontend control** uses the shared shadcn components (`components/ui/*`, never a native `<select>`; `ui-guard.test.ts`), loads `frontend-design`, uses `AppLink`/`appUrl` for ScadBuddy links, and every choice a person makes twice is remembered (Task 12: the Invoices view and the last supplier).
- **msw mocks** go in `frontend/src/mocks/features/filamentPurchasing.ts` (exports `handlers` and `reset`; the glob in `mocks/features.ts` loads it). One route has one feature module (`mocks/features.test.ts`). A type error in a test fails all three `frontend-e2e` shards: run `pnpm typecheck` first.
- **Generated API files are never committed** (`pnpm gen:api` in `frontend/` and `agent/` after every route change). Each task that changes an API shape also changes its consumers and runs their typecheck in its Step 4.
- **Each task fixes the notes it makes false** (`CLAUDE.md` files, docstrings, `NOT_A_TOOL` reasons, `tabs.ts`'s header comment); a stale note is a Blocking review finding.
- **Commands:** backend `cd backend && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test uv run --frozen pytest <files>`, then `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`; frontend `cd frontend && pnpm gen:api && pnpm lint && pnpm typecheck && pnpm exec vitest run <file>`, `pnpm exec playwright test e2e/<spec>`; agent `cd agent && pnpm gen:api && pnpm test <file> && pnpm typecheck`.

## Decisions this plan makes (spec §13 and silences)

Each with what it costs if wrong.

1. **Cost reaches an order through the price book, as today.** A spool's `cost_per_kg` override is set from its invoice line. Orders still read the price book (`orders/costs.py` `book_per_gram`); `orders/spool_import.py` still sets a book record's cost from the newest priced spool when a person clicks import. This plan changes neither. A new price point never rewrites a type's `cost_per_kg` by itself (anti-goal: a silent overwrite). The Types page offers "Use as default cost" (one PATCH). Cost: one click.
2. **Price point = price of one pack, tax included, shipping excluded, in the invoice's currency.** `price_per_kg` is derived on read: `price / pack_size / (type.label_weight_g / 1000)`, to four places, so a label-weight edit never leaves a stale number. An invoice line makes `(line_total / quantity) * pack_size`. A manual point is typed on the same basis. Cost: a person who types a pre-tax price gets a low per-kg number; the form says "with tax".
3. **A line's `quantity` counts spools** (the skill: "a bundle that splits into N spools is quantity N"). `pack_size` only sizes the price point and suggests a quantity. Cost: none.
4. **Cost per spool** (spec §9) = `allocated_cost / quantity`; the spool's `cost_per_kg` override = that ÷ (effective label weight in kg), to four places. `allocated_cost = line_total + shipping share`; `line_total = line_subtotal + line_tax`. Shipping is spread by `line_total` share. All sums use largest-remainder rounding at four places, so the lines add up to the invoice exactly (Task 2).
5. **Tax:** all lines carry `line_tax` (basis `line`), or none do and the invoice's `tax` is spread by `line_subtotal` with basis `receipt-wide <rate>%` where rate is `tax / sum(line_subtotal)` in percent to one place (spec: "receipt-wide 7.0%"). Some but not all is a 422. With none and `tax = 0` the basis is `none`.
6. **Currency:** a draft may be in any currency; confirm is refused (409) unless it equals `orders_currency`. No exchange rates. Cost: a foreign invoice stays a draft until the shop's currency changes or a person retypes it.
7. **Reconciliation:** the draft shows `difference = total - (sum(line_total) + shipping)`; confirm is refused (409) when `abs(difference) > 0.05`. The constant is `RECONCILE_TOLERANCE`. Cost: a receipt with larger rounding is corrected by hand.
8. **One invoice per supplier and number:** `unique (supplier_id, upper(number)) WHERE number IS NOT NULL`. A second create, or a number edit that collides, is a 409 whose JSON body carries `existing_invoice_id` (machine readable; `detail` is text for people). A different `import_run_id` with the same number is also that 409; the same `import_run_id` is matched first and returns the existing invoice. A draft or confirmed invoice with no number is never refused. Cost: none.
9. **Confirm is idempotent by state.** A second confirm returns the confirmed invoice and creates nothing; two at once serialise on the invoice row (`FOR UPDATE`). `POST /filament/invoices` with an existing `import_run_id` returns that invoice (200, not 201): #6267's retry key. No `Idempotency-Key` header. Cost: none.
10. **A confirmed invoice is frozen** except `note` and its file. Lines and header refuse edits (409); delete is refused (409, "edit its spools instead"). Only a draft deletes (its blob hold is released). Cost: a wrong confirmed invoice is fixed by editing the spools' costs on the spool page.
11. **A line may fill spools that exist** (`update_spool_ids`, spec §9 diff rule). Each must be unarchived, have no invoice line and no `cost_per_kg` of its own, and have no type or the line's type; else 409 naming the spool, and nothing is written. A needs-type spool takes the line's type. New spools = `quantity - len(update_spool_ids)`. Cost: none.
12. **A new spool from an invoice starts full** (`grams_left` = its effective label weight, a `created` event of source `person`). Cost: none; a weighing corrects it.
13. **New types and products at confirm.** A line with no `type_id` and a `proposed_type` makes the type (`SpoolTypeStore.create_on`). A line with no `supplier_product_id` finds the supplier's product (one SQL lookup by `upper(sku)` inside `InvoiceStore.confirm`; `SuppliersStore` has no such method) where `sku` is the line's `sku`, else its `description` ("the product name is the SKU"; never borrowed from another invoice), else creates it with the line's type. A found product with another type is a 409. The #6266 UI offers existing types only (with a link to `/filament/types/new`); `proposed_type` is for the API and #6267. Cost: none.
14. **File types:** a PDF or a photo (`attachments.sniff` kind `document` or `image`); anything else is 422. Served with the order attachments' header set (`api/order_attachments.py` `get_attachment_content`) except `Cache-Control`: it is `private, no-cache`, because a second file replaces the first under one URL. The upload is not an `Operation`: identical bytes share one blob and the row update is idempotent. Cost: none.
15. **Shopping list (BB parity, `/inventory/shopping-list`, measured 2026-11-17 on `https://bambuddy.internal.nullreference.io/openapi.json`, Bambuddy 1.3.1):** items hold material, subtype, brand, colour name, `quantity_spools`, note, `status` (`pending`, `purchased`), `purchased_at`, `added_at`. Ours adds an optional `type_id` and keeps the text identity so an item survives its type's archiving. BB's `status` is an unconstrained string; the two values are our call. No auto-fill from low stock (the forecast is #6269), no matching of invoice lines to items. Cost: none.
16. **Per-spool suppliers (§11 "Per spool: suppliers") are the spool's invoice line's supplier plus its type's supplier products**, shown on the spool page. BB's per-spool links with a quoted price are not copied: a quoted price is a price point. Supplier stats (BB `/inventory/stats/suppliers`) are `GET /filament/suppliers/stats`: per supplier, spools bought, grams left, grams consumed, spend. SKU settings (lead time, safety margin, §5.2 `sku_settings`) go with the forecast in #6269. Cost: two parity items wait one phase.
17. **BB's supplier list is not pulled.** The pull (§8.1) has no supplier data to sync in this phase; #6265's engine decides. Cost: none.

Decisions 18 to 30 are #6868 (an empty spool adds its type to the shopping list, built after this plan; its five intake rulings, then the ones made while building):

18. **R-6868-1: Trigger.** A spool becomes empty when its remaining filament reaches 0 g through any write that moves it (print usage, a weigh, an AMS weight), or when it is archived. It fires once, on the move into empty, not on every later write. Why: owner direction on #6868 (2026-11-21), made on intake.
19. **R-6868-2: One pending item per type.** If a `pending` item for the spool's type already exists, nothing is added. A spool with no type adds a text item from its own material, subtype, brand and colour. Why: intake ruling; a list that grows by one row per spool of a type would bury what to buy.
20. **R-6868-3: Setting.** `filament_shopping_auto_add_empty`: a remembered choice with a toggle in Settings → Filament (#6219), on by default. It is a stored-only setting (no environment value, no source badge), like `print_auto_import`. Why: intake ruling; stored only because the check reads it in the emptying write's own transaction (`SELECT value FROM settings`), where no process environment reaches, so an env-seeded value could disagree with what the check sees.
21. **R-6868-4: Note.** The item's note names the spool that ran out and when: `Spool #N ran out on YYYY-MM-DD HH:MM UTC.` or `Spool #N was archived on ...`, from the event's physical time. Why: intake ruling; UTC because the note is stored text that every viewer reads.
22. **R-6868-5: Spec §2 non-goal.** "Stock reorder automation" stays out: this adds a list item and orders nothing. Why: owner direction, 2026-11-21.
23. **R-6868-6: One place.** The check is in `ledger.record`, after `refresh_derived`, in the caller's transaction. Every spool event goes through `record`, so print usage, BB usage, a weighing, an AMS reading, `sync_ams_weights`, a person's edit, the CSV import, an invoice confirm, the Bambuddy pull and both archive paths get it with no per-path code. Why: one hook cannot miss a path that a later change adds.
24. **R-6868-7: What is not a move into empty.** Unknown grams (`grams_before` null) that become 0: a spool created at 0 g, or the first reading of an old reel. An unarchived spool already at 0. Any write on an archived spool. Archiving a spool that is already at 0 (its move into empty already counted). The pull's archived import of a spool Bambuddy had archived before SB knew it (`data.bb_archived_at`). Why: each is an import or a repeat, not a spool running out under SB's watch; the safer choice is no item rather than a list filled by an import.
25. **R-6868-8: A spool with no type.** A pending item with no type and the same material, subtype, brand and colour name (case ignored) holds it. Why: ruling 2 per type, applied to the text identity a typeless item keeps.
26. **R-6868-9: Concurrency.** The check and the insert run under `pg_try_advisory_xact_lock` of the item's key (`scadbuddy:filament:shopping_auto:<type id>` or the text key). A writer that cannot take it adds nothing and never waits. Why: the caller holds spool row locks, and a waiting lock could deadlock a write that empties two spools; the holder is adding that same item. Cost: if the holder rolls back, that empty spool adds no item.
27. **R-6868-10: Never fail the write.** The add runs in a savepoint; a failure is logged and leaves the emptying write as it was. A rolled-back write takes its item with it. Why: the ledger is the record of filament; a shopping list row is a convenience.
28. **R-6868-11: The item names its spool.** New column `filament_shopping_items.empty_spool_id` (FK, `ON DELETE SET NULL`, migration `20261121T1444Z_filament_shopping_empty_spool.sql`), null for a person's item. The Shopping list tab marks such an item "Auto" and links its note to the spool, and says under "To buy" that a spool that runs out adds its type, with a link to Settings. The agent's `filament_shopping_list` description says so. Why: a person sees why an item is there and can open the spool.
29. **R-6868-12: Not person-only.** The setting grants no access and reaches no printer or Bambuddy, so it is not in `PRINT_WATCH`-style person-only lists, and its switch carries no user-only mark (`SwitchField` `userOnly={false}`). Why: #2171 and #4208 reject extra lockdown of controls that grant nothing.
30. **R-6868-13: Mocks.** The msw mocks serve `empty_spool_id` and the setting, and seed one auto-added item (Hot Pink, spool #3); they do not empty spools themselves. Why: the behaviour is the backend's transaction; the mocks only need the shapes the UI shows.

## Decisions made while building

Each line: what was decided, why, and what it costs if wrong. Measured against the code on 2026-11-17.

- **Shopping items list newest first, pending above purchased** (`shopping.py`: `ORDER BY (status = 'pending') DESC, created_at DESC`) — the store's order is the one the UI shows; `created_at` is BB's `added_at`, with no second column — one sort flip.
- **Every store sets `updated_at` itself** (no trigger) — the tables have none, and each write names it — a stale timestamp if a new write forgets it.
- **Tax on a receipt whose lines cost nothing is a 422** (`InvoiceMathError`, mapped in `api/filament_purchasing.py`) — a free receipt with tax is a typing error — one mapping.
- **A draft may hold header tax and no lines; confirm refuses an invoice with no lines** (`InvoiceNoLinesError`) — the upload flow makes empty drafts — one more refusal (409, as every confirm refusal; `_conflict`).
- **The agent's invoice edit is PATCH then PUT and is not atomic** (the tool description says so) — the brief's shape, and no combined route exists — a half-applied edit if the second call fails; a combined route later.
- **PUT lines and DELETE answer a plain Problem 409 for a confirmed invoice** — they carry no `existing_invoice_id`, which only a number collision has — one `responses` entry.
- **Confirm maps only `IntegrityError`, `DataError`, `SpoolTypelessError`, `SpoolInUseError` and pydantic `ValidationError` to a 409 naming the line** (`InvoiceLineError`); any other exception is a 500 — a client conflict must not hide a bug — a 500 for an unforeseen client error.
- **A line with no SKU and a description over 128 characters is refused at confirm** ("set a SKU") — the SKU is never truncated, because a cut could collide — one more typed field.
- **Lines that propose the same new type code in one confirm share the type the first line made** — a second create would hit the unique code — nothing.
- **A named `supplier_product_id` that is archived is refused (409), like the by-SKU lookup** — an archived product is not for new buying; a concurrent insert of the same new SKU re-reads the product (`ON CONFLICT DO NOTHING`) instead of a 500 — nothing.
- **Supplier stats include archived suppliers and mark each row `archived`** — spend history stays true — a filter flip.
- **`type_suppliers` on the spool page lists unarchived suppliers and products only** — it shows where to buy now; `type_prices` keeps archived rows, marked `supplier_archived` and `product_archived` — a filter flip.
- **`spool_count` counts every spool bought, archived included; `remaining_g` counts unarchived spools only** (route and tool descriptions say so) — bought is history, remaining is stock — a filter flip.
- **A repeat "purchased" keeps the first `purchased_at`** (`COALESCE`) — the first purchase time is the true one — nothing.
- **A name collision is a 409 whose body carries `existing_invoice_id`** (not parsed from the text) — a client needs it without parsing `detail` — a UUID parse in the client.
- **A draft's supplier is editable (`supplier_id` on a draft PATCH; a confirmed invoice answers 409), and a supplier move sets the draft lines' `supplier_product_id` to NULL** — a product belongs to one supplier, so a kept link would be false — a person re-picks the products.
- **The invoice list carries `spool_count`** (by `invoice_line_id`, the set confirm stamps) so the tab can show "Spools" — one more query per list — a count that drifts from the stamp.
- **The invoice page saves in order: only the newest answer is applied; after a failed newest save it refetches the invoice under the same guard; the status line offers Retry** — an older answer would bring back a header the newer save changed, and a failed one would drop an earlier success — a stale field until a reload.
- **"Use as default cost" is per product** (one button per supplier product in `PricePanel`) and only fills the cost field as an unsaved edit — a type has several suppliers, and a silent overwrite is an anti-goal (Decision 1) — one click.
- **A price sheet that fails to load shows "Not loaded" in that type's "Newest price" cell; the other prices stay** (`allSettled`) — one failed supplier must not blank the column — an N+1 read stays, as no one-call endpoint exists.
- **`change_pct` on a price sheet is rounded to one decimal place, ROUND_HALF_UP** (`suppliers.py` `_change_pct`) — it is a percent shown to people — a format change.
- **The invoice-line spool picker has no "no invoice" filter; the server refuses a spool that has an invoice line at confirm (409 naming the spool)** — the picker lists the type's unarchived spools, and the server is the one rule — a person sees the refusal after confirm instead of a shorter list.
- **The mock's `minorExponent` list stays (the shop currency is USD), with a comment naming the backend source it mirrors** — the mock needs a decimal count only for money it formats — a wrong decimal count for an exotic currency in the mock only.
- **`useMoney.format` shows the bare decimal, with no symbol, when no currency is known (unread or failed)** (final review ruling 1) — a guessed `$` shows a wrong amount as if it were right — a bare number with no unit for a moment on a slow price book.
- **`InvoicePage`'s refetch waits for a save in flight** (ruling 2; `stale` and `settle`, as the failed-save path) — a read answered before the save commits shows pre-save lines — one more read after a busy save.
- **A refused supplier move restores only rows whose `productId` is still empty** (ruling 3) — a product picked while the request was out is the person's newer input — a row picked and then refused keeps the pick, not the old product.
- **`InvoiceLineIn` refuses more `update_spool_ids` than `quantity` and a line total past `numeric(12,4)` (422); `CheckViolation` and `DataError` in create and PUT lines are a 422** (ruling 4; `Quantity` is now at most 1,000, the shopping cap, final review Minor) — the route says 422 for a bad field, and a 100,000-spool request held a thread under one lock — a customer with a 1,000+ spool line must split it.
- **Create refuses an archived supplier (422, as PATCH does); confirm refuses an invoice whose supplier is archived (409, `InvoiceSupplierArchivedError`)** (ruling 5) — confirm would add products and price points under an archived supplier — an archived supplier's old draft must be restored or moved before it confirms.
- **`filament_shopping_remove` needs `item_id` or an explicit `clear: "all" | "purchased"`; neither is an error and nothing is sent** (ruling 6) — an omitted argument must never empty the list — an agent that sent `purchased_only` now gets an error and retries with `clear`.
- **`filament_invoice_upsert` says a confirmed invoice takes a note-only edit** (ruling 7) — the description said 409 for any confirmed invoice, which the route does not — nothing.
- **Confirm locks every line's spools in one sorted query before the lines are processed** (final review backend Minor 1) — two confirms with the same spools in different line order could deadlock — one extra query per confirm that fills spools.
- **A mixed set of small fixes from the final review** (ruling 8 and 9): per-type "Add to shopping list" in-flight guards; server figures keyed by line id; the "Buy" button's name starts with "Buy"; focus goes to the heading after Confirm, to "Add line" after a removal and to the new row after an add; Confirm waits for a header save; figures dim while an edit is unsaved; the file viewer folds below `lg`; whitespace is no edit on the supplier page; "Use as default cost" is off for a price in another currency — each is small UI behaviour with a test — a folded viewer costs a tap on a phone.
- **The e2e asserts the spool's unit cost amount** (ruling 10; `line 1, $23.60 each`) — "each" alone passes with a wrong or empty cost — the e2e needs an edit if the receipt's numbers change.
- **The shopping list keeps its `status` filter** (the store takes it; `filament_shopping_*` and the route accept `pending` or `purchased`) — the brief's store has it and a tab needs it — nothing.

## Pre-flight rulings (2026-11-17)

Twenty findings of the pre-flight scan, all accepted as proposed; the plan above already carries them.

- F1: `create_on` and `update_on` of `spools.py` take optional `ref_kind` and `ref_id`; Task 6 Files, Interfaces and tests, and the Global Constraint say so — the ledger link to the invoice line could not be written otherwise.
- F2: the invoice file is served `Cache-Control: private, no-cache`, not `immutable` (Task 5, Decision 14, with a test) — a second file replaces the first under one URL.
- F3: Task 6 tests use `backend/tests/orders/test_spools.py` — the named `test_spool_import.py` does not exist.
- F4: Task 9 gains `e2e/page-titles.spec.ts`, `App.tsx` routes in `FILAMENT_TABS` order and an `<h1>` per new page (Tasks 9, 10) — both checks fail otherwise.
- F5: `SuppliersStore(pool, shop_currency)` with one callable shared with `InvoiceStore` (Task 3) — `add_price` needs the shop currency.
- F6: `InvoiceStore(pool, shop_currency, spools, types)` and `component.py` in Task 6 Files (Tasks 4, 6) — `confirm` needs both stores.
- F7: `confirm` turns `types.create_on`'s database errors into `InvoiceLineError` (409), with a test (Task 6) — a raw error would be a 500.
- F8: `SpoolInvoiceRef` and `SpoolTypeSupplier` live in `models.py` (Tasks 1, 7) — `purchasing_models.py` imports from it, so the reverse import is circular.
- F9: `TypePricePoint` joins Task 1's model list — Task 3 uses it.
- F10: `InvoiceLineIn` fields are listed and `tax_basis` is output only (Task 1, "What #6267 relies on") — the plan called a computed value an input.
- F11: one migration file in dependency order, the `ts+1` file and its paragraph removed (File structure, Task 1) — the two-file split was needless and contradicted the table.
- F12: the `upper(sku)` product lookup is SQL in `InvoiceStore.confirm`, not a `SuppliersStore` method (Decision 13, "What #6267 relies on") — the note named a method that does not exist.
- F13: `update` raises `DuplicateInvoiceError` too, `create` reads `import_run_id` first, and the 409 body carries `existing_invoice_id`, which Task 12 uses (Decision 8, Tasks 4, 12, Review Focus 1) — a typed number on a numberless draft collides on `update`, and the UI needs the id without parsing `detail`.
- F14: supplier `consumed_g` is one SQL: `print_actual` plus `print_estimate` rows with no `print_ended` event of the same ref; the `ledger.consumed_g` hedge is gone (Task 7) — it must match the ledger's own rule.
- F15: the spool-detail mock returns `invoice` and `type_suppliers` for spools from `addInvoicedSpools` (Task 9) — Task 12's spool page test needs them.
- F16: `filament_supplier_list` also covers `GET /suppliers/{supplier_id}` (Task 3) — the coverage test needs a tool for every route.
- F17: Task 9 edits the `frontend/CLAUDE.md` tab sentence, Task 14 no longer does — Task 9 is the change that makes it false.
- F18: Task 10 extracts `AddPriceForm`, Task 11's `PricePanel` reuses it — one form, not two.
- F19: Review Focus 1 now says a number entered twice or a retried `import_run_id`; two uploads of one PDF are not refused — there is no file-hash check.
- F20: store classes use module aliases for `list[...]` annotations (Tasks 3, 4, 8) — a method named `list` breaks the annotation under mypy.

## Review Focus

1. The same supplier's invoice number is entered twice (typed on a create or on a later edit): the second is refused with 409 whose body carries `existing_invoice_id`. A create retried with the same `import_run_id` returns the first invoice. No second draft or blob hold exists (Task 4). Two uploads of one PDF are not refused: there is no file-hash check, and a numberless draft is never refused (Decision 8).
2. Shipping or receipt-wide tax that does not divide evenly across lines, a single line, or a free (0 subtotal) line: the allocated costs add up to the invoice's lines plus shipping exactly, with no lost or doubled cent (Task 2).
3. Confirm is retried, double-clicked or raced: one set of spools exists, one price point per line, and the second call answers the confirmed invoice (Task 6).
4. A person types `javascript:alert(1)` as a supplier website or product URL: refused 422 (Task 3).
5. A person uploads an HTML or SVG page as an invoice file: refused 422, never stored, never served inline (Task 5).

---

## File structure

```
backend/scadbuddy/migrations/
  <ts>_filament_suppliers.sql        suppliers, products, invoices, lines, price points, shopping list, spools.invoice_line_id   Task 1
backend/scadbuddy/filament/
  purchasing_models.py   pydantic models for this plan                                      Tasks 1-8
  invoice_money.py       pure tax, shipping and cost arithmetic                             Task 2
  suppliers.py           SuppliersStore: suppliers, products, price points, sheet, stats    Tasks 3, 7
  invoices.py            InvoiceStore: drafts, lines, file hold, confirm                    Tasks 4-6
  shopping.py            ShoppingStore                                                      Task 8
  types.py               + create_on(conn, body)                                            Task 6
  component.py           Filament gains suppliers, invoices, shopping                       Tasks 3, 4, 8
backend/scadbuddy/api/filament_purchasing.py   every route of this plan                     Tasks 3-8
backend/tests/filament/test_invoice_money.py, test_suppliers.py, test_invoices.py,
  test_invoice_confirm.py, test_shopping.py, test_purchasing_migrations.py
backend/tests/api/test_filament_purchasing.py
agent/src/tools/filamentPurchasing.ts, agent/test/filamentPurchasing.test.ts                Tasks 3-8
  agent/src/tools/index.ts (spread), coverage.ts (NOT_A_TOOL for the file routes)
frontend/src/api/filamentPurchasing.ts            typed client                              Task 9
frontend/src/lib/decimal.ts                       decimal-string arithmetic and display     Task 9
frontend/src/mocks/features/filamentPurchasing.ts mock routes                               Task 9
frontend/src/pages/filament/
  tabs.ts (3 tabs on), SuppliersTab.tsx, SupplierPage.tsx, InvoicesTab.tsx, InvoicePage.tsx,
  ShoppingListTab.tsx, TypePage.tsx (+ PricePanel)
frontend/src/components/filament/purchasing/
  PriceSheet.tsx, ProductsTable.tsx, PricePanel.tsx, InvoiceUpload.tsx, InvoiceHeaderForm.tsx,
  InvoiceLinesTable.tsx, InvoiceTotals.tsx, InvoiceFile.tsx, SpoolMatchPicker.tsx, invoicesQuery.ts
frontend/e2e/filament-purchasing.spec.ts
```

---

## Part A — backend

### Task 1: Tables and models

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_filament_suppliers.sql` (one file), `backend/scadbuddy/filament/purchasing_models.py`
- Test: `backend/tests/filament/test_purchasing_migrations.py`

**Interfaces:**
- Produces the tables below and the models named in Tasks 3-8 (`Supplier`, `SupplierCreate`, `SupplierPatch`, `SupplierProduct`, `ProductCreate`, `ProductPatch`, `PricePoint`, `PricePointCreate`, `PriceSheetRow`, `SupplierStats`, `FileRef`, `Invoice`, `InvoiceCreate`, `InvoicePatch`, `InvoiceLine`, `InvoiceLineIn` (fields: `description`, `sku`, `quantity`, `unit_price`, `line_subtotal?`, `line_tax?`, `colour_code`, `type_id`, `supplier_product_id`, `proposed_type`, `update_spool_ids`; `tax_basis`, `line_total` and `allocated_cost` are server output only), `InvoiceDetail`, `TypePricePoint`, `ShoppingItem`, `ShoppingItemCreate`, `ShoppingItemPatch`). Inputs are `extra="forbid"` (the `_In` base of `filament/models.py`).
- `SpoolInvoiceRef` and `SpoolTypeSupplier` are not here: Task 7 defines them in `filament/models.py` beside `SpoolDetail`, because `purchasing_models.py` imports `_In` and `Cost` from `models.py` and a back-import would be circular.
- Produces a `Url` type: `Annotated[str, AfterValidator(...)]`, `http`/`https` scheme only, at most 500 characters, `ValueError` otherwise.

Tables (ids `uuid PRIMARY KEY DEFAULT gen_random_uuid()`; `created_at`/`updated_at timestamptz NOT NULL DEFAULT now()` on each):

| Table | Columns |
|---|---|
| `filament_suppliers` | `name text NOT NULL`, `website text`, `customer_number text`, `note text`, `archived_at`; unique `upper(name) WHERE archived_at IS NULL` |
| `filament_supplier_products` | `supplier_id uuid NOT NULL REFERENCES filament_suppliers`, `type_id uuid NOT NULL REFERENCES filament_spool_types`, `sku text NOT NULL`, `product_url text`, `pack_size integer NOT NULL DEFAULT 1 CHECK (pack_size > 0)`, `note text`, `archived_at`; unique `(supplier_id, upper(sku)) WHERE archived_at IS NULL` |
| `filament_price_points` | `product_id uuid NOT NULL REFERENCES filament_supplier_products`, `price numeric(12,4) NOT NULL CHECK (price >= 0)`, `currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$')`, `on_date date NOT NULL`, `source text NOT NULL CHECK (source IN ('invoice','manual'))`, `invoice_line_id uuid REFERENCES filament_invoice_lines`, CHECK `(source = 'invoice') = (invoice_line_id IS NOT NULL)`; unique `(invoice_line_id) WHERE invoice_line_id IS NOT NULL`; index `(product_id, on_date DESC)` |
| `filament_shopping_items` | `type_id uuid REFERENCES filament_spool_types`, `material text NOT NULL`, `subtype text`, `brand text`, `colour_name text`, `quantity_spools integer NOT NULL DEFAULT 1 CHECK (quantity_spools > 0)`, `note text`, `status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','purchased'))`, `purchased_at timestamptz` |
| `filament_invoices` | `supplier_id uuid NOT NULL REFERENCES filament_suppliers`, `number text`, `invoice_date date NOT NULL`, `currency text NOT NULL` (same check), `subtotal`, `shipping`, `tax`, `total` all `numeric(12,4) NOT NULL CHECK (>= 0)` (`shipping`, `tax` default 0), `file_key text`, `file_name text`, `file_media_type text`, `file_size bigint`, `status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed'))`, `import_run_id text`, `confirmed_at timestamptz`, `note text`; unique `(supplier_id, upper(number)) WHERE number IS NOT NULL`; unique `(import_run_id) WHERE import_run_id IS NOT NULL`; CHECK the four `file_*` columns are all null or all set |
| `filament_invoice_lines` | `invoice_id uuid NOT NULL REFERENCES filament_invoices ON DELETE CASCADE`, `position integer NOT NULL`, `description text NOT NULL`, `sku text`, `quantity integer NOT NULL CHECK (quantity > 0)`, `unit_price numeric(12,4) NOT NULL CHECK (>= 0)`, `line_subtotal`, `line_tax`, `line_total`, `allocated_cost` (`numeric(12,4) NOT NULL CHECK (>= 0)`), `tax_basis text NOT NULL DEFAULT 'none'`, `colour_code text`, `type_id uuid REFERENCES filament_spool_types`, `supplier_product_id uuid REFERENCES filament_supplier_products`, `proposed_type jsonb`, `update_spool_ids uuid[] NOT NULL DEFAULT '{}'`; unique `(invoice_id, position)`; CHECK `cardinality(update_spool_ids) <= quantity` |
| `filament_spools` (alter) | `ADD COLUMN invoice_line_id uuid REFERENCES filament_invoice_lines`; index on it |

`filament_price_points` references `filament_invoice_lines`, so the one migration file creates the tables in dependency order: suppliers, products, invoices, lines, price points (with its inline FK), shopping items, then the `filament_spools` alter.

- [ ] **Step 1: Failing tests** in `test_purchasing_migrations.py` (`pg_pool` fixture, `pytestmark = pytest.mark.requires_postgres`): `test_a_supplier_name_is_unique_while_unarchived` (second `INSERT` with `'ACME'` after `'acme'` raises `UniqueViolation`; after archiving the first it inserts); `test_a_product_sku_is_unique_per_supplier_case_insensitively`; `test_a_price_point_of_source_invoice_names_a_line` (`source='invoice'` with no `invoice_line_id` raises `CheckViolation`; `source='manual'` with one raises too); `test_an_invoice_number_is_unique_per_supplier` (same number, other case, same supplier: `UniqueViolation`; other supplier: inserts; two `NULL` numbers insert); `test_a_line_cannot_fill_more_spools_than_it_has` (`quantity=1` with two ids: `CheckViolation`); `test_a_file_is_all_or_nothing` (`file_key` set, `file_name` null: `CheckViolation`); `test_the_models_refuse_a_script_url` (`SupplierCreate(name="A", website="javascript:alert(1)")` raises `ValidationError`; `"https://acme.example/x"` is kept).
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament/test_purchasing_migrations.py` — FAIL (no tables).
- [ ] **Step 3:** Write the migration file and `purchasing_models.py`; each table carries a header comment citing #6266 and spec §5.8.
- [ ] **Step 4:** Run the test file and `tests/test_pg_migrations.py` — PASS.
- [ ] **Step 5:** Commit `feat(filament): supplier, price point and invoice tables (#6266)`.

### Task 2: Invoice arithmetic

**Files:**
- Create: `backend/scadbuddy/filament/invoice_money.py`
- Test: `backend/tests/filament/test_invoice_money.py`

**Interfaces:**
- Produces (all `Decimal`, quantised to `Decimal("0.0001")`, `ROUND_HALF_UP`; no I/O):
  - `RECONCILE_TOLERANCE = Decimal("0.05")`
  - `spread(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]`: largest-remainder shares of ``total`` by ``weights``, summing to ``total`` exactly; all-zero weights share equally.
  - `tax_per_line(subtotals: Sequence[Decimal], given: Sequence[Decimal | None], invoice_tax: Decimal) -> tuple[list[Decimal], str]`: the per-line tax and the basis text. All ``given`` set: those values, basis `"line"`. None set and ``invoice_tax > 0``: `spread(invoice_tax, subtotals)`, basis `f"receipt-wide {rate:.1f}%"`, rate = `invoice_tax / sum(subtotals) * 100`. None set and no tax: zeros, `"none"`. Mixed: raises `MixedLineTaxError(ValueError)`.
  - `allocate(line_totals: Sequence[Decimal], shipping: Decimal) -> list[Decimal]`: each `line_total + share` where `share` is from `spread(shipping, line_totals)`.
  - `spool_cost_per_kg(allocated_cost: Decimal, quantity: int, label_weight_g: int) -> Decimal`: `allocated_cost / quantity / (label_weight_g / 1000)`.
  - `price_per_pack(line_total: Decimal, quantity: int, pack_size: int) -> Decimal` and `price_per_kg(price: Decimal, pack_size: int, label_weight_g: int) -> Decimal`.
  - `difference(total: Decimal, line_totals: Sequence[Decimal], shipping: Decimal) -> Decimal`: `total - (sum(line_totals) + shipping)`.

- [ ] **Step 1: Failing tests** (plain pytest, no database):
  - `test_spread_adds_up_exactly` (Review Focus 2): `spread(Decimal("5.00"), [Decimal("10"), Decimal("10"), Decimal("10")])` sums to `Decimal("5.0000")` and is `[1.6667, 1.6667, 1.6666]` in some order with the extra on the first; `spread(Decimal("0.01"), [1, 1, 1])` sums to `0.0100`.
  - `test_a_single_line_takes_all_of_it`: `spread(Decimal("4.50"), [Decimal("9")]) == [Decimal("4.5000")]`.
  - `test_a_free_line_takes_no_share`: `spread(Decimal("3"), [Decimal("0"), Decimal("30")]) == [Decimal("0.0000"), Decimal("3.0000")]`; all-zero weights `[0, 0]` share `[1.5000, 1.5000]`.
  - `test_receipt_wide_tax_names_its_rate`: subtotals `[40, 60]`, no given, tax `7` → `([2.8000, 4.2000], "receipt-wide 7.0%")`.
  - `test_line_tax_is_kept_as_given`: given `[Decimal("1.40"), Decimal("2.10")]` → those and `"line"`.
  - `test_mixed_line_tax_is_refused`: given `[Decimal("1"), None]` raises `MixedLineTaxError`.
  - `test_no_tax_has_basis_none`.
  - `test_allocation_adds_shipping_by_value`: totals `[30, 70]`, shipping `10` → `[33.0000, 77.0000]`.
  - `test_spool_cost_per_kg`: `spool_cost_per_kg(Decimal("21.4000"), 1, 1000) == Decimal("21.4000")`; `(Decimal("30"), 2, 500) == Decimal("30.0000")`.
  - `test_price_per_pack_and_kg`: `price_per_pack(Decimal("60"), 3, 2) == Decimal("40.0000")`; `price_per_kg(Decimal("40"), 2, 1000) == Decimal("20.0000")`.
  - `test_difference`: totals `[30, 70]`, shipping 10, total 110 → `Decimal("0.0000")`; total 110.04 → `0.0400`.
- [ ] **Step 2:** Run `uv run --frozen pytest tests/filament/test_invoice_money.py` — FAIL.
- [ ] **Step 3:** Implement. `spread` is the only algorithm the signature does not give: floor each share at four places, then hand the leftover units of `0.0001` out by descending remainder, ties to the lowest index.
- [ ] **Step 4:** Run — PASS; `ruff` and `mypy` on the file.
- [ ] **Step 5:** Commit `feat(filament): invoice tax, shipping and cost arithmetic (#6266)`.

### Task 3: Suppliers, products and price points

**Files:**
- Create: `backend/scadbuddy/filament/suppliers.py`, `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/filamentPurchasing.ts`
- Modify: `backend/scadbuddy/filament/component.py` (`Filament.suppliers: SuppliersStore`), `agent/src/tools/index.ts`
- Test: `backend/tests/filament/test_suppliers.py`, `backend/tests/api/test_filament_purchasing.py`, `agent/test/filamentPurchasing.test.ts`

**Interfaces:**
- Consumes: Task 1 models, Task 2 `price_per_kg`.
- Method names `list` shadow the builtin inside the class, so annotations that say `list[...]` break under mypy: define module aliases (`_Suppliers = list[Supplier]`, and so on) above the class and annotate with them, as `SpoolTypeStore` does. The same holds for `InvoiceStore` (Task 4) and `ShoppingStore` (Task 8).
- Produces `SuppliersStore(pool, shop_currency: Callable[[], str])` (the one callable `component._build` makes for both stores, read on each use; `add_price` defaults `currency` from it):
  - `list(*, include_archived=False) -> list[Supplier]` (with `product_count`), `get(id) -> Supplier | None`, `create(body: SupplierCreate) -> Supplier`, `update(id, patch: SupplierPatch) -> Supplier | None` (`archived: bool` in the patch).
  - `products(supplier_id: UUID, *, include_archived=False) -> list[SupplierProduct]` and `product(id) -> SupplierProduct | None`, each with `newest_price: PricePoint | None`; `add_product(supplier_id, body: ProductCreate) -> SupplierProduct | None` (None for no supplier), `update_product(id, patch: ProductPatch)`.
  - `prices(product_id) -> list[PricePoint]` (newest `on_date` first, then `created_at`), `add_price(product_id, body: PricePointCreate) -> PricePoint | None` (source `manual`; `currency` defaults to `shop_currency()`, `on_date` to today in UTC), `delete_price(id) -> bool` (`PriceFromInvoiceError(ValueError)` for an invoice point).
  - `type_prices(type_id) -> list[TypePricePoint]` (a `PricePoint` plus `supplier_id`, `supplier_name`, `sku`), newest first, across the type's products: the Types page's price history.
  - `price_sheet(supplier_id) -> list[PriceSheetRow] | None`: one row per unarchived product: `product`, `type_code`, `type_label` (colour name or code), `newest`, `previous`, `change_pct: Decimal | None` ((newest.price_per_kg - previous.price_per_kg) / previous.price_per_kg * 100, one place; `None` without two points).
  - Errors: `SupplierExistsError`, `ProductExistsError` (both `ValueError`, from `UniqueViolation`), `ProductRefError` (a missing type).
  - `PricePoint.price_per_kg: Decimal | None` is `invoice_money.price_per_kg(price, product.pack_size, type.label_weight_g)`.
- Produces routes (all in `api/filament_purchasing.py`, `APIRouter(prefix="/filament", tags=["filament"])`): `GET/POST /suppliers`, `GET/PATCH /suppliers/{supplier_id}`, `GET/POST /suppliers/{supplier_id}/products`, `GET /suppliers/{supplier_id}/price-sheet`, `PATCH /products/{product_id}`, `GET/POST /products/{product_id}/prices`, `DELETE /prices/{price_id}`, `GET /types/{type_id}/prices`. 404 for a missing row, 409 for the three errors, 422 for a bad URL.
- Produces tools: `filament_supplier_list` (read; an optional `supplier_id` reads one, so its `routes` list both `GET /api/v1/filament/suppliers` and `GET /api/v1/filament/suppliers/{supplier_id}`), `filament_supplier_upsert` (write; add without `supplier_id`, else edit, `archived`), `filament_product_list` (read, by `supplier_id`), `filament_product_upsert` (write), `filament_price_list` (read, by `product_id` or `type_id`), `filament_price_add` (write; a manual point), `filament_price_sheet` (read). Deleting a manual price is the tool `filament_price_delete` (write).

- [ ] **Step 1: Failing tests.**
  - Store: `test_a_second_supplier_with_the_name_is_refused`; `test_a_product_names_a_type_and_a_unique_sku`; `test_price_per_kg_is_derived_from_the_types_label_weight` (pack 2, price `40.00`, label 1000 → `20.0000`; patch the type's `label_weight_g` to 500 → the same point reads `40.0000`); `test_the_sheet_shows_newest_previous_and_change` (points 20.00 on 2026-10-01 and 22.00 on 2026-11-01, pack 1, label 1000 → `change_pct == Decimal("10.0")`); `test_the_sheet_has_none_for_one_point`; `test_an_invoice_price_is_not_deletable` (inserted by SQL with a line: `delete_price` raises `PriceFromInvoiceError`, a manual one returns true).
  - API: `test_a_script_url_is_refused` (Review Focus 4: `POST /filament/suppliers` with `"website": "javascript:alert(1)"` and `POST …/products` with `"product_url": "data:text/html,x"` both 422); `test_a_price_defaults_to_the_shops_currency_and_today` (`orders_currency` `EUR` → `currency == "EUR"`); `test_a_price_is_a_string` (`response.json()["price"] == "21.4000"`, a `str`, never a number); `test_missing_rows_are_404`.
  - Agent: `filament_price_add` posts `{price: "21.40", on_date: "2026-11-17"}` to `POST /api/v1/filament/products/{id}/prices` (msw), and `coverage.test.ts` passes with the new routes.
- [ ] **Step 2:** Run the three files — FAIL.
- [ ] **Step 3:** Implement the store, the routes (`FilamentDep`, `actor(caller)` from `api/filament.py` where an actor is needed), the tools (`filament.ts`'s `defineTool` and `ok()` style) and the `Filament` field in `component.py`.
- [ ] **Step 4:** Run the backend files, ruff, mypy; `cd agent && pnpm gen:api && pnpm test filamentPurchasing coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): suppliers, supplier products and price points (#6266)`.

### Task 4: Invoice drafts

**Files:**
- Create: `backend/scadbuddy/filament/invoices.py`
- Modify: `backend/scadbuddy/filament/component.py` (`Filament.invoices: InvoiceStore`), `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/filamentPurchasing.ts`
- Test: `backend/tests/filament/test_invoices.py`, `backend/tests/api/test_filament_purchasing.py`, `agent/test/filamentPurchasing.test.ts`

**Interfaces:**
- Consumes: Tasks 1-2, `orders/currency.py` `minor_exponent`, the settings reader for `orders_currency` (passed in as `shop_currency: Callable[[], str]`, the same callable `SuppliersStore` gets, read on each use as `FilamentDefaults` is).
- Produces `InvoiceStore(pool, shop_currency, spools, types)` (`spools: FilamentSpoolStore` and `types: SpoolTypeStore` are used by Task 6's `confirm`; Task 4 stores them):
  - `create(body: InvoiceCreate, *, actor: str) -> tuple[InvoiceDetail, bool]` (`bool` true when made, false when an existing `import_run_id` matched). A missing supplier is `InvoiceRefError`; a taken number `DuplicateInvoiceError(existing_id: UUID)`, also raised by `update` when a typed number collides (a numberless draft gets its number later). `create` first SELECTs by `import_run_id`, so that match wins before the number index can fire. `InvoiceCreate.lines: list[InvoiceLineIn] = []` goes through `replace_lines`.
  - `get(id) -> InvoiceDetail | None`, `list(*, supplier_id=None, status=None) -> list[Invoice]` (newest `invoice_date`, then `created_at`, first), `update(id, patch: InvoicePatch) -> InvoiceDetail | None` (header fields; a confirmed invoice accepts only `note`, else `InvoiceConfirmedError`; a number that collides is `DuplicateInvoiceError`), `replace_lines(id, lines: Sequence[InvoiceLineIn]) -> InvoiceDetail | None` (draft only), `delete(id) -> bool` (draft only; releases the `filament_invoice` hold).
  - Every write that touches a draft ends in `_recompute(conn, invoice_id)`: `tax_per_line` → `line_total` → `allocate` → rows, in the same transaction. `line_subtotal` defaults to `quantity * unit_price` when not sent. `MixedLineTaxError` is a 422.
  - `Invoice` and `InvoiceDetail` carry `minor_exponent`, `difference`, `reconciles` (`abs(difference) <= RECONCILE_TOLERANCE`), `line_count`, and `file: FileRef | None`; a line carries `unit_cost` (`allocated_cost / quantity`, four places) and `spool_ids: list[UUID]` (spools whose `invoice_line_id` is the line).
- Produces routes: `GET/POST /invoices` (POST: 201 made, 200 for an existing `import_run_id`), `GET/PATCH/DELETE /invoices/{invoice_id}`, `PUT /invoices/{invoice_id}/lines`. 404 missing; 409 duplicate (the body carries `existing_invoice_id`, the first invoice's id, beside `detail`) and confirmed; 422 mixed tax.
- Produces tools: `filament_invoice_list` (read), `filament_invoice_get` (read), `filament_invoice_upsert` (write: `invoice_id` omitted creates, with `lines`; given edits the header and, when `lines` is sent, replaces them), `filament_invoice_delete` (write, drafts).

- [ ] **Step 1: Failing tests.**
  - `test_a_draft_allocates_shipping_and_receipt_tax` (two lines subtotal 40 and 60, invoice tax `7.00`, shipping `10.00`: `tax_basis == "receipt-wide 7.0%"`, `line_tax` `2.8000` and `4.2000`, line totals `42.8000` and `64.2000`, shipping shares `4.0000` and `6.0000` by value, `allocated_cost` `46.8000` and `70.2000`, summing to `117.0000 == 107 + 10`).
  - `test_line_tax_given_on_every_line_is_kept` (basis `line`); `test_mixed_line_tax_is_a_422` (API).
  - `test_the_draft_reports_its_difference` (total typed 117.04 → `difference == "0.0400"`, `reconciles` true; 118.00 → false).
  - `test_the_same_number_twice_is_refused` (Review Focus 1: store raises `DuplicateInvoiceError` with the first id; API 409 whose body has `existing_invoice_id` equal to the first id; one invoice row remains); `test_a_number_edit_onto_a_taken_number_is_a_409` (a numberless draft patched with the first invoice's number: `DuplicateInvoiceError`, API 409 with `existing_invoice_id`).
  - `test_a_second_import_run_id_returns_the_first` (store: `(detail, False)`, same id; API: 200 then the first call 201; the second call's lines are not applied).
  - `test_editing_lines_recomputes` (replace one line's quantity: allocated costs change; the sum still equals lines plus shipping).
  - `test_a_draft_deletes_and_a_confirmed_one_does_not` (set `status='confirmed'` by SQL: `delete` raises `InvoiceConfirmedError`, API 409; `replace_lines` and `update` of `shipping` likewise; `update` of `note` works).
  - `test_the_exponent_comes_from_the_currency` (`currency="JPY"` → `minor_exponent == 0`).
  - Agent: `filament_invoice_upsert` without an id posts to `POST /api/v1/filament/invoices` with its lines.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement the store, routes and tools. `create` reads `import_run_id` first (a match returns the existing invoice), then relies on the number's unique index: catch `UniqueViolation` and raise `DuplicateInvoiceError`; a race on `import_run_id` re-reads and returns the existing invoice.
- [ ] **Step 4:** Run backend files, ruff, mypy, `cd agent && pnpm gen:api && pnpm test filamentPurchasing coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): invoice drafts with tax and shipping allocation (#6266)`.

### Task 5: The invoice file

**Files:**
- Modify: `backend/scadbuddy/filament/invoices.py` (`set_file`, `file_row`), `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/coverage.ts`
- Test: `backend/tests/api/test_filament_purchasing.py`, `backend/tests/filament/test_invoices.py`

**Interfaces:**
- Consumes: `orders/attachments.py` `sniff`, `clean_filename`, `keep_file`, `blob_path` (the blob is `attachment-<sha256>`, shared with identical order attachments); `api/media.py` `receive_upload`; `ClaimStore`-free: the upload is read to a temp file in `paths.cache` and `keep_file(blobs, handle, digest)` publishes it.
- Produces `InvoiceStore.set_file(invoice_id, *, key: str, name: str, media_type: str, size: int) -> FileRef | None` (replaces a previous file: drops the old hold, inserts `blob_refs (key, 'filament_invoice', <invoice id>)` in the same transaction; a confirmed invoice accepts a file) and `InvoiceStore.file_key(invoice_id) -> tuple[str, FileRef] | None`.
- Produces routes: `POST /invoices/{invoice_id}/file` (multipart, one `file` part, `media_upload_max_bytes` limit; 404 before a byte is read for a missing invoice; 422 for no file, empty, or not a PDF or photo; answers `FileRef`) and `GET /invoices/{invoice_id}/file` (`FileResponse`; inline for `application/pdf` and photos, the headers of `get_attachment_content` (`NOSNIFF`, `INERT` for non-PDF) except `Cache-Control: private, no-cache` (never `immutable`: a replacement file keeps the URL); 404 when there is no file or its blob is gone). `FileRef` is `{name, media_type, size}`.
- Produces `NOT_A_TOOL` entries (`coverage.ts`) for both routes: "a multipart upload and a binary download; the assistant reaches an invoice's file as a blob pointer in #6267".

- [ ] **Step 1: Failing tests** (API, `TestClient`, multipart):
  - `test_a_pdf_is_kept_and_listed` (`%PDF-1.4…` bytes named `inv.pdf`: 200, `{"name": "inv.pdf", "media_type": "application/pdf", "size": N}`; `GET …/file` returns the bytes with `content-type: application/pdf`, `content-disposition` starting `inline`; the invoice's `file` equals the pointer; a `blob_refs` row `('filament_invoice', <id>)` exists).
  - `test_a_photo_is_kept` (PNG magic bytes → `image/png`).
  - `test_an_html_page_is_refused` (Review Focus 5: `<html><script>…` named `invoice.pdf` → 422, no `blob_refs` row, `file` still null); `test_an_svg_is_refused`; `test_an_empty_file_is_refused`.
  - `test_a_second_file_replaces_the_first` (one hold remains, on the new key; `GET …/file` then returns the new bytes).
  - `test_the_file_is_served_uncached` (`cache-control` is `private, no-cache` with no `immutable`; `x-content-type-options: nosniff` is set).
  - `test_deleting_a_draft_releases_its_blob` (`blob_refs` has no row for the holder after `DELETE /invoices/{id}`).
  - `test_the_pointer_has_no_bytes` (the invoice JSON has no `file_key` and no base64: `"base64" not in response.text`).
  - `test_a_missing_invoice_is_404_before_the_body_is_read`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement the two routes following `api/order_attachments.py` `_upload` and `get_attachment_content` (without the operation: Decision 14), and the two store methods.
- [ ] **Step 4:** Run backend files, ruff, mypy, `cd agent && pnpm test coverage` — PASS.
- [ ] **Step 5:** Commit `feat(filament): an invoice's file in the blob store (#6266)`.

### Task 6: Confirm an invoice

**Files:**
- Modify: `backend/scadbuddy/filament/spools.py` (`create_on`, `update_on`: optional `ref_kind`, `ref_id`), `backend/scadbuddy/filament/types.py` (`create_on`), `backend/scadbuddy/filament/invoices.py` (`confirm`), `backend/scadbuddy/filament/component.py` (`InvoiceStore` gets the spool and type stores), `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/filamentPurchasing.ts`
- Test: `backend/tests/filament/test_spools.py`, `backend/tests/filament/test_invoice_confirm.py`, `backend/tests/api/test_filament_purchasing.py`, `backend/tests/orders/test_spools.py`, `agent/test/filamentPurchasing.test.ts`
- Order inside the task: the `ref_kind` and `ref_id` arguments (`spools.py`) first, then `types.create_on`, then `confirm`.

**Interfaces:**
- Consumes: Tasks 1-5; `FilamentSpoolStore.create_on(conn, body: SpoolCreate, *, actor, ref_kind: str | None = None, ref_id: UUID | None = None) -> UUID` and `update_on(conn, spool_id, patch: SpoolPatch, *, actor, ref_kind: str | None = None, ref_id: UUID | None = None) -> bool` (both pass the pair to `ledger.record`; existing callers are unchanged); `ledger.record(conn, spool_id, kind, *, source, absolute_g, ref_kind, ref_id, actor, data)`.
- Produces `SpoolTypeStore.create_on(conn: Conn, body: SpoolTypeCreate) -> SpoolType` (the body of today's `create`, which now wraps it in `self._orders.transaction()`; behaviour of `create` unchanged, `test_types.py` passes unchanged).
- Uses `InvoiceStore(pool, shop_currency, spools: FilamentSpoolStore, types: SpoolTypeStore)`, built in `component._build`.
- Produces `InvoiceStore.confirm(invoice_id: UUID, *, actor: str) -> tuple[InvoiceDetail, bool] | None` (`None` no such invoice; `bool` false when it was already confirmed). One transaction: lock the invoice row `FOR UPDATE`; if confirmed, return it. Checks, each a typed `ValueError` that rolls everything back: `InvoiceNoLinesError`; `InvoiceCurrencyError` (not `orders_currency`, Decision 6); `InvoiceNotReconciledError(difference)` (Decision 7); `InvoiceLineError(position, reason)` for a line with no type and no `proposed_type`, a product found with another type, or a spool that is archived, already has an invoice line or its own cost, or has another type (Decision 11). `types.create_on` keeps raw database errors (`create` maps them), so `confirm` catches `UniqueViolation` and foreign-key errors from it (a `proposed_type` that duplicates a type, or names a missing catalog row) and raises `InvoiceLineError(position, reason)` (409, never a 500). Then per line, in order: resolve the type (`create_on` for `proposed_type`); resolve the product (Decision 13); for each id in `update_spool_ids`, `update_on(... SpoolPatch(type_id=..., cost_per_kg=...))`; `quantity - len(update_spool_ids)` new spools through `create_on(SpoolCreate(type_id, cost_per_kg, grams_left=<effective label weight>))`; each `create_on` and `update_on` call passes `ref_kind='invoice_line'`, `ref_id=<line id>`; every touched spool gets `invoice_line_id` by one `UPDATE` and, for a new one, its `created` event is already written with actor `invoice:<id>` (a second `ledger.record(..., "edited"...)` is not needed for new spools; for an updated one `update_on`'s `edited` event is); a `filament_price_points` row (source `invoice`, `price_per_pack(line_total, quantity, product.pack_size)`, `on_date` the invoice date, `currency` the invoice's); finally `status='confirmed'`, `confirmed_at=now()`, the line's resolved `type_id` and `supplier_product_id` written back.
- Produces route `POST /invoices/{invoice_id}/confirm` (200 with `InvoiceDetail`; the already-confirmed repeat is also 200; 404; 409 for each error, message text names the line or the difference). Tool `filament_invoice_confirm`: **risk `outward`** (it creates spools and costs; spec §9 step 6 "asks for approval"), `routes: ['POST /api/v1/filament/invoices/{invoice_id}/confirm']`.

- [ ] **Step 1: Failing tests.**
  - `test_confirm_makes_spools_with_cost_and_link` (one line, qty 2, no shipping, line total 42.8, type label 1000 g: two spools, each `cost_per_kg == Decimal("21.4000")`, `invoice_line_id` set, `grams_left == 1000`, a `created` event with actor `invoice:<id>`; a price point `price == Decimal("21.4000")` for pack 1; status `confirmed`).
  - `test_confirm_twice_creates_nothing_more` (Review Focus 3: second call returns `(detail, False)`; spool count and price point count unchanged) and `test_two_confirms_at_once_make_one_set` (two threads via `ThreadPoolExecutor`: exactly `quantity` spools).
  - `test_a_line_fills_an_existing_costless_spool` (qty 3, one `update_spool_ids` spool that has no type: it gets the type and the cost, plus two new spools; three spools carry the line).
  - `test_a_created_or_updated_spools_event_carries_the_invoice_line_ref` (`ref_kind == 'invoice_line'`, `ref_id` the line id, actor `invoice:<id>`) and, in `test_spools.py`, `test_a_retried_event_with_the_same_ref_is_idempotent` (`create_on` or `update_on` twice with one `ref_kind` and `ref_id`: one event row).
  - `test_a_duplicate_proposed_type_is_a_409_line_error` (a `proposed_type` that equals an existing type: `InvoiceLineError` naming the position, API 409, invoice still `draft`).
  - `test_a_costed_spool_stops_the_whole_confirm` (a spool with its own `cost_per_kg`: 409, `InvoiceLineError` naming position 1; no spool, no price point, invoice still `draft` — the transaction rolled back).
  - `test_a_foreign_currency_is_refused_and_stays_a_draft`; `test_a_total_off_by_more_than_five_cents_is_refused` and `…_by_four_cents_confirms`; `test_an_empty_invoice_is_refused`.
  - `test_a_new_type_and_product_are_made_at_confirm` (`proposed_type` and no `sku`: the type exists, the product's `sku == line.description`).
  - `test_a_sku_that_names_another_type_is_refused`.
  - `test_the_product_is_found_by_sku_case_insensitively` (a second invoice with `sku="a00-b01"` reuses product `A00-B01`; one product row).
  - `test_an_invoiced_spools_cost_reaches_the_price_book` (`orders/test_spools.py` pattern: confirm, then `spool_groups`/import for the type's group: the book record's `cost_per_kg` is the invoiced spool's `21.4000`; no other orders code changed).
  - Agent: `filament_invoice_confirm` posts to the route; its tier is `outward` (`registry` test pattern in `agent/test/filament.test.ts`).
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement. The per-line work is the order listed in Interfaces; keep it in one private method per step so the transaction reads top to bottom.
- [ ] **Step 4:** Run backend files, `tests/filament/test_spools.py`, `tests/filament/test_types.py`, `tests/orders/test_spools.py`, ruff, mypy; `cd agent && pnpm gen:api && pnpm test filamentPurchasing coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): confirm an invoice into spools, costs and price points (#6266)`.

### Task 7: Spool invoice link and supplier stats

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolDetail.invoice: SpoolInvoiceRef | None = None`; additive, never a required field: `…#6262 Task 5` lesson), `backend/scadbuddy/filament/spools.py` (the detail read), `backend/scadbuddy/filament/suppliers.py` (`stats`), `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/filamentPurchasing.ts`, `agent/src/tools/filament.ts` (`filament_spool_get`'s description)
- Test: `backend/tests/filament/test_spool_detail.py`, `backend/tests/filament/test_suppliers.py`, `backend/tests/api/test_filament_purchasing.py`

**Interfaces:**
- Produces `SpoolInvoiceRef` and `SpoolTypeSupplier` in `filament/models.py` (not `purchasing_models.py`: that file imports from `models.py`, so the reverse import would be circular). `SpoolInvoiceRef`: `invoice_id`, `number`, `invoice_date`, `supplier_id`, `supplier_name`, `line_id`, `line_position`, `unit_cost: Decimal`. `SpoolDetail.invoice` is it for a spool whose `invoice_line_id` is set, else `None`. `SpoolDetail.type_suppliers: list[SpoolTypeSupplier] = []`, where `SpoolTypeSupplier` is (`supplier_id`, `supplier_name`, `sku`, `product_url`, `newest_price: Decimal | None`, `price_per_kg: Decimal | None`) for the spool's type's unarchived products (Decision 16).
- Produces `SuppliersStore.stats() -> list[SupplierStats]` and route `GET /suppliers/stats` (declared before `/suppliers/{supplier_id}` so the path wins): per supplier with confirmed invoices, `spool_count` (spools whose line's invoice is the supplier's), `remaining_g` (their unarchived `grams_left`), `consumed_g` (one SQL over the spool events: `sum(-delta_g)` of events with source `print_actual`, plus those with source `print_estimate` that have no `print_ended` event of the same `(ref_kind, ref_id)`: the ledger's own rule; there is no `ledger.consumed_g`), `spend` (`sum(allocated_cost)` of their confirmed invoices' lines). Sorted by `consumed_g` descending, then name (BB's order).
- Produces tool `filament_supplier_stats` (read). `filament_spool_get`'s description mentions `invoice` and `type_suppliers`.

- [ ] **Step 1: Failing tests:** `test_a_spool_detail_names_its_invoice` (after a confirm: `detail.invoice.number == "R-1001"`, `supplier_name`, `unit_cost == Decimal("21.4000")`); `test_a_spool_with_no_invoice_has_none`; `test_the_detail_lists_the_types_suppliers` (two suppliers' products of one type; each with its newest price per kg); `test_stats_count_spend_and_stock` (two confirmed invoices of one supplier, one `print_actual` event of 50 g on one spool and one `print_estimate` of 30 g whose `(ref_kind, ref_id)` also has a `print_ended` event: `spool_count == 3`, `consumed_g == 50.0` (the covered estimate is dropped), `spend` is the sum of both confirmed invoices' `allocated_cost`; a draft invoice has no spools and adds no spend); `test_a_supplier_with_only_drafts_is_left_out`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend files, ruff, mypy, `pnpm gen:api && pnpm typecheck` in `frontend/` (the new optional fields must not break any fixture) and `agent/` with `pnpm test filamentPurchasing coverage` — PASS.
- [ ] **Step 5:** Commit `feat(filament): a spool's invoice and supplier stats (#6266)`.

### Task 8: Shopping list

**Files:**
- Create: `backend/scadbuddy/filament/shopping.py`
- Modify: `backend/scadbuddy/filament/component.py` (`Filament.shopping`), `backend/scadbuddy/api/filament_purchasing.py`, `agent/src/tools/filamentPurchasing.ts`
- Test: `backend/tests/filament/test_shopping.py`, `backend/tests/api/test_filament_purchasing.py`, `agent/test/filamentPurchasing.test.ts`

**Interfaces:**
- Produces `ShoppingStore(pool)` (annotations use module aliases such as `_Items = list[ShoppingItem]`, because the method `list` shadows the builtin): `list(*, status=None) -> list[ShoppingItem]` (pending first, then newest `added_at`), `add(body: ShoppingItemCreate) -> ShoppingItem` (`type_id` given fills material, subtype, brand and colour name from the type's catalog names when the body leaves them out; a missing type is `ShoppingRefError`; with no `type_id` `material` is required), `update(id, patch: ShoppingItemPatch) -> ShoppingItem | None` (`status`, `quantity_spools`, `note`; `purchased` sets `purchased_at = now()`, `pending` clears it), `remove(id) -> bool`, `clear(*, purchased_only: bool) -> int`.
- Produces routes `GET/POST /shopping-list`, `PATCH/DELETE /shopping-list/{item_id}`, `DELETE /shopping-list?purchased_only=` (200 `{removed: int}`; default `purchased_only=false`, BB's "clear all"). Tools: `filament_shopping_list` (read), `filament_shopping_add` (write), `filament_shopping_update` (write, status, quantity, note), `filament_shopping_remove` (write; `item_id` removes one; a clear needs an explicit `clear: "all" | "purchased"`, see "Decisions made while building").

- [ ] **Step 1: Failing tests:** `test_an_item_from_a_type_takes_its_names` (type PLA / Bambu Lab / Basic / "Misty Blue": item material `PLA`, brand `Bambu Lab`, subtype `Basic`, `colour_name == "Misty Blue"`, `type_id` set); `test_an_item_survives_its_types_archive` (archive the type: the item still lists with its text); `test_marking_purchased_stamps_the_time_and_pending_clears_it`; `test_clear_purchased_only_keeps_pending` (3 items: 1 purchased, 2 pending → `clear(purchased_only=True) == 1`); `test_a_zero_quantity_is_a_422` (API); `test_an_item_with_neither_type_nor_material_is_a_422`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run backend files, ruff, mypy, `cd agent && pnpm gen:api && pnpm test filamentPurchasing coverage && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the shopping list (#6266)`.

---

## Part B — frontend

Design direction for every task below (the implementer still runs the frontend-design process; this is the brief). The subject is a shop's paperwork: a supplier's price list and a receipt. The one memorable element is the **reconciliation desk** on the invoice page: the file on the left (a PDF or photo), the lines on the right, and a totals ledger under the lines with the difference in tabular figures that turns from a warning to a settled mark as the numbers agree. Everything else stays quiet and uses the app's tokens, shadcn components and the Filament section's own spool chip and RGBA swatch (`RgbaSwatch`); no new palette. Money is right-aligned with tabular figures. At phone width the file stacks above the lines and the lines become cards. Bambu Studio's colour code (`Misty Blue (30108)`) shows beside the swatch.

### Task 9: Client, decimal helper, mocks and tabs

**Files:**
- Create: `frontend/src/api/filamentPurchasing.ts`, `frontend/src/lib/decimal.ts`, `frontend/src/mocks/features/filamentPurchasing.ts`, `frontend/src/lib/decimal.test.ts`
- Modify: `frontend/e2e/page-titles.spec.ts` (its `PAGES` list gains the three tab routes, a seeded supplier id and a seeded invoice id; it fails on any route `App.tsx` declares that it lacks), `frontend/CLAUDE.md` (the sentence that says the Suppliers, Invoices and Shopping list tabs are not available yet: fixed here, not in Task 14), `frontend/src/pages/filament/tabs.ts` (`suppliers`, `invoices`, `shopping-list` become `available: true`; the header comment names only Metrics as pending #6269), `frontend/src/App.tsx` (literal `path="filament/suppliers"`, `"filament/invoices"`, `"filament/shopping-list"` routes with `FilamentPage tab=…`, and `filament/suppliers/:id`, `filament/invoices/:id`; declared in `FILAMENT_TABS` order, because `tabs.test.ts` compares the routes with the tabs: suppliers and invoices after tags, shopping-list after catalog), `frontend/src/pages/filament/FilamentPage.tsx` (renders the three tabs; a placeholder body until Tasks 10-13), `frontend/src/mocks/features/filament/index.ts` (exports `addInvoicedSpools(typeId, count, costPerKg, lineId)` that Task 9's confirm mock calls lazily; the spool-detail mock returns `invoice` and `type_suppliers` for spools made by it)
- Test: `frontend/src/pages/filament/tabs.test.ts`, `FilamentPage.test.tsx`, `mocks/features.test.ts`

**Interfaces:**
- `lib/decimal.ts`: `type Decimal = string`; `parseDecimal(text: string): string | null` (accepts `12`, `12.5`, `12,5`; returns the canonical dot form, `null` for anything else; never goes through `Number`); `addDecimals(a, b)`, `subDecimals(a, b)`, `mulDecimal(a, intFactor: number)`, `cmpDecimals(a, b): -1 | 0 | 1`, all exact on strings (BigInt over the longest scale); `formatCost(value: string, currency: string, exponent: number): string` (`Intl.NumberFormat` currency style with `minimumFractionDigits: exponent` and `maximumFractionDigits: exponent + 2`, the digits given, never `Intl`'s own, as `lib/money.ts` `formatUnitCost` does).
- `api/filamentPurchasing.ts`: one typed function per route, from `schema.d.ts`: `listSuppliers`, `getSupplier`, `createSupplier`, `patchSupplier`, `listProducts`, `createProduct`, `patchProduct`, `listPrices`, `addPrice`, `deletePrice`, `getPriceSheet`, `listTypePrices`, `getSupplierStats`, `listInvoices`, `getInvoice`, `createInvoice`, `patchInvoice`, `putInvoiceLines`, `deleteInvoice`, `uploadInvoiceFile` (multipart `FormData`, the browser's own `File`), `confirmInvoice`, `listShopping`, `addShopping`, `patchShopping`, `removeShopping`, `clearShopping`. The invoice file URL is `invoiceFileUrl(id)`, an `appUrl`-free API path.
- The spool-detail mock returns `invoice` (a `SpoolInvoiceRef`: number, supplier, line, `unit_cost`) for spools made by `addInvoicedSpools`, and `type_suppliers` from the seeded products of the spool's type; Task 12's `SpoolPage` test needs both.
- Mocks: every route of Tasks 3-8 over an in-memory seed (3 suppliers, 5 products with price histories, two drafts and two confirmed invoices, a shopping list of 4 with one purchased). Confirm runs the same arithmetic as the backend (`lib/decimal.ts`) and calls `addInvoicedSpools`. It refuses what the routes refuse, with the same status and text (duplicate number 409 with `existing_invoice_id`, confirmed 409, bad URL 422, wrong currency 409).

- [ ] **Step 1: Failing tests:** `parseDecimal('12,5') === '12.5'` and `parseDecimal('1e3') === null`; `addDecimals('0.1', '0.2') === '0.3'`; `subDecimals('117.04', '117') === '0.04'`; `cmpDecimals('10.0', '10') === 0`; `formatCost('21.4', 'EUR', 2)` contains `21.40`; `tabs.test.ts`'s route list equals the nine available tabs of `FILAMENT_TABS` in order (Metrics absent); `FilamentPage` shows nine tabs including "Invoices" and no "Metrics"; `e2e/page-titles.spec.ts` covers every route `App.tsx` declares; `mocks/features.test.ts` passes (one module per route).
- [ ] **Step 2:** Run `pnpm exec vitest run src/lib/decimal.test.ts src/pages/filament src/mocks/features.test.ts` — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run them, `pnpm gen:api && pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): purchasing client, decimal helper and mocks (#6266)`.

### Task 10: Suppliers tab and supplier page

**Files:**
- Create: `frontend/src/pages/filament/SuppliersTab.tsx`, `SupplierPage.tsx`, `frontend/src/components/filament/purchasing/ProductsTable.tsx`, `PriceSheet.tsx`, `AddPriceForm.tsx`
- Test: `frontend/src/pages/filament/SuppliersTab.test.tsx`, `SupplierPage.test.tsx`, `components/filament/purchasing/PriceSheet.test.tsx`

**Interfaces:**
- Consumes Task 9's client and `lib/decimal.ts`.
- `SuppliersTab`: a table (name, website as a safe link, customer number, products, spools bought, spend from `getSupplierStats`), "Add supplier" opening a `Sheet` form (name, website, customer number, note; validation refuses a non-`http(s)` website before the request), archive toggle, "Show archived".
- `SupplierPage` at `/filament/suppliers/:id`: the editor, `ProductsTable` (SKU, type chip with `RgbaSwatch`, pack size, product URL, newest price with its per-kg and date; an inline "Add price" row, the shared `AddPriceForm({ productId, currency, onAdded })`: price "with tax", date defaulting to today, currency read-only; Task 11 reuses it), and `PriceSheet` (`PriceSheetRow`s: newest, previous, a `+x.x %`/`-x.x %` change chip; empty change shown as a dash). Adding a product picks a type from `listTypes`. Unsaved edits hold navigation (`lib/useLeaveGuard.ts`). `SuppliersTab`, `SupplierPage`, `InvoicesTab`, `InvoicePage` and `ShoppingListTab` each render an `<h1>` (`page-titles.spec.ts`).

- [ ] **Step 1: Failing tests:** the list shows each supplier's name and spend; adding a supplier with website `javascript:x` shows an inline error and sends no request; adding a price sends `{price: "21.40", on_date}` as a string; the sheet shows `+10.0 %` for the seeded rise and a dash for a one-point product; an archived supplier is hidden until "Show archived"; a product's URL renders as an anchor with `rel="noopener noreferrer"`.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement with the shared `Select`, `Sheet`, `Input` and `Button`.
- [ ] **Step 4:** Run them, `pnpm lint && pnpm typecheck && pnpm exec vitest run src/pages/filament src/components/filament` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Suppliers tab and price sheets (#6266)`.

### Task 11: Prices on the Types page

**Files:**
- Create: `frontend/src/components/filament/purchasing/PricePanel.tsx`
- Modify: `frontend/src/pages/filament/TypePage.tsx`, `frontend/src/pages/filament/TypesTab.tsx` (a "Newest price" column), `frontend/src/mocks/features/filamentPurchasing.ts` if a route is missing
- Test: `frontend/src/components/filament/purchasing/PricePanel.test.tsx`, `frontend/src/pages/filament/TypePage.test.tsx`

**Interfaces:**
- `PricePanel({ typeId, currency, exponent, onUseAsCost })` (its "Add price" is Task 10's `AddPriceForm`, not a second copy): the type's supplier products and price history from `listTypePrices` (newest first, per supplier and SKU, per-pack price and per-kg), a small sparkline of per-kg over time (inline SVG, no chart library), "Add price" and "Add supplier product" (a `Sheet`), and a button per supplier "Use as default cost" that calls `onUseAsCost(pricePerKg)`, which `TypePage` puts in its cost field as an unsaved edit (Decision 1: never saved by itself). Shown only for a saved type.

- [ ] **Step 1: Failing tests:** the panel lists the seeded history newest first; "Use as default cost" sets the type's cost field to the point's per-kg and sends no request until Save; a type with no price shows "No prices yet"; the Types tab shows the newest per-kg price.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pnpm exec vitest run src/pages/filament src/components/filament`, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): price history on the Types page (#6266)`.

### Task 12: Invoices tab and the invoice page

**Files:**
- Create: `frontend/src/pages/filament/InvoicesTab.tsx`, `InvoicePage.tsx`, `frontend/src/components/filament/purchasing/InvoiceUpload.tsx`, `InvoiceHeaderForm.tsx`, `InvoiceLinesTable.tsx`, `InvoiceTotals.tsx`, `InvoiceFile.tsx`, `SpoolMatchPicker.tsx`, `invoicesQuery.ts`
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (Colour and cost section: an "Invoice" row linking to `/filament/invoices/<id>` with supplier, number and unit cost; the type's suppliers listed in the Printers-free "Purchasing" line), `frontend/src/api/types.ts` (re-exports)
- Test: `InvoicesTab.test.tsx`, `InvoicePage.test.tsx`, `components/filament/purchasing/InvoiceTotals.test.tsx`, `InvoiceUpload.test.tsx`, `invoicesQuery.test.ts`, `SpoolPage.test.tsx`

**Interfaces:**
- `invoicesQuery.ts`: `InvoicesQuery { status: 'all' | 'draft' | 'confirmed'; supplier?: string }`, `readQuery(params)`, `writeQuery(q)`, `rememberedQuery(): InvoicesQuery | null` and `remember(q)` over `localStorage` in `try/catch` (page renders without it); a visit to `/filament/invoices` with no query opens the remembered view; "Remember my view" is a `Switch` as the Orders list's.
- `InvoiceUpload({ onCreated })`: a drop zone, a file button and a paste handler (`onPaste` reads `clipboardData.files`); accepts a PDF or an image (client check by type, the server's sniff is the truth); the supplier `Select` defaults to the remembered last supplier; it posts `createInvoice` (draft with today's date and the shop currency, zeros) then `uploadInvoiceFile`, and opens the draft. A failed upload leaves the draft and says so.
- `InvoicePage` at `/filament/invoices/:id`: split pane (`lg:` two columns; stacked on a phone). `InvoiceFile` shows the PDF in an `<object>` or the photo in an `<img>` (the file URL), "Open" (anchor, `target=_blank`) and "Download" (`downloadBlob` from `lib/embed.ts`). `InvoiceHeaderForm` edits supplier, number, date, subtotal, shipping, tax, total (decimal strings through `parseDecimal`; a bad number is an inline error). `InvoiceLinesTable` edits each line: description, SKU, colour code with the swatch, quantity, unit price, line tax (empty on every line means receipt-wide), type (`Select` from `listTypes`, with a "New type" link to `/filament/types/new` in a new tab), supplier product (suggested by SKU), and `SpoolMatchPicker` ("Fills existing spools": the unarchived spools with no cost and no invoice of that type or with no type, a checkbox list). The table autosaves a row by `putInvoiceLines` on blur, debounced; the server's allocated cost, tax and basis show read-only per line. `InvoiceTotals` shows subtotal, tax (with the basis), shipping, line sum, typed total, and **difference** with a status mark: settled when `reconciles`, a warning with the amount otherwise.
- "Confirm" (`Button`) is disabled with the reason while a line has no type, the currency is not the shop's, or `reconciles` is false; its click opens an `AlertDialog` listing "N spools will be made, M filled, K price points" before `confirmInvoice`. A confirmed invoice is read-only: the lines show their spools (`SpoolLink`), and only the note and the file are editable. "Delete draft" asks to confirm.

- [ ] **Step 1: Failing tests:** `InvoiceTotals` shows difference `0.04` and "reconciles" for `117.04` typed against `117.00` of lines and the warning with `0.50` for `117.50`; the page's Confirm is disabled with "Line 2 has no type" until a type is picked, then enabled; confirm sends one `POST …/confirm` and then shows the invoice read-only with its spools linked; pasting an image into the upload creates a draft and uploads once (`createInvoice` then `uploadInvoiceFile`, request order asserted); an upload failure leaves the draft and shows "The draft was saved; the file was not"; a second invoice with a used number shows the 409 text with a link to the first, built from the body's `existing_invoice_id` (no parsing of `detail`); a line with no line tax on any line shows "receipt-wide 7.0%" from the response; the list's remembered view reopens (`invoicesQuery.test.ts` with `localStorage` throwing: the page still renders); the spool page shows "Invoice R-1001 · Acme" linking to the invoice.
- [ ] **Step 2:** Run `pnpm exec vitest run src/pages/filament src/components/filament` — FAIL.
- [ ] **Step 3:** Implement. `InvoicesTab` is a table (date, supplier, number, status chip, total, spools, file icon) with a status and supplier filter.
- [ ] **Step 4:** Run them, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the Invoices tab and the invoice page (#6266)`.

### Task 13: Shopping list tab

**Files:**
- Create: `frontend/src/pages/filament/ShoppingListTab.tsx`
- Modify: `frontend/src/pages/filament/TypePage.tsx` ("Add to shopping list" button), `frontend/src/pages/filament/TypesTab.tsx` (row action)
- Test: `frontend/src/pages/filament/ShoppingListTab.test.tsx`, `TypePage.test.tsx`

**Interfaces:**
- A table of items (swatch from the type when `type_id` is set, material, subtype, brand, colour name, spools, note, status), a form row to add by type (`Select` from `listTypes`, quantity) or by text, a status toggle per item, remove, "Clear purchased" and "Clear all" (`AlertDialog`). The remembered choice: the last quantity used.

- [ ] **Step 1: Failing tests:** adding from a type posts `{type_id, quantity_spools: 2}`; marking purchased sends `PATCH {status: "purchased"}` and moves the row below the pending ones; "Clear purchased" sends `DELETE …?purchased_only=true`; "Add to shopping list" on the type page adds one pending item; quantity `0` is refused inline.
- [ ] **Step 2:** Run — FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pnpm exec vitest run src/pages/filament`, `pnpm lint && pnpm typecheck` — PASS.
- [ ] **Step 5:** Commit `feat(filament): the shopping list tab (#6266)`.

---

## Part C — close

### Task 14: End to end, docs and the PR

**Files:**
- Create: `frontend/e2e/filament-purchasing.spec.ts`
- Modify: `backend/CLAUDE.md` (the `filament/` entry: the three new stores, `invoice_money.py`, the routes, the confirm rules and Decisions 1, 4-8 in a few lines), `frontend/CLAUDE.md` (the Filament section entry: `lib/decimal.ts`, the mock module, the e2e spec; the tab sentence was fixed in Task 9), `docs/superpowers/specs/2026-11-14-filament-management-design.md` (§5.8: add the decisions as "As built (#6266)": price point basis, quantity counts spools, tax and shipping rules, frozen confirmed invoices, shopping list statuses; §10 unchanged; §12 row #6266 unchanged), `plugins/CLAUDE.md` only if it names the assistant's filament tools (it must not go stale)
- Test: `frontend/e2e/filament-purchasing.spec.ts`

**Interfaces:**
- E2E (msw-mocked Playwright against `pnpm preview`): `a supplier, a price, an invoice and its spools` — open Filament → Suppliers, add "Acme" and a product with a price, open Invoices, paste a PDF, fill two lines (receipt-wide tax, shipping), see difference `0.00` settled, confirm, land on the read-only invoice, follow a spool link and see "Invoice" with its unit cost; `a total that does not add up cannot be confirmed` (Confirm disabled with the difference shown).

- [ ] **Step 1: Failing e2e** as above.
- [ ] **Step 2:** Run `cd frontend && pnpm exec playwright test e2e/filament-purchasing.spec.ts` — FAIL.
- [ ] **Step 3:** Fix what the spec finds; write the docs. The full backend suite for the touched trees, `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`, `cd frontend && pnpm lint && pnpm typecheck && pnpm exec vitest run src/pages/filament src/components/filament src/mocks && pnpm exec playwright test e2e/filament-purchasing.spec.ts e2e/filament.spec.ts`, `cd agent && pnpm test && pnpm typecheck`, `.github/scripts/lint-base64-images.sh`.
- [ ] **Step 4:** Grep every note this PR may have made false: `grep -rn "#6266\|not available until\|Suppliers, Invoices" frontend/src backend/scadbuddy agent/src docs/superpowers/specs`; fix each.
- [ ] **Step 5:** Commit `feat(filament): suppliers, price sheets and invoices end to end (#6266)`. The controller opens the PR `feat(filament): suppliers, supplier products, price sheets and invoices (#6266)`, body `Closes #6266`, and shepherds it.

---

## What #6267 relies on

Left in place, so #6267 adds no schema to these tables:
- `filament_invoices.import_run_id` (unique) and `POST /filament/invoices` returning the existing invoice for a repeated run id (Decision 9): the flow's retry key.
- `InvoiceLineIn.proposed_type` (a `SpoolTypeCreate`), `update_spool_ids`, `colour_code` and `sku` as inputs (`tax_basis` is output only, computed by the server): the draft the session writes (spec §9 steps 4-5) through `filament_invoice_upsert`, visible and editable on the Invoices page.
- `POST /filament/invoices/{id}/confirm`: one transaction, idempotent by state (spec §9 step 6); `filament_invoice_confirm` is the chat's approval-gated tool.
- `POST /filament/invoices/{id}/file`: the flow stores the uploaded PDF or image here. A blob-pointer variant for the assistant (`{name, mediaType}`) and a `crud_router` area for the profile-fetch worker are #6267's; the two routes are `NOT_A_TOOL` until then.
- `SpoolTypeStore.create_on` (the confirm's own product lookup by `upper(sku)` is SQL in `InvoiceStore`, not a `SuppliersStore` method): the diff-before-create step reads types, products and spools through the existing `filament_*_list` tools.
- Not here: the import prompt and flow, Bambu colour codes to RGBA, 3DFP profiles and the worker, the `filament` plugin skill and the `print` skill change.

## Self-review

- **Spec coverage:** §5.8 suppliers (Task 3), invoices with lines, tax basis, colour code, allocated cost, file blob, status, import run id (Tasks 1, 4-6), shopping list (Task 8); §5.2 supplier products and price points (Tasks 1, 3, 11), `sku_settings` deferred (Decision 16); §9 cost rules, diff-before-create for existing spools, SKU rule, "every spool gets a cost and a source" (Tasks 2, 6, 7); §9 steps 1 (upload or paste, Task 12) and 6 (confirm in one transaction); steps 2-5 and 7-8 are #6267. §10 Suppliers, Invoices, Shopping list tabs (Tasks 9-13). §11 parity: suppliers, supplier stats, shopping list, per-spool suppliers (Decision 16), SKU settings and forecast to #6269. §13: names and the price book (Decision 1). Agent tools for each route: Tasks 3-8, with two `NOT_A_TOOL` entries (Task 5). Gap noted: no purchase-order flow (spec non-goal).
- **Steps:** every test step names its tests and values; code steps give signatures and the one algorithm (`spread`); no step says "handle errors".
- **Types:** `FileRef`, `InvoiceDetail`, `SpoolInvoiceRef`, `PricePoint.price_per_kg`, `confirm`'s return `tuple[InvoiceDetail, bool]` and `create`'s `tuple[InvoiceDetail, bool]` are used the same way in Tasks 4, 6, 7, 9 and 12. `create_on` is defined in Task 6 and used only there and by #6267.
- **Review Focus:** all five lines have a test in the task named in brackets.
- **Proportion:** the plan is about the length of the spec; code blocks are signatures and test names.
