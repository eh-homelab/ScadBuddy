# A new spool's first weighing under the label weight (slice B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a new, unused spool reads under its label weight, ScadBuddy names the shortfall and three readings of the situation with their numbers, the person picks one before the weighing is recorded, and the choice stays on the event.

**Architecture:** The #7280 check (`insert_weights.check_on`) grows one more answer, `below_label`, with the shortfall and the resolutions; the weighing API takes `resolution` beside `reading_g`, and the store checks it again under the row locks, books the side effect (`added_full` false, or a new label weight) as an `edited` event, then the weighing with slice A's breakdown. One frontend component shows the choice in the weigh form and on the phone; the agent tool offers the choices the way it offers the insert weight.

**Tech Stack:** Python 3.12, psycopg, pydantic, FastAPI; React 19 + Vite, vitest, msw; the agent's zod tools.

**Spec:** `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` §5.3 (and §5.1 for the breakdown). Issue #7683, epic #7681. Builds on slice A (#7682, PR #7717): `filament/empty_weight.py` `effective_on`, `ledger.breakdown`, `ledger.expected_before`, `SpoolEventAdd.reading_g`, `NoEmptyWeightError`, `ReadingBelowEmptyError`.

## Global Constraints

- "New" is #7280's rule, unchanged: `insert_weights.unused` (added full in effect: no consumption, grams left at the label within `FULL_TOLERANCE_G`). `below_label` needs an insert with a weight (the check still answers `no_insert` first; with no empty weight the client records the reading as filament, which is `tared` by nature, with no choice).
- `below_label` fires when `reading_g < label - FULL_TOLERANCE_G`, after the `not_full` check and before `offer_reason`. `shortfall_g = label - reading_g`.
- Resolutions, in this order, each with `filament_g`: `tared` (`reading_g`), `short` (`reading_g - empty_weight_g`, only when that is ≥ 0), `label_wrong` (`reading_g - empty_weight_g`, only when ≥ 0, and `new_label_weight_g = round half up of filament_g`, at least 1).
- `default` is `tared` when `shortfall_g <= 0.10 × label`, else `short`; when `short` is absent, `tared`.
- The event's `data` keeps slice A's keys plus `resolution` and `shortfall_g`. For `tared`, `empty_source` is `tared`, `empty_g` 0, `empty_basis` and `empty_name` null (a new `empty_source` value; §5.1 gets an "As built in #7683" line).
- A `short` weighing first writes `added_full = false` with an `edited` event (`data.changes = {"added_full": [before, false]}`); a `label_wrong` one first writes the spool's own `label_weight_g = new_label_weight_g` with an `edited` event (`data.changes = {"label_weight_g": [before, new]}`, `before` the spool's own value, null when it came from the type). Both at the weighing's `at`, booked before it (same transaction).
- A `resolution` the check does not offer now is 422 `no_below_label` with the check's `reason` (the way an accept without an offer is 409 `no_insert_weight_offer`); nothing is booked.
- Copy, exactly: heading "The reading is {shortfall} under the label weight. What is up?"; options "The scale was tared on the empty spool: {filament} of filament", "The spool is short: {filament} of filament, {shortfall} under its label", "The label weight is wrong: {filament} of filament, and the label becomes {new label}". Grams through the page's `grams()`.
- Conventional commits with the session's `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer; never amend; never commit the generated API files.

## Review Focus

1. A reading under the label but also under the empty weight (reading 150 g, insert 200 g, label 1000 g): only `tared` is offered, `short` and `label_wrong` absent, default `tared`. Task 1's `test_under_the_empty_weight_offers_only_tared`.
2. A reading exactly at the label, or within the tolerance: no `below_label`, `not_above_label` as today. Task 1's `test_at_the_label_is_not_below_it`.
3. A resolution sent for a used spool (the check says `used`): 422 `no_below_label` with `reason: used`, no event booked. Task 2's `test_a_resolution_without_a_below_label_answer_is_refused` and Task 4's route test.
4. `label_wrong` on a spool whose label came from its type: the spool gets its own `label_weight_g`, the type keeps its own, the `edited` event's `before` is null. Task 2's `test_label_wrong_sets_the_spools_own_label`.
5. The phone with a decimal reading under the label: the choice appears, the default is preselected, and the POST carries `resolution`. Task 6's `WeightField.test.tsx` case.

---

### Task 1: The check answers `below_label`

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`NoOfferReason` ~line 323, `InsertWeightCheck` ~line 356)
- Modify: `backend/scadbuddy/filament/insert_weights.py` (`check_on`, between the `full` check and `offer_reason`)
- Modify: `backend/scadbuddy/api/filament.py` (`_INSERT_WEIGHT_RULE` text and the offer route's description: name `below_label`)
- Test: `backend/tests/filament/test_insert_weights.py`

**Interfaces:**
- Produces: `NoOfferReason` gains `"below_label"`. New `BelowLabelResolution(BaseModel)`: `kind: Literal["tared", "short", "label_wrong"]`, `filament_g: float`, `new_label_weight_g: int | None = None`. `InsertWeightCheck` gains `shortfall_g: float | None = None`, `resolutions: list[BelowLabelResolution] = []`, `default: Literal["tared", "short", "label_wrong"] | None = None`. Pure helper `below_label(reading_g: float, label: int, empty_g: int) -> tuple[float, list[BelowLabelResolution], str]` in `insert_weights.py` (shortfall, resolutions, default), unit-tested without Postgres.

- [ ] **Step 1: Write the failing tests**

```python
def test_below_label_lists_the_three_with_their_numbers() -> None:
    shortfall, found, default = below_label(900, 1000, 200)
    assert shortfall == 100 and default == "tared"
    assert [(r.kind, r.filament_g, r.new_label_weight_g) for r in found] == [
        ("tared", 900, None), ("short", 700, None), ("label_wrong", 700, 700)]

def test_a_large_shortfall_defaults_to_short() -> None:
    assert below_label(800, 1000, 200)[2] == "short"

def test_under_the_empty_weight_offers_only_tared() -> None:
    _, found, default = below_label(150, 1000, 200)
    assert [r.kind for r in found] == ["tared"] and default == "tared"

def test_at_the_label_is_not_below_it(pg_pool) -> None:    # requires_postgres, a new spool
    assert check_on(conn, spool, 1000).reason == "not_above_label"
    assert check_on(conn, spool, 999.8).reason == "not_above_label"

def test_a_new_spool_under_its_label_answers_below_label(pg_pool) -> None:
    check = check_on(conn, spool, 900)
    assert check.reason == "below_label" and check.shortfall_g == 100 and check.default == "tared"
    assert [r.kind for r in check.resolutions] == ["tared", "short", "label_wrong"]

def test_a_used_spool_under_its_label_is_used(pg_pool) -> None:
    assert check_on(conn, used_spool, 900).reason == "used" and check_on(conn, used_spool, 900).resolutions == []
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_insert_weights.py -v`
Expected: FAIL, `ImportError: cannot import name 'below_label'`

- [ ] **Step 3: Implement** the models, `below_label`, the branch in `check_on` (its `answer` helper fills the three new fields), and the route texts.

- [ ] **Step 4: Run the tests to verify they pass** (same command, plus `tests/api/test_filament_insert_weights.py`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament/models.py backend/scadbuddy/filament/insert_weights.py backend/scadbuddy/api/filament.py backend/tests/filament/test_insert_weights.py
git commit -m "feat(filament): the insert weight check names a reading under the label and its resolutions (#7683)"
```

### Task 2: The weighing takes a resolution

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolEventAdd` ~line 1687: `resolution`)
- Modify: `backend/scadbuddy/filament/ledger.py` (`breakdown`: `empty_source` `tared`)
- Modify: `backend/scadbuddy/filament/spools.py` (`add_event`; new `NoBelowLabelError`)
- Test: `backend/tests/filament/test_weighing_breakdown.py`, `backend/tests/filament/test_ledger.py`

**Interfaces:**
- Consumes: Task 1's `check_on(..., lock=True)` answer.
- Produces: `SpoolEventAdd.resolution: Literal["tared", "short", "label_wrong"] | None = None`; validator: `resolution` needs `reading_g`, refuses `grams` ("a resolution takes reading_g alone"), weighed only. `ledger.breakdown(..., tared: bool = False)`: with `tared`, `empty_g = 0`, `empty_source = "tared"`, `empty_basis = None`, `empty_name = None`. `class NoBelowLabelError(ValueError)` with `reason: NoOfferReason | None`. In `add_event`, before the breakdown: with `resolution`, `check = insert_weights.check_on(conn, spool_id, body.reading_g, lock=True)`; `NoBelowLabelError(check.reason)` unless `check.reason == "below_label"` and the resolution is in `check.resolutions`; `grams = that resolution's filament_g`; `short` writes `added_full = false` plus its `edited` event; `label_wrong` writes `label_weight_g` plus its `edited` event; `data |= {"resolution": …, "shortfall_g": check.shortfall_g}`; the breakdown is computed with `tared=(resolution == "tared")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_breakdown_of_a_tared_reading_has_no_empty_weight() -> None:   # test_ledger.py
    assert breakdown(reading_g=900, grams=900, empty=EmptyWeight(200, "insert", "catalog", "Plastic", None), expected=None, tared=True)["empty_source"] == "tared"

def test_tared_books_the_reading_as_filament(...) -> None:        # a new spool, 1000 g label, 200 g insert
    event = store.add_event(spool, m.SpoolEventAdd(kind="weighed", source="scale", reading_g=900, resolution="tared"), actor="t")
    assert event.grams_after == 900 and event.data["resolution"] == "tared" and event.data["shortfall_g"] == 100
    assert [e.kind for e in store.events(spool)] == ["created", "weighed"]

def test_short_marks_the_spool_not_added_full(...) -> None:
    event = store.add_event(..., reading_g=800, resolution="short")
    assert event.grams_after == 600
    kinds = [(e.kind, e.data.get("changes")) for e in store.events(spool)]
    assert kinds[-2] == ("edited", {"added_full": [True, False]}) and store.get(spool).added_full is False

def test_label_wrong_sets_the_spools_own_label(...) -> None:     # label from the type
    event = store.add_event(..., reading_g=900, resolution="label_wrong")
    spool_row = store.get(spool)
    assert spool_row.label_weight_g == 700 and "label_weight_g" in spool_row.overridden
    assert [e.data.get("changes") for e in store.events(spool)][-2] == {"label_weight_g": [None, 700]}

def test_a_resolution_without_a_below_label_answer_is_refused(...) -> None:   # a used spool
    with pytest.raises(NoBelowLabelError) as refused: store.add_event(..., reading_g=900, resolution="tared")
    assert refused.value.reason == "used" and [e.kind for e in store.events(spool)][-1] != "weighed"

def test_a_resolution_the_check_does_not_offer_is_refused(...) -> None:   # reading 150 g, resolution short
    with pytest.raises(NoBelowLabelError): ...
```

Model validator tests: `resolution` with `grams` → "a resolution takes reading_g alone"; `resolution` without `reading_g` → "a resolution needs reading_g".

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_weighing_breakdown.py tests/filament/test_ledger.py -v`
Expected: FAIL

- [ ] **Step 3: Implement.** The `edited` events: the column UPDATE and `ledger.record(conn, spool_id, "edited", source="person", actor=actor, at=body.at, data={"changes": {...}})` in the same transaction, before the weighing's `record`; reuse the store's existing edited-event helper if `update()` exposes one that takes a connection, else write the two statements.

- [ ] **Step 4: Run the tests to verify they pass** (the two files plus `tests/filament/test_insert_weights.py`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament/models.py backend/scadbuddy/filament/ledger.py backend/scadbuddy/filament/spools.py backend/tests/filament/test_weighing_breakdown.py backend/tests/filament/test_ledger.py
git commit -m "feat(filament): a weighing under the label takes its resolution (#7683)"
```

### Task 3: The route, the agent tool and the mocks

**Files:**
- Modify: `backend/scadbuddy/api/filament.py` (`add_spool_event`: `NoBelowLabelError` → 422 `code="no_below_label"`, `reason`)
- Modify: `agent/src/tools/filament.ts` (`filament_spool_add_event`: input `resolution: z.enum(['tared','short','label_wrong']).optional()`; with `reading_g`, when the check answers `below_label` and no `resolution` was given, return `{event: null, below_label: check, next: "Nothing was booked. Tell the person the shortfall and the three readings with their numbers, then call again with the same reading_g and resolution."}`; with `resolution`, POST `{...body, reading_g, resolution}`; description updated)
- Modify: `frontend/src/mocks/features/filament/index.ts` (`insertWeightCheck`: the `below_label` branch by Task 1's rule; the events POST: `resolution` admitted, applied as Task 2 does, 422 `no_below_label` otherwise)
- Test: `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`, `frontend/src/mocks/features/filament/index.test.ts`

- [ ] **Step 1: Write the failing tests**: the route test (`resolution: tared` on a new spool under its label → 201 with `data.resolution`; on a used spool → 422 `no_below_label` with `reason: used`); the agent test (check answers `below_label`, no resolution → `event: null` and the choices; with `resolution` → the POST body carries it); the mocks test (the check's `below_label` answer and the POST's 422).
- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament.py tests/api/test_filament_insert_weights.py -v`; `cd agent && pnpm exec vitest run test/filament.test.ts`; `cd frontend && pnpm exec vitest run src/mocks/features/filament/index.test.ts`

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/api/filament.py backend/tests/api/test_filament.py agent/src/tools/filament.ts agent/test/filament.test.ts frontend/src/mocks/features/filament/index.ts frontend/src/mocks/features/filament/index.test.ts
git commit -m "feat(filament): the weighing route, the agent tool and the mocks take a resolution (#7683)"
```

### Task 4: The choice in the weigh form

**Files:**
- Create: `frontend/src/components/filament/BelowLabelChoice.tsx`
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (the weigh form ~line 955: when `insertCheck?.reason === 'below_label'`, render the choice under the reading; `readingOk` accepts the reading; the "N g of filament" line follows the chosen resolution; `weigh()` posts `{kind: 'weighed', source: 'scale', reading_g: scale, resolution}` and skips the offer path)
- Test: `frontend/src/components/filament/BelowLabelChoice.test.tsx` (new), `frontend/src/pages/filament/SpoolPage.insertWeight.test.tsx`

**Interfaces:**
- Produces: `BelowLabelChoice({ check, value, onChange, disabled }: { check: InsertWeightCheck; value: Resolution; onChange: (next: Resolution) => void; disabled?: boolean })`: a `fieldset` with the legend from Global Constraints and one radio per `check.resolutions` entry, labelled with the copy above; `Resolution` is the union type exported from the component.

- [ ] **Step 1: Write the failing tests**

```tsx
it('lists the three readings with their numbers and the default checked', () => {
  render(<BelowLabelChoice check={below(900)} value="tared" onChange={() => {}} />)
  expect(screen.getByRole('group', { name: 'The reading is 100 g under the label weight. What is up?' })).toBeInTheDocument()
  expect(screen.getByRole('radio', { name: 'The scale was tared on the empty spool: 900 g of filament' })).toBeChecked()
  expect(screen.getByRole('radio', { name: 'The spool is short: 700 g of filament, 100 g under its label' })).not.toBeChecked()
  expect(screen.getByRole('radio', { name: 'The label weight is wrong: 700 g of filament, and the label becomes 700 g' })).toBeInTheDocument()
})
it('lists only tared under the empty weight', …)
```

SpoolPage (`insertWeight.test.tsx`, spool #9 is new with a 1000 g label and a 180 g insert): typing `900` shows the group with "tared" checked; "Record weighing" posts `{ kind: 'weighed', source: 'scale', reading_g: 900, resolution: 'tared' }`; choosing "short" posts `resolution: 'short'` and the spool read after it has `added_full: false`.

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/filament/BelowLabelChoice.test.tsx src/pages/filament/SpoolPage.insertWeight.test.tsx src/pages/filament/SpoolPage.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/filament/BelowLabelChoice.tsx frontend/src/components/filament/BelowLabelChoice.test.tsx frontend/src/pages/filament/SpoolPage.tsx frontend/src/pages/filament/SpoolPage.insertWeight.test.tsx
git commit -m "feat(filament): the weigh form asks what is up when a new spool reads under its label (#7683)"
```

### Task 5: The choice on the phone

**Files:**
- Modify: `frontend/src/components/filament/scan/WeightField.tsx` (use `useInsertWeightCheck(spool.id, value)` when `empty !== null`; on `below_label`, render `BelowLabelChoice` with the default preselected; `save()` waits for the check (`pending`) and posts `{ kind: 'weighed', source: 'person', reading_g: value, resolution }`; the "less than the empty spool" error does not fire while `tared` is chosen)
- Test: `frontend/src/components/filament/scan/WeightField.test.tsx`

- [ ] **Step 1: Write the failing test**: a new spool with a 180 g insert and a 1000 g label, typed `950.5`: the group appears with "tared" checked; Save weight posts `{ kind: 'weighed', source: 'person', reading_g: 950.5, resolution: 'tared' }`.
- [ ] **Step 2: Run it to verify it fails**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run it to verify it passes**

Run: `cd frontend && pnpm exec vitest run src/components/filament/scan/WeightField.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/filament/scan/WeightField.tsx frontend/src/components/filament/scan/WeightField.test.tsx
git commit -m "feat(filament): the phone asks what is up when a new spool reads under its label (#7683)"
```

### Task 6: The timeline, the docs and the full checks

**Files:**
- Modify: `frontend/src/components/filament/SpoolTimeline.tsx` (a `weighed` event with `data.resolution` adds "scale tared on the spool" / "spool short by N g" / "label corrected"), test `SpoolTimeline.test.tsx`
- Modify: `frontend/src/components/filament/WeighingBreakdown.tsx` (`empty_source` `tared`: "filament {grams} (the scale was tared on the spool)"), test `WeighingBreakdown.test.tsx`
- Modify: `backend/CLAUDE.md` (the `insert_weights.py` / ledger note: `below_label`, `resolution`), `docs/features/spools-and-filament.md` (a paragraph), `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` (§5.1 "As built in #7683": `empty_source` `tared`; §5.3 "As built": the check needs an insert, `no_below_label`)
- Add: this plan at `docs/superpowers/plans/2026-11-25-weighing-below-label.md`

- [ ] **Step 1: Write the two frontend tests, see them fail, implement, see them pass.**
- [ ] **Step 2: Write the docs and add the plan file.**
- [ ] **Step 3: Run the full checks**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest tests/filament tests/api/test_filament.py tests/api/test_filament_insert_weights.py -n 4
cd frontend && pnpm lint && pnpm typecheck && pnpm test
cd agent && pnpm lint && pnpm typecheck && pnpm test -- test/filament.test.ts
npx --yes markdownlint-cli2@0.20.0 "docs/**/*.md"
```

- [ ] **Step 4: Commit, push, open the PR** (`feat(filament): a new spool's first weighing under the label weight: the choice (#7683)`, body ends with `Fixes #7683`, the owner marker and the Claude Code footer).
