# Weighing as reconciliation (design)

Status: approach chosen by the owner on 2026-11-25 (C, §6); the doc is in review on its
PR. Epic [#7681](https://github.com/eh-homelab/ScadBuddy/issues/7681), under the filament
epic [#6260](https://github.com/eh-homelab/ScadBuddy/issues/6260). It builds on the ledger
(#6261, `2026-11-14-filament-management-design.md` §5.5), the insert weight offer (#7280)
and the spool parts design (#7278, `2026-11-23-spool-parts-design.md`).

## 1. Intent

The scale is the truth. ScadBuddy knows what was on a spool and when: the label weight,
every print's grams, Bambuddy's usage rows, the AMS readings. A weighing is the moment
those beliefs meet a measurement. Today a weighing only replaces the grams left. The
owner wants each weighing to **reconcile**: split the reading into filament and empty
spool, compare the filament with what the ledger expected, show the difference in plain
words, and keep the numbers so the next weighing and the spool's end of life can use
them (owner, 2026-11-25).

Three moments matter:

- **The first weighing** of a new spool. Above the label weight it gives the empty
  spool's weight (#7280 does this). Below the label weight something is off, and
  ScadBuddy must say what the possibilities are instead of booking a number quietly.
- **Every weighing** after that. The ledger says what should be left; the scale says
  what is. The gap is the drift, and it belongs on the record with its sign.
- **The last weighing**, when the roll is finished, respooled or damaged out. It closes
  the books: what the spool started with, what the ledger consumed, what the scale says
  was consumed.

## 2. Goals

- G1. Every scale weighing keeps the gross reading, the empty weight taken off it with
  its basis, the filament, the grams left the ledger expected just before it, and the
  drift.
- G2. The spool page and the history show the newest weighing's breakdown and drift as a
  sentence a person understands without the ledger rules.
- G3. A new spool's first weighing under the label weight is a choice with numbers,
  never a silent booking.
- G4. A spool's end of life has a reason and an optional final weighing, and the record
  keeps the lifetime reconciliation.
- G5. A print's length is booked next to its grams, so grams, length and density check
  each other and name the likely cause of a drift.
- G6. The empty weight comes from the insert type until the spool has a carrier and
  parts (#7278); then it comes from the sum, with no change to the weighing's record.

## 3. Non-goals

- No scale hardware or phone capture here. The reading arrives as it does today, typed
  or through #7273's phone page.
- No change to the grams-left rule (§5.5 of the filament spec): the anchor stays the
  newest absolute reading, and a weighing's filament is still what anchors. The
  reconciliation reads the ledger and writes to the event's `data`; it never moves an
  anchor or a delta.
- Carriers and add-ons as tracked units are #7278, not this epic. This epic only
  defines what it reads from them once they exist (G6).
- Bambuddy keeps one `core_weight` and one weight per spool; the breakdown is not sent
  to it. The sync sends what it sends today.

## 4. Anti-goals

- A reconciliation that corrects the ledger by itself. A drift is shown and kept; a
  person decides what it means. Nothing is booked as filament because the arithmetic
  said so.
- A second weight model. The breakdown lives on the `weighed` event's `data`, read back
  by the same code that reads the ledger. No new table for weighings.
- A hidden choice. When the numbers allow more than one reading of the situation (§5.3),
  the person picks; a default is shown, not applied.

## 5. Design

### 5.1 What a weighing records (slice A, #7682)

Measured on origin/main 6a2bccb22, 2026-11-25:

- `POST /filament/spools/{id}/events` takes `kind: weighed` with `grams` = the filament
  left, "without the empty spool" (`filament/models.py` `SpoolEventAdd`).
- The spool page subtracts the insert's `empty_weight_g` from the typed reading before
  it posts (`SpoolPage.tsx` `weigh`: `filament = scale - empty.empty_weight_g`). The gross
  reading is not stored anywhere.
- `ledger.record` writes `data` = `{note}` only for a weighing (`filament/spools.py`
  `add_event`).
- The grams left just before an event are what `ledger.grams_left` gives for the spool's
  events up to that time.

The change:

- `SpoolEventAdd` gains `reading_g` (optional, the gross reading in grams). When it is
  given with `grams`, the store checks `reading_g - grams` against the spool's effective
  empty weight; the difference is the empty weight the client used, and it is what the
  event keeps. When `grams` is left out and `reading_g` given, the store computes
  `grams = reading_g - empty_g` with the effective empty weight, and refuses (422) a spool
  with no empty weight known (`no_empty_weight`) or a reading under that empty weight
  (`reading_below_empty`), naming it.
- The `weighed` event's `data` keeps: `reading_g`; `empty_g`; `empty_source` (`insert`,
  or `parts` once #7278 lands, or `person` when the client's `reading_g - grams` is not
  the effective empty weight, which includes a spool with none known); `empty_basis` (for `insert`, the insert's
  `empty_weight_basis`, with `placeholder` decided by `insert_weights.is_placeholder`,
  the basis or the note; null for `person`); `empty_name` (the insert's name, null for
  `person`); `expected_g` (the grams left the ledger gave just before the event's time,
  null when there was no anchor); `drift_g` (`grams - expected_g`, null when
  `expected_g` is null: negative means the scale found less filament than the ledger
  expected). The ledger's walk computes `expected_g` at write time, in the transaction
  that records the event, over the events before the weighing's `at` and, by the
  ledger's rule 1 (at one instant deltas come before readings), the delta events at
  exactly that `at` (a back-dated weighing is compared with what was believed then).
- The effective empty weight is one function in `filament`, as #7278 §5 names it. Until
  #7278, it returns the insert type's `empty_weight_g` through the spool → type
  resolution that `check_on` in `insert_weights.py` already does.
- `person` and `scale` weighings are treated alike here; `bb` weighings (pulled from
  Bambuddy) record no breakdown, since Bambuddy gives no reading.
- As built in #7683: `empty_source` has a third value, `tared`, for a weighing resolved as
  §5.3's `tared`: nothing was taken off the reading, so `empty_g` is 0 and `empty_basis`
  and `empty_name` are null.

### 5.2 What the person sees (slice A)

- The spool page's Weight section shows, for the newest weighing with a breakdown:

  ```text
  Weighed 1180 g on 25 Nov: filament 980 g + spool 200 g (DEEPLEE - Plastic, placeholder).
  The ledger expected 992 g. The scale found 12 g less than the prints accounted for.
  ```

  The second line is left out when `expected_g` is null. "less" / "more" follows the
  sign; "matches the prints" under 1 g.
- An added-full spool whose anchor is still its `created` event (nothing weighed or
  re-anchored it since; a later AMS or sync anchor would count printed filament as a
  gap) shows the gap between its label weight and that starting amount:

  ```text
  Started at the label weight 1000 g. Bambuddy reported 979.3 g; 20.7 g unexplained.
  ```

  This is the owner's "it shows we started with 979.3" (2026-11-25): the number stays,
  the sentence says where it came from. It reads `GramsBasis`'s anchor and the spool's
  `added_full`; nothing is booked.
- The history timeline shows the breakdown and the drift on each weighing that has
  them. The existing `anchorText` ("Starting amount 979.3 g, Bambuddy") stays for the
  basis panel.

### 5.3 The first weighing under the label weight (slice B, #7683)

#7280's check (`insert_weights.check_on`) answers `not_above_label` when a new, unused
spool reads at or under its label weight, and the page then books the reading less the
placeholder empty weight. The change:

- For a spool that is new by #7280's rule (added full, nothing consumed, grams left at
  the label), a reading under the label weight makes the check answer `below_label`
  with `shortfall_g = label - reading` and three resolutions, each with its filament
  grams:
  1. `tared`: the scale was tared on the empty spool, so the reading is the filament
     alone. Filament = reading. The empty weight is not touched.
  2. `short`: the spool holds less than its label. Filament = reading − empty weight.
     The spool stops being "added full" (`added_full = false`).
  3. `label_wrong`: the label weight is wrong for this spool. Filament = reading − empty
     weight, and the spool's own `label_weight_g` becomes that filament (an `edited`
     event, as any label change).
- The spool page shows the three with their numbers and a default of `tared` when the
  reading is within 10 % of the label, else `short`. The person picks; the weighing
  posts `resolution` with `reading_g`, and the event's `data.resolution` and
  `data.shortfall_g` keep it. The agent's weighing tool takes the same field.
- With no empty weight known, only `tared` is offered.
- #7280's offer needs a reading above the label weight, so `below_label` and the offer
  never meet on one reading.
- As built in #7683: the check needs an insert with a weight, as #7280's does (it answers
  `no_insert` first); with no empty weight known the client records the reading as the
  filament, which is `tared` by nature, with no choice. `below_label` fires for a reading
  under `label - 0.5 g` (#7280's full tolerance), after `used` and `not_full` and before
  `not_above_label`. The resolutions come in the order `tared`, `short`, `label_wrong`;
  the last two only when the reading is at least the empty weight, so a reading under it
  offers `tared` alone. `label_wrong`'s new label is the filament rounded half up, at
  least 1 g. The store checks the choice again under the row locks: a `resolution` the
  check does not offer now is 422 `no_below_label` with the check's `reason`, and
  nothing is booked. `short` and `label_wrong` book their side effect as an `edited`
  event (`data.changes` `{"added_full": [before, false]}` or
  `{"label_weight_g": [before, new]}`, `before` the spool's own value, null when it came
  from the type) at the weighing's `at`, before the weighing, in the same transaction.
- As built in #7683: a weighing with a `resolution` keeps `expected_g` (what the ledger
  believed) and records `drift_g` null, so `last_drift_g` stays null and no Drift mark
  appears: the person just explained the gap (the owner's call of 2026-11-25).

### 5.4 End of life (slice C, #7684)

Archiving writes an `archived` event with no reason (`filament/spools.py` `_archive`).
The change:

- `SpoolPatch.archived` gains company: `archive_reason` (`finished`, `respooled`,
  `damaged`, `other`) and `final_reading_g` (optional). The archive route and the agent
  tool take both. The reason is required on a new archive from the UI; the API keeps it
  optional for the sync's archives (`bb`), which record `other`.
- With `final_reading_g`, the store books a `weighed` event (§5.1) just before the
  `archived` event, at the archive's time.
- The `archived` event's `data` keeps `reason` and the lifetime reconciliation:
  `started_g` (the spool's first `scale` or `person` filament reading, else its label
  weight for an added-full spool, else null), `ledger_consumed_g` (the plates' grams
  the ledger's walk counts from `started_g`'s event to the archive, by §5.5's rules of
  the filament spec, ignoring every `consumed_reset`: the spool's `consumed_g` counts
  only since the newest reset, so it is not this number), `scale_consumed_g`
  (`started_g - final filament`, null without a final reading), `drift_g`
  (`ledger_consumed_g - scale_consumed_g`, the same sign as §5.1's: negative means
  the scale found less filament than the ledger expected).
- Before the archive, the page says what it sees when the numbers disagree with the
  reason: `finished` with more than 5 g left ("25 g of filament are still on it"),
  or anything but `finished` at 0 g. The person can go on.
- `respooled` offers to continue as a new spool now, through the copy flow that exists
  (#7150 `copy_of`), with the final filament as the new spool's starting grams
  (`added_full = false`), the same type and tag choice left to the person. The two
  events link: `data.continued_as` on the old spool's `archived` event, and
  `data.continued_from` on the new spool's `created` event. The carrier the filament
  moved onto is #7278's business; until then the new spool's empty weight is its insert
  type's.
- An archived spool's page shows the closing summary in the Weight section; the history
  timeline shows the reason on the archive.

### 5.5 Tolerances

Two settings, in the filament area, rememberable as every setting is:

- `filament_weighing_drift_notice_g` (default 10; slice A): a drift at or over it is
  shown as a notice on the spool and in the list, not just in the Weight section.
- `filament_weighing_finished_left_g` (default 5; slice C): what counts as empty for
  §5.4's check.

### 5.6 Length (slice D, #7685)

A print's events book grams only (`filament/print_uses.py` writes `data={"job": …}`).
Bambu Studio's sliced plate carries each filament's `used_m` and `used_g` in
`Metadata/slice_info.config`, which ScadBuddy already reads for the grams (where the
plan must confirm this; the builder measures it on a plate in the native library). The
change:

- `print_started` and `print_ended` keep `data.length_m` next to the grams, from the
  same source as the grams (the estimate, then the actual when the printer reports
  it; Bambuddy's usage rows have no length and keep none).
- The spool's lifetime shows grams and metres consumed and the density they imply:
  `grams / (metres × 100 × π × (diameter_cm / 2)²)`, every length in centimetres
  (1 m = 100 cm; 1 m of 1.75 mm PLA at 1.24 g/cm³ is about 2.98 g), against the type's
  `density_g_cm3`. A mismatch over 5 % is a sentence on the spool, never a
  booking.
- §5.2's drift sentence names a likely cause when one fits: the implied density is off
  and the label weight is right (a wrong density on the type), or the drift matches
  the empty weight's placeholder (a wrong empty weight), or the drift matches the
  label's shortfall (a wrong label weight). With no fit it names none.

### 5.7 Carriers (slice E, #7278)

Once #7278 lands, the effective empty weight (§5.1) is the insert's weight plus every
installed part's, `empty_source` becomes `parts`, and `empty_basis` keeps the insert's
basis. The breakdown sentence lists them
("spool 200 g + AMS adaptor 18 g"). #7278's G4 ("parts don't add up") is the
first-weighing check of §5.3 with parts in the sum; the two share the code.

## 6. Approaches

1. **B. Carriers first, in the first PR.** The breakdown would be exact from day one
   for spools with a carrier. It makes slice A wait for #7278 and its inventory work,
   and most spools today have no carrier, only an insert type. Rejected by the owner.
2. **C. Reconciliation and end-of-life weighings first, the empty weight from the
   insert type, carriers second (chosen).** Slices A to D ship on what exists; E
   swaps the empty weight's source without touching the records.
3. Reconciliation as a report only, no stored numbers. Cheaper, but the end-of-life
   summary and the drift history need the numbers as they were at the time, and the
   empty weight can change later. Rejected.

## 7. Constraints

- The ledger's rules (§5.5 of the filament spec) stand; `expected_g` is computed by the
  same walk (`ledger.basis` / `grams_left`) over the events §5.1 names.
- Migrations: new event `data` keys need none; `archive_reason` lives in the event's
  `data` too. If the builder keeps `added_full` changes or the label edit on the spool
  row, they are `edited` events as today. One new timestamped migration file only if a
  column is needed.
- The agent's filament tools (`filament_spool_add_event`, `filament_spool_archive`)
  take the new fields at their tiers; nothing new is outward.
- Every number shown follows the frontend's `grams()` formatting.

## 8. Phases

| Slice | Issue | Builds | After |
|---|---|---|---|
| A | [#7682](https://github.com/eh-homelab/ScadBuddy/issues/7682) | §5.1, §5.2, §5.5 (the drift notice) | - |
| B | [#7683](https://github.com/eh-homelab/ScadBuddy/issues/7683) | §5.3 | A |
| C | [#7684](https://github.com/eh-homelab/ScadBuddy/issues/7684) | §5.4, §5.5 (finished) | A |
| D | [#7685](https://github.com/eh-homelab/ScadBuddy/issues/7685) | §5.6 | A |
| E | [#7278](https://github.com/eh-homelab/ScadBuddy/issues/7278) | §5.7 | A, #7067 (merged) |

## 9. Left to the builder

- Whether `expected_g` for a back-dated weighing reuses `ledger.basis` with a filtered
  list or a cut-off argument.
- The exact wording of the sentences, kept short and in the page's voice.
- Where the closing summary sits on an archived spool's page.
- Whether the drift notice joins "Needs you" or stays on the filament list.
