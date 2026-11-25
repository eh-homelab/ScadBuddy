# Machine rate actuals: logged maintenance and replaced parts feed back into the rate

Status: draft for the owner's approval. Covers #5774, under the orders epic #4248.
Written 2026-11-17. The owner answered the §8 questions on 2026-11-18
([PR #6576 comment](https://github.com/eh-homelab/ScadBuddy/pull/6576#issuecomment-6971188892));
this version applies the answers. Nothing is built before the owner approves this design.

Builds on: the order operations spec (`2026-11-07-order-operations-design.md`, §6, the
derived machine rate of #4625, shipped in #5683 and #5759) and the job-cost spec
(`2026-11-05-job-cost-design.md`). Marks: **(owner)** is a decision the owner made in
§8; **(call)** is the author's choice; the rest is what the issue, §6 and the owner
already said. §2 lists the measured facts. Each one says where and how it was measured.
Code facts cite files and functions on `main` at 71227b2.

## 1. Why

The machine rate's per-hour figures come from inputs typed once (§6.1): each wear part's
cost and life in print-hours, each maintenance task's minutes and consumables, and
Bambuddy's interval for each task. Nothing checks these figures against what really
happens. If a nozzle lasts 600 h and not the 1000 h typed, the rate stays low until a
person types the life again.

This design logs what really happens and computes an actual per-hour figure for each
wear part and each task. Settings shows the estimate and the actual side by side. For
each figure, the person chooses which one prices. The choice is remembered. Each figure
also shows a confidence: how much logged history backs it.

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
  or `hour`). So a printer that only the LAN reaches has no counter today. §3.6 builds
  one from the reports' `gcode_state`.
- **Prod has one physical printer, and it is linked to Bambuddy.** `list_printers` on
  2026-11-17: `3DP-31B-598` (`bambuddy_printer_id` 1) and one Bambuddy virtual printer.
  `machine_rate_get` for it answers `source` `price_book` (rate 0.90555 USD): it has no
  profile yet, so no actual can price it until it has one.

### 2.3 The LAN watch already sees every state change (for §3.6)

Read on `main` at 71227b2.

- **One watch per LAN printer keeps the latest merged report.** The `printer_watch`
  activity (`backend/scadbuddy/printers/watch.py`, module docstring) follows one
  printer's MQTT reports for as long as the connection lasts. It writes a merged report
  at most every `write_every` (1 s, `WatchActivities.__init__`), but a change of
  `gcode_state` (one of `SHOWN`) is written at once. While the connection is up and
  quiet, it touches the row's `seen_at` every `HEARD_EVERY` (10 s), stamped `now` less
  `LINK_PROOF` (`2 * KEEPALIVE`, 20 s; `printers/lan.py` `KEEPALIVE = 10`). A dropped
  connection fails the attempt, and `FOLLOW_RETRY` starts the next one.
- **The watch runs in the print worker and holds no database.** It writes through
  `crud/print.py` `RemoteReports.advance` (`PUT …/print/reports/{printer_id}`) and
  `RemoteReports.touch` (`POST …/touch`), each bounded by `WATCH_CALL_BUDGET` (2 s). A
  write that fails is logged and the next quiet tick or report writes the newer state.
- **The CRUD service writes the row under a lock.** `api/crud_print.py` `put_report` calls
  `printers/reports.py` `PrinterReportStore.advance_in` in one transaction. `advance_in`
  reads the old row `FOR UPDATE` (its `report`, `seen_at` and `running_subtask`), skips a
  write older than the row (`stale`), and writes only while the printer is LAN-linked
  (`lan_host IS NOT NULL`). A resend after a lost answer carries the same `seen_at`.
  `touch_report` calls `PrinterReportStore.touch`, which never moves `seen_at` back
  (`GREATEST`).
- **A report may be a delta.** `reports.py` `advance_print`: a report with no
  `subtask_name` or no `gcode_state` (a delta before a reconnect's `pushall` answer)
  says nothing about the state.
- **ScadBuddy's "running" set is wider than Bambuddy's counter.** `printers/status.py`
  `RUNNING` is `{"PREPARE", "SLICING", "RUNNING", "PAUSE"}`. Bambuddy counts only
  `RUNNING` (§2.2). `status.py` `STALE_AFTER` (90 s) is when a silent printer counts as
  offline.
- **An unlink deletes the report row.** `printers/store.py` `PrinterStore.unlink_lan`
  deletes the `printer_reports` row. A printer merge moves each table in
  `PRINTER_REFERENCES` (`printer_reports` is one) onto the kept printer.

## 3. Decisions

### 3.1 Events come from Bambuddy and from ScadBuddy

- **A task done, on a linked printer:** imported from Bambuddy's log (§2.1). The person
  keeps marking tasks done in Bambuddy, as today. ScadBuddy reads the time and the
  counter. The person may add the real minutes and consumables to an imported event in
  ScadBuddy. An event with none uses the task's typed figures (§3.3).
- **A task done, on a printer Bambuddy does not track:** logged in ScadBuddy. The counter
  reading comes from ScadBuddy's own hour counter (§3.6) on a LAN-linked printer, and is
  typed on a printer with no counter (§3.2).
- **A replaced wear part: logged in ScadBuddy only (owner).** It carries its real cost.
  Bambuddy has no part or cost (§2.1). ScadBuddy does not import a Bambuddy custom task
  (such as "Replace nozzle") as a replacement. A replacement's counter reading comes from
  the printer's counter (§3.2).
- ScadBuddy never writes to Bambuddy. It does not mark a task done there when a person
  logs one in ScadBuddy **(call)**: one place owns each fact. On a linked printer, the
  Settings form says "Mark tasks done in Bambuddy; ScadBuddy reads them" and offers no
  typed task event.

### 3.2 Print-hours: one counter per printer

Each printer has one counter source. ScadBuddy picks it, not the person:

| Printer | Counter source (`hours_source`) |
| --- | --- |
| Linked to Bambuddy (`bambuddy_printer_id` set), LAN-linked or not | `bambuddy`: Bambuddy's counter (§2.2) |
| LAN-linked only | `lan_watch`: ScadBuddy's own counter (§3.6) |
| Neither | `typed`: the person types the reading (no counter exists) |

- **Bambuddy's counter wins on a linked printer (call).** Its imported events already
  carry Bambuddy's `hours_at_maintenance`, and a gap needs both ends on one scale.
  Bambuddy's counter also covers the time before ScadBuddy watched the printer. On a
  printer that is linked and LAN-linked, ScadBuddy's counter still runs. The view shows
  it beside Bambuddy's as a check, but it never prices.
- **Each event records its `hours_source`.** A gap or a derived `hours_used` between two
  events of different sources is not usable. It is skipped and listed in
  `skipped_gaps` with the reason `counter_changed` (§3.3). This happens when the
  printer's source changes: it is linked to or unlinked from Bambuddy, or a printer not
  linked to Bambuddy is LAN-linked or LAN-unlinked.
- **The reading at an event.** An imported event carries its `hours_at_maintenance`. A
  ScadBuddy event (a replacement, or a task on a printer Bambuddy does not track) gets
  its `hours_at` from the printer's counter when it is logged: Bambuddy's `current_hours`
  (a live read), or ScadBuddy's counter (a read of `printer_run_hours`, §3.6, on the
  CRUD service). The person may correct the reading for an event logged late. The
  event keeps the printer's source, because the correction is on the same scale.
- **Manual corrections (call).** Two stay, and both are corrections, not the normal path:
  (1) the reading of one event, as above; (2) a typed `hours_used` on a replacement
  ("this nozzle ran 640 h"), for a part fitted before the counter started. ScadBuddy's
  counter also takes an offset (`lan_hours_offset`, §3.6), as Bambuddy's
  `print_hours_offset` does. No path asks the person to type the counter at each event
  on a printer that has a counter.
- Hours are kept to 0.1 h (`numeric(10, 1)`), as the intervals are. Bambuddy's floats are
  rounded half up to 0.1, as `machine_rate.py` `_hours` does today. A counter reading
  keeps 0 (a new helper, not `_hours`, which answers `None` for an interval that rounds to
  0): a first event at 0.0 h has a known counter.

### 3.3 The rule: per-hour actuals

The rule is a ratio of sums over a window of the last N gaps (so it is weighted by
hours, not a mean of per-gap rates). A short gap then cannot swing the figure.

**A task's actual per hour:**

```
gaps      = the last N usable gaps between two events of the task, newest first
gap_i     = hours_at(event_i) - hours_at(event_i-1)
cost_i    = minutes_i x labour rate / 60 + consumables_i        (event_i's own figures)
actual    = sum(cost_i) / sum(gap_i)
```

- An event with no minutes uses the task's typed minutes; one with no consumables uses
  the typed consumables. The figure is then marked `partly_typed`.
- The first event of a task opens no gap. Its counter before is unknown: Bambuddy's
  counter started when Bambuddy first saw the printer, not when the task was last done.
- A gap is not usable when it is 0 h or less (two events at one counter reading, or a
  counter reset; reason `not_positive`) or when its two events have different
  `hours_source` (reason `counter_changed`, §3.2). It is skipped and named in the
  breakdown (`skipped_gaps`, each with its reason).
- **The window is N usable gaps, not N + 1 events.** `compute` walks back from the
  newest event until it has N usable gaps or runs out of events. A skipped gap does not
  count toward N, so the window then holds more than N + 1 events. "k of N" counts usable
  gaps. A part's window is the same: N replacements that have a usable `hours_used`. A
  derived `hours_used` is not usable when it is 0 h or less, or when the two
  replacements have different `hours_source`: it is skipped and listed in the part's
  `skipped_gaps`, as a task's gap is. A typed `hours_used` is always above 0 (its
  column's check) and always usable.
- The actual also gives an **actual interval** (`sum(gap_i) / count`) and an **actual
  cost per occurrence** (`sum(cost_i) / count`), shown beside the typed ones.

Example, from prod's Clean Build Plate (§2.1), with 10 minutes at a labour rate of 30 and
no consumables (5.00 per occurrence), N = 3: estimate `5.00 / 25 = 0.2000` per hour;
actual `15.00 / (35.0 + 37.9 + 123.7) = 15.00 / 196.6 = 0.0763` per hour; confidence
100 % (3 of 3 gaps, §3.4).

**A wear part's actual per hour:**

```
replacements = the last N replacements of the part that have a usable hours_used, newest first
hours_used_r = hours the removed part ran: hours_at(r) - hours_at(r-1),
               or the hours typed on r
actual       = sum(cost_r) / sum(hours_used_r)
```

- `cost_r` is what the new part cost. In a steady state that equals what the removed part
  cost. **(call)** This keeps one cost per row and needs no pairing of rows. Every
  replacement is logged in ScadBuddy with its cost (§3.1), so a part's actual is never
  `partly_typed`.
- A first replacement with no earlier one and no typed hours has no `hours_used`. It
  adds neither its cost nor hours to any sum. It only marks where the next part's life
  starts: its `hours_at` is the base of the next replacement's `hours_used`.

**Fewer than N (owner):**

- N is a per-printer setting, `actuals_window`, default **3**, from 1 to 20. A person can
  set 1 to use actuals at once.
- With fewer than N usable gaps (task) or replacements (part), the **estimate prices**.
  The breakdown says why (`basis` `estimate`, `actual_why`, `have` k). `actual_why` is
  `no_events` when `have` is 0 (for example a task with one event, so no gap, or a part
  with only its first replacement), and `too_few` when `have` is 1 to N-1. A rate does
  not jump on one odd event.
- Every figure carries a **confidence** (§3.4) that tells the reader how far to trust
  it. The partial actual is still shown, with its confidence, but it does not price.

**Rounding:** each actual per hour is rounded half up to 0.0001 once, as every part is
today (`RATE_PLACES`). The breakdown still adds up to its rate.

### 3.4 Confidence: how far to trust a figure (owner)

The owner asked that each figure show how sure it is, from the events logged so far.

**The rule (call):**

```
confidence = floor(100 x min(have, N) / N)          an integer percent, 0..100
```

- `have` is the count that "k of N" shows: usable gaps for a task, replacements with a
  usable `hours_used` for a part (§3.3). `N` is the printer's `actuals_window`.
- So with N = 3: no events or one task event is 0 %, one gap or replacement is 33 %, two
  are 66 %, three or more are 100 %.
- **100 % and "the actual prices" are the same fact** for a row set to `actual`: the
  actual prices exactly when `have >= N`. Below 100 %, the estimate prices and the
  confidence says how close the actual is to taking over.
- **Rejected: a spread penalty (call).** A factor from the spread of the gaps (for
  example 1 minus the coefficient of variation) would mark the build plate's 35.0, 37.9
  and 123.7 h gaps as about 37 %. But the window holds 1 to 20 gaps, a spread from two or
  three gaps is mostly noise, and a reader cannot check the number from the line. The
  event list (§9) shows each gap, so the reader sees the spread directly.

**What the confidence belongs to.** It measures the logged history behind a row's
actual. It shows in three places:

- On the actual figure, always: "Actual 0.0583/h, 66 % confidence: 2 of 3 replacements
  logged".
- On the priced figure, when the row is set to `actual` and the estimate prices
  (`too_few`): "Estimate 0.0350/h prices, 33 % confidence: 1 of 3 replacements logged".
- A row set to `estimate` prices at a typed figure. Its priced figure carries no
  confidence (the typed figure is the person's own); its actual still shows one.

**The rate's confidence.** The breakdown gains `confidence`: the lowest `confidence` of
the rows set to `actual`, or null when no row is set to `actual`. It names the
least-backed figure that a person chose to trust. A quote and job cost show it (§6).

**Where it lives.** `confidence` is computed in `compute` (`orders/machine_rate.py`)
from the events and N each time. It is not stored on a row, so it never goes stale. A
quote stores it inside its `machine_rate_breakdown`, so the quote keeps the confidence
it was priced with.

### 3.5 Which figure prices: a remembered choice per figure

- Each wear part and each task row gets `priced_by`, `estimate` or `actual`. It is stored
  with the row, shown as a toggle on the row, and kept until a person changes it (the
  owner's rule: every setting is a remembered choice with a toggle).
- **A new row starts at `estimate` (owner).**
- **One choice per task (owner).** A task priced by its actual replaces both parts of its
  estimate: Bambuddy's interval and the typed cost per occurrence. The actual interval
  and the actual cost per occurrence are not chosen apart.
- Power and depreciation have no actuals in this issue (§7).

### 3.6 ScadBuddy's own hour counter, from the LAN watch (owner)

Printers only the LAN reaches get their hours from the printer: ScadBuddy counts the
printer's time in `RUNNING` from the reports its LAN watch already writes (§2.3). It runs
on every LAN-linked printer. It prices only where it is the counter source (§3.2).

**What is counted.** The seconds the printer's merged report says `gcode_state`
`RUNNING`, and only `RUNNING`, as Bambuddy counts (§2.2). `PREPARE`, `SLICING` and
`PAUSE` are not counted, though `status.py` `RUNNING` lists them (§2.3): the counter
uses its own constant, `COUNTED_STATE = "RUNNING"`, in a new module
`printers/run_hours.py`.

**Where it is stored.** A new table, `printer_run_hours` (§4), one row per printer,
owned by the CRUD service (#5295). No worker reads or writes it: the watch's writes
already reach the CRUD service over HTTP (§2.3), so the counter needs no new database
client, no new grant and no new route for the worker.

**How it moves.** The CRUD service advances the counter inside the two writes it already
gets from the watch, in the same transaction:

- `PrinterReportStore.advance_in` (each report write), after the `stale` check and only
  when the report row is written. It locks the counter row `FOR UPDATE` after the report
  row, so writes for one printer are in order.
- `PrinterReportStore.touch` (each quiet tick). Today `touch` is one autocommit `UPDATE`
  with no transaction (`printers/reports.py` `PrinterReportStore.touch`). PR 1 wraps the
  report's `UPDATE` and the counter's `UPDATE` in one transaction, and locks in the same
  order as `advance_in`: the report row first, then the counter row.

Each step is a pure function, `run_hours.accrue(row, t, report)`, where `t` is the
write's or touch's `seen_at` and `report` is the merged report (none for a touch):

```
d = t - row.counted_to
if d < 0:                               # a late write, a clock step back, an earlier touch
    add nothing; keep counted_to; on a report whose gcode_state is not "RUNNING"
    while row.state is "RUNNING": state = its gcode_state (stop counting); nothing else
    (stop here)
if d == 0:                              # a resend (same seen_at)
    add nothing; keep counted_to
elif row.state != "RUNNING":
    add nothing
elif d <= GAP_LIMIT:                    # GAP_LIMIT = status.STALE_AFTER (90 s)
    run_seconds += d
elif report shows gcode_state "RUNNING" and the same subtask_name as row.subtask:
    run_seconds += d                    # the same print went on while unseen
elif report is None:                    # a touch after a long silence
    change nothing (counted_to stays; the next report decides the gap)
else:
    unseen_seconds += d                 # not counted; the view shows it
counted_to = t                          # d > 0; not for the "change nothing" case
on a report: state, subtask = its gcode_state and subtask_name, each only when present
```

- **Gaps.** While the link is up, the row is written at least every
  `reports.REFRESH_EVERY` (30 s) while reports arrive (a report with nothing new is
  skipped only within that of the last written report, `report_seen_at`, which a touch
  does not move, and of `counted_to`, #7389), and touched every `HEARD_EVERY` (10 s)
  when the link is quiet (§2.3), so a gap over 90 s means the watch or the link was
  down. If the same print still runs after the
  gap, the printer printed through it (a printer does not stop when ScadBuddy's link
  drops), so the gap counts. If the state or the print changed, ScadBuddy cannot know
  when, so it counts none of the gap and adds it to `unseen_seconds`. **(call)** The
  counter is then low, never high, and the view says by how much.
- **Restarts of the watch** (a new attempt after `FOLLOW_RETRY`, a worker restart, a
  deploy) are gaps like any other: the row keeps `counted_to`, `state` and `subtask`, and
  the first report after the restart applies the rule.
- **A missed report** (a write that failed, §2.3) is a longer gap between two writes.
  The next written report carries the newer state. A `RUNNING` to `PAUSE` and back that
  falls wholly inside a missed stretch of 90 s or less is counted as running: at most
  90 s per missed stretch.
- **A delta report** without `gcode_state` (§2.3) keeps the row's state.
- **A resend** of a report whose answer was lost carries the same `seen_at`, so `d` is 0
  and nothing is counted twice. A `stale` write never reaches the counter.
- **Clocks.** `seen_at` comes from the print worker's clock.
  - A **forward step** is a gap and follows the gap rule. A step of 90 s or less while
    `RUNNING` counts the step. A longer one counts the step only when the same print still
    runs after it. Otherwise it goes to `unseen_seconds`.
  - A **backward step** (a watch host whose clock ran ahead, then stepped back) makes a
    report earlier than `counted_to`. It reaches the counter only when the report row's
    `seen_at` is past `FUTURE_TOLERANCE` (`reports.py` `stale`). A **late write** (a print
    worker's request still running on the API after the worker gave its answer up) gets
    there the same way while the stored `seen_at` is ahead, with no step at all, and so
    does a report after a merge moved a counter under a printer with older reports. Their
    times cannot tell them apart: a write 35 s late on a host 45 s ahead looks like a
    clock corrected by 35 s. Moving `counted_to` back on a late write would count the
    stretch to it twice, so no write moves it back. **(call: low, never high)** Such a
    report counts nothing and may only stop the counting: a state other than `RUNNING`
    replaces a counted one. It never starts it. A clock that ran ahead so loses its lead
    once: counting goes on when its reports pass `counted_to` again.
  - A **touch** stamped before `counted_to` never moves it back. A touch is stamped
    `watch.LINK_PROOF` (20 s) before it is sent, so after a report this is the usual case,
    and moving back would count that stretch twice.
- **Unlink and relink.** `PrinterStore.unlink_lan` keeps the counter row (hours are a
  fact about the printer) but sets its `state` to null in the same transaction, so no gap
  spans an unlink. A relink counts on from the kept total.
- **Merge.** `printer_run_hours` joins `PRINTER_REFERENCES` with a repoint hook. The kept
  printer's row stays. The gone printer's row moves only when the kept printer has none.
- **First report.** The first written report of a printer inserts the row with
  `counted_to` = its `seen_at` and `run_seconds` 0.

**The reading.** `hours = round_half_up(run_seconds / 3600 + lan_hours_offset, 0.1)`.
`lan_hours_offset` is a typed number on the printer's cost profile (default 0): the
hours the printer ran before ScadBuddy watched it, as Bambuddy's `print_hours_offset`
(§2.2). It is a correction, set once. An event reads the counter on the CRUD service
when it is logged (§3.2), with a plain `SELECT` of the row.

**What it does not do.** It does not read Bambuddy, it does not replace Bambuddy's
counter on a linked printer, and it does not try to rebuild the hours before its first
report. A printer that was never LAN-linked has no row and no `lan_watch` hours.

### 3.7 Imports

- A linked printer's log is read through the orders seam (`orders/integrations.py`,
  `orders/bambuddy.py`), a new client method `maintenance_history(item_id)` (scope
  `Read Status`, operation `maintenance.history`). The prod key reads it (§2.1). The
  seam's `MaintenanceTask` gains two fields from the same per-printer read: `item_id`
  (Bambuddy's `id` of the task on the printer, which the history route takes) and
  `current_hours` (the counter, which the view and the replacement form show).
- Only maintenance tasks are imported. Replacements are logged in ScadBuddy only (§3.1).
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

Two new migrations. Each adds new tables and columns only; neither edits a merged file.
Every table is the CRUD service's own (#5295).

The counter, `<yyyymmdd>T<hhmm>Z_printer_run_hours.sql` (PR 1, §11):

```sql
-- ScadBuddy's own print-hour counter for a LAN-linked printer (§3.6), advanced by the
-- CRUD service inside the LAN watch's report writes and touches.
CREATE TABLE printer_run_hours (
  printer uuid PRIMARY KEY REFERENCES printers (id) ON DELETE CASCADE,
  run_seconds numeric(16, 6) NOT NULL DEFAULT 0 CHECK (run_seconds >= 0),
  -- Gaps over the limit that ended in another state or print: not counted.
  unseen_seconds numeric(16, 6) NOT NULL DEFAULT 0 CHECK (unseen_seconds >= 0),
  -- Counted up to here. Never moves back: a write stamped before it counts nothing.
  counted_to timestamptz NOT NULL,
  -- The last report's gcode_state and subtask_name. Null state: nothing to count
  -- (also set by an unlink, so no gap spans it).
  state text,
  subtask text,
  started_at timestamptz NOT NULL DEFAULT now()
);
```

The actuals, `<yyyymmdd>T<hhmm>Z_machine_rate_actuals.sql` (PR 2):

```sql
-- A maintenance task done on a printer: imported from Bambuddy's log, or logged in
-- ScadBuddy (a printer Bambuddy does not track).
CREATE TABLE printer_maintenance_events (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  task text NOT NULL CHECK (length(task) BETWEEN 1 AND 100),
  performed_at timestamptz NOT NULL,
  -- The printer's print-hour counter when it was done. Null: not known (no gap).
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  -- Which counter `hours_at` is on (§3.2). A gap across two sources is not usable.
  hours_source text CHECK (hours_source IN ('bambuddy', 'lan_watch', 'typed')),
  -- Null: the task's typed figure prices this event (`partly_typed`).
  minutes numeric(8, 1) CHECK (minutes >= 0),
  consumables numeric(14, 4) CHECK (consumables >= 0),
  notes text CHECK (length(notes) <= 1000),
  origin text NOT NULL CHECK (origin IN ('bambuddy', 'scadbuddy')),
  bambuddy_history_id integer,
  -- A ScadBuddy event's writer key, so a retry writes once (as job cost's `source`).
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  UNIQUE (printer, bambuddy_history_id),
  CHECK ((hours_at IS NULL) = (hours_source IS NULL))
);
CREATE INDEX printer_maintenance_events_task
  ON printer_maintenance_events (printer, task, hours_at DESC);

-- A wear part replaced, logged in ScadBuddy only (§3.1): its real cost, and the hours
-- the removed part ran.
CREATE TABLE printer_wear_replacements (
  id uuid PRIMARY KEY,
  printer uuid NOT NULL REFERENCES printers (id) ON DELETE CASCADE,
  part text NOT NULL CHECK (length(part) BETWEEN 1 AND 100),
  replaced_at timestamptz NOT NULL,
  hours_at numeric(10, 1) CHECK (hours_at >= 0),
  hours_source text CHECK (hours_source IN ('bambuddy', 'lan_watch', 'typed')),
  -- Typed (a correction): the removed part's hours. Null: from `hours_at` and the
  -- replacement before.
  hours_used numeric(10, 1) CHECK (hours_used > 0),
  cost numeric(14, 4) NOT NULL CHECK (cost >= 0),
  notes text CHECK (length(notes) <= 1000),
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  created_by text NOT NULL,
  UNIQUE (printer, source),
  CHECK ((hours_at IS NULL) = (hours_source IS NULL))
);
CREATE INDEX printer_wear_replacements_part
  ON printer_wear_replacements (printer, part, hours_at DESC);

ALTER TABLE printer_cost_profiles
  ADD COLUMN actuals_window integer NOT NULL DEFAULT 3
    CHECK (actuals_window BETWEEN 1 AND 20),
  ADD COLUMN lan_hours_offset numeric(10, 1) NOT NULL DEFAULT 0
    CHECK (lan_hours_offset >= 0);
ALTER TABLE printer_wear_parts
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual'));
ALTER TABLE printer_maintenance_costs
  ADD COLUMN priced_by text NOT NULL DEFAULT 'estimate'
    CHECK (priced_by IN ('estimate', 'actual'));
```

- `source` is the writer's key for a ScadBuddy row (as job cost's), and
  `bambuddy:<history id>` for an imported task event, so both kinds fill the `NOT NULL`
  column and a retry or a second import writes once.
- `lan_hours_offset` sits on the cost profile, not on `printer_run_hours`, so a person
  can set it before the watch writes the first report. A printer with no profile reads
  offset 0.
- Events name their task or part by name, as the cost rows do (`printer_wear_parts` and
  `printer_maintenance_costs` are replaced whole by `PUT`, so they have no stable id). A
  rename in the editor sends `renamed_from`, and `MachineRateStore.replace` moves that
  name's events in the same transaction. Events whose name no row has are kept and
  listed as `orphaned` in the view, so a delete loses no history.
- The three tables join `printers/store.py` `PRINTER_REFERENCES` with a `repoint` hook: a
  merge moves the gone printer's events onto the kept one, except one whose
  `bambuddy_history_id` or `source` the kept printer already has; the counter row moves
  as §3.6 says.
- The order of events is by `hours_at`, then by time. An event with no `hours_at` opens
  and closes no gap.

## 5. API (CRUD service only)

All routes are in `api/machine_rates.py`, on the API process. No worker, agent pod or
untrusted code reads or writes these tables (#5295). The Bambuddy read uses the API's
own server-side client through the orders seam; the key never reaches the browser. The
counter moves inside the existing `api/crud_print.py` `put_report` and `touch_report`
(§3.6); the watch's request and answer do not change.

- `PUT /printers/{id}/machine-rate`: the body gains `profile.actuals_window` and
  `profile.lan_hours_offset`, and each wear part and task gains `priced_by` and
  `renamed_from`.
- `GET /printers/{id}/machine-rate`: the view gains `actuals`: per part and per task, its
  events (those in the window of §3.3, N usable gaps with the skipped ones between them,
  newest first, so the list and "k of N" always agree with `compute`), the actual per
  hour, the actual interval or hours used, `have`, `need`, `confidence`, `partly_typed`
  and `skipped_gaps` (each with its reason); and `events_read_at` and `orphaned`. It also
  gains `counter`:
  - `source`: `bambuddy`, `lan_watch` or `typed` (§3.2);
  - `hours`: the reading that events get now (null when the source is `typed`, or when
    Bambuddy does not answer);
  - `lan_watch`: ScadBuddy's counter when the printer has one, else null:
    `{hours, run_hours, offset_hours, unseen_hours, counted_to, started_at}`. On a
    linked printer it is the check beside Bambuddy's (§3.2).
- `GET /printers/{id}/machine-rate/events?kind=task|part&name=&cursor=`: every event,
  paged, newest first.
- `POST /printers/{id}/machine-rate/events`: a ScadBuddy event (`kind` `task` or
  `part`), with `source` for a retry. `hours_at` may be left out: the route fills it from
  the printer's counter (§3.2) and records `hours_source`. A `hours_at` in the body is a
  correction on the same counter. Refusals:
  - a task event on a linked printer: 409 `maintenance_from_bambuddy` (§3.1);
  - no `hours_at` on a linked printer while Bambuddy does not answer: 503
    `counter_unavailable` (the form says to try again or enter the reading);
  - no `hours_at` and no `hours_used` on a printer with no counter: 422
    `counter_reading_required`.
- `PATCH /printers/{id}/machine-rate/events/{event_id}`: `minutes`, `consumables`,
  `cost`, `hours_at`, `hours_used`, `notes`. An imported event's `performed_at`,
  `hours_source` and `bambuddy_history_id` stay as read.
- `DELETE /printers/{id}/machine-rate/events/{event_id}`: a ScadBuddy event only. An
  imported one is 409 `imported_event` (the next import would bring it back).
- `POST /printers/{id}/machine-rate/import`: reads the log now; answers the view.

The agent gets `machine_rate_events_list` (read), `machine_rate_event_add` and
`machine_rate_event_update` (write: they change the shop's records, reach no printer and
no customer), and `machine_rate_get` shows the actuals, the confidences and the counter.

## 6. The rate, the quote and job cost

- `compute` takes each row's events, `priced_by` and N. Each `MaintenancePart` and
  `WearPartRate` gains `basis` (`estimate` or `actual`), `estimate_per_hour`,
  `actual_per_hour` (null with none), `have`, `need`, `confidence` (an integer 0 to 100,
  §3.4), `partly_typed`, and a new field
  `actual_why: Literal["too_few", "no_events"] | None` for an actual not used. It is apart
  from today's `MaintenancePart.why` (`NotCounted`, `orders/models.py`), which keeps its
  one meaning, "listed and not counted". A task that is not counted and has too few
  events carries both reasons. `WearPartRate` has no `why` and gains only `actual_why`.
  `per_hour` stays the figure that prices. The breakdown gains `actuals` (true when any
  part has `basis` `actual`), `actuals_window` and `confidence` (§3.4: the lowest of the
  rows set to `actual`, or null).
- A quote's `machine_rate_breakdown` (jsonb) records all of it, so a quote shows which
  parts priced from actuals, with what confidence, and never moves later. Quotes recorded
  before read `basis` `estimate` and `confidence` null by the model's defaults; no
  migration rewrites them.
- **Job cost (`GET /orders/{id}/costs`):** each `AttemptCost` gains
  `machine_rate_basis` and `machine_rate_confidence`:
  - `quote_actuals` or `quote_estimate`: priced at its quote's `machine_time` line, and
    that quote's breakdown had `actuals` true or false; the confidence is that
    breakdown's;
  - `printer_actuals` or `printer_estimate`: priced at today's printer rate (already
    marked `estimated` `machine`), and that rate has `actuals` true or false; the
    confidence is that rate's;
  - `price_book`: the book's `machine_rate_per_hour`; confidence null;
  - null with no machine figure.
  `CostRates` gains `printer_actuals`, `serial_actuals`, `printer_confidence` and
  `serial_confidence` beside the rate maps, filled by `MachineRates.money_rates`.
  `OrderCosts.machine_rate_bases` lists the distinct values of its attempts, so a page
  can say "machine time partly from actuals". The figures do not change; only the labels
  are new. The profit report and the unit-cost history use the same `_order_costs`, so
  they read the same labels.
- The UI (`OrderCosts.tsx`) shows the attempt's machine figure with a short tag:
  "actuals" or "estimate", and the confidence when there is one ("estimate, 33 %
  confidence"). A tooltip names the source ("from the quote" or "today's rate for
  3DP-31B-598") and the least-backed part ("Nozzle: 1 of 3 replacements logged"). The
  quote view's machine line shows the same tag.

## 7. Out of scope

- Actual power (a smart plug's kWh, Bambuddy's `energy_kwh`) and actual depreciation.
- Writing to Bambuddy (marking a task done, setting `wear_cost_per_hour`).
- Importing replacements from Bambuddy (the owner chose ScadBuddy only, §8).
- A calendar-age cut of the window (events older than a year dropped). N is the window.
- Rebuilding a LAN printer's hours from before its first watched report. The offset
  (§3.6) is the correction.

## 8. Decisions (owner, 2026-11-18)

The owner answered the draft's five questions in a
[PR #6576 comment](https://github.com/eh-homelab/ScadBuddy/pull/6576#issuecomment-6971188892).

1. **A new wear part or task starts at `estimate`.** §3.5, §4 (`priced_by` default).
2. **N = 3, a setting (`actuals_window`). Before N, the estimate prices, and each figure
   shows a confidence % from the events logged so far.** §3.3, §3.4 (the rule:
   `floor(100 x min(have, N) / N)`), §5, §6, §9.
3. **One choice per task.** The actual interval and the actual cost per occurrence are
   not chosen apart. §3.5.
4. **Printers only the LAN reaches: hours are pulled from the printer.** ScadBuddy keeps
   its own counter from the LAN watch's `RUNNING` time, in this design and its PRs, not
   typed and not a later issue. §2.3, §3.2, §3.6, §4, §11 (PR 1). Bambuddy's counter
   stays the source for a linked printer (§3.2).
5. **Replacements are logged in ScadBuddy only.** No Bambuddy custom task is imported as a
   replacement. §3.1, §3.7, §4.

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
sets `priced_by`. Under the actual, a short line gives its confidence and says why it is
or is not in use. A row with no actual shows "No events yet, 0 % confidence" in place of
the figure, and the toggle still works.

```
Wear parts                                     Window: last [ 3 ] events
┌──────────────────────────────────────────────────────────────────────────┐
│ Nozzle   cost [35.00]  life [1000] h                                     │
│          Estimate 0.0350/h   ( Estimate | Actual )   Actual 0.0583/h      │
│                                  100 % confidence: 3 of 3, from 600 h    │
│          Replacements (4) ▸        Log replacement                       │
├──────────────────────────────────────────────────────────────────────────┤
│ Plate    cost [40.00]  life [2000] h                                     │
│          Estimate 0.0200/h   ( Estimate | Actual )   No events yet       │
│                                                       0 % confidence     │
└──────────────────────────────────────────────────────────────────────────┘
Maintenance tasks   (Mark tasks done in Bambuddy; ScadBuddy reads them.)
┌──────────────────────────────────────────────────────────────────────────┐
│ Clean Build Plate   [10] min  [0.00] consumables   every 25 h (Bambuddy) │
│          Estimate 0.2000/h   ( Estimate | Actual )   Actual 0.0763/h      │
│                     100 % confidence: every 65.5 h, 3 of 3 gaps          │
│          Done (5) ▸                                                       │
└──────────────────────────────────────────────────────────────────────────┘
Print-hours   495.3 h from Bambuddy        ScadBuddy's count: 12.4 h (check)
```

- The figure the toggle does not choose is muted, not hidden: a person sees the gap
  between plan and real use at a glance. With "Actual" chosen and fewer than N events,
  the estimate keeps full weight and its line says "Estimate prices, 33 % confidence:
  1 of 3 replacements logged" (for a task: "1 of 3 gaps logged"). The actual beside it
  is muted, with the same confidence.
- The confidence is text, not a colour or a bar alone, so it reads the same in both
  themes and to a screen reader.
- "Replacements (4) ▸" and "Done (5) ▸" open the event list in place (newest first:
  date, counter and its source ("Bambuddy", "ScadBuddy count" or "typed"), hours used or
  gap, cost or minutes and consumables, "from Bambuddy" or "logged here"). A skipped gap
  shows its reason ("counter changed", "no hours between"). Each imported task event has
  "Add minutes and consumables". Each ScadBuddy event has Edit and Delete. Opening is the
  one motion: the list expands under its row.
- "Log replacement" opens a small form: date (today), counter reading (filled from the
  printer's counter, §3.2, with its source named under it: "from Bambuddy" or "from
  ScadBuddy's count"; editable for a replacement logged late), "hours used instead" (a
  correction for a part fitted before the counter, folded away by default), cost (the
  part's typed cost, filled), notes. Only a printer with no counter shows the reading
  empty and required. The button says "Log replacement"; the toast says "Replacement
  logged".
- **The print-hours line** sits under the tasks. On a linked printer: "495.3 h from
  Bambuddy", and, when the printer is also LAN-linked, ScadBuddy's count muted beside it
  as a check. On a LAN-only printer: "612.4 h, counted by ScadBuddy since 2026-11-20",
  then "Offset [0.0] h" (the hours before ScadBuddy watched it) and, when
  `unseen_hours` is above 0, "3.1 h not seen (the printer was offline and its state
  changed)". On a printer with no counter: "No counter: enter the reading when you log
  an event."
- "Read from Bambuddy" sits beside the window field with "Last read 10 min ago".
- The rate's breakdown (`Breakdown`) adds "actual" or "estimate" and the confidence after
  each part, and a line "Some parts priced from actuals; lowest confidence 66 %" when
  `actuals` is true.
- Errors name the fix: "Bambuddy did not answer. The rate uses the events last read at
  14:02." "Bambuddy did not answer, so there is no counter reading. Try again, or enter
  the reading." An empty list says "No replacements logged. Log one when you change this
  part."
- Phone width: each row stacks its inputs, then the figure pair on its own line, with
  the toggle full width, then the confidence line.

## 10. Tests

- `compute` vectors (pure, `backend/tests/orders/test_machine_rate.py`): the ratio of sums; N
  from 1 to 3; fewer than N (`actual_why` `too_few`, estimate prices); a zero or negative
  gap skipped (`not_positive`) and the window then reaching one event further back; a gap
  across two `hours_source` values skipped (`counter_changed`), for a task and for a
  part's derived `hours_used`; a task not counted with too few events (`why` and
  `actual_why` both set); the first event opens no gap; an event with no minutes uses the
  typed ones (`partly_typed`); a part with typed `hours_used`; a part's derived
  `hours_used` of 0 h or less skipped and listed in `skipped_gaps`; rounding adds up to
  the rate; prod's Clean Build Plate numbers (§3.3) as a fixture, at 100 % confidence.
- Confidence vectors (same file): with N = 3, `have` 0, 1, 2, 3 and 5 give 0, 33, 66,
  100 and 100; with N = 1, one gap gives 100; a task with one event (no gap) gives 0;
  skipped gaps do not raise it; a row at 100 % and set to `actual` has `basis` `actual`,
  and one below 100 % has `basis` `estimate`; the rate's `confidence` is the lowest of the
  rows set to `actual` and null when none is; a row set to `estimate` does not lower it.
- Counter, pure (`backend/tests/printers/test_run_hours.py`, `run_hours.accrue`): a gap
  of 90 s or less in `RUNNING` counts; `PAUSE`, `PREPARE`, `SLICING`, `IDLE` and
  `FINISH` count nothing; a gap over 90 s with the same print still `RUNNING` counts
  (a watch restart); one that ends in another state or print counts nothing and adds to
  `unseen_seconds`; a touch over 90 s changes nothing and the next report decides; a
  resend at the same `seen_at` adds nothing; a touch stamped before `counted_to` adds
  nothing; a report before `counted_to` (late, or after a clock step back) never moves it
  back, counts nothing and may stop the counting but never start it; a delta report without `gcode_state` keeps the state; a replay of the
  recorded H2C reports (`tests/printers/fixtures/`) in order gives the expected seconds.
- Counter, store (`requires_postgres`, a new `backend/tests/printers/test_run_hours_store.py`): `advance_in` creates the row on the first report and
  advances it under the lock; a `stale` write and a write for a printer not LAN-linked
  leave it unchanged; `touch` advances it; `unlink_lan` keeps the total and clears
  `state`, and a relink counts on; a merge moves the row only when the kept printer has
  none; the reading adds `lan_hours_offset` and rounds half up to 0.1.
- Counter, routes (`backend/tests/crud/test_crud_printers.py`): `put_report` and
  `touch_report` move the counter and answer as before;
  the print worker's `RemoteReports` is unchanged (no new worker route, no worker
  database access).
- Store (`requires_postgres`, `backend/tests/orders/test_machine_rate_store.py`): import idempotent by `bambuddy_history_id`; a ScadBuddy
  retry by `source` writes once; a rename moves events; an orphaned name is listed;
  a merge moves events and skips duplicates; the migrations apply on a database that
  holds #4625's rows.
- Bambuddy seam: a recorded `GET /maintenance/items/{id}/history` reply (shape from §2.1,
  with the naive `performed_at` read as UTC); 403, 404, a timeout and a reply that does
  not validate leave the stored events and set no `events_read_at`; the sweep behind its
  patch replays the old history (`tests/fixtures/` histories).
- Routes: every route and refusal in §5; `POST` events with no `hours_at` fills it from
  each counter source (`bambuddy` live, `lan_watch` from `printer_run_hours`) and records
  `hours_source`; `counter_unavailable` and `counter_reading_required`; `PUT` with
  `priced_by`, `actuals_window` and `lan_hours_offset`; the view's `counter` block for a
  linked, a LAN-only, a linked and LAN-linked, and a counterless printer; the quote
  records `basis`, `actuals` and `confidence`; an old quote's breakdown reads `estimate`
  and confidence null.
- Job cost: each `machine_rate_basis` value with its `machine_rate_confidence`; the
  figures equal those before the change.
- `tests/test_orders_bambuddy_seam.py` still passes (only `orders/bambuddy.py`
  imports the client).
- Frontend (vitest, `MachineRates.test.tsx`, mocks in
  `mocks/features/orders/machineRates.ts`): the pair and its toggle; "k of N" with its
  confidence text, for a row priced by its actual and for one where the estimate prices
  below 100 %; the event list with counter sources and skipped reasons; log a
  replacement with the reading filled from each source; the print-hours line and its
  offset; the job-cost tag with confidence in `OrderCosts`. Playwright: log a
  replacement and see the actual and its confidence change, at desktop and phone width.
- Agent: the three tools' schemas and tiers (`agent/src/tools/orders.ts` tests), and
  `machine_rate_get` showing `confidence` and `counter`.

## 11. PR split

Each PR leaves the page usable.

1. **Backend: ScadBuddy's hour counter (§3.6).** The `printer_run_hours` migration,
   `printers/run_hours.py` (`accrue`, `COUNTED_STATE`, the reading), the calls in
   `PrinterReportStore.advance_in` and `touch`, `unlink_lan`'s reset of `state`, the merge
   hook, and the counter tests. It changes no API answer. It goes first so the counter
   gathers hours while the rest is built.
2. **Backend: data and rule.** The actuals migration (with `lan_hours_offset`),
   `compute` with actuals and confidence, the store, the event routes with the counter
   fill (`lan_watch`, `typed`, and `bambuddy` from the live `current_hours`: the seam's
   `MaintenanceTask` gains `item_id` and `current_hours` here, §3.7), `priced_by`, `actuals_window` and
   `lan_hours_offset` in `PUT`, the view's `counter` block, the merge hooks, and the
   breakdown fields. No Bambuddy log import yet.
3. **Backend: Bambuddy import.** The client method `maintenance_history`, the view's read, the
   import route, the sweep behind its patch, recordings.
4. **Job cost and agent.** `machine_rate_basis`, `machine_rate_confidence`, the
   `CostRates` actuals and confidence maps, the three agent tools.
5. **Frontend.** The figure pairs, toggles and confidence lines, event lists, the log
   form, the print-hours line, the breakdown labels and the job-cost tag; mocks, vitest,
   Playwright.

PR 1 can start at once. PRs 4 and 5 can run beside each other once 2 is merged. Each PR
body says `Refs #5774`; the last says `Fixes #5774`.
