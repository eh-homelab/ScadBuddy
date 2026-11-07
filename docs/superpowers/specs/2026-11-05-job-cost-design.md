# Job cost: what each job really cost, and the margin it left

Status: approved by the owner on 2026-11-05. Part of the orders epic (#4248). It builds on the
real-cost read that phase 3 built (#4251, `orders/costs.py`, spec 2026-10-18 §6.4 "As
built") and on the storefront spec's test-print attempts
(`2026-11-02-storefront-design.md` §4.4).

## 1. Why

The owner prices jobs from a slice quote, then prints them. Test prints, failed prints,
reprints and the owner's own time all cost money that the quote did not see. The owner
said: "I need to understand each job's real cost." They want it for three things, all of
them: to price the next quote better, to see which products lose money, and to see each
job's margin. They also said: "we should highlight it to the operator." ScadBuddy must
put the number where the operator already looks, not on a page they must go and find.

## 2. Decisions

The owner's:

- Every print on a job counts toward its cost: test, failed, reprint and production.
- Real cost shows in four places (§6): the order page, the orders list, a warning when
  quoting, and a products report. The owner chose all four on the mockups.
- The owner's own time counts. They left how to record it to the author.

The author's calls (the owner said "figure it out"; marked **(call)** where they appear):

- Time is recorded as time entries with a labour rate (§4.3).
- The assistant's spend on an item counts (§4.4).
- Margin is measured against what was charged, not what was paid (§5).

## 3. Goals, non-goals, anti-goals

Goals:

- Every order and every line item has a real cost in money and a margin, computed from
  what is recorded, with each part traceable to its source (an attempt, a time entry, a
  cost entry).
- A loss or a thin margin is visible without opening the order.
- A quote shows what past jobs of the same model really cost before it is sent.
- A report shows profit, margin and waste by model over a period.

Non-goals:

- Accounting. No ledger, no tax reports, no export to bookkeeping software in this spec.
- Spool-level inventory valuation. A filament's cost is its price-book `cost_per_kg`.
- Electricity metering, printer depreciation as a separate line, or overheads. The machine
  rate per hour already stands for all of them.

Anti-goals (a failure even if it works):

- A cost that looks exact but is a guess. Every figure that is estimated, not measured,
  says so (§4.1 "estimated"), and the margin built on it says so too.
- Time tracking that nags. Recording time must stay optional and take seconds; with no
  entries the item still gets a labour figure (§4.3).
- Rewriting history. A figure is computed from the prices recorded with the job (the
  quote's, the entry's), never from today's price book, except where §4.1 says so and
  marks it.

## 4. What a job's cost is made of

An item's real cost is the sum of four parts. An order's real cost is the sum over its
items plus its order-level cost entries (§4.5).

### 4.1 Prints

Every attempt in `order_item_fulfilments` for the item, whatever its `purpose`
(`production` or `test`, storefront spec §4.4) and whatever its outcome.

- **Filament.** Today `costs.py` prices an attempt's `actual_grams` at its accepted
  quote's price per gram. That stays the first source. Two gaps are closed:
  - **No actual grams** (no Bambuddy archive: a native hot-path print, an archive not yet
    linked, an attempt recorded by hand). The attempt's grams are then taken from the
    quote it printed from: the item's quote whose own `sliced_sha256` equals the
    attempt's (a quote's `sliced_sha256` is its first plate's file, and the attempt
    records that same value), whose plates give grams per unit, times the attempt's
    `copies` (else its `quantity`, else 1: `copies` is null on a hand-recorded
    attempt). The figure is marked `estimated`. A
    failed attempt with no actual grams counts its full sliced grams, marked `estimated`
    **(call: overstating a failure's waste is safer than hiding it)**.
  - **No price per gram** (a `manual` quote, no accepted quote yet, or a test attempt of
    a revision with no accepted quote). The grams are priced at the price book's
    `cost_per_kg` for the item's colour slots' filaments, weighted by the slots' share of
    the sliced grams when known and equally otherwise, and marked `estimated`.
- **Machine time.** New: each attempt's seconds (`actual_seconds`, else the matched
  quote's plate seconds times `copies` (with the same fallback), marked `estimated`) times a machine rate. The
  rate is the quote's own: a slice quote's `machine_time` line amount over its plates'
  hours (the line's label also names it). With no slice quote, or a quote with no
  `machine_time` line, it is today's `pricing_machine_rate_per_hour`, marked
  `estimated`.

An attempt with neither grams nor seconds from any source (a hand-recorded attempt of an
item with no sliced file) costs zero for prints and is flagged `unknown`.

### 4.2 The item's quoted figures

Unchanged from `costs.py`: the accepted quote's grams, seconds, filament cost, unit cost
and price, for the item's quantity. These are the "quoted" column beside the "actual".

### 4.3 Time (labour) **(call)**

New table `order_time_entries`: `id`, `order_id`, `order_item_id` (nullable: time on the
order as a whole, such as packing), `kind` (`design`, `setup`, `post_processing`,
`packing`, `other`), `minutes` (> 0), `rate_minor_per_hour` (the labour rate in force when
the entry was written, copied so a later rate change does not rewrite it), `note`,
`started_at` (nullable, set by the timer), `created_by` (`Actor`), `created_at`.

- A new stored setting `pricing_labour_rate_per_hour` (env-seeded like every runtime
  setting, `SCADBUDDY_PRICING_LABOUR_RATE_PER_HOUR`, default `0`, `live`).
- An item's labour cost is the sum of its entries' `minutes × rate`.
- **An item with no entries** (except a `custom` item, §5) gets a default labour figure, marked `estimated`: its
  accepted quote's `labour` line (as recorded, for the item's quantity), else today's
  `pricing_labour_per_item` times its quantity **(call: so a margin never silently
  leaves labour out; one real entry replaces the default)**.
- A timer: "Start timer" on an item writes nothing; "Stop" writes one entry with the
  elapsed minutes, rounded up to the minute. The running timer lives in the browser
  (per viewer, `localStorage`), so an abandoned one costs nothing **(call)**. It is
  lost if the browser's storage is cleared, and another device does not see it; the
  running timer says "on this browser only", and the page offers to log the elapsed
  time when it finds a timer older than 12 hours.
- Agent tool `order_time_add` (`write`).

### 4.4 Other costs: assistant, shipping, payment fees, anything else **(call)**

New table `order_cost_entries`: `id`, `order_id`, `order_item_id` (nullable), `kind`
(`assistant`, `payment_fee`, `material`, `other`), `amount_minor` (in the
order's currency), `note`, `source` (nullable: what wrote it, such as
`ai_session:<id>` or `payment:<id>`), `created_by`, `created_at`. A unique index on
`(order_id, kind, source) WHERE source IS NOT NULL` makes a writer that runs twice (a
replayed webhook, a retried flow) write once.

- **Assistant.** When the assistant works on an item (the request-to-model flow, a later
  project), the agent service itself posts one `assistant` entry when the work finishes,
  through `POST /orders/{id}/cost-entries` with a service credential, `source`
  `ai_session:<id>`, with the spend it knows. Nothing in the backend reads the agent's
  `ai_*` tables (the database-access rule, #5295). The order's currency may not be USD: a stored setting
  `pricing_usd_rate` (one USD in the order currency, default `1`, env-seeded) converts it,
  and the entry's note keeps the USD figure. Until that flow exists, nothing writes these
  entries; the shop can add one by hand.
- **Payment fees.** When the payments seam (#4957) learns a provider's fee for a payment,
  it writes one `payment_fee` entry on the order, `source` `payment:<payment id>`, in the
  transaction that records the payment (agreed with the storefront spec's author). A
  refund's fee reversal is out of scope for now.
- **Shipping.** A shipment's existing `cost_minor` (`order_shipments`) is the shipping
  cost; it counts as an order-level cost. There is no separate shipping entry kind, so a
  label is never counted twice.
- **Materials, other.** Added by hand from the order page (magnets, keyrings, paint).
  Agent tool `order_cost_add` (`write`).
- **Late costs.** Time and cost entries can be added and deleted on an order in any
  status, closed, cancelled and on hold included, because a fee or a label often arrives
  after the order moved on **(call)**. Each write still takes the order's row lock and
  writes an `order_events` row.

### 4.5 Order-level costs

Time entries and cost entries with no item belong to the order. They count in the order's
cost and margin, and are not spread over items **(call)**.

## 5. Charged, cost and margin **(call)**

- **Charged** for an item: its accepted quote's total. With no accepted quote, the item
  has no charged figure and no margin; its cost still shows. A `custom` item (#4954) is
  charged its price. Its cost is its print attempts, if it has any (§4.1, priced at the
  price book and marked `estimated`, since it has no slice quote), its time entries and
  its cost entries. It takes **no default labour** (§4.3): its manual quote has no labour
  line, and a custom item is often bought-in or a service **(call)**.
- **Charged** for an order: the sum of its items' charged figures plus every charge
  except `tax`, each with its stored sign (a discount is already stored negative, so it
  is added, never subtracted again). `tax` is not revenue. The field is named
  `revenue_minor` to keep it apart from the existing `OrderTotals.charges_minor`.
- **Cancelled work** **(call)**. A cancelled item is charged nothing, and its cost
  (test prints, failed prints, time) still counts, so it shows as a loss: that is the
  real cost of the job that fell through. A cancelled order follows the same rule; it
  appears in the list's "Loss" filter and in the report. The quote warning's history
  (§6.3) leaves cancelled items out.
- **Currency** **(call)**. The rate settings (`pricing_labour_rate_per_hour`,
  `pricing_usd_rate`, `pricing_machine_rate_per_hour`) are in the shop's
  `orders_currency`. For an order in another currency, any part that needs today's
  setting is `unknown` (recorded figures such as the quote's lines and entries still
  count). The report groups by currency first and never adds two currencies together.
- **Margin** = charged − real cost, in money and as a percent of charged.
- **Thin** when the margin percent is below a stored setting
  `orders_thin_margin_percent` (env-seeded, default `15`); **loss** when the margin is
  below zero.
- A margin built on any `estimated` part is shown as estimated (a "≈" before it and a
  tooltip naming the estimated parts). A margin built on an `unknown` part is shown with a
  warning and not tinted as safe.

The figures are computed on read, as `costs.py` does now; nothing stores a cost or a
margin. §6.2 and §6.4 need the same figures for many orders at once (§8).

## 6. Where the operator sees it

### 6.1 The order page (mockup A)

The existing Cost card grows into the order's money view:

- One row per item: charged, real cost, margin. A loss row is red, a thin row amber.
- Each row expands to its parts: every attempt (purpose, outcome, grams, time, filament
  and machine cost, `estimated` when it is), time entries, cost entries.
- An order total row with margin in money and percent.
- "Log time" and "Add cost" on each item and on the order; the item's timer.
- The order header shows a "Loss" or "Thin margin" badge when the order is one, so it is
  seen without scrolling to the card.
- Shown from the moment an item has any cost or charged figure, not only once accepted
  (today the card waits for `accepted`): design time and test prints come before it.

### 6.2 The orders list (mockup B)

A "Margin" column (percent; money in the tooltip), with loss rows tinted red and thin rows
amber. Sortable, and a filter "Loss or thin". Orders with no charged figure show "—".

### 6.3 When quoting (mockup C)

When a slice or manual quote is about to be sent (the quote editor, and the agent's
`order_item_quote` result), ScadBuddy looks up the same model's history: items with the
same `slug` that reached `fulfilled` or later and were not cancelled, last 10 within 365
days **(call)**. It compares their average real cost per unit against **this quote's unit
price** (not its `unit_cost_minor`, which leaves out labour and setup on a slice quote and
equals the price on a manual one). If that price would leave a margin below the thin
threshold (§5), it shows a warning with:

(It deliberately groups by `slug` alone, across commits and inputs: a size or text
change moves the cost less than test and failed prints do, and narrowing by inputs would
leave most models with no history **(call)**.)


- the average real cost per unit and how many jobs it is from;
- the main reason: of those jobs' cost parts, the one that most exceeded what their own
  accepted quotes priced for it. Parts the quotes never price (test prints, failed
  prints, cost entries) count in full;
- a suggested unit price: the average real cost per unit marked up to the shop's
  `pricing_markup_percent` (or to the thin threshold if the markup is zero), with one
  click to use it.

It is advice only; it never blocks a quote. With no history it shows nothing.

As built (#5328): a job's time is its last accepted attempt's `accepted_at`. An item can
become `fulfilled` only by accepting an attempt, so every fulfilled item has one. The
warning tells a price below the real cost apart from a price above it with a thin margin
(`below_cost`), and words each one differently. A job whose accepted quote has no
per-part lines (a `manual` quote, a quote with a `manual` line, or no accepted quote)
priced filament, machine time and labour together. Those three parts are left out of that
job's comparison, so only its failed prints, test prints and other costs can be its main
reason. Its price is never split across parts. The browser asks the route whether a price
warns (`warning`, `below_cost`); it does not work out the thin rule itself.

### 6.4 The products report (mockup D)

A new page, Orders → Profit, `GET /orders/reports/profit?from=&to=&group=model`:

- One row per model `slug` (mesh and parametric alike), plus one row per custom item
  `sku`, and one "Custom, no SKU" row.
- Columns: orders, units, charged, real cost, profit, margin %, and **waste**: the share of
  real cost spent on test and failed attempts.
- The period defaults to the last 90 days by the order's accepted time. Loss rows red,
  thin amber. A row links to the orders behind it.
- Agent tool `orders_profit_report` (`read`).

## 7. API and tools

- `GET /orders/{id}/costs` gains, per attempt: `purpose`, `machine_minor`,
  `estimated: [..]` (which figures are estimated); per item: `labour_minor`,
  `other_minor`, `cost_minor`, `revenue_minor`, `margin_minor`, `margin_percent`,
  `margin_state` (`ok`, `thin`, `loss`, `unknown`), `time_entries`, `cost_entries`; and
  the same sums on the order. Existing fields keep their meaning.
- `GET /orders` items gain `margin_percent` and `margin_state`, and the list takes
  `margin_state` as a filter.
- `POST/DELETE /orders/{id}/time-entries`, `POST/DELETE /orders/{id}/cost-entries`, each
  an `order_events` audit row, as every order write is. `POST …/cost-entries` also
  accepts the agent service's service credential, which is how the `assistant` entry
  arrives (§4.4).
- `GET /orders/history/unit-cost?slug=` for §6.3.
- `GET /orders/reports/profit` for §6.4.
- Agent tools: `order_costs` (exists, gains the fields), `order_time_add`,
  `order_cost_add` (`write`), `orders_profit_report` (`read`); every new route has a tool
  or a coverage entry.
- Settings → Orders gains the labour rate, the USD rate and the thin threshold.

## 8. Constraints

- Money is integer minor units, rounded once per figure as `pricing.py` does.
- The list column and the report cannot call Bambuddy per order. Bambuddy's own cost is
  not in any sum today and stays out, so the list and the report read only Postgres. The
  per-order read keeps showing Bambuddy's figure beside the sums.
- Settings are stored in Postgres and seeded from env vars; migrations are new files.
- The storefront's phase 1 (#5280) adds `order_item_fulfilments.purpose`
  (`production` | `test`, default `production`) and `revision_id` (test attempts only).
  `OrderStore.fulfilments` defaults to `purpose="production"`; every read in this spec
  passes `purpose=None` so test attempts count. This spec adds no columns to that table.
  If it is built before #5280 merges, every attempt is `production` and "waste" counts
  failed attempts only, with no other change.

## 9. Left to the builder

- How the list and the report compute margins at scale (one SQL query, a view, or a
  per-order cache invalidated by the order's events), provided the figures match the
  per-order read exactly.
  As built for the list (#5327, #5515): `GET /orders` computes each page's margins in
  Python, with the same computation as the per-order read (`costs.py` `order_margins`).
  The `margin_state` filter reads store pages of 100 and computes their margins. It
  stops at 10 pages (`api/orders.py` `MAX_SCAN_PAGES`) and returns the rows found and
  the cursor reached, so a page can be short or empty while a cursor remains.
  As built for the report (#5329): `GET /orders/reports/profit` also computes in Python.
  It reads the period's orders in batches of 100, newest accepted first
  (`OrderStore.accepted_orders`, over the index `orders_accepted_at`), and costs each batch with `_order_costs` over one
  `cost_rows` read. It stops after 2000 orders (`report.py` `MAX_REPORT_ORDERS`). Then
  `complete` is false, and `read_from` is the oldest accepted time it read. The page
  says so. Nothing is stored or cached. The period includes `from` and excludes `to`.
  Cancelled orders count: they are charged nothing, and their cost is a loss. Each row
  sums its items' figures from the per-order read. An item with a model `slug` is in that
  model's row, else an item with a `sku` in that SKU's row, else in the "Custom, no SKU"
  row (an empty SKU and a request item with no design yet go there too). The order's own figures (its
  entries, its shipments' cost, its charges but tax) are on no item. They are an extra
  `order` row: the order's read less its items'. Its `orders` counts only the orders
  with a figure of their own. No list filter matches those orders, so that row has no
  link. Each currency also has a `total` row, the sum of its orders' reads. A row's `product` (`model:<slug>`, `sku:<sku>` or
  `custom`) is a new `GET /orders` filter. With `accepted_from`, `accepted_to` and
  `currency`, also new, it lists the row's orders. The agent's `order_list` takes them
  too.
- The weighting of a multi-filament estimate when the sliced split is unknown.
- The report's chart, if any.

## 10. Phases

1. The money view: §4.1, §4.2, §5 and §6.1, plus time and cost entries (§4.3, §4.4 by
   hand) and the settings. It does not wait for the storefront (§8).
2. The list column and filter (§6.2).
3. The quote warning (§6.3).
4. The profit report (§6.4).
5. The automatic writers: the assistant entry (with the request-to-model project) and
   payment fees (with #4957).
