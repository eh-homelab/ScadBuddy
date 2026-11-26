# Insert empty weight from a new spool (#7280) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a person (or the agent) weighs a new, unused spool, offer to set its insert's
`empty_weight_g` to `reading - label_weight_g`, averaged over every such reading of that insert.

**Architecture:** A new table keeps one derived reading per spool; a new column says where an
insert's empty weight came from. A pure module decides whether a reading earns an offer; two
routes answer the offer (read only) and accept it (writes the reading row and the insert). The
weighing route itself does not change. The UI and the agent tool ask for the offer before they
book the weighing, accept first when the person chose to, then book the weighing with the
filament worked out from the insert's new empty weight.

**Tech Stack:** Python 3.12, FastAPI, psycopg, Postgres 18; TypeScript (agent tools, zod);
React + vitest + msw (frontend).

**Spec:** issue #7280 (`gh issue view 7280 --json body`) and the rulings below.

## Rulings (decided by the owner's session, 2026-11-23; do not re-ask)

- **R-7280-1** A reading is from a new, unused spool when the spool's ledger has no consumption
  (no use, no manual adjustment down) and its grams left still equal the label weight. Only then
  is an offer made, and only when reading > label weight and the derived weight
  (reading - label_weight_g) is at most the label weight.
- **R-7280-2** Each derived reading is its own row (new table, one row per spool at most:
  insert_type_id, spool_id, reading_g, label_weight_g, derived_g, created_at), in a new migration
  file. The insert's derived value is the mean of its rows, and the UI shows how many readings it
  used.
- **R-7280-3** A column says where an insert's empty weight came from: `catalog` (default for
  existing rows), `person`, `placeholder`, `derived`, `weighed_empty`. The migration writes no
  prod data beyond the column default. An insert is a placeholder when its basis is
  `placeholder` OR its note begins "Placeholder weight" (runtime check). A person's edit of the
  weight in the catalog UI/tool sets `person`.
- **R-7280-4** The offer is checked by default only for a placeholder weight (or a previous
  `derived` one). For `catalog`, `person` or `weighed_empty` it is offered unchecked. Nothing
  changes the insert unless the person (or the agent on the person's behalf) accepts.
- **R-7280-5** The offer appears in the weigh flow in the UI and in the agent tool that records a
  weighing: its answer carries the offer (insert, reading, label weight, derived value, current
  value and basis, sample count, default checked); accepting is an explicit parameter. It is a
  ScadBuddy-only write (no Bambuddy call; the insert reaches Bambuddy only by the usual sync).
- **R-7280-6** The UI says that makers overfill by about 10-30 g, so the estimate comes out
  slightly heavy, and that a later weighing of an empty spool replaces it. No empty-spool
  weighing flow exists today, so `weighed_empty` is only a value of the column here; recording it
  is a follow-up issue (#7300).
- **R-7280-7** The spool's weighing itself is recorded exactly as today
  (`POST /filament/spools/{id}/events` is unchanged).
- **R-7280-8** Every new tool or parameter follows the one access model (#2171): the offer read is
  part of the existing `write` tool `filament_spool_add_event`; no new tier, no extra lockdown.
  The plugin skill documents the new parameters, and the plugin version is bumped. No setting is
  added, so no setting docs change.

Decisions this plan adds (D-n):

- **D1** "Label weight" is the spool's effective one: `coalesce(s.label_weight_g, t.label_weight_g)`
  (spool's own, else its type's), as `ledger.refresh_derived` reads it. The insert is the spool's
  effective one: `coalesce(s.insert_type_id, t.insert_type_id)`.
- **D2** "No consumption" means none of the spool's events is: a `print_started` or
  `print_ended`; a `print_estimate` or `print_actual` source; a `delta_g` below 0; an absolute
  reading (`ledger.reading(e, label)` not None) below `label - 0.5`. "Grams left equal the label
  weight" is `abs(grams_left - label) < 0.5`. An archived spool gets no offer.
- **D3** The weighing order: the offer is read before the weighing (the weighing changes grams
  left). Accepting runs before the weighing, so the weighing's filament is
  `reading - new empty weight`. Accept re-checks every condition under the spool's and the
  insert's row locks.
- **D4** `empty_weight_g` stays an integer: the mean of the rows rounded half up.
  `derived_g = reading_g - label_weight_g` is kept unrounded.
- **D5** A second accept for one spool replaces its row (`ON CONFLICT (spool_id) DO UPDATE`).
  A spool whose effective insert changed since its row moves its row to the new insert; the old
  insert keeps its weight.
- **D6** A catalog create through the API sets basis `person` unless the body says `placeholder`;
  a patch that sends `empty_weight_g` sets `person` unless it also sends
  `empty_weight_basis: placeholder`. `derived`, `catalog` and `weighed_empty` are never taken from a
  body. Bambuddy's pull keeps inserting with the column default `catalog`.

Review follow-ups (#7449 to #7452, decided by the owner's session, 2026-11-25; they change D2
and R-7280-6 where they differ):

- **R-7449-1** A spool with NULL `grams_left` and no consuming event counts as full: it holds
  its label weight. The reason docs say so (`models.py` `NoOfferReason`, the routes' text).
- **R-7449-2** A new mean that rounds below 1 g answers its own reason, `mean_below_1g`, not
  `not_above_label` (backend `NoOfferReason`, the frontend mocks, the agent tool text, the skill).
- **R-7449-3** The full tolerance holds at its edge: `abs(grams_left - label) <= 0.5`.
- **R-7449-4** An archived insert type answers `no_insert`, in `check_on` and in the re-check
  under lock in `accept_on`. The check still gives `filament_g` from its weight. A reading
  under the label still answers `below_label` with its resolutions (#7683), because none of them
  writes the insert: the review of cac9b3da1 found that an archived insert had hidden them, and a
  tared reading was then booked one insert weight light.
- **R-7450-1** A spool's delete takes its reading row out in the delete's transaction
  (`insert_weights.forget_spool_on`, called by `FilamentSpoolStore.delete`), with the insert's row
  locked first. When the insert's basis is `derived`, its `empty_weight_g` becomes the mean of the
  rows left; `derived_count` is a count of the rows, so it follows. Neither the issue nor this plan
  says what an insert with no readings left weighs, so: the last weight stays, the basis stays
  `derived`, and nothing new is set (`derived_count` reads 0). An accept that moves a spool's
  row to its new insert leaves the old insert the same way. The spool page then says the weight
  is "from new-spool readings no longer counted", which is true in both cases. Another basis is never changed.
- **R-7450-2** `InsertWeightAccept` takes an optional `new_empty_weight_g`. When it is given and
  the mean under the locks differs, the accept writes nothing and answers 409
  `insert_weight_changed` with the `new_empty_weight_g` now. The UI sends the offer's value, and on
  that 409 says the weight changed and checks again. The agent tool takes the value the person
  approved as its own `new_empty_weight_g` input (the first answer's offer) and sends it, never a
  later check's value: the review of c02fc7c6d found that the later check's value let a moved
  mean through.
- **R-7450-3** When `current_basis` is `person` and the weight is not a placeholder, the offer says
  that the weight a person set is replaced.
- **R-7451-1** No empty-spool weighing exists yet (#7300): the UI and the skill say to correct the
  estimate by editing the insert's empty weight. This replaces R-7280-6's "a later weighing of an
  empty spool replaces it".
- **R-7451-2** The tool's approval text without `accept_insert_weight` is "Check, or record, a
  reading of …", as that call books nothing when there is an offer.
- **R-7451-3** The skill, and the offer when its mean uses more than one reading, say that the
  spool can end a few grams off its label weight and then gets no later offer (`not_full`).
- **R-7452-1** A check that has not answered `CHECK_WAIT_MS` (10 s) after its request left stops
  holding "Record weighing": the weighing works without an offer, as when the check fails, and the
  form says the check did not answer. A later answer is still shown.

## Global Constraints

- New migration file only, named `$(date -u +%Y%m%dT%H%MZ)_filament_insert_weight_readings.sql`;
  never edit a merged migration; no data rows written.
- No new database client; the routes live in the API (`api/filament.py`).
- No Bambuddy read or write anywhere in this change.
- Never commit `backend/openapi.json` or any `schema.d.ts`.
- Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Copy (UI and docs) in plain short sentences (ASD-STE100 style), no emojis.

## Review Focus

1. A spool with no insert or no label weight (common for imports): the check answers no offer
   with a reason, never a 500. Test in Task 1.
2. A spool already printed from, then weighed full again (grams left back at label): still no
   offer, because the ledger has use. Test in Task 1.
3. A reading far above the label (a 1 kg spool weighed with something on the scale, 2600 g):
   no offer (`derived_too_heavy`). Test in Task 1.
4. Two readings for one insert: the weight is their mean, `derived_count` 2; the same spool
   accepted twice keeps one row. Test in Task 1.
5. The UI offer must not double-book: Record weighing with the box checked calls accept once,
   then books one weighing with the new filament grams. Test in Task 3.

---

### Task 1: Backend: table, basis, derivation and routes

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC stamp>_filament_insert_weight_readings.sql`
- Create: `backend/scadbuddy/filament/insert_weights.py`
- Modify: `backend/scadbuddy/filament/models.py` (insert type models, new models)
- Modify: `backend/scadbuddy/filament/catalog.py` (computed columns, basis on person writes)
- Modify: `backend/scadbuddy/api/filament.py` (two routes)
- Modify: `backend/CLAUDE.md` (one line in the filament layout entry)
- Test: `backend/tests/filament/test_insert_weights.py`, `backend/tests/api/test_filament_insert_weights.py`
  (follow the fixtures of the existing `tests/filament/` and `tests/api/test_filament*.py` files)

**Interfaces:**
- Produces (wire, used by Tasks 2 and 3):
  - `GET /api/v1/filament/spools/{spool_id}/insert-weight-offer?reading_g=<float>` →
    `InsertWeightCheck`; 404 no spool; 422 reading_g missing or not > 0.
  - `POST /api/v1/filament/spools/{spool_id}/insert-weight` body `{reading_g: float}` →
    `InsertWeightAccepted` (200); 404 no spool; 409 `code: "no_insert_weight_offer"` with
    `reason` when the check has no offer now.
  - `InsertWeightBasis = Literal["catalog", "person", "placeholder", "derived", "weighed_empty"]`
  - `InsertWeightOffer`: `insert_type_id: UUID, insert_name: str, spool_id: UUID, reading_g: float,
    label_weight_g: int, derived_g: float, current_empty_weight_g: int,
    current_basis: InsertWeightBasis, placeholder: bool, new_empty_weight_g: int,
    sample_count: int` (rows the new mean uses, this one included), `checked_by_default: bool`.
  - `InsertWeightCheck`: `offer: InsertWeightOffer | None`, `reason: NoOfferReason | None`
    (None exactly when offer is set), `insert_type_id: UUID | None`,
    `empty_weight_g: int | None` (the insert's current weight), `filament_g: float | None`
    (`reading_g - empty_weight_g`, None without an insert).
  - `NoOfferReason = Literal["archived", "no_insert", "no_label_weight", "used", "not_full",
    "not_above_label", "derived_too_heavy"]`
  - `InsertWeightAccepted`: `insert_type: InsertType`, `empty_weight_g: int`, `sample_count: int`,
    `filament_g: float` (`reading_g - empty_weight_g` after the change).
  - `InsertType` gains `empty_weight_basis: InsertWeightBasis = "catalog"`,
    `placeholder: bool = False`, `derived_count: int = 0`.
  - `InsertTypeCreate` gains `empty_weight_basis: Literal["person", "placeholder"] = "person"`;
    `InsertTypePatch` gains `empty_weight_basis: Literal["person", "placeholder"] | None = None`.

- [ ] **Step 1: Migration.** `filament_insert_types.empty_weight_basis text NOT NULL DEFAULT
  'catalog'` with a CHECK on the five values; table `filament_insert_weight_readings` (`id uuid`
  PK default `gen_random_uuid()`, `insert_type_id uuid NOT NULL REFERENCES filament_insert_types
  (id) ON DELETE CASCADE`, `spool_id uuid NOT NULL UNIQUE REFERENCES filament_spools (id) ON
  DELETE CASCADE`, `reading_g double precision NOT NULL CHECK (reading_g > 0)`,
  `label_weight_g integer NOT NULL CHECK (label_weight_g > 0)`, `derived_g double precision NOT
  NULL CHECK (derived_g > 0)`, `created_at timestamptz NOT NULL DEFAULT now()`), index on
  `insert_type_id`. A header comment cites #7280 and R-7280-2/3.

- [ ] **Step 2: Failing unit tests** in `tests/filament/test_insert_weights.py` for the pure
  functions (events built as `m.SpoolEvent`):
  - `is_placeholder("placeholder", None) is True`; `is_placeholder("catalog", "Placeholder weight: weigh an empty spool") is True`;
    `is_placeholder("catalog", "real") is False`; `is_placeholder("person", None) is False`.
  - `unused(events=[created, weighed grams_after=1000], grams_left=1000.0, label=1000) is True`;
    with a `print_ended` delta -12 → False; with a `weighed` reading 950 then one at 1000 → False;
    `grams_left=990.0` → False.
  - `offer_reason(reading=1200, label=1000)` is None; `1000` → `"not_above_label"`;
    `2600` → `"derived_too_heavy"`; `2000` → None (derived 1000 equals the label: allowed).
  - `mean_weight([150.0, 151.0]) == 151` (150.5 rounds half up); `mean_weight([200.0]) == 200`.
  - `checked_by_default("derived", False) is True`; `("catalog", True) is True`;
    `("person", False) is False`; `("weighed_empty", False) is False`.

- [ ] **Step 3: Run them; they fail** (`uv run --frozen pytest tests/filament/test_insert_weights.py -q`).

- [ ] **Step 4: Implement `filament/insert_weights.py`:** the pure functions above
  (`is_placeholder(basis: str, note: str | None) -> bool`, case-insensitive on the note after
  stripping leading space; `unused(events: Sequence[m.SpoolEvent], *, grams_left: float | None,
  label: int) -> bool` by D2, using `ledger.reading`; `offer_reason(reading: float, label: int) ->
  NoOfferReason | None`; `mean_weight(derived: Sequence[float]) -> int`;
  `checked_by_default(basis: str, placeholder: bool) -> bool`) and two connection functions:
  - `check_on(conn, spool_id: UUID, reading_g: float, *, lock: bool = False) -> InsertWeightCheck | None`
    (None for no spool): reads the spool (effective insert and label per D1, `grams_left`,
    `archived_at`; `FOR UPDATE OF s` with `lock`), the insert (its row `FOR UPDATE` with `lock`),
    its other rows' `derived_g` (excluding this spool's), and the spool's events
    (`ledger.events`). Reason order: archived, no_insert, no_label_weight, used (D2 events),
    not_full, then `offer_reason`.
  - `accept_on(conn, spool_id: UUID, reading_g: float) -> InsertWeightAccepted | None` (None for
    no spool; raises `NoInsertWeightOfferError(reason)` when `check_on(lock=True)` has no offer):
    upserts the row (D5), sets the insert's `empty_weight_g` to the mean of all its rows, basis
    `derived`, `updated_at = now()`, and answers the insert re-read through the catalog's columns.
  - Store wrappers that open a pool connection and a transaction: follow how `api/filament.py`
    reaches other filament stores (`FilamentDep`), and add the store as an attribute there rather
    than a new component if the existing `Filament` bundle allows it.

- [ ] **Step 5: Catalog changes in `catalog.py`.** `CatalogSpec` gets `computed: Mapping[str, str]
  = {}` (field name → SQL expression over the catalog row); `_columns` emits `<expr> AS <name>`
  for those and skips them as plain columns. For insert-types: `placeholder` is
  `(empty_weight_basis = 'placeholder' OR ltrim(coalesce(note, '')) ILIKE 'placeholder weight%')`
  and `derived_count` is the count of `filament_insert_weight_readings` rows of the insert. In
  `update`, for insert-types, when `empty_weight_g` is sent and `empty_weight_basis` is not, also
  set `empty_weight_basis = 'person'` (D6). `create` already inserts the body's
  `empty_weight_basis`.

- [ ] **Step 6: Failing store and API tests.** In `tests/filament/test_insert_weights.py`
  (Postgres-backed, like the existing ledger tests) and `tests/api/test_filament_insert_weights.py`:
  - a spool (label 1000, insert basis `catalog` 200 g, `created` with 1000 g left) and reading
    1150 → `offer.derived_g == 150`, `new_empty_weight_g == 150`, `sample_count == 1`,
    `checked_by_default is False`, `filament_g == 950`.
  - same with the insert's note "Placeholder weight, weigh an empty spool" → `placeholder is True`,
    `checked_by_default is True`.
  - accept → insert `empty_weight_g == 150`, `empty_weight_basis == "derived"`,
    `derived_count == 1`, answer `filament_g == 1000`; the spool's events are unchanged (no event
    booked by accept).
  - a second spool of the same insert at 1151 → `new_empty_weight_g == 151` (mean 150.5),
    `sample_count == 2`, `checked_by_default is True` (basis now `derived`); accept the first spool
    again at 1149 → still 2 rows.
  - spool with no insert → `reason == "no_insert"`, `filament_g is None`; spool with no label →
    `"no_label_weight"`; spool with a `print_ended` delta then a weighing back to 1000 →
    `"used"`; reading 2600 → `"derived_too_heavy"`; reading 1000 → `"not_above_label"`;
    archived spool → `"archived"`.
  - accept with no offer → 409, body `code == "no_insert_weight_offer"`, `reason == "used"`;
    unknown spool → 404 on both routes; `reading_g=0` → 422.
  - `PATCH` of the catalog insert-type with `empty_weight_g` → basis `person`; with
    `empty_weight_g` and `empty_weight_basis: "placeholder"` → `placeholder`; `POST` create →
    `person`; a patch of only the note leaves the basis.

- [ ] **Step 7: Implement the two routes** in `api/filament.py` next to `add_spool_event`, with
  descriptions that state R-7280-1/4/7 in one paragraph each, `asyncio.to_thread` as the
  neighbours do, the 409 through `ApiError(..., code="no_insert_weight_offer", reason=...)`. The
  accept route nudges the sync as every other write route does (do not add it to `NOT_NUDGED`).

- [ ] **Step 8: Run** `uv run --frozen pytest -n 2 tests/filament/test_insert_weights.py
  tests/api/test_filament_insert_weights.py` and the existing catalog tests that touch insert
  types (`git grep -l insert-types backend/tests`); then `uv run --frozen ruff check`,
  `ruff format --check` and `mypy` on the touched files. All pass.

- [ ] **Step 9: Docs and commit.** Add one sentence to `backend/CLAUDE.md`'s filament entry:
  `insert_weights.py` (#7280: an insert's empty weight from new spools, its rows
  `filament_insert_weight_readings`, the basis column, routes `…/insert-weight-offer` and
  `…/insert-weight`). Commit `feat(filament): derive an insert's empty weight from a new spool's weighing (#7280)`.

### Task 2: Agent tool and plugin skill

**Files:**
- Modify: `agent/src/tools/filament.ts` (`filament_spool_add_event`)
- Modify: `plugins/scadbuddy/skills/filament/SKILL.md`, the plugin's `plugin.json` version (and the
  agent copy's, as `plugins/CLAUDE.md` says)
- Test: the agent test file that covers `filament_spool_add_event` (find it with
  `git grep -l filament_spool_add_event agent/test`), or a new `agent/test/filamentInsertWeight.test.ts`

**Interfaces:**
- Consumes: Task 1's two routes and models (`pnpm gen:api` writes them into `agent/src/api/schema.d.ts`).
- Produces: tool input gains `reading_g?: number` (> 0: the scale reading with the spool) and
  `accept_insert_weight?: boolean`. Answer: `{ event, filament_g, insert_weight: InsertWeightCheck | null,
  insert_weight_accepted: InsertWeightAccepted | null }`.

- [ ] **Step 1: Failing tests** (mock the backend as the neighbouring tool tests do):
  - `kind: weighed, reading_g: 1150` → GETs the offer, POSTs the event with `grams: 950`
    (the check's `filament_g`), does not POST accept, answer carries `insert_weight.offer`.
  - same with `accept_insert_weight: true` → order is GET offer, POST accept, POST event with
    `grams` = accept's `filament_g`.
  - `accept_insert_weight: true` and the check has no offer → throws naming the reason, books
    nothing.
  - `reading_g` and `grams` both → refused; `reading_g` with a check whose `filament_g` is null
    → refused ("no empty weight known; pass grams"); `reading_g` or `accept_insert_weight` on a
    kind other than weighed → refused.
  - `kind: weighed, grams: 900` with no `reading_g` → only the event POST, as today.
- [ ] **Step 2: Run them; they fail.**
- [ ] **Step 3: Implement.** Add both routes to the tool's `routes`. Description: one sentence each
  for `reading_g`, the offer (R-7280-1, R-7280-4: `checked_by_default` is advice; accept only on
  the person's word) and `accept_insert_weight`; it writes ScadBuddy only. Keep `risk: 'write'`.
- [ ] **Step 4: Plugin skill.** In the filament skill's weighing part, a short paragraph: weigh with
  `reading_g`, read `insert_weight.offer`, tell the person the numbers and the overfill note
  (about 10-30 g heavy; a later empty-spool weighing replaces it), and pass
  `accept_insert_weight: true` only when they say so. Cite #7280. Make sure the plugin version is
  bumped once and mirrored into `agent/plugins/scadbuddy/` (`plugins/CLAUDE.md`), then run
  `.github/scripts/lint-plugin.sh`.
- [ ] **Step 5: Run** the touched agent tests, `pnpm typecheck` and `pnpm lint` in `agent/`
  (the agent's own CLAUDE.md names the commands; `pnpm gen:api` first). All pass.
- [ ] **Step 6: Commit** `feat(agent): filament_spool_add_event offers an insert's empty weight from a new spool (#7280)`.

### Task 3: Frontend weigh flow

**Files:**
- Modify: `frontend/src/api/filament.ts` (two calls), `frontend/src/pages/filament/SpoolPage.tsx`
  (or a new `frontend/src/components/filament/InsertWeightOffer.tsx` that the page renders),
  `frontend/src/mocks/features/filament/index.ts` (two handlers)
- Test: `frontend/src/pages/filament/SpoolPage.insertWeight.test.tsx`

**Interfaces:**
- Consumes: Task 1's routes; generated types `components['schemas']['InsertWeightCheck']` etc.
- Produces: `insertWeightOffer(spoolId: string, readingG: number): Promise<InsertWeightCheck>`,
  `acceptInsertWeight(spoolId: string, readingG: number): Promise<InsertWeightAccepted>`.

- [ ] **Step 1: Load the `frontend-design:frontend-design` skill.**
- [ ] **Step 2: Failing tests** (msw, as `SpoolPage.test.tsx` does):
  - a spool with an insert and a valid reading above the label shows "Empty spool weight from this
    new spool", the numbers (reading, label, derived, current and new weight, readings used), and
    the overfill copy: "Makers overfill by about 10-30 g, so this estimate comes out slightly
    heavy. A later weighing of an empty spool replaces it."
  - the checkbox starts checked when `checked_by_default` is true, unchecked otherwise.
  - checked + Record weighing → one accept request, then one events request with
    `grams` = accept's `filament_g`; unchecked → no accept, events `grams` = reading - current
    empty weight (today's value).
  - no offer (e.g. `reason: "used"`) → no panel, weighing as today.
- [ ] **Step 3: Run them; they fail** (`pnpm gen:api` once, then `pnpm exec vitest run <file>`).
- [ ] **Step 4: Implement.** Fetch the check when the reading parses and the spool has an insert
  (debounce about 300 ms; ignore a stale answer for an older reading). The panel sits under the
  reading in the weigh form. Show the basis in words (catalog: "from the catalog", person: "set by
  a person", placeholder: "a placeholder", derived: "from new spools", weighed_empty: "weighed
  empty"). On the existing "Empty spool N g (name)." line, add "from N new spools" when the insert's
  basis is `derived`. Mocks: handlers that apply R-7280-1 on the mock's spools and ledger, and update
  the mock insert on accept.
- [ ] **Step 5: Run** the new test, `SpoolPage.test.tsx`, `pnpm typecheck`, `pnpm lint`. All pass.
- [ ] **Step 6: Commit** `feat(frontend): offer an insert's empty weight when weighing a new spool (#7280)`.

### Task 4: Follow-up, PR (controller)

- [x] File a follow-up issue (#7300): record an empty-spool weighing (a used-up spool) that sets the
  insert's weight with basis `weighed_empty` (R-7280-6), Part of #6260, referencing #7280.
- [ ] Final whole-branch review, push, PR, auto-merge, rulings comment on #7280.
