# Rack nozzle selection (#836)

Status: design approved in conversation 2026-10-01; this spec awaits review.
Part of epic #84.

## 1. What this changes

An H2C's rack-side extruder picks its hotend from a six-position rack. Today
ScadBuddy leaves that pick to Bambuddy, which takes the first eligible position,
preferring one that last held the group's color. That ignores the filament's
material and spreads no wear.

ScadBuddy now ranks the rack itself and sends its pick on the queue item. Simple
mode shows the pick and the reason for it. Advanced mode lets the user change the
ranking algorithm or pick a position by hand.

Out of scope: the non-rack extruder (one fixed hotend, nothing to choose), the
slice itself (#840 already decides which side prints), and full per-print
telemetry (#912; this spec takes only the usage counter it needs).

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
(`resolve_rack_plan_mapping`). An explicit pick that no longer fits fails the
item with "Nozzle rack pick no longer fits the printer". Groups left out are
auto-assigned as today.

## 3. The ranking

For each `on_rack` group, the eligible positions are those holding a nozzle of
the group's diameter and flow type. This is the same test as Bambuddy's
`_rack_slot_is_eligible`. Positions already picked for another group are
excluded. Among the eligible positions:

1. **Material safe for the filament.** An abrasive filament needs a hardened
   nozzle. A filament is abrasive when its material names `CF`, `GF` or `Glow`. A
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

**Usage counter.** Two new tables. Use is summed from one row per print, so a
settle seen twice (two replicas, a restart) cannot count a print twice.

`rack_nozzle_seen`, primary key `(printer_id, serial)`:

| Column | Type | Notes |
|---|---|---|
| `printer_id` | int | |
| `serial` | text | |
| `first_seen_at` | timestamptz | |

`rack_nozzle_prints`, primary key `(queue_item_id, group_id)`:

| Column | Type | Notes |
|---|---|---|
| `queue_item_id` | int | |
| `group_id` | int | |
| `printer_id` | int | |
| `serial` | text | the hotend picked for this group |
| `picked_at` | timestamptz | |
| `settled_at` | timestamptz | null until the print settles |
| `print_seconds` | bigint | null when no archive was linked |
| `grams` | numeric | null when no archive was linked |

- Every status read that ScadBuddy already makes for the print dialog inserts
  `rack_nozzle_seen` for each serial it sees (`ON CONFLICT DO NOTHING`).
- The pick writes its `rack_nozzle_prints` rows right after `POST /queue/` returns
  the item id.
- The settle fills them. `runs.py` cannot: a run finishes at queue time, with no
  duration or grams. The hook is `PrintWatcher` (`bambuddy/watcher.py`), which
  follows every print server side until it settles, whether or not anyone is
  looking. On the read where `ProgressObserver.observe` first sees the print
  settled (the same point it publishes `print.settled`), the watcher reads the
  queue item's linked archive (`PrintLinkStore`, #306) for `duration_seconds`
  and `filament_used_grams`, and sets `settled_at`, `print_seconds` and `grams`
  `WHERE settled_at IS NULL`. With no archive linked yet it sets `settled_at`
  only: the print still counts, and its time and grams stay unknown.
- A group's use is `count(*)`, `sum(print_seconds)` and `sum(grams)` over its
  serial's settled rows. "Least used" orders by `print_seconds`, then prints.
- The serial is recorded when the pick is made, because a hotend can be moved to
  another position later.
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
A `RackChoice` carries `nozzle_rack_choice` (`{group_id: position}`) and the
picks' serials. `slice_and_queue` puts the former on `QueueItemCreate` and returns
the latter on `QueueOutcome`. `print_run.py`, its only caller, builds the callback:

1. Read `filament-requirements` for the sliced file and the live `nozzle_rack`.
2. Rank the rack for each `on_rack` group, and return the choice from the
   algorithm or the user's override.
3. After the enqueue, write the picks to `rack_nozzle_prints` against the
   returned queue item id.

A raise or `None` from the callback means no choice: the item is queued without
`nozzle_rack_choice`, and Bambuddy picks, as today.

The pure ranking lives in `bambuddy/rack.py` (`rank_rack(groups, rack,
algorithm, usage) -> {group_id: Pick}`), with no I/O.

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
  material and use. Choosing one sends it as an explicit pick. "Automatic" goes
  back to the ranking.

**Failure handling.**

| Condition | Behavior |
|---|---|
| Status or requirements unreadable | Send no choice, so Bambuddy picks. The run result says "rack pick left to Bambuddy: <reason>" |
| No eligible position for a group | Send no choice for that group. Bambuddy fails or auto-assigns exactly as today |
| An explicit pick goes stale before dispatch | Bambuddy fails the item with its own message, which the run tracking already surfaces |

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
  the picks on `QueueOutcome`.
- `bambuddy/rack.py` (new): the material table, the abrasive test and `rank_rack`.
- `bambuddy/print_run.py`: the `choose_rack` callback, and writing the picks to
  `rack_nozzle_prints` after the enqueue.
- `bambuddy/watcher.py`: on the first settled read, fill the print's
  `rack_nozzle_prints` rows from its linked archive.
- Migration: `rack_nozzle_seen` and `rack_nozzle_prints`.
- `api/printing.py` / `/check`: rack options and picks per side.
- Settings: `printer_rack_algorithms`, remembered and cleared like
  `printer_bed_types`.
- Frontend print dialog: the Simple line and warning, plus Advanced's Algorithm
  and Nozzle selects.

## 7. Serials

Serials go into the `rack_nozzle_seen` and `rack_nozzle_prints` tables and nowhere else. They never appear
in logs, in API errors, in the print dialog (which shows positions), in test
fixtures (which use invented serials), or in commits.

## 8. Unknowns to settle before building on them

| # | Question | Test | If it fails |
|---|---|---|---|
| 1 | What do the `nozzle_type` codes say about material? | Compare each rack position's code with the hotend's own label or Bambu's hotend list. Record the table here | Material step treats every nozzle as unknown (not hardened), and the abrasive warning is always shown for CF/GF/Glow |
| 2 | Does `filament-requirements` on a ScadBuddy-sliced file return `group_id` and `on_rack`? | Called on queue item 160's sliced file (library file 228) | **Pass, 2026-10-01.** Both filaments came back as `group_id: 0`, `group: {on_rack: true, nozzle_diameter: "0.20", volume_type: "Standard", filament_color: "#00B1B7"}`. Two colors on one hotend are one group, and the group's color is its first filament's, so step 2 of §3 matches on the group color |
| 3 | Does an explicit pick change which hotend the printer mounts? | Queue a one-color print with a pick that differs from Bambuddy's default, with manual start. **Needs the owner's OK; it is a physical print** | Drop the explicit pick and keep only the warning |

## 9. Testing

- `rank_rack` table tests:
  - material over color;
  - color over use;
  - each algorithm;
  - position tiebreak;
  - no eligible position;
  - two groups never sharing a position;
  - an unknown code counted as not hardened.
- API tests for `/check` rack options and for `nozzle_rack_choice` on the queued
  item, with Bambuddy mocked.
- Usage tests:
  - a settled print fills its row from the linked archive;
  - a second settle of the same print changes nothing;
  - a settle with no linked archive counts the print with null time and grams.
- A `slice_and_queue` test: `choose_rack`'s choice lands on the queued item,
  and a raise from it queues the item without one.
- Frontend tests: the Simple line, the warning, and the Advanced selects sending
  an explicit pick.
- Live acceptance after deploy (unknown 3 above), recorded in §8.
