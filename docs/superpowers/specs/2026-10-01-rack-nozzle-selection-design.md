# Rack nozzle selection (#836)

Status: the ranking and the Advanced controls were agreed with the owner in
conversation on 2026-10-01. This written spec is not yet approved; its review
gates the implementation plan.
Part of epic #84.

## 1. What this changes

An H2C's rack-side extruder picks its hotend from a six-position rack. Today
ScadBuddy leaves that pick to Bambuddy, which takes the first eligible position,
preferring one that last held the group's color. That ignores the filament's
material and spreads no wear.

ScadBuddy now ranks the rack itself and sends its pick on the queue item. Simple
mode shows the pick and the reason for it. Advanced mode lets the user change the
ranking algorithm or pick a position by hand.

This supersedes one decision of the spool-first-print work: its spec (§6,
`2026-09-27-spool-first-print-design.md`) and plan (`2026-09-27-spool-first-print.md`)
say ScadBuddy sends no `nozzle_rack_choice`, and
`backend/tests/api/test_print_run_choices.py` asserts
`sent.get("nozzle_rack_choice") is None`. That test changes with this work: it
keeps asserting no choice when the rack is unreadable, and a new case asserts the
ranked choice otherwise.

Out of scope:

- the non-rack extruder (one fixed hotend, nothing to choose);
- the slice itself (#840 already decides which side prints);
- full per-print telemetry (#912; this spec takes only the usage counter it
  needs);
- prints dispatched through Bambuddy's `run` (pipeline) path. Like
  `filament_overrides`, `nozzle_rack_choice` exists only on `QueueItemCreate`
  (see `dispatch.py`'s module docstring), so those prints keep Bambuddy's own
  pick. Covering them needs a rack field on the pipeline request, which
  Bambuddy does not have.

## 2. What the printer and Bambuddy give us (measured 2026-10-01)

**`GET /printers/{id}/status` → `nozzle_rack[]`.** Ids `0` and `1` are the two
carriage hotends. Ids `16`–`21` are rack positions 1–6 (position = id − 15). The
firmware omits a rack id while that hotend is on the carriage; Bambuddy's
`_rack_by_position` recovers it when exactly one is missing. Per entry:

| Field | Value on this printer | Usable? |
|---|---|---|
| `nozzle_diameter` | `"0.2"`, `"0.4"` | Yes |
| `nozzle_type` | `HS00`, `HS01`, `HH01` | Second letter is flow (`S` Standard, `H` High Flow). The rest is unverified; see §8 |
| `wear` | `128` on every populated slot | **No.** A constant, not a measurement |
| `filament_color` | Last color loaded, `RRGGBBAA` | Yes |
| `serial_number` | Per hotend | Yes; stored, never logged (§7) |

**`GET /library/files/{sliced_id}/filament-requirements`** gives each filament a
`group_id` and a `group` object, `{on_rack, nozzle_diameter, volume_type,
filament_color}`, read from the sliced 3MF (`extract_rack_plan_from_3mf`).

**`POST /queue/` → `nozzle_rack_choice`**: `{"<group_id>": <position 1–6>}`.
The keys are **filament group ids**, not extruder indexes. ScadBuddy's
`QueueItemCreate` comment says otherwise and is corrected by this change.
Bambuddy re-checks the pick against the live rack at dispatch
(`resolve_rack_plan_mapping`). A sent pick that no longer fits fails the item
with "Nozzle rack pick no longer fits the printer". Groups left out are
auto-assigned as today.

In this spec, **a sent pick** is any entry ScadBuddy puts in
`nozzle_rack_choice`, whether the ranking chose it or the user did. **A manual
pick** is one the user chose in Advanced mode.

## 3. The ranking

For each `on_rack` group, the eligible positions are those holding a nozzle of
the group's diameter and flow type. This is the same test as Bambuddy's
`_rack_slot_is_eligible` (Bambuddy `backend/app/services/bambu_mqtt.py`, read
on the deployed image 2026-10-01), and `rack.py` mirrors it exactly:

- **Diameter** is compared as `round(float(x), 2)`, never as a string: the rack
  reports `"0.2"` and `filament-requirements` reports `"0.20"` for the same
  nozzle (§2, §8), so a string compare would make every position ineligible and
  the feature would silently do nothing. A diameter that does not parse makes
  that position ineligible.
- **Flow**: the group's `volume_type` is a name (`"Standard"`, `"High Flow"`)
  and the slot's `nozzle_type` is a code. The group wants High Flow when
  `volume_type.strip().lower()` starts with `"high flow"`; the slot is High Flow
  when its code starts with `HH`. They must agree, and are compared only when
  both are present, so a missing code or name does not rule a position out.

Groups are allocated one at a time, the group with the fewest eligible positions
first, then by `group_id`. Positions already picked for an earlier group are
excluded. The order is fixed so the same plate on the same rack always gets the
same picks, and so a group with one usable position is not starved by a group
that had several. Among the eligible positions:

1. **Material safe for the filament.** An abrasive filament needs a hardened
   nozzle. A filament is abrasive when either:
   - its `filament-requirements` `type`, split on `-` and spaces, has a `CF` or
     `GF` token (case-insensitive). Measured 2026-10-01 on the deployed slicer's
     Bambu profiles: fiber filaments are always typed with a suffix, `PLA-CF`,
     `PETG-CF`, `PA6-CF`, `PA-GF`, `ABS-GF`, `PPA-GF` and so on; or
   - the inventory spool ScadBuddy assigned to it has `Glow` in its `subtype` or
     `material`. Glow cannot be read from `type`: `Bambu PLA Glow @base`
     inherits `fdm_filament_pla`, so its `type` is plain `PLA`. A group whose
     spool is unknown (no inventory spool assigned) is judged by `type` alone,
     and the dialog says Glow could not be checked.

   A group is abrasive when **any** of its filaments is: several filaments can
   share one hotend, and taking only the first (as the group's color does) could
   send a CF filament through brass with no warning. A
   nozzle's material comes from its `nozzle_type` through a table (§8). A code not
   in the table counts as not hardened. For a non-abrasive filament the rule
   prefers a non-hardened nozzle, keeping hardened ones for filaments that need
   them. If no safe nozzle exists for an abrasive filament, the pick still names
   the best remaining position and carries the warning in §5.
2. **Already holds this color.** The position's `filament_color` matches the
   group's color, ignoring alpha. This saves a purge. It is Bambuddy's own
   preference, kept.
3. **The algorithm key** (§4).
4. **Lowest position**, as the final tiebreak, matching Bambuddy.

## 4. Algorithms

| Algorithm | Key | Default |
|---|---|---|
| Least used | Fewest print seconds recorded on that hotend's serial | Yes |
| Oldest first | Earliest `first_seen_at` for the serial | |
| Newest first | Latest `first_seen_at` | |
| Let Bambuddy pick | Send no choice; Bambuddy's color-then-lowest rule applies | |

The algorithm is remembered per printer, like `printer_bed_types`
(`printer_rack_algorithms: dict[str, Algorithm]` in stored settings). It is shown
and cleared in Settings' remembered choices.

**Usage counter.** Three new tables. Use is counted per Bambuddy archive, which
is one physical print: a queue item with `quantity` N produces N archives, and
each one counts. Every write is `ON CONFLICT DO NOTHING`, so a settle seen twice
(two replicas, a restart) cannot count a print twice.

`rack_nozzle_seen`, primary key `(printer_id, serial)`:

| Column | Type | Notes |
|---|---|---|
| `printer_id` | int | |
| `serial` | text | |
| `first_seen_at` | timestamptz | |

`rack_nozzle_picks`, primary key `(queue_item_id, group_id)`: what was picked.

| Column | Type | Notes |
|---|---|---|
| `queue_item_id` | int | |
| `group_id` | int | |
| `printer_id` | int | |
| `serial` | text | the hotend picked for this group |
| `picked_at` | timestamptz | |

`rack_nozzle_prints`, primary key `(archive_id, group_id)`: what was printed.

| Column | Type | Notes |
|---|---|---|
| `archive_id` | int | |
| `group_id` | int | |
| `serial` | text | copied from the pick |
| `settled_at` | timestamptz | |
| `print_seconds` | bigint | null when the archive has no time |
| `grams` | numeric | null when the archive has no weight |

- `rack_nozzle_seen` is written from exactly two status reads, both on the
  print path: `choices.py`'s (the dialog's choices read) and `choose_rack`'s.
  Not from `BambuddyClient.printer_status` itself: `download.py` and
  `project_file.py` read status for other reasons, and "first seen" means first
  seen by the print flow, which is what Oldest and Newest first rank on.
- The pick writes its `rack_nozzle_picks` rows right after `POST /queue/` returns
  the item id.
- The settle writes `rack_nozzle_prints`. `runs.py` cannot: a run finishes at
  queue time, with no duration or grams. The hook is `PrintWatcher`
  (`bambuddy/watcher.py`), which follows every print server side until it
  settles, whether or not anyone is looking. On the read where
  `ProgressObserver.observe` first sees the print settled (the same point it
  publishes `print.settled`), the watcher takes every archive linked to the
  output (`PrintLinkStore.for_output`, #306) whose `queue_item_id` has picks,
  reads each with `GET /archives/{id}` (`ArchiveDetail`), and writes one row per
  archive and picked group. This runs **after** `observe` has returned (so
  `print.settled` is already published) and before `_done`, and only on the
  `progress is not None and progress.settled` branch. Today's
  `if progress is None or progress.settled` also covers `progress is None` (an
  output never printed through `slice_queue`, see `progress_for`), which has no
  picks and must not reach the write. It is advisory:
  every exception from it, Bambuddy's or the database's, is caught and logged
  with the output id and archive id only (never a serial), and the watcher goes
  on to `_done` exactly as today. A failed write is not retried; that print's
  use is simply missing. Each row is written:
  - `print_seconds` is `actual_time_seconds`, else `print_time_seconds` (the
    slicer's estimate) when the print reported no actual time;
  - `grams` is `filament_used_grams`.
  - Both are whole-print totals: Bambuddy gives no per-group split. A plate with
    two rack groups credits each picked hotend with the full totals, which
    overstates both. This is accepted rather than guessed at; a per-group split
    would need the sliced 3MF's per-filament usage, which is out of scope here.
- An archive linked by `content_hash` with no `queue_item_id` cannot be tied to
  a pick and is not counted.
- A group's use is `count(*)`, `sum(print_seconds)` and `sum(grams)` over its
  serial's `rack_nozzle_prints` rows. "Least used" orders by `print_seconds`,
  then prints.
- The serial is recorded when the pick is made, because a hotend can be moved to
  another position later.
- Known limit: the queue item names a position, not a serial. If a hotend of the
  same diameter and flow is swapped into that position while the item waits,
  the pick still fits, Bambuddy prints through the new hotend, and its use is
  credited to the old serial. Re-reading the serial at settle would not fix
  this, because the rack may have changed again by then; the hotend actually
  mounted during the print is not something §2 has measured. Accepted here;
  tracking the hotend actually mounted belongs to the telemetry work in #912.
- Until history builds up every count is 0, so color and then position decide. If
  the printer's `wear` ever reports real values, it replaces `print_seconds` as the
  key with no UI change.

## 5. Where it runs and what the user sees

**Backend.** The seam is `slice_and_queue` (`bambuddy/dispatch.py`), the only
place that holds the sliced file id (`sliced`) and builds the `QueueItemCreate`.
Its existing `before_enqueue` hook takes no arguments and cannot set a queue
field, so it gains a parameter:

```python
choose_rack: Callable[[int], Awaitable[RackChoice | None]] | None = None
```

It is called with `sliced` after the slice succeeds and before `before_enqueue`.
A `RackChoice` carries `nozzle_rack_choice` and the picks' serials.
`nozzle_rack_choice` is `dict[str, int]`, keyed by `str(group_id)`, the type
`QueueItemCreate.nozzle_rack_choice` already has and the JSON shape Bambuddy
expects; `rank_rack` returns int group ids and `print_run.py` converts them when
it builds the `RackChoice`. `slice_and_queue` puts the choice on
`QueueItemCreate` and returns the serials on `QueueOutcome`.

`print_run.py`, its only caller, builds the callback:

1. Read `filament-requirements` for the sliced file, and re-read printer status
   for the live `nozzle_rack`. This is a fresh read on every call, once per
   plate, never `PreparedRun.printer_status`: on an all-plates print each plate
   slices in turn, and a rack read before the first slice can be stale by the
   last.
2. Collapse the requirements into groups. Several filaments can share one
   `group_id` (two colors on one hotend, §8), so `print_run.py` keeps one
   `RackGroup` per `group_id`, with that group's `group` fields and the material
   of its filaments; `rank_rack` never sees a duplicate.
3. Rank the rack for each `on_rack` group, and return the choice from the
   algorithm or the user's manual pick.
4. After the enqueue, write the picks to `rack_nozzle_picks` against the
   returned queue item id.

A raise or `None` from the callback means no choice: the item is queued without
`nozzle_rack_choice`, and Bambuddy picks, as today.

The pure ranking lives in `bambuddy/rack.py`, with no I/O:

```python
rank_rack(groups: list[RackGroup], rack: list[NozzleRackSlot],
          algorithm: Algorithm, usage: Mapping[str, Usage],
          manual: Mapping[int, int]) -> dict[int, Pick]
```

`manual` is the user's manual picks, `{group_id: position}`. They are placed
first and their positions are excluded for every other group, so a manual pick
and a ranked one can never name the same position. A manual pick that is not
eligible for its group, or two manual picks on one position, are refused before
anything is sliced: `/run` answers 422 naming the group. The Advanced select
lists only eligible positions and disables one already taken by another group,
so the refusal is a guard, not a path the UI offers.

`Pick` is internal and carries the serial: `group_id`, `position`, `serial`,
`reason`, `unsafe_material: bool` and the ranked `candidates`. `usage` is keyed
by serial. Nothing that carries a serial reaches the browser:

- `/check` answers with its own models, `RackOption` (`position`, `color`,
  `nozzle_type`, `material`, `prints`, `print_seconds`) and `RackPickView`
  (`group_id`, `position`, `reason`, `unsafe_material`, `options`), built field
  by field from `Pick`. A test asserts no serial appears in the `/check` body.
- `QueueOutcome` carries serials only as far as `print_run.py` (§6).

The choices endpoint (`/check`) returns the rack options per side, so the dialog
can show them before the run:

- the eligible positions, each with color, type, its decoded material and its use;
- the pick and its reason.

**Simple mode** shows one line per rack-side group, for example: "Rack nozzle:
position 3 (0.4 Standard) — already loaded with this color". When the pick is
unsafe for the material, it adds a warning, for example: "No hardened 0.4 nozzle
in the rack for PLA-CF; position 2 is brass." Like `hf-mounted`, the warning never
blocks Print.

**Advanced mode** adds:

- an **Algorithm** select (§4), saved per printer;
- a per-group **Nozzle** select listing the eligible positions with color, type,
  material and use. Choosing one sends it as a manual pick. "Automatic" goes
  back to the ranking.

**Failure handling.**

| Condition | Behavior |
|---|---|
| Status or requirements unreadable | Send no choice, so Bambuddy picks. The run result carries a `rack-left-to-bambuddy` warning: "rack pick left to Bambuddy: <reason>" |
| No eligible position for a group | Send no choice for that group. Bambuddy fails or auto-assigns exactly as today |
| A sent pick goes stale before dispatch | Bambuddy fails the item with its own message, which the run tracking already surfaces |

That last row applies in Simple mode too, not only to manual picks: every sent
pick is checked again at dispatch. It happens only when the rack changed while
the item waited so that the picked position no longer holds a nozzle of the
group's diameter and flow. The user requeues, and the new pick reads the new
rack. Sending no choice would avoid this failure but give up the whole ranking,
so the trade is accepted.

## 6. Code changes

- `bambuddy/models.py`:
  - correct the `nozzle_rack_choice` comment to say group ids;
  - add `serial_number` to `NozzleRackSlot`;
  - add `group_id: int | None` and `group: FilamentGroup | None` to
    `FilamentRequirement`, with a new `FilamentGroup` model (`on_rack`,
    `nozzle_diameter`, `volume_type`, `filament_color`). Today the model parses
    only `slot_id`, `type`, `color` and the usage fields, so `rank_rack` has no
    input without this.
- `bambuddy/dispatch.py`: the `choose_rack` parameter on `slice_and_queue`, and
  the picks on `QueueOutcome`. The picks carry serials, so `print_run.py`'s
  `_queued` must keep building `PrintRunResult` from named fields and never
  `model_dump()` the outcome into an API response (§7).
- `bambuddy/filaments.py`: two new `WarningKind` literals beside `hf-mounted`,
  both carried on `PrintRunResult.warnings` and on `/check`'s warnings like the
  existing kinds:
  - `rack-unsafe-material`: the pick is not hardened for an abrasive group;
  - `rack-left-to-bambuddy`: no choice was sent, with the reason ("status
    unreadable", "requirements unreadable", "no eligible position for group N").
    This is the message §5's failure table promises.
- `bambuddy/rack.py` (new): the material table, the abrasive test and `rank_rack`.
- `bambuddy/print_run.py`: the `choose_rack` callback, and writing the picks to
  `rack_nozzle_picks` after the enqueue.
- `bambuddy/watcher.py`: on the first settled read, write the print's
  `rack_nozzle_prints` rows from its linked archives.
- Migration: `rack_nozzle_seen`, `rack_nozzle_picks` and `rack_nozzle_prints`.
- `bambuddy/rack_usage.py` (new): `RackUsageStore`, the one store that owns all
  three tables, on the `PrintLinkStore` pattern (`print_links.py`): `seen()`,
  `record_picks()`, `record_prints()` and `usage(printer_id)`. `print_run.py`,
  `choices.py` and `watcher.py` call it; none of them holds SQL.
- `api/printing.py` / `/check`: rack options and picks per side.
- Settings: `printer_rack_algorithms`, remembered and cleared like
  `printer_bed_types`.
- Frontend print dialog: the Simple line and warning, plus Advanced's Algorithm
  and Nozzle selects.

## 7. Serials

Serials go into the three `rack_nozzle_*` tables and nowhere else. They never appear
in logs, in API errors, in the print dialog (which shows positions), in test
fixtures (which use invented serials), or in commits.

## 8. Unknowns to settle before building on them

| # | Question | Test | If it fails |
|---|---|---|---|
| 1 | What do the `nozzle_type` codes say about material? | Compare each rack position's code with the hotend's own label or Bambu's hotend list. Record the table here | Material step treats every nozzle as unknown (not hardened), and the abrasive warning is always shown for CF/GF/Glow |
| 2 | Does `filament-requirements` on a ScadBuddy-sliced file return `group_id` and `on_rack`? | Called on queue item 160's sliced file (library file 228) | **Pass, 2026-10-01.** Both filaments came back as `group_id: 0`, `group: {on_rack: true, nozzle_diameter: "0.20", volume_type: "Standard", filament_color: "#00B1B7"}`. Two colors on one hotend are one group, and the group's color is its first filament's, so step 2 of §3 matches on the group color |
| 3 | Does a sent pick change which hotend the printer mounts? | Queue a one-color print with a pick that differs from Bambuddy's default, with manual start. **Needs the owner's OK; it is a physical print** | Send no pick and keep only the warning |

## 9. Testing

- `rank_rack` table tests:
  - material over color;
  - color over use;
  - each algorithm;
  - position tiebreak;
  - no eligible position;
  - two groups never sharing a position, with the more constrained group first;
  - `"0.2"` on the rack matching a `"0.20"` group;
  - an unknown code counted as not hardened;
  - `PLA-CF`, `PA6-CF` and `ABS-GF` abrasive, `PLA` and `PLA-AERO` not;
  - a `PLA` group whose spool's subtype is `Glow` abrasive;
  - a manual pick reserved before the ranking, so no ranked group takes its
    position; an ineligible or duplicated manual pick refused with 422;
  - a group of PLA and PLA-CF counted as abrasive;
  - `"High Flow"` matching only `HH` codes, `"Standard"` only non-`HH`, and a
    missing code or name matching either.
- API tests for `/check` rack options and for `nozzle_rack_choice` on the queued
  item, with Bambuddy mocked.
- Usage tests:
  - a settled print writes one row per linked archive and picked group;
  - a `quantity` 2 item with two archives counts two prints;
  - a second settle of the same print changes nothing;
  - `actual_time_seconds` is used, and `print_time_seconds` only when it is null;
  - an archive with no `queue_item_id` is not counted;
  - an archive read or database write that raises is logged, writes nothing, and
    the print still settles and publishes `print.settled` once;
  - an output whose progress is `None` never reaches the write.
- A `choose_rack` test: on a two-plate print the second plate's pick uses a
  rack read after the first plate sliced.
- A `slice_and_queue` test: `choose_rack`'s choice lands on the queued item,
  and a raise from it queues the item without one.
- `print_run.py` tests:
  - two filaments sharing a `group_id` become one `RackGroup`;
  - `test_print_run_choices.py` keeps "no choice when the rack is unreadable"
    and gains "the ranked choice is sent".
- A stale-pick test: a queue item that Bambuddy failed with "Nozzle rack pick no
  longer fits the printer" shows that message in the print's progress and run
  result, for a ranked pick and for a manual one.
- A `/check` test that no serial appears in the response body.
- A logging test (`caplog` at `DEBUG`, as in `tests/test_library_processes.py`):
  a full pick, enqueue and settle with invented serials, including the failure
  paths of §5, leaves none of those serials in any log record's message or
  `extra`.
- Frontend tests: the Simple line, the warning, and the Advanced selects sending
  a manual pick.
- Live acceptance after deploy (unknown 3 above), recorded in §8.
