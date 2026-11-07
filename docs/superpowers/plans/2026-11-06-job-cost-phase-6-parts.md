# Job cost phase 6: parts inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track non-filament parts (hooks, rings, magnets): their stock and cost, the parts each
item uses, and take them out of stock as a `material` cost entry when the item is fulfilled.

**Architecture:** Four new tables (`parts`, `part_stock_moves`, `model_part_rules`,
`order_item_parts`). `orders/parts.py` holds the models, the SQL (functions over a `Conn`) and
the pure rule and average arithmetic. Part writes run in their own transaction under the part's
row lock; item-part writes and use/return run in the lifecycle under the order's lock, then the
parts' locks in id order. Routes `api/parts.py` (`/parts…`, `/orders/{id}/parts`,
`/orders/{id}/items/{item}/parts`). Agent tools in `agent/src/tools/parts.ts`. Frontend
`pages/PartsPage.tsx`, an item parts editor and a shortage line on the order page, a low-stock
banner on the orders list.

**Tech Stack:** Python 3.12, FastAPI, psycopg, Postgres 18; TypeScript (agent, React frontend).

**Spec:** issue #5543, added as §11 of `docs/superpowers/specs/2026-11-05-job-cost-design.md`.

## Global Constraints

- Money is integer minor units. A part's unit cost is a rate, kept as `numeric(20,6)` minor
  units; each cost entry amount is rounded once, half up.
- New migration file only; never edit a merged one.
- Every lifecycle write takes the order's row lock and writes an `order_events` row.
- Only the API touches SQL; agent tools call routes.
- Stock can go below zero; nothing blocks on stock.

## Review Focus

- An item fulfilled, unfulfilled and fulfilled again: stock and cost entries must net to one use.
- Item parts edited after the item was fulfilled: refused, so a return gives back what was taken.
- An order in another currency than the part's: stock moves, no cost entry (no mixed currency).
- Adding stock while on hand is zero or negative: the new unit cost is the pack's, not an average
  with a negative count.
- A rule colour in a different case from the item's colour (`#FF69B4` vs `#ff69b4`) still matches.

---

### Task 1: Schema, models, part store and arithmetic

**Files:** Create `backend/scadbuddy/migrations/<ts>_parts_inventory.sql`,
`backend/scadbuddy/orders/parts.py`; Test `backend/tests/test_orders_parts.py`.

**Interfaces (produces):**
- `PartInput`, `PartPatch`, `Part` (`id, name, supplier, link, currency, pack_size,
  pack_price_minor, unit_cost_minor: str, on_hand, low_stock, low: bool`), `StockAdd(packs=1,
  pack_price_minor|None, note)`, `StockAdjust(on_hand, note min 1)`, `StockMove`,
  `PartRuleLine(per_unit, part_id|None, when: list[PartRuleWhen(colour, part_id)])`,
  `PartRule(slug, lines)`, `ItemPart(part_id, per_unit)`.
- `average(on_hand, unit_cost, count, amount) -> Decimal`; `rule_parts(lines, colours) ->
  list[ItemPart]`; `use_amount(unit_cost, count) -> int`.
- `PartStore(store: OrderStore)`: `list`, `get`, `create`, `update`, `add_stock`, `adjust`,
  `moves`, `rule`, `set_rule`; conn-level `lock_parts`, `move`, `item_parts`, `set_item_parts`.

- [ ] Tests: `average(0, 0, 100, 599) == Decimal("5.99")`; `average(100, 5, 100, 700) == 6`;
  `average(-3, 5, 10, 100) == 10`; `rule_parts` picks the `when` part on a case-insensitive
  colour match and the else part otherwise, sums one part named twice, skips a null else.
- [ ] Store tests (requires Postgres): create sets unit cost to pack/size; add stock averages and
  writes an `add` move; adjust needs a note and writes `adjust` with the delta; `low` when
  `on_hand <= low_stock` and `low_stock > 0`.
- [ ] Implement; run; commit.

### Task 2: Lifecycle: item parts, use and return

**Files:** Modify `backend/scadbuddy/orders/lifecycle.py` (`add_item`, `accept_fulfilment`,
`unfulfil_item`, new `set_item_parts`); Test `backend/tests/test_orders_parts_lifecycle.py`.

- `add_item` fills `order_item_parts` from `model_part_rules[reference.slug]`; event
  `item.parts_set` with `from_rule: true` when any.
- `set_item_parts(order_id, item_id, parts, by)`: refused `order_<status>` when ended,
  `item_parts_used` when the item is fulfilled, `part_not_found`.
- When `accept_fulfilment` makes the item `fulfilled`: each part's `per_unit × quantity` leaves
  stock (`use` move), and a `material` cost entry `source part:<item>:<part>` at
  `use_amount(unit_cost, count)` is written when the part's currency is the order's; event
  `parts.used`. `order_item_parts.used` records the count.
- `unfulfil_item` returns `used` (a `return` move, averaged back at the entry's amount),
  deletes the entry, event `parts.returned`. A failed attempt never uses parts.
- [ ] Tests for each bullet and the review focus lines; implement; commit.

### Task 3: Shortage, quote warning, routes

**Files:** Create `backend/scadbuddy/api/parts.py`; modify `orders/history.py`,
`api/orders.py` (unit-cost `item_id`); Test `backend/tests/api/test_parts.py`.

- `GET/POST /parts`, `PATCH /parts/{id}`, `POST /parts/{id}/stock`, `POST /parts/{id}/adjust`,
  `GET /parts/{id}/moves`, `GET/PUT /parts/rules/{slug}`, `GET /orders/{id}/parts`
  (`items`, `short: [{part_id, name, needed, on_hand, short}]`), `PUT
  /orders/{id}/items/{item}/parts`.
- `unit_cost_history(..., parts_unit_minor)`: the history's `part:` entries leave each job's
  cost and the item's parts today are added per unit, so the suggestion carries the parts once.
- [ ] Route tests; implement; commit.

### Task 4: Agent tools

`part_list` (read), `part_stock_add` (write), `order_item_parts_set` (write) in
`agent/src/tools/parts.ts`, registered as the order tools are; coverage entries for the other
routes. Tests beside the order tools' tests.

### Task 5: Frontend

Orders → Parts page (list, low-stock badge, add part, add stock, adjust with note, moves,
model rules); the order page's item parts editor and "short: …" line; the orders list's
low-stock banner; msw mocks. Vitest per component.

### Task 6: Spec §11 and CLAUDE.md layout notes
