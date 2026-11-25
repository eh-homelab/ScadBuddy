# Weighing breakdown and drift (slice A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every scale weighing keeps its gross reading, the empty weight taken off it with its basis, the grams left the ledger expected and the drift, and the spool page, list and timeline say so in a sentence.

**Architecture:** The breakdown is computed in the store's transaction that records the `weighed` event (`filament/spools.py` `add_event`) and lives in the event's `data`; one new module `filament/empty_weight.py` resolves a spool's effective empty weight (the insert type now, #7278's parts later). `refresh_derived` copies the newest weighing's drift into a new column `last_drift_g`, and `_hydrate` turns it into `drift_notice` against a new setting. The frontend sends the gross reading beside the net grams and renders the sentences from the event's `data`.

**Tech Stack:** Python 3.12, psycopg, pydantic, FastAPI; React 19 + Vite, vitest, msw; the agent's zod tools.

**Spec:** `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` §5.1, §5.2, §5.5. Issue #7682, epic #7681.

## Global Constraints

- The grams-left rule (`ledger.py` module docstring) is unchanged: no anchor or delta moves. `expected_g` is `ledger.grams_left` over the spool's events before the weighing's `at`, plus the delta events at exactly that `at` (the ledger's rule 1: at one instant deltas come before readings).
- Event `data` keys, exactly: `reading_g`, `empty_g`, `empty_source`, `empty_basis`, `empty_name`, `expected_g`, `drift_g`. `drift_g = grams - expected_g`, rounded to 0.1 g. `expected_g` and `drift_g` are `null` when the ledger had no anchor before the weighing.
- `empty_source` is `insert` (this slice; `parts` is #7278's) or `person` (the client's `reading_g - grams` is not the effective empty weight, which includes a spool with none known). `empty_basis` is the insert's `empty_weight_basis` for `insert`, `null` for `person`.
- A `bb` weighing (the pull's) records no breakdown: `bambuddy_pull.py` is not touched.
- Settings: `filament_weighing_drift_notice_g`, int, default 10, `ge=0, le=100000`, mode `live`, section `filament`, kind `count`, doc title "Drift notice at, grams".
- Every grams the UI shows goes through the page's `grams()` formatter (one decimal, `g`).
- Migration: one new file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_filament_spool_last_drift.sql`; never edit a merged one.
- Commits are conventional (`feat(filament): …`), each with the Co-Authored-By trailer the session carries.

## Review Focus

1. A back-dated weighing (`at` set before older events): `expected_g` must be what the ledger said *then*, not now. Pinned in Task 2's `test_expected_is_the_grams_left_before_a_back_dated_weighing`.
2. A weighing on a spool with no reading yet (fresh spool, no anchor): `expected_g` null, `drift_g` null, no crash, the page shows the breakdown line only. Task 2's `test_first_weighing_has_no_expected` and Task 6's `shows only the breakdown when nothing was expected`.
3. `reading_g` sent with `grams` that disagree with the effective empty weight (an old client, or a person who subtracted an adaptor by hand): `empty_g = reading_g - grams`, `empty_source = person`, nothing refused. Task 2's `test_a_client_that_subtracted_its_own_empty_weight_is_recorded_as_person`.
4. `reading_g` alone on a spool with no empty weight known: 422 `no_empty_weight`, nothing booked. Task 4's `test_a_reading_alone_needs_an_empty_weight`.
5. A spool whose newest weighing has no breakdown (every weighing before this slice): `last_drift_g` null, `drift_notice` false, no sentence, the old "Measured N g on …" stays. Task 3's `test_an_old_weighing_leaves_last_drift_null`.
6. `reading_g` alone under the effective empty weight (150 g on a 200 g insert): 422 `reading_below_empty`, nothing booked, never negative filament. Task 2's `test_a_reading_alone_under_the_empty_weight_is_refused` and Task 4's route test.

---

### Task 1: The effective empty weight

**Files:**
- Create: `backend/scadbuddy/filament/empty_weight.py`
- Test: `backend/tests/filament/test_empty_weight.py`

**Interfaces:**
- Produces: `class EmptyWeight(NamedTuple): grams: int; source: Literal["insert"]; basis: str; name: str; note: str | None` and `def effective_on(conn: Conn, spool_id: UUID) -> EmptyWeight | None`: the insert type of the spool, else of its type (`coalesce(s.insert_type_id, t.insert_type_id)`, the same join as `insert_weights.check_on`), `None` for no insert or no such spool. `basis` is `placeholder` whenever `insert_weights.is_placeholder(empty_weight_basis, note)` says so, else the column's value (`catalog`, `person`, `derived`, `weighed_empty`). The docstring names #7278: parts are added here, callers do not change.

- [ ] **Step 1: Write the failing tests** (`requires_postgres`, the `PgPool` fixture and `CatalogStore` / `FilamentSpoolStore` setup as `tests/filament/test_insert_weights.py` does)

```python
def test_the_spools_own_insert_wins_over_its_types(pool) -> None:
    # type insert 180 g "Cardboard", spool insert 200 g "Plastic"
    assert effective_on(conn, spool_id) == EmptyWeight(200, "insert", "catalog", "Plastic", None)

def test_a_placeholder_note_makes_the_basis_placeholder(pool) -> None:
    # insert basis "catalog", note "Placeholder weight until weighed"
    assert effective_on(conn, spool_id).basis == "placeholder"

def test_the_types_insert_is_taken_when_the_spool_sets_none(pool) -> None:
    assert effective_on(conn, spool_id).grams == 180

def test_no_insert_is_none(pool) -> None:
    assert effective_on(conn, spool_id) is None
    assert effective_on(conn, uuid4()) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_empty_weight.py -v`
Expected: FAIL, `ModuleNotFoundError: scadbuddy.filament.empty_weight`

- [ ] **Step 3: Implement `effective_on` in `backend/scadbuddy/filament/empty_weight.py`**

One query: spool left-joined to its type and to `filament_insert_types` on the coalesced id, selecting `empty_weight_g, empty_weight_basis, name, note`; then `is_placeholder` on the basis and note.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/filament/test_empty_weight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament/empty_weight.py backend/tests/filament/test_empty_weight.py
git commit -m "feat(filament): a spool's effective empty weight in one place (#7682)"
```

### Task 2: The weighing records its breakdown

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolEventAdd`, ~line 1682; `SpoolDetail`, ~line 1881)
- Modify: `backend/scadbuddy/filament/ledger.py` (new `expected_before`, after `grams_left`)
- Modify: `backend/scadbuddy/filament/spools.py` (`add_event`, ~line 1372; `detail`)
- Test: `backend/tests/filament/test_ledger.py`, `backend/tests/filament/test_weighing_breakdown.py` (new)

**Interfaces:**
- Consumes: Task 1's `effective_on`.
- Produces:
  - `SpoolEventAdd.reading_g: Grams | None = None` ("the scale reading with the spool, a weighing only"). Validator: `reading_g` with a kind other than `weighed` is a `ValueError("only a weighing has reading_g")`; `reading_g < grams` is `ValueError("reading_g is below grams")`; a weighing with neither `grams` nor `reading_g` keeps today's "a weighing needs grams".
  - `ledger.expected_before(events: Sequence[m.SpoolEvent], at: datetime | None, *, weight_locked: bool, label_weight_g: int | None) -> float | None`: `grams_left` over the events with `e.at < at`, plus the delta events (`source` in `_DELTA`) with `e.at == at`, since rule 1 puts deltas before readings at one instant (all events when `at` is None).
  - `ledger.breakdown(*, reading_g: float, grams: float, empty: EmptyWeight | None, expected: float | None) -> dict[str, Any]`: the seven data keys of Global Constraints. `empty_source` is `insert` only when `empty is not None and abs(reading_g - grams - empty.grams) < 0.05`, else `person` with `empty_basis` null and `empty_name` null.
  - `class NoEmptyWeightError(ValueError)` in `spools.py`: a weighing with `reading_g` and no `grams` on a spool with no effective empty weight. `class ReadingBelowEmptyError(ValueError)` (carries `empty_g`): a weighing with `reading_g` and no `grams` whose reading is under that empty weight.
  - `add_event`: for `kind == "weighed"`, resolve `empty = effective_on(conn, spool_id)`; `grams = body.grams if body.grams is not None else body.reading_g - empty.grams` (raise `NoEmptyWeightError` when `empty` is None, `ReadingBelowEmptyError` when `body.reading_g < empty.grams`); when `body.reading_g` is not None, `data |= breakdown(...)` with `expected = expected_before(ledger.events(conn, spool_id), body.at, weight_locked=…, label_weight_g=…)` read from the spool's row (the same `coalesce` as `refresh_derived`). The `grams` go to `absolute_g` as today.
  - `SpoolDetail.last_weighing: SpoolEvent | None = None`: the newest `weighed` event, set in `detail()`.

- [ ] **Step 1: Write the failing pure tests in `tests/filament/test_ledger.py`** (the `_event` helper there)

```python
def test_expected_before_takes_only_events_before_the_time() -> None:
    events = [_event(1, "weighed", "scale", grams_after=800),
              _event(2, "print_ended", "print_actual", delta_g=-50, ref_kind="print_run", ref_id="r"),
              _event(5, "print_ended", "print_actual", delta_g=-30, ref_kind="print_run", ref_id="s")]
    assert expected_before(events, T0 + timedelta(minutes=3), weight_locked=False, label_weight_g=1000) == 750
    assert expected_before(events, T0 + timedelta(minutes=5), weight_locked=False, label_weight_g=1000) == 720  # a delta at the same instant counts
    assert expected_before(events, None, weight_locked=False, label_weight_g=1000) == 720

def test_breakdown_names_the_insert_when_the_client_took_it_off() -> None:
    empty = EmptyWeight(200, "insert", "placeholder", "Plastic", "placeholder weight")
    assert breakdown(reading_g=1180, grams=980, empty=empty, expected=992) == {
        "reading_g": 1180, "empty_g": 200, "empty_source": "insert", "empty_basis": "placeholder",
        "empty_name": "Plastic", "expected_g": 992, "drift_g": -12.0}

def test_breakdown_is_a_persons_when_the_grams_disagree_with_the_insert() -> None:
    empty = EmptyWeight(200, "insert", "catalog", "Plastic", None)
    assert breakdown(reading_g=1180, grams=962, empty=empty, expected=None) == {
        "reading_g": 1180, "empty_g": 218, "empty_source": "person", "empty_basis": None,
        "empty_name": None, "expected_g": None, "drift_g": None}
```

- [ ] **Step 2: Write the failing store tests in `tests/filament/test_weighing_breakdown.py`** (`requires_postgres`; a spool with a 1000 g label and a 200 g insert, as `test_insert_weights.py` sets up; `store.add_event(spool_id, m.SpoolEventAdd(kind="weighed", source="scale", reading_g=1180, grams=980), actor="t")`)

```python
def test_a_weighing_with_a_reading_keeps_its_breakdown(...) -> None:
    # earlier: a scale weighing of 1000 g at T0, a print of -8 g at T0+1h
    event = store.add_event(spool, m.SpoolEventAdd(kind="weighed", source="scale", reading_g=1180, grams=980), actor="t")
    assert event.grams_after == 980
    assert event.data["expected_g"] == 992 and event.data["drift_g"] == -12.0
    assert event.data["empty_source"] == "insert" and event.data["empty_name"] == "Plastic"

def test_a_reading_alone_takes_the_insert_off(...) -> None:
    event = store.add_event(spool, m.SpoolEventAdd(kind="weighed", source="scale", reading_g=1180), actor="t")
    assert event.grams_after == 980 and event.data["empty_g"] == 200

def test_a_reading_alone_needs_an_empty_weight(...) -> None:   # a spool with no insert
    with pytest.raises(NoEmptyWeightError): store.add_event(...reading_g=1180...)

def test_a_reading_alone_under_the_empty_weight_is_refused(...) -> None:   # 200 g insert
    with pytest.raises(ReadingBelowEmptyError): store.add_event(...reading_g=150...)
    assert [e.kind for e in store.events(spool)] == ["created"]

def test_first_weighing_has_no_expected(...) -> None:           # a spool with no reading yet
    assert event.data["expected_g"] is None and event.data["drift_g"] is None

def test_expected_is_the_grams_left_before_a_back_dated_weighing(...) -> None:
    # weighing of 1000 g at T0, print -8 g at T0+1h, print -20 g at T0+3h; weigh at T0+2h
    assert event.data["expected_g"] == 992

def test_a_client_that_subtracted_its_own_empty_weight_is_recorded_as_person(...) -> None:
    event = store.add_event(spool, m.SpoolEventAdd(kind="weighed", source="scale", reading_g=1180, grams=962), actor="t")
    assert event.data["empty_source"] == "person" and event.data["empty_g"] == 218

def test_a_weighing_without_a_reading_records_no_breakdown(...) -> None:
    assert "reading_g" not in event.data

def test_detail_carries_the_newest_weighing(...) -> None:
    assert store.detail(spool).last_weighing.id == event.id
```

Plus the model validator tests in the same file (no Postgres): `reading_g` on a `dried` is refused "only a weighing has reading_g"; `reading_g=900, grams=980` is refused "reading_g is below grams".

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_ledger.py tests/filament/test_weighing_breakdown.py -v`
Expected: FAIL, `ImportError: cannot import name 'expected_before'` and friends

- [ ] **Step 4: Implement** the model field and validator, `expected_before` and `breakdown` in `ledger.py`, `NoEmptyWeightError` and the `add_event` change in `spools.py`, and `SpoolDetail.last_weighing` in `detail()`. Update `SpoolEventAdd`'s docstring (it says `grams` is the filament left; add `reading_g`) and the ledger module docstring's `data` list in one sentence.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/filament/test_ledger.py tests/filament/test_weighing_breakdown.py tests/filament/test_insert_weights.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/scadbuddy/filament/models.py backend/scadbuddy/filament/ledger.py backend/scadbuddy/filament/spools.py backend/tests/filament/test_ledger.py backend/tests/filament/test_weighing_breakdown.py
git commit -m "feat(filament): a weighing records its reading, empty weight, expected grams and drift (#7682)"
```

### Task 3: `last_drift_g`, the drift notice and its setting

**Files:**
- Create: `backend/scadbuddy/migrations/<timestamp>_filament_spool_last_drift.sql` (`ALTER TABLE filament_spools ADD COLUMN last_drift_g double precision;`)
- Modify: `backend/scadbuddy/filament/ledger.py` (`refresh_derived` ~line 445, `_book_on_top` ~line 496)
- Modify: `backend/scadbuddy/filament/models.py` (`Spool`: `last_drift_g: float | None = None`, `drift_notice: bool = False`)
- Modify: `backend/scadbuddy/filament/spools.py` (`FilamentDefaults.drift_notice_g: int = 10`, `_SETTING_DEFAULTS`, the defaults reader that fills it from the settings, `_hydrate`)
- Modify: `backend/scadbuddy/core/settings.py` (~line 811 and the `live` map ~line 1204), `backend/scadbuddy/core/setting_docs.py` (~line 436)
- Modify: `frontend/src/pages/settings/fields.ts` (~line 184: a `count` entry in section `filament`)
- Test: `backend/tests/filament/test_weighing_breakdown.py`, `backend/tests/test_setting_docs.py` (runs as is), `frontend/src/pages/settings/sections/filament.test.tsx` (the field renders)

**Interfaces:**
- Produces: `Spool.last_drift_g` (the newest weighing's `data.drift_g`, null when it has none), `Spool.drift_notice` (`last_drift_g is not None and abs(last_drift_g) >= defaults.drift_notice_g`), the setting `filament_weighing_drift_notice_g`.

- [ ] **Step 1: Write the failing tests** (in `test_weighing_breakdown.py`)

```python
def test_the_newest_weighings_drift_is_on_the_spool(...) -> None:
    assert store.get(spool).last_drift_g == -12.0

def test_an_old_weighing_leaves_last_drift_null(...) -> None:   # a weighing with no reading_g after one with
    assert store.get(spool).last_drift_g is None

def test_drift_notice_follows_the_setting(...) -> None:
    # drift -12 g: notice with the default 10, none with FilamentDefaults(drift_notice_g=20)
```

Frontend: in `sections/filament.test.tsx`, the field "Drift notice at, grams" is rendered with value 10 from the mock settings.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_weighing_breakdown.py tests/test_setting_docs.py -v`
Expected: FAIL, `last_drift_g` missing

- [ ] **Step 3: Implement**: the migration file; `refresh_derived` sets `last_drift_g` from `weighed[-1].data.get("drift_g")` (the full walk) and `_book_on_top` sets it for every on-top event that `_weighs`, from its `data.drift_g` or to null when it has none, so a newer weighing without a breakdown (a `bb` one) clears an older drift; the settings field with its `live` mode and doc; `FilamentDefaults.drift_notice_g` read where `low_stock_pct` is; `_hydrate` computes `drift_notice`; the frontend field entry. The mocks' settings seed (`frontend/src/mocks`, where `filament_low_stock_pct` is seeded) gets the new key at 10.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/filament/test_weighing_breakdown.py tests/filament/test_ledger.py tests/test_setting_docs.py tests/filament/test_migrations.py -v` and `cd frontend && pnpm exec vitest run src/pages/settings/sections/filament.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/migrations/*_filament_spool_last_drift.sql backend/scadbuddy/filament backend/scadbuddy/core frontend/src/pages/settings/fields.ts frontend/src/mocks backend/tests
git commit -m "feat(filament): last_drift_g on the spool and the drift notice setting (#7682)"
```

### Task 4: The route, the agent tool and the mocks

**Files:**
- Modify: `backend/scadbuddy/api/filament.py` (`add_spool_event` ~line 1633: map `NoEmptyWeightError` to 422 `code="no_empty_weight"` and `ReadingBelowEmptyError` to 422 `code="reading_below_empty"` with `empty_g`; the route's description names `reading_g` and both refusals)
- Modify: `agent/src/tools/filament.ts` (`filament_spool_add_event` ~line 1634: every events POST that has a `reading_g` sends `reading_g` in the body beside `grams`; the description says the breakdown is kept)
- Modify: `frontend/src/mocks/features/filament/index.ts` (~line 2362: `FIELDS.event` admits `reading_g`; a weighing with `reading_g` computes `grams` from the spool's insert when left out, 422 `no_empty_weight` with none, 422 `reading_below_empty` under it, and writes `data` by Task 2's `breakdown` rule with `expected_g = row.grams_left`, and sets `last_drift_g`; a weighing without one leaves `last_drift_g` null)
- Test: `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`, `frontend/src/mocks/features/filament/index.test.ts`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_weighing_with_a_reading_answers_its_breakdown(client) -> None:
    # a spool with a 200 g insert and a 1000 g label, weighed once with grams 1000
    weighed = client.post(f"{one}/events", json={"kind": "weighed", "source": "scale", "reading_g": 1180})
    assert weighed.status_code == 201 and weighed.json()["grams_after"] == 980
    assert weighed.json()["data"]["drift_g"] == -20.0
    assert client.get(one).json()["last_weighing"]["id"] == weighed.json()["id"]

def test_a_reading_alone_needs_an_empty_weight(client) -> None:
    refused = client.post(f"{one}/events", json={"kind": "weighed", "reading_g": 1180})   # no insert
    assert refused.status_code == 422 and refused.json()["code"] == "no_empty_weight"

def test_a_reading_alone_under_the_empty_weight_is_refused(client) -> None:   # 200 g insert
    refused = client.post(f"{one}/events", json={"kind": "weighed", "reading_g": 150})
    assert refused.status_code == 422 and refused.json()["code"] == "reading_below_empty"
    assert [e["kind"] for e in client.get(f"{one}/events").json()] == ["created"]
```

Agent (`agent/test/filament.test.ts`, the existing `filament_spool_add_event` cases' style): with `reading_g: 1180` and no offer, the POST body is `{kind: 'weighed', source: 'person', reading_g: 1180, grams: 980}`.

Mocks (`index.test.ts`): a POST with `reading_g` answers `data.reading_g` and `data.drift_g`, and the spool read after it carries `last_drift_g`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/api/test_filament.py -k "reading" -v`; `cd agent && pnpm exec vitest run test/filament.test.ts`; `cd frontend && pnpm exec vitest run src/mocks/features/filament/index.test.ts`
Expected: FAIL

- [ ] **Step 3: Implement** the three changes.

- [ ] **Step 4: Run the tests to verify they pass** (the same three commands)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/api/filament.py backend/tests/api/test_filament.py agent/src/tools/filament.ts agent/test/filament.test.ts frontend/src/mocks/features/filament
git commit -m "feat(filament): the weighing route, the agent tool and the mocks take reading_g (#7682)"
```

### Task 5: The page and the phone send the reading

**Files:**
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (`weigh` ~line 718: the POST body gains `reading_g: scale` when `empty` is set)
- Modify: `frontend/src/components/filament/scan/WeightField.tsx` (~line 61: `reading_g: value` beside `grams` when `empty !== null`)
- Test: `frontend/src/pages/filament/SpoolPage.insertWeight.test.tsx` (the recorded bodies gain `reading_g`), `frontend/src/components/filament/scan/WeightField.test.tsx`

- [ ] **Step 1: Update the body assertions** (`{ kind: 'weighed', grams: 1020, source: 'scale', reading_g: 1200 }` and the phone's `{ kind: 'weighed', source: 'person', grams: …, reading_g: … }`) and run them to see them fail.

Run: `cd frontend && pnpm exec vitest run src/pages/filament/SpoolPage.insertWeight.test.tsx src/components/filament/scan/WeightField.test.tsx`
Expected: FAIL on the bodies

- [ ] **Step 2: Implement** the two body changes. A spool with no empty weight sends `grams` only, as today.

- [ ] **Step 3: Run the tests to verify they pass**
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/filament/SpoolPage.tsx frontend/src/components/filament/scan/WeightField.tsx frontend/src/pages/filament/SpoolPage.insertWeight.test.tsx frontend/src/components/filament/scan/WeightField.test.tsx
git commit -m "feat(filament): the weigh form and the phone send the scale reading (#7682)"
```

### Task 6: The breakdown sentence and the start gap

**Files:**
- Create: `frontend/src/components/filament/WeighingBreakdown.tsx`
- Modify: `frontend/src/components/filament/GramsBasis.tsx` (props gain `addedFull: boolean | null`, `labelWeight: number | null`)
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (~line 946: the "Measured … on …" line becomes `<WeighingBreakdown event={saved.last_weighing} />` when that event has `data.reading_g`, else stays; `GramsBasis` gets the two props from `saved.added_full` and `effective?.label_weight_g`)
- Test: `frontend/src/components/filament/WeighingBreakdown.test.tsx` (new), `frontend/src/components/filament/GramsBasis.test.tsx` (new or existing), `frontend/src/pages/filament/SpoolPage.test.tsx`

**Interfaces:**
- Produces: `WeighingBreakdown({ event }: { event: SpoolEvent })`, a `<p>` with two sentences:
  - `Weighed {reading} on {date}: filament {grams_after} + spool {empty_g} ({empty_name}, {basis word}).` The basis word: `catalog` → "from the catalog", `placeholder` → "placeholder", `derived` → "from new spools", `weighed_empty` → "weighed empty", `person` → "by hand" (the backend already resolved `placeholder` by `is_placeholder`, so the word follows `empty_basis` alone); for `empty_source` `person`: `spool {empty_g} (taken off by hand)`.
  - When `expected_g` is not null: `The ledger expected {expected}. ` then `The scale matches the prints.` (|drift| < 1), `The scale found {|drift|} less than the prints accounted for.` (drift < 0) or `… more than …` (drift > 0).
  - `GramsBasis` adds, before the anchor row, when `addedFull === true`, `labelWeight != null` and the anchor is the spool's `created` event (a later AMS or sync anchor holds printed filament, so it never shows a gap): `Started at the label weight {label}. {SOURCE word, capitalised} reported {anchor grams}; {label - anchor} unexplained.` ("Bambuddy reported 979.3 g; 20.7 g unexplained."). Nothing when the gap is under 0.05 g.

- [ ] **Step 1: Write the failing tests**

```tsx
it('says what the reading was made of and how it compares', () => {
  render(<WeighingBreakdown event={weighed({ grams_after: 980, data: { reading_g: 1180, empty_g: 200, empty_source: 'insert', empty_basis: 'placeholder', empty_name: 'Plastic', expected_g: 992, drift_g: -12 } })} />)
  expect(screen.getByText(/Weighed 1180 g on .*: filament 980 g \+ spool 200 g \(Plastic, placeholder\)\./)).toBeInTheDocument()
  expect(screen.getByText(/The ledger expected 992 g\. The scale found 12 g less than the prints accounted for\./)).toBeInTheDocument()
})
it('shows only the breakdown when nothing was expected', …)   // no "ledger expected"
it('says the scale matches under a gram', …)                   // drift 0.4
it('says more when the drift is positive', …)
it('names a person-taken empty weight', …)                     // "(taken off by hand)"
```

GramsBasis: `shows the unexplained start gap of an added-full spool` (anchor `created`, source `bb`, 979.3 g, label 1000 → "Started at the label weight 1000 g. Bambuddy reported 979.3 g; 20.7 g unexplained."), `shows no gap for a scale anchor`, `shows no gap when a later Bambuddy or AMS reading is the anchor` (anchor `synced_from_bb` or `ams_reading` after prints), `shows no gap when the spool was not added full`.

SpoolPage: with the mocks' spool #1 given a `last_weighing` that has a breakdown (seed it in `seed.ts` as the newest event of spool 1), the Weight section shows "filament 640 g + spool"; spool #4 (added full, anchor from Bambuddy in the seed) shows "unexplained".

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/filament/WeighingBreakdown.test.tsx src/components/filament/GramsBasis.test.tsx src/pages/filament/SpoolPage.test.tsx`
Expected: FAIL

- [ ] **Step 3: Implement** the component, the `GramsBasis` row and the page wiring.

- [ ] **Step 4: Run the tests to verify they pass**
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/filament/WeighingBreakdown.tsx frontend/src/components/filament/WeighingBreakdown.test.tsx frontend/src/components/filament/GramsBasis.tsx frontend/src/components/filament/GramsBasis.test.tsx frontend/src/pages/filament/SpoolPage.tsx frontend/src/pages/filament/SpoolPage.test.tsx frontend/src/mocks/features/filament/seed.ts
git commit -m "feat(filament): the spool page says what a weighing was made of and the start gap (#7682)"
```

### Task 7: The timeline and the drift badge

**Files:**
- Modify: `frontend/src/components/filament/SpoolTimeline.tsx` (`detail` ~line 33: a `weighed` event with `data.reading_g` pushes `reading {reading_g} g, spool {empty_g} g` and, with `drift_g` not null, `{|drift|} g {less|more} than expected` or `matches the ledger`)
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (~line 749: the heading's rest gains `Drift ±N g` when `saved.drift_notice`)
- Modify: the spools list row component that shows a spool's state (find where `low_stock` renders a badge in `frontend/src/pages/filament/SpoolsTab.tsx` or its row component; add a `Drift` badge from `drift_notice` beside it)
- Test: `frontend/src/components/filament/SpoolTimeline.test.tsx`, `frontend/src/pages/filament/SpoolPage.test.tsx`, `frontend/src/pages/filament/SpoolsTab.test.tsx`

- [ ] **Step 1: Write the failing tests**: the timeline shows "reading 1180 g, spool 200 g, 12 g less than expected" for the seeded weighing; the page heading of a spool with `drift_notice: true` and `last_drift_g: -12` shows "Drift −12 g"; the list row shows a "Drift" badge for it and none for spool #2.

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/filament/SpoolTimeline.test.tsx src/pages/filament/SpoolPage.test.tsx src/pages/filament/SpoolsTab.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/filament/SpoolTimeline.tsx frontend/src/components/filament/SpoolTimeline.test.tsx frontend/src/pages/filament/SpoolPage.tsx frontend/src/pages/filament/SpoolPage.test.tsx frontend/src/pages/filament/SpoolsTab.tsx frontend/src/pages/filament/SpoolsTab.test.tsx
git commit -m "feat(filament): drift on the timeline, the spool heading and the list (#7682)"
```

### Task 8: Docs and the full checks

**Files:**
- Modify: `backend/CLAUDE.md` (the `filament/` `ledger.py` note: one clause on `reading_g`, the breakdown's `data` keys and `last_drift_g`; `empty_weight.py` listed as the one empty-weight resolver)
- Modify: `docs/features/spools-and-filament.md` (a short "Weighing breakdown" paragraph with the keys and the sentence)
- Modify: `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` (an "As built in #7682" line under §5.1 only if the build departs from its key list)

- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Run the full checks**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest tests/filament tests/api/test_filament.py tests/test_setting_docs.py -n auto
cd frontend && pnpm lint && pnpm typecheck && pnpm test
cd agent && pnpm lint && pnpm typecheck && pnpm test -- test/filament.test.ts
npx --yes markdownlint-cli2@0.20.0 "docs/**/*.md"
```

Expected: all pass.

- [ ] **Step 3: Commit, push, open the PR** (`feat(filament): weighing breakdown and drift on every weighing (#7682)`, body ends with `Fixes #7682`, the owner marker and the Claude Code footer).
