# Machine rate actuals: logged maintenance and replaced parts feed back into the rate

Status: draft for the owner's approval. Covers #5774, under the orders epic #4248.
Written 2026-11-17. Nothing is built before the owner approves this design.

Builds on: the order operations spec (`2026-11-07-order-operations-design.md`, §6, the
derived machine rate of #4625, shipped in #5683 and #5759) and the job-cost spec
(`2026-11-05-job-cost-design.md`). Marks: **(call)** is the author's choice; the rest is
what the issue, §6 and the owner already said. §2 lists the measured facts. Each one says
where and how it was measured.

## 1. Why

The machine rate's per-hour figures come from inputs typed once (§6.1): each wear part's
cost and life in print-hours, each maintenance task's minutes and consumables, and
Bambuddy's interval for each task. Nothing checks these figures against what really
happens. If a nozzle lasts 600 h and not the 1000 h typed, the rate stays low until a
person types the life again.

This design logs what really happens and computes an actual per-hour figure for each
wear part and each task. Settings shows the estimate and the actual side by side. For
each figure, the person chooses which one prices. The choice is remembered.

## 2. Measured facts

### 2.1 Bambuddy keeps a maintenance log (open point 1)

- **Bambuddy records each task done, with the print-hour counter at that time.** Its
  table `maintenance_history` has `performed_at`, `hours_at_maintenance` (the printer's
  print-hour counter when the task was marked done) and `notes`. It has **no** minutes,
  no consumables and no cost. Source:
  [`backend/app/models/maintenance.py`](https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/models/maintenance.py)
  at maziggy/bambuddy@1aa7bce (main on 2026-11-13, read 2026-11-17 with `gh api`).
- **"Perform" writes the log row.** `POST /api/v1/maintenance/items/{item_id}/perform`
  adds one `maintenance_history` row with the current counter, then sets
  `last_performed_at` and `last_performed_hours` on the item. Source:
  [`backend/app/api/routes/maintenance.py`](https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/api/routes/maintenance.py)
  `perform_maintenance`, same commit.
- **The log is readable.** `GET /api/v1/maintenance/items/{item_id}/history` answers
  every row of one item, newest first (`MaintenanceHistoryResponse`: `id`,
  `printer_maintenance_id`, `performed_at`, `hours_at_maintenance`, `notes`). It needs
  `MAINTENANCE_READ`, the same permission as the per-printer route that #5683 reads. Same
  file, `get_maintenance_history`.
- **The log has real data on prod.** Measured on 2026-11-17 with a read-only script run
  in the prod API pod (`kubectl -n bambuddy exec`, ScadBuddy's own `client_for` and the
  stored settings; no key printed), against Bambuddy 1.3.1 (`get_bambuddy_status`).
  Printer 1 (the H2C, registry printer `3DP-31B-598`) has six tasks. Two have history:

  | Task | Interval | Log rows | `hours_at_maintenance` |
  | --- | --- | --- | --- |
  | Clean Build Plate | 25 h | 5 | 66.5, 269.0, 304.0, 341.9, 465.6 |
  | Check PTFE Tube | 500 h | 1 | 269.0 |

  The other four (Lubricate Linear Rails 50 h, Clean Linear Rails 100 h, Clean
  Nozzle/Hotend 100 h, Check Belt Tension 200 h) have no rows.
- **The gaps differ much from the interval.** The build plate's last three gaps, from the
  counter readings rounded to 0.1 h, are 35.0 h, 37.9 h and 123.7 h (mean 65.5 h), against an interval of 25 h. The person does
  this task less often than the schedule says. The current rate prices it as if done
  every 25 h.
- **Bambuddy has no part replacement and no part cost.** Its tasks are types with an
  interval (`maintenance_types`). A person can add a custom type, such as "Replace
  nozzle" (`POST /api/v1/maintenance/types`), and mark it done. That gives a
  replacement's time and counter, but never its cost. Same files.
- **Bambuddy has one flat wear rate per printer.** `printers.wear_cost_per_hour`
  (Bambuddy #694) is a typed number. Its print log multiplies it by a run's duration
  (`backend/app/services/print_log.py` `wear_cost_for_run`, same commit). It is null on
  prod printer 1 (the same 2026-11-17 read, `GET /api/v1/printers/1`). It is not an
  actual. This design does not read it or write it.
- **Time zone.** The history's `performed_at` has no zone (`2026-11-15T03:59:38.036376`).
  The overview's `last_performed_at` for the same event is `2026-11-15T03:59:38.223609Z`.
  So the history's times are UTC. Same 2026-11-17 read.

### 2.2 Where print-hours come from (open point 2)

- **Bambuddy's counter is the best source for a linked printer.** Bambuddy computes a
  printer's hours as `runtime_seconds / 3600 + print_hours_offset`. `runtime_seconds`
  counts only the time in the RUNNING state (paused time is not counted, Bambuddy #1521).
  Source: `get_printer_total_hours` in the routes file above, and
  [`backend/app/models/printer.py`](https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/backend/app/models/printer.py).
  The counter is not in `GET /printers/{id}`. It is in the maintenance overview as
  `total_print_hours` and in each task as `current_hours`, and in each log row as
  `hours_at_maintenance`. On prod, printer 1 reads 495.3 h, offset 0 (same read).
- **Bambuddy's print archives undercount.** Printer 1 has 216 archives since
  2026-09-21. Their `actual_time_seconds` sum to 406.7 h. 84 of the 216 have no actual
  time. The counter says 495.3 h. Measured with the same read-only script (`GET /api/v1/archives/?printer_id=1`,
  every page). So a sum of archive durations is about 18 % low (406.7 against 495.3). It is rejected.
- **ScadBuddy's own print runs cover only the prints ScadBuddy started.** A job queued in
  Bambuddy or started on the printer has no `print_runs` row, and `print_outcomes` has no
  duration (`backend/scadbuddy/migrations/20261024T1854Z_print_outcomes.sql`). Rejected.
- **The printer reports no lifetime counter over the LAN.** The recorded H2C reports
  (`backend/tests/printers/fixtures/lan-report-h2c.json`, `lan-report-h2c-idle.json`)
  hold no lifetime print-time or hour key (searched for every key that contains `time`
  or `hour`). So a printer that only the LAN reaches has no counter today.
- **Prod has one physical printer, and it is linked to Bambuddy.** `list_printers` on
  2026-11-17: `3DP-31B-598` (`bambuddy_printer_id` 1) and one Bambuddy virtual printer.
  `machine_rate_get` for it answers `source` `price_book` (rate 0.90555 USD): it has no
  profile yet, so no actual can price it until it has one.

## 3. Decisions

### 3.1 Events come from Bambuddy and from ScadBuddy (both)

- **A task done, on a linked printer:** imported from Bambuddy's log (§2.1). The person
  keeps marking tasks done in Bambuddy, as today. ScadBuddy reads the time and the
  counter. The person may add the real minutes and consumables to an imported event in
  ScadBuddy. An event with none uses the task's typed figures (§3.3).
- **A task done, on a printer Bambuddy does not track:** typed in ScadBuddy, with the
  counter reading the person gives (§3.4).
- **A replaced wear part:** typed in ScadBuddy, with its real cost. Bambuddy has no part
  or cost (§2.1). **(call)** A wear part may also name a Bambuddy task (`bambuddy_task`,
  for example a custom "Replace nozzle" type). Then each log row of that task is imported
  as a replacement of the part, at the part's typed cost until the person enters the real
  one. A task that a wear part names is left out of the maintenance list and adds nothing
  to the maintenance part of the rate, so the part is never counted twice. The breakdown
  lists it under the wear part (`why` `wear_part`).
- ScadBuddy never writes to Bambuddy. It does not mark a task done there when a person
  logs one in ScadBuddy **(call)**: one place owns each fact. On a linked printer, the
  Settings form says "Mark tasks done in Bambuddy; ScadBuddy reads them" and offers no
  typed task event.

### 3.2 Print-hours

- **Linked printer:** Bambuddy's counter (§2.2). An imported event carries its
  `hours_at_maintenance`. A typed replacement reads `current_hours` from Bambuddy when it
  is logged, and the person may change it (a replacement logged late).
- **Printer only the LAN reaches:** the person types the counter reading at the event, or
  types the part's hours directly ("this nozzle ran 640 h"). **(call)** ScadBuddy does not
  build its own counter in this issue. §8 asks the owner whether to add one from the LAN
  watch's RUNNING time later.
- Hours are kept to 0.1 h (`numeric(10, 1)`), as the intervals are. Bambuddy's floats are
  rounded half up to 0.1, as `machine_rate.py` `_hours` does today. A counter reading
  keeps 0 (a new helper, not `_hours`, which answers `None` for an interval that rounds to
  0): a first event at 0.0 h has a known counter.

### 3.3 The rule: per-hour actuals

The rule is a ratio of sums over a window of the last N gaps (so it is weighted by
hours, not a mean of per-gap rates). A short gap then cannot swing the figure.

**A task's actual per hour:**

```
gaps      = the last N gaps between two events of the task, newest first
gap_i     = hours_at(event_i) - hours_at(event_i-1)
cost_i    = minutes_i x labour rate / 60 + consumables_i        (event_i's own figures)
actual    = sum(cost_i) / sum(gap_i)
```

- An event with no minutes uses the task's typed minutes; one with no consumables uses
  the typed consumables. The figure is then marked `partly_typed`.
- The first event of a task opens no gap. Its counter before is unknown: Bambuddy's
  counter started when Bambuddy first saw the printer, not when the task was last done.
- A gap of 0 h or less (two events at one counter reading, or a counter reset) is
  skipped and named in the breakdown (`skipped_gaps`).
- The actual also gives an **actual interval** (`sum(gap_i) / count`) and an **actual
  cost per occurrence** (`sum(cost_i) / count`), shown beside the typed ones.

Example, from prod's Clean Build Plate (§2.1), with 10 minutes at a labour rate of 30 and
no consumables (5.00 per occurrence), N = 3: estimate `5.00 / 25 = 0.2000` per hour;
actual `15.00 / (35.0 + 37.9 + 123.7) = 15.00 / 196.6 = 0.0763` per hour.

**A wear part's actual per hour:**

```
replacements = the last N replacements of the part that have hours_used, newest first
hours_used_r = hours the removed part ran: hours_at(r) - hours_at(r-1),
               or the hours typed on r
actual       = sum(cost_r) / sum(hours_used_r)
```

- `cost_r` is what the new part cost. In a steady state that equals what the removed part
  cost. **(call)** This keeps one cost per row and needs no pairing of rows.
- A first replacement with no earlier one and no typed hours has no `hours_used`. It
  adds neither its cost nor hours to any sum. It only marks where the next part's life
  starts: its `hours_at` is the base of the next replacement's `hours_used`.

**Fewer than N:**

- With at least one gap (task) or one replacement with hours (part), the actual is shown
  as "k of N".
- **(call)** It prices only once it has N. Until then a figure set to "actual" prices at
  its estimate and the breakdown says why (`basis` `estimate`, `why` `too_few`, `have`
  k). A rate does not jump on one odd event.
- N is a per-printer setting, `actuals_window`, default 3, from 1 to 20. A person can set
  1 to use actuals at once.

**Rounding:** each actual per hour is rounded half up to 0.0001 once, as every part is
today (`RATE_PLACES`). The breakdown still adds up to its rate.

### 3.4 Which figure prices: a remembered choice per figure

- Each wear part and each task row gets `priced_by`, `estimate` or `actual`. It is stored
  with the row, shown as a toggle on the row, and kept until a person changes it (the
  owner's rule: every setting is a remembered choice with a toggle).
- **(call)** A new row starts at `estimate`. §8 asks whether the default should be
  `actual`.
- A task priced by its actual replaces both parts of its estimate: Bambuddy's interval
  and the typed cost per occurrence. **(call)** One figure per row, not two. §8 asks
  whether to split interval and cost.
- Power and depreciation have no actuals in this issue (§7).

### 3.5 Imports

- A linked printer's log is read through the orders seam (`orders/integrations.py`,
  `orders/bambuddy.py`), a new client method `maintenance_history(item_id)` (scope
  `Read Status`, operation `maintenance.history`). The prod key reads it (§2.1). The
  seam's `MaintenanceTask` gains two fields from the same per-printer read: `item_id`
  (Bambuddy's `id` of the task on the printer, which the history route takes) and
  `current_hours` (the counter, which the view and the replacement form show).
- Reads happen at three times. (1) The Settings view of a printer (`GET
  /printers/{id}/machine-rate`), after the live task read it already makes. (2) A "Read
  from Bambuddy" button (`POST …/machine-rate/import`). (3) A new housekeeping sweep,
  `housekeeping_import_maintenance`, on the sweeps' Schedule only (not `PRUNE_SWEEPS`:
  it calls Bambuddy, as `PRINTER_SYNC_SWEEP` does in `workflows/housekeeping.py`), behind its own `workflow.patched`. It runs in the API's `library` worker,
  which holds the database.
- A quote never waits on the log: it prices with the stored events, so the quote's hot
  path makes no new Bambuddy call.
- Each import is idempotent: an event keeps `bambuddy_history_id`, unique per printer. A
  row read again changes nothing. A row gone from Bambuddy (an item deleted there) stays
  in ScadBuddy **(call)**: an event is a fact that happened.
- Only the tasks the rate lists are read (the ones Bambuddy names for the printer), at
  most `MAX_ROWS` (50). Bambuddy not answering leaves the stored events as they are, and
  the view says when they were last read (`events_read_at`).

## 4. Data model

One new migration, `<yyyymmdd>T<hhmm>Z_machine_rate_actuals.sql`. It adds new tables
and columns only; it edits no merged file. Every table is the CRUD service's own (#5295).

```sql
-- A maintenance task done on a printer: imported from Bambuddy's log, or typed.
CREATE TABLE printer_maintenance_events (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  task text NOT NULL CHECK (length(task) BETWEEN 1 AND 100),
  performed_at timestamptz NOT NULL,
  -- The printer's print-hour counter when it was done. Null: not known (no gap).
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  -- Null: the task's typed figure prices this event (`partly_typed`).
  minutes numeric(8, 1) CHECK (minutes >= 0),
  consumables numeric(14, 4) CHECK (consumables >= 0),
  notes text CHECK (length(notes) <= 1000),
  origin text NOT NULL CHECK (origin IN ('bambuddy', 'typed')),
  bambuddy_history_id integer,
  -- A typed event's writer key, so a retry writes once (as job cost's `source`).
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  UNIQUE (printer, bambuddy_history_id)
);
CREATE INDEX printer_maintenance_events_task
  ON printer_maintenance_events (printer, task, hours_at DESC);

-- A wear part replaced: its real cost, and the hours the removed part ran.
CREATE TABLE printer_wear_replacements (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  part text NOT NULL CHECK (length(part) BETWEEN 1 AND 100),
  replaced_at timestamptz NOT NULL,
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  -- Typed: the removed part's hours. Null: from `hours_at` and the replacement before.
  hours_used numeric(10, 1) CHECK (hours_used > 0),
  cost numeric(14, 4) NOT NULL CHECK (cost >= 0),
  notes text CHECK (length(notes) <= 1000),
  origin text NOT NULL CHECK (origin IN ('bambuddy', 'typed')),
  bambuddy_history_id integer,
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  UNIQUE (printer, bambuddy_history_id)
);
CREATE INDEX printer_wear_replacements_part
  ON printer_wear_replacements (printer, part, replaced_at DESC);

ALTER TABLE printer_cost_profiles
  ADD COLUMN actuals_window integer NOT NULL DEFAULT 3
    CHECK (actuals_window BETWEEN 1 AND 20);
ALTER TABLE printer_wear_parts
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual')),
  ADD COLUMN bambuddy_task text CHECK (length(bambuddy_task) BETWEEN 1 AND 100);
ALTER TABLE printer_maintenance_costs
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual'));
```

- `source` is the writer's key for a typed row (as job cost's), and
  `bambuddy:<history id>` for an imported row, so both kinds fill the `NOT NULL` column
  and a retry or a second import writes once.
- Events name their task or part by name, as the cost rows do (`printer_wear_parts` and
  `printer_maintenance_costs` are replaced whole by `PUT`, so they have no stable id). A
  rename in the editor sends `renamed_from`, and `MachineRateStore.replace` moves that
  name's events in the same transaction. Events whose name no row has are kept and
  listed as `orphaned` in the view, so a delete loses no history.
- Both tables join `printers/store.py` `PRINTER_REFERENCES` with a `repoint` hook: a
  merge moves the gone printer's events onto the kept one, except one whose
  `bambuddy_history_id` or `source` the kept printer already has.
- The order of events is by `hours_at`, then by time. An event with no `hours_at` opens
  and closes no gap.

## 5. API (CRUD service only)

All routes are in `api/machine_rates.py`, on the API process. No worker, agent pod or
untrusted code reads or writes these tables (#5295). The Bambuddy read uses the API's
own server-side client through the orders seam; the key never reaches the browser.

- `PUT /printers/{id}/machine-rate`: the body gains `profile.actuals_window`, and each
  wear part and task gains `priced_by`, and `renamed_from`. Each part gains
  `bambuddy_task`.
- `GET /printers/{id}/machine-rate`: the view gains `actuals`: per part and per task, its
  events (the last `actuals_window + 1`, newest first), the actual per hour, the actual
  interval or hours used, `have` and `need`, `partly_typed`, `skipped_gaps`; and
  `events_read_at`, `current_hours` (Bambuddy's counter, linked only) and `orphaned`.
- `GET /printers/{id}/machine-rate/events?kind=task|part&name=&cursor=`: every event,
  paged, newest first.
- `POST /printers/{id}/machine-rate/events`: a typed event (`kind` `task` or `part`), with
  `source` for a retry. A task event on a linked printer is 409
  `maintenance_from_bambuddy` (§3.1).
- `PATCH /printers/{id}/machine-rate/events/{event_id}`: `minutes`, `consumables`,
  `cost`, `hours_at`, `hours_used`, `notes`. An imported event's `performed_at` and
  `bambuddy_history_id` stay as read.
- `DELETE /printers/{id}/machine-rate/events/{event_id}`: a typed event only. An imported
  one is 409 `imported_event` (the next import would bring it back).
- `POST /printers/{id}/machine-rate/import`: reads the log now; answers the view.

The agent gets `machine_rate_events_list` (read), `machine_rate_event_add` and
`machine_rate_event_update` (write: they change the shop's records, reach no printer and
no customer), and `machine_rate_get` shows the actuals.

## 6. The rate, the quote and job cost

- `compute` takes each row's events and `priced_by`. Each `MaintenancePart` and
  `WearPartRate` gains `basis` (`estimate` or `actual`), `estimate_per_hour`,
  `actual_per_hour` (null with none), `have`, `need`, and `why` for an actual not used
  (`too_few`, `no_events`). `per_hour` stays the figure that prices. `NotCounted` gains
  `wear_part` (§3.1). The breakdown gains
  `actuals` (true when any part has `basis` `actual`) and `actuals_window`.
- A quote's `machine_rate_breakdown` (jsonb) records all of it, so a quote shows which
  parts priced from actuals and never moves later. Quotes recorded before read
  `basis` `estimate` by the model's defaults; no migration rewrites them.
- **Job cost (`GET /orders/{id}/costs`):** each `AttemptCost` gains
  `machine_rate_basis`:
  - `quote_actuals` or `quote_estimate`: priced at its quote's `machine_time` line, and
    that quote's breakdown had `actuals` true or false;
  - `printer_actuals` or `printer_estimate`: priced at today's printer rate (already
    marked `estimated` `machine`), and that rate has `actuals` true or false;
  - `price_book`: the book's `machine_rate_per_hour`;
  - null with no machine figure.
  `CostRates` gains `printer_actuals` and `serial_actuals` beside the rate maps, filled by
  `MachineRates.money_rates`. `OrderCosts.machine_rate_bases` lists the distinct values
  of its attempts, so a page can say "machine time partly from actuals". The figures do
  not change; only the label is new. The profit report and the unit-cost history use the
  same `_order_costs`, so they read the same labels.
- The UI (`OrderCosts.tsx`) shows the attempt's machine figure with a short tag:
  "actuals" or "estimate", with a tooltip that names the source ("from the quote" or
  "today's rate for 3DP-31B-598").

## 7. Out of scope

- Actual power (a smart plug's kWh, Bambuddy's `energy_kwh`) and actual depreciation.
- A ScadBuddy print-hour counter for printers only the LAN reaches (§8).
- Writing to Bambuddy (marking a task done, setting `wear_cost_per_hour`).
- A calendar-age cut of the window (events older than a year dropped). N is the window.

## 8. Questions for the owner

1. Should a new wear part or task start at `actual` (prices from actuals once N events
   exist) instead of `estimate`?
2. Is N = 3 the right default window, and should a figure with fewer than N price at
   its estimate (this design) or at the partial actual?
3. One choice per task (this design), or two: the actual interval and the actual cost
   per occurrence chosen apart?
4. Printers only the LAN reaches have no counter. Type the counter at each event (this
   design), or build ScadBuddy's own counter from the LAN watch's RUNNING time in a later
   issue? Prod has no such printer today (§2.2).
5. Should a replaced part be logged in Bambuddy too (a custom "Replace nozzle" task that
   ScadBuddy imports, §3.1), or only in ScadBuddy?

## 9. Settings UI

Where: Settings → Orders → "Machine rate per printer"
(`frontend/src/components/orders/MachineRates.tsx`), in the existing editor of one
printer. It keeps the app's components (`Input`, `Button`, `DataTable`) and tokens; no
new palette or type. The frontend-design skill was loaded for this section.

The nozzle figures in the sketch are made up for the layout; the build plate's come from
§3.3.

**The one strong element:** each wear part and task row ends in a figure pair. The
estimate and the actual per hour stand side by side. The figure that prices is in full
weight; the other is muted. Between them, a two-option toggle (`Estimate` | `Actual`)
sets `priced_by`. Under the actual, a short line says why it is or is not in use. A row
with no actual shows "No events yet" in place of the figure, and the toggle still works.

```
Wear parts                                     Window: last [ 3 ] events
┌──────────────────────────────────────────────────────────────────────────┐
│ Nozzle   cost [35.00]  life [1000] h                                     │
│          Estimate 0.0350/h   ( Estimate | Actual )   Actual 0.0583/h      │
│                                                       3 of 3, from 600 h │
│          Replacements (4) ▸        Log replacement                       │
├──────────────────────────────────────────────────────────────────────────┤
│ Plate    cost [40.00]  life [2000] h                                     │
│          Estimate 0.0200/h   ( Estimate | Actual )   No events yet       │
└──────────────────────────────────────────────────────────────────────────┘
Maintenance tasks   (Mark tasks done in Bambuddy; ScadBuddy reads them.)
┌──────────────────────────────────────────────────────────────────────────┐
│ Clean Build Plate   [10] min  [0.00] consumables   every 25 h (Bambuddy) │
│          Estimate 0.2000/h   ( Estimate | Actual )   Actual 0.0763/h      │
│                                          every 65.5 h, 3 of 3 gaps       │
│          Done (5) ▸                                                       │
└──────────────────────────────────────────────────────────────────────────┘
```

- The figure the toggle does not choose is muted, not hidden: a person sees the gap
  between plan and real use at a glance. With "Actual" chosen and fewer than N events,
  the actual line says "2 of 3: the estimate prices until 3", and the estimate keeps
  full weight.
- "Replacements (4) ▸" and "Done (5) ▸" open the event list in place (newest first:
  date, counter, hours used or gap, cost or minutes and consumables, "from Bambuddy" or
  "typed"). Each imported task event has "Add minutes and consumables". Each typed event
  has Edit and Delete. Opening is the one motion: the list expands under its row.
- "Log replacement" opens a small form: date (today), counter reading (Bambuddy's
  `current_hours`, filled, editable; empty on a LAN-only printer) or hours used, cost
  (the part's typed cost, filled), notes. The button says "Log replacement"; the toast
  says "Replacement logged".
- "Read from Bambuddy" sits beside the window field with "Last read 10 min ago".
- The rate's breakdown (`Breakdown`) adds "actual" or "estimate" after each part, and a
  line "Some parts priced from actuals" when `actuals` is true.
- Errors name the fix: "Bambuddy did not answer. The rate uses the events last read at
  14:02." An empty list says "No replacements logged. Log one when you change this part."
- Phone width: each row stacks its inputs, then the figure pair on its own line, with
  the toggle full width.

## 10. Tests

- `compute` vectors (pure, `backend/tests/orders/test_machine_rate.py`): the ratio of sums; N
  from 1 to 3; fewer than N (`too_few`, estimate prices); a zero or negative gap skipped;
  the first event opens no gap; an event with no minutes uses the typed ones
  (`partly_typed`); a part with typed `hours_used`; rounding adds up to the rate; prod's
  Clean Build Plate numbers (§3.3) as a fixture.
- Store (`requires_postgres`, `backend/tests/orders/test_machine_rate_store.py`): import idempotent by `bambuddy_history_id`; a typed
  retry by `source` writes once; a rename moves events; an orphaned name is listed;
  a merge moves events and skips duplicates; the migration applies on a database that
  holds #4625's rows.
- Bambuddy seam: a recorded `GET /maintenance/items/{id}/history` reply (shape from §2.1,
  with the naive `performed_at` read as UTC); 403, 404, a timeout and a reply that does
  not validate leave the stored events and set no `events_read_at`; the sweep behind its
  patch replays the old history (`tests/fixtures/` histories).
- Routes: every route and refusal in §5; `PUT` with `priced_by` and `actuals_window`;
  the quote records `basis` and `actuals`; an old quote's breakdown reads `estimate`.
- Job cost: each `machine_rate_basis` value; the figures equal those before the change.
- `tests/test_orders_bambuddy_seam.py` still passes (only `orders/bambuddy.py`
  imports the client).
- Frontend (vitest, `MachineRates.test.tsx`, mocks in
  `mocks/features/orders/machineRates.ts`): the pair and its toggle; "k of N"; the event
  list; log a replacement; the job-cost tag in `OrderCosts`. Playwright: log a
  replacement and see the actual change, at desktop and phone width.
- Agent: the three tools' schemas and tiers (`agent/src/tools/orders.ts` tests).

## 11. PR split

Each PR leaves the page usable.

1. **Backend: data and rule.** The migration, `compute` with actuals, the store, the
   typed event routes, `priced_by` and `actuals_window` in `PUT`, the merge hook, and the
   breakdown fields. No Bambuddy read yet.
2. **Backend: Bambuddy import.** The client method, the seam, the view's read, the
   import route, the sweep behind its patch, recordings.
3. **Job cost and agent.** `machine_rate_basis`, `CostRates` actuals maps, the three
   agent tools.
4. **Frontend.** The figure pairs, toggles, event lists, the log form, the breakdown
   labels and the job-cost tag; mocks, vitest, Playwright.

PRs 3 and 4 can run beside each other once 1 is merged. Each PR body says `Refs #5774`;
the last says `Fixes #5774`.
