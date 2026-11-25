# End-of-life weighings (slice C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Archiving a spool takes a reason (finished, respooled, damaged, other) and an optional final weighing, and the archived event keeps the lifetime reconciliation: what the spool started with, what the ledger consumed, what the scale says was consumed, and the drift. A respooled spool continues as a new spool with the filament left.

**Architecture:** The closing numbers are one pure function in `ledger.py` over the spool's events; the archive store books the final weighing (slice A's breakdown path, extracted to a helper) and the `archived` event with the closing in one transaction; a read route previews the closing and its notices for the archive dialog; the copy flow gains `continued_from`, which links the two spools' events. The page gets an archive dialog and a closing summary.

**Tech Stack:** Python 3.12, psycopg, pydantic, FastAPI; React 19 + Vite, vitest, msw; the agent's zod tools.

**Spec:** `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` §5.4, §5.5 (`finished_left_g`). Issue #7684, epic #7681. Builds on slice A (#7682, merged in #7717): `ledger.breakdown`, `ledger.expected_before`, `filament/empty_weight.py` `effective_on`, `NoEmptyWeightError`, `ReadingBelowEmptyError`, `SpoolDetail.last_weighing`.

## Global Constraints

- Reasons, exactly: `finished`, `respooled`, `damaged`, `other`. The UI requires one on a new archive; the API keeps it optional (the sync's archives record `other`; a restore takes none).
- Closing keys on the `archived` event's `data`: `reason`, `started_g`, `started_from` (`reading` or `label`, null when neither), `ledger_consumed_g`, `final_filament_g`, `scale_consumed_g`, `drift_g`. `drift_g = ledger_consumed_g - scale_consumed_g` (negative: the scale found less filament than the ledger expected, the sign of slice A). Every grams value rounded to 0.1 g; `null` where undefined.
- `started_g`: the first event with `source` in `scale`/`person` and `grams_after` not null (`created` included), else the effective label weight when `added_full` is true, else null. `ledger_consumed_g`: the grams of `metrics.plates(events)` whose `at` is at or after that event's `at` (every plate for the label case; null when `started_g` is null), never affected by `consumed_reset`.
- A final weighing is booked at the archive's time, before the `archived` event, with slice A's `reading_g` rules (`final_reading_g` alone takes the effective empty weight off; `final_grams` is the net; both together as `reading_g` and `grams`; 422 `no_empty_weight` and `reading_below_empty` as slice A). `final_filament_g` is that weighing's `grams_after`; without one it is the spool's `grams_left` at the archive, and `scale_consumed_g` is null.
- Setting `filament_weighing_finished_left_g`, int, default 5, `ge=0, le=100000`, mode `live`, section `filament`, kind `count`, doc title "Finished spool has at most, grams"; `FilamentDefaults.finished_left_g`.
- Notices (the preview's `notices`, each `{code, text}`): `filament_left` when `reason == finished` and `final_filament_g > finished_left_g` ("{N} of filament are still on it."); `not_finished_but_empty` when `reason != finished` and `final_filament_g <= finished_left_g` ("It reads empty; is it finished?"). The person can go on; nothing refuses.
- `continued_from` (a spool id) on `SpoolDraft`: the new spool's `created` event gets `data.continued_from`, and the source's newest `archived` event gets `data.continued_as` (the new id) in the same transaction; 422 `continued_from_not_archived` when the source is not archived.
- Copy, exactly: dialog title "Archive spool #N"; reason labels "Finished", "Respooled", "Damaged", "Other"; reading field "Scale reading, with the spool (g)" with an empty weight known, else "Filament left (g)"; buttons "Archive" and, for Respooled, "Archive and continue as a new spool". Closing summary: "{Reason} on {date}: started with {started_g} ({from the label | first weighing}), the ledger used {ledger_consumed_g}, the scale says {scale_consumed_g} were used; {|drift|} {less|more} than the ledger expected." and without a final reading "…the ledger used {ledger_consumed_g}; not weighed at the end."
- Conventional commits with the session's `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` trailer; never amend; never commit the generated API files. The route added here, `GET /filament/spools/{id}/closing`, is a read under the spool (the inventory epic owner was told).

## Review Focus

1. A spool with a `consumed_reset`: `ledger_consumed_g` counts every plate since `started_g`'s event, not since the reset. Task 1's `test_closing_ignores_consumed_resets`.
2. A spool never weighed and not added full: `started_g` null, `ledger_consumed_g` null, the summary says "not known"; the archive still works. Task 1's `test_closing_with_nothing_to_start_from` and Task 4's dialog test.
3. A restore (`archived: false`) with a reason or a final reading: 422, nothing booked. Task 2's `test_a_restore_takes_no_reason_or_reading`.
4. `final_reading_g` under the empty weight: 422 `reading_below_empty`, no archive, no event. Task 2's `test_a_final_reading_under_the_empty_weight_archives_nothing`.
5. `continued_from` naming an unarchived or unknown spool: 422 / 404, no spool made. Task 3's tests.

---

### Task 1: The closing numbers, the setting and the preview

**Files:**
- Modify: `backend/scadbuddy/filament/ledger.py` (new `closing`)
- Modify: `backend/scadbuddy/filament/models.py` (`ArchiveReason`, `Closing`, `ClosingNotice`, `ClosingPreview`)
- Modify: `backend/scadbuddy/filament/spools.py` (`FilamentDefaults.finished_left_g`; new `closing_preview(spool_id, reason, final_reading_g, final_grams) -> ClosingPreview | None`)
- Modify: `backend/scadbuddy/core/settings.py`, `backend/scadbuddy/core/setting_docs.py`, `frontend/src/pages/settings/fields.ts`, the mocks' settings seed
- Modify: `backend/scadbuddy/api/filament.py` (`GET /spools/{spool_id}/closing`, query `reason`, `final_reading_g`, `final_grams`, in `NOT_NUDGED`)
- Test: `backend/tests/filament/test_ledger.py`, `backend/tests/filament/test_weighing_breakdown.py`, `backend/tests/api/test_filament.py`, `backend/tests/test_setting_docs.py`

**Interfaces:**
- Produces: `ArchiveReason = Literal["finished", "respooled", "damaged", "other"]`. `ledger.closing(events: Sequence[m.SpoolEvent], *, label_weight_g: int | None, added_full: bool | None, final_filament_g: float | None, reason: ArchiveReason | None) -> dict[str, Any]` (the keys of Global Constraints). `m.Closing(BaseModel)` mirrors them (`reason: ArchiveReason | None`, the rest `float | None` / `started_from: Literal["reading","label"] | None`). `m.ClosingNotice(code: Literal["filament_left","not_finished_but_empty"], text: str)`. `m.ClosingPreview(closing: Closing, final_filament_g: float | None, empty_g: int | None, notices: list[ClosingNotice])`. `spools.closing_preview` resolves the final filament the way Task 2's archive will (slice A's empty weight rules; a reading under the empty weight answers `final_filament_g` null and a 422 is left to the archive), computes `closing` and the notices with `defaults.finished_left_g`.

- [ ] **Step 1: Write the failing tests**

```python
def test_closing_from_a_first_weighing() -> None:        # test_ledger.py, the _event helper
    events = [_event(0, "created", None), _event(1, "weighed", "scale", grams_after=1000),
              _event(2, "print_ended", "print_actual", delta_g=-300, ref_kind="print_run", ref_id="a"),
              _event(3, "consumed_reset", "person"),
              _event(4, "print_ended", "print_actual", delta_g=-200, ref_kind="print_run", ref_id="b")]
    got = closing(events, label_weight_g=1000, added_full=None, final_filament_g=480, reason="finished")
    assert got == {"reason": "finished", "started_g": 1000, "started_from": "reading", "ledger_consumed_g": 500,
                   "final_filament_g": 480, "scale_consumed_g": 520, "drift_g": -20}

def test_closing_ignores_consumed_resets() -> None:      # the same events: 500, not 200
def test_closing_from_the_label_of_an_added_full_spool() -> None:   # no reading: started_from label, every plate
def test_closing_with_nothing_to_start_from() -> None:   # not added full, never weighed: started_g, ledger_consumed_g, scale_consumed_g, drift_g all None
def test_closing_without_a_final_reading_has_no_scale_side() -> None:

def test_the_preview_names_filament_left_on_a_finished_spool(pg_pool) -> None:   # test_weighing_breakdown.py
    preview = store.closing_preview(spool, "finished", final_reading_g=230, final_grams=None)   # 200 g insert: 30 g left
    assert [n.code for n in preview.notices] == ["filament_left"] and preview.final_filament_g == 30

def test_the_preview_notices_an_empty_spool_not_finished(pg_pool) -> None:   # reason damaged, reading 203: 3 g
def test_the_preview_follows_the_setting(pg_pool) -> None:   # FilamentDefaults(finished_left_g=50): no notice at 30 g
```

Route test (`tests/api/test_filament.py`): `GET …/closing?reason=finished&final_reading_g=230` answers the preview; an unknown spool is 404; a bad reason is 422.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run --frozen pytest tests/filament/test_ledger.py tests/filament/test_weighing_breakdown.py tests/api/test_filament.py -k "closing" -v`
Expected: FAIL, `ImportError: cannot import name 'closing'`

- [ ] **Step 3: Implement** `closing` (plates by `metrics.plates`, imported lazily or moved so `ledger` does not import `metrics` at module load if that cycles; the builder picks), the models, the setting with its doc and field, `FilamentDefaults.finished_left_g`, `closing_preview` and the route.

- [ ] **Step 4: Run the tests to verify they pass** (plus `tests/test_setting_docs.py`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament/ledger.py backend/scadbuddy/filament/models.py backend/scadbuddy/filament/spools.py backend/scadbuddy/core/settings.py backend/scadbuddy/core/setting_docs.py backend/scadbuddy/api/filament.py frontend/src/pages/settings/fields.ts frontend/src/mocks backend/tests
git commit -m "feat(filament): a spool's closing numbers, their preview and the finished-left setting (#7684)"
```

### Task 2: Archive with a reason and a final weighing

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolArchive`: `reason: ArchiveReason | None = None`, `final_reading_g: Grams | None = None`, `final_grams: Grams | None = None`; validator: any of them with `archived: false` → "a restore takes no reason or reading"; `final_reading_g < final_grams` → "final_reading_g is below final_grams")
- Modify: `backend/scadbuddy/filament/spools.py` (`add_event`'s weighed branch extracted to `_weigh_on(conn, spool_id, body: SpoolEventAdd, actor, data) -> SpoolEvent`; `archive(spool_id, archived, *, actor, reason=None, final_reading_g=None, final_grams=None)` does, in one transaction: lock the row, book the final weighing through `_weigh_on` when given, compute `ledger.closing(...)` with the spool's effective label weight and `added_full`, then the existing archive path with that dict as the `archived` event's `data`; `_archive` gains a `data` parameter; the sync's archive (`bb_pull_apply` / wherever `_archive` is reached with `at`) passes `{"reason": "other"}` plus the closing without a final reading)
- Modify: `backend/scadbuddy/api/filament.py` (`archive_spool` passes the three; 422 mapping of `NoEmptyWeightError` and `ReadingBelowEmptyError` as `add_spool_event` does; the description names them)
- Modify: `agent/src/tools/filament.ts` (`filament_spool_archive`: `reason`, `final_reading_g`, `final_grams` inputs; description)
- Modify: `frontend/src/mocks/features/filament/index.ts` (the archive POST takes the three, books the weighing and the archived event with the closing by Task 1's rule; the closing GET)
- Test: `backend/tests/filament/test_weighing_breakdown.py`, `backend/tests/api/test_filament.py`, `agent/test/filament.test.ts`, `frontend/src/mocks/features/filament/index.test.ts`

- [ ] **Step 1: Write the failing tests**

```python
def test_archiving_with_a_final_reading_books_the_weighing_then_the_closing(pg_pool) -> None:
    spool_row = store.archive(spool, True, actor="t", reason="finished", final_reading_g=230)
    kinds = [e.kind for e in store.events(spool)]
    assert kinds[-2:] == ["weighed", "archived"]
    archived = store.events(spool)[-1]
    assert archived.data["reason"] == "finished" and archived.data["final_filament_g"] == 30
    assert archived.data["scale_consumed_g"] == 970 and archived.data["drift_g"] == pytest.approx(...)

def test_archiving_without_a_reading_closes_on_the_ledger(pg_pool) -> None:   # scale_consumed_g None, final_filament_g = grams_left
def test_a_final_reading_under_the_empty_weight_archives_nothing(pg_pool) -> None:
    with pytest.raises(ReadingBelowEmptyError): store.archive(spool, True, actor="t", reason="damaged", final_reading_g=150)
    assert store.get(spool).archived_at is None and [e.kind for e in store.events(spool)][-1] != "weighed"
def test_a_restore_takes_no_reason_or_reading() -> None:   # the model validator
def test_the_syncs_archive_records_other(pg_pool) -> None:  # through the pull path that archives a linked spool
```

Route: `POST …/archive {archived: true, reason: "finished", final_reading_g: 230}` → 200 and the spool's events end `weighed`, `archived`; `{archived: false, reason: "finished"}` → 422. Agent: the body carries the three. Mocks: same.

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**

Run: `cd backend && uv run --frozen pytest tests/filament/test_weighing_breakdown.py tests/filament/test_bb_fields_pull.py tests/api/test_filament.py -v`; `cd agent && pnpm exec vitest run test/filament.test.ts`; `cd frontend && pnpm exec vitest run src/mocks/features/filament/index.test.ts`

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament backend/scadbuddy/api/filament.py backend/tests agent/src/tools/filament.ts agent/test/filament.test.ts frontend/src/mocks/features/filament
git commit -m "feat(filament): archive with a reason and a final weighing that closes the books (#7684)"
```

### Task 3: Respooled continues as a new spool

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolDraft.continued_from: UUID | None = None`)
- Modify: `backend/scadbuddy/filament/spools.py` (`create_on` / `bulk_on`: with `continued_from`, lock the source row, 422 `ContinuedFromNotArchivedError` when it is not archived (the route maps it), `data.continued_from` on the `created` event, and `UPDATE filament_spool_events SET data = data || '{"continued_as": …}' WHERE id = (the source's newest archived event)`; `quantity` must be 1 with `continued_from`, else `ValueError`)
- Modify: `backend/scadbuddy/api/filament.py` (the bulk create maps the error 422 `continued_from_not_archived`; the copy route's description names `continued_from`)
- Modify: `agent/src/tools/filament.ts` (`filament_spool_upsert`: `continued_from` input, passed through)
- Modify: `frontend/src/lib/filamentLinks.ts` (`continueSpool(id, grams)` → `/filament/spools/new?copy=<id>&continue=1&grams=<g>`), `frontend/src/pages/filament/SpoolPage.tsx` (with `continue=1`: `draft.grams_left` prefilled from `grams`, `added_full` `no`, the copy banner says "Continues spool #N: its filament, respooled.", the create sends `continued_from`)
- Modify: `frontend/src/mocks/features/filament/index.ts` (bulk create honours `continued_from`)
- Test: `backend/tests/filament/test_weighing_breakdown.py`, `backend/tests/api/test_filament_spool_create_ops.py`, `frontend/src/pages/filament/SpoolPage.test.tsx`

- [ ] **Step 1: Write the failing tests**: store (`created` event has `continued_from`, the source's archived event has `continued_as`; an unarchived source raises; a missing source is `SpoolCopySourceError` as a bad `copy_of` is); route (422 code); page (opening `/filament/spools/new?copy=<#9>&continue=1&grams=480` shows grams 480, "Added full: No" and the banner; Save sends `continued_from`).
- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/filament backend/scadbuddy/api/filament.py backend/tests agent/src/tools/filament.ts frontend/src/lib/filamentLinks.ts frontend/src/pages/filament/SpoolPage.tsx frontend/src/pages/filament/SpoolPage.test.tsx frontend/src/mocks/features/filament
git commit -m "feat(filament): a respooled spool continues as a new spool (#7684)"
```

### Task 4: The archive dialog

**Files:**
- Create: `frontend/src/components/filament/ArchiveSpoolDialog.tsx`, `frontend/src/components/filament/useClosingPreview.ts` (the `useInsertWeightCheck` pattern over `GET …/closing`, 300 ms after the inputs settle)
- Modify: `frontend/src/api/filament.ts` (`archiveSpool(id, body: SpoolArchive)`, `closingPreview(id, reason, finalReadingG, finalGrams)`)
- Modify: `frontend/src/pages/filament/SpoolPage.tsx` (the Archive button opens the dialog; `respooled` plus "Archive and continue" archives then navigates to `filamentLinks.continueSpool(id, preview.final_filament_g ?? saved.grams_left)`)
- Test: `frontend/src/components/filament/ArchiveSpoolDialog.test.tsx` (new), `frontend/src/pages/filament/SpoolPage.test.tsx`

**Interfaces:**
- Produces: `ArchiveSpoolDialog({ spool, emptyWeightG, open, onOpenChange, onArchived }: { spool: Spool; emptyWeightG: number | null; open: boolean; onOpenChange: (o: boolean) => void; onArchived: (spool: Spool, continueWithGrams: number | null) => void })`: the reason radio group (none chosen by default; Archive disabled until one is), the reading input, the preview's closing lines and notices as they arrive, the two buttons.

- [ ] **Step 1: Write the failing tests**

```tsx
it('needs a reason, previews the closing and archives with the reading', …)   // reason Finished, reading 230 → preview "30 g of filament are still on it.", Archive posts {archived: true, reason: 'finished', final_reading_g: 230}
it('offers to continue a respooled spool', …)   // reason Respooled → the second button; click → onArchived(spool, 480)
it('archives on the ledger alone without a reading', …)   // posts {archived: true, reason: 'damaged'}
```

SpoolPage: the Archive button opens the dialog; after "Archive and continue", the page is at `/filament/spools/new?copy=…&continue=1&grams=…`.

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/filament/ArchiveSpoolDialog.test.tsx src/pages/filament/SpoolPage.test.tsx`

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/filament/ArchiveSpoolDialog.tsx frontend/src/components/filament/ArchiveSpoolDialog.test.tsx frontend/src/components/filament/useClosingPreview.ts frontend/src/api/filament.ts frontend/src/pages/filament/SpoolPage.tsx frontend/src/pages/filament/SpoolPage.test.tsx
git commit -m "feat(filament): the archive dialog takes a reason and a final weighing (#7684)"
```

### Task 5: The closing summary, the timeline, the docs and the full checks

**Files:**
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolDetail.closing: Closing | None`), `backend/scadbuddy/filament/spools.py` (`detail()`: the newest `archived` event's data when the spool is archived)
- Create: `frontend/src/components/filament/ClosingSummary.tsx` (the sentence of Global Constraints, "{Reason}" capitalised, "not known" for null numbers); modify `SpoolPage.tsx` (the Weight section shows it on an archived spool, above `WeighingBreakdown`)
- Modify: `frontend/src/components/filament/SpoolTimeline.tsx` (an `archived` event shows its reason and, with `continued_as`, "continues as spool …" linked; a `created` event with `continued_from` shows "continues spool …")
- Modify: `backend/CLAUDE.md` (the archive note: reason, final weighing, closing, `continued_from`), `docs/features/spools-and-filament.md`, `docs/superpowers/specs/2026-11-25-weighing-reconciliation-design.md` (§5.4 "As built in #7684": `started_from`, `final_grams`, the preview route, `continued_from` on the draft)
- Add: this plan at `docs/superpowers/plans/2026-11-25-weighing-end-of-life.md`
- Test: `frontend/src/components/filament/ClosingSummary.test.tsx`, `SpoolTimeline.test.tsx`, `backend/tests/filament/test_weighing_breakdown.py` (`detail().closing`)

- [ ] **Step 1: Write the tests, see them fail, implement, see them pass.**
- [ ] **Step 2: Write the docs and add the plan file.**
- [ ] **Step 3: Run the full checks**

```bash
cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest tests/filament tests/api/test_filament.py tests/api/test_filament_spool_create_ops.py tests/test_setting_docs.py -n 4
cd frontend && pnpm lint && pnpm typecheck && pnpm test
cd agent && pnpm lint && pnpm typecheck && pnpm test -- test/filament.test.ts
npx --yes markdownlint-cli2@0.20.0 "docs/**/*.md"
```

- [ ] **Step 4: Commit, push, open the PR** (`feat(filament): end-of-life weighings: finished, respooled, damaged out, with the lifetime reconciliation (#7684)`, body ends with `Fixes #7684`, the owner marker and the Claude Code footer).
