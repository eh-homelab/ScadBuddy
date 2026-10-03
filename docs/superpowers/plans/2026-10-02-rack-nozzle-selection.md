# Rack Nozzle Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** ScadBuddy ranks the H2C's six-position nozzle rack for each sliced plate and
sends its pick as `nozzle_rack_choice`, shows the pick and its reason, and lets Advanced
mode change the ranking algorithm or pick a position by hand (#836, epic #84).

**Architecture:** A new feature package `scadbuddy/rack/` holds the pure ranking
(`rank.py`), the one store for the three `rack_nozzle_*` tables (`usage.py`) and its
component (`component.py`). `slice_and_queue` gains a `choose_rack` callback that
`print_run.py` builds per plate from a fresh rack read; the picks are written after
`POST /queue/`, and the print watcher writes per-archive use when the print settles.
`/check` previews the rack side's pick before Print; the dialog shows it.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, psycopg 3 + psycopg_pool, pytest +
respx; React 19 + Vite, vitest + msw, openapi-typescript (`pnpm gen:api`).

**Spec:** `docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md` (binding).
Also read `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` §6 (superseded
in one decision by Task 15) and the repo `CLAUDE.md` (commands, migrations, layout).

## Global Constraints

Copied verbatim from the spec; every task's requirements include these.

- §7: "Serials go into the three `rack_nozzle_*` tables and nowhere else. They never appear
  in logs, in API errors, in the print dialog (which shows positions), in test fixtures
  (which use invented serials), or in commits."
- §4: "every exception from it, Bambuddy's or the database's, is caught and logged with a
  fixed message, `type(exc).__name__`, the output id and the archive id, and never
  `str(exc)` or a traceback: a database or HTTP error can carry the serial it failed on in
  its own text (§7). The same rule holds for every `RackUsageStore` and `choose_rack` error
  path."
- §3: "**Diameter** is compared as `round(float(x), 2)`, never as a string"
- §3: "A code not in the table counts as not hardened."
- §8: "`rack/rank.py` ships with the table **empty**, so every code counts as not hardened:
  abrasive groups always get `rack-unsafe-material`, never a silent brass pick."
- §6: "`print_run.py`'s `_queued` must keep building `PrintRunResult` from named fields and
  never `model_dump()` the outcome into an API response (§7)."
- §6: "the rack code is its own feature package, `scadbuddy/rack/` (`rank.py`, `usage.py`,
  `component.py`), never a new `AppState` field. Routes read it through
  `api/components.py` `component_dep`; `print_run.py`, `choices.py` and `watcher.py` call
  it, and none of them holds SQL."
- §4: "`printer_rack_algorithms: dict[str, Algorithm]`, a new `StoredSettings` field stored
  in the existing jsonb `settings` row, the way `printer_print_options` is stored
  (`library/settings_store.py`), so it needs no migration and is not added to
  `OWN_TABLES`."
- §4: "`rack_nozzle_picks` and `rack_nozzle_prints` are written `ON CONFLICT DO NOTHING`
  ... `rack_nozzle_seen` is the one upsert: `ON CONFLICT (serial) DO UPDATE SET printer_id
  = excluded.printer_id`, which moves the hotend's printer and never touches
  `first_seen_at`."
- §6: "All three carry `slot_id: null`, which the frontend reads as plate-wide"
- §5: "Like `hf-mounted`, the warning never blocks Print."
- §5: "**The callback never raises; `slice_and_queue` does not catch it.**"

Owner and repository rules (not from the spec, equally binding):

- US spelling in new prose, UI text and messages ("color"). Existing identifiers such as
  `normalise_colour`, `filament_colour`, `filament_colours` keep their names.
- Commit with explicit paths only: `git add <path> ...`, never `git add -A` or `git add .`.
- Invented serials in tests look like `TEST-HOTEND-17` (`tests/rack/helpers.py`). Never
  paste a real one into a file, a test name, a commit message or a log.
- Backend checks per task (in `backend/`): `uv run --frozen ruff check .`,
  `uv run --frozen ruff format --check .`, `uv run --frozen mypy`, and the task's pytest
  command. Tests marked `requires_postgres` need
  `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`
  (CLAUDE.md shows the `docker run` for it); `tests/api` also needs Temporal (CLAUDE.md).
  A skipped test is not a passing test: run them with the database up.
- Frontend checks (in `frontend/`): `pnpm lint && pnpm typecheck && pnpm test`.
  `pnpm typecheck` regenerates the API types from the backend first.
- Never commit `backend/openapi.json` or either `schema.d.ts`.
- New migration files are named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.

## Review Focus

Inputs the spec implies but its §9 list does not exercise, most likely to bite first. Each
has a test in the owning task.

1. **A hotend reported as `filament_color: "00000000"`** (no filament loaded, ids 1, 18
   and 20 in the recording) beside a black (`#000000`) group: it must not count as
   "already holds this color", since `normalise_colour` alone turns it into `#000000`.
   Task 4, `test_an_empty_hotends_zero_alpha_color_never_matches_black`.
2. **High Flow chosen on the rack side while the slice stays Standard (#484):** the
   preview must not offer, and `/run` must refuse with a reason, an `HH` position that the
   sliced Standard group cannot use and Bambuddy would fail at dispatch. Task 12,
   `test_a_high_flow_choice_is_judged_as_the_standard_slice_it_becomes`.
3. **A rack position missing from status** (its hotend on the carriage, or two absent):
   the mounted hotend is recovered for the position only when exactly one is missing, and
   no pick ever names a position status does not list. Task 4,
   `test_the_mounted_hotend_fills_the_one_missing_position_only`.
4. **An empty or repeated `serial_number`** in one status read: `seen()` skips empty and
   writes each serial once; a pick of an empty-serial hotend is still sent but writes no
   `rack_nozzle_picks` row, because nothing can be attributed to it. Tasks 5 and 10,
   `test_seen_skips_empty_and_repeated_serials` and
   `test_a_pick_with_no_serial_is_sent_but_not_recorded`.
5. **An output printed a second time:** the settle walks every link of the output,
   including the first print's archives; the first print's rows stay as they were and are
   not counted twice. Task 11, `test_a_second_print_of_one_output_counts_only_its_own_archives`.

## Rulings on gaps in the spec

Each ruling is written into the spec by Task 2, so the spec stays the authority.

- **§9 vs §5 on a raising `choose_rack`.** §9 asks for "a raise from it queues the item
  without one" in a `slice_and_queue` test; §5 says `slice_and_queue` awaits it bare. §5
  wins (it is the reviewed design): Task 7 pins that a raise fails the plate with nothing
  queued, and Task 9 pins that a raise *inside the callback's body* queues without a choice.
- **#1016, manual pick re-checked after the slice.** `rank_rack` re-checks a manual entry
  against the sliced group's diameter and flow. One that does not fit is ranked like any
  other group, and the run carries `rack-manual-partial` naming the position and group.
- **Preview and the 422 judge the flow the slice will carry**, which is Standard while
  #484 is open (`SLICED_VOLUME_TYPE`). Judging the dialog's High Flow choice would accept
  an `HH` position that Bambuddy rejects at dispatch.
- **Manual pick under "Let Bambuddy pick".** The manual pick is still sent for its group,
  since it is an explicit per-print choice. Other groups get no pick, as the algorithm says.
- **A per-print algorithm.** `PrintRunRequest.rack_algorithm` overrides the remembered one
  for one print. The dialog sends it, so `/check` previews what the selector shows, and
  `PUT /print/printers/{id}/rack-algorithm` remembers it.
- **No warning without a rack group.** `choose_rack` reads requirements first. When no
  `on_rack` group exists, it does not read status and warns of nothing. The only exception
  is a manual pick, which gets "manual rack pick unused".
- **The mounted hotend's position.** It is recovered as Bambuddy's `_rack_by_position`
  does: carriage id `RACK_SIDE` (0) fills the one missing position.
- **Fields beyond §5's models.** `RackOption` gains `nozzle_diameter` and `flow` (the
  Simple line prints "(0.4 Standard)"). `RackPickView` gains `glow_unchecked` (§3: "the
  dialog says Glow could not be checked"), and its `group_id` is `null` in the preview.

---

## File structure

| File | Responsibility |
|---|---|
| `backend/scadbuddy/rack/__init__.py` (new) | Package marker |
| `backend/scadbuddy/rack/rank.py` (new) | Pure ranking: eligibility, material table, abrasive test, `rank_rack`, warnings |
| `backend/scadbuddy/rack/usage.py` (new) | `RackUsageStore` (three tables), `record_seen`, `save_picks`, `record_settled`, `settle_hook` |
| `backend/scadbuddy/rack/component.py` (new) | `RACK_USAGE`, `COMPONENT`, `RackUsageDep`, the watcher hook registration |
| `backend/scadbuddy/migrations/<ts>_rack_nozzle_usage.sql` (new) | The three tables |
| `backend/scadbuddy/bambuddy/models.py` | `serial_number`, `FilamentGroup`, `group_id`/`group`, `RackAlgorithm`, the `nozzle_rack_choice` comment |
| `backend/scadbuddy/bambuddy/filaments.py` | Three `WarningKind` literals |
| `backend/scadbuddy/bambuddy/dispatch.py` | `choose_rack`, `RackChoice`, `QueueOutcome.rack_picks`, docstring |
| `backend/scadbuddy/bambuddy/print_run.py` | `rack_chooser`, picks write, warning dedup, preview, manual 422, request fields |
| `backend/scadbuddy/bambuddy/choices.py` | `rack_nozzle_seen` write, `ChoicesView.rack_algorithm` |
| `backend/scadbuddy/bambuddy/watcher.py` | `on_settled` hooks |
| `backend/scadbuddy/bambuddy/runs.py` | `run_key` excludes the new optional fields |
| `backend/scadbuddy/core/components.py` | `Core` gains `settings_store`, `print_links`, `print_watcher` |
| `backend/scadbuddy/core/events.py` | `SettingsSection` gains `printer_rack_algorithm` |
| `backend/scadbuddy/library/settings_store.py` | `printer_rack_algorithms`, `rack_algorithm()`, setter, forget |
| `backend/scadbuddy/api/printing.py`, `api/library_print.py`, `api/settings.py` | Plumbing, the algorithm route, remembered choices |
| `agent/src/tools/coverage.ts` | The new route's allowlist entry |
| `frontend/src/components/print/RackNozzle.tsx` (new) | Simple line, Advanced step |
| `frontend/src/components/PrintPicker.tsx`, `src/lib/useRunPrint.ts`, `src/api/client.ts`, `src/api/types.ts`, `src/pages/settings/RememberedChoicesPanel.tsx`, `src/mocks/...` | Wiring |
| `docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md` | Gate result, labels, rulings |
| `docs/superpowers/specs/2026-09-27-spool-first-print-design.md`, `docs/superpowers/plans/2026-09-27-spool-first-print.md` | Supersession notes |

---

### Task 1: The live pick gate (unknown 3) and the hotend labels (unknown 1)

This task writes no code. It decides whether the feature exists at all.

**Files:**
- Modify: `docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md` (Status line, §8)

**Interfaces:**
- Consumes: nothing.
- Produces: a recorded §8 verdict. If it fails, **the plan stops after Step 7**.

- [ ] **Step 1: STOP and ask the owner.** Do not queue anything yet. Send the owner this
  message, and wait for an explicit yes:

  > Unknown 3 of the rack spec (#836) needs one physical print on the H2C: a small
  > one-color plate queued with **manual start** and a hand-written `nozzle_rack_choice`
  > naming a rack position that Bambuddy would not pick itself. You start it from
  > Bambuddy's queue; it can be cancelled once the hotend swap has happened. Which plate
  > should I use (a small one-color 0.4 mm Standard print), and may I queue it?
  > Separately, for unknown 1: please read the label on each rack hotend and tell me its
  > code (as the printer shows it, e.g. HS01) and material (brass, hardened steel,
  > stainless, tungsten carbide...).

- [ ] **Step 2: Read the rack without printing a serial.** On a machine with the `primary`
  kube context (eh-homelab/clusters CLAUDE.md, "Kube contexts"; the same `kubectl exec`
  pattern as `backend/tests/bambuddy/recordings/README.md`):

```bash
kubectl config current-context   # must be primary
POD=$(kubectl -n bambuddy get pod -l app.kubernetes.io/name=bambuddy -o jsonpath='{.items[0].metadata.name}')
bb() { kubectl -n bambuddy exec "$POD" -- curl -s "$@"; }
bb http://localhost:8000/api/v1/printers/1/status \
  | jq -c '.nozzle_rack[] | {id, position: (if .id >= 16 then .id - 15 else null end), nozzle_type, nozzle_diameter, filament_color}'
```

  Expected: one line per hotend, no `serial_number` field printed. Note which 0.4 mm `HS`
  positions exist (at least two are needed).

- [ ] **Step 3: Pick the test position.** Get the sliced library file of the owner's plate
  (`SLICED`), and its single group:

```bash
SLICED=<the sliced library file id the owner named>
bb "http://localhost:8000/api/v1/library/files/$SLICED/filament-requirements" \
  | jq -c '.filaments[] | {slot_id, type, used_in_plate, group_id, group}'
```

  Expected: one `used_in_plate` filament with `group.on_rack: true`, `nozzle_diameter`
  `"0.40"` and `volume_type` `"Standard"`. Bambuddy's own pick is the lowest eligible
  position whose `filament_color` matches the group's color, else the lowest eligible.
  Choose `P` as an eligible position that is **neither** of those. Write down `GROUP`
  (the `group_id`), `P` and Bambuddy's default `D`.

- [ ] **Step 4: Fingerprint position P's hotend without recording its serial.**

```bash
P=<position>; GROUP=<group id>
bb http://localhost:8000/api/v1/printers/1/status \
  | jq -r --argjson id "$((P + 15))" '.nozzle_rack[] | select(.id == $id) | .serial_number' \
  | sha256sum > /tmp/rack-pick-before.sha
```

- [ ] **Step 5: Queue with manual start (only after the owner's yes).**

```bash
bb -X POST http://localhost:8000/api/v1/queue/ -H 'Content-Type: application/json' \
  -d "{\"printer_id\":1,\"library_file_id\":$SLICED,\"plate_id\":1,\"manual_start\":true,\"nozzle_rack_choice\":{\"$GROUP\":$P}}" \
  | jq '{id, status, error_message}'
```

  Expected: a queue item with `status` `"pending"` or `"queued"`. If the loopback answers
  401, ask the owner for the Bambuddy API key, read it with `read -rs KEY` (no echo, no
  shell history), and send the header on stdin so it never appears in a command line on
  either side of `kubectl exec` (`ps`, `/proc/<pid>/cmdline`):

```bash
printf 'X-API-Key: %s\n' "$KEY" | kubectl -n bambuddy exec -i "$POD" -- curl -s -H @- \
  -X POST http://localhost:8000/api/v1/queue/ -H 'Content-Type: application/json' \
  -d "{\"printer_id\":1,\"library_file_id\":$SLICED,\"plate_id\":1,\"manual_start\":true,\"nozzle_rack_choice\":{\"$GROUP\":$P}}" \
  | jq '{id, status, error_message}'
unset KEY
```

  Tell the owner the item id and ask them to press Start.

- [ ] **Step 6: Compare the mounted hotend with P's, by hash only.** Once the owner reports
  the hotend swap is done, run this. Carriage id 0 is the rack side
  (`extruders.py` `RACK_SIDE`):

```bash
bb http://localhost:8000/api/v1/printers/1/status \
  | jq -r '.nozzle_rack[] | select(.id == 0) | .serial_number' | sha256sum > /tmp/rack-pick-after.sha
cmp -s /tmp/rack-pick-before.sha /tmp/rack-pick-after.sha && echo "PASS: the printer mounted position $P" || echo "FAIL: another hotend is mounted"
rm -f /tmp/rack-pick-before.sha /tmp/rack-pick-after.sha
```

- [ ] **Step 7: Record the verdict in the spec, then branch.** In §8, replace row 3's
  "If it fails" cell content with the result, keeping the column's text:
  - On PASS: prefix the row's last cell with
    `**Pass, <YYYY-MM-DD>.** Queue item <id> named position <P> for group <GROUP>
    (Bambuddy's own pick would have been <D>); the printer mounted position <P>'s hotend,
    compared by hash, serial not recorded.` and change the Status line at the top to
    `Status: approved (PR #915); unknown 3 passed on <YYYY-MM-DD> (§8).`
  - On FAIL: prefix it with `**Fail, <YYYY-MM-DD>.** ...what happened...`, commit (Step 9),
    then **stop executing this plan.** Tell the owner the spec must be revised to warnings
    only (§8 "If it fails"), and that Tasks 2-15 do not apply as written.

- [ ] **Step 8: Record the labels (unknown 1) and the rollout note (#1011, finding 2).**
  In §8 row 1, append the owner's answers as a list, `code → material`, one per code
  (not per position), for example `HS01 → <material>`. Do **not** fill
  `NOZZLE_MATERIALS` in code: the table ships empty regardless (owner's decision). Then
  add this paragraph directly under the §8 table:

  > **Rollout (#1011).** `NOZZLE_MATERIALS` ships empty, so every nozzle counts as not
  > hardened. Until a follow-up fills it from the labels above, **every** print whose rack
  > group is abrasive (a `CF`/`GF` type, or a Glow spool) carries `rack-unsafe-material`,
  > in Simple mode as well, whichever algorithm is chosen. That is the intended fail-safe,
  > not a bug: report it as expected during the first deployment.

- [ ] **Step 9: Commit**

```bash
git add docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md
git commit -m "docs(836): record the live rack pick test and the hotend labels" -m "Refs #836, #1011"
```

---

### Task 2: Spec amendments from the #915 review

**Files:**
- Modify: `docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md`

**Interfaces:**
- Consumes: Task 1's verdict (PASS).
- Produces: the amended spec every later task argues from.

- [ ] **Step 1 (#1011, finding 1): the logging trade-off.** At the end of §7 add:

  > **Decision (#1011).** The type-name-only rule is kept knowingly. A failure on these
  > paths is logged with a fixed message naming the step, `type(exc).__name__` and the
  > ids, which says *where* and *what class* failed. A redaction pass over `str(exc)` was
  > rejected: it would have to know every serial format the firmware might report, and
  > one it missed would leak.

- [ ] **Step 2 (#1012, finding 2): concurrent color staleness.** In §5 "Concurrent
  prints", after "There is no reservation between the read and `POST /queue/`." add:

  > The same holds for color: two requests ranked against one rack read can both prefer
  > the position holding their color, and the one dispatched second finds that hotend
  > holding the first print's color. Bambuddy re-checks diameter and flow at dispatch,
  > never color, so the second print loses only the purge saving. It does not fail.

- [ ] **Step 3 (#1012, finding 1): the regression test stays.** In §9, after the bullet
  "three groups over two shared positions...", add a sub-bullet:
  `- this test is a **permanent** regression test of the allocation order: a change to
  the order must change it deliberately (#1012).`

- [ ] **Step 4 (#1015, finding 1): read before write.** In §4, after the bullet that lists
  where `rack_nozzle_seen` is written, add:

  > Within `choose_rack`, `usage(serials)` is read **before** that read's
  > `rack_nozzle_seen` write (#1015). `prepare_run`'s read has normally recorded the
  > hotend already, so a hotend new to the print flow reaches the ranking with a
  > `first_seen_at` of moments ago. That is the newest of all, so it orders exactly as
  > `None` does: last for Oldest first, first for Newest first. `None` itself is reached
  > only when no earlier read recorded it.

- [ ] **Step 5 (#1015, finding 2): why picks use DO NOTHING.** In §4, after "`rack_nozzle_picks`
  and `rack_nozzle_prints` are written `ON CONFLICT DO NOTHING`, so a settle seen twice
  ... cannot count a print twice." add:

  > For `rack_nozzle_picks` a conflict is never expected: Bambuddy mints a new queue item
  > id per `POST /queue/`, and the write runs once per item. `DO NOTHING` is kept so a
  > replayed run cannot fail a print that is already queued. `record_picks` returns the
  > rows written, and the caller logs a warning when that is fewer than it sent, so a
  > bug that writes twice still surfaces (#1015).

- [ ] **Step 6 (#1016): manual picks are re-checked.** In §5, after the bullet "`manual` is
  `{group_id: position}`...", add:

  > - `rank_rack` re-checks each manual entry against the **sliced** group's diameter and
  >   flow, like any ranked pick (#1016). One that does not fit is not sent: that group is
  >   ranked like any other, and the run carries `rack-manual-partial` naming the position
  >   and the group. Both the 422 and the preview judge the flow the slice will carry,
  >   which is Standard while #484 is open, because that is the flow Bambuddy re-checks
  >   at dispatch.

- [ ] **Step 7: the plan's other rulings.** Add a new §10 at the end of the spec:

```markdown
## 10. Rulings made while planning (2026-10-02)

- §9 asks a `slice_and_queue` test for "a raise from it queues the item without one";
  §5 is the design: `slice_and_queue` awaits `choose_rack` bare, so a raise from the
  callback itself fails the plate with nothing queued, and the no-choice behavior is
  tested on a raise *inside* the callback's body.
- Under "Let Bambuddy pick" a manual pick is still sent for its group: it is the user's
  explicit choice for this print. The other groups get no pick.
- `PrintRunRequest.rack_algorithm` overrides the remembered algorithm for one print, so
  the dialog's preview matches its selector; the selector also remembers it per printer.
- `choose_rack` reads the requirements first; a plate with no `on_rack` group reads no
  status and warns of nothing (unless a manual pick was made: "manual rack pick unused").
- The rack-side hotend on the carriage (status id 0, `RACK_SIDE`) is the one missing
  position when exactly one of 16-21 is absent, as Bambuddy's `_rack_by_position`.
- A rack color whose alpha byte is `00` (the rack reports `00000000` for a hotend with
  no filament loaded) is no color: it never matches, not even a black group.
- `RackOption` also carries `nozzle_diameter` and `flow`; `RackPickView` carries
  `glow_unchecked`, and its `group_id` is `null` in the preview.
- An empty `serial_number` is skipped by `seen()`; a pick of such a hotend is sent but
  writes no `rack_nozzle_picks` row.
```

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md
git commit -m "docs(836): fold the #915 review findings and the plan's rulings into the spec" -m "Refs #1011, #1012, #1015, #1016"
```

---

### Task 3: Wire shapes and warning kinds

**Files:**
- Modify: `backend/scadbuddy/bambuddy/models.py` (`NozzleRackSlot` ~262, `FilamentRequirement` ~615, `QueueItemCreate.nozzle_rack_choice` ~786)
- Modify: `backend/scadbuddy/bambuddy/filaments.py` (`WarningKind` ~66)
- Test: `backend/tests/bambuddy/test_rack_models.py` (new)

**Interfaces:**
- Produces: `NozzleRackSlot.serial_number: str` (repr-hidden); `FilamentGroup(on_rack: bool,
  nozzle_diameter: str, volume_type: str, filament_color: str)`;
  `FilamentRequirement.group_id: int | None`, `.group: FilamentGroup | None`;
  `RackAlgorithm = Literal["least_used", "oldest_first", "newest_first", "bambuddy"]` in
  `bambuddy/models.py` (there, not in `rack/`, for the same reason `NozzleChoice` is: the
  settings store imports it, and `rack/rank.py` reaches the client, which imports the
  store); `WarningKind` gains `"rack-unsafe-material"`, `"rack-left-to-bambuddy"`,
  `"rack-manual-partial"`.

- [ ] **Step 1: Write the failing test**

```python
"""Bambuddy's rack and filament-group wire shapes (#836, spec 2026-10-01 §2, §6)."""

from __future__ import annotations

from typing import get_args

from scadbuddy.bambuddy.filaments import WarningKind
from scadbuddy.bambuddy.models import (
    FilamentRequirements,
    PrinterStatus,
    QueueItemCreate,
    RackAlgorithm,
)
from tests.bambuddy.conftest import recording


def test_a_rack_slot_carries_its_serial_and_hides_it_from_repr() -> None:
    body = recording("printer-status-rack.json")
    body["nozzle_rack"][2]["serial_number"] = "TEST-HOTEND-17"
    slot = PrinterStatus.model_validate(body).nozzle_rack[2]
    assert slot.id == 17
    assert slot.serial_number == "TEST-HOTEND-17"
    assert "TEST-HOTEND-17" not in repr(slot)


def test_a_sliced_files_requirements_carry_the_group() -> None:
    """As measured on library file 228 (spec §8 unknown 2)."""
    parsed = FilamentRequirements.model_validate(
        {
            "filaments": [
                {
                    "slot_id": 1,
                    "type": "PLA",
                    "color": "#00B1B7",
                    "group_id": 0,
                    "group": {
                        "on_rack": True,
                        "nozzle_diameter": "0.20",
                        "volume_type": "Standard",
                        "filament_color": "#00B1B7",
                    },
                }
            ]
        }
    )
    [filament] = parsed.filaments
    assert filament.group_id == 0
    assert filament.group is not None
    assert (filament.group.on_rack, filament.group.nozzle_diameter) == (True, "0.20")


def test_an_unsliced_upload_has_no_group() -> None:
    parsed = FilamentRequirements.model_validate(
        {"filaments": [{"slot_id": 1, "type": "", "group_id": None, "group": None}]}
    )
    assert (parsed.filaments[0].group_id, parsed.filaments[0].group) == (None, None)


def test_a_numeric_group_diameter_is_read_as_text() -> None:
    parsed = FilamentRequirements.model_validate(
        {"filaments": [{"slot_id": 1, "group_id": 0, "group": {"nozzle_diameter": 0.4}}]}
    )
    assert parsed.filaments[0].group is not None
    assert parsed.filaments[0].group.nozzle_diameter == "0.4"


def test_the_rack_choice_is_keyed_by_group_id_on_the_wire() -> None:
    item = QueueItemCreate(printer_id=1, library_file_id=77, nozzle_rack_choice={"0": 4})
    assert item.model_dump(mode="json", exclude_none=True)["nozzle_rack_choice"] == {"0": 4}


def test_the_rack_warning_kinds_and_algorithms_exist() -> None:
    kinds = set(get_args(WarningKind))
    assert {"rack-unsafe-material", "rack-left-to-bambuddy", "rack-manual-partial"} <= kinds
    assert get_args(RackAlgorithm) == ("least_used", "oldest_first", "newest_first", "bambuddy")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_rack_models.py -v`
Expected: FAIL with `ImportError: cannot import name 'RackAlgorithm'`.

- [ ] **Step 3: Implement**

In `models.py`, below `Tier = Literal[...]` (~282) add:

```python
#: How ScadBuddy ranks an H2C's nozzle rack (#836, spec 2026-10-01 §4). Here rather than
#: in ``scadbuddy/rack`` so the settings store can remember it per printer without
#: importing the ranking (which reaches the client, which imports the store).
RackAlgorithm = Literal["least_used", "oldest_first", "newest_first", "bambuddy"]
```

In `NozzleRackSlot`, after `filament_colour`, add:

```python
    #: The hotend's own serial (#836). It goes into the ``rack_nozzle_*`` tables and
    #: nowhere else (spec 2026-10-01 §7), so it is kept out of ``repr``.
    serial_number: str = Field(default="", repr=False)
```

Above `class FilamentRequirement`, add:

```python
class FilamentGroup(BambuddyModel):
    """A sliced filament's hotend group (#836): ``filament-requirements``' ``group``,
    read from the sliced 3MF. Measured 2026-10-01 on library file 228:
    ``{on_rack: true, nozzle_diameter: "0.20", volume_type: "Standard",
    filament_color: "#00B1B7"}``. An unsliced upload answers ``null`` (spec §5)."""

    on_rack: bool = False
    nozzle_diameter: str = ""
    volume_type: str = ""
    filament_color: str = ""

    @field_validator("nozzle_diameter", "volume_type", "filament_color", mode="before")
    @classmethod
    def _text(cls, value: Any) -> Any:
        return "" if value is None else str(value)
```

In `FilamentRequirement`, after `used_in_plate`, add:

```python
    #: The slicer's filament group (#836): one group is one hotend, and several
    #: filaments can share it. Both are ``None`` on an unsliced upload.
    group_id: int | None = None
    group: FilamentGroup | None = None
```

Replace the `#:` comment line above `nozzle_rack_choice` in `QueueItemCreate` with:

```python
    #: Rack position (1-6) per **filament group id**, the stringified ``group_id`` of
    #: ``filament-requirements`` on the sliced file (spec 2026-10-01 §2). Not an
    #: extruder index. Bambuddy re-checks it against the live rack at dispatch.
```

In `filaments.py`, extend `WarningKind` after `"hf-mounted",`:

```python
    "rack-unsafe-material",
    "rack-left-to-bambuddy",
    "rack-manual-partial",
```

- [ ] **Step 4: Run it to verify it passes**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_rack_models.py tests/bambuddy/test_client.py -v && uv run --frozen mypy`
Expected: PASS, mypy clean.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/models.py backend/scadbuddy/bambuddy/filaments.py backend/tests/bambuddy/test_rack_models.py
git commit -m "feat(rack): read hotend serials and filament groups, add the rack warning kinds" -m "Refs #836"
```

---

### Task 4: `rack/rank.py`, the pure ranking

**Files:**
- Create: `backend/scadbuddy/rack/__init__.py`, `backend/scadbuddy/rack/rank.py`
- Create: `backend/tests/rack/__init__.py`, `backend/tests/rack/helpers.py`
- Test: `backend/tests/rack/test_rank.py`

**Interfaces:**
- Consumes: Task 3's models and kinds.
- Produces (all in `scadbuddy.rack.rank`):
  - `RackAlgorithm` (re-exported), `DEFAULT_ALGORITHM: RackAlgorithm = "least_used"`,
    `SLICED_VOLUME_TYPE = "Standard"`, `NOZZLE_MATERIALS: dict[str, str] = {}`,
    `HARDENED_MATERIALS: frozenset[str]`, `FIRST_RACK_ID = 16`
  - `@dataclass(frozen=True) Usage(prints: int = 0, print_seconds: int = 0, grams: float = 0.0, first_seen_at: datetime | None = None)`
  - `@dataclass(frozen=True) RackGroup(group_id, nozzle_diameter, volume_type, color=None, materials=(), abrasive=False, glow_unchecked=False, label="")` with `.name`
  - `@dataclass(frozen=True) RackCandidate(position, nozzle_diameter, high_flow, nozzle_type, color, material, prints, print_seconds, key)` (no serial)
  - `@dataclass(frozen=True) Pick(group_id, position, serial, reason, unsafe_material, manual, candidates)` (serial repr-hidden)
  - `diameter(text) -> float | None`, `eligible(slot, nozzle_diameter, volume_type) -> bool`,
    `rack_positions(rack) -> dict[int, NozzleRackSlot]`, `nozzle_material(code) -> str | None`,
    `hardened(code) -> bool`, `abrasive_type(filament_type) -> bool`, `glow(*texts) -> bool`
  - `rack_groups(filaments: Sequence[FilamentRequirement], spools_by_slot: Mapping[int, SpoolOption]) -> list[RackGroup]`
  - `candidates_for(group, rack, algorithm, usage, taken=frozenset()) -> tuple[RackCandidate, ...]`
  - `rank_rack(groups, rack, algorithm, usage, manual) -> dict[int, Pick]` (spec §5 signature)
  - `manual_for(groups, position, algorithm) -> tuple[dict[int, int], list[FilamentWarning]]`
  - `rack_warnings(groups, rack, algorithm, picks, manual) -> list[FilamentWarning]`

- [ ] **Step 1: Write the test helpers**

`backend/tests/rack/__init__.py` is empty. `backend/tests/rack/helpers.py`:

```python
"""Invented rack fixtures (#836). Every serial here is made up (spec 2026-10-01 §7)."""

from __future__ import annotations

from typing import Any

from scadbuddy.bambuddy.models import (
    FilamentGroup,
    FilamentRequirement,
    NozzleRackSlot,
    PrinterStatus,
)
from scadbuddy.rack.rank import RackGroup
from tests.bambuddy.conftest import recording


def serial(rack_id: int) -> str:
    """An invented serial for the hotend at status id ``rack_id``."""
    return f"TEST-HOTEND-{rack_id:02d}"


#: Every invented serial :func:`invented_status` hands out.
INVENTED_SERIALS = [serial(rack_id) for rack_id in (0, 1, 17, 18, 19, 20, 21)]


def slot(
    position: int,
    nozzle_type: str = "HS01",
    diameter: str = "0.4",
    color: str = "000000FF",
    *,
    serial_number: str | None = None,
) -> NozzleRackSlot:
    rack_id = position + 15
    return NozzleRackSlot(
        id=rack_id,
        nozzle_type=nozzle_type,
        nozzle_diameter=diameter,
        filament_colour=color,
        serial_number=serial(rack_id) if serial_number is None else serial_number,
    )


def mounted(nozzle_type: str = "HS00", diameter: str = "0.2", color: str = "27272CFF") -> NozzleRackSlot:
    """The rack-side hotend on the carriage: status id 0 (``extruders.RACK_SIDE``)."""
    return NozzleRackSlot(
        id=0,
        nozzle_type=nozzle_type,
        nozzle_diameter=diameter,
        filament_colour=color,
        serial_number=serial(0),
    )


def group(
    group_id: int = 0,
    *,
    diameter: str = "0.40",
    volume: str = "Standard",
    color: str | None = None,
    materials: tuple[str, ...] = ("PLA",),
    abrasive: bool = False,
) -> RackGroup:
    return RackGroup(
        group_id=group_id,
        nozzle_diameter=diameter,
        volume_type=volume,
        color=color,
        materials=materials,
        abrasive=abrasive,
    )


def requirement(
    slot_id: int = 1,
    *,
    filament_type: str = "PLA",
    group_id: int | None = 0,
    diameter: str = "0.40",
    volume: str = "Standard",
    color: str = "#FF6A13",
    on_rack: bool = True,
    used: bool = True,
) -> FilamentRequirement:
    return FilamentRequirement(
        slot_id=slot_id,
        type=filament_type,
        color=color,
        used_in_plate=used,
        group_id=group_id,
        group=FilamentGroup(
            on_rack=on_rack, nozzle_diameter=diameter, volume_type=volume, filament_color=color
        ),
    )


def status(*rack: NozzleRackSlot) -> PrinterStatus:
    return PrinterStatus(id=1, name="H2C", connected=True, nozzle_rack=list(rack))


def invented_status() -> dict[str, Any]:
    """``printer-status-rack.json`` with each hotend's serial replaced by an invented one."""
    body: dict[str, Any] = recording("printer-status-rack.json")
    for entry in body["nozzle_rack"]:
        entry["serial_number"] = serial(entry["id"])
    return body
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/rack/test_rank.py`:

```python
"""``rank_rack`` and its helpers (#836, spec 2026-10-01 §3, §4, §5, §9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.rack import rank
from scadbuddy.rack.rank import (
    NOZZLE_MATERIALS,
    Usage,
    abrasive_type,
    candidates_for,
    eligible,
    manual_for,
    rack_groups,
    rack_positions,
    rack_warnings,
    rank_rack,
)
from tests.rack.helpers import group, mounted, requirement, serial, slot

#: A code the tests treat as hardened; the shipped table is empty (spec §8).
HARD = "HS99"


@pytest.fixture
def hardened_code(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setitem(NOZZLE_MATERIALS, HARD, "hardened steel")
    monkeypatch.setitem(NOZZLE_MATERIALS, "HS01", "brass")
    return HARD


def positions(picks: dict[int, rank.Pick]) -> dict[int, int]:
    return {group_id: pick.position for group_id, pick in picks.items()}


# --- eligibility ------------------------------------------------------------------


def test_a_rack_point_two_matches_a_group_point_two_zero() -> None:
    assert eligible(slot(2, "HS00", "0.2"), "0.20", "Standard")
    assert not eligible(slot(2, "HS00", "0.2"), "0.40", "Standard")


def test_high_flow_matches_only_hh_and_standard_only_the_rest() -> None:
    assert eligible(slot(2, "HH01"), "0.40", "High Flow")
    assert not eligible(slot(2, "HS01"), "0.40", "High Flow")
    assert eligible(slot(2, "HS01"), "0.40", " standard ")
    assert not eligible(slot(2, "HH01"), "0.40", "Standard")


def test_a_missing_code_or_flow_name_matches_either() -> None:
    assert eligible(slot(2, ""), "0.40", "High Flow")
    assert eligible(slot(2, ""), "0.40", "Standard")
    assert eligible(slot(2, "HH01"), "0.40", "")


def test_an_unparsable_diameter_is_never_eligible() -> None:
    assert not eligible(slot(2, "HS01", "abc"), "0.40", "Standard")
    assert not eligible(slot(2, "HS01", ""), "0.40", "Standard")
    assert not eligible(slot(2), "abc", "Standard")


def test_the_mounted_hotend_fills_the_one_missing_position_only() -> None:
    """Review Focus 3: recovered as Bambuddy's ``_rack_by_position`` does."""
    one_missing = [mounted(), *(slot(p) for p in (2, 3, 4, 5, 6))]
    assert rack_positions(one_missing)[1].id == 0
    two_missing = [mounted(), *(slot(p) for p in (3, 4, 5, 6))]
    found = rack_positions(two_missing)
    assert sorted(found) == [3, 4, 5, 6]
    assert all(entry.id != 0 for entry in found.values())


# --- materials --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filament_type", "abrasive"),
    [
        ("PLA-CF", True),
        ("PA6-CF", True),
        ("ABS-GF", True),
        ("petg cf", True),
        ("PLA", False),
        ("PLA-AERO", False),
        ("", False),
        (None, False),
    ],
)
def test_cf_and_gf_tokens_are_abrasive(filament_type: str | None, abrasive: bool) -> None:
    assert abrasive_type(filament_type) is abrasive


def test_the_material_table_ships_empty_so_every_code_is_unknown() -> None:
    assert NOZZLE_MATERIALS == {}
    picks = rank_rack(
        [group(abrasive=True, materials=("PLA-CF",))], [slot(2, "HH01"), slot(3, "HS01")],
        "least_used", {}, {},
    )
    assert picks[0].unsafe_material is True


def test_an_unknown_code_is_not_hardened(hardened_code: str) -> None:
    assert rank.hardened(hardened_code)
    assert not rank.hardened("HZ42")


def test_material_ranks_above_color(hardened_code: str) -> None:
    rack = [slot(2, "HS01", color="FF6A13FF"), slot(3, hardened_code)]
    abrasive = group(color="#FF6A13", abrasive=True, materials=("PLA-CF",))
    plain = group(color="#FF6A13")
    assert positions(rank_rack([abrasive], rack, "least_used", {}, {})) == {0: 3}
    # On a plate with no abrasive filament the brass nozzle wins even without the color.
    rack = [slot(2, hardened_code, color="FF6A13FF"), slot(3, "HS01")]
    assert positions(rank_rack([plain], rack, "least_used", {}, {})) == {0: 3}


def test_color_ranks_above_use() -> None:
    rack = [slot(2, color="FF6A13FF"), slot(3)]
    usage = {serial(17): Usage(prints=40, print_seconds=360_000)}
    picks = rank_rack([group(color="#FF6A13")], rack, "least_used", usage, {})
    assert positions(picks) == {0: 2}
    assert picks[0].reason == "already loaded with this color"


def test_a_hash_rgb_group_matches_an_rgba_slot_and_empty_never_matches() -> None:
    rack = [slot(2), slot(3, color="00B1B7FF")]
    assert positions(rank_rack([group(color="#00B1B7")], rack, "least_used", {}, {})) == {0: 3}
    rack = [slot(2, color=""), slot(3, color="")]
    picks = rank_rack([group(color=None)], rack, "least_used", {}, {})
    assert picks[0].reason == "lowest free position"


def test_an_empty_hotends_zero_alpha_color_never_matches_black() -> None:
    """Review Focus 1: the rack reports ``00000000`` for a hotend with no filament."""
    rack = [slot(2, color="00000000"), slot(3, color="000000FF")]
    picks = rank_rack([group(color="#000000")], rack, "least_used", {}, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "already loaded with this color"


# --- algorithms -------------------------------------------------------------------


def test_least_used_orders_by_print_seconds_then_prints() -> None:
    rack = [slot(2), slot(3), slot(4)]
    usage = {
        serial(17): Usage(prints=3, print_seconds=500),
        serial(18): Usage(prints=1, print_seconds=500),
        serial(19): Usage(prints=1, print_seconds=900),
    }
    picks = rank_rack([group()], rack, "least_used", usage, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "least used"


def test_an_unused_hotend_ranks_before_a_used_one() -> None:
    """Spec §4: the ``coalesce`` gives a never-used serial zeros, never a missing entry."""
    rack = [slot(2), slot(3)]
    usage = {serial(17): Usage(prints=1, print_seconds=60), serial(18): Usage()}
    assert positions(rank_rack([group()], rack, "least_used", usage, {})) == {0: 3}


def test_oldest_and_newest_first_order_on_first_seen_with_the_unseen_last_and_first() -> None:
    rack = [slot(2), slot(3), slot(4)]
    usage = {
        serial(17): Usage(first_seen_at=datetime(2026, 9, 1, tzinfo=UTC)),
        serial(18): Usage(first_seen_at=datetime(2026, 8, 1, tzinfo=UTC)),
        serial(19): Usage(first_seen_at=None),
    }
    oldest = candidates_for(group(), rack, "oldest_first", usage)
    newest = candidates_for(group(), rack, "newest_first", usage)
    assert [c.position for c in oldest] == [3, 2, 4]
    assert [c.position for c in newest] == [4, 2, 3]


def test_the_position_breaks_every_other_tie() -> None:
    picks = rank_rack([group()], [slot(5), slot(3), slot(4)], "least_used", {}, {})
    assert positions(picks) == {0: 3}
    assert picks[0].reason == "lowest free position"


def test_let_bambuddy_pick_sends_no_choice() -> None:
    assert rank_rack([group()], [slot(2), slot(3)], "bambuddy", {}, {}) == {}


def test_let_bambuddy_pick_warns_whenever_bambuddy_could_take_a_non_hardened_nozzle(
    hardened_code: str,
) -> None:
    cf = group(abrasive=True, materials=("PLA-CF",))

    def kinds(*rack: rank.NozzleRackSlot) -> list[str]:
        return [w.kind for w in rack_warnings([cf], list(rack), "bambuddy", {}, {})]

    assert kinds(slot(2), slot(3)) == ["rack-unsafe-material"]
    assert kinds(slot(2, hardened_code), slot(3)) == ["rack-unsafe-material"]
    assert kinds(slot(2, hardened_code), slot(3, hardened_code)) == []


# --- allocation -------------------------------------------------------------------


def test_no_eligible_position_gives_no_pick_and_says_so() -> None:
    picks = rank_rack([group(diameter="0.60")], [slot(2), slot(3)], "least_used", {}, {})
    assert picks == {}
    [warning] = rack_warnings([group(diameter="0.60")], [slot(2)], "least_used", picks, {})
    assert warning.kind == "rack-left-to-bambuddy"
    assert warning.slot_id is None
    assert warning.message == "Rack pick left to Bambuddy: no eligible position for group 0."


def test_an_unparsable_group_diameter_gives_no_pick_and_says_why() -> None:
    bad = group(diameter="abc")
    assert rank_rack([bad], [slot(2)], "least_used", {}, {}) == {}
    [warning] = rack_warnings([bad], [slot(2)], "least_used", {}, {})
    assert warning.message == "Rack pick left to Bambuddy: group 0: nozzle size unreadable."


def test_two_groups_never_share_a_position_and_the_constrained_one_goes_first() -> None:
    # Group 1 (High Flow) can use only position 2, whose code is missing; group 0
    # (Standard) can use 2 or 3. By group id alone, group 0 would take 2.
    rack = [slot(2, ""), slot(3, "HS01")]
    picks = rank_rack([group(0), group(1, volume="High Flow")], rack, "least_used", {}, {})
    assert positions(picks) == {0: 3, 1: 2}


def test_the_dynamic_recount_beats_a_static_sort() -> None:
    """After group 1 takes position 4, group 0's count drops to tie group 2's, and group
    0 goes first by id. A static sort on the initial counts (2: 3, 0: 4) would have run
    group 2 first and given it the red hotend at position 1."""
    rack = [slot(1, color="FF0000FF"), slot(2), slot(3), slot(4, "HH01")]
    groups = [
        group(0, volume="", color="#FF0000"),  # any flow: {1, 2, 3, 4}
        group(1, volume="High Flow"),  # {4}
        group(2, color="#FF0000"),  # Standard: {1, 2, 3}
    ]
    assert positions(rank_rack(groups, rack, "least_used", {}, {})) == {1: 4, 0: 1, 2: 2}


def test_three_groups_over_two_positions_starve_the_last_deterministically() -> None:
    """PERMANENT regression test of the allocation order (spec §3, §9; #1012). The
    heuristic is most-constrained-first, not an optimal assignment; if a change to the
    order changes these picks, change this test deliberately, never to make it pass."""
    rack = [slot(2), slot(4)]
    groups = [group(0), group(1), group(2)]
    first = rank_rack(groups, rack, "least_used", {}, {})
    assert positions(first) == {0: 2, 1: 4}
    assert positions(rank_rack(groups, rack, "least_used", {}, {})) == positions(first)
    [warning] = rack_warnings(groups, rack, "least_used", first, {})
    assert warning.message == "Rack pick left to Bambuddy: no eligible position for group 2."


# --- manual picks -----------------------------------------------------------------


def test_a_manual_pick_is_reserved_before_the_ranking() -> None:
    rack = [slot(2, color="FF6A13FF"), slot(3)]
    groups = [group(0, color="#FF6A13"), group(1, color="#FF6A13")]
    picks = rank_rack(groups, rack, "least_used", {}, {1: 2})
    assert positions(picks) == {0: 3, 1: 2}
    assert picks[1].manual and picks[1].reason == "chosen by hand"


def test_a_manual_pick_that_does_not_fit_the_sliced_group_is_ranked_instead() -> None:
    """#1016: the manual entry is re-checked against the sliced group, like any pick."""
    rack = [slot(2), slot(3, "HH01")]
    picks = rank_rack([group(0)], rack, "least_used", {}, {0: 3})
    assert positions(picks) == {0: 2} and not picks[0].manual
    [warning] = rack_warnings([group(0)], rack, "least_used", picks, {0: 3})
    assert warning.kind == "rack-manual-partial"
    assert warning.message == (
        "Rack position 3 does not fit group 0 (0.4 mm Standard), so ScadBuddy chose "
        "position 2 instead."
    )


def test_a_manual_non_hardened_pick_for_an_abrasive_group_is_sent_and_warned() -> None:
    cf = group(abrasive=True, materials=("PLA-CF",))
    picks = rank_rack([cf], [slot(2), slot(3)], "least_used", {}, {0: 3})
    assert positions(picks) == {0: 3} and picks[0].unsafe_material
    [warning] = rack_warnings([cf], [slot(2), slot(3)], "least_used", picks, {0: 3})
    assert warning.kind == "rack-unsafe-material"
    assert warning.message == (
        "Position 3, chosen by hand, is not known to be hardened, and PLA-CF is abrasive."
    )


def test_a_manual_pick_under_let_bambuddy_pick_is_still_sent() -> None:
    assert positions(rank_rack([group()], [slot(2), slot(3)], "bambuddy", {}, {0: 3})) == {0: 3}


def test_with_two_groups_the_manual_pick_goes_to_the_lower_id() -> None:
    manual, notes = manual_for([group(3), group(1)], 4, "least_used")
    assert manual == {1: 4}
    [note] = notes
    assert note.kind == "rack-manual-partial"
    assert note.message == (
        "The slice put the rack side in 2 nozzle groups: group 1 got position 4, and "
        "ScadBuddy ranked group 3."
    )


def test_a_manual_pick_on_a_plate_with_no_rack_group_is_unused() -> None:
    manual, notes = manual_for([], 4, "least_used")
    assert manual == {}
    assert [(n.kind, n.message) for n in notes] == [
        (
            "rack-left-to-bambuddy",
            "Rack pick left to Bambuddy: manual rack pick unused: this plate does not "
            "print from the rack.",
        )
    ]


def test_no_manual_pick_means_nothing_to_say() -> None:
    assert manual_for([group()], None, "least_used") == ({}, [])


# --- grouping and abrasive --------------------------------------------------------


def _spool(material: str, subtype: str | None = None) -> SpoolOption:
    return SpoolOption(spool_id=1, material=material, subtype=subtype)


def test_two_filaments_sharing_a_group_id_are_one_group() -> None:
    groups = rack_groups(
        [requirement(1, color="#FF6A13"), requirement(2, color="#00B1B7")],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert [(g.group_id, g.color) for g in groups] == [(0, "#FF6A13")]


def test_a_group_of_pla_and_pla_cf_is_abrasive() -> None:
    [found] = rack_groups(
        [requirement(1), requirement(2, filament_type="PLA-CF")],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert found.abrasive and found.materials == ("PLA", "PLA-CF")


def test_an_unused_cf_filament_does_not_make_its_group_abrasive() -> None:
    [found] = rack_groups(
        [requirement(1), requirement(2, filament_type="PLA-CF", used=False)],
        {1: _spool("PLA"), 2: _spool("PLA")},
    )
    assert not found.abrasive


@pytest.mark.parametrize("subtype", ["Glow", "glow", "GLOW"])
def test_a_pla_group_on_a_glow_spool_is_abrasive(subtype: str) -> None:
    [found] = rack_groups([requirement(1)], {1: _spool("PLA", subtype)})
    assert found.abrasive and not found.glow_unchecked


def test_a_filament_with_no_spool_is_judged_by_type_and_says_glow_was_unchecked() -> None:
    [found] = rack_groups([requirement(1)], {})
    assert not found.abrasive and found.glow_unchecked


def test_off_rack_and_ungrouped_filaments_are_not_rack_groups() -> None:
    assert rack_groups(
        [requirement(1, on_rack=False), requirement(2, group_id=None)], {}
    ) == []


def test_no_candidate_carries_a_serial() -> None:
    picks = rank_rack([group()], [slot(2), slot(3)], "least_used", {}, {})
    assert serial(17) not in repr(picks)
    assert all(not hasattr(c, "serial") for c in picks[0].candidates)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/rack/test_rank.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'scadbuddy.rack'`.

- [ ] **Step 4: Implement**

`backend/scadbuddy/rack/__init__.py`:

```python
"""Rack nozzle selection on the Bambu H2C (#836)."""
```

`backend/scadbuddy/rack/rank.py`:

```python
"""Ranking the H2C's nozzle rack for a plate's rack groups (#836).

Spec ``docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md`` §3-§5. Pure:
no I/O and no logging. A serial rides on :class:`Pick` only (spec §7), never on a
:class:`RackCandidate`, so a view built from the candidates cannot carry one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime

from scadbuddy.bambuddy.extruders import RACK_SIDE
from scadbuddy.bambuddy.filaments import (
    FilamentWarning,
    SpoolOption,
    WarningKind,
    normalise_colour,
)
from scadbuddy.bambuddy.models import FilamentRequirement, NozzleRackSlot, RackAlgorithm

__all__ = ["RackAlgorithm"]

DEFAULT_ALGORITHM: RackAlgorithm = "least_used"
#: The flow every slice carries while Bambuddy has no High Flow presets (#484): the
#: preview and the manual pick's 422 judge it, since Bambuddy re-checks a pick against
#: the sliced group at dispatch.
SLICED_VOLUME_TYPE = "Standard"
#: ``nozzle_type`` code -> material, from the hotends' own labels (spec §8 unknown 1).
#: Ships EMPTY: every code then counts as not hardened, so an abrasive group always
#: carries ``rack-unsafe-material`` and never gets a silent brass pick.
NOZZLE_MATERIALS: dict[str, str] = {}
HARDENED_MATERIALS: frozenset[str] = frozenset({"hardened steel", "tungsten carbide"})
ABRASIVE_TOKENS: frozenset[str] = frozenset({"CF", "GF"})
#: Rack ids 16-21 are positions 1-6 (spec §2).
FIRST_RACK_ID = 16
POSITIONS = range(1, 7)
_SPLIT = re.compile(r"[-\s]+")

REASON_MANUAL = "chosen by hand"
REASON_ONLY = "the only eligible position"
REASON_COLOR = "already loaded with this color"
REASON_POSITION = "lowest free position"
_ALGORITHM_REASONS: dict[RackAlgorithm, str] = {
    "least_used": "least used",
    "oldest_first": "oldest hotend",
    "newest_first": "newest hotend",
    "bambuddy": REASON_POSITION,
}


@dataclass(frozen=True)
class Usage:
    """One serial's use (spec §4). A serial with no rows is all zeros, never missing."""

    prints: int = 0
    print_seconds: int = 0
    grams: float = 0.0
    first_seen_at: datetime | None = None


@dataclass(frozen=True)
class RackGroup:
    """One ``on_rack`` filament group of a sliced plate, or the preview's rack side."""

    group_id: int
    nozzle_diameter: str
    volume_type: str
    color: str | None = None
    materials: tuple[str, ...] = ()
    abrasive: bool = False
    #: A filament had no inventory spool, so Glow could not be checked (spec §3).
    glow_unchecked: bool = False
    #: How a warning names it; empty means "group <id>".
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or f"group {self.group_id}"


@dataclass(frozen=True)
class RackCandidate:
    """An eligible position, ranked. Carries no serial (spec §5)."""

    position: int
    nozzle_diameter: str
    high_flow: bool
    nozzle_type: str
    color: str | None
    material: str | None
    prints: int
    print_seconds: int
    #: (material, color, algorithm, algorithm, position): lower is better.
    key: tuple[float, ...]


@dataclass(frozen=True)
class Pick:
    """A position chosen for a group. Internal: the serial never leaves the backend."""

    group_id: int
    position: int
    serial: str = field(repr=False)
    reason: str
    unsafe_material: bool
    manual: bool
    candidates: tuple[RackCandidate, ...]


def diameter(text: str | None) -> float | None:
    """``round(float(x), 2)``; ``"0.2"`` and ``"0.20"`` are one size (spec §3)."""
    try:
        return round(float(text or ""), 2)
    except ValueError:
        return None


def _wants_high_flow(volume_type: str) -> bool:
    return volume_type.strip().lower().startswith("high flow")


def eligible(slot: NozzleRackSlot, nozzle_diameter: str, volume_type: str) -> bool:
    """Bambuddy's ``_rack_slot_is_eligible``: same diameter, and the same flow when both
    the slot's code and the group's flow name are present (spec §3)."""
    want, have = diameter(nozzle_diameter), diameter(slot.nozzle_diameter)
    if want is None or have is None or want != have:
        return False
    if len(slot.nozzle_type) < 2 or not volume_type.strip():
        return True
    return slot.high_flow == _wants_high_flow(volume_type)


def rack_positions(rack: Sequence[NozzleRackSlot]) -> dict[int, NozzleRackSlot]:
    """Position -> hotend. Ids 16-21 are positions 1-6. The firmware omits the id of the
    hotend on the carriage; when exactly one position is missing, the rack-side carriage
    hotend (id ``RACK_SIDE``) is that position, as Bambuddy's ``_rack_by_position``."""
    found = {
        entry.id - FIRST_RACK_ID + 1: entry
        for entry in rack
        if entry.id - FIRST_RACK_ID + 1 in POSITIONS
    }
    missing = [position for position in POSITIONS if position not in found]
    on_carriage = next((entry for entry in rack if entry.id == RACK_SIDE), None)
    if len(missing) == 1 and on_carriage is not None:
        found[missing[0]] = on_carriage
    return dict(sorted(found.items()))


def nozzle_material(code: str) -> str | None:
    return NOZZLE_MATERIALS.get(code)


def hardened(code: str) -> bool:
    """A code not in the table counts as not hardened (spec §3)."""
    return nozzle_material(code) in HARDENED_MATERIALS


def abrasive_type(filament_type: str | None) -> bool:
    """A ``CF`` or ``GF`` token in the type, split on ``-`` and spaces (spec §3)."""
    return any(token.upper() in ABRASIVE_TOKENS for token in _SPLIT.split(filament_type or ""))


def glow(*texts: str | None) -> bool:
    """``glow`` in a spool's material or subtype, case-insensitively (spec §3)."""
    return any("glow" in (text or "").lower() for text in texts)


def _slot_colour(raw: str) -> str | None:
    """A rack color, or ``None`` for a zero alpha: the rack reports ``00000000`` for a
    hotend with no filament loaded, which is no color rather than black."""
    text = raw.strip().lstrip("#")
    if len(text) == 8 and text[6:].upper() == "00":
        return None
    return normalise_colour(raw)


def rack_groups(
    filaments: Sequence[FilamentRequirement], spools_by_slot: Mapping[int, SpoolOption]
) -> list[RackGroup]:
    """One :class:`RackGroup` per ``on_rack`` group of the plate's used filaments. A group
    is abrasive when any of its filaments is; its color is the group's own (its first
    filament's, spec §8 unknown 2)."""
    grouped: dict[int, list[FilamentRequirement]] = {}
    for filament in filaments:
        if (
            filament.used_in_plate
            and filament.group_id is not None
            and filament.group is not None
            and filament.group.on_rack
        ):
            grouped.setdefault(filament.group_id, []).append(filament)
    groups: list[RackGroup] = []
    for group_id, members in sorted(grouped.items()):
        shape = members[0].group
        if shape is None:  # narrowed above; keeps mypy honest without an assert
            continue
        spools = [spools_by_slot.get(member.slot_id) for member in members]
        groups.append(
            RackGroup(
                group_id=group_id,
                nozzle_diameter=shape.nozzle_diameter,
                volume_type=shape.volume_type,
                color=shape.filament_color or None,
                materials=tuple(dict.fromkeys(m.type for m in members if m.type)),
                abrasive=any(abrasive_type(member.type) for member in members)
                or any(spool is not None and glow(spool.material, spool.subtype) for spool in spools),
                glow_unchecked=any(spool is None for spool in spools),
            )
        )
    return groups


def _available(
    group: RackGroup, positions: Mapping[int, NozzleRackSlot], taken: Set[int]
) -> dict[int, NozzleRackSlot]:
    return {
        position: entry
        for position, entry in positions.items()
        if position not in taken and eligible(entry, group.nozzle_diameter, group.volume_type)
    }


def _algorithm_key(algorithm: RackAlgorithm, use: Usage) -> tuple[float, float]:
    if algorithm == "least_used":
        return float(use.print_seconds), float(use.prints)
    seen = use.first_seen_at
    if algorithm == "oldest_first":
        return (1.0, 0.0) if seen is None else (0.0, seen.timestamp())
    if algorithm == "newest_first":
        return (0.0, 0.0) if seen is None else (1.0, -seen.timestamp())
    return 0.0, 0.0


def _rank(
    group: RackGroup,
    positions: Mapping[int, NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    taken: Set[int],
) -> tuple[RackCandidate, ...]:
    want = normalise_colour(group.color)
    found: list[RackCandidate] = []
    for position, entry in _available(group, positions, taken).items():
        use = usage.get(entry.serial_number, Usage()) if entry.serial_number else Usage()
        have = _slot_colour(entry.filament_colour)
        found.append(
            RackCandidate(
                position=position,
                nozzle_diameter=entry.nozzle_diameter,
                high_flow=entry.high_flow,
                nozzle_type=entry.nozzle_type,
                color=have,
                material=nozzle_material(entry.nozzle_type),
                prints=use.prints,
                print_seconds=use.print_seconds,
                key=(
                    0.0 if hardened(entry.nozzle_type) == group.abrasive else 1.0,
                    0.0 if want is not None and want == have else 1.0,
                    *_algorithm_key(algorithm, use),
                    float(position),
                ),
            )
        )
    return tuple(sorted(found, key=lambda candidate: candidate.key))


def candidates_for(
    group: RackGroup,
    rack: Sequence[NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    taken: Set[int] = frozenset(),
) -> tuple[RackCandidate, ...]:
    """Every eligible free position for ``group``, best first."""
    return _rank(group, rack_positions(rack), algorithm, usage, taken)


def _reason(group: RackGroup, candidates: Sequence[RackCandidate], algorithm: RackAlgorithm) -> str:
    """The first rule that set the pick apart from the runner-up."""
    if len(candidates) == 1:
        return REASON_ONLY
    best, runner = candidates[0].key, candidates[1].key
    index = next(i for i, (a, b) in enumerate(zip(best, runner, strict=True)) if a != b)
    if index == 0:
        if group.abrasive:
            return f"hardened nozzle for {_materials(group)}"
        return "keeps the hardened nozzles for abrasive filament"
    if index == 1:
        return REASON_COLOR
    if index in (2, 3):
        return _ALGORITHM_REASONS[algorithm]
    return REASON_POSITION


def rank_rack(
    groups: list[RackGroup],
    rack: list[NozzleRackSlot],
    algorithm: RackAlgorithm,
    usage: Mapping[str, Usage],
    manual: Mapping[int, int],
) -> dict[int, Pick]:
    """Picks by group id (spec §3, §5). Manual positions are placed first, each only where
    it fits its sliced group (#1016); then the remaining groups are allocated one at a
    time, the one with the fewest free eligible positions next (ties by group id),
    re-counted before each allocation. A group with no free eligible position has no
    pick. "Let Bambuddy pick" ranks nothing; a manual pick is still kept."""
    positions = rack_positions(rack)
    by_id = {group.group_id: group for group in groups}
    picks: dict[int, Pick] = {}
    taken: set[int] = set()
    for group_id, position in sorted(manual.items()):
        group = by_id.get(group_id)
        chosen = positions.get(position)
        if group is None or chosen is None or position in taken:
            continue
        if not eligible(chosen, group.nozzle_diameter, group.volume_type):
            continue
        candidates = _rank(group, positions, algorithm, usage, frozenset(taken))
        taken.add(position)
        picks[group_id] = Pick(
            group_id=group_id,
            position=position,
            serial=chosen.serial_number,
            reason=REASON_MANUAL,
            unsafe_material=group.abrasive and not hardened(chosen.nozzle_type),
            manual=True,
            candidates=candidates,
        )
    if algorithm == "bambuddy":
        return picks
    remaining = [group for group in groups if group.group_id not in picks]
    while remaining:
        group = min(
            remaining,
            key=lambda g: (len(_available(g, positions, taken)), g.group_id),
        )
        remaining.remove(group)
        candidates = _rank(group, positions, algorithm, usage, frozenset(taken))
        if not candidates:
            continue
        best = positions[candidates[0].position]
        taken.add(candidates[0].position)
        picks[group.group_id] = Pick(
            group_id=group.group_id,
            position=candidates[0].position,
            serial=best.serial_number,
            reason=_reason(group, candidates, algorithm),
            unsafe_material=group.abrasive and not hardened(best.nozzle_type),
            manual=False,
            candidates=candidates,
        )
    return picks


def _warning(kind: WarningKind, message: str) -> FilamentWarning:
    """Every rack warning is plate-wide: ``slot_id`` stays ``None`` (spec §6)."""
    return FilamentWarning(kind=kind, message=message)


def _materials(group: RackGroup) -> str:
    return ", ".join(group.materials) or "this filament"


def _size(group: RackGroup) -> str:
    size = diameter(group.nozzle_diameter)
    return f"{size:g}" if size is not None else group.nozzle_diameter


def manual_for(
    groups: Sequence[RackGroup], position: int | None, algorithm: RackAlgorithm
) -> tuple[dict[int, int], list[FilamentWarning]]:
    """The run's ``manual`` map from the dialog's one rack-side position (spec §5)."""
    if position is None:
        return {}, []
    if not groups:
        return {}, [
            _warning(
                "rack-left-to-bambuddy",
                "Rack pick left to Bambuddy: manual rack pick unused: this plate does not "
                "print from the rack.",
            )
        ]
    ids = sorted(group.group_id for group in groups)
    first, rest = ids[0], ids[1:]
    if not rest:
        return {first: position}, []
    then = "Bambuddy picks for" if algorithm == "bambuddy" else "ScadBuddy ranked"
    noun = "group" if len(rest) == 1 else "groups"
    others = ", ".join(str(group_id) for group_id in rest)
    return {first: position}, [
        _warning(
            "rack-manual-partial",
            f"The slice put the rack side in {len(ids)} nozzle groups: group {first} got "
            f"position {position}, and {then} {noun} {others}.",
        )
    ]


def rack_warnings(
    groups: Sequence[RackGroup],
    rack: Sequence[NozzleRackSlot],
    algorithm: RackAlgorithm,
    picks: Mapping[int, Pick],
    manual: Mapping[int, int],
) -> list[FilamentWarning]:
    """What the run carries about each group's pick (spec §5, §6)."""
    positions = rack_positions(rack)
    found: list[FilamentWarning] = []
    for group in sorted(groups, key=lambda g: g.group_id):
        pick = picks.get(group.group_id)
        wanted = manual.get(group.group_id)
        if wanted is not None and (pick is None or not pick.manual):
            flow = group.volume_type.strip() or "any flow"
            outcome = (
                f"ScadBuddy chose position {pick.position} instead."
                if pick is not None
                else "it was not used."
            )
            found.append(
                _warning(
                    "rack-manual-partial",
                    f"Rack position {wanted} does not fit {group.name} ({_size(group)} mm "
                    f"{flow}), so {outcome}",
                )
            )
        if pick is not None:
            if pick.unsafe_material:
                material = nozzle_material(positions[pick.position].nozzle_type)
                held = f"is {material}" if material else "is not known to be hardened"
                found.append(
                    _warning(
                        "rack-unsafe-material",
                        f"Position {pick.position}, chosen by hand, {held}, and "
                        f"{_materials(group)} is abrasive."
                        if pick.manual
                        else f"No hardened {_size(group)} nozzle in the rack for "
                        f"{_materials(group)}; position {pick.position} {held}.",
                    )
                )
            continue
        if algorithm == "bambuddy":
            options = _available(group, positions, frozenset())
            if group.abrasive and any(not hardened(e.nozzle_type) for e in options.values()):
                found.append(
                    _warning(
                        "rack-unsafe-material",
                        "Bambuddy picks the nozzle and may use a non-hardened one for "
                        f"{_materials(group)}.",
                    )
                )
            continue
        if diameter(group.nozzle_diameter) is None:
            message = f"Rack pick left to Bambuddy: {group.name}: nozzle size unreadable."
        else:
            message = f"Rack pick left to Bambuddy: no eligible position for {group.name}."
        found.append(_warning("rack-left-to-bambuddy", message))
    return found
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/rack/test_rank.py -v && uv run --frozen mypy && uv run --frozen ruff check . && uv run --frozen ruff format --check .`
Expected: PASS. If `ruff format --check` reports the new files, run `uv run --frozen ruff format scadbuddy/rack tests/rack` and re-run.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/rack/__init__.py backend/scadbuddy/rack/rank.py backend/tests/rack/__init__.py backend/tests/rack/helpers.py backend/tests/rack/test_rank.py
git commit -m "feat(rack): rank the H2C nozzle rack per filament group" -m "Refs #836, #1012, #1016"
```

---

### Task 5: Migration, `RackUsageStore` and its component

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC timestamp>_rack_nozzle_usage.sql`
- Create: `backend/scadbuddy/rack/usage.py`, `backend/scadbuddy/rack/component.py`
- Test: `backend/tests/rack/test_usage.py`

**Interfaces:**
- Consumes: `rank.Usage`, `rank.rack_positions`.
- Produces (in `scadbuddy.rack.usage`):
  - `class PickedHotend(BaseModel)`: `group_id: int`, `position: int`, `serial: str` (repr-hidden)
  - `class RackUsage(Protocol)`: `seen(printer_id: int, serials: Iterable[str]) -> None`,
    `usage(serials: Iterable[str]) -> dict[str, Usage]`,
    `record_picks(queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]) -> int`,
    `picked_items(queue_item_ids: Iterable[int]) -> set[int]`,
    `record_prints(*, archive_id: int, queue_item_id: int, settled_at: datetime, print_seconds: int | None, grams: float | None) -> int` (all `async`)
  - `class RackUsageStore` implementing it, with `close()`
  - In `scadbuddy.rack.component`: `RACK_USAGE: Key[RackUsageStore]`, `COMPONENT`, `RackUsageDep`

- [ ] **Step 1: Write the failing tests**

`backend/tests/rack/test_usage.py`:

```python
"""``RackUsageStore`` on Postgres (#836, spec 2026-10-01 §4). Invented serials only."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import psycopg
import pytest

from scadbuddy.rack.rank import Usage, rank_rack
from scadbuddy.rack.usage import PickedHotend, RackUsageStore
from tests.rack.helpers import group, serial, slot

pytestmark = pytest.mark.requires_postgres

AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
A, B = serial(17), serial(18)


@pytest.fixture
async def store(pg_conninfo: str) -> AsyncIterator[RackUsageStore]:
    opened = RackUsageStore(pg_conninfo)
    try:
        yield opened
    finally:
        opened.close()


def _seen_rows(conninfo: str) -> dict[str, tuple[int, datetime]]:
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute("SELECT serial, printer_id, first_seen_at FROM rack_nozzle_seen").fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


async def test_a_hotend_moved_to_another_printer_keeps_its_age_and_history(
    store: RackUsageStore, pg_conninfo: str
) -> None:
    await store.seen(1, [A])
    first = _seen_rows(pg_conninfo)[A]
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    await store.record_prints(archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=600, grams=12.5)

    await store.seen(2, [A])

    printer_id, first_seen = _seen_rows(pg_conninfo)[A]
    assert (printer_id, first_seen) == (2, first[1])
    assert (await store.usage([A]))[A] == Usage(prints=1, print_seconds=600, grams=12.5, first_seen_at=first[1])


async def test_seen_skips_empty_and_repeated_serials(store: RackUsageStore, pg_conninfo: str) -> None:
    """Review Focus 4."""
    await store.seen(1, ["", A, A, ""])
    assert list(_seen_rows(pg_conninfo)) == [A]


async def test_a_serial_with_no_rows_is_zeros_and_ranks_before_a_used_one(store: RackUsageStore) -> None:
    """Spec §4: ``sum`` over no rows is NULL, which an ascending sort puts last."""
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    await store.record_prints(archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=60, grams=None)

    usage = await store.usage([A, B])

    assert usage[B] == Usage()
    assert usage[A].prints == 1 and usage[A].grams == 0.0
    picks = rank_rack([group()], [slot(2), slot(3)], "least_used", usage, {})
    assert picks[0].position == 3


async def test_a_pick_written_twice_writes_once_and_says_so(store: RackUsageStore) -> None:
    """#1015: never expected, but a second write is visible in the count, not an error."""
    picks = [PickedHotend(group_id=0, position=2, serial=A), PickedHotend(group_id=1, position=3, serial=B)]
    assert await store.record_picks(51, 1, picks) == 2
    assert await store.record_picks(51, 1, picks) == 0
    assert await store.picked_items([51, 52]) == {51}


async def test_a_print_copies_the_picks_serial_and_a_second_settle_changes_nothing(
    store: RackUsageStore,
) -> None:
    await store.record_picks(51, 1, [PickedHotend(group_id=0, position=2, serial=A)])
    assert await store.record_prints(archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=60, grams=3.0) == 1
    assert await store.record_prints(archive_id=101, queue_item_id=51, settled_at=AT, print_seconds=99, grams=9.0) == 0
    assert (await store.usage([A]))[A].print_seconds == 60


async def test_an_item_with_no_picks_records_no_print(store: RackUsageStore) -> None:
    assert await store.record_prints(archive_id=101, queue_item_id=77, settled_at=AT, print_seconds=60, grams=1.0) == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/rack/test_usage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scadbuddy.rack.usage'`.
(With no `SCADBUDDY_TEST_DATABASE_URL` they skip. Start the database first.)

- [ ] **Step 3: Write the migration**

```bash
cd backend && f="scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_rack_nozzle_usage.sql" && echo "$f"
```

Write this content to that path:

```sql
-- #836: rack hotend usage (spec docs/superpowers/specs/2026-10-01-rack-nozzle-selection-design.md §4).
-- Serials live in these three tables and nowhere else (spec §7).
CREATE TABLE rack_nozzle_seen (
    serial        text        PRIMARY KEY,
    printer_id    integer     NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rack_nozzle_picks (
    queue_item_id integer     NOT NULL,
    group_id      integer     NOT NULL,
    printer_id    integer     NOT NULL,
    serial        text        NOT NULL,
    picked_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (queue_item_id, group_id)
);

CREATE TABLE rack_nozzle_prints (
    archive_id    integer     NOT NULL,
    group_id      integer     NOT NULL,
    serial        text        NOT NULL,
    settled_at    timestamptz NOT NULL,
    print_seconds bigint,
    grams         numeric,
    PRIMARY KEY (archive_id, group_id)
);

CREATE INDEX rack_nozzle_prints_serial ON rack_nozzle_prints (serial);
```

- [ ] **Step 4: Implement the store**

`backend/scadbuddy/rack/usage.py`:

```python
"""Rack hotend usage (#836, spec 2026-10-01 §4): ``rack_nozzle_seen``,
``rack_nozzle_picks`` and ``rack_nozzle_prints`` (``migrations/*_rack_nozzle_usage.sql``).

The one store that owns the three tables. It connects on first use and applies the
backend's migrations itself, as ``analyzers.decisions.PostgresDecisionStore`` does. Every
public method is a coroutine that runs its query in a worker thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from psycopg import Connection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from scadbuddy.rack.rank import Usage
from scadbuddy.render.pg_store import migrate

#: As the decision store's: a request is told the store is unavailable rather than hang.
CONNECT_TIMEOUT = 5.0


class PickedHotend(BaseModel):
    """A sent pick with the hotend it named. Carries a serial: backend only (spec §7)."""

    group_id: int
    position: int
    serial: str = Field(repr=False)


class RackUsage(Protocol):
    async def seen(self, printer_id: int, serials: Iterable[str]) -> None: ...

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]: ...

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int: ...

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]: ...

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int: ...


class RackUsageStore:
    def __init__(
        self, conninfo: str, *, pool_size: int = 2, connect_timeout: float = CONNECT_TIMEOUT
    ) -> None:
        self._pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=pool_size,
            open=False,
            timeout=connect_timeout,
            connection_class=Connection[DictRow],
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": max(1, int(connect_timeout)),
            },
            name="scadbuddy-rack-usage",
        )
        self._pool_open = False
        self._migrated = False
        self._open_lock = threading.Lock()

    def _ready(self) -> ConnectionPool[Connection[DictRow]]:
        with self._open_lock:
            if not self._pool_open:
                self._pool.open(wait=False)
                self._pool_open = True
        if not self._migrated:
            with self._pool.connection() as conn:
                migrate(conn)
            self._migrated = True
        return self._pool

    def close(self) -> None:
        with self._open_lock:
            if self._pool_open:
                self._pool.close()
                self._pool_open = False

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        """Upsert each non-empty serial once: moves its printer, never its
        ``first_seen_at`` (spec §4)."""
        unique = sorted({serial for serial in serials if serial})
        if unique:
            await asyncio.to_thread(self._seen, printer_id, unique)

    def _seen(self, printer_id: int, unique: list[str]) -> None:
        with self._ready().connection() as conn:
            conn.execute(
                "INSERT INTO rack_nozzle_seen (serial, printer_id)"
                " SELECT serial, %s FROM unnest(%s::text[]) AS serial"
                " ON CONFLICT (serial) DO UPDATE SET printer_id = excluded.printer_id",
                (printer_id, unique),
            )

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        """One :class:`Usage` per non-empty serial; zeros for one with no rows (spec §4)."""
        unique = sorted({serial for serial in serials if serial})
        if not unique:
            return {}
        return await asyncio.to_thread(self._usage, unique)

    def _usage(self, unique: list[str]) -> dict[str, Usage]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT s.serial, seen.first_seen_at, count(p.archive_id) AS prints,"
                " coalesce(sum(p.print_seconds), 0) AS print_seconds,"
                " coalesce(sum(p.grams), 0) AS grams"
                " FROM unnest(%s::text[]) AS s(serial)"
                " LEFT JOIN rack_nozzle_seen AS seen ON seen.serial = s.serial"
                " LEFT JOIN rack_nozzle_prints AS p ON p.serial = s.serial"
                " GROUP BY s.serial, seen.first_seen_at",
                (unique,),
            ).fetchall()
        return {
            str(row["serial"]): Usage(
                prints=int(row["prints"]),
                print_seconds=int(row["print_seconds"]),
                grams=float(row["grams"]),
                first_seen_at=row["first_seen_at"],
            )
            for row in rows
        }

    async def record_picks(
        self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]
    ) -> int:
        """The picks for one queue item; the rows actually written (#1015)."""
        if not picks:
            return 0
        return await asyncio.to_thread(self._record_picks, queue_item_id, printer_id, list(picks))

    def _record_picks(self, queue_item_id: int, printer_id: int, picks: list[PickedHotend]) -> int:
        written = 0
        with self._ready().connection() as conn, conn.transaction():
            for pick in picks:
                cursor = conn.execute(
                    "INSERT INTO rack_nozzle_picks (queue_item_id, group_id, printer_id, serial)"
                    " VALUES (%s, %s, %s, %s)"
                    " ON CONFLICT (queue_item_id, group_id) DO NOTHING",
                    (queue_item_id, pick.group_id, printer_id, pick.serial),
                )
                written += cursor.rowcount
        return written

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        """Which of these queue items have picks."""
        ids = sorted(set(queue_item_ids))
        if not ids:
            return set()
        return await asyncio.to_thread(self._picked_items, ids)

    def _picked_items(self, ids: list[int]) -> set[int]:
        with self._ready().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT queue_item_id FROM rack_nozzle_picks"
                " WHERE queue_item_id = ANY(%s)",
                (ids,),
            ).fetchall()
        return {int(row["queue_item_id"]) for row in rows}

    async def record_prints(
        self,
        *,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        """One row per picked group of the item, the serial copied from its pick in SQL,
        so it never passes through Python on the settle path."""
        return await asyncio.to_thread(
            self._record_prints, archive_id, queue_item_id, settled_at, print_seconds, grams
        )

    def _record_prints(
        self,
        archive_id: int,
        queue_item_id: int,
        settled_at: datetime,
        print_seconds: int | None,
        grams: float | None,
    ) -> int:
        with self._ready().connection() as conn:
            cursor = conn.execute(
                "INSERT INTO rack_nozzle_prints"
                " (archive_id, group_id, serial, settled_at, print_seconds, grams)"
                " SELECT %s, group_id, serial, %s, %s, %s FROM rack_nozzle_picks"
                " WHERE queue_item_id = %s"
                " ON CONFLICT (archive_id, group_id) DO NOTHING",
                (archive_id, settled_at, print_seconds, grams, queue_item_id),
            )
        return cursor.rowcount
```

`backend/scadbuddy/rack/component.py`:

```python
"""Rack hotend usage (#836) as a component (`core/components.py`)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from scadbuddy.api.components import component_dep
from scadbuddy.core.components import Component, Components, Core, Key
from scadbuddy.rack.usage import RackUsageStore

RACK_USAGE: Key[RackUsageStore] = Key("rack_usage")


def _build(core: Core, components: Components) -> RackUsageStore:
    return RackUsageStore(core.settings.database_url)


@asynccontextmanager
async def _run(store: RackUsageStore) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await asyncio.to_thread(store.close)


COMPONENT = Component(RACK_USAGE, build=_build, run=_run)

RackUsageDep = Annotated[RackUsageStore, component_dep(RACK_USAGE)]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/rack/test_usage.py tests/test_pg_migrations.py tests/test_components.py -v && uv run --frozen mypy`
Expected: PASS (none skipped with the database up). If mypy reports
`core.settings.database_url` as `str | None`, write
`RackUsageStore(core.settings.database_url or "")`: `Settings` requires it (#401).

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/migrations/*_rack_nozzle_usage.sql backend/scadbuddy/rack/usage.py backend/scadbuddy/rack/component.py backend/tests/rack/test_usage.py
git commit -m "feat(rack): the rack_nozzle_* tables and their store" -m "Refs #836, #1015"
```

---

### Task 6: `printer_rack_algorithms`, the remembered algorithm

**Files:**
- Modify: `backend/scadbuddy/library/settings_store.py` (`REMEMBERED_ROWS` ~87, `StoredSettings` ~144, a setter after `set_printer_bed_type` ~556)
- Modify: `backend/scadbuddy/core/events.py` (`SettingsSection` ~193)
- Modify: `backend/scadbuddy/api/settings.py` (`RememberedChoices` ~191, `_remembered` ~311, `get_remembered`'s docstring)
- Modify: `backend/scadbuddy/api/printing.py` (a route after `put_printer_bed_type` ~122)
- Modify: `backend/scadbuddy/bambuddy/choices.py` (`ChoicesView`, `choices_for`)
- Modify: `agent/src/tools/coverage.ts` (`NOT_A_TOOL`)
- Test: `backend/tests/test_settings_store.py`, `backend/tests/api/test_print_rack.py` (new)

**Interfaces:**
- Consumes: `RackAlgorithm` (Task 3).
- Produces: `StoredSettings.printer_rack_algorithms: dict[str, RackAlgorithm]`;
  `StoredSettings.rack_algorithm(printer_id: int | None) -> RackAlgorithm`;
  `SettingsStore.set_printer_rack_algorithm(printer_id: int, algorithm: RackAlgorithm | None) -> StoredSettings`;
  `PUT /api/v1/print/printers/{printer_id}/rack-algorithm` taking `{"algorithm": RackAlgorithm | null}`
  and answering `PrinterRackAlgorithm{printer_id, algorithm}`;
  `RememberedChoices.printer_rack_algorithms`; `ChoicesView.rack_algorithm: RackAlgorithm`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_settings_store.py`:

```python
def test_a_printers_rack_algorithm_round_trips_and_is_forgotten(
    store: SettingsStore, settings: Settings
) -> None:
    """#836: kept in the jsonb ``settings`` row, one printer at a time, no own table."""
    store.set_printer_rack_algorithm(1, "oldest_first")
    store.set_printer_rack_algorithm(2, "bambuddy")
    loaded = _fresh_load(settings)
    assert loaded.printer_rack_algorithms == {"1": "oldest_first", "2": "bambuddy"}
    assert (loaded.rack_algorithm(1), loaded.rack_algorithm(3), loaded.rack_algorithm(None)) == (
        "oldest_first",
        "least_used",
        "least_used",
    )

    store.set_printer_rack_algorithm(2, None)
    assert _fresh_load(settings).printer_rack_algorithms == {"1": "oldest_first"}

    store.forget_remembered()
    assert _fresh_load(settings).printer_rack_algorithms == {}


def test_an_unknown_stored_rack_algorithm_is_dropped_not_fatal() -> None:
    """A newer version's algorithm must not stop this one loading its settings."""
    loaded = StoredSettings.model_validate(
        {"printer_rack_algorithms": {"1": "newest_first", "2": "from-the-future"}}
    )
    assert loaded.printer_rack_algorithms == {"1": "newest_first"}
```

`backend/tests/api/test_print_rack.py` (new):

```python
"""The rack nozzle routes (#836): the remembered algorithm, the preview, the manual pick."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_the_rack_algorithm_is_remembered_per_printer_and_forgotten(client: TestClient) -> None:
    put = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "newest_first"})
    assert put.status_code == 200, put.text
    assert put.json() == {"printer_id": 1, "algorithm": "newest_first"}
    remembered = client.get("/api/v1/settings/remembered").json()
    assert remembered["printer_rack_algorithms"] == {"1": "newest_first"}

    forgot = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": None})
    assert forgot.json() == {"printer_id": 1, "algorithm": "least_used"}
    assert client.get("/api/v1/settings/remembered").json().get("printer_rack_algorithms", {}) == {}


def test_an_unknown_algorithm_is_refused(client: TestClient) -> None:
    response = client.put("/api/v1/print/printers/1/rack-algorithm", json={"algorithm": "random"})
    assert response.status_code == 422
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py -k rack tests/api/test_print_rack.py -v`
Expected: FAIL with `AttributeError: 'SettingsStore' object has no attribute 'set_printer_rack_algorithm'` and 404/405 on the route.

- [ ] **Step 3: Implement**

In `settings_store.py`:
- import: change `from scadbuddy.bambuddy.models import NozzleChoice, SlotChoice, Tier` to
  `from scadbuddy.bambuddy.models import NozzleChoice, RackAlgorithm, SlotChoice, Tier`.
- `REMEMBERED_ROWS`: add `"printer_rack_algorithms",` after `"model_print_options",`.
- In `StoredSettings`, after `model_print_options`:

```python
    #: Stringified Bambuddy printer id -> how ScadBuddy picks that printer's rack nozzle
    #: (#836). Stored like ``printer_print_options``, one key at a time in the jsonb
    #: ``settings`` row: no table of its own, and not in ``OWN_TABLES``.
    printer_rack_algorithms: dict[str, RackAlgorithm] = Field(default_factory=dict)

    @field_validator("printer_rack_algorithms", mode="before")
    @classmethod
    def _known_rack_algorithms(cls, value: Any) -> Any:
        """A value this version does not know (a newer one wrote it) is dropped, so it
        cannot stop the settings loading."""
        if not isinstance(value, dict):
            return {}
        known = get_args(RackAlgorithm)
        return {key: algorithm for key, algorithm in value.items() if algorithm in known}

    def rack_algorithm(self, printer_id: int | None) -> RackAlgorithm:
        """The printer's remembered rack algorithm, else Least used (spec §4)."""
        if printer_id is None:
            return "least_used"
        return self.printer_rack_algorithms.get(str(printer_id), "least_used")
```

- After `set_printer_bed_type`:

```python
    def set_printer_rack_algorithm(
        self, printer_id: int, algorithm: RackAlgorithm | None
    ) -> StoredSettings:
        """Remember how one printer's rack nozzle is picked (#836); ``None`` forgets it."""
        with self._pool.connection() as conn:
            _put_entry(conn, "printer_rack_algorithms", str(printer_id), algorithm)
        return self._written("printer_rack_algorithm")
```

In `core/events.py`, add `"printer_rack_algorithm",` after `"printer_bed_type",` in `SettingsSection`.

In `api/settings.py`:
- import `RackAlgorithm` from `scadbuddy.bambuddy.models`;
- in `RememberedChoices`, after `printer_bed_types`:

```python
    #: Stringified Bambuddy printer id -> its rack nozzle algorithm (#836).
    printer_rack_algorithms: dict[str, RackAlgorithm] = Field(default_factory=dict)
```

- in `_remembered`, add `printer_rack_algorithms=settings.printer_rack_algorithms,`;
- in `get_remembered`'s docstring, after the bed-type sentence's clause, add
  `` ``PUT /print/printers/{id}/rack-algorithm`` with a ``null`` algorithm, `` to the list.

In `api/printing.py`, below `PrinterBedType`:

```python
class PrinterRackAlgorithmPut(BaseModel):
    """How to pick this printer's rack nozzle (#836); ``null`` forgets it."""

    algorithm: RackAlgorithm | None = None


class PrinterRackAlgorithm(BaseModel):
    """The rack algorithm in force on one printer (#836)."""

    printer_id: int
    algorithm: RackAlgorithm
```

and after `put_printer_bed_type`:

```python
@router.put(
    "/printers/{printer_id}/rack-algorithm",
    response_model=PrinterRackAlgorithm,
    summary="Remember how this printer's rack nozzle is picked",
)
def put_printer_rack_algorithm(
    printer_id: int, body: PrinterRackAlgorithmPut, store: SettingsStoreDep
) -> PrinterRackAlgorithm:
    """The print dialog's Advanced rack algorithm (#836, spec §4), per printer. Needs no
    Bambuddy, like the printer's remembered plate."""
    settings = store.set_printer_rack_algorithm(printer_id, body.algorithm)
    return PrinterRackAlgorithm(printer_id=printer_id, algorithm=settings.rack_algorithm(printer_id))
```

with `from scadbuddy.bambuddy.models import RackAlgorithm` in the imports.

In `bambuddy/choices.py`: import `RackAlgorithm` from `scadbuddy.bambuddy.models`; add to
`ChoicesView` after `model_choices`:

```python
    #: How the chosen printer's rack nozzle is ranked (#836): remembered per printer,
    #: else Least used. The dialog's Advanced selector opens on it.
    rack_algorithm: RackAlgorithm = "least_used"
```

and in `choices_for`'s `return ChoicesView(...)` add `rack_algorithm=settings.rack_algorithm(printer_id),`.

In `agent/src/tools/coverage.ts`, add to `NOT_A_TOOL` (next to the other print entries):

```ts
  {
    operation: 'PUT /api/v1/print/printers/{printer_id}/rack-algorithm',
    reason:
      "Remembers how the print dialog ranks a printer's nozzle rack (#836). It lands UI-first, " +
      'like the rest of the rack picker; an agent prints through print_output, which takes the ' +
      'remembered algorithm, and a tool for changing it is a follow-up.',
  },
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/test_settings_store.py tests/api/test_print_rack.py tests/api/test_settings.py -v && uv run --frozen mypy`
then `cd agent && pnpm test test/coverage.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/library/settings_store.py backend/scadbuddy/core/events.py backend/scadbuddy/api/settings.py backend/scadbuddy/api/printing.py backend/scadbuddy/bambuddy/choices.py agent/src/tools/coverage.ts backend/tests/test_settings_store.py backend/tests/api/test_print_rack.py
git commit -m "feat(rack): remember the rack algorithm per printer" -m "Refs #836"
```

---

### Task 7: `slice_and_queue` takes `choose_rack`

**Files:**
- Modify: `backend/scadbuddy/bambuddy/dispatch.py`
- Test: `backend/tests/bambuddy/test_dispatch.py`

**Interfaces:**
- Consumes: `PickedHotend` (Task 5).
- Produces: `class RackChoice(BaseModel)`: `nozzle_rack_choice: dict[str, int]`,
  `picks: list[PickedHotend]`; `QueueOutcome.rack_picks: list[PickedHotend]`;
  `slice_and_queue(..., choose_rack: Callable[[int], Awaitable[RackChoice | None]] | None = None)`,
  awaited with the sliced file id after the slice and before `before_enqueue`.

- [ ] **Step 1: Write the failing tests** (append to `tests/bambuddy/test_dispatch.py`)

```python
import pytest

from scadbuddy.bambuddy.dispatch import RackChoice
from scadbuddy.rack.usage import PickedHotend


@respx.mock
async def test_the_rack_choice_rides_on_the_queue_item(bambuddy: BambuddyClient) -> None:
    _, queued = routes()
    asked: list[int] = []

    async def choose(sliced: int) -> RackChoice | None:
        asked.append(sliced)
        return RackChoice(
            nozzle_rack_choice={"0": 4},
            picks=[PickedHotend(group_id=0, position=4, serial="TEST-HOTEND-19")],
        )

    outcome = await slice_and_queue(
        bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose
    )

    assert asked == [52]
    assert json.loads(queued.calls.last.request.read())["nozzle_rack_choice"] == {"0": 4}
    assert [(p.group_id, p.position) for p in outcome.rack_picks] == [(0, 4)]
    assert "TEST-HOTEND-19" not in repr(outcome)


@respx.mock
async def test_no_rack_choice_sends_no_field(bambuddy: BambuddyClient) -> None:
    _, queued = routes()

    async def choose(sliced: int) -> RackChoice | None:
        return None

    outcome = await slice_and_queue(
        bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose
    )

    assert "nozzle_rack_choice" not in json.loads(queued.calls.last.request.read())
    assert outcome.rack_picks == []


@respx.mock
async def test_the_rack_is_chosen_after_the_slice_and_before_before_enqueue(
    bambuddy: BambuddyClient,
) -> None:
    sliced, _ = routes()
    order: list[str] = []

    async def choose(file_id: int) -> RackChoice | None:
        order.append(f"choose after {sliced.call_count} slice")
        return None

    async def before() -> None:
        order.append("before_enqueue")

    await slice_and_queue(
        bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose, before_enqueue=before
    )

    assert order == ["choose after 1 slice", "before_enqueue"]


@respx.mock
async def test_a_choose_rack_that_raises_fails_the_plate_with_nothing_queued(
    bambuddy: BambuddyClient,
) -> None:
    """Spec §5: awaited bare, like ``before_enqueue``. The callback the run builds never
    raises (Task 9 tests that); this pins that ``slice_and_queue`` adds no ``try``."""
    _, queued = routes()

    async def choose(sliced: int) -> RackChoice | None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await slice_and_queue(bambuddy, library_file_id=41, plan=PLAN, printer_id=1, choose_rack=choose)
    assert not queued.called
```

Place the two new imports with the module's other imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_dispatch.py -v`
Expected: FAIL with `ImportError: cannot import name 'RackChoice'`.

- [ ] **Step 3: Implement**

In `dispatch.py`'s module docstring, replace the first paragraph's first sentence with:

```python
**Why this exists at all.** ``filament_overrides``, ``required_filament_types`` and
``nozzle_rack_choice`` (#836) are fields of ``PrintQueueItemCreate`` and of nothing else.
```

Add `from scadbuddy.rack.usage import PickedHotend` to the imports, then after `QueueOutcome`'s
fields add:

```python
    #: What ``choose_rack`` picked, with each hotend's serial, for ``print_run.py`` to
    #: record (spec 2026-10-01 §5). Never reported: ``_queued`` builds the run's result
    #: from named fields and never dumps this model (spec §6, §7).
    rack_picks: list[PickedHotend] = Field(default_factory=list)
```

and below it:

```python
class RackChoice(BaseModel):
    """What ``choose_rack`` answers: the queue field, keyed by stringified filament group
    id, and the picks behind it (spec 2026-10-01 §5)."""

    nozzle_rack_choice: dict[str, int]
    picks: list[PickedHotend] = Field(default_factory=list)
```

In `slice_and_queue`'s signature, after `before_enqueue`:

```python
    choose_rack: Callable[[int], Awaitable[RackChoice | None]] | None = None,
```

In its docstring, after the `before_enqueue` paragraph:

```python
    ``choose_rack`` is awaited with the sliced file's id after the slice and before
    ``before_enqueue``; its choice goes on the item and its picks on the outcome. It is
    awaited bare, like ``before_enqueue``: the callback is what never raises (spec
    2026-10-01 §5), so a ``try`` here would hide a broken one.
```

Replace the block from `remembered = ...` to the `return`:

```python
    remembered = options.queue_fields() if options is not None else {}
    remembered.pop("quantity", None)
    remembered.pop("project_id", None)
    rack = await choose_rack(sliced) if choose_rack is not None else None
    if before_enqueue is not None:
        await before_enqueue()
    item = await client.enqueue(
        QueueItemCreate(
            **remembered,
            printer_id=printer_id,
            library_file_id=sliced,
            quantity=copies,
            plate_id=plate_id,
            filament_overrides=filaments.filament_overrides if filaments else None,
            required_filament_types=filaments.required_filament_types if filaments else None,
            # On this route the project can ride on the item itself, so there is no
            # window in which the entry exists unfiled (#79).
            project_id=project_id,
            nozzle_rack_choice=rack.nozzle_rack_choice if rack is not None else None,
        )
    )
    return QueueOutcome(
        slice_job_id=accepted.job_id,
        sliced_library_file_id=sliced,
        preset_key=request.preset_key,
        queue_item_ids=[item.id],
        printer_id=printer_id,
        rack_picks=list(rack.picks) if rack is not None else [],
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_dispatch.py -v && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/dispatch.py backend/tests/bambuddy/test_dispatch.py
git commit -m "feat(rack): slice_and_queue takes the rack choice from a callback" -m "Refs #836"
```

---

### Task 8: `rack_nozzle_seen` from the choices read and `prepare_run`

**Files:**
- Modify: `backend/scadbuddy/rack/usage.py` (add `record_seen`)
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`prepare_run`, `check_print`, `check_for_output`, `check_for_library`, `execute_run`)
- Modify: `backend/scadbuddy/bambuddy/choices.py` (`choices_for`, `choices_for_output`)
- Modify: `backend/scadbuddy/api/printing.py` (`post_run`, `accept_run`, `post_check`, `get_choices`)
- Modify: `backend/scadbuddy/api/library_print.py` (`get_library_choices`, `post_library_run`, `post_library_check`)
- Test: `backend/tests/rack/test_seen.py` (new), `backend/tests/api/test_print_rack.py`

**Interfaces:**
- Consumes: `RackUsage`, `RackUsageDep`, `rack_positions`.
- Produces: `record_seen(store: RackUsage | None, printer_id: int, status: PrinterStatus | None) -> None` (never raises);
  `rack: RackUsage | None = None` keyword on `prepare_run`, `check_print`,
  `check_for_output`, `check_for_library`, `execute_run`, `choices_for`,
  `choices_for_output`, and `accept_run`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/rack/test_seen.py`:

```python
"""``record_seen`` (#836, spec §4): the rack's hotends, advisory, never logged by serial."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import pytest

from scadbuddy.rack.usage import record_seen
from tests.rack.helpers import mounted, serial, slot, status


class Seen:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[int, list[str]]] = []
        self.error = error

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((printer_id, sorted(serials)))


async def test_the_racks_hotends_are_recorded_and_the_left_hotend_is_not() -> None:
    store = Seen()
    left = mounted().model_copy(update={"id": 1, "serial_number": serial(1)})
    await record_seen(store, 1, status(mounted(), left, *(slot(p) for p in (2, 3, 4, 5, 6))))  # type: ignore[arg-type]
    assert store.calls == [(1, sorted([serial(0), *(serial(p + 15) for p in (2, 3, 4, 5, 6))]))]


async def test_no_status_or_no_store_records_nothing() -> None:
    store = Seen()
    await record_seen(store, 1, None)  # type: ignore[arg-type]
    await record_seen(None, 1, status(slot(2)))
    assert store.calls == []


async def test_a_failed_write_is_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    store = Seen(RuntimeError(f"duplicate key (serial)=({serial(17)})"))
    with caplog.at_level(logging.DEBUG):
        await record_seen(store, 1, status(slot(2)))  # type: ignore[arg-type]
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == "could not record the rack's hotends"
    assert getattr(record, "error") == "RuntimeError"
    assert serial(17) not in repr(record.__dict__) and record.exc_info is None
```

Append to `backend/tests/api/test_print_rack.py` (with the imports at the top of the module):

```python
import asyncio

import httpx
import respx

from scadbuddy.api.deps import STATE_ATTR
from scadbuddy.rack.component import RACK_USAGE
from tests.api.test_print_run_choices import API, body, run_routes
from tests.api.test_print_filaments import prepared
from tests.api.test_send import upload_route
from tests.rack.helpers import invented_status, serial

pytestmark = pytest.mark.requires_postgres


def rack_usage(client: TestClient) -> RackUsageStore:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    store: RackUsageStore = state.components.get(RACK_USAGE)
    return store


def invented_rack_route() -> None:
    """The recorded rack with invented serials; replaces ``hardware_routes``' answer
    (respx re-uses a route registered again with the same pattern)."""
    respx.get(f"{API}/printers/1/status").mock(
        return_value=httpx.Response(200, json=invented_status())
    )


@respx.mock
def test_the_check_records_the_racks_hotends_as_seen(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body())

    assert response.status_code == 200, response.text
    usage = asyncio.run(rack_usage(client).usage([serial(17), serial(0), serial(1)]))
    assert usage[serial(17)].first_seen_at is not None
    assert usage[serial(0)].first_seen_at is not None
    assert usage[serial(1)].first_seen_at is None  # the left hotend is not a rack hotend
```

Add `import pytest` and `from scadbuddy.rack.usage import RackUsageStore` to that module's
imports too. Merge the import lines with the ones Task 6 put there; ruff sorts them.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/rack/test_seen.py tests/api/test_print_rack.py -v`
Expected: FAIL with `ImportError: cannot import name 'record_seen'`.

- [ ] **Step 3: Implement**

In `rack/usage.py` add `import logging`, `from scadbuddy.bambuddy.models import PrinterStatus`,
`from scadbuddy.rack.rank import Usage, rack_positions`, a module
`logger = logging.getLogger(__name__)`, and:

```python
async def record_seen(store: RackUsage | None, printer_id: int, status: PrinterStatus | None) -> None:
    """Record the rack's hotends as seen by the print flow (spec §4). Advisory: a failure
    is logged by exception type and never stops the read that called it."""
    if store is None or status is None:
        return
    serials = [entry.serial_number for entry in rack_positions(status.nozzle_rack).values()]
    try:
        await store.seen(printer_id, serials)
    except Exception as exc:
        logger.warning(
            "could not record the rack's hotends",
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
```

In `print_run.py`, import `from scadbuddy.rack.usage import RackUsage, record_seen`, then:

- `prepare_run(client, source, settings, request, *, rack: RackUsage | None = None)`; replace
  its `return PreparedRun(...)` with:

```python
    printer_status = await _read_status(client, printer_id)
    await record_seen(rack, printer_id, printer_status)
    return PreparedRun(
        plate_ids=plate_ids,
        printer_id=printer_id,
        catalogue=catalogue,
        printer_status=printer_status,
    )
```

- `check_print(client, source, settings, request, *, rack: RackUsage | None = None)`, passing
  `rack=rack` to `prepare_run`; `check_for_output(..., request, *, rack: RackUsage | None = None)`
  and `check_for_library(..., request, *, rack: RackUsage | None = None)` passing `rack=rack`
  on to `check_print`.
- `execute_run(client, source, settings, request, prepared, before_enqueue=None, *, rack: RackUsage | None = None)`.
  It does not use `rack` until Task 9.

In `choices.py`, import `from scadbuddy.rack.usage import RackUsage, record_seen`; add
`rack: RackUsage | None = None` as a keyword to `choices_for` and `choices_for_output`
(which passes it on). In `choices_for`, right after the `try:` block that reads `status`,
add:

```python
        await record_seen(rack, printer_id, status)
```

In `api/printing.py`, import `from scadbuddy.rack.component import RackUsageDep` and
`from scadbuddy.rack.usage import RackUsage`, then:
- `accept_run(..., started=None, rack: RackUsage | None = None)`: pass `rack=rack` to both
  `prepare_run(...)` and `execute_run(..., before_enqueue, rack=rack)`.
- `post_run`: add the parameter `rack: RackUsageDep,` and `rack=rack,` to `accept_run(...)`.
- `post_check`: add `rack: RackUsageDep,` and `rack=rack` to `check_for_output(...)`.
- `get_choices`: add `rack: RackUsageDep,` and `rack=rack` to `choices_for_output(...)`.

In `api/library_print.py`, the same for `get_library_choices` (`choices_for(..., rack=rack)`),
`post_library_run` (`accept_run(..., rack=rack)`) and `post_library_check`
(`check_for_library(..., rack=rack)`), importing `RackUsageDep` the same way.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/rack/test_seen.py tests/api/test_print_rack.py tests/api/test_print_run_choices.py tests/api/test_print_choices.py tests/api/test_print_library.py -v && uv run --frozen mypy`
Expected: PASS: every existing run, check and choices test still passes.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/rack/usage.py backend/scadbuddy/bambuddy/print_run.py backend/scadbuddy/bambuddy/choices.py backend/scadbuddy/api/printing.py backend/scadbuddy/api/library_print.py backend/tests/rack/test_seen.py backend/tests/api/test_print_rack.py
git commit -m "feat(rack): record the rack's hotends from every print-path status read" -m "Refs #836"
```

---

### Task 9: The `choose_rack` callback in the run, and warning dedup

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`rack_chooser`, `_spools_by_slot`, `execute_run`'s plate loop, `PrintRunRequest`)
- Modify: `backend/scadbuddy/bambuddy/runs.py` (`run_key`)
- Test: `backend/tests/bambuddy/test_rack_chooser.py` (new), `backend/tests/api/test_print_run_choices.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 7, 8.
- Produces:
  - `PrintRunRequest.rack_position: int | None` (1-6) and `.rack_algorithm: RackAlgorithm | None`
  - `class RackReads(Protocol)`: `filament_requirements(file_id, *, plate_id=None)`, `printer_status(printer_id)`
  - `rack_chooser(client: RackReads, *, printer_id: int, plan: FilamentPlan, spools: Mapping[int, SpoolOption], algorithm: RackAlgorithm, manual_position: int | None, rack: RackUsage | None, warnings: list[FilamentWarning]) -> Callable[[int], Awaitable[RackChoice | None]]`

- [ ] **Step 1: Write the failing unit tests**

`backend/tests/bambuddy/test_rack_chooser.py`:

```python
"""The run's ``choose_rack`` callback (#836, spec 2026-10-01 §5). It never raises."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

import pytest

from scadbuddy.bambuddy import print_run
from scadbuddy.bambuddy.filaments import FilamentPlan, FilamentWarning, SpoolOption
from scadbuddy.bambuddy.models import (
    FilamentRequirement,
    FilamentRequirements,
    PrinterStatus,
    SlotChoice,
)
from scadbuddy.bambuddy.print_run import rack_chooser
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend
from tests.rack.helpers import requirement, serial, slot, status

PLAN = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)])
SPOOLS = {1: SpoolOption(spool_id=9, material="PLA")}


class Reads:
    def __init__(
        self,
        requirements: FilamentRequirements | Exception,
        rack: PrinterStatus | Exception,
    ) -> None:
        self.requirements = requirements
        self.rack = rack
        self.status_reads = 0

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements:
        if isinstance(self.requirements, Exception):
            raise self.requirements
        return self.requirements

    async def printer_status(self, printer_id: int) -> PrinterStatus:
        self.status_reads += 1
        if isinstance(self.rack, Exception):
            raise self.rack
        return self.rack


class Usages:
    """A ``RackUsage`` that remembers the order of its calls."""

    def __init__(self, usage: dict[str, Usage] | None = None) -> None:
        self.calls: list[str] = []
        self._usage = usage or {}

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        self.calls.append("seen")

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        self.calls.append("usage")
        return self._usage

    async def record_picks(self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]) -> int:
        return len(picks)

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        return set()

    async def record_prints(
        self, *, archive_id: int, queue_item_id: int, settled_at: datetime,
        print_seconds: int | None, grams: float | None,
    ) -> int:
        return 0


def grouped(*filaments: FilamentRequirement) -> FilamentRequirements:
    return FilamentRequirements(filaments=list(filaments))


def chooser(
    reads: Reads,
    warnings: list[FilamentWarning],
    *,
    usages: Usages | None = None,
    manual: int | None = None,
    algorithm: print_run.RackAlgorithm = "least_used",
) -> print_run.ChooseRack:
    return rack_chooser(
        reads,
        printer_id=1,
        plan=PLAN,
        spools=SPOOLS,
        algorithm=algorithm,
        manual_position=manual,
        rack=usages if usages is not None else Usages(),
        warnings=warnings,
    )


async def test_the_ranked_choice_is_returned_keyed_by_group_id() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(color="#FF6A13")), status(slot(2), slot(4, color="FF6A13FF")))
    choice = await chooser(reads, warnings)(77)
    assert choice is not None
    assert choice.nozzle_rack_choice == {"0": 4}
    assert [(p.group_id, p.position, p.serial) for p in choice.picks] == [(0, 4, serial(19))]
    assert warnings == []


async def test_usage_is_read_before_the_read_is_recorded_as_seen() -> None:
    """#1015: ``first_seen_at`` is ranked as it stood before this read."""
    usages = Usages()
    await chooser(Reads(grouped(requirement()), status(slot(2))), [], usages=usages)(77)
    assert usages.calls == ["usage", "seen"]


async def test_two_filaments_on_one_group_send_one_entry() -> None:
    reads = Reads(grouped(requirement(1), requirement(2, color="#00B1B7")), status(slot(2), slot(3)))
    choice = await chooser(reads, [])(77)
    assert choice is not None and choice.nozzle_rack_choice == {"0": 2}


async def test_each_call_reads_the_rack_afresh() -> None:
    """A two-plate print: the second plate ranks a rack read after the first sliced."""
    reads = Reads(grouped(requirement(color="#FF6A13")), status(slot(2, color="FF6A13FF"), slot(3)))
    choose = chooser(reads, [])
    first = await choose(77)
    reads.rack = status(slot(2), slot(3, color="FF6A13FF"))
    second = await choose(78)
    assert first is not None and second is not None
    assert (first.nozzle_rack_choice, second.nozzle_rack_choice) == ({"0": 2}, {"0": 3})
    assert reads.status_reads == 2


async def test_a_plate_with_no_rack_group_reads_no_status_and_says_nothing() -> None:
    warnings: list[FilamentWarning] = []
    reads = Reads(grouped(requirement(on_rack=False)), RuntimeError("never read"))
    assert await chooser(reads, warnings)(77) is None
    assert (reads.status_reads, warnings) == (0, [])


async def test_a_manual_pick_on_a_plate_with_no_rack_group_is_unused() -> None:
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(grouped(requirement(on_rack=False)), status()), warnings, manual=3)(77) is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]


@pytest.mark.parametrize(
    ("requirements", "rack", "reason"),
    [
        (ApiError(503, "down"), status(slot(2)), "requirements unreadable"),
        (grouped(requirement()), ApiError(503, "down"), "status unreadable"),
    ],
)
async def test_an_unreadable_read_sends_no_choice_and_says_why(
    requirements: FilamentRequirements | Exception, rack: PrinterStatus | Exception, reason: str
) -> None:
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(requirements, rack), warnings)(77) is None
    assert [(w.kind, w.message, w.slot_id) for w in warnings] == [
        ("rack-left-to-bambuddy", f"Rack pick left to Bambuddy: {reason}.", None)
    ]


async def test_rank_rack_raising_queues_without_a_choice_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §5: the whole body is one ``try``; nothing escapes the callback."""

    def broken(*args: object, **kwargs: object) -> object:
        raise ZeroDivisionError

    monkeypatch.setattr(print_run, "rank_rack", broken)
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(grouped(requirement()), status(slot(2))), warnings)(77) is None
    assert [w.message for w in warnings] == ["Rack pick left to Bambuddy: rack pick failed."]


async def test_a_rack_with_no_eligible_position_says_so() -> None:
    warnings: list[FilamentWarning] = []
    assert await chooser(Reads(grouped(requirement()), status()), warnings)(77) is None
    assert [w.message for w in warnings] == [
        "Rack pick left to Bambuddy: no eligible position for group 0."
    ]


async def test_let_bambuddy_pick_reads_no_usage_and_sends_nothing() -> None:
    usages = Usages()
    choice = await chooser(
        Reads(grouped(requirement()), status(slot(2))), [], usages=usages, algorithm="bambuddy"
    )(77)
    assert choice is None and usages.calls == ["seen"]
```

- [ ] **Step 2: Write the failing API tests** (append to `tests/api/test_print_run_choices.py`)

```python
def grouped_requirements_route(
    *, sliced_id: int = 77, filament_type: str = "PLA", color: str = "#FF6A13", diameter: str = "0.40"
) -> None:
    """The sliced file's requirements grouped as on library file 228 (spec §8 unknown 2);
    every other file answers the recording. Registered with ``inventory_routes``' pattern,
    so respx re-uses that route and this answer replaces its own."""
    base = recording("filament-requirements.json")

    def answer(request: httpx.Request) -> httpx.Response:
        if f"/library/files/{sliced_id}/" not in request.url.path:
            return httpx.Response(200, json=base)
        filament = {
            "slot_id": 1, "type": filament_type, "color": color, "used_grams": 3.2,
            "used_meters": 1.1, "used_in_plate": True, "group_id": 0,
            "group": {"on_rack": True, "nozzle_diameter": diameter, "volume_type": "Standard", "filament_color": color},
        }
        return httpx.Response(200, json={"file_id": sliced_id, "filaments": [filament]})

    respx.route(method="GET", path__regex=r"/api/v1/library/files/\d+/filament-requirements").mock(
        side_effect=answer
    )


@respx.mock
def test_the_ranked_rack_choice_is_sent(client: TestClient, model: str) -> None:
    """Spec §1: with a readable rack and a sliced group, the ranked pick is sent. The
    recorded rack's 0.4 Standard positions are 2, 4 and 6; position 4 holds FF6A13."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 4}


@respx.mock
def test_an_unreadable_rack_sends_no_choice_and_says_so(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content).get("nozzle_rack_choice") is None
    assert {
        (w["kind"], w["message"]) for w in response.json()["warnings"]
    } >= {("rack-left-to-bambuddy", "Rack pick left to Bambuddy: status unreadable.")}


@respx.mock
def test_a_rack_warning_repeated_on_every_plate_is_shown_once(
    client: TestClient, model: str, paths: DataPaths
) -> None:
    """Spec §6: rack warnings join the plate loop's de-duplication."""
    output_id = two_plate_output(client, model, paths)
    upload_route()
    printers_route()
    h2c_presets()
    spool_preset_routes()
    hardware_routes()
    split_plates_routes()
    grouped_requirements_route(filament_type="PLA-CF")
    slice_routes()
    queue_route()

    response = run_print(
        client, output_id, json=run_request(all_plates=True, choices={"nozzles": [{"size": "0.4"}], "tier": "standard"})
    )

    assert response.status_code == 200, response.text
    kinds = [w["kind"] for w in response.json()["warnings"]]
    assert kinds.count("rack-unsafe-material") == 1
```

In the existing `test_choices_slice_with_derived_presets_and_queue_without_a_pipeline`, leave
`assert sent.get("nozzle_rack_choice") is None` and add this comment above it:
`# The recorded requirements carry no group, so nothing prints from the rack (#836).`

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_rack_chooser.py tests/api/test_print_run_choices.py -k "rack or ranked" -v`
Expected: FAIL with `ImportError: cannot import name 'rack_chooser'`.

- [ ] **Step 4: Implement**

In `print_run.py` imports add:

```python
from collections.abc import Awaitable, Callable, Mapping
from scadbuddy.bambuddy.dispatch import QueueOutcome, RackChoice, SlicePlan, slice_and_queue
from scadbuddy.bambuddy.filaments import SpoolOption
from scadbuddy.bambuddy.models import FilamentRequirements, PrinterStatus, RackAlgorithm
from scadbuddy.rack.rank import manual_for, rack_groups, rack_positions, rack_warnings, rank_rack
from scadbuddy.rack.usage import PickedHotend, RackUsage, record_seen
```

(merge with the existing import lines; `FilamentPlan` and `FilamentWarning` already come from `filaments`).

Add to `PrintRunRequest` after `print_sequence`:

```python
    #: A rack position (1-6) chosen by hand for the rack side (#836, spec 2026-10-01 §5).
    #: Omitted is Automatic: ScadBuddy ranks the rack.
    rack_position: int | None = Field(default=None, ge=1, le=6)
    #: How to rank the rack for this print (#836). Omitted means the printer's remembered
    #: algorithm, else Least used.
    rack_algorithm: RackAlgorithm | None = None
```

Below `PrintCheck`, add:

```python
ChooseRack = Callable[[int], Awaitable[RackChoice | None]]


class RackReads(Protocol):
    """The two reads ``choose_rack`` makes; ``BambuddyClient`` is one."""

    async def filament_requirements(
        self, file_id: int, *, plate_id: int | None = None
    ) -> FilamentRequirements: ...

    async def printer_status(self, printer_id: int) -> PrinterStatus: ...


def rack_chooser(
    client: RackReads,
    *,
    printer_id: int,
    plan: FilamentPlan,
    spools: Mapping[int, SpoolOption],
    algorithm: RackAlgorithm,
    manual_position: int | None,
    rack: RackUsage | None,
    warnings: list[FilamentWarning],
) -> ChooseRack:
    """The plate's ``choose_rack`` (spec 2026-10-01 §5). It reads the sliced file's
    requirements and then, only when a group prints from the rack, a fresh status, once
    per plate and never ``PreparedRun.printer_status``. It ranks, and appends its warnings
    to ``warnings``, the plate's list. It **never raises**: the whole body is one ``try``,
    and on any exception it logs a fixed message with the exception's type, appends
    ``rack-left-to-bambuddy`` and returns ``None``, so Bambuddy picks."""

    async def choose(sliced: int) -> RackChoice | None:
        stage = "requirements unreadable"
        try:
            requirements = await client.filament_requirements(sliced)
            stage = "rack pick failed"
            groups = rack_groups(requirements.filaments, spools)
            manual, notes = manual_for(groups, manual_position, algorithm)
            warnings.extend(notes)
            if not groups:
                return None
            stage = "status unreadable"
            status_read = await client.printer_status(printer_id)
            stage = "rack usage unreadable"
            serials = [e.serial_number for e in rack_positions(status_read.nozzle_rack).values()]
            # Read before this read is recorded as seen (#1015).
            usage = (
                await rack.usage(serials)
                if rack is not None and algorithm != "bambuddy"
                else {}
            )
            await record_seen(rack, printer_id, status_read)
            stage = "rack pick failed"
            picks = rank_rack(groups, status_read.nozzle_rack, algorithm, usage, manual)
            warnings.extend(rack_warnings(groups, status_read.nozzle_rack, algorithm, picks, manual))
        except Exception as exc:
            logger.warning(
                "rack pick left to Bambuddy",
                extra={"printer_id": printer_id, "stage": stage, "error": type(exc).__name__},
            )
            warnings.append(
                FilamentWarning(
                    kind="rack-left-to-bambuddy", message=f"Rack pick left to Bambuddy: {stage}."
                )
            )
            return None
        if not picks:
            return None
        return RackChoice(
            nozzle_rack_choice={str(group_id): pick.position for group_id, pick in sorted(picks.items())},
            picks=[
                PickedHotend(group_id=group_id, position=pick.position, serial=pick.serial)
                for group_id, pick in sorted(picks.items())
            ],
        )

    return choose


def _spools_by_slot(options: FilamentOptions, plan: FilamentPlan) -> dict[int, SpoolOption]:
    """Each plate slot's chosen inventory spool, for the Glow test (spec §3)."""
    by_id = {spool.spool_id: spool for spool in options.spools}
    return {slot.slot_id: by_id[slot.spool_id] for slot in plan.slots if slot.spool_id in by_id}
```

(`Protocol` comes from `typing`, which `print_run.py` already imports.)

In `execute_run`, after `hardware += high_flow_warnings(...)` add:

```python
    algorithm = request.rack_algorithm or settings.rack_algorithm(printer_id)
```

and replace the plate loop's head and dedup loop with:

```python
    for plate_id, options, resolved, plan in planned:
        rack_notes: list[FilamentWarning] = []
        outcome = await slice_and_queue(
            client,
            library_file_id=library_file_id,
            plan=plan,
            printer_id=printer_id,
            filaments=queue_filaments(options, request.filament_plan),
            plate_id=plate_id,
            copies=copies,
            project_id=project_id,
            options=print_options,
            before_enqueue=before_enqueue,
            choose_rack=rack_chooser(
                client,
                printer_id=printer_id,
                plan=request.filament_plan,
                spools=_spools_by_slot(options, request.filament_plan),
                algorithm=algorithm,
                manual_position=request.rack_position,
                rack=rack,
                warnings=rack_notes,
            ),
        )
        sent = await source.record(library_file_id, plate_id, outcome, project_id, sent)
        outcomes.append(outcome)
        for warning in [
            *resolved.warnings,
            *check(options, request.filament_plan, copies=copies),
            *rack_notes,
        ]:
            # Checked once below, against what every plate needs together. A rack
            # warning repeated on every plate is one fact, shown once (spec §6).
            if warning.kind != "low-filament" and warning not in warnings:
                warnings.append(warning)
```

In `runs.py` `run_key`, widen the tuple of optional names and the docstring:

```python
    ``request_id``, ``print_sequence``, ``rack_position`` and ``rack_algorithm`` are part
    of it when sent; without them the key is what it was before the fields existed.
    """
    optional = ("request_id", "print_sequence", "rack_position", "rack_algorithm")
    exclude = {name for name in optional if getattr(request, name) is None}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_rack_chooser.py tests/api/test_print_run_choices.py tests/api/test_print_runs.py tests/test_print_runs_store.py -v && uv run --frozen mypy`
Expected: PASS, with the existing `run_key` tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/print_run.py backend/scadbuddy/bambuddy/runs.py backend/tests/bambuddy/test_rack_chooser.py backend/tests/api/test_print_run_choices.py
git commit -m "feat(rack): rank the rack per plate after the slice and send the pick" -m "Refs #836, #1015"
```

---

### Task 10: Record the picks after `POST /queue/`

**Files:**
- Modify: `backend/scadbuddy/rack/usage.py` (`save_picks`)
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`execute_run`)
- Test: `backend/tests/rack/test_save_picks.py` (new), `backend/tests/api/test_print_rack.py`

**Interfaces:**
- Consumes: `QueueOutcome.rack_picks`, `RackUsage.record_picks`.
- Produces: `save_picks(store: RackUsage | None, printer_id: int, queue_item_ids: Sequence[int], picks: Sequence[PickedHotend]) -> None`
  (never raises).

- [ ] **Step 1: Write the failing tests**

`backend/tests/rack/test_save_picks.py`:

```python
"""``save_picks`` (#836, spec §5): advisory, and silent about serials."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest

from scadbuddy.rack.usage import PickedHotend, save_picks
from tests.rack.helpers import serial


class Picks:
    def __init__(self, *, error: Exception | None = None, written: int | None = None) -> None:
        self.rows: list[tuple[int, int, list[PickedHotend]]] = []
        self.error = error
        self.written = written

    async def record_picks(self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]) -> int:
        if self.error is not None:
            raise self.error
        self.rows.append((queue_item_id, printer_id, list(picks)))
        return len(picks) if self.written is None else self.written


PICK = PickedHotend(group_id=0, position=4, serial=serial(19))


async def test_each_queue_item_gets_the_picks() -> None:
    store = Picks()
    await save_picks(store, 1, [51, 52], [PICK])  # type: ignore[arg-type]
    assert [(item, printer) for item, printer, _ in store.rows] == [(51, 1), (52, 1)]


async def test_a_pick_with_no_serial_is_sent_but_not_recorded() -> None:
    """Review Focus 4: nothing can be attributed to an empty serial."""
    store = Picks()
    blank = PickedHotend(group_id=1, position=2, serial="")
    await save_picks(store, 1, [51], [PICK, blank])  # type: ignore[arg-type]
    assert [p.group_id for p in store.rows[0][2]] == [0]


async def test_a_failed_write_is_logged_by_type_and_dropped(caplog: pytest.LogCaptureFixture) -> None:
    store = Picks(error=RuntimeError(f"violates key (serial)=({serial(19)})"))
    with caplog.at_level(logging.DEBUG):
        await save_picks(store, 1, [51], [PICK])  # type: ignore[arg-type]
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == "could not record the rack picks"
    assert serial(19) not in repr(record.__dict__) and record.exc_info is None


async def test_fewer_rows_than_picks_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """#1015: a conflict is never expected; when one happens it is visible."""
    with caplog.at_level(logging.WARNING):
        await save_picks(Picks(written=0), 1, [51], [PICK])  # type: ignore[arg-type]
    assert [r.getMessage() for r in caplog.records] == [
        "a rack pick was already recorded for this queue item"
    ]
```

Append to `backend/tests/api/test_print_rack.py` (adding `import json` and
`from scadbuddy.api.components import getter_for` and
`from tests.api.test_print_run_choices import grouped_requirements_route, run_print`
and `from tests.api.test_print_filaments import queue_route, slice_routes` to its imports):

```python
class BrokenUsage(RackUsageStore):
    """Reads work as an empty history; the picks write fails."""

    def __init__(self) -> None:
        pass

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        return None

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        return {}

    async def record_picks(self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]) -> int:
        raise RuntimeError("the database went away")

    def close(self) -> None:
        return None


@respx.mock
def test_a_failed_picks_write_still_returns_the_queued_run(client: TestClient, model: str) -> None:
    """Spec §5: the item is queued, so the write is advisory."""
    client.app.dependency_overrides[getter_for(RACK_USAGE)] = BrokenUsage  # type: ignore[attr-defined]
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route()
    slice_routes()
    queued = queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert response.json()["queue_item_ids"] == [51]
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 4}


@respx.mock
def test_the_picks_are_recorded_against_the_queue_item(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()
    grouped_requirements_route()
    slice_routes()
    queue_route()

    response = run_print(client, output_id, json=body(nozzles=[{"size": "0.4"}], tier="standard"))

    assert response.status_code == 200, response.text
    assert asyncio.run(rack_usage(client).picked_items([51])) == {51}
```

(`Iterable`, `Sequence` from `collections.abc`; `Usage` from `scadbuddy.rack.rank`;
`PickedHotend` from `scadbuddy.rack.usage`.)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/rack/test_save_picks.py tests/api/test_print_rack.py -v`
Expected: FAIL with `ImportError: cannot import name 'save_picks'`.

- [ ] **Step 3: Implement**

In `rack/usage.py`:

```python
async def save_picks(
    store: RackUsage | None,
    printer_id: int,
    queue_item_ids: Sequence[int],
    picks: Sequence[PickedHotend],
) -> None:
    """Write the sent picks against each queue item, right after ``POST /queue/`` (spec
    §5). Advisory: the item is queued, so a failure is logged by type and dropped, and
    that print's use goes uncounted. A pick of a hotend with no serial is skipped: it was
    sent, but nothing can be attributed to it."""
    attributable = [pick for pick in picks if pick.serial]
    if store is None or not attributable:
        return
    try:
        for queue_item_id in queue_item_ids:
            written = await store.record_picks(queue_item_id, printer_id, attributable)
            if written != len(attributable):
                logger.warning(
                    "a rack pick was already recorded for this queue item",
                    extra={"queue_item_id": queue_item_id, "sent": len(attributable), "written": written},
                )
    except Exception as exc:
        logger.warning(
            "could not record the rack picks",
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
```

In `print_run.py`, import `save_picks` with the other `rack.usage` names, and in the plate
loop right after `outcome = await slice_and_queue(...)` add:

```python
        await save_picks(rack, printer_id, outcome.queue_item_ids, outcome.rack_picks)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/rack/test_save_picks.py tests/api/test_print_rack.py tests/api/test_print_run_choices.py -v && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/rack/usage.py backend/scadbuddy/bambuddy/print_run.py backend/tests/rack/test_save_picks.py backend/tests/api/test_print_rack.py
git commit -m "feat(rack): record each sent pick against its queue item" -m "Refs #836, #1015"
```

---

### Task 11: The settle write

**Files:**
- Modify: `backend/scadbuddy/bambuddy/watcher.py` (`PrintWatcher.__init__`, `_loop`, a `_settled`)
- Modify: `backend/scadbuddy/rack/usage.py` (`record_settled`, `settle_hook`)
- Modify: `backend/scadbuddy/rack/component.py` (register the hook)
- Modify: `backend/scadbuddy/core/components.py` (`Core`)
- Test: `backend/tests/bambuddy/test_watcher.py`, `backend/tests/rack/test_settle.py` (new), `backend/tests/bambuddy/test_progress.py`, `backend/tests/api/test_print_rack.py`

**Interfaces:**
- Consumes: `PrintLinkStore.for_output`, `BambuddyClient.archive`, `RackUsage.picked_items`/`record_prints`.
- Produces: `SettledHook = Callable[[OutputMeta], Awaitable[None]]` in `watcher.py`;
  `PrintWatcher(..., on_settled: Sequence[SettledHook] = ())` and its list attribute
  `on_settled`; `record_settled(output_id: str, *, client: ArchiveReader, links: LinkReader, store: RackUsage, now: Callable[[], datetime] = ...) -> int`;
  `settle_hook(store: RackUsage, links: PrintLinkStore, load: Callable[[], StoredSettings]) -> SettledHook`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/bambuddy/test_watcher.py`:

```python
def test_a_settle_hook_runs_once_after_print_settled_is_published(paths: DataPaths) -> None:
    async def scenario() -> tuple[list[list[str]], list[Event]]:
        write_output(paths)
        watcher, seen = watcher_for(paths, Script(progress("running"), progress("done", settled=True, done=1)))
        at_hook: list[list[str]] = []

        async def hook(meta: OutputMeta) -> None:
            at_hook.append(kinds(seen))

        watcher.on_settled.append(hook)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return at_hook, seen

    at_hook, seen = asyncio.run(scenario())
    assert at_hook == [["print.progress", "print.progress", "print.settled"]]
    assert kinds(seen).count("print.settled") == 1


def test_a_failing_settle_hook_still_settles_and_forgets_the_print(paths: DataPaths) -> None:
    async def scenario() -> tuple[MemoryPrintLog, list[Event]]:
        write_output(paths)
        log = MemoryPrintLog({OUTPUT: NOW})
        watcher, seen = watcher_for(paths, Script(progress("done", settled=True, done=1)), prints=log)

        async def hook(meta: OutputMeta) -> None:
            raise RuntimeError("the database went away")

        watcher.on_settled.append(hook)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return log, seen

    log, seen = asyncio.run(scenario())
    assert kinds(seen) == ["print.progress", "print.settled"]
    assert asyncio.run(log.printed_at(OUTPUT)) is None


def test_an_output_never_printed_through_the_queue_never_reaches_the_hook(paths: DataPaths) -> None:
    async def scenario() -> int:
        write_output(paths)
        watcher, _ = watcher_for(paths, Script(None))
        calls: list[str] = []

        async def hook(meta: OutputMeta) -> None:
            calls.append(meta.id)

        watcher.on_settled.append(hook)
        watcher.watch(OUTPUT)
        await until_idle(watcher)
        return len(calls)

    assert asyncio.run(scenario()) == 0
```

`backend/tests/rack/test_settle.py`:

```python
"""``record_settled`` (#836, spec 2026-10-01 §4), on Postgres."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from scadbuddy.bambuddy.models import ArchiveDetail
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.usage import PickedHotend, RackUsageStore, record_settled
from tests.rack.helpers import serial

pytestmark = pytest.mark.requires_postgres

OUTPUT = "c" * 32
AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
A = serial(19)


class Links:
    def __init__(self, *links: PrintLink) -> None:
        self.links = list(links)

    async def for_output(self, output_id: str) -> list[PrintLink]:
        return self.links


class Archives:
    def __init__(self, *archives: ArchiveDetail, failing: set[int] | None = None) -> None:
        self.by_id = {archive.id: archive for archive in archives}
        self.failing = failing or set()

    async def archive(self, archive_id: int) -> ArchiveDetail:
        if archive_id in self.failing:
            raise ApiError(503, f"archive {archive_id} unreadable near {A}")
        return self.by_id[archive_id]


def link(archive_id: int, queue_item_id: int | None) -> PrintLink:
    matched = "queue_item" if queue_item_id is not None else "content_hash"
    return PrintLink(archive_id=archive_id, matched_by=matched, queue_item_id=queue_item_id)


@pytest.fixture
async def store(pg_conninfo: str) -> AsyncIterator[RackUsageStore]:
    opened = RackUsageStore(pg_conninfo)
    await opened.record_picks(51, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    try:
        yield opened
    finally:
        opened.close()


async def settle(store: RackUsageStore, links: Links, archives: Archives) -> int:
    return await record_settled(OUTPUT, client=archives, links=links, store=store, now=lambda: AT)


async def test_each_linked_archive_of_a_picked_item_is_one_print(store: RackUsageStore) -> None:
    """A ``quantity`` 2 item: two archives, two prints."""
    archives = Archives(
        ArchiveDetail(id=101, actual_time_seconds=600, print_time_seconds=900, filament_used_grams=5.0),
        ArchiveDetail(id=102, actual_time_seconds=None, print_time_seconds=900, filament_used_grams=None),
    )
    assert await settle(store, Links(link(101, 51), link(102, 51)), archives) == 2
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds, usage.grams) == (2, 1500, 5.0)


async def test_a_second_settle_of_the_same_print_changes_nothing(store: RackUsageStore) -> None:
    archives = Archives(ArchiveDetail(id=101, actual_time_seconds=600))
    await settle(store, Links(link(101, 51)), archives)
    assert await settle(store, Links(link(101, 51)), archives) == 0
    assert (await store.usage([A]))[A].prints == 1


async def test_an_archive_with_no_queue_item_or_no_picks_is_not_counted(store: RackUsageStore) -> None:
    archives = Archives(ArchiveDetail(id=101), ArchiveDetail(id=103))
    assert await settle(store, Links(link(101, None), link(103, 99)), archives) == 0


async def test_a_second_print_of_one_output_counts_only_its_own_archives(store: RackUsageStore) -> None:
    """Review Focus 5: the second settle walks the first print's archive too."""
    await settle(store, Links(link(101, 51)), Archives(ArchiveDetail(id=101, actual_time_seconds=60)))
    await store.record_picks(52, 1, [PickedHotend(group_id=0, position=4, serial=A)])
    archives = Archives(
        ArchiveDetail(id=101, actual_time_seconds=9999), ArchiveDetail(id=102, actual_time_seconds=40)
    )
    assert await settle(store, Links(link(101, 51), link(102, 52)), archives) == 1
    usage = (await store.usage([A]))[A]
    assert (usage.prints, usage.print_seconds) == (2, 100)


async def test_an_unreadable_archive_is_logged_by_type_and_the_rest_are_written(
    store: RackUsageStore, caplog: pytest.LogCaptureFixture
) -> None:
    archives = Archives(ArchiveDetail(id=102, actual_time_seconds=40), failing={101})
    with caplog.at_level(logging.DEBUG):
        assert await settle(store, Links(link(101, 51), link(102, 51)), archives) == 1
    [record] = [r for r in caplog.records if r.name == "scadbuddy.rack.usage"]
    assert record.getMessage() == "could not record a rack nozzle's print"
    assert (getattr(record, "output_id"), getattr(record, "archive_id"), getattr(record, "error")) == (
        OUTPUT, 101, "ApiError",
    )
    assert A not in repr(record.__dict__) and record.exc_info is None
```

Append to `backend/tests/bambuddy/test_progress.py`:

```python
@pytest.mark.parametrize("picked", ["ranked", "manual"])
def test_a_stale_rack_pick_shows_bambuddys_own_words(picked: str) -> None:
    """Spec §5: a pick that no longer fits fails the item at dispatch, ranked or manual
    alike (the item carries no trace of which); the progress shows Bambuddy's message."""
    item = QueueItem(id=51, status="failed", error_message="Nozzle rack pick no longer fits the printer")
    shown = from_queue(item, bambuddy_url="http://bambuddy.test/queue")
    assert (shown.stage, shown.settled) == ("failed", True)
    assert shown.error_message == "Nozzle rack pick no longer fits the printer"
```

Append to `backend/tests/api/test_print_rack.py`:

```python
def test_the_rack_feature_hooks_the_settle_write_into_the_watcher(client: TestClient) -> None:
    state = getattr(client.app.state, STATE_ATTR)  # type: ignore[attr-defined]
    assert len(state.print_watcher.on_settled) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_watcher.py tests/rack/test_settle.py tests/bambuddy/test_progress.py tests/api/test_print_rack.py -v`
Expected: FAIL with `AttributeError: 'PrintWatcher' object has no attribute 'on_settled'` and
`ImportError: cannot import name 'record_settled'`. The stale-pick test passes at once:
it pins existing behavior.

- [ ] **Step 3: Implement the watcher hook**

In `watcher.py`: import `Sequence` from `collections.abc`; below `Reader = ...` add:

```python
#: Awaited on the read that finds a print settled (#836): after its ``print.settled`` is
#: published, before it is forgotten.
SettledHook = Callable[[OutputMeta], Awaitable[None]]
```

Add `on_settled: Sequence[SettledHook] = (),` to `PrintWatcher.__init__`'s keywords (after
`now`), and in the body:

```python
        #: Each runs on the settled branch only; what one raises is logged by type and the
        #: watch ends as before. A feature registers itself here (``rack/component.py``).
        self.on_settled: list[SettledHook] = list(on_settled)
```

Add the method:

```python
    async def _settled(self, meta: OutputMeta) -> None:
        for hook in self.on_settled:
            try:
                await hook(meta)
            except Exception as exc:
                # Type only: a hook's error can carry data it must not log (#836, spec §7).
                logger.warning(
                    "a settled-print hook failed",
                    extra={"output_id": meta.id, "error": type(exc).__name__},
                )
```

In `_loop`, replace

```python
            changed = self.observer.observe(meta, progress)
            if progress is None or progress.settled:
```

with

```python
            changed = self.observer.observe(meta, progress)
            if progress is not None and progress.settled:
                # After observe, so print.settled is already published (spec §4). Not on
                # progress None: an output never printed through slice_queue has no picks.
                await self._settled(meta)
            if progress is None or progress.settled:
```

- [ ] **Step 4: Implement the settle write and its registration**

In `rack/usage.py` add imports `from collections.abc import Awaitable, Callable`,
`from datetime import UTC`, `from scadbuddy.bambuddy.client import client_for`,
`from scadbuddy.bambuddy.models import ArchiveDetail`,
`from scadbuddy.bambuddy.print_links import PrintLink, PrintLinkStore`,
`from scadbuddy.library.outputs import OutputMeta`,
`from scadbuddy.library.settings_store import StoredSettings`, and:

```python
class ArchiveReader(Protocol):
    async def archive(self, archive_id: int) -> ArchiveDetail: ...


class LinkReader(Protocol):
    async def for_output(self, output_id: str) -> list[PrintLink]: ...


def _now() -> datetime:
    return datetime.now(UTC)


async def record_settled(
    output_id: str,
    *,
    client: ArchiveReader,
    links: LinkReader,
    store: RackUsage,
    now: Callable[[], datetime] = _now,
) -> int:
    """One ``rack_nozzle_prints`` row per linked archive and picked group (spec §4); the
    rows written. An archive linked by hash has no queue item and is not counted. Each
    failure is logged by type and ids and skipped; nothing is retried."""
    try:
        linked = [link for link in await links.for_output(output_id) if link.queue_item_id is not None]
        picked = await store.picked_items(link.queue_item_id for link in linked if link.queue_item_id)
    except Exception as exc:
        logger.warning(
            "could not read a settled print's rack picks",
            extra={"output_id": output_id, "error": type(exc).__name__},
        )
        return 0
    written = 0
    for link in linked:
        if link.queue_item_id is None or link.queue_item_id not in picked:
            continue
        try:
            archive = await client.archive(link.archive_id)
            seconds = (
                archive.actual_time_seconds
                if archive.actual_time_seconds is not None
                else archive.print_time_seconds
            )
            written += await store.record_prints(
                archive_id=link.archive_id,
                queue_item_id=link.queue_item_id,
                settled_at=now(),
                print_seconds=seconds,
                grams=archive.filament_used_grams,
            )
        except Exception as exc:
            logger.warning(
                "could not record a rack nozzle's print",
                extra={"output_id": output_id, "archive_id": link.archive_id, "error": type(exc).__name__},
            )
    return written


def settle_hook(
    store: RackUsage, links: PrintLinkStore, load: Callable[[], StoredSettings]
) -> Callable[[OutputMeta], Awaitable[None]]:
    """The watcher's ``on_settled`` hook for the rack (spec §4)."""

    async def hook(meta: OutputMeta) -> None:
        if not links.available:
            return
        async with client_for(load()) as client:
            await record_settled(meta.id, client=client, links=links, store=store)

    return hook
```

In `core/components.py`'s `TYPE_CHECKING` block add:

```python
    from scadbuddy.bambuddy.print_links import PrintLinkStore
    from scadbuddy.bambuddy.watcher import PrintWatcher
    from scadbuddy.library.settings_store import SettingsStore
```

and to `Core`:

```python
    settings_store: SettingsStore
    print_links: PrintLinkStore
    print_watcher: PrintWatcher
```

In `rack/component.py`, import `settle_hook` and change `_build`:

```python
def _build(core: Core, components: Components) -> RackUsageStore:
    store = RackUsageStore(core.settings.database_url)
    # The watcher is a core service and must not import a feature: the rack registers its
    # settle write itself (spec §4).
    core.print_watcher.on_settled.append(
        settle_hook(store, core.print_links, core.settings_store.load)
    )
    return store
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/bambuddy/test_watcher.py tests/rack/test_settle.py tests/bambuddy/test_progress.py tests/api/test_print_rack.py tests/test_components.py -v && uv run --frozen mypy`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/bambuddy/watcher.py backend/scadbuddy/rack/usage.py backend/scadbuddy/rack/component.py backend/scadbuddy/core/components.py backend/tests/bambuddy/test_watcher.py backend/tests/rack/test_settle.py backend/tests/bambuddy/test_progress.py backend/tests/api/test_print_rack.py
git commit -m "feat(rack): count each settled print against the hotend it was picked for" -m "Refs #836"
```

---

### Task 12: The `/check` preview and the manual pick API

**Files:**
- Modify: `backend/scadbuddy/bambuddy/print_run.py` (`RackOption`, `RackPickView`, `PrintCheck.rack`, `rack_preview`, `_check_manual_pick`, `prepare_run`, `check_print`)
- Test: `backend/tests/api/test_print_rack.py`, `backend/tests/bambuddy/test_rack_chooser.py`

**Interfaces:**
- Consumes: `candidates_for`, `rank_rack`, `rack_warnings`, `eligible`, `SLICED_VOLUME_TYPE`, `abrasive_type`, `glow`.
- Produces: `RackOption(position, nozzle_diameter, flow, color, nozzle_type, material, prints, print_seconds)`;
  `RackPickView(group_id: int | None, position: int | None, reason: str | None, unsafe_material: bool, glow_unchecked: bool, options: list[RackOption])`;
  `PrintCheck.rack: RackPickView | None`; `/run` and `/check` refuse a non-fitting
  `rack_position` with 422 / `errors` before any slice.

- [ ] **Step 1: Write the failing tests** (append to `tests/api/test_print_rack.py`)

```python
CHECK_04 = {"nozzles": [{"size": "0.4"}], "tier": "standard"}


def check(client: TestClient, output_id: str, **extra: Any) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/print/outputs/{output_id}/check", json={**body(**CHECK_04), **extra}
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


@respx.mock
def test_the_check_previews_the_rack_sides_eligible_positions(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()

    rack = check(client, output_id)["rack"]

    assert [option["position"] for option in rack["options"]] == [2, 4, 6]
    assert {option["flow"] for option in rack["options"]} == {"standard"}
    assert rack["position"] in (2, 4, 6) and rack["reason"]
    assert rack["group_id"] is None


@respx.mock
def test_a_manual_pick_that_fits_is_previewed_and_sent(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    grouped_requirements_route(color="#00629B")  # position 2's color: the ranking prefers 2
    slice_routes()
    queued = queue_route()

    rack = check(client, output_id, rack_position=6)["rack"]
    assert (rack["position"], rack["reason"]) == (6, "chosen by hand")

    response = run_print(client, output_id, json={**body(**CHECK_04), "rack_position": 6})
    assert response.status_code == 200, response.text
    assert json.loads(queued.calls.last.request.content)["nozzle_rack_choice"] == {"0": 6}


@respx.mock
def test_a_manual_pick_that_does_not_fit_is_refused_before_anything_is_sliced(
    client: TestClient, model: str
) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    sliced = slice_routes()

    message = (
        "Rack position 1 holds a 0.2 mm Standard nozzle, and this prints with a 0.4 mm "
        "Standard nozzle. Choose another position, or Automatic."
    )
    assert check(client, output_id, rack_position=1)["errors"] == [message]
    response = run_print(client, output_id, json={**body(**CHECK_04), "rack_position": 1})
    assert (response.status_code, response.json()["detail"]) == (422, message)
    assert not sliced.called


@respx.mock
def test_a_high_flow_choice_is_judged_as_the_standard_slice_it_becomes(
    client: TestClient, model: str
) -> None:
    """Review Focus 2 (#484): an HH position is neither offered nor accepted."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    high_flow = {"nozzles": [{"size": "0.4", "flow": "high_flow"}], "tier": "standard"}

    response = client.post(
        f"/api/v1/print/outputs/{output_id}/check", json={**body(**high_flow), "rack_position": 3}
    )

    assert response.status_code == 200, response.text
    assert response.json()["errors"] == [
        "Rack position 3 holds a 0.4 mm High Flow nozzle, and this prints with a 0.4 mm "
        "Standard nozzle. Choose another position, or Automatic."
    ]
    assert [o["position"] for o in response.json()["rack"]["options"]] == [2, 4, 6]


@respx.mock
def test_an_abrasive_spool_is_warned_about_in_the_preview(client: TestClient, model: str) -> None:
    """The material table ships empty, so a CF spool always warns (spec §8, #1011)."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    spools = recording("inventory-spools.json")
    for spool in spools:
        spool["material"], spool["subtype"] = "PLA", "CF"
    respx.get(f"{API}/inventory/spools").mock(return_value=httpx.Response(200, json=spools))

    result = check(client, output_id)

    assert result["rack"]["unsafe_material"] is True
    assert "rack-unsafe-material" in {w["kind"] for w in result["warnings"]}


@respx.mock
def test_an_unreadable_rack_previews_nothing_and_refuses_nothing(client: TestClient, model: str) -> None:
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    respx.get(f"{API}/printers/1/status").mock(return_value=httpx.Response(503))

    result = check(client, output_id, rack_position=3)

    assert (result["rack"], result["errors"]) == (None, [])
```

Add `from typing import Any` and `from tests.bambuddy.conftest import recording` to the module's imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_print_rack.py -v`
Expected: FAIL with `KeyError: 'rack'` and a missing 422.

- [ ] **Step 3: Implement**

In `print_run.py`, extend the imports:

```python
from scadbuddy.bambuddy.filaments import normalise_colour
from scadbuddy.bambuddy.models import FlowType, Spool
from scadbuddy.rack.rank import (
    SLICED_VOLUME_TYPE,
    RackCandidate,
    RackGroup,
    abrasive_type,
    candidates_for,
    eligible,
    glow,
)
```

Below `PrintCheck`'s definition, add the models and give `PrintCheck` its field:

```python
class RackOption(BaseModel):
    """One eligible rack position, as the dialog lists it (spec 2026-10-01 §5). Built
    field by field from a :class:`RackCandidate`, which carries no serial."""

    position: int
    nozzle_diameter: str
    flow: FlowType
    color: str | None = None
    nozzle_type: str
    #: Decoded from the code through ``rack.rank.NOZZLE_MATERIALS``; ``None`` is unknown.
    material: str | None = None
    prints: int = 0
    print_seconds: int = 0


class RackPickView(BaseModel):
    """The rack side's predicted pick before Print (spec §5). A preview: the real pick is
    made per sliced group and can differ when the slicer splits the side."""

    #: ``None`` in the preview, which ranks the side as one group.
    group_id: int | None = None
    position: int | None = None
    reason: str | None = None
    unsafe_material: bool = False
    #: A chosen spool is not in the inventory, so Glow could not be checked (spec §3).
    glow_unchecked: bool = False
    options: list[RackOption] = Field(default_factory=list)
```

and in `PrintCheck` add:

```python
    #: The rack side's preview (#836); ``None`` with no readable rack.
    rack: RackPickView | None = None
```

Then add:

```python
def _rack_option(candidate: RackCandidate) -> RackOption:
    return RackOption(
        position=candidate.position,
        nozzle_diameter=candidate.nozzle_diameter,
        flow="high_flow" if candidate.high_flow else "standard",
        color=candidate.color,
        nozzle_type=candidate.nozzle_type,
        material=candidate.material,
        prints=candidate.prints,
        print_seconds=candidate.print_seconds,
    )


def _spool_material(spool: Spool) -> str:
    return f"{spool.material}-{spool.subtype}" if spool.subtype else spool.material


async def rack_preview(
    client: BambuddyClient,
    request: PrintRunRequest,
    settings: StoredSettings,
    *,
    printer_id: int,
    status: PrinterStatus | None,
    rack: RackUsage | None,
) -> tuple[RackPickView | None, list[FilamentWarning]]:
    """The rack side ranked as one group from the dialog's size and spools (spec §5):
    the preview ``/check`` shows. Judged on the flow the slice will carry (Standard until
    #484). Every chosen spool counts toward the material test, since the slice may put any
    of them on the rack side. Advisory: a failure previews nothing."""
    if status is None or not rack_positions(status.nozzle_rack):
        return None, []
    try:
        inventory = {spool.id: spool for spool in await client.spools()}
        chosen = [inventory.get(slot.spool_id) for slot in request.filament_plan.slots]
        known = [spool for spool in chosen if spool is not None]
        group = RackGroup(
            group_id=0,
            nozzle_diameter=request.choices.nozzles[0].size,
            volume_type=SLICED_VOLUME_TYPE,
            color=normalise_colour(known[0].rgba) if known else None,
            materials=tuple(dict.fromkeys(_spool_material(spool) for spool in known)),
            abrasive=any(
                abrasive_type(f"{spool.material} {spool.subtype or ''}")
                or glow(spool.material, spool.subtype)
                for spool in known
            ),
            glow_unchecked=len(known) < len(chosen),
            label="the rack side",
        )
        algorithm = request.rack_algorithm or settings.rack_algorithm(printer_id)
        serials = [e.serial_number for e in rack_positions(status.nozzle_rack).values()]
        usage = await rack.usage(serials) if rack is not None else {}
        manual = {0: request.rack_position} if request.rack_position is not None else {}
        picks = rank_rack([group], status.nozzle_rack, algorithm, usage, manual)
        options = candidates_for(group, status.nozzle_rack, algorithm, usage)
        warnings = rack_warnings([group], status.nozzle_rack, algorithm, picks, manual)
    except Exception as exc:
        logger.warning(
            "the rack preview could not be built",
            extra={"printer_id": printer_id, "error": type(exc).__name__},
        )
        return None, []
    pick = picks.get(0)
    return (
        RackPickView(
            position=pick.position if pick else None,
            reason=pick.reason if pick else None,
            unsafe_material=pick.unsafe_material if pick else False,
            glow_unchecked=group.glow_unchecked,
            options=[_rack_option(c) for c in sorted(options, key=lambda c: c.position)],
        ),
        warnings,
    )


def _check_manual_pick(request: PrintRunRequest, status: PrinterStatus | None) -> None:
    """Spec §5: a manual pick that cannot print this is a 422 before anything is sliced.
    Judged on the flow the slice will carry, which is what Bambuddy re-checks at
    dispatch. An unreadable rack refuses nothing: Bambuddy still re-checks it then."""
    if request.rack_position is None or status is None:
        return
    size = request.choices.nozzles[0].size
    held = rack_positions(status.nozzle_rack).get(request.rack_position)
    if held is not None and eligible(held, size, SLICED_VOLUME_TYPE):
        return
    holds = (
        "holds no hotend"
        if held is None
        else f"holds a {held.nozzle_diameter} mm {'High Flow' if held.high_flow else 'Standard'} nozzle"
    )
    raise RunRefusalError(
        f"Rack position {request.rack_position} {holds}, and this prints with a {size} mm "
        f"{SLICED_VOLUME_TYPE} nozzle. Choose another position, or Automatic."
    )
```

In `prepare_run`, after `await record_seen(rack, printer_id, printer_status)` add
`_check_manual_pick(request, printer_status)`.

In `check_print`, replace the final `return PrintCheck(warnings=...)` with:

```python
    rack_view, rack_notes = await rack_preview(
        client,
        request,
        settings,
        printer_id=prepared.printer_id,
        status=prepared.printer_status,
        rack=rack,
    )
    # The one mounted-nozzle advisory kept (#723): a warning, never a refusal.
    return PrintCheck(
        warnings=[*high_flow_warnings(prepared.printer_status, request.choices.nozzles), *rack_notes],
        rack=rack_view,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/api/test_print_rack.py tests/api/test_print_run_choices.py tests/api/test_print_library.py -v && uv run --frozen mypy`
Expected: PASS. If `test_a_manual_pick_that_does_not_fit...` reports a `nozzle_diameter`
of `"0.2"` vs `"0.20"`, the message prints the rack's own text, as the expected string does.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/bambuddy/print_run.py backend/tests/api/test_print_rack.py
git commit -m "feat(rack): preview the rack pick in the check and take a manual position" -m "Refs #836, #1016"
```

---

### Task 13: Serial-safety tests across the whole path

**Files:**
- Test: `backend/tests/rack/test_serials.py` (new), `backend/tests/api/test_print_rack.py`

**Interfaces:**
- Consumes: `rack_chooser`, `save_picks`, `record_settled`, `record_seen`, `/check`.
- Produces: none. These tests pin spec §7 and §9's logging and `/check` items.

- [ ] **Step 1: Write the tests**

`backend/tests/rack/test_serials.py`:

```python
"""Spec 2026-10-01 §7, §9: a full pick, enqueue and settle with invented serials,
including the failure paths of §5, leaves no serial in any log record."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

import pytest

from scadbuddy.bambuddy.filaments import FilamentPlan, FilamentWarning, SpoolOption
from scadbuddy.bambuddy.models import ArchiveDetail, SlotChoice
from scadbuddy.bambuddy.print_links import PrintLink
from scadbuddy.bambuddy.print_run import rack_chooser
from scadbuddy.core.problems import ApiError
from scadbuddy.rack.rank import Usage
from scadbuddy.rack.usage import PickedHotend, record_seen, record_settled, save_picks
from tests.bambuddy.test_rack_chooser import Reads, grouped
from tests.rack.helpers import INVENTED_SERIALS, requirement, slot, status


def leak(serial: str) -> str:
    return f'duplicate key value violates "rack_nozzle_picks_pkey": (serial)=({serial})'


class Leaky:
    """A store whose every call fails with a serial in its message, as a database's might."""

    async def seen(self, printer_id: int, serials: Iterable[str]) -> None:
        raise RuntimeError(leak(next(iter(serials))))

    async def usage(self, serials: Iterable[str]) -> dict[str, Usage]:
        raise RuntimeError(leak(next(iter(serials))))

    async def record_picks(self, queue_item_id: int, printer_id: int, picks: Sequence[PickedHotend]) -> int:
        raise RuntimeError(leak(picks[0].serial))

    async def picked_items(self, queue_item_ids: Iterable[int]) -> set[int]:
        return set(queue_item_ids)

    async def record_prints(
        self, *, archive_id: int, queue_item_id: int, settled_at: datetime,
        print_seconds: int | None, grams: float | None,
    ) -> int:
        raise RuntimeError(leak(INVENTED_SERIALS[2]))


class Archives:
    async def archive(self, archive_id: int) -> ArchiveDetail:
        if archive_id == 101:
            raise ApiError(503, f"archive read failed for hotend {INVENTED_SERIALS[3]}")
        return ArchiveDetail(id=archive_id, actual_time_seconds=60)


class Links:
    async def for_output(self, output_id: str) -> list[PrintLink]:
        return [
            PrintLink(archive_id=101, matched_by="queue_item", queue_item_id=51),
            PrintLink(archive_id=102, matched_by="queue_item", queue_item_id=51),
        ]


async def test_no_serial_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    rack = status(slot(2, serial_number=INVENTED_SERIALS[2]), slot(4, serial_number=INVENTED_SERIALS[4]))
    plan = FilamentPlan(slots=[SlotChoice(slot_id=1, spool_id=9)])
    warnings: list[FilamentWarning] = []
    with caplog.at_level(logging.DEBUG):
        choose = rack_chooser(
            Reads(grouped(requirement()), rack),
            printer_id=1,
            plan=plan,
            spools={1: SpoolOption(spool_id=9, material="PLA")},
            algorithm="least_used",
            manual_position=None,
            rack=Leaky(),
            warnings=warnings,
        )
        assert await choose(77) is None  # usage() failed: no choice, a warning
        await record_seen(Leaky(), 1, rack)
        await save_picks(Leaky(), 1, [51], [PickedHotend(group_id=0, position=2, serial=INVENTED_SERIALS[2])])
        await record_settled("c" * 32, client=Archives(), links=Links(), store=Leaky())

    assert len(caplog.records) >= 4
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__!r}"
        assert not [serial for serial in INVENTED_SERIALS if serial in text], record.getMessage()
        assert record.exc_info is None and record.exc_text is None
    assert [w.kind for w in warnings] == ["rack-left-to-bambuddy"]
    assert not [s for s in INVENTED_SERIALS if s in repr(warnings)]
```

Append to `backend/tests/api/test_print_rack.py`:

```python
@respx.mock
def test_no_serial_appears_anywhere_in_the_check_body(client: TestClient, model: str) -> None:
    """Spec §5, §9: searched as a string over the whole JSON, options included."""
    output_id = prepared(client, model)
    upload_route()
    run_routes()
    invented_rack_route()

    response = client.post(f"/api/v1/print/outputs/{output_id}/check", json=body(**CHECK_04))

    assert response.status_code == 200, response.text
    assert response.json()["rack"]["options"], "the check must have ranked a rack to be a real test"
    assert not [s for s in INVENTED_SERIALS if s in response.text]
```

Add `INVENTED_SERIALS` to the module's import from `tests.rack.helpers`.

- [ ] **Step 2: Run them**

Run: `cd backend && uv run --frozen pytest tests/rack/test_serials.py tests/api/test_print_rack.py -k "serial" -v`
Expected: PASS. A failure here is a real leak: fix the logging call it names, never the test.

- [ ] **Step 3: Run the whole backend suite**

Run: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`
Expected: PASS, no new skips with Postgres and Temporal up.

- [ ] **Step 4: Commit**

```bash
git add backend/tests/rack/test_serials.py backend/tests/api/test_print_rack.py
git commit -m "test(rack): no hotend serial reaches a log record or the check body" -m "Refs #836, #1011"
```

---

### Task 14: The print dialog and Settings

**Files:**
- Create: `frontend/src/components/print/RackNozzle.tsx`, `frontend/src/components/print/RackNozzle.test.tsx`, `frontend/src/mocks/features/rack.ts`
- Modify: `frontend/src/api/types.ts`, `frontend/src/api/client.ts` (after `putPrinterBedType` ~801)
- Modify: `frontend/src/lib/useRunPrint.ts`, `frontend/src/components/PrintPicker.tsx`
- Modify: `frontend/src/pages/settings/RememberedChoicesPanel.tsx`
- Modify (typed mock literals): `frontend/src/mocks/choices.ts:10`, `src/mocks/handlers.ts:2741` (`ChoicesView`) and `:2910` (`PrintCheck`), `src/mocks/features/library.ts:145` and `:170`, `src/pages/CustomizePage.test.tsx:685`, `src/lib/usePrintChoices.test.ts:13`
- Test: `frontend/src/components/PrintPicker.test.tsx`, `frontend/src/pages/settings/RememberedChoicesPanel.rack.test.tsx` (new)

**Interfaces:**
- Consumes: `PrintCheck.rack`, `ChoicesView.rack_algorithm`, `PrintRunRequest.rack_position` / `rack_algorithm`, `PUT /print/printers/{id}/rack-algorithm`, `RememberedChoices.printer_rack_algorithms`.
- Produces: `RackNozzleLine({ rack, algorithm })`, `RackNozzleStep({ rack, algorithm, position, onAlgorithm, onPosition })`,
  `ALGORITHM_LABELS`, `api.putPrinterRackAlgorithm(printerId, algorithm | null)`.

- [ ] **Step 1: Regenerate the API types and add the aliases**

Run: `cd frontend && pnpm gen:api`. Then in `src/api/types.ts`, next to `PrinterBedType`:

```ts
export type RackPickView = Schemas['RackPickView']
export type RackOption = Schemas['RackOption']
export type RackAlgorithm = NonNullable<Schemas['ChoicesView']['rack_algorithm']>
export type PrinterRackAlgorithm = Schemas['PrinterRackAlgorithm']
```

In `src/api/client.ts`, after `putPrinterBedType` (and import `PrinterRackAlgorithm`, `RackAlgorithm`):

```ts
  /** #836 — how this printer's rack nozzle is ranked; `null` forgets it (Least used). */
  putPrinterRackAlgorithm: (printerId: number, algorithm: RackAlgorithm | null) =>
    request<PrinterRackAlgorithm>(`/print/printers/${printerId}/rack-algorithm`, {
      method: 'PUT',
      body: JSON.stringify({ algorithm }),
    }),
```

Add `rack_algorithm: 'least_used',` to each `ChoicesView` literal and `rack: null,` to each
`PrintCheck` literal listed under **Files** (the two `{ errors: [], warnings: [] } satisfies PrintCheck`
become `{ errors: [], warnings: [], rack: null } satisfies PrintCheck`).

`src/mocks/features/rack.ts`:

```ts
import { HttpResponse, http } from 'msw'
import type { PrinterRackAlgorithm, RackAlgorithm } from '../../api/types'

/** #836 — the rack algorithm remembered per printer. */
const base = '/api/v1'
const state: { algorithms: Record<string, RackAlgorithm> } = { algorithms: {} }

export function reset(): void {
  state.algorithms = {}
}

export const handlers = [
  http.put(`${base}/print/printers/:id/rack-algorithm`, async ({ params, request }) => {
    const id = String(params['id'])
    const body = (await request.json()) as { algorithm: RackAlgorithm | null }
    if (body.algorithm === null) delete state.algorithms[id]
    else state.algorithms[id] = body.algorithm
    return HttpResponse.json({
      printer_id: Number(id),
      algorithm: state.algorithms[id] ?? 'least_used',
    } satisfies PrinterRackAlgorithm)
  }),
]
```

- [ ] **Step 2: Write the failing component tests**

`src/components/print/RackNozzle.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RackPickView } from '../../api/types'
import { RackNozzleLine, RackNozzleStep } from './RackNozzle'

const rack: RackPickView = {
  group_id: null,
  position: 3,
  reason: 'already loaded with this color',
  unsafe_material: false,
  glow_unchecked: false,
  options: [
    { position: 2, nozzle_diameter: '0.4', flow: 'standard', color: '#00629B', nozzle_type: 'HS01', material: null, prints: 4, print_seconds: 7200 },
    { position: 3, nozzle_diameter: '0.4', flow: 'standard', color: '#FF6A13', nozzle_type: 'HS01', material: null, prints: 0, print_seconds: 0 },
  ],
}

describe('RackNozzleLine (#836)', () => {
  it('names the position, its size and flow, and the reason', () => {
    render(<RackNozzleLine rack={rack} algorithm="least_used" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent(
      'Rack nozzle: position 3 (0.4 Standard) — already loaded with this color',
    )
  })

  it('says Bambuddy picks when the algorithm leaves it to Bambuddy', () => {
    render(<RackNozzleLine rack={{ ...rack, position: null, reason: null }} algorithm="bambuddy" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent('Rack nozzle: Bambuddy picks at dispatch')
  })

  it('says when Glow could not be checked', () => {
    render(<RackNozzleLine rack={{ ...rack, glow_unchecked: true }} algorithm="least_used" />)
    expect(screen.getByText(/Glow could not be checked/)).toBeInTheDocument()
  })

  it('shows nothing without a rack', () => {
    const { container } = render(<RackNozzleLine rack={null} algorithm="least_used" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('RackNozzleStep (#836)', () => {
  it('changes the algorithm and picks a position by hand, or goes back to Automatic', () => {
    const onAlgorithm = vi.fn()
    const onPosition = vi.fn()
    render(
      <RackNozzleStep rack={rack} algorithm="least_used" position={null} onAlgorithm={onAlgorithm} onPosition={onPosition} />,
    )
    fireEvent.change(screen.getByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    expect(onAlgorithm).toHaveBeenCalledWith('oldest_first')
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '2' } })
    expect(onPosition).toHaveBeenLastCalledWith(2)
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '' } })
    expect(onPosition).toHaveBeenLastCalledWith(null)
    expect(screen.getByRole('option', { name: /Position 2 · 0.4 Standard · material unknown · 4 prints/ })).toBeInTheDocument()
  })
})
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd frontend && pnpm test src/components/print/RackNozzle.test.tsx`
Expected: FAIL with `Failed to resolve import "./RackNozzle"`.

- [ ] **Step 4: Implement the component**

`src/components/print/RackNozzle.tsx`:

```tsx
import type { RackAlgorithm, RackOption, RackPickView } from '../../api/types'

/** #836 — the four ways to rank the rack (spec 2026-10-01 §4). */
export const ALGORITHM_LABELS: Record<RackAlgorithm, string> = {
  least_used: 'Least used',
  oldest_first: 'Oldest first',
  newest_first: 'Newest first',
  bambuddy: 'Let Bambuddy pick',
}

const flowLabel = (option: RackOption) => (option.flow === 'high_flow' ? 'High Flow' : 'Standard')

/**
 * #836 — Simple mode's one line: the rack position ScadBuddy would pick for the rack
 * side, and why. A preview: the run picks per sliced group, and its result says what was
 * sent. Warnings (an unsafe material) come through the check's verdict and never hold Print.
 */
export function RackNozzleLine({ rack, algorithm }: { rack: RackPickView | null | undefined; algorithm: RackAlgorithm }) {
  if (!rack) return null
  const picked = rack.options?.find((option) => option.position === rack.position)
  let text: string | null = null
  if (picked) text = `Rack nozzle: position ${picked.position} (${picked.nozzle_diameter} ${flowLabel(picked)}) — ${rack.reason ?? ''}`
  else if (algorithm === 'bambuddy') text = 'Rack nozzle: Bambuddy picks at dispatch'
  if (text === null) return null
  return (
    <div className="text-[12.5px] text-ink">
      <p data-testid="rack-nozzle-line">{text}</p>
      {rack.glow_unchecked && (
        <p className="text-[12px] text-muted">A spool is not in the inventory, so Glow could not be checked.</p>
      )}
    </div>
  )
}

interface StepProps {
  rack: RackPickView | null | undefined
  algorithm: RackAlgorithm
  position: number | null
  onAlgorithm: (next: RackAlgorithm) => void
  onPosition: (next: number | null) => void
}

/** #836 — Advanced mode: the algorithm (remembered per printer) and a hand-picked position. */
export function RackNozzleStep({ rack, algorithm, position, onAlgorithm, onPosition }: StepProps) {
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Rack nozzle</legend>
      <label className="mt-1.5 flex items-center gap-2 text-[12px] text-muted">
        Algorithm
        <select
          aria-label="Rack algorithm"
          value={algorithm}
          onChange={(event) => onAlgorithm(event.target.value as RackAlgorithm)}
          className="rounded-[4px] border border-line bg-surface-1 px-1.5 py-0.5 text-ink"
        >
          {(Object.keys(ALGORITHM_LABELS) as RackAlgorithm[]).map((value) => (
            <option key={value} value={value}>
              {ALGORITHM_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      <label className="mt-1.5 flex items-center gap-2 text-[12px] text-muted">
        Nozzle
        <select
          aria-label="Rack nozzle position"
          value={position === null ? '' : String(position)}
          onChange={(event) => onPosition(event.target.value === '' ? null : Number(event.target.value))}
          className="rounded-[4px] border border-line bg-surface-1 px-1.5 py-0.5 text-ink"
        >
          <option value="">Automatic</option>
          {(rack?.options ?? []).map((option) => (
            <option key={option.position} value={option.position}>
              {`Position ${option.position} · ${option.nozzle_diameter} ${flowLabel(option)} · ${option.material ?? 'material unknown'} · ${option.prints} prints${option.color ? ` · ${option.color}` : ''}`}
            </option>
          ))}
        </select>
      </label>
    </fieldset>
  )
}
```

Run: `cd frontend && pnpm test src/components/print/RackNozzle.test.tsx` → PASS.

- [ ] **Step 5: Write the failing dialog and Settings tests**

Append to `src/components/PrintPicker.test.tsx`:

```tsx
describe('PrintPicker · rack nozzle (#836)', () => {
  const rack = {
    group_id: null,
    position: 3,
    reason: 'already loaded with this color',
    unsafe_material: false,
    glow_unchecked: false,
    options: [
      { position: 2, nozzle_diameter: '0.4', flow: 'standard', color: '#00629B', nozzle_type: 'HS01', material: null, prints: 4, print_seconds: 7200 },
      { position: 3, nozzle_diameter: '0.4', flow: 'standard', color: '#FF6A13', nozzle_type: 'HS01', material: null, prints: 0, print_seconds: 0 },
    ],
  }

  it('shows the pick and the unsafe-material warning in Simple mode without holding Print', async () => {
    server.use(
      http.post('/api/v1/print/outputs/:id/check', () =>
        HttpResponse.json({
          errors: [],
          warnings: [{ kind: 'rack-unsafe-material', slot_id: null, message: 'No hardened 0.4 nozzle in the rack for PLA-CF; position 3 is not known to be hardened.' }],
          rack: { ...rack, unsafe_material: true },
        }),
      ),
    )
    renderPicker()
    await loaded()
    expect(await screen.findByTestId('rack-nozzle-line')).toHaveTextContent('position 3 (0.4 Standard)')
    expect(await screen.findByTestId('print-verdict-warning')).toHaveTextContent('No hardened 0.4 nozzle')
    expect(screen.getByTestId('run-print')).toBeEnabled()
  })

  it('sends a hand-picked position and the chosen algorithm, and remembers the algorithm', async () => {
    server.use(http.post('/api/v1/print/outputs/:id/check', () => HttpResponse.json({ errors: [], warnings: [], rack })))
    const runs = watch('POST', '/run')
    const puts = watch('PUT', '/rack-algorithm')
    renderPicker()
    await loaded()
    await showAdvanced()
    fireEvent.change(await screen.findByLabelText('Rack algorithm'), { target: { value: 'newest_first' } })
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '2' } })
    await waitFor(() => expect(screen.getByTestId('run-print')).toBeEnabled())
    fireEvent.click(screen.getByTestId('run-print'))

    await waitFor(() => expect(runs.bodies.length).toBe(1))
    expect(runs.bodies[0]).toMatchObject({ rack_position: 2, rack_algorithm: 'newest_first' })
    expect(puts.bodies).toEqual([{ algorithm: 'newest_first' }])
  })
})
```

`src/pages/settings/RememberedChoicesPanel.rack.test.tsx`:

```tsx
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BambuddyTargets } from '../../api/types'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { RememberedChoicesPanel } from './RememberedChoicesPanel'

describe('RememberedChoicesPanel · rack algorithm (#836)', () => {
  it('lists a remembered rack algorithm and forgets it', async () => {
    const forgot: unknown[] = []
    server.use(
      http.get('/api/v1/settings/remembered', () => HttpResponse.json({ printer_rack_algorithms: { '1': 'oldest_first' } })),
      http.put('/api/v1/print/printers/1/rack-algorithm', async ({ request }) => {
        forgot.push(await request.json())
        return HttpResponse.json({ printer_id: 1, algorithm: 'least_used' })
      }),
    )
    renderPage(<RememberedChoicesPanel targets={{ printers: [{ id: 1, name: 'H2C' }] } as BambuddyTargets} />)
    expect(await screen.findByText('Oldest first')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Forget rack nozzle for H2C' }))
    await waitFor(() => expect(forgot).toEqual([{ algorithm: null }]))
  })
})
```

Run: `cd frontend && pnpm test src/components/PrintPicker.test.tsx src/pages/settings/RememberedChoicesPanel.rack.test.tsx`
Expected: FAIL: no `rack-nozzle-line`, no `Rack algorithm` select, no "Oldest first" row.

- [ ] **Step 6: Wire the dialog, the run and Settings**

`src/lib/useRunPrint.ts`: add to `RunInput`

```ts
  /** #836 — a hand-picked rack position, or `null` for Automatic. */
  rackPosition: number | null
  /** #836 — how the rack is ranked for this print. */
  rackAlgorithm: RackAlgorithm
```

(import `RackAlgorithm` from `../api/types`), destructure both in `useRunPrint`, add them to
the refusal-reset effect's dependency list, and add to the `body` literal after `options,`:

```ts
        ...(rackPosition === null ? {} : { rack_position: rackPosition }),
        rack_algorithm: rackAlgorithm,
```

`src/components/PrintPicker.tsx`:
- import `RackNozzleLine, RackNozzleStep` from `./print/RackNozzle`, and `RackAlgorithm` from `../api/types`;
- after the `options` state:

```tsx
  /** #836 — the rack's ranking for this print (remembered per printer) and a hand pick. */
  const [rackAlgorithm, setRackAlgorithm] = useState<RackAlgorithm>('least_used')
  const [rackPosition, setRackPosition] = useState<number | null>(null)
  const openedAlgorithm = choices?.rack_algorithm
  useEffect(() => {
    setRackAlgorithm(openedAlgorithm ?? 'least_used')
    setRackPosition(null)
  }, [openedAlgorithm, printerId])
  function changeRackAlgorithm(next: RackAlgorithm) {
    setRackAlgorithm(next)
    if (printerId !== null) void api.putPrinterRackAlgorithm(printerId, next).catch(() => undefined)
  }
```

- pass `rackPosition, rackAlgorithm,` to `useRunPrint({...})`;
- in `checkRequest`'s object, after `all_plates: allPlates,` add
  `...(rackPosition === null ? {} : { rack_position: rackPosition }), rack_algorithm: rackAlgorithm,`;
- in `close()`, add `setRackPosition(null)` beside `setOptions({})`;
- render the line above the verdict in both branches. Change
  `const verdict = (<PrintVerdict ... />)` to:

```tsx
  const verdict = (
    <>
      <RackNozzleLine rack={check.verdict?.rack} algorithm={rackAlgorithm} />
      <PrintVerdict verdict={checkVerdict} error={check.error} onRetry={check.reload} />
    </>
  )
```

  and add `|| Boolean(check.verdict?.rack)` to `verdictShown`;
- in the Advanced block, directly after `<NozzleStep ... />`:

```tsx
                  {check.verdict?.rack && (
                    <RackNozzleStep
                      rack={check.verdict.rack}
                      algorithm={rackAlgorithm}
                      position={rackPosition}
                      onAlgorithm={changeRackAlgorithm}
                      onPosition={setRackPosition}
                    />
                  )}
```

`src/pages/settings/RememberedChoicesPanel.tsx`: import `ALGORITHM_LABELS` from
`../../components/print/RackNozzle`, and after the `printer_bed_types` loop add:

```tsx
    for (const [printerId, algorithm] of Object.entries(remembered.printer_rack_algorithms ?? {})) {
      rows.push({
        key: `rack:${printerId}`,
        kind: 'Rack nozzle',
        subject: printerName(printerId),
        value: ALGORITHM_LABELS[algorithm],
        forget: () => api.putPrinterRackAlgorithm(Number(printerId), null),
      })
    }
```

- [ ] **Step 7: Run the frontend checks**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS (re-run once if a vitest test times out under load, per CLAUDE.md "Known flakes").

- [ ] **Step 8: Commit**

```bash
git add frontend/src/components/print/RackNozzle.tsx frontend/src/components/print/RackNozzle.test.tsx frontend/src/mocks/features/rack.ts frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/lib/useRunPrint.ts frontend/src/components/PrintPicker.tsx frontend/src/components/PrintPicker.test.tsx frontend/src/pages/settings/RememberedChoicesPanel.tsx frontend/src/pages/settings/RememberedChoicesPanel.rack.test.tsx frontend/src/mocks/choices.ts frontend/src/mocks/handlers.ts frontend/src/mocks/features/library.ts frontend/src/pages/CustomizePage.test.tsx frontend/src/lib/usePrintChoices.test.ts
git commit -m "feat(rack): show the rack pick in the print dialog and pick it by hand" -m "Refs #836"
```

---

### Task 15: Amend the spool-first spec and plan

**Files:**
- Modify: `docs/superpowers/specs/2026-09-27-spool-first-print-design.md` (§6, "Which rack nozzle is used", ~428)
- Modify: `docs/superpowers/plans/2026-09-27-spool-first-print.md` (Global constraint line 18, test line ~1269)

**Interfaces:**
- Consumes: the shipped feature (Tasks 3-14).
- Produces: documents that no longer contradict the rack spec.

- [ ] **Step 1: Amend the spool-first spec's §6.** Replace the bullet
  "**Which rack nozzle is used.** The printer picks the physical nozzle matching the sliced
  size and flow type; ScadBuddy does not set `nozzle_rack_choice`. ..." with:

```markdown
- **Which rack nozzle is used.** Superseded by
  `2026-10-01-rack-nozzle-selection-design.md` (#836): ScadBuddy ranks the H2C's rack per
  sliced filament group and sends `nozzle_rack_choice`, keyed by group id, on every
  slice-and-queue print; "Let Bambuddy pick" in Advanced mode restores Bambuddy's own
  choice. The slicer still decides which extruders the file prints on; ScadBuddy only
  tells it which sides have the size (`extruder_nozzle_stats`, §4.3, #834).
```

- [ ] **Step 2: Amend the spool-first plan.** Change line 18's constraint to
  `- ScadBuddy sends **no \`ams_mapping\`** (§6). It sent no \`nozzle_rack_choice\` either
  until #836 (\`2026-10-01-rack-nozzle-selection-design.md\`), which supersedes that
  decision.` and, directly above the plan's
  `assert sent.get("nozzle_rack_choice") is None` (~1269), add the line
  `    # Superseded by #836: true only when no group prints from the rack.`

- [ ] **Step 3: Verify nothing else still claims ScadBuddy never sends it**

Run: `grep -rn "nozzle_rack_choice" docs/ backend/scadbuddy | grep -v "2026-10-01-rack-nozzle\|2026-10-02-rack-nozzle"`
Expected: every remaining hit either describes the new behavior or names #836 as superseding it.

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers/specs/2026-09-27-spool-first-print-design.md docs/superpowers/plans/2026-09-27-spool-first-print.md
git commit -m "docs(836): the rack spec supersedes the spool-first 'no nozzle_rack_choice' decision" -m "Refs #836"
```

- [ ] **Step 5: The PR body carries the rollout note (#1011).** When the branch's PR is
  opened, its body includes, under its own heading, the §8 rollout paragraph Task 1 wrote
  (every abrasive print warns `rack-unsafe-material` until the material table is filled),
  and `Fixes #836`, `Fixes #1011`, `Fixes #1012`, `Fixes #1015`, `Fixes #1016`.
