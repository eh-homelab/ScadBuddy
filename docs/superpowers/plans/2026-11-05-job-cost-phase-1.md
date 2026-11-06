# Job cost phase 1 (the money view) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every order and line item shows its real cost (prints, labour, other costs), what was charged and the margin, on the order page, with time and cost entries a person or the agent records.

**Architecture:** Two new tables (`order_time_entries`, `order_cost_entries`) written only through `Lifecycle` (row lock, audit row, publish after commit). `orders/costs.py` grows from a filament comparison into the whole money read, still computed on read and never stored. Three new stored settings feed it. The order page's Cost card becomes the money view and shows from the first cost or charge.

**Tech Stack:** Python 3.12, FastAPI, psycopg, Postgres; React 19 + msw; the agent's TypeScript tool registry.

**Spec:** `docs/superpowers/specs/2026-11-05-job-cost-design.md`. Phase 1 is §4, §5, §6.1 and the parts of §7 that §6.1 needs (issue #5326). The list column, quote warning, report and automatic writers are #5327 to #5330.

## Global Constraints

- Money is integer minor units, rounded once per figure, half up, as `pricing.py` does.
- A figure computed from today's price book or settings is marked `estimated`; a part with no source is `unknown`.
- A rate setting needs the order's currency to be the shop's `orders_currency`; otherwise that part is `unknown`.
- Settings are stored in Postgres and env-seeded (`SCADBUDDY_<FIELD>`), `live`.
- Migrations are a new timestamped file; nothing else is edited.
- #5280 merged during the build: the read takes every attempt of every purpose (`OrderStore.order_fulfilments`, one query per order), so test prints count; no column is added to `order_item_fulfilments`.
- Time and cost entries are added and deleted in every order status, on hold included.
- Every new API operation has an agent tool or a `coverage.ts` entry.

## Review Focus

1. A hand-recorded attempt (no `sliced_sha256`, no copies, quantity 0 when failed) of an item with an accepted slice quote: grams and seconds come from the accepted quote for `max(copies or quantity, 1)` units, marked estimated (test in Task 2).
2. An order in a currency that is not `orders_currency`: today's rates are `unknown`, recorded quote lines and entries still count (test in Task 2; a time entry written there has a null rate, Task 1).
3. A cancelled item with test or failed prints: charged 0, its cost counts, state `loss` (test in Task 2).
4. A cost entry with the same `source` written twice: one row, one audit row (test in Task 1).
5. A custom item: charged its price, cost only its entries, no default labour (test in Task 2).

---

### Task 1: Tables, settings, store and lifecycle writes

**Files:**
- Create: `backend/scadbuddy/migrations/<utc>_order_cost_entries.sql`
- Modify: `backend/scadbuddy/orders/models.py`, `orders/store.py`, `orders/lifecycle.py`, `core/settings.py`, `library/settings_store.py`, `api/settings.py`
- Test: `backend/tests/orders/test_cost_entries.py`, `backend/tests/api/test_settings.py` (DEFAULTS)

**Interfaces (produces):**
- `TimeKind = Literal["design","setup","post_processing","packing","other"]`, `CostKind = Literal["assistant","payment_fee","material","other"]`.
- `TimeEntryInput(order_item_id: str|None, kind: TimeKind, minutes: int (1..1_000_000), note: str (≤1000) = "", started_at: datetime|None)`; `TimeEntry` adds `id, order_id, rate_minor_per_hour: Money|None, created_by: Actor, created_at`.
- `CostEntryInput(order_item_id, kind: CostKind, amount_minor: Money (>0), note, source: str|None (pattern `^[a-z_]+:\S{1,200}$`))`; `CostEntry` adds `id, order_id, created_by, created_at`.
- `OrderStore.time_entries(order_id) / cost_entries(order_id)`, `insert_time_entry(conn, order_id, body, rate, by)`, `insert_cost_entry(conn, order_id, body, by) -> tuple[CostEntry, bool]` (bool: inserted), `delete_time_entry(conn, order_id, entry_id) -> TimeEntry|None`, same for cost.
- `Lifecycle.add_time_entry(order_id, body, by, *, rate_minor_per_hour: int|None) -> TimeEntry`, `remove_time_entry(order_id, entry_id, by)`, `add_cost_entry(order_id, body, by) -> CostEntry`, `remove_cost_entry(order_id, entry_id, by)`. Events `time.added`, `time.removed`, `cost.added`, `cost.removed` (ids, item id, kind, minutes/amount). Refusals: `item_not_found` (404), `time_entry_not_found` / `cost_entry_not_found` (404).
- Settings `pricing_labour_rate_per_hour = "0"`, `pricing_usd_rate = "1"` (> 0), `orders_thin_margin_percent = "15"`, decimal strings, `live`.

- [ ] Tests: an entry is added to a closed, cancelled and on-hold order; one audit row each; `order.changed` published; an item of another order is refused 404; the second write of one `source` returns the first row and writes no second audit row; delete removes and audits; a foreign id 404s.
- [ ] Migration: both tables (`minutes integer CHECK (minutes > 0)`, `amount_minor bigint CHECK (amount_minor > 0)`, `order_item_id REFERENCES order_items ON DELETE CASCADE`), index on `(order_id, created_at)`, the partial unique index `(order_id, kind, source) WHERE source IS NOT NULL`.
- [ ] Implement, run `pytest tests/orders/test_cost_entries.py`, commit.

### Task 2: The money read (`orders/costs.py`)

**Interfaces:**
- Consumes: Task 1 store reads and settings.
- Produces: `CostRates(shop_currency: str, machine_rate_per_hour: str, labour_per_item: str, labour_rate_per_hour: str, thin_margin_percent: str)`; `order_costs(store, integrations, order_id, rates: CostRates) -> OrderCosts | None`.
- `AttemptCost` gains `purpose` (the attempt's), `grams`, `seconds`, `machine_minor`, `cost_minor`, `estimated: list[Literal["grams","seconds","filament","machine"]]`, `unknown: bool`.
- `ItemCost` gains `prints_minor`, `machine_minor`, `labour_minor`, `labour_default: bool`, `other_minor`, `cost_minor`, `revenue_minor|None`, `margin_minor|None`, `margin_percent: float|None`, `margin_state: Literal["ok","thin","loss","unknown"]|None`, `estimated: list[str]`, `unknown: list[str]`, `time_entries`, `cost_entries`.
- `OrderCosts` gains the same sums plus `shipping_minor`, `charges_minor` (non-tax charges), order-level `time_entries`/`cost_entries`, `thin_margin_percent`.

Rules (spec §4, §5): an attempt's quote is the item's quote with its `sliced_sha256`, else the accepted slice quote; units are `copies`, else `quantity`, else 1. Grams: `actual_grams`, else quote grams × units (estimated). Price per gram: the quote's filament lines over its grams, else the price book `cost_per_kg` of the item's colour slots' filaments weighted by the quote's slot grams (plate slot `n` is colour slot `n - 1`), equally otherwise (estimated; `unknown` without a filament or in a foreign currency). Machine: seconds (`actual_seconds`, else quote seconds × units, estimated) × the quote's `machine_time` line over its hours, else today's rate (estimated). Labour: entries' `minutes × rate / 60` each rounded; an entry with no rate is `unknown`; with no entries, a model item gets its accepted quote's `labour` line × quantity, else today's `pricing_labour_per_item` × quantity (estimated); a custom item gets none. Charged: the accepted quote's total, 0 when cancelled, None without one. Order revenue: every non-cancelled item charged plus non-tax charges, else None. Margin: loss below 0, else `unknown` with an unknown part, else thin below the threshold, else ok.

- [ ] Tests in `backend/tests/orders/test_costs.py`: each rule above, the five Review Focus cases 1-3 and 5, and the existing tests kept.
- [ ] Implement, run, commit.

### Task 3: Routes

**Files:** `backend/scadbuddy/api/orders.py`; test `backend/tests/api/test_orders_costs.py`.

- `GET /orders/{id}/costs` passes `CostRates` from the settings snapshot.
- `POST /orders/{id}/time-entries` (201, `TimeEntry`; the rate is `to_minor(pricing_labour_rate_per_hour, currency)` when the order's currency is the shop's, else None), `DELETE /orders/{id}/time-entries/{entry_id}` (204), `POST /orders/{id}/cost-entries` (201, or 200 for a repeated `source`), `DELETE /orders/{id}/cost-entries/{entry_id}` (204).
- [ ] Tests over the client: round trip, 404s, 422 for `minutes: 0`, costs show the entries. Commit.

### Task 4: Agent tools

**Files:** `agent/src/tools/orders.ts`, `agent/src/tools/coverage.ts`, `agent/src/sessions/touched.ts`, `agent/test/orders.test.ts`.

- `order_time_add` (`write`, `POST …/time-entries`), `order_cost_add` (`write`, `POST …/cost-entries`); the two DELETEs are coverage entries (the order page's; a delete is outward). `order_costs`' description names the new fields.
- [ ] Tests: tiers, bodies sent, coverage. Commit.

### Task 5: Frontend

**Files:** `frontend/src/components/orders/OrderCosts.tsx` (+ test), `components/orders/CostEntryForms.tsx`, `pages/OrderPage.tsx`, `api/client.ts`, `api/types.ts`, `mocks/features/orders/costs.ts`, `mocks/features/settings.ts`, `mocks/fixtures.ts`, `pages/settings/fields.ts` (+ orders section test).

- The card shows from the first cost or charge; one row per item (charged, cost, margin, red loss / amber thin, `≈` for estimated with the parts in a title), each expandable (`Collapsible`) to its attempts, time and cost entries; an order total row; "Log time", "Add cost" on each item and the order; a per-item timer kept in `localStorage` (`scadbuddy.order-timer.<item id>`), Stop writes one entry rounded up to the minute. The header shows a "Loss" or "Thin margin" badge.
- [ ] Tests: rows, states, logging time, adding a cost, the timer, the badge. `pnpm lint && pnpm typecheck && pnpm test`. Commit.

### Task 6: Docs, CI, PR

- [ ] Root/backend CLAUDE.md orders bullet: one paragraph on job cost phase 1.
- [ ] Run every tree's CI commands; open the PR; shepherd.
