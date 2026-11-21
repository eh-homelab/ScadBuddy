# Orders, quotes, pricing and job cost

## What it does

Customers with contacts and addresses; orders of line items, each pinning one template
commit and its inputs; quotes priced by hand, from a past print's archive, or from a slice
(each plate's grams and time at the price book's filaments and rates); a rough estimate
from a render's volume; payment requests, deposits and the balance; printing an item;
fulfilment, shipping and closing, with an audit line per order. The real cost and margin
of each order, a profit report, a parts inventory and per-printer machine rates.
See README "Features" and the [orders spec](../superpowers/specs/2026-10-18-orders-design.md).

## How it works

Orders live in the API process. Every transition runs under the order's row lock in
`Lifecycle`. Slow steps (render an item, slice-quote it, print it, add an attachment) are
operation kinds on the `library` queue; printing an item starts a `PrintRun` on the print
worker and follows it from the event bus. The sequence is in
[workflows.md "Orders"](../architecture/workflows.md#orders).

| Step | Route | Core |
|---|---|---|
| Create order | `POST /api/v1/orders` (`backend/scadbuddy/api/orders.py:810`) | `Lifecycle.create_order` (`backend/scadbuddy/orders/lifecycle.py:839`) |
| Add item | `api/orders.py:1744` | `add_item` (`lifecycle.py:3541`) |
| Rough estimate | `GET /orders/estimate` (`api/orders.py:1058`) | `backend/scadbuddy/orders/estimate.py:142` |
| Slice quote | `POST /orders/{id}/items/{item}/quote` (`api/orders.py:2646`) | `quote_run` (`backend/scadbuddy/orders/quote.py:843`) |
| Accept quote, order | `api/orders.py:2802`, `:1431` | `accept_quote` (`lifecycle.py:4460`), `accept_order` (`:2826`) |
| Payment | `backend/scadbuddy/api/order_payments.py:68`, webhook `backend/scadbuddy/api/payment_webhooks.py:58` | `Payments.handle_webhook` (`backend/scadbuddy/payments/service.py:584`) |
| Print item | `POST /orders/{id}/items/{item}/print` (`api/orders.py:2863`) | `print_item` (`backend/scadbuddy/orders/printing.py:696`) |
| Fulfil, ship | `api/orders.py:3236`, `:3286` | `fulfil_items` (`lifecycle.py:4935`), `add_shipment` (`:5830`) |

Pricing (`backend/scadbuddy/orders/pricing.py:103-171`): one filament line per slot
(grams × cost per kg), machine time at the printer's rate, setup per plate, labour per
item, markup, optional rounding, × quantity. The machine rate formula is in
`backend/scadbuddy/orders/machine_rate.py:1-21`. A built-in `order-fulfilment` flow can
print, wait and notify the customer (`backend/scadbuddy/orders/builtin_flow.py:46`).

## Code

- UI: `frontend/src/pages/OrdersPage.tsx`, `OrderPage.tsx`, `ProfitPage.tsx`,
  `PartsPage.tsx`, `CustomersPage.tsx`; `frontend/src/components/orders/`.
- Routes: `backend/scadbuddy/api/orders.py:187`, `order_payments.py`, `order_attachments.py`,
  `order_messages.py`, `order_purchases.py`, `order_sources.py`, `order_bank_deposits.py`,
  `customers.py`, `parts.py`, `machine_rates.py`.
- Core: `backend/scadbuddy/orders/` (`lifecycle.py:607`, `quote.py`, `pricing.py`,
  `costs.py:662`, `operations.py:1049-1123`, `integrations.py` — the only seam to Bambuddy).
- Agent tools: `agent/src/tools/orders.ts` (65 tools); customer messages through a bound
  plugin, `agent/src/orders/messaging.ts`.

## Data

- Tables: `customers`, `customer_contacts`, `customer_addresses`, `orders`, `order_items`,
  `order_item_quotes`, `order_item_revisions`, `order_item_fulfilments`, `order_shipments`,
  `order_events`, `order_payments`, `order_charges`, `order_attachments`, `order_messages`,
  `order_time_entries`, `order_cost_entries`, `parts*`, `filaments` (the price book),
  `printer_cost_profiles`, `payment_*`, `bank_*`.
- Blob keys: `quote-<sha256>`, `attachment-<sha256>`.
- Settings: `orders_currency`, `pricing_*`, `orders_tax_percent`, `rough_estimate_factor`,
  `orders_deposit_policy` (`backend/scadbuddy/core/settings.py:184-273`).

## Tests

- Backend: `backend/tests/orders/` (pricing, quote, lifecycle, printing, costs, deposits),
  `backend/tests/api/test_orders*.py`, `test_order_*.py`, `test_price_book*.py`,
  `backend/tests/payments/`, `backend/tests/flows/test_order_fulfilment_flow.py`.
- e2e: `frontend/e2e/orders.spec.ts`.

## Limits and known issues

- An archive quote prices a plate of several units as one
  ([#6336](https://github.com/eh-homelab/ScadBuddy/issues/6336)).
- The built-in fulfilment flow ignores a remembered `force_colour_match`
  ([#6304](https://github.com/eh-homelab/ScadBuddy/issues/6304)).
- Item spools are filled only from Bambuddy's inventory, not ScadBuddy's own spools
  (`backend/scadbuddy/orders/spools.py:162-165`).
- Machine-rate actuals from logged maintenance:
  [#5774](https://github.com/eh-homelab/ScadBuddy/issues/5774). Deposit tests that cross
  midnight UTC fail: [#6305](https://github.com/eh-homelab/ScadBuddy/issues/6305).
