# Orders, quotes, pricing and job cost

## What it does

Customers with contacts and addresses; orders of line items, each pinning one template
commit and its inputs; quotes priced by hand, from a past print's archive, from a slice, or from the storefront's cached slice price (`backend/scadbuddy/orders/models.py:75`)
(each plate's grams and time at the price book's filaments and rates); a rough estimate
from a render's volume; payment requests, deposits and the balance; printing an item;
fulfilment, shipping and closing, with an audit line per order. The real cost and margin
of each order, a profit report, the parts each item uses (inventory items, see
[inventory.md](inventory.md)) and per-printer machine rates.
See README "Features" and the [orders spec](../superpowers/specs/2026-10-18-orders-design.md).

## How it works

Orders live in the API process. Every transition runs under the order's row lock in
`Lifecycle`. Slow steps (render an item, slice-quote it, print it, add an attachment) are
operation kinds on the `library` queue; printing an item starts a `PrintRun` on the print
worker and follows it from the event bus. The sequence is in
[workflows.md "Orders"](../architecture/workflows.md#orders).

| Step | Route | Core |
|---|---|---|
| Create order | `POST /api/v1/orders` (`backend/scadbuddy/api/orders.py:815`) | `Lifecycle.create_order` (`backend/scadbuddy/orders/lifecycle.py:859`) |
| Add item | `api/orders.py:1761` | `add_item` (`lifecycle.py:3616`) |
| Rough estimate | `GET /orders/estimate` (`api/orders.py:1075`) | `backend/scadbuddy/orders/estimate.py:150` |
| Slice quote | `POST /orders/{id}/items/{item}/quote` (`api/orders.py:2665`) | `quote_run` (`backend/scadbuddy/orders/quote.py:967`) |
| Accept quote, order | `api/orders.py:2821`, `:1431` | `accept_quote` (`lifecycle.py:4535`), `accept_order` (`:2826`) |
| Payment | `backend/scadbuddy/api/order_payments.py:68`, webhook `backend/scadbuddy/api/payment_webhooks.py:58` | `Payments.handle_webhook` (`backend/scadbuddy/payments/service.py:584`) |
| Print item | `POST /orders/{id}/items/{item}/print` (`api/orders.py:2882`) | `print_item` (`backend/scadbuddy/orders/printing.py:696`) |
| Fulfil, ship | `api/orders.py:3225`, `:3286` | `fulfil_items` (`lifecycle.py:5013`), `add_shipment` (`:5830`) |

Pricing (`backend/scadbuddy/orders/pricing.py:104-172`): one filament line per slot
(grams × cost per kg), machine time at the printer's rate, setup per plate, labour per
item, markup, optional rounding, × quantity. The machine rate formula is in
`backend/scadbuddy/orders/machine_rate.py:1-21`. A built-in `order-fulfilment` flow can
print, wait and notify the customer (`backend/scadbuddy/orders/builtin_flow.py:46`).

Attempt media: with Bambuddy linked, an attempt that names an archive gets the archive's
latest finish photo and its timelapse as attachments with no click. The `AttemptMedia`
workflow imports them (`backend/scadbuddy/workflows/attempt_media.py`, `orders/archives.py`
`mirror_attempt`). Four starters start it: a hand attempt, a heard print outcome, a read
of the attempt's archives and the housekeeping sweep `housekeeping_import_attempt_media`. A medium larger than the upload limit is recorded in
`order_attempt_media_refusals`, and no import tries it again until the limit is higher.
A medium an archive does not offer an hour after it completed (a printer with timelapse
off) is recorded in `order_attempt_media_not_offered`, and the sweep leaves it out (#7453).
The order page's row shows what is attached, says to attach a refused medium by hand, and
offers a copy only for what is still missing ([orders spec §8.2](../superpowers/specs/2026-10-18-orders-design.md#82-archives)).

## Parts and job cost

Since #7067 a part is an inventory item (kind `hardware` for every migrated part), not a
`parts` row (`backend/scadbuddy/migrations/20261124T0918Z_parts_to_items.sql:1-10`). An order
item's parts are rows of `order_item_parts` by `item_id`. A model's rule copies them to a new
item (`Lifecycle.add_item`). When an item becomes `fulfilled`, scadbuddy-core's `inventory.Take`
takes each part from its lots FIFO; `unfulfil_item` gives each use back at its own cost
(`inventory.GiveBack`). Both run in the `FulfilOrderItem` workflow
(`backend/scadbuddy/workflows/order_stock.py`, #6949 K1 Task 16).

Job cost has a `parts` part, `Margin.parts_minor`, which `ItemCost` inherits (`backend/scadbuddy/orders/costs.py:183`).
It is read from `stock_moves`, not from cost entries: the migration deleted the `part:` cost
entries that the moves replace (Ruling 3). A `use` of unknown cost that nothing returned
makes the figure unknown (R-P2-19). The profit report reads the same figure (R-P2-31). The
quote estimate prices parts at what a FIFO take would cost now and returns `parts_unknown`
(R-P2-29). More in [inventory.md](inventory.md).

## Code

- UI: `frontend/src/pages/OrdersPage.tsx`, `OrderPage.tsx`, `ProfitPage.tsx`,
  `CustomersPage.tsx`; `frontend/src/components/orders/`. Parts and the model part rules are in
  the Inventory area, `frontend/src/pages/inventory/` (#7067); `/orders/parts` redirects to
  `/inventory?kind=hardware`, the parts' kind (`PARTS_KIND`, `frontend/src/lib/inventoryLinks.ts`;
  `frontend/src/App.tsx:158`).
- Routes: `backend/scadbuddy/api/orders.py:187`, `order_payments.py`, `order_attachments.py`,
  `order_messages.py`, `order_purchases.py`, `order_sources.py`, `order_bank_deposits.py`,
  `customers.py`, `parts.py`, `machine_rates.py`.
- Core: `backend/scadbuddy/orders/` (`lifecycle.py:627`, `quote.py`, `pricing.py`,
  `costs.py:731`, `operations.py:1140-1214`, `integrations.py` — the only seam to Bambuddy).
- Agent tools: `agent/src/tools/orders.ts` (68 tools); customer messages through a bound
  plugin, `agent/src/orders/messaging.ts`.

## Data

- Tables: `customers`, `customer_contacts`, `customer_addresses`, `orders`, `order_items`,
  `order_item_quotes`, `order_item_revisions`, `order_item_fulfilments`,
  `order_attempt_media_refusals` (an attempt's media the import refused as too large),
  `order_attempt_media_not_offered` (an attempt's media its archives did not offer an hour
  after they completed; the sweep skips them),
  `order_shipments`,
  `order_events`, `order_payments`, `order_charges`, `entity_attachments` (an order's files, the rows with an `order_id`), `order_messages`,
  `order_time_entries`, `order_cost_entries`, `order_item_parts` and `model_part_rules` (by `item_id` since #7067; the parts are `items` and their stock `stock_lots` and `stock_moves`, see [inventory.md](inventory.md)), `filaments` (the price book),
  `printer_cost_profiles`, `payment_*`, `bank_*`.
- Blob keys: `quote-<sha256>`, `attachment-<sha256>`.
- Settings: `orders_currency`, `pricing_*`, `orders_tax_percent`, `rough_estimate_factor`,
  `orders_deposit_policy` (`backend/scadbuddy/core/settings.py:235-324`).

## Tests

- Backend: `backend/tests/orders/` (pricing, quote, lifecycle, printing, costs, deposits),
  `backend/tests/api/test_orders*.py`, `test_order_*.py`, `test_price_book*.py`,
  `backend/tests/payments/`, `backend/tests/flows/test_order_fulfilment_flow.py`.
- e2e: `frontend/e2e/orders.spec.ts`.

## Limits and known issues

- An item's spools are filled from ScadBuddy's own spools; while Bambuddy is linked, its
  tray assignments only place spools no registry printer reports
  (`backend/scadbuddy/orders/spools.py:215-221`).
- Machine-rate actuals come from logged maintenance events and part replacements
  (`backend/scadbuddy/orders/machine_rate.py:22-28`); a row prices by its actual only once
  it has enough of them.
