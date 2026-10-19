# Orders Phase 1: Customers and Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Customers, orders and line items as ScadBuddy's own records (#4249, epic #4248): a customer with any number of contact points and addresses; an order of line items, each pinning one template commit with its inputs, library commits and colours; a price typed by hand; an order whose state past `accepted` is derived from its items and whose close refuses while any item is neither fulfilled nor cancelled; the pages, the mocks and the agent tools for all of it. Nothing here renders, slices, prints or reaches Bambuddy.

**Architecture:** A new feature package `backend/scadbuddy/orders/` holds the rows (`store.py`, over its own small pool as `print_library/component.py` opens one), the wire models (`models.py`), the lifecycle (`lifecycle.py`: every transition and the derivation of an order's status from its items and shipments, run under the order's row lock), the order numbers (`numbers.py`) and the component (`component.py`, `ORDERS`, discovered as every component is). Routes are `api/customers.py` and `api/orders.py`, plain reads and writes: nothing in this phase does work that an operation kind would run (the slice quote is phase 2, #4250). Every write publishes `order.changed` or `order_item.changed` on the event bus and the realtime `orders` topic relays it. The frontend gets four pages (`/orders`, `/orders/:id`, `/customers`, `/customers/:id`) on generic table, filter and drawer components extracted from the Activity page, the Customize page's "Add to order…" and an output's "Order this". The agent gets one `defineTool` per route group.

**Tech Stack:** Python 3.12, psycopg 3, FastAPI, pydantic 2; React 19, Vite, msw, vitest, Playwright; TypeScript on the Claude Agent SDK (the agent's tool registry).

**Spec:** `docs/superpowers/specs/2026-10-18-orders-design.md` §3 (3.1-3.6), §4, §6.1 and §6.3 as far as a person writes them, §7.3, §9's last two paragraphs, §10, §11 phase 1.

## Global Constraints

- Everything is Postgres, in new migration files `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (CLAUDE.md "Migrations"). The tables are those of spec §3.1-3.4, §6.1 (the hand-written columns only, Ruling 3) and §6.3.
- A new backend service is a `Component` in `scadbuddy/<feature>/component.py`, never an `AppState` field; routes reach it through `component_dep` (`api/components.py`).
- No body carries an author, a principal or a status the store derives (`extra="forbid"`; spec Ruling 12). `created_by` and `order_events.by` come from `core/authorship.py` `current_author()`, as `api/flows.py` `_started_by` reads it, else `browser`.
- Money is integer minor units (`*_minor`, bigint) with the order's `currency`; rates are decimal strings; nothing is a float (spec §3.6, Ruling 11).
- `orders.status` is never written directly past `accepted`: `in_production`, `ready`, and whether `shipped` and `closed` are allowed are derived in the same transaction as the change, under the order's row lock (spec §3.5, Ruling 5).
- Nothing the process produced is deleted: items past `draft` cancel, quotes stay, customers archive; a `draft` item and a mis-uploaded attachment are the only deletes, each on the audit line (spec Ruling 6; attachments are phase 3).
- A line item's reference is `{slug, commit (40-hex), inputs, libraries}` plus `colours` and a nullable `output_id`; it is complete without the output (spec §3.3, Ruling 1). Its `inputs` join `referenced_asset_ids` so the asset sweep keeps its uploads.
- Bus events carry ids only (`core/events.py` docstring). The two new kinds are `order.changed` and `order_item.changed`, named as the bus names them (spec §7.1, Ruling 13); the trigger kinds that read them are phase 4.
- Every `/api/v1` operation this phase adds has an agent tool or a `coverage.ts` entry, or `agent/test/coverage.test.ts` fails. The generated API files are never committed.
- Frontend mocks go under `src/mocks/features/orders/`, each file exporting `handlers` (and `reset`), picked up without editing `handlers.ts`.
- The Activity page keeps its behaviour and its tests through the component extraction (Task 9); a visual diff of `/activity` before and after is the check.
- Every measured or external fact in a docstring or doc cites its source (#259).

## Rulings

Where the spec leaves a choice open, or phase 1 needs something a later phase owns, decided here:

1. **The full `order_item_quotes` table lands in phase 1, with `manual` quotes only.** Spec §3.5 lets no order be accepted while an item has no accepted quote, so without quotes phase 1 could never accept an order. The table carries every §3.4 column (`sliced_sha256` and `sliced_blob_key` nullable, `plates`, `lines`, the four money columns, `source`, `accepted`), so phase 2 adds the slice path and no migration. A `manual` quote is `POST …/quotes` with `{"unit_price_minor": N, "label": "…"}`: one line of kind `manual`, `unit_cost_minor = unit_price_minor`, both file fields null, no blob ref (spec §3.4's last amendment). Its `quantity` is the item's at the time; a quantity change drops the accepted quote as a reference change does (§4's `PATCH` row).
2. **`order_shipments` and `order_shipment_items` land in phase 1.** Spec §3.5 reaches `shipped` only through a shipment covering every fulfilled item, and §11 says "close works" in phase 1, so the two §6.3 tables and the routes `POST /orders/{id}/shipments`, `PATCH …/{shipment}` are built here, exactly as §6.3 says them. Phase 3 (#4251) keeps the packed-photo attachment and `/costs`. The plan's PR says so on #4251.
3. **`order_item_fulfilments` lands in phase 1 with the hand-written columns only:** `id`, `order_item_id`, `attempt`, `quantity`, `outcome`, `failure_reason`, `started_at`, `finished_at`, `notes`, `accepted_at`, `created_by`, `created_at`. Phase 3 adds the print path's columns (`print_run_id`, `sliced_sha256`, `sliced_blob_key`, `printer`, `actual_seconds`, `actual_grams`, `archive_id`, `timelapse_blob_key`) in its own migration. "Manual fulfilment" is `POST …/fulfilments` by a person, then `POST …/fulfilments/{f}/accept`.
4. **Phase 1's item states are `draft`, `quoted`, `approved`, `fulfilled`, `cancelled`, plus `failed` from a hand-recorded failed attempt.** `queued`, `printing` and `printed` arrive with the print path (phase 3) and are in the `CHECK` constraint now, so phase 3 adds no migration for them. A `failed` item leaves that state the way spec §6.1 says: recording its next fulfilment attempt returns it to `approved` (a cancel still takes it to `cancelled`). `in_production` is derived in phase 1 as "any item has an attempt and not every item is settled", which is spec §3.5's "any item past `approved`" restricted to the states phase 1 has (an attempt is the only way past `approved` here); once phase 3 adds `queued`, the derivation must read "any item `queued` or beyond, or with an attempt" so it still agrees with §3.5, and Task 12's spec edit says so.
5. **Order numbers are `ORD-<yyyy>-<n>` from a per-year counter row** (`order_numbers(year int primary key, last int)`), taken under `SELECT … FOR UPDATE` in the order's insert transaction, so numbers never skip on a rolled-back insert and never repeat across replicas. A Postgres sequence would skip. This serialises every order create of a year on that one row for the length of the insert transaction, which is deliberate: a shop's orders arrive a few a day, gapless numbers are the point, and the lock is held only across the insert, so it is never to be "optimised" into a sequence.
6. **The reference is validated at write time against the catalogue:** the slug must exist (or be a tombstone, for a re-pointed item of a deleted template, which is refused with 409 `template_deleted`), the commit must be a commit of that model's history (`library/history.py` `ModelHistory`; `COMMIT_ID_PATTERN` in `core/gitrepo.py:100` accepts 7 to 40 hex characters, so an abbreviated commit is resolved to its full 40-character id with `GitRepo.resolve` (`core/gitrepo.py:810`, `rev-parse --verify`) before anything is stored, and an unknown or ambiguous prefix is 422 on `commit`; `resolve` answers against the whole models repository, which holds every model, so the resolved id is then checked with `ModelHistory.touched(resolved, model_path(slug))` (`library/history.py:226`), and a commit that never changed that model is 422 on `commit` too. That one path is enough: a model's slug is its directory in the models repository and never moves (a rename changes its name, not its slug), so there is no earlier path to cover. `library/catalogue.py` `_merge_base_of` checks two `places` because an upstream may live elsewhere (`Upstream.path`, a built-in's directory); an item references a catalogue slug, never an upstream, and a template copied from one has its own history under its own slug from its seed commit on (`ModelHistory.seed_commit`), so commits of the upstream before the copy are not revisions of the item's template; `order_items.commit` only ever holds the full id its CHECK requires, never a raw CHECK violation), and `inputs` must pass `render/inputs.py` `normalize_inputs` against that commit's schema. An invalid reference is 422 naming the field. `libraries` is copied from the output's `meta.json` when the item is made from an output, else resolved as a render at that commit would resolve them (`render/submit.py`'s library pins), so a later render reproduces the item.
7. **`colours` is filled from the render, never typed:** from the output's `meta.json` `colors` when the item is made from an output, else from the model's default-render preview record when one exists (`render/previews.py`), else left empty for the quote (phase 2) to fill. A `filament_id` is null in phase 1 (the price book is phase 2).
8. **`orders_currency` is a stored setting** (`core/settings.py`, `library/settings_store.py`, `api/settings.py`, the Settings page), ISO 4217, default `USD`, `applies: next order` (not a restart): an order copies it at create and never changes it. The validation is the three-letter uppercase pattern; the full code list is not shipped.
9. **The customer picker and the pages read whole lists; orders page is cursor-paged** as the Activity page is (`PAGE = 50`, `next_cursor`), since customers are few and orders grow. Both lists answer 400 for an unknown query parameter through `api/activity.py`'s `_unknown` (problem type `UNKNOWN_PARAMETER`), reused rather than copied.
10. **`DELETE /orders/{id}/items/{item}` on a non-draft item answers 409 `item_not_draft`** with the cancel route named; it never cancels for the caller (spec §4's row says "else it is a cancel": the route tells, the caller decides).
11. **Address snapshots are the customer's address rows copied as JSON at `accept`**, not at create: a draft order follows the customer's edits, an accepted one does not. `PATCH /orders/{id}` may set `shipping_address_id` / `billing_address_id` while `draft` or `quoted`; at `accept` the rows are copied into `shipping_address` / `billing_address` and the ids kept beside them. For a draft order to follow the customer's edits, a customer `PATCH` must keep address rows by id: `replace_addresses` (and `replace_contacts`) updates a row whose `id` the body carries in place, inserts a row without one, and deletes only the rows the body leaves out, so an address an open order points at keeps its id (`ON DELETE SET NULL` fires only for an address the body dropped, which then clears the order's pointer, as a deleted address should). Task 6 `test_editing_a_customer_keeps_a_draft_orders_address`.
12. **The audit line is written in the writer's transaction** (`order_events`), one row per status change, quote acceptance, charge or payment change, shipment, and delete, with `detail` holding only ids and the before/after status, never a body.
13. **The generic components are `components/records/`:** `DataTable` (a typed `<table>` in a `Card` with the `HEAD`/`CELL` classes of `ActivityTable`), `FilterBar` (chips, selects, search, the `shownChips` helper of `ActivityFilters`), `RecordDrawer` (the `Sheet` shell of `ActivityDrawer`). The Activity components become thin wrappers over them; their props and test ids do not change.
14. **The agent's tiers are the spec's §7.3:** `read` for lists and gets; `write` for `customer_upsert`, `order_create`, `order_update`, `order_item_add`, `order_item_update`, `order_item_quote` (manual in this phase); `outward` for `order_accept`, `order_item_accept_quote`, `order_item_fulfil`, `order_ship`, `order_close`. `order_notify_customer` is phase 4.

## Spec corrections

- §11 phase 1 says "Close works with manual fulfilment (§6.1 by hand)" but puts shipments in phase 3, while §3.5 needs a shipment to reach `shipped` and then `closed`. Ruling 2 moves §6.3's tables and routes into phase 1; §11 should say so. Task 12 edits the spec's phase 1 and phase 3 lines.
- §3.5's item lifecycle names `queued`, `printing`, `printed` without saying they wait for the print path. Ruling 4 says which states phase 1 reaches; no spec edit.
- §4's row for `POST /orders/{id}/items/{item}/quotes` says only "with a `manual` body". Ruling 1 fixes the body as `{unit_price_minor, label?}`; Task 12 adds that to §4.

## Review Focus

1. **Two replicas creating orders in the same year** must get distinct, gapless numbers (Task 2 `test_numbers_are_distinct_under_concurrent_creates`, two threads on two connections).
2. **`close` while an item is still open** is refused with every offending item named, and nothing changes (Task 4 `test_close_names_every_open_item`).
3. **A customer edit after `accept`** does not move the order's shipping address (Task 4 `test_accepted_order_keeps_its_address_snapshot`).
4. **A reference change drops the accepted quote** and the order falls back from `quoted` to `draft` when it was not yet accepted; after `accept` the reference is immutable. A quantity change drops it the same way (Ruling 1). (Task 4 `test_reference_change_drops_the_quote`, `test_quantity_change_drops_the_quote`, `test_reference_is_immutable_after_accept`).
5. **The asset sweep keeps an ordered upload** whose only reference is an item's `inputs` (Task 3 `test_sweep_keeps_an_ordered_asset`).
6. **A cancelled order cancels its open items and keeps its fulfilled ones' attempts** (Task 4 `test_cancel_keeps_fulfilled_attempts`).
7. **A bad commit for the slug** (not in that model's history) is 422, and a deleted template's slug is 409 (Task 3 `test_a_commit_of_another_model_is_refused`, `test_a_deleted_template_is_refused`).
8. **The Activity page is unchanged** after Task 9: its tests, its e2e spec and its screenshots.

---

### Task 1: `orders_currency` setting

**Files:**
- Modify: `backend/scadbuddy/core/settings.py` (field after `library_backend` :151; `APPLIES` dict at :623)
- Modify: `backend/scadbuddy/library/settings_store.py` (`StoredSettings` :306, `SettingsPatch` :560, the descriptions)
- Modify: `backend/scadbuddy/api/settings.py` (view :114 and :357)
- Modify: `frontend/src/pages/settings/fields.ts`, `frontend/src/pages/SettingsPage.tsx` (a text field in the General section; `:112` and `:200` show how a string setting is read and kept raw)
- Modify: `frontend/src/mocks/features/settings.ts` (the default value)
- Test: `backend/tests/test_settings_store.py`, `backend/tests/api/test_settings.py`, `frontend/src/pages/SettingsPage.sections.test.tsx`

**Interfaces:**
- Produces: `Settings.orders_currency: str = "USD"` (`pattern=r"^[A-Z]{3}$"`), `StoredSettings.orders_currency`, `SettingsPatch.orders_currency: str | None`, the API's `orders_currency` field; `APPLIES["orders_currency"] = "next order"`.

- [ ] **Step 1: Write the failing tests**: `test_orders_currency_defaults_to_usd_and_is_three_letters` (a `PATCH` of `"usd"` is 422, `"EUR"` is saved) in `tests/api/test_settings.py`; a `SettingsPage.sections.test.tsx` case that the General section shows "Orders currency" with `USD`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** the field through the three backend files and the Settings page field.
- [ ] **Step 4: Run** `uv run --frozen pytest tests/test_settings_store.py tests/api/test_settings.py`, `uv run --frozen mypy`, `pnpm test -- SettingsPage` → PASS.
- [ ] **Step 5: Commit** `feat(orders): orders_currency setting (#4249)`

---

### Task 2: The tables, the row store and the order numbers

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_orders.sql`
- Create: `backend/scadbuddy/orders/__init__.py`, `backend/scadbuddy/orders/models.py`, `backend/scadbuddy/orders/store.py`, `backend/scadbuddy/orders/numbers.py`
- Modify: `backend/scadbuddy/core/events.py` (`OrderChanged`, `OrderItemChanged` after `ActivityEvent` :209; the `Event` union :329)
- Test: `backend/tests/orders/test_store.py`, `backend/tests/orders/test_numbers.py` (`requires_postgres`, the `pg_conninfo` fixture), `backend/tests/test_events.py` (the union round-trips)

**Interfaces:**
- Produces:
  - Schema, spec §3.1-3.4, §6.1 (Ruling 3), §6.3, Ruling 5: `customers`, `customer_contacts` (`kind` CHECK in `phone|email|instagram|facebook|tiktok|etsy|website|other`; a partial unique index on `(customer_id, kind) WHERE "primary"`), `customer_addresses`, `orders` (`status` CHECK over §3.5's list, `currency char(3)`, `on_hold boolean`, `shipping_address_id`/`billing_address_id` nullable references with `ON DELETE SET NULL`, the two jsonb snapshots, `number text UNIQUE`), `order_numbers(year int primary key, last int not null)`, `order_charges`, `order_payments`, `order_events`, `order_items` (`status` CHECK over the full §3.5 list, `commit` CHECK `~ '^[0-9a-f]{40}$'`, `quantity >= 1`), `order_item_quotes` (partial unique index `(order_item_id) WHERE accepted`), `order_item_fulfilments` (unique `(order_item_id, attempt)`), `order_shipments`, `order_shipment_items`. Indexes: `orders (customer_id)`, `orders (status, created_at desc)`, `order_items (order_id, position)`, `order_events (order_id, id)`.
  - `models.py`: pydantic models `Customer`, `Contact`, `Address`, `Order`, `OrderItem`, `Reference` (`slug`, `commit`, `inputs: dict`, `libraries: list[LibraryPin]`, `template_name`, `render_key`), `ColourSlot`, `Quote`, `QuoteLine`, `Charge`, `Payment`, `Fulfilment`, `Shipment`, `ShipmentItem`, `OrderEvent`; `OrderStatus`/`ItemStatus` literals; `Money = int` with a `minor` suffix convention documented once.
  - `store.py`: `class OrderStore` (sync, autocommit pool, `dict_row`), with `transaction()` (a context manager yielding a connection in a transaction), `lock_order(conn, order_id) -> Order` (`SELECT … FOR UPDATE`), and plain CRUD per table: `insert_customer/update_customer/archive_customer/customer/customers(q, include_archived)`, `replace_contacts`, `replace_addresses` (each keeps rows by id: update where the body names an `id`, insert where it does not, delete the rest, Ruling 11), `insert_order(conn, …)`, `order(id)`, `orders(filter, cursor, limit) -> (rows, next_cursor)` (cursor as `activity/index.py` encodes one), `update_order_fields`, `set_order_status(conn, id, status, at)`, `insert_item/update_item/delete_draft_item/items(order_id)`, `insert_quote/accept_quote(conn, item_id, quote_id)` (clears the other accepted one in the same statement), `insert_fulfilment/update_fulfilment/accept_fulfilment`, `insert_shipment/update_shipment`, `charges/replace_charges`, `payments/insert_payment`, `insert_event(conn, order_id, by, kind, detail)`, `events(order_id)`, `item_inputs() -> Iterator[dict]` (for the asset scan).
  - `events.py`: `class OrderChanged(BaseEvent): kind: Literal["order.changed"]; order_id: str; status: OrderStatus`; `class OrderItemChanged(BaseEvent): kind: Literal["order_item.changed"]; order_id: str; item_id: str; status: ItemStatus; attempt: int | None` (the bus carries ids and the new status only, spec §7.1; Task 4 publishes them, Task 5 routes them).
  - `numbers.py`: `next_number(conn, year: int) -> str` → `ORD-<yyyy>-<n>`, under `SELECT last FROM order_numbers WHERE year = %s FOR UPDATE` (inserting the year's row with `ON CONFLICT DO NOTHING` first).

- [ ] **Step 1: Write the failing tests**

```python
# tests/orders/test_numbers.py
def test_numbers_count_per_year(pool) -> None:
    with pool.connection() as c, c.transaction():
        assert next_number(c, 2026) == "ORD-2026-1"
        assert next_number(c, 2026) == "ORD-2026-2"
        assert next_number(c, 2027) == "ORD-2027-1"

def test_numbers_are_distinct_under_concurrent_creates(pool) -> None:
    # two threads, two connections, 50 numbers each: 100 distinct, none skipped
    ...

def test_a_rolled_back_insert_does_not_skip(pool) -> None:
    ...

# tests/orders/test_store.py
def test_a_customer_has_at_most_one_primary_per_kind(rows) -> None: ...
def test_an_item_keeps_at_most_one_accepted_quote(rows) -> None: ...
def test_order_list_pages_newest_first_by_cursor(rows) -> None: ...
def test_fulfilment_attempts_are_numbered_per_item(rows) -> None: ...

# tests/test_events.py (beside the existing union round-trips)
def test_order_events_round_trip() -> None: ...
```

- [ ] **Step 2: Run to verify they fail**: `uv run --frozen pytest tests/orders -v` → FAIL (no module).
- [ ] **Step 3: Implement** the migration, `models.py`, `store.py`, `numbers.py`. Model the pool on `print_library/component.py`'s (`ROW_POOL_SIZE`, `CONNECT_TIMEOUT`, `TCP_KEEPALIVE`).
- [ ] **Step 4: Run to verify they pass.** Run `uv run --frozen mypy` and `uv run --frozen ruff check .`.
- [ ] **Step 5: Commit** `feat(orders): customers, orders, items, quotes, fulfilments and shipments tables with the row store (#4249)`

---

### Task 3: The reference: validation, colours, libraries and the asset scan

**Files:**
- Create: `backend/scadbuddy/orders/reference.py`
- Modify: `backend/scadbuddy/library/assets.py` (`referenced_asset_ids` :766: add the items' `inputs`; it takes the store's `item_inputs()` through a new optional `extra: Iterable[Mapping]` argument, as `params` is passed today)
- Modify: the asset sweep's caller (where `referenced_asset_ids` is called with `params`; grep `referenced_asset_ids(` under `backend/scadbuddy/`) to pass the orders store's inputs. The sweep fails closed: when the orders component is absent or its pool cannot answer (`item_inputs()` raises), the caller skips the sweep for that pass and logs why, as it does when `stopped`, rather than sweeping with an incomplete reference set and deleting an ordered upload (`test_sweep_skips_when_orders_are_unavailable`)
- Test: `backend/tests/orders/test_reference.py`, `backend/tests/test_assets.py` (the sweep test)

**Interfaces:**
- Produces: `reference.py`: `async def resolve_reference(catalogue, history, outputs, *, slug, commit, inputs, output_id) -> Reference` (Ruling 6: slug exists, commit is expanded to its full id with `resolve` and then checked with `touched` against the slug's path, inputs normalise against the commit's schema; `ReferenceError(field, problem)` → 422; a tombstoned slug → `TemplateDeletedError` → 409); `def colours_of(output_meta | preview) -> list[ColourSlot]` (Ruling 7); `def render_key_of(ref) -> str` (`render/job_models.py` `render_key`, or `inputs_key` for a pipeline template, chosen as `render/submit.py:291-296` chooses); `def reference_from_output(meta, inputs) -> Reference` (`meta.json`'s `slug`, `model_version`, `libraries`, `colors`; `inputs.json`).

- [ ] **Step 1: Write the failing tests**: `test_reference_from_an_output_copies_commit_libraries_and_colours`, `test_a_commit_of_another_model_is_refused` (422: a commit that resolves but never touched the slug's path, so the test exercises `touched`, not `resolve`), `test_a_short_commit_is_stored_full` (a 7-character prefix resolves to the 40-character id; an ambiguous one is 422), `test_a_deleted_template_is_refused` (409), `test_inputs_are_normalised_against_the_commit` (an unknown param is 422), `test_sweep_keeps_an_ordered_asset` (an asset named only by an item's inputs survives the sweep).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders tests/test_assets.py` → PASS; mypy.
- [ ] **Step 5: Commit** `feat(orders): a line item's reference is checked against the catalogue and keeps its assets (#4249)`

---

### Task 4: The lifecycle

**Files:**
- Create: `backend/scadbuddy/orders/lifecycle.py`
- Test: `backend/tests/orders/test_lifecycle.py` (`requires_postgres`)

**Interfaces:**
- Produces: `class Lifecycle(store: OrderStore, bus)` with one method per transition, each opening `store.transaction()`, taking `lock_order`, checking, writing, deriving, writing the audit row and publishing after commit: `accept_order(order_id, by)` (every item not `cancelled` is `approved`, else `items_not_quoted` naming the others; cancelled items are exempt, and an order whose every item is cancelled is refused with `no_open_items`; snapshots the addresses, Ruling 11; `accepted_at`), `cancel_order(order_id, by, reason)` (refused past `shipped`; cancels open items, keeps attempts), `hold(order_id, by)` / `release(order_id, by)` (the `on_hold` flag), `close_order(order_id, by)` (refused with 409 `order_not_closable` listing every item not `fulfilled`/`cancelled` and every fulfilled item not covered by a shipment), `accept_quote(order_id, item_id, quote_id, by)` (item → `approved`; order `draft` → `quoted` when every item not `cancelled` is approved), `record_fulfilment(order_id, item_id, body, by)` (attempt numbered per item; `failed` outcome → item `failed`; a new attempt on a `failed` item → `approved` first, Ruling 4), `accept_fulfilment(order_id, item_id, f, by)` (item → `fulfilled` when accepted quantities reach the item's), `cancel_item(order_id, item_id, by)`, `add_shipment(order_id, body, by)` / `update_shipment`; and `def derive(order, items, shipments) -> OrderStatus` (pure: `in_production` / `ready` / whether `shipped`, `closed` are allowed; Ruling 4), unit-tested without Postgres.
- Every transition's refusal is a domain exception, `class LifecycleError(Exception)` with `slug` and the offending ids (`items`, `shipments`), raised by `orders/lifecycle.py` without any HTTP shape, as `flows/store.py` raises `GrantStateError`. Task 7's `api/orders.py` maps each one to an `ApiError` (`core/problems.py`) with status 409, a `type_` of `https://scadbuddy.dev/problems/orders/<slug>` and the ids in `detail`, as `api/flows.py` maps `GrantStateError` in `_grant_state`; `orders/` never imports from `api/`.

- [ ] **Step 1: Write the failing tests**: the Review Focus cases 2, 3, 4, 6; plus `test_accept_needs_every_item_quoted`, `test_quoted_falls_back_to_draft_when_an_item_is_added`, `test_a_failed_attempt_then_a_good_one_fulfils` (the second attempt first returns the item to `approved`), `test_accept_ignores_cancelled_items`, `test_accept_refuses_an_order_of_only_cancelled_items`, `test_shipped_needs_every_fulfilled_item_covered`, `test_derive_is_pure` (table-driven over `derive`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/orders` → PASS; mypy.
- [ ] **Step 5: Commit** `feat(orders): the order and item lifecycle, derived past accepted and locked per order (#4249)`

---

### Task 5: The realtime topic and the component

**Files:**
- Modify: `backend/scadbuddy/api/realtime.py` (`COLLECTION_TOPICS` gains `"orders"`; `_TOPIC` gains `order:[0-9a-f-]{36}`; `topics_of` arms)
- Create: `backend/scadbuddy/orders/component.py` (`ORDERS`, `OrdersComponent` with `store`, `lifecycle`, built lazily as `PrintLibrary` is, since the pool opens after `build_all`)
- Test: `backend/tests/api/test_realtime_orders.py` (modelled on `test_realtime_activity.py`)

**Interfaces:**
- Consumes: `OrderChanged`, `OrderItemChanged` (Task 2).
- Produces: `topics_of` → `["orders", f"order:{order_id}"]` for both; `ORDERS: Key[OrdersComponent]`, `OrdersDep = Annotated[OrdersComponent, component_dep(ORDERS)]`.

- [ ] **Step 1: Write the failing tests**: `test_an_order_change_is_news_for_orders_and_its_order` (a socket following `orders` hears a `PATCH`).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** `assert_never` in `topics_of` forces the arms.
- [ ] **Step 4: Run** the two test modules; mypy.
- [ ] **Step 5: Commit** `feat(orders): the orders realtime topic and the ORDERS component (#4249)`

---

### Task 6: Routes: customers

**Files:**
- Create: `backend/scadbuddy/api/customers.py`
- Modify: `backend/scadbuddy/main.py` (include the router as the others are)
- Test: `backend/tests/api/test_customers.py`

**Interfaces:**
- Produces (spec §4): `GET /customers` (`q`, `include_archived`), `POST /customers`, `GET /customers/{id}`, `PATCH /customers/{id}` (contacts and addresses as nested lists; a row with an `id` is updated in place, one without is inserted, the rest are deleted, Ruling 11; an `id` of another customer's row is 422), `POST /customers/{id}/archive`. Bodies `CustomerCreate`/`CustomerPatch` with `extra="forbid"`; a contact's `kind` is the enum; at most one `primary` per kind (422). `response_model` is `Customer`. Unknown query parameters answer 400 (`api/activity.py` `_unknown` and its `UNKNOWN_PARAMETER` problem type, reused).

- [ ] **Step 1: Write the failing tests**: create with two phones and an address; list by `q`; archive hides from the default list; a second primary email is 422; an unknown query parameter is 400; a body with `created_by` is 422; `test_editing_a_customer_keeps_a_draft_orders_address` (a draft order's `shipping_address_id` survives a `PATCH` that carries that address with its `id`, and is cleared by one that leaves it out; the order is inserted through `OrderStore` directly, Task 2, since the order routes arrive in Task 7).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/api/test_customers.py` → PASS; mypy.
- [ ] **Step 5: Commit** `feat(orders): customers routes (#4249)`

---

### Task 7: Routes: orders, items, quotes, fulfilments, shipments, events

**Files:**
- Create: `backend/scadbuddy/api/orders.py`
- Modify: `backend/scadbuddy/main.py`
- Test: `backend/tests/api/test_orders.py`, `backend/tests/api/test_orders_lifecycle.py`

**Interfaces:**
- Produces (spec §4, Rulings 1-3, 10): `GET /orders` (`status`, `customer_id`, `q`, `from`, `to`, `cursor`, `limit` ≤ 100; `OrderPage{items, next_cursor}`), `POST /orders` (`customer_id`, `notes`, `due_at`; copies `orders_currency`; takes a number), `GET /orders/{id}` (`OrderDetail`: the order, its items with their quotes and attempts, charges, payments, shipments), `PATCH /orders/{id}` (notes, due date, address ids, charges replaced whole; while `draft` or `quoted`), `POST /orders/{id}/items` (`{slug, commit, inputs, output_id?, quantity, title?, notes?}`, Task 3's resolution), `PATCH /orders/{id}/items/{item}` (reference replaced whole while `draft`/`quoted`, drops the accepted quote; quantity; title; notes), `DELETE …/items/{item}` (draft only, Ruling 10), `POST …/items/{item}/quotes` (manual, Ruling 1), `POST …/quotes/{quote}/accept`, `POST /orders/{id}/accept`, `/cancel`, `/hold`, `/release`, `/close`, `POST …/items/{item}/cancel`, `POST …/items/{item}/fulfilments`, `PATCH …/fulfilments/{f}`, `POST …/fulfilments/{f}/accept`, `POST /orders/{id}/shipments`, `PATCH …/shipments/{s}`, `POST /orders/{id}/payments`, `GET /orders/{id}/events`. Every write publishes through the lifecycle or, for plain field edits, through the route (`emit(bus, OrderChanged(...))`). `created_by` and `by` from `current_author()`.

- [ ] **Step 1: Write the failing tests**: in `test_orders.py` the CRUD, paging, a 400 for an unknown parameter, 422 for a body with `status`; in `test_orders_lifecycle.py` the whole happy path through the API (customer → order → item from an output made by `tests/api/test_outputs.py`'s helpers → manual quote → accept quote → accept order → fulfilment → accept → shipment → close) and the Review Focus refusals as HTTP (409s with the problem types and `detail` ids).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run --frozen pytest tests/api/test_orders.py tests/api/test_orders_lifecycle.py tests/api/test_customers.py` (the `tests/api/` paths together, CLAUDE.md) → PASS; `ruff`, `mypy`; `cd frontend && pnpm gen:api` to see the spec export still works.
- [ ] **Step 5: Commit** `feat(orders): orders, items, manual quotes, fulfilments, shipments and the audit line routes (#4249)`

---

### Task 8: Agent tools and coverage

**Files:**
- Create: `agent/src/tools/orders.ts`
- Modify: `agent/src/tools/index.ts` (`...orderTools` in `ALL_TOOLS`), `agent/src/tools/coverage.ts` (`NOT_A_TOOL`: `GET /api/v1/orders/{id}/events` "the order page's audit line; order_get carries the status and the agent's own calls are in Activity")
- Test: `agent/test/orders.test.ts` (over the fake backend as `test/activity.test.ts` does), `agent/test/coverage.test.ts` (unchanged, must pass)

**Interfaces:**
- Produces, each a `defineTool` with its `routes` (Ruling 14): `customer_list`, `customer_get`, `customer_upsert` (`POST`/`PATCH`/archive), `order_list`, `order_get`, `order_create`, `order_update` (`PATCH`, hold, release, cancel, payments), `order_item_add`, `order_item_update` (`PATCH`, `DELETE`, item cancel), `order_item_quote` (manual; `{unit_price_minor, label?}`; phase 2 widens it), `order_item_accept_quote` (outward), `order_accept` (outward), `order_item_fulfil` (outward: `POST` fulfilment plus accept), `order_ship` (outward), `order_close` (outward). Each `summarize`/`title` names the order number.

- [ ] **Step 1: Write the failing tests**: one per tool against recorded answers; `coverage.test.ts` lists no uncovered `/orders` or `/customers` operation.
- [ ] **Step 2: Run to verify they fail**: `pnpm test -- orders coverage`.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pnpm lint && pnpm typecheck && pnpm test` in `agent/` → PASS.
- [ ] **Step 5: Commit** `feat(agent): customer and order tools (#4249)`

---

### Task 9: Generic table, filter bar and drawer

**Files:**
- Create: `frontend/src/components/records/DataTable.tsx`, `FilterBar.tsx`, `RecordDrawer.tsx`, with tests beside them
- Modify: `frontend/src/components/activity/ActivityTable.tsx`, `ActivityFilters.tsx`, `ActivityDrawer.tsx` (become wrappers; props and `data-testid`s unchanged)
- Test: the existing `components/activity/*.test.tsx`, `pages/ActivityPage.test.tsx`, `e2e/activity.spec.ts` (unchanged)

**Interfaces:**
- Produces: `DataTable<Row>({ columns: Column<Row>[], rows, rowKey, rowTestId?, onOpen?, empty })` where `Column<Row> = { key, header, cell: (row) => ReactNode, className? }`; `FilterBar({ chips: Chip[], selects: SelectSpec[], search?: {value, onChange, placeholder}, shown })`; `RecordDrawer({ open, onClose, title, children, actions? })` over `@/components/ui/sheet`.

- [ ] **Step 1: Write the failing tests** for the three components (render, open, chip selection), then run the Activity suites to record the baseline: `pnpm test -- activity` and `pnpm exec playwright test e2e/activity.spec.ts`.
- [ ] **Step 2: Implement** the three, then rewrite the Activity components over them.
- [ ] **Step 3: Run** the new tests and the baseline suites → PASS, same assertions.
- [ ] **Step 4: Commit** `refactor(frontend): generic DataTable, FilterBar and RecordDrawer under the Activity components (#4249)`

---

### Task 10: Mocks, client and types

**Files:**
- Create: `frontend/src/mocks/features/orders/customers.ts`, `orders.ts` (an in-memory store shared through `orders/state.ts`; `reset` restores it; unknown query parameters 400; the lifecycle's refusals as the backend answers them, so the pages' error paths are testable)
- Modify: `frontend/src/api/client.ts` (`api.customers`, `api.customer`, `api.createCustomer`, …, `api.orders(query, cursor)`, `api.order`, `api.createOrder`, `api.addOrderItem`, …, each a plain `request`; the list with `CURSOR_HEADER` as `api.activity` does), `frontend/src/api/types.ts` (aliases: `Customer`, `Order`, `OrderDetail`, `OrderItem`, `Quote`, `Fulfilment`, `Shipment`, `OrderEvent`, `OrderPage`)
- Create: `frontend/src/lib/ordersQuery.ts` (`?status=&customer=&q=&from=&to=`, as `activityQuery.ts`)
- Test: `frontend/src/mocks/features/orders/orders.test.ts` (the mock's lifecycle refusals match the backend's problem types), `frontend/src/lib/ordersQuery.test.ts`

- [ ] **Step 1: Write the failing tests.**
- [ ] **Step 2: Implement.** `pnpm gen:api` first, so the aliases resolve against the Task 7 schema.
- [ ] **Step 3: Run** `pnpm typecheck && pnpm test -- orders` → PASS.
- [ ] **Step 4: Commit** `feat(frontend): orders and customers API client, types, query and msw mocks (#4249)`

---

### Task 11: The pages, the nav, "Add to order…", "Order this"

**Files:**
- Create: `frontend/src/pages/OrdersPage.tsx`, `OrderPage.tsx`, `CustomersPage.tsx`, `CustomerPage.tsx`, with tests; `frontend/src/components/orders/` (`OrderItemsTable.tsx`, `QuoteEditor.tsx` (the manual price), `FulfilmentForm.tsx`, `ShipmentForm.tsx`, `ChargesEditor.tsx`, `OrderStatusBadge.tsx`, `CustomerForm.tsx` (contacts and addresses lists), `AddToOrderDialog.tsx` (pick or create a customer, pick an open order or create one, quantity))
- Modify: `frontend/src/App.tsx` (lazy routes `orders`, `orders/:id`, `customers`, `customers/:id`), `frontend/src/components/AppShell.tsx` (`NAV` gains `{ to: '/orders', label: 'Orders', icon: ReceiptIcon }` after Library), `frontend/src/pages/CustomizePage.tsx` (an "Add to order…" item in `ActionBar`'s menu, enabled once an output exists for the current inputs, opening `AddToOrderDialog` with the output's id), `frontend/src/pages/HistoryPage.tsx` (an "Order this" item on each output), `frontend/src/agent/catalog.ts` (no new browser tool: the pages are plain; the agent uses Task 8's tools)
- Create: `frontend/e2e/orders.spec.ts` (the happy path over the mocks: create a customer, an order, add an item from Customize, a manual quote, accept, fulfil, ship, close; a phone-width pass as `activity.spec.ts` has), and `frontend/e2e/page-titles.spec.ts` gains the four titles
- Test: each page's `*.test.tsx` (loading, the list's filters in the URL, the detail's refusal banners from the mock's 409s)

- [ ] **Step 1: Write the failing page tests and the e2e spec.**
- [ ] **Step 2: Implement** over Task 9's components and Task 10's client. Money is shown with `Intl.NumberFormat(undefined, { style: 'currency', currency })` from minor units (divide by `10 ** fractionDigits` of the currency, read from `Intl.NumberFormat(...).resolvedOptions().maximumFractionDigits`).
- [ ] **Step 3: Run** `pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/orders.spec.ts e2e/activity.spec.ts e2e/page-titles.spec.ts` → PASS. Check `frontend-e2e-split` still adds up (`--list` counts).
- [ ] **Step 4: Commit** `feat(frontend): orders and customers pages, Add to order from Customize and from an output (#4249)`

---

### Task 12: Docs

**Files:**
- Modify: `CLAUDE.md` ("Layout": a `backend/scadbuddy/orders/` entry naming the component, the store, the lifecycle's lock-and-derive rule, the two events, the pages and the mocks)
- Modify: `docs/superpowers/specs/2026-10-18-orders-design.md` (§4: the manual quote body; §11 phases 1 and 3: shipments moved, per Ruling 2; §3.5: a note that phase 1 derives `in_production` from attempts, and that phase 3's `queued` writer must widen `derive` to "any item `queued` or beyond, or with an attempt" so the derivation keeps meaning "any item past `approved`", Ruling 4)
- Modify: `README.md` "Features" (one line: orders)

- [ ] **Step 1: Write** the three edits.
- [ ] **Step 2: Commit** `docs(orders): CLAUDE.md layout, spec §4 and §11 for phase 1 (#4249)`

---

## Self-review

- Spec coverage. §3.1-3.3: Tasks 2, 3, 6, 7. §3.4 (manual only, Ruling 1): Tasks 2, 7. §3.5 and Ruling 5: Task 4. §3.6: Task 2's column types and Task 11's formatting. §4 (phase 1's rows): Tasks 6, 7; the quote operation, print, attachments, costs and the price book are phases 2 and 3. §6.1 by hand and §6.3: Rulings 2, 3, Tasks 2, 4, 7. §7.1's events: Task 2, their topic Task 5 (the trigger kinds are phase 4). §7.3: Task 8 with Ruling 14. §9's last paragraphs (first-party pages, the extracted components, "Add to order…", "Order this"): Tasks 9, 11. Ruling 1 (commit, never HEAD): Task 3. Ruling 6: Task 4, Ruling 10. Ruling 12: Tasks 6, 7.
- Dependencies between tasks: Task 1 before 7 (the currency at create). Task 2 before 3, 4, 5. Task 5 before 6, 7 (the component and the events). Task 7 before 8 and 10 (the schema). Task 9 before 11. Task 12 last.
- Types used across tasks: `Reference`, `ColourSlot` (Task 2) in Tasks 3, 7, 10; `Lifecycle.derive` (Task 4) in Task 7; `OrderChanged`/`OrderItemChanged` (Task 2) in Tasks 4, 5, 7; `LifecycleError` (Task 4) in Task 7; `OrderPage` and the problem types (Task 7) in Tasks 10, 11.
- What phase 2 and 3 will touch again, on purpose: `order_item_quote` widens from manual to the slice operation (Tasks 7, 8); `order_item_fulfilments` gains the print-path columns in a new migration (Ruling 3); `colours[].filament_id` is filled by the price book (Ruling 7); item states `queued`/`printing`/`printed` get their writers (Ruling 4).
