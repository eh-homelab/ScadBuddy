# Length per print and the density check (slice D) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every LAN print books the filament length the sliced plate gives beside its grams, the spool's metrics show grams and metres consumed and the density they imply against the type's, and a weighing's drift sentence names a likely cause when one fits.

**Architecture:** The sliced plate's `slice_info.config` carries `used_m` beside `used_g` per filament; the reader that gives a LAN start its grams per slot (`slicing/slice_info.py`) gives the metres too, they ride in `LanUploaded` and `SlotUse` to `print_uses.py`, which writes `data.length_m` on the `print_started` event, and `prints.book_end` copies it onto the `print_ended`. `metrics.py` sums the metres of the plates that carry one, derives the density from the grams of those same plates and the spool's diameter, and `SpoolMetrics` carries it with the type's density for the page. The drift's likely cause is a pure frontend rule over the weighing's breakdown, the start gap and the metrics. Nothing is booked from a mismatch.

**Tech Stack:** Python 3.12, psycopg, pydantic, FastAPI; React 19 + Vite, vitest, msw.

**Spec:** `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` §5.6 (with §5.2 for the drift sentence). Issue #7685, epic #7681.

**Measured:** Bambu Studio writes `used_m` beside `used_g` on each `<filament>` of `Metadata/slice_info.config`: `backend/tests/printers/fixtures/rack-slice-info-h2c.config` (a real H2C slice, 2026-11-20) has `used_m="2.01" used_g="6.09"` for filament 1, and Bambuddy's own filament-requirements read of a sliced file answers `used_meters` (`backend/tests/bambuddy/recordings/filament-requirements.json`). `bambuddy/filaments3mf.py` parses `used_g` only today; `FilamentRequirement.used_meters` exists and is left at 0.

## Global Constraints

- The grams-left rule (`ledger.py` module docstring) is unchanged: a length is `data` on a print event, never a delta. Bambuddy's `bb_usage` rows keep no length.
- Event `data` key, exactly: `length_m` (metres, rounded to 0.01) on `print_started` and `print_ended` of `ref_kind` `print_run`. Absent (not `null`, not `0`) when the sliced plate gave no length for the slot; `0.0` on a `print_ended` of basis `not_applied` whose start had one, as its grams are 0.
- The density formula (spec §5.6): `grams / (metres × 100 × π × (diameter_mm / 20)²)` g/cm³; 1 m of 1.75 mm filament at 1.24 g/cm³ is 2.98 g. The grams in it are only those of the plates that carry a length.
- The mismatch tolerance is 5 % (`DENSITY_TOLERANCE = 0.05`): `|implied / type − 1| > 0.05`. A mismatch is a sentence, never a booking, never a notice badge.
- `SpoolMetrics` gains `used_m: float | None` (null when no plate carries a length), `implied_density_g_cm3: float | None` (null without metres, or with 0 metres), `density_g_cm3: float | None` (the spool's effective type density) and `density_mismatch: bool`.
- Every grams or metres the UI shows goes through the page's formatters: grams to one decimal with ` g`, metres to one decimal with ` m`, a density to two decimals with ` g/cm³`.
- No migration: the length lives in the events' JSONB `data`.
- Commits are conventional (`feat(filament): …`), each with the Co-Authored-By trailer the session carries.

## Review Focus

1. A spool whose plates predate this slice (no `length_m` on any event): `used_m` null, no density, no sentence, the metrics tile reads as before. Task 2's `test_plates_with_no_length_give_no_metres` and Task 3's `shows no metres or density for a spool with none`.
2. A plate with a length but 0 g, or 0 m with grams: no division by zero, no density from it. Task 2's `test_zero_metres_give_no_density`.
3. A sliced plate whose `used_m` is missing or not a number: the slot's grams still book, its `length_m` is absent. Task 1's `test_a_slot_with_no_length_books_grams_only`.
4. A failed plate (`not_applied`): its end books `length_m` `0.0` as it books 0 g, and the metrics count neither. Task 1's `test_settle_books_the_length_beside_the_grams`, Task 2's `test_a_failed_plate_adds_no_metres`.
5. The drift sentence before the history has loaded, or on a spool with no weighing breakdown: no cause, no crash. Task 4's `names no cause without a history`.
6. A 2.85 mm spool: the density uses its own diameter, not 1.75. Task 2's `test_the_density_uses_the_spools_diameter`.

---

### Task 1: The length rides with the grams

**Files:**
- Modify: `backend/scadbuddy/bambuddy/filaments3mf.py` (`_sliced_plates`, ~line 285: the `<filament>` parse)
- Modify: `backend/scadbuddy/slicing/slice_info.py` (`NativeSliceRead`, `plate_grams`, `read_sliced`)
- Modify: `backend/scadbuddy/workflows/print_models.py` (`LanUploaded`, ~line 318)
- Modify: `backend/scadbuddy/printers/print_activities.py` (`lan_upload` ~line 310, `_save_uses` ~line 425)
- Modify: `backend/scadbuddy/filament/print_uses.py` (`SlotUse`, `slot_uses`, `record_uses_in`)
- Modify: `backend/scadbuddy/filament/prints.py` (`OpenUse`, `_OPEN`, `book_end`)
- Modify: `backend/tests/support/sliced3mf.py` (`sliced_3mf`)
- Test: `backend/tests/slicing/test_slice_info.py`, `backend/tests/filament/test_print_uses.py`, `backend/tests/filament/test_prints.py`

**Interfaces:**
- Produces:
  - `filaments3mf.py`: each sliced `FilamentRequirement` gets `used_meters=round(metres, 2)` from the node's `used_m`; a missing or non-numeric `used_m` is `0.0` (the field's default), the grams still parse.
  - `slice_info.py`: `class PlateUses(BaseModel): grams_by_slot: dict[int, float]; length_by_slot: dict[int, float]` and `def plate_uses(payload, plate_id) -> PlateUses` (what `plate_grams` is today, with the metres beside; `_on_printed` moves both the same way; a slot with `used_meters` 0 is absent from `length_by_slot`). `plate_grams` stays as `plate_uses(...).grams_by_slot`. `NativeSliceRead.length_by_slot: dict[int, float] = {}` set by `read_sliced`.
  - `LanUploaded.length_by_slot: dict[int, float] = Field(default_factory=dict)` (empty in a history from before it). `lan_upload` sets both from one `plate_uses` read.
  - `SlotUse.length_m: float | None = None`; `slot_uses(spools, grams, started_at, lengths: Mapping[int, float] | None = None)`: a slot's `length_m` is `lengths[slot_id]` when present and above 0, else `None`.
  - `record_uses_in`: `data={"job": subtask}` plus `"length_m": use.length_m` when it is not `None`.
  - `prints.OpenUse.length_m: float | None` read as `(e.data ->> 'length_m')::float`; `book_end` adds `"length_m": use.length_m if applied else 0.0` to the end's `data` only when the start had one.
  - The CRUD wire (`api/crud_print.py` `…/filament/print-uses` body, `crud/print.py` `RemotePrintUses`) carries `SlotUse` as a model, so the field crosses with no route change; `test_crud_filament_print_uses.py` (if present) gets one case with `length_m`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/support/sliced3mf.py`: `sliced_3mf(..., metres: Mapping[int, list[float | None]] | None = None)`; a slot with a metres value writes `used_m="<value>"`, one with `None` writes no `used_m`.

`backend/tests/slicing/test_slice_info.py`:

```python
def test_plate_uses_reads_the_metres_beside_the_grams() -> None:
    payload = sliced_3mf(plates={1: (3600, [12.5, 3.0])}, metres={1: [4.2, None]}, gcode={1: b""})
    assert plate_uses(payload, 1) == PlateUses(grams_by_slot={1: 12.5, 2: 3.0}, length_by_slot={1: 4.2})
    assert read_sliced(payload, 1).length_by_slot == {1: 4.2}

def test_a_slot_with_no_length_books_grams_only() -> None:
    # `used_m="x"`: the grams still parse, the length is absent.
    payload = sliced_3mf(plates={1: (3600, [12.5])}, gcode={1: b""}).replace(b'used_g="12.5"', b'used_m="x" used_g="12.5"')
    assert plate_uses(payload, 1) == PlateUses(grams_by_slot={1: 12.5}, length_by_slot={})
```

`backend/tests/filament/test_print_uses.py`:

```python
def test_a_slot_carries_its_length() -> None:
    uses = slot_uses({1: _option(-1, red), 2: _option(-2, red)}, {1: 12.5, 2: 3.0}, STARTED, {1: 4.2})
    assert [u.length_m for u in uses] == [4.2, None]

@pg
def test_the_start_and_its_end_keep_the_length(pg_pool: PgPool) -> None:
    # record_uses with length_m=4.2 → the print_started's data has length_m 4.2;
    # settle_uses with stage "done" → the print_ended's data has length_m 4.2.

@pg
def test_settle_books_the_length_beside_the_grams(pg_pool: PgPool) -> None:
    # a start of 4.2 m that "failed" → print_ended delta 0, data["length_m"] == 0.0;
    # a start with no length → its print_ended has no "length_m" key.
```

`backend/tests/filament/test_prints.py`: `plate_ended` on a FINISH report copies the length (the existing report-end test, with a `length_m` on the start).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/slicing/test_slice_info.py tests/filament/test_print_uses.py tests/filament/test_prints.py -q -n 4`
Expected: FAIL (`PlateUses` undefined, unexpected keyword `lengths`, `length_m` missing).

- [ ] **Step 3: Implement the Interfaces above**

`_sliced_plates`' `used_m` parse mirrors its `used_g` parse (`float(node.get("used_m", "0"))` in its own `try`, `0.0` on `ValueError`). `plate_uses` factors the body of today's `plate_grams`.

- [ ] **Step 4: Run the tests, ruff, mypy**

Run: the pytest line above; `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(filament): a LAN print books its filament length beside the grams (#7685)"
```

### Task 2: Metres consumed and the implied density

**Files:**
- Modify: `backend/scadbuddy/filament/metrics_models.py` (`Plate`, `SpoolMetrics`)
- Modify: `backend/scadbuddy/filament/metrics.py` (`plates`, `spool_metrics`, new `implied_density`)
- Modify: `backend/scadbuddy/filament/metrics_store.py` (`history`, ~line 160)
- Test: `backend/tests/filament/test_metrics.py`, `backend/tests/filament/test_metrics_store.py`

**Interfaces:**
- Consumes: Task 1's `data.length_m` on `print_ended` events.
- Produces:
  - `Plate.length_m: float | None` (from the end event's `data.length_m`; `None` for a BB usage row or an end with none).
  - `metrics.implied_density(grams: float, metres: float, diameter_mm: float) -> float | None`: the Global Constraints formula; `None` when `metres <= 0` or `grams <= 0`.
  - `DENSITY_TOLERANCE: float = 0.05`.
  - `spool_metrics(..., diameter_mm: float, density_g_cm3: float | None)` sets `used_m` (the sum over plates with a length; `None` when no plate has one), `implied_density_g_cm3` (over the grams and metres of those plates), `density_g_cm3` (the argument) and `density_mismatch` (both known and over the tolerance). A `not_applied` plate (0 g, `length_m` 0) adds nothing.
  - `metrics_store.history` passes `float(spool.effective.diameter_mm)` and `spool.effective.density_g_cm3` (as float, or None).

- [ ] **Step 1: Write the failing tests** (`test_metrics.py`'s `sb_end` helper gains `length_m: float | None = None`, written into `data`)

```python
def test_plates_carry_their_length() -> None:
    found = plates([sb_end(days(1), 2.98, "a", length_m=1.0), sb_end(days(2), 5, "b")])
    assert [p.length_m for p in found] == [1.0, None]

def test_implied_density_of_a_metre_of_pla() -> None:
    assert implied_density(2.98, 1.0, 1.75) == pytest.approx(1.239, abs=0.001)

def test_the_density_uses_the_spools_diameter() -> None:
    assert implied_density(7.9, 1.0, 2.85) == pytest.approx(1.239, abs=0.001)

def test_zero_metres_give_no_density() -> None:
    assert implied_density(5.0, 0.0, 1.75) is None
    assert implied_density(0.0, 1.0, 1.75) is None

def test_metrics_sum_the_metres_of_the_plates_that_have_one() -> None:
    # two plates with a length (2.98 g / 1 m, 5.96 g / 2 m), one without (10 g):
    # used_g 18.94, used_m 3.0, implied ≈ 1.239, density_g_cm3 1.24, no mismatch
def test_plates_with_no_length_give_no_metres() -> None:
    # used_m None, implied None, density_mismatch False (with a type density of 1.24)
def test_a_failed_plate_adds_no_metres() -> None:
    # a not_applied end with length_m 0.0 beside a 1 m plate: used_m 1.0
def test_a_density_off_by_more_than_five_percent_is_a_mismatch() -> None:
    # 3.3 g over 1 m (≈1.37) against 1.24: density_mismatch True; 3.0 g (≈1.247): False
```

`test_metrics_store.py`: the history of a spool with one lengthed plate answers `metrics.used_m` and `metrics.density_g_cm3` from its type.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_metrics.py tests/filament/test_metrics_store.py -q -n 4`
Expected: FAIL

- [ ] **Step 3: Implement the Interfaces above**

- [ ] **Step 4: Run the tests, ruff, mypy; then `uv run --frozen pytest tests/filament -q -n 4`**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(filament): metres consumed and the density the prints imply (#7685)"
```

### Task 3: The page shows metres, the density and a print's length

**Files:**
- Modify: `frontend/src/components/filament/SpoolMetricsPanel.tsx`
- Modify: `frontend/src/components/filament/SpoolTimeline.tsx` (the `print_ended` / `print_started` line, ~line 62)
- Modify: `frontend/src/mocks/features/filament/index.ts` (the history handler, ~line 1308; `metrics` gains the four fields, from the seed's events' `data.length_m`)
- Modify: `frontend/src/mocks/features/filament/seed.ts` (the `print_ended` of spool 2, ~line 515, gets `length_m: 11.7` beside `job`; spool 3's `run1/job-a` end gets `length_m: 6.7`)
- Test: `frontend/src/components/filament/SpoolMetricsPanel.test.tsx`, `SpoolTimeline.test.tsx`

**Interfaces:**
- Consumes: `SpoolMetrics.used_m`, `implied_density_g_cm3`, `density_g_cm3`, `density_mismatch`.
- Produces:
  - The Used tile's note ends with `, 11.7 m` when `used_m` is not null.
  - A tile `Density`: value `1.31 g/cm³` (the implied), `why` "No print has a length yet" when null; note "The type says 1.24 g/cm³" when the type has one, else "The type has no density". With `density_mismatch` the note is instead the sentence `The prints imply 1.31 g/cm³, 6% over the type's 1.24 g/cm³: check the type's density or the label.` ("under" for a lower value; the percentage rounded to a whole number).
  - The timeline's print line adds `4.2 m` after the grams when `data.length_m` is a number above 0.
  - `metricsOf` in the mock: `used_m` is the sum of `data.length_m` over the plates it counts (null when none), the density by the Global Constraints formula over those plates' grams with the spool's effective `diameter_mm`, `density_g_cm3` the effective type density, `density_mismatch` over 5 %.

- [ ] **Step 1: Write the failing tests**

```ts
it('shows the metres and the implied density beside the grams', ...)   // used_m 3, implied 1.24, type 1.24 → "3 m", "1.24 g/cm³", "The type says 1.24 g/cm³"
it('says when the prints imply another density than the type', ...)    // mismatch → the sentence with "6% over"
it('shows no metres or density for a spool with none', ...)            // used_m null → no " m", Density tile "—", "No print has a length yet"
// SpoolTimeline: a print_ended with length_m 4.2 reads "… 4.2 m"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/filament/SpoolMetricsPanel.test.tsx src/components/filament/SpoolTimeline.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement the Interfaces above** (`pnpm gen:api` first, with the backend of Task 2)

- [ ] **Step 4: Run the tests, then `pnpm exec vitest run src/mocks src/pages/filament/SpoolPage.test.tsx`, `pnpm lint`, `pnpm exec tsc -p tsconfig.app.json --noEmit`**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(filament): the spool page shows metres, the implied density and a print's length (#7685)"
```

### Task 4: The drift's likely cause

**Files:**
- Create: `frontend/src/components/filament/driftCause.ts`
- Modify: `frontend/src/components/filament/WeighingBreakdown.tsx` (a `cause` prop, appended as a sentence)
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (~line 1008: computes the cause from `saved.last_weighing.data`, the start gap of `GramsBasis`' rule and `history?.metrics`)
- Modify: `frontend/src/components/filament/GramsBasis.tsx` (export `startGapG(anchor, addedFull, labelWeight): number | null`, the number the sentence already computes)
- Test: `frontend/src/components/filament/driftCause.test.ts`, `WeighingBreakdown.test.tsx`

**Interfaces:**
- Consumes: Task 3's metrics fields; `WeighingBreakdown`'s `Breakdown` data; `GramsBasis`' start gap.
- Produces:
  - `type DriftCause = { kind: 'label_weight'; gap: number } | { kind: 'density'; implied: number; type: number } | { kind: 'empty_weight' }`.
  - `driftCause(input: { driftG: number | null; emptyBasis: string | null; startGapG: number | null; usedG: number; impliedDensity: number | null; typeDensity: number | null; densityMismatch: boolean }): DriftCause | null`, checked in this order, the first fit wins, none for a `driftG` under 1 g in magnitude or null:
    1. `label_weight`: `startGapG != null` and `|driftG + startGapG| <= max(1, 0.1 × |startGapG|)` (the scale found exactly the gap the label over-promised).
    2. `density`: `densityMismatch` and `|driftG − usedG × (1 − typeDensity / impliedDensity)| <= max(1, 0.1 × |driftG|)` (the ledger took the slicer's grams; the type's density would have taken `usedG × type / implied`).
    3. `empty_weight`: `emptyBasis === 'placeholder'`.
  - The sentences (`WeighingBreakdown`, after the drift sentence): ` Likely cause: the label weight; the spool started 20.7 g under it.` / ` Likely cause: the type's density (1.24 g/cm³) disagrees with the prints' (1.31 g/cm³).` / ` Likely cause: the spool's empty weight is a placeholder.`

- [ ] **Step 1: Write the failing tests**

```ts
it('names the label weight when the drift is the start gap', ...)         // drift −20.7, gap 20.7 → label_weight
it('names the density when the drift is what the slicer over-counted', ...) // used 300 g, implied 1.31, type 1.24, drift ≈ −16 → density
it('names a placeholder empty weight last', ...)                           // basis placeholder, nothing else fits → empty_weight
it('names no cause under a gram of drift, or with no fit', ...)
it('names no cause without a history', ...)                                 // impliedDensity null, densityMismatch false, gap null, basis catalog → null
// WeighingBreakdown: each cause's sentence
```

- [ ] **Step 2: Run them to verify they fail**

- [ ] **Step 3: Implement the Interfaces above**

- [ ] **Step 4: Run `pnpm exec vitest run src/components/filament src/pages/filament/SpoolPage.test.tsx`, `pnpm lint`, the typecheck**

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(filament): the drift sentence names a likely cause (#7685)"
```

### Task 5: Docs and the full checks

**Files:**
- Modify: `backend/CLAUDE.md` (the `print_uses.py` and `metrics.py` notes: the length and the density)
- Modify: `docs/features/spools-and-filament.md` (a section "Length and the density check (#7685)")
- Modify: `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` (§5.6: an "As built in #7685" bullet with the measurement above and the cause order)
- Modify: `agent/src/tools/filament.ts` (`filament_spool_history`'s description names `used_m`, `implied_density_g_cm3` and `density_mismatch`)

- [ ] **Step 1: Write the docs**
- [ ] **Step 2: Run the full checks**

Backend: ruff, format, mypy, `pytest tests/filament tests/slicing tests/printers -q -n 4`. Frontend: `pnpm lint && pnpm typecheck && pnpm exec vitest run src/components/filament src/pages/filament src/mocks`. Agent: `pnpm exec tsc --noEmit`, `pnpm exec vitest run test/filament.test.ts test/coverage.test.ts`.

- [ ] **Step 3: Commit, push, open the PR** (`Fixes #7685`, the owner marker, the footer).
