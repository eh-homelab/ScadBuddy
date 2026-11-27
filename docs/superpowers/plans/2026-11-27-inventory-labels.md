# Spool Type and Inventory Labels Implementation Plan (#7169)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A spool type (`T-<n>`), an item (`I-<n>`) and an item kind (`K-<n>`, which exists)
each get a printed label whose QR opens its page, and each code resolves in `/codes`, the
NFC tag read and the photo scan.

**Architecture:** One new migration gives `filament_spool_types` and `items` a `number` from
one shared sequence. `codes/resolve.py` `PREFIXES` and its Go copy (`internal/scan/resolve.go`)
gain `T` and `I`. `POST /api/v1/filament/labels` takes three more id lists and draws each as
the existing code label (the place label, renamed `CodeLabel`), so the PDF, PNG, sheets and
CUPS print take them unchanged. The browser's `resolvedCode.ts`, the Labels page and three
Print actions finish it. No new route.

**Tech Stack:** PostgreSQL 18 migration, Python 3.12 (FastAPI, pydantic, psycopg, ReportLab),
Go (`tools/go/core`, pgx), React, vitest, msw.

**Spec:** `docs/superpowers/specs/2026-11-27-inventory-labels-design.md` (Facts, R1 to R7,
D1 to D13, "Overlap with PR #8323"). Read it first.

## Global Constraints

- New routes are Go (`tools/go/core`); this plan adds none. Python changes are edits to
  existing files. Only the API and Go core hold SQL. A change to the code resolve changes
  both `codes/resolve.py` and `tools/go/core/internal/scan/resolve.go`.
- Prefixes, exact: `T` → `filament_spool_types`, kind `spool_type`; `I` → `items`, kind
  `item`. `L`, `S`, `K` do not change.
- Names, exact: a spool type's is `concat_ws(' ', code, colour_name)`; an item's and a kind's
  is `name`. One SQL fragment per table, in `codes/resolve.py` `NAME_SQL` and in the Go
  `prefixes` entry.
- The sequence is `item_numbers_seq AS integer`, not `OWNED BY`, shared by both tables'
  `number integer NOT NULL DEFAULT nextval('item_numbers_seq')` with a `UNIQUE` constraint
  each (`filament_spool_types_number_key`, `items_number_key`). Backfill: spool types first,
  then items, each by `created_at, id`.
- The migration is one NEW file, `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_label_numbers.sql`.
  No other migration file changes.
- `LabelsRequest` gains `type_ids: list[UUID]`, `item_ids: list[UUID]`, `kind_ids: list[str]`,
  each `default_factory=list, max_length=MAX_LABELS`. The five lists count 1 to `MAX_LABELS`
  (100) in all. Label order: spools, places, types, items, kinds; each list in the order sent.
- A code label: `CodeLabel(code, name, qr)` with `qr = f"{base.rstrip('/')}/x/{code}"`, at most
  `MAX_PAYLOAD_BYTES` (84); no base is 409 `no_label_base`; an unknown id is 404; an archived
  row still gets its label.
- Labels page params, exact: `?types=`, `?items=`, `?kinds=` (comma lists, at most
  `MAX_LABELS`). Links: `filamentLinks.typeLabel(id)`, `itemLabel(id)`, `kindLabel(id)`.
- Two PRs. PR A is Tasks 1 to 3 and starts now; none of its files is one that #8323 changes.
  PR B is Tasks 4 to 6 and starts only after #8323 has merged; its implementer reads the
  merged files first. Each PR keeps `pnpm typecheck` green on its own.
- UI tasks load `frontend-design:frontend-design` first. The msw mocks serve every new path
  and field.
- Tests insert fixed `created_at` values where order matters; no test sleeps or reads the
  date.
- Locally: `ruff`, `mypy`, `go vet`, focused `pytest`/`go test`/vitest, `pnpm typecheck`,
  `pnpm lint`. CI runs the rest. Backend Postgres tests locally use a Docker Postgres on a
  port of its own (`CLAUDE.local.md`).
- Simplified Technical English in docs and comments. No counts in docs. A link for an
  external fact; how a measured fact was measured.
- New commits only; titles `feat(filament): … (#7169)` or `feat(inventory): … (#7169)`, with
  the repo's trailers.

## Review Focus

1. **A `T` number that is an item's.** With one sequence, `T-<n>` where `n` is an item's
   number names no spool type. It must be 404 `code_unknown`, never the item. Test: Task 2
   `test_a_type_code_never_names_an_item`, Go `TestATypeCodeNeverNamesAnItem`.
2. **Only new-subject labels with no label address.** A request of only `type_ids` and no base
   must answer 409 `no_label_base`, as a place label does. Test: Task 3
   `test_a_type_label_needs_the_label_address`.
3. **The cap across all lists.** 60 spool ids and 41 item ids is 101 labels and must be 422.
   Test: Task 3 `test_the_cap_counts_every_list`.
4. **A new row after the migration.** A type made after the migration takes a number above
   every backfilled type and item; a second one takes the next. Test: Task 1
   `test_a_new_type_takes_the_next_shared_number`.
5. **One id on the Labels page fails.** `?types=a,b` where `b` is 404: `a` draws and `b` is
   named as not read, as `?locations=` does. Test: Task 5
   `names a type that could not be read and draws the rest`.

---

### Task 1: The numbers: one sequence for spool types and items

**Files:**
- Create: `backend/scadbuddy/migrations/<UTC timestamp>_label_numbers.sql`
- Modify: `backend/scadbuddy/filament/models.py` (`SpoolType`: `number: int`; not on
  `SpoolTypeCreate` or `SpoolTypePatch`)
- Modify: `frontend/src/mocks/features/filament/seed.ts` (`spoolType`: each seeded type gets
  `number`, from one counter shared with nothing else in the mock), so the generated
  `S['SpoolType']` still typechecks. A type the mock makes later gets its number in Task 5.
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/filament/test_types.py` (the
  existing type store tests; add there)

**Interfaces:**
- Produces: columns `filament_spool_types.number` and `items.number` (`integer NOT NULL`,
  unique, default from `item_numbers_seq`); `SpoolType.number: int` on every type read.

- [ ] **Step 1: Write the failing tests**

`test_pg_migrations.py`:

```python
def test_the_label_numbers_migration_numbers_types_then_items(pg_conninfo: str) -> None:
    # migrate(conn, MIGRATIONS[:_index("label_numbers")]); insert two spool types with
    # created_at '2026-01-02' and '2026-01-01', and two items (kind 'hardware' or any seeded
    # lot kind) with created_at '2026-01-01' and '2026-01-03'; migrate(conn).
    assert type_numbers == {older_type: 1, newer_type: 2}
    assert item_numbers == {older_item: 3, newer_item: 4}
    assert nextval == 5  # SELECT nextval('item_numbers_seq')
```

`test_types.py`:

```python
def test_a_new_type_takes_the_next_shared_number(store, pg_pool) -> None:
    # one item inserted by SQL first (tests may write items), then two types by create()
    assert first.number > item_number
    assert second.number == first.number + 1
    assert store.get(first.id).number == first.number
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run pytest tests/test_pg_migrations.py -k label_numbers tests/filament/test_types.py -k shared_number -x`
Expected: FAIL (no migration; `SpoolType` has no `number`).

- [ ] **Step 3: Write the migration and the field**

The migration, in this order: `CREATE SEQUENCE item_numbers_seq AS integer;` add
`number integer` to both tables; `UPDATE` spool types with `row_number() OVER (ORDER BY
created_at, id)`; `UPDATE` items with the spool types' max plus `row_number()` by the same
order; `setval('item_numbers_seq', <max of both> + 1, false)`; then per table `SET DEFAULT
nextval('item_numbers_seq')`, `SET NOT NULL`, `ADD CONSTRAINT <table>_number_key UNIQUE
(number)`. A header comment says why one sequence (design D3). Add `number: int` to
`SpoolType` after `code`, with a comment: the typed code is `T-<number>`; `code` is the
maker's.

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: the Step 2 command, then `uv run ruff check scadbuddy tests && uv run mypy scadbuddy`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

`feat(filament): spool types and items take a number from one sequence (#7169)`.

---

### Task 2: `T-<n>` and `I-<n>` resolve, in Python and in Go

**Files:**
- Modify: `backend/scadbuddy/codes/resolve.py` (`Kind`, `PREFIXES`, `_NAME` → `NAME_SQL`)
- Modify: `backend/scadbuddy/api/codes.py` (the route's `description`, the module docstring)
- Modify: `tools/go/core/internal/scan/resolve.go` (`prefixes`)
- Modify: `frontend/src/lib/resolvedCode.ts` (`NOUN`: `spool_type: 'Type'`, `item: 'Item'`;
  `codeTarget`: `filamentLinks.type(id)`, `inventoryPaths.item(id)`), so the new `Resolved`
  kinds typecheck and the scan page and `/x/<code>` open them (design D6, D12)
- Modify: `agent/src/tools/filament.ts` (`filament_code_resolve` description: `L-12` a place,
  `S-142` a spool, `T-4` a spool type, `I-9` an item, `K-3` an item kind)
- Modify: `docs/features/spools-and-filament.md` ("Codes (#6955)" bullet),
  `docs/features/inventory.md` (the kind code sentence: items have `I-<n>`)
- Test: `backend/tests/codes/test_resolve.py`, `backend/tests/api/test_codes.py`,
  `tools/go/core/internal/scan/scan_test.go`, `frontend/src/lib/resolvedCode.test.ts`
  (create if missing)

**Interfaces:**
- Consumes: Task 1's columns.
- Produces: `Kind = Literal["location", "spool", "item_kind", "spool_type", "item"]`;
  `NAME_SQL: dict[str, str]` keyed by table (`"filament_spool_types": "concat_ws(' ', code,
  colour_name)"`, `"items": "name"`, the old three); `PREFIXES["T"] = Lookup(
  "filament_spool_types", "spool_type")`, `PREFIXES["I"] = Lookup("items", "item")`. Go:
  `"T": {"filament_spool_types", "spool_type", "concat_ws(' ', code, colour_name)"}`,
  `"I": {"items", "item", "name"}`. The OpenAPI `Resolved.kind` enum gains the two values.

- [ ] **Step 1: Write the failing tests**

`test_resolve.py`:
- `test_a_type_resolves_by_its_number`: a type with code `10101`, colour name `Charcoal` →
  `{"kind": "spool_type", "id": <uuid>, "code": "T-<n>", "name": "10101 Charcoal", "archived": False}`.
- `test_a_type_with_no_colour_name_is_its_code`: name `"10101"`.
- `test_an_item_resolves_by_its_number`: an item inserted by SQL → kind `item`, its name.
- `test_an_archived_item_still_resolves`: `archived` True.
- `test_a_type_code_never_names_an_item`: `resolve(conn, Code("T", item_number))` is None, and
  `Code("I", type_number)` is None.

`test_codes.py`: `GET /api/v1/codes/t<n>` (lower case, no dash) answers the type.

`resolvedCode.test.ts`: `codeLabel` gives `Type T-4` and `Item I-9`; `codeTarget` gives the
type page and the item page.

`scan_test.go`: `TestATypeCodeResolvesAsPythonsDoes` and `TestAnItemCodeResolvesAsPythonsDoes`
(same fields as the Python tests, as `TestAKindsCodeResolvesAsPythonsDoes`), and
`TestATypeCodeNeverNamesAnItem`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run pytest tests/codes tests/api/test_codes.py -x` and
`cd tools/go/core && go test ./internal/scan -run 'Type|Item'`
Expected: FAIL (unknown prefix answers None / nil).

- [ ] **Step 3: Add the entries**

Python and Go as in Interfaces. `labels.py` imports `NAME_SQL` in Task 3, so it is public now.

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: the Step 2 commands, `uv run ruff check scadbuddy tests && uv run mypy scadbuddy`,
`go vet ./internal/scan`, `cd agent && pnpm lint && pnpm typecheck`,
`cd frontend && pnpm gen:api && pnpm exec vitest run src/lib/resolvedCode.test.ts && pnpm typecheck && pnpm lint`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

`feat(filament): T-<n> and I-<n> codes resolve in /codes, tags and the scan (#7169)`.

---

### Task 3: The labels file draws type, item and kind labels

**Files:**
- Modify: `backend/scadbuddy/filament/label_pdf.py` (`PlaceLabel` → `CodeLabel`; its docstring
  names the five subjects)
- Modify: `backend/scadbuddy/filament/label_png.py` (the rename only)
- Modify: `backend/scadbuddy/filament/labels.py` (`pdf_labels`, a `_code_labels` helper)
- Modify: `backend/scadbuddy/filament/models.py` (`LabelsRequest`)
- Modify: `backend/scadbuddy/api/filament.py` (`render_labels`: the count, the call, the
  `description`)
- Modify: `docs/features/spools-and-filament.md` ("Labels:" bullet)
- Test: `backend/tests/api/test_filament_label_pdf.py`, `backend/tests/filament/test_label_pdf.py`,
  `backend/tests/filament/test_label_png.py` (the rename)

**Interfaces:**
- Consumes: `NAME_SQL`, `PREFIXES` (Task 2); Task 1's columns.
- Produces: `CodeLabel(code: str, name: str, qr: str)` (frozen dataclass);
  `Label = SpoolLabel | CodeLabel`;
  `pdf_labels(spools: FilamentSpoolStore, locations: LocationStore, spool_ids: Sequence[UUID],
  location_ids: Sequence[UUID], base: str | None, *, type_ids: Sequence[UUID] = (),
  item_ids: Sequence[UUID] = (), kind_ids: Sequence[str] = ()) -> list[Label]`. The code rows
  are read on `locations.pool`, the pool `printer_labels` already uses.
  `LabelNotFoundError("spool type" | "item" | "item kind", id)` for an unknown id.
  `LabelsRequest._one_to_max` sums the five lists.

- [ ] **Step 1: Write the failing tests**

`test_filament_label_pdf.py` (the `drawn` fixture and `_place` exist):
- `test_a_type_label_opens_the_label_address`: with a base set, `{"type_ids": [t]}` draws
  `[CodeLabel(code=f"T-{n}", name="10101 Charcoal", qr=f"https://sb.example.com/x/T-{n}")]`.
- `test_a_type_label_needs_the_label_address`: no base → 409 `no_label_base`.
- `test_an_item_and_a_kind_get_code_labels`: one item, one kind → `I-<n>` with the item's
  name, `K-<n>` with the kind's name.
- `test_an_archived_item_still_gets_its_label`.
- `test_an_unknown_type_is_404`.
- `test_the_labels_keep_the_subject_order`: one id of each of the five lists sent in any key
  order → spool, place, type, item, kind.
- `test_the_cap_counts_every_list`: 60 spool ids and 41 item ids → 422.
- `test_a_png_of_one_type_label_is_named_by_its_code`: two type labels as `png`, roll →
  ZIP entries `T-<n>.png`, `T-<m>.png`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd backend && uv run pytest tests/api/test_filament_label_pdf.py tests/filament/test_label_pdf.py tests/filament/test_label_png.py -x`
Expected: FAIL (`type_ids` is an extra field: 422).

- [ ] **Step 3: Implement**

`_code_labels(conn, table: str, prefix: str, what: str, ids, base) -> list[CodeLabel]`: one
`SELECT id, number, {NAME_SQL[table]} AS name FROM {table} WHERE id = ANY(%s)` (table and
fragment from code only), then the labels in `ids` order; a missing id is
`LabelNotFoundError(what, id)`; the base and payload checks are the place label's, moved into
one helper both use. The route's `count` sums the five lists. The description adds: "then
`type_ids`, `item_ids` and `kind_ids` … a spool type's, an item's and a kind's label is the
place label's shape: its code (`T-<n>`, `I-<n>`, `K-<n>`) and name with the QR
`<label address>/x/<code>`".

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: the Step 2 command, `uv run pytest tests/filament/test_label_fit_vectors.py`, then
`uv run ruff check scadbuddy tests && uv run mypy scadbuddy`
Expected: PASS; clean.

- [ ] **Step 5: Commit and open PR A**

`feat(filament): the labels file draws spool type, item and kind labels (#7169)`. Open PR A,
title `feat(filament): codes and label files for spool types and items (#7169)`, body
`Refs #7169` (PR B fixes it) and the overlap paragraph of the design.

---

### Task 4: After #8323 merges: an item read carries its number and code

**Files:**
- Modify: `tools/go/core/internal/inventory/wire/wire.go` (`Item`: `Number int64
  json:"number"`, `Code string json:"code"`, placed and commented as on `ItemKind`)
- Modify: `tools/go/core/internal/inventory/store/items.go` (the item read's SELECT and scan;
  `Code` is `"I-" + number`)
- Modify: `backend/scadbuddy/inventory/models.py` (`Item`: `number: int`, `code: str`, as
  `ItemKind`)
- Modify: `frontend/src/mocks/features/inventory.ts` (each mock item gets `number` and `code`;
  new items take the next number)
- Test: `tools/go/core/internal/inventory/store/items_test.go`,
  `backend/tests/inventory/test_go_answers.py` (or the merged file that checks Go's answers
  against the Python models)

**Interfaces:**
- Consumes: `items.number` (Task 1).
- Produces: every item answer (list, get, create, patch) carries `number: int` and
  `code: "I-<n>"`; the generated `Schemas['Item']` has both.

- [ ] **Step 1: Read the merged files**

`git log -1 --format=%H origin/main -- tools/go/core/internal/inventory/store/items.go`;
read `items.go`, `wire.go` (`ItemKind`'s `Number`/`Code`), and how `kinds.go` fills them.

- [ ] **Step 2: Write the failing tests**

`items_test.go`: `TestAnItemReadCarriesItsCode`: a created item's answer has `Number > 0` and
`Code == "I-" + strconv.FormatInt(Number, 10)`; a second item's number is larger. The Python
answer check: an item answer with `number` and `code` validates against `Item`.

- [ ] **Step 3: Run them to see them fail**

Run: `cd tools/go/core && go test ./internal/inventory/store -run Code`
Expected: FAIL.

- [ ] **Step 4: Implement as `ItemKind` does**

- [ ] **Step 5: Run them to see them pass, and the checks**

Run: Step 3's command, `go vet ./internal/inventory/...`, the Python check test,
`cd frontend && pnpm gen:api && pnpm typecheck`
Expected: PASS; clean.

- [ ] **Step 6: Commit**

`feat(inventory): an item read carries its number and code I-<n> (#7169)`.

---

### Task 5: The browser: resolved types and items, and the Labels page

Load `frontend-design:frontend-design` first.

**Files:**
- Modify: `frontend/src/lib/filamentLinks.ts` (`typeLabel`, `itemLabel`, `kindLabel`)
- Modify: `frontend/src/components/filament/Label.tsx` (`LocationLabel` → `CodeLabel`)
- Modify: `frontend/src/pages/filament/LabelsPage.tsx` (one code-label mode for
  `locations`, `printers`, `types`, `items`, `kinds`; `fileBody`; heading and back link)
- Modify: `frontend/src/mocks/features/filament/index.ts` (`resolveCodeText`: `K`, `T`, `I`;
  each mock type has `number`; `POST /filament/labels` takes the three lists)
- Modify: `frontend/src/pages/CodePage.tsx` (placeholder `L-12, T-4 or I-9`)
- Test: `frontend/src/components/filament/Label.test.tsx`, `frontend/src/pages/filament/LabelsPage.test.tsx`

**Interfaces:**
- Consumes: `SpoolType.number` (Task 1), `Resolved.kind` values and `resolvedCode.ts`
  (Task 2), `LabelsRequest`
  lists (Task 3), `Item.number`/`code` and `ItemKind.code` (Task 4, #8323).
- Produces: `filamentLinks.typeLabel(id: string): string` →
  `/filament/labels?types=<id>`; `itemLabel(id)` → `?items=<id>`; `kindLabel(id)` →
  `?kinds=<id>`.

- [ ] **Step 1: Write the failing tests**

`LabelsPage.test.tsx`:
- `draws a spool type label from ?types=`: heading "Spool type labels"; the label shows
  `T-<n>` and `10101 Charcoal`; Download sends `type_ids: [id]` and no other ids.
- `draws item and kind labels from ?items= and ?kinds=`: `item_ids` / `kind_ids` sent; the
  back link goes to the inventory list / the kinds page.
- `names a type that could not be read and draws the rest` (`?types=a,b`, `b` 404).
- `says a type label needs the label address`, as the place labels do.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/filament/Label.test.tsx src/pages/filament/LabelsPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

The code-label mode keeps one list of `{ id, subject, code, name, svg }` with
`subject: 'location' | 'spool_type' | 'item' | 'item_kind'`; `fileBody` groups ids by
subject into `location_ids`, `type_ids`, `item_ids`, `kind_ids`. A type's label text is
`T-${type.number}` and `[type.code, type.colour_name].filter(Boolean).join(' ')` (design D5);
an item's and a kind's are their `code` and `name` from `inventoryApi.item(id)` and
`inventoryApi.kinds(true)`. Headings: "Spool type labels", "Item labels", "Kind labels".

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: the Step 2 vitest command, `pnpm typecheck && pnpm lint`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

`feat(filament): the Labels page prints spool type, item and kind labels (#7169)`.

---

### Task 6: Print actions on the type, item and kind pages

Load `frontend-design:frontend-design` first.

**Files:**
- Modify: `frontend/src/pages/filament/TypePage.tsx` (header, beside Archive: "Print label",
  `filamentLinks.typeLabel`; a saved type only)
- Modify: `frontend/src/pages/inventory/ItemPage.tsx` (header: "Print label",
  `filamentLinks.itemLabel`; the item's `code` shown beside its name)
- Modify: `frontend/src/pages/inventory/KindsPage.tsx` (each kind row: a printer icon link,
  `aria-label` `Print label for <name>`, `filamentLinks.kindLabel`, as `LocationTree.tsx`)
- Modify: `docs/features/inventory.md` (items and kinds print labels from their pages)
- Test: `frontend/src/pages/filament/TypePage.test.tsx`, `frontend/src/pages/inventory/ItemPage.test.tsx`,
  `frontend/src/pages/inventory/KindsPage.test.tsx`

**Interfaces:**
- Consumes: Task 5's links; Task 4's `Item.code`; `ItemKind.code` (#8323).

- [ ] **Step 1: Write the failing tests**

Each page: `links to its label` (the link's `href` is the Task 5 path for the row's id). The
new type page has no Print label link. ItemPage: `shows the item's code`.

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm exec vitest run src/pages/filament/TypePage.test.tsx src/pages/inventory/ItemPage.test.tsx src/pages/inventory/KindsPage.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run them to see them pass, and the checks**

Run: the Step 2 command, `pnpm typecheck && pnpm lint`
Expected: PASS; clean.

- [ ] **Step 5: Commit, open PR B, file the follow-up**

`feat(inventory): print a label from the type, item and kind pages (#7169)`. PR B's body says
`Fixes #7169`. File the follow-up issue "codes and labels for lots and units" under #6949,
blocked on Q1 (design D1), and tell session c4f175d1 in its channel that the scan opens
types and items through `resolvedCode.ts` with no change on its side (design D12).

---

## Self-review

1. **Spec coverage.** D1, D2: Tasks 2, 3, 5. D3, D4: Task 1. D5: Tasks 2, 3, 5. D6: Tasks 2,
   5. D7, D8, D9: Task 3 (and Task 5 for the browser label). D10: Tasks 1, 4. D11: Tasks 5, 6.
   D12: Task 2 (`resolvedCode.ts`) and Task 6 step 5. D13: Task 2. R4: PR B (Tasks 4 to 6)
   waits for #8323; PR A touches none of its files. R5: Tasks 5,
   6 load the skill, and the mocks are in Tasks 4, 5.
2. **Step scan.** Each test step names its tests and the exact values; each code step names
   its file and signature. Task 4 depends on #8323's merged shape, so step 1 reads it and
   step 4 copies `ItemKind`'s pattern.
3. **Type consistency.** `CodeLabel` in Python and the browser; `NAME_SQL`, `PREFIXES`,
   `Kind` values `spool_type` and `item`; `type_ids`, `item_ids`, `kind_ids`; `typeLabel`,
   `itemLabel`, `kindLabel` match across tasks.
4. **Review Focus.** Each line names its test and owning task.
5. **Proportion.** The plan has signatures and assertions only; the one SQL order it fixes is
   the migration's, which the test does not fully decide.
