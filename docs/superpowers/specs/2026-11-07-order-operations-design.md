# Order operations: more item sources, a Shopify-style items card, item versions, a derived machine rate

Status: draft for the owner's approval. Covers #4777 (sources 2 and 4), #4955, #4673 and
#4625, all under the orders epic #4248. Written 2026-11-07 by the orders session.

Builds on: the orders spec (`2026-10-18-orders-design.md`), the storefront spec
(`2026-11-02-storefront-design.md`, §4 revisions), the job-cost spec
(`2026-11-05-job-cost-design.md`). Marks: **(call)** is the author's choice; the rest is
what the issues and the owner already said.

## 1. Why

The owner's biggest pains are getting the model and the quote (steps 2 and 3) and
printing (step 5) (brainstorm of 2026-11-05). Each part below removes clicks or guesswork
from those steps:

- An item comes from wherever the thing already is: any recent render, or a past print.
- The items card works like Shopify's order editor, so common edits take one or two clicks.
- An item can hold many sliced versions, each quoted, so the print is exactly what was quoted.
- The machine rate is computed from what a print-hour really costs, not typed once and left.

## 2. Goals, non-goals, anti-goals

Goals: the four parts below, each shippable on its own.

Non-goals: a public storefront (scadify's #5282), payments (#4956/#4957), new item kinds
(§3.2 reuses `model` and `library_file`).

Anti-goals (a failure even if it works):
- A quote that differs from what is printed.
- A price that moves after the customer accepted it (accepted quotes stay snapshots, #4250).
- A page that is unusable between PRs.

## 3. More item sources (#4777, sources 2 and 4)

Sources 1 (model → output), 3 (library file, #5544) and 5 (custom, #4954) are merged.

### 3.1 Recent Renders across models (source 2)

- New route `GET /outputs` (the CRUD service): saved outputs of every model, newest first.
  Filters: `from`, `to` (ISO times), `slug` (repeatable), `tag`, `q` (name or parameter
  text), `ordered` (`yes`/`no`: on any order or not), and paging as `list_outputs`.
  The answer rows are `OutputDetail`, as today's per-model list.
- **(call)** Outputs are `meta.json` files per model today (`library/outputs.py`
  `list_for`), so a cross-model, date-filtered, paged query needs an index: a new table
  `output_index` (output id, slug, name, created_at, the inputs that differ from defaults
  as text for `q`), written in the same step that saves an output, and backfilled once
  from the files at start. State lives in Postgres; the files stay the bytes' home.
- `tag` filters by the model's catalogue tags (outputs have none).
- The Add item dialog gets a "Recent renders" tab: thumbnail, model, output name, the
  parameters that differ from defaults, age. Filters: 24 h / 7 d / 30 d / custom range,
  model, tag, text, ordered. Multi-select, each with a quantity.
- **(call)** Adding several outputs is one request, `POST /orders/{id}/items/batch`
  (`items: [ItemCreate]`, at most 50), all or nothing in one transaction, so a failure
  never leaves half an order. Each item goes through `add_item` as today (revision 1,
  parts rule, after-acceptance rules).
- Agent: `list_recent_outputs` (read) with the same filters; `order_item_add` gains
  `items` for a batch.

### 3.2 A past print (source 4)

- The dialog gets a "Past prints" tab over every Bambuddy archive: `GET /farm/archives`
  (the agent's `list_print_archives`), filtered by date, printer, project, status, name.
  `GET /prints` is not enough: it lists only archives linked to a ScadBuddy output or
  library file.
- **(call)** No new item kind. A past print becomes an item of a kind that already prints
  through a tracked run (a `print_runs` row, `claim_print`, `follow_print`):
  - from a ScadBuddy output: a `model` item from that output, exactly as source 1;
  - from a ScadBuddy library file: a `library_file` item of that file (#5544);
  - from anywhere else: the archive's 3MF is copied into ScadBuddy's native library
    (the blob store, one library file per archive, reused if the same archive is picked
    again), and the item is a `library_file` item of that copy.
  So every past print reprints through the library print run, is claimed and settles as
  other items do. An archive gone before the copy is 409 `archive_gone`.
- The item keeps where it came from: `origin_archive_ids` (the archives it was taken
  from) for the source chip and the quote below.
- Native LAN prints have no Bambuddy archive. They are already reachable as their output
  (Recent renders) or library file (#5544), so "Past prints" lists Bambuddy archives only.
- A new quote source `archive` **(call)**: priced from what the print used, at the price
  book in force, with no re-slice.
  - A multi-plate print is several archives (one per plate). The quote sums every plate
    archive of the same print run, and refuses with `archive_plates_missing` when a plate
    of the run has no archive.
  - Grams per slot are the slicer's estimates from each archive (`filament_slots`);
    seconds are the actual print time when the archive succeeded, else the estimate.
  - A failed or cancelled archive is not a quote source (`archive_not_succeeded`); slice
    and manual quotes stay available.
  - Job cost (§4.1 of its spec) treats an `archive` quote's grams as the quote's, so the
    price per gram works as for a slice quote.
- Agent: `order_item_add` takes `archive_id`.

## 4. The Shopify-style items card (#4955)

The issue asks for an audit and a mock signed off by the owner before build. This section
is that mock; approving this document approves it.

### 4.1 Target

```
Items                                   [Add product] [Add custom item]
[ ] ▢ Name keychain ×10  model·cap-keychain  10 × $4.50 = $45.00  Unfulfilled  ⋯
[ ] ▢ Hook pack           custom              1 × $3.00 =  $3.00  Fulfilled    ⋯
    selected: 1   [Fulfil] [Print] [Remove]
Summary   Subtotal $48.00 · Shipping $5.00 · Tax $3.84 · Total $56.84
          Paid $20.00 · Balance $36.84
```

- One card. A row: thumbnail, title, source chip (model, output, file, past print,
  custom, request), quantity × unit price = line total, status chip, the money view's
  margin badge (job cost), and a ⋯ menu.
- Inline edit: click the quantity, price or title. Enter saves, Esc cancels, Tab moves
  along the row. **(call)** Today's rules stay: while the order is `draft` or `quoted`, a
  typed price records a manual quote and accepts it (as `OrderItemsTable.tsx` does now).
  Past `quoted`, quantity and price are read-only, with the reason shown (`item_locked`);
  the only change after acceptance is #5350's re-quote of a revised item. The title stays
  editable while the item is open.
- ⋯ menu: Quote from slice, Print setups (§5), Print, Fulfil, Unfulfil, Duplicate, Remove.
  An action shows only when it is allowed now. A refused action says why in words.
- Bulk: select rows, then Fulfil, Print or Remove. Fulfil opens one dialog with a quantity
  per item.
- Summary card: subtotal, charges added inline (shipping, discount, fee, tax), total,
  paid, balance. Payments come from #4957 when it lands; until then from today's manual
  payments.

### 4.2 Build steps (one PR each; the page works after every step)

1. The card and the row layout. It replaces today's table in one step and keeps every
   existing action, moved into the ⋯ menu.
2. Inline edit of quantity, price, title.
3. Bulk select and the one-dialog fulfil.
4. The summary card.
5. Playwright e2e: add, inline edit, fulfil, unfulfil on the mocked API.

**(call)** No new backend routes except `POST /orders/{id}/items/{item}/duplicate`
(copies the item and its current revision, quote not copied) and a bulk fulfil
`POST /orders/{id}/fulfil` (`items: [{item_id, quantity}]`, one transaction).

## 5. Item versions (#4673)

The owner's framing (2026-10-24): a quote is a simulation of the print; an item holds many,
the way the Carly keychain went through about 100 variations; one is accepted and printed.

Words: the storefront spec owns "revision" (a design the customer sees: inputs and
colours). A "version" in #4673's sense is one quote under a revision: its print choices,
sliced once. **(call)** The UI and tools say **print setup**, because models already
have "versions" (git commits: `list_versions`, `diff_version`, `restore_version`).

### 5.1 Data

- A quote already holds `revision_id` (storefront §4.3), `sliced_sha256`, plates and lines.
  It gains `choices` (the full print choices) and `filament_plan` **(call: jsonb columns
  on `order_item_quotes`)**.
- `order_item_quote.slice` gains `prime_tower {enabled, no_sparse_layers}`,
  `supports {enabled, type, threshold_angle, on_build_plate_only}` and `print_sequence`
  beside `choices`, in exactly `PrintRunRequest`'s shapes (they are top-level fields
  there, not part of `PrintChoices`). The quote stores them with `choices`.
- **First step of §5 (a print-path PR):** the order print sends the accepted quote's own
  sliced file. Today the order run re-slices the output with the print request's choices
  and reuses the quote's slice only when they match (`orders/printing.py`, "What it does
  not do yet"). After this step the print request carries no choices of its own: it
  prints the accepted quote's `sliced_blob_key`, so a print can never differ from its
  quote. A quote with no stored slice (manual, archive) re-slices with the stored choices,
  or the item's defaults when it has none.
- Changing inputs or colours makes a new revision (storefront §4.1 rule), then a quote
  under it. Changing only print choices makes another quote under the same revision.
  Nothing is replaced; stale rules stay as the storefront spec sets them.
- A quote under a revision still passes the `expected_revision_id` check: a slice that
  finishes after a newer revision exists writes nothing.
- A new revision from a slice goes through the Lifecycle's existing revise path, never a
  direct store write. So `design_first` approval, `superseded` answers, the #5350
  re-quote after acceptance (`REQUOTED`, `print_in_flight`) and #5281's `deposit_unpaid`
  refusal apply unchanged: until the deposit is paid or waived, sending the new revision
  and starting a test print are refused.

### 5.2 Surfaces

- "Slice" on a row opens the print dialog's choice step for that item, with the inputs
  editable in place. Submit runs `order_item_quote` (slice). Repeat freely.
- After acceptance, "Accept" can pick another print setup only through #5350's re-quote
  path; otherwise the accepted one is pinned.
- "Print setups" lists every quote of the item, grouped by revision: price, time, grams,
  plate count, a plate preview, and what changed from the accepted one (inputs, choices).
  "Accept" pins one; it is today's `accept_quote`. Accept is disabled, with the reason,
  on a quote whose revision is not the item's current one (`accept_quote` refuses it).
- Agent: `order_item_quote.slice` gains `inputs`/`commit` (makes the revision first) and
  the three choices; `order_get` lists print setups with their choices.

### 5.3 Depends on

#5280 (scadify, revisions). Its revisions, `revision_id` on quotes and test prints are on
main (#5315, #5337, #5347), and #5280 is closed. scadify reviewed §5 against them on
2026-11-07. §5 is not blocked.

### 5.4 As built

- PR 1 (#5720): `SliceQuoteRequest` takes `prime_tower`, `supports` and `print_sequence`
  (`PrintRunRequest`'s own types); `orders/operations.py` `preview_request` passes them to
  the background slice. `order_item_quotes.choices` (`printer_id`, `printer`, `path`,
  `choices`, `prime_tower`, `supports`, `print_sequence`, each present, null when left out;
  `orders/quote.py` `print_setup`) and `filament_plan` are written with a slice quote and
  shown on `Quote`, so `order_get` lists them. A manual or archive quote has neither. The
  agent's `order_item_quote.slice` takes the three.
- PR 2: the print reads the whole setup. An item's print or test print takes the body
  `ItemPrintRequest` (`orders/printing.py`: `choices` and `filament_plan` optional), and
  each of `QUOTED_SETUP` (every `choices` key above) and the spools it leaves out is the
  quote's (`quoted_setup` in the check's result, `operations.py` `filled` in the check
  and in `print_body`), so `{}` prints what was quoted and reuses the quote's background
  slice while it is kept. One sent that differs is `requote_needed`, naming it
  (`setup_differences` over `COMPARED_SETUP`), never sliced again silently, with three
  exceptions **(calls)**: `path` is filled but not compared (#5352: another route prints
  the same setup and slices again there); a printer name either side leaves null is not
  compared (the run refuses a `printer`/`printer_id` pair that does not link); and the
  spools are never compared (the check fills them from the item's filaments, §5.4 of the
  orders spec). Supports off compare equal whatever type or angle rides along. A quote
  that records no setup (manual, archive, or made before PR 1) prints with the
  request's, which must then send `choices` and `filament_plan` (422
  `print_setup_missing`). The built-in `order-fulfilment` flow sends `{}` for an item
  whose accepted quote records a setup. The agent's `order_item_print` takes every field
  optional. The page's print dialog opens on the accepted quote's setup (`PrintItem.tsx`
  `quotedSeed`, `PrintPicker`'s `itemPrint.seed`, applied as a batch step's own seed),
  so its Print sends what was quoted.
- PR 3: a print setup with new inputs. The quote route's body is `ItemSliceQuoteRequest`
  (`orders/quote.py`): `SliceQuoteRequest` with `inputs` and `commit`, each left out the
  item's. When they differ from the item's reference, the route (`api/orders.py`
  `_slice_revision`) resolves them as a revision's reference and makes the revision through
  `Lifecycle.add_revision`, `after_acceptance` for an item with an accepted quote on a
  `TAKES_ITEMS` order not yet revised after acceptance (decided under the order's lock),
  so every refusal of that path applies unchanged. That step is `run_operation`'s `before_start`, so it runs only when
  no record answers the key and its operation is not running (a replay makes no
  revision), and it first runs the quote's own check on the body alone (`quote_check`
  `body_only`: the cart, the filaments the body names, a support slot the body also names
  as a colour, the price book and the machine rate; the new design's slots, which only
  its render gives, are the run's to judge, as are the carried pins and the support
  slots against those slots), so a refusal there writes nothing and leaves an accepted item's one revise after
  acceptance unused. The same design makes no revision (`add_revision` `reuse_current`
  compares it under the order's lock and writes nothing), so two first sends of one key
  that race make one. The new revision has no colour slots until it renders, so the
  route carries the item's pinned filaments with it (`carried_filaments`), and the
  render's slots take them by slot number where the body names none (`quote.py`
  `ensure_output`). A slot the new design adds that neither names is known only after
  the render: the run refuses it (422 `filaments`) after the revision is written, and a
  slice that names its filament quotes that revision. The design keys the operation's
  request (`design`); the revision and the carried filaments do not (the kind's
  `unkeyed`). `commit` is 7 to 40 hex characters. The operation's request carries the revision as `revision_id`, and the check
  (`quote_check`), the run (`quote_run`) and the record (`record_slice_quote`, under the
  order's lock) refuse 409 `revision_superseded` once another revision is current; the
  order page says to slice again (`refusal.ts` `SUPERSEDED_TEXT`). A storefront cart is
  refused `order_not_submitted` before anything of a revision is read. The agent's
  `order_item_quote.slice` takes `inputs` and `commit`.

## 6. A derived machine rate (#4625)

Interim since 2026-10-24: `machine_rate_per_hour = 0.90`, computed by hand from
maintenance ~0.24, wear ~0.14, power ~0.03, depreciation ~0.48.

### 6.1 Inputs, per printer

- **(call) Printer identity:** the tables are keyed by the registry printer (the
  `printer` string of ScadBuddy's printers registry), which covers LAN-only printers. A
  printer linked to Bambuddy (`link_printer_bambuddy`) reads its maintenance intervals
  from Bambuddy; a LAN-only printer has its maintenance tasks typed in ScadBuddy, or none.
- **Printer cost profile** (new table `printer_cost_profiles`, one row per printer):
  purchase price, expected life in print-hours, watts, price per kWh, labour rate for
  maintenance **(call: defaults to `pricing_labour_rate_per_hour`)**.
- **Wear parts** (new table `printer_wear_parts`): name, cost, expected life in
  print-hours (nozzle or hotend, plate, PTFE, filters).
- **Maintenance tasks**: read from Bambuddy's `/api/v1/maintenance/overview` by the CRUD
  service's Bambuddy client (interval in print-hours per task). ScadBuddy stores, per
  task, minutes per occurrence and consumables cost (new table
  `printer_maintenance_costs`, keyed by printer and Bambuddy task name).

**As built (#5683):** the tasks are read per printer from
`GET /api/v1/maintenance/printers/{id}` (scope Read Status), not from
`/maintenance/overview`. The per-printer route answers one printer's tasks with each
task's `interval_type` and `enabled`. Calendar and disabled tasks are listed and never
counted. Shapes:
[`backend/app/schemas/maintenance.py`](https://github.com/maziggy/bambuddy/blob/2f87192334f66bc4b27b84ba48c6a2d216dd8afc/backend/app/schemas/maintenance.py)
at maziggy/bambuddy@2f87192. Each read keeps the intervals of the tasks it counted, to the
tenth of an hour (an interval that rounds to nothing is not counted), and clears the
rest, so the stored rate (Bambuddy unreachable, the money view) is the rate last read.
Once the printer is unlinked, the intervals sent with its inputs are kept as typed.

### 6.2 The rate

`rate = Σ tasks (minutes × labour rate / 60 + consumables) / interval_hours
       + Σ wear (cost / life) + watts / 1000 × kWh price + purchase / life`.

- A slice quote prices machine time with the rate of the printer it sliced for. A quote
  that names a Bambuddy `printer_id` resolves it to the registry printer it is linked to;
  a quote that names neither uses the configured default printer. A printer with no
  profile uses the price book's `machine_rate_per_hour` (today's behaviour).
- Quotes gain two columns: `printer` (the registry printer it was priced for) and
  `machine_rate_breakdown` (jsonb: each part of the sum), so a price can be explained and
  never moves later. `QuoteLine` stays as it is.
- Bambuddy unreachable: the quote uses the last stored intervals and says so in the
  breakdown **(call)**.
- Settings → Pricing gets a per-printer "Machine rate" panel showing each part of the sum.

### 6.3 Support material

A slot or the support interface filament (PETG under PLA on the H2C) maps to a price-book
filament in the quote dialog, and is priced as its own filament line from the slice's
grams.

### 6.4 Out of scope

Actuals (logged maintenance or replaced parts feeding back). A later issue.

## 7. Constraints

- Only the CRUD service touches SQL (#5295). New tables are its own; nothing new reads
  another service's tables.
- Migrations are new files. Generated API files are never committed.
- scadify is changing `orders/lifecycle.py`, `models.py`, `store.py` and `api/orders.py`
  for payments and deposits; it lands first and this work rebases. Each PR's files are
  sent to scadify before it opens.
- After scadify's #5281 (B1) is on main, every new transaction here that changes items
  or charges (the batch add, duplicate, bulk fulfil) calls `self._retax(conn, order)`,
  and totals come from `orders/totals.py` (moved out of `api/orders.py`).

## 8. Order of work

1. §3 sources (independent).
2. §4 items card, steps 1–5.
3. §6 machine rate (independent; can run beside §4).
4. §5 print setups (not blocked; #5280 is closed).

## 9. Left to the builder

Component layout and names, query shapes, page sizes, index choices, how the dialog's
tabs are arranged, test layout.
