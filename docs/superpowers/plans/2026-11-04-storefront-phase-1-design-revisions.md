# Storefront phase 1: design revisions for line items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each order line item goes through design revisions (a render, a test-print photo, or both) that the shop sends, records the customer's answer to, quotes and test-prints, all in the back office (#5280).

**Architecture:** A new `order_item_revisions` table holds every design an item has had; `order_items.current_revision_id` names the one in force, and the item's reference columns and colours become a copy that one store method (`OrderStore.set_design`) writes, called only from one lifecycle method (`Lifecycle._revise`). Every transition stays a `Lifecycle` method under the order's row lock with its audit row and bus event. Test prints reuse the print path (#4719) as attempts with `purpose = 'test'`, which every production rule filters out at the store.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, Temporal (operations), pytest; TypeScript agent tools (zod, `defineTool`); React 19 + msw + vitest + Playwright.

**Spec:** `docs/superpowers/specs/2026-11-02-storefront-design.md` §4.1-§4.7, §6.2 (`add_revision`, `send_revision`, `answer_revision`), §6.3 (`item_has_no_design`, `revision_not_newest`, `revision_not_sent`, `revision_answered`). The orders spec is `docs/superpowers/specs/2026-10-18-orders-design.md`. Issue #5280.

Not in this phase (later plans): `submit_order`, `orders.source`, the `customer` actor kind (§6.4), deposits, the balance, `set_deposit`, `waive_balance`, the public site.

## Global Constraints

- Migrations: ONE new file `backend/scadbuddy/migrations/$(date -u +%Y%m%dT%H%MZ)_order_item_revisions.sql`; edit no merged file (CLAUDE.md "Migrations").
- Only `Lifecycle._revise` (through `OrderStore.set_design`) writes an item's `slug`, `commit`, `inputs`, `libraries`, `template_name`, `render_key`, `colours` and `current_revision_id` after the item's insert (spec §4.1). `OrderStore.insert_item` writes them once, and its only caller (`add_item`) makes revision 1 in the same transaction.
- `ItemKind = Literal["model", "custom", "request"]`. `custom`: reference columns all null, no revisions. `model`: all set. `request`: all null or all set (spec §4.2). The kind stays `request` after its first revision.
- `Approval = Literal["design_first", "together"]`; default `together` for `model` (and `custom`, where it is never read), `design_first` for `request` (spec §4.5).
- `RevisionAnswer = Literal["accepted", "changes_requested", "superseded"]`; a person may record only `accepted` or `changes_requested`; `superseded` is written only by `_revise` (spec §4.3).
- `AttachmentRole` gains `render` and `test_print`; the subject `order_item_revision:<id>` (spec §4.3).
- `FulfilmentPurpose = Literal["production", "test"]`; every existing row is `production`; `revision_id` is required for `test` (spec §4.4).
- New refusal slugs, each a `LifecycleError` mapped by `orders/problems.py` `refused` to 409 `https://scadbuddy.dev/problems/orders/<slug>`: `item_has_no_design`, `revision_not_newest`, `revision_not_sent`, `revision_answered` (spec §6.3), and this plan's `revision_empty`, `revision_not_quoted`, `revision_not_accepted`, `revision_not_found` (404). See "Spec gaps" below.
- Phase 1 "Send" only stamps `sent_at`. The shop records the customer's answer itself, as `current_actor()` (a `browser` `Actor`), naming the customer in the note (spec §4.3).
- No HTML from notes: a revision's `note` and `answer_note` are plain text, rendered as text in the UI.
- Every new `/api/v1` operation needs an agent tool or a `NOT_A_TOOL` entry in `agent/src/tools/coverage.ts` (`test/coverage.test.ts`).
- Generated files (`backend/openapi.json`, both `schema.d.ts`) are never committed; `pnpm gen:api` regenerates them.

### Spec gaps this plan settles (each a call the owner may overturn)

1. Spec §4.3 puts "a quote whose filaments differ … makes a new revision" in the quote's **check**. The check writes nothing by rule (`orders/quote.py:5-7`, Ruling 8; an operation's check runs before its row exists). This plan makes that revision in the run's first write, where the filaments are written today (`orders/quote.py:234` `set_filaments`), before anything is sliced. The effect the spec wants (the quote is sliced from the revision it names) holds.
2. The print path prints the item's **output**, not the quote's sliced file (`orders/operations.py:271` `print_body`, `printing.py:131` `print_check`): the sliced file is held as provenance. So a test print can print only the item's current revision (its output is the current reference's render). `order_item_print` with `purpose: test` refuses an older revision with `revision_not_newest`.
3. "A revision is sent with at least one attachment" has no slug: `revision_empty`.
4. `together` sends "a revision with a quote" and accepts "its quote": a `together` send or accept with no quote of the revision is `revision_not_quoted`.
5. `design_first` accepts the price only after the design: `accept_quote` of a quote whose revision is not `accepted`, on a `design_first` item, is `revision_not_accepted`; "Add to model photos" on a revision not `accepted` is the same slug.
6. `order_attachments.fulfilment_id` already exists on the wire (`orders/models.py:457`), derived from an `order_item_fulfilment:<id>` subject (`orders/store.py:240-263`). This plan adds the column for the revision subject only (a `test_print` photo naming its attempt) and keeps the derivation for the attempt subject.
7. `template_media` rows are rewritten whole on every media edit (`library/media_store.py:74` `replace`), so `source_commit` and `source_revision_id` must travel on `MediaItem` or the first reorder loses them.
8. A request item's customer text is the item's `notes` (no new column).

## Review Focus

1. **A draft item with test attempts is deleted** (a new revision drops the item to `draft`; its test attempts hold sliced files under `order_item_fulfilment`, which nothing cascades). Expected: `delete_item` refuses an item with any attempt (`item_not_draft`, "cancel it instead"), and removes the revision subjects' attachments with their holds when it may delete. Test in Task 6.
2. **The print follow settles a test attempt** (`follow_print`, `release_print`, `update_fulfilment` read the latest attempt and move the item). Expected: a test attempt's outcome is recorded, and the item's status never moves. Test in Task 5.
3. **A quote recorded by the previous build during the rollout** (its `revision_id` is NULL). Expected: read as naming the item's current revision, so it is neither stale nor lost. Test in Task 4.
4. **An older revision's inputs name an uploaded asset** (`OrderStore.item_inputs` feeds the asset sweep's referenced ids). Expected: the asset stays while any revision names it. Test in Task 1.
5. **Two revisions race a slice quote** (a person makes a revision while the quote's render runs). Expected: `record_slice_quote` refuses `revision_not_newest`, drops its holds, and records nothing. Test in Task 4.

---

## File structure

Backend (`backend/scadbuddy/`):
- `migrations/<ts>_order_item_revisions.sql` (create): every schema change and the backfill.
- `orders/models.py` (modify): `Revision`, `RevisionAnswer`, `Approval`, `FulfilmentPurpose`; new fields on `OrderItem`, `Quote`, `Fulfilment`, `Attachment`.
- `orders/store.py` (modify): the revision rows, `set_design`, the purpose filter.
- `orders/revisions.py` (create): `RevisionInput`, `RevisionAnswerInput` bodies and `revision_subject`, kept out of the 2,100-line `lifecycle.py`.
- `orders/lifecycle.py` (modify): `_revise`, `add_revision`, `send_revision`, `answer_revision`, `add_request_item`, test-print claims, the revision rules on quotes and attachments.
- `orders/quote.py`, `orders/printing.py`, `orders/operations.py`, `orders/costs.py` (modify).
- `api/orders.py`, `api/order_attachments.py` (modify): the routes.
- `library/media.py`, `library/media_store.py`, `library/catalogue.py`, `library/media_operations.py` (modify): the source columns.

Agent: `agent/src/tools/orders.ts`, `agent/src/tools/coverage.ts`, `agent/test/orders.test.ts`.

Frontend (`frontend/src/`): `api/types.ts`, `api/client.ts`, `mocks/features/orders/state.ts`, `mocks/features/orders/orders.ts`, `mocks/features/orders/revisions.ts` (create), `components/orders/RevisionsPanel.tsx` (create) with its test, `components/orders/PrintItem.tsx`, `pages/OrderPage.tsx`, `../e2e/orders.spec.ts`.

---

### Task 1: Schema, row shapes and the store's revision rows

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_order_item_revisions.sql`
- Modify: `backend/scadbuddy/orders/models.py:50,92-100,274-296,312-329,335-373,413-416,448-476`
- Modify: `backend/scadbuddy/orders/store.py:116-140,173-201,240-263,870-1010,1013-1052,1147-1194,1485-1494,1641-1690`
- Test: `backend/tests/test_pg_migrations.py`, `backend/tests/orders/test_store.py`

**Interfaces:**
- Consumes: `render/pg_store.py:84` `migrate(conn, migrations)`; `tests/test_pg_migrations.py` `_index(slug)`.
- Produces (models):
  - `ItemKind = Literal["model", "custom", "request"]`; `Approval = Literal["design_first", "together"]`; `RevisionAnswer = Literal["accepted", "changes_requested", "superseded"]`; `FulfilmentPurpose = Literal["production", "test"]`; `AttachmentRole` plus `"render"`, `"test_print"`.
  - `class Revision(BaseModel)`: `id: str`, `item_id: str`, `number: int`, `reference: Reference`, `colours: list[ColourSlot]`, `note: str`, `created_at: datetime`, `created_by: Actor`, `sent_at: datetime | None`, `answer: RevisionAnswer | None`, `answer_note: str | None`, `answered_at: datetime | None`, `answered_by: Actor | None`.
  - `OrderItem` gains `current_revision_id: str | None = None`, `approval: Approval = "together"`.
  - `Quote` gains `revision_id: str | None = None`.
  - `Fulfilment` gains `purpose: FulfilmentPurpose = "production"`, `revision_id: str | None = None`.
  - `Attachment` gains `revision_id: str | None = None` (from an `order_item_revision:<id>` subject).
- Produces (store, all in the caller's transaction where they take `conn`):
  - `revision_subject(revision_id: str) -> str` (`"order_item_revision:<id>"`), beside `item_subject` (`store.py:216`).
  - `insert_item(conn, order_id, *, kind: ItemKind = "model", reference: Reference | None, colours, output_id, quantity, title=None, notes="", sku=None, approval: Approval | None = None) -> OrderItem` (`store.py:870`; `approval` None picks the kind's default; `title` defaults to the template's name, or is required for a request).
  - `insert_revision(conn, item_id: str, *, reference: Reference, colours: Sequence[ColourSlot], note: str, created_by: Actor) -> Revision` (number = the item's `MAX(number) + 1`).
  - `set_design(conn, item_id: str, revision: Revision, *, output_id: str | None) -> OrderItem`: the one `UPDATE order_items` that sets the reference columns, `colours`, `current_revision_id`, and `kind = 'model', requires_printing = true` when the item was `custom`.
  - `revision(revision_id, *, conn=None) -> Revision | None`; `revisions(item_id, *, conn=None) -> list[Revision]` (by `number`).
  - `stamp_revision(conn, revision_id, *, sent_at: datetime | UnsetType = UNSET, answer: RevisionAnswer | UnsetType = UNSET, answer_note: str | None | UnsetType = UNSET, answered_at: datetime | UnsetType = UNSET, answered_by: Actor | UnsetType = UNSET) -> Revision`.
  - `update_item(conn, item_id, *, …, approval: Approval | UnsetType = UNSET) -> OrderItem` (`store.py:946`): gains `approval`. Its `reference` and `colours` parameters stay until Task 2 removes them.
  - `insert_quote(..., revision_id: str | None = None)` (`store.py:1013`).
  - `insert_fulfilment(..., purpose: FulfilmentPurpose = "production", revision_id: str | None = None)` (`store.py:1147`).
  - `fulfilments(item_id, *, conn=None, purpose: FulfilmentPurpose | None = "production") -> list[Fulfilment]` (`store.py:1485`): None is every attempt.
  - `insert_attachment(..., subject_item_id: str | None = None, fulfilment_id: str | None = None)` (`store.py:1641`; `attempt_item_id` renamed at the parameter only, the column keeps its name).
  - `item_inputs()` (`store.py:1002`) also yields every revision's `inputs`.

- [ ] **Step 1: Write the failing migration test** in `backend/tests/test_pg_migrations.py`

```python
def test_the_revisions_migration_gives_every_model_item_revision_one(pg_conninfo: str) -> None:
    # Migrate to just before the file; insert one customer, one order, a model item with
    # colours [{"slot":0,"colour":"#ff0000","filament_id":null}], a custom item, a quote on
    # each and one fulfilment on the model item; then migrate the rest.
    ...
    assert migrate(conn)[0] == MIGRATIONS[_index("order_item_revisions")].id
    rev = conn.execute("SELECT * FROM order_item_revisions").fetchall()
    assert len(rev) == 1 and rev[0]["number"] == 1 and rev[0]["item_id"] == model_id
    assert rev[0]["slug"] == "demo" and rev[0]["colours"] == [{"slot": 0, "colour": "#ff0000", "filament_id": None}]
    assert rev[0]["sent_at"] is None and rev[0]["answer"] is None
    item = conn.execute("SELECT current_revision_id, approval FROM order_items WHERE id = %s", (model_id,)).fetchone()
    assert item == {"current_revision_id": rev[0]["id"], "approval": "together"}
    assert conn.execute("SELECT current_revision_id FROM order_items WHERE id = %s", (custom_id,)).fetchone()["current_revision_id"] is None
    quotes = dict(conn.execute("SELECT order_item_id, revision_id FROM order_item_quotes").fetchall())
    assert quotes == {model_id: rev[0]["id"], custom_id: None}
    assert conn.execute("SELECT purpose, revision_id FROM order_item_fulfilments").fetchone() == {"purpose": "production", "revision_id": None}
```

- [ ] **Step 2: Write the failing store tests** in `backend/tests/orders/test_store.py`

```python
def test_a_request_item_has_no_reference_until_its_design(rows: OrderStore) -> None:
    # insert_item(kind="request", reference=None, colours=[], output_id=None, quantity=1, title="A dragon")
    assert item.kind == "request" and item.reference is None and item.approval == "design_first"

def test_the_table_refuses_a_half_designed_request(rows: OrderStore, pool: PgPool) -> None:
    # raw UPDATE setting only slug on a request row
    with pytest.raises(psycopg.errors.CheckViolation): ...

def test_revisions_are_numbered_per_item(rows: OrderStore) -> None:
    assert [r.number for r in rows.revisions(item.id)] == [1, 2]   # two insert_revision on one item
    assert rows.revisions(other.id)[0].number == 1

def test_set_design_copies_the_revision_onto_the_item(rows: OrderStore) -> None:
    assert moved.reference == revision.reference and moved.colours == revision.colours
    assert moved.current_revision_id == revision.id

def test_fulfilments_leave_out_test_attempts_unless_asked(rows: OrderStore) -> None:
    assert [a.purpose for a in rows.fulfilments(item.id)] == ["production"]
    assert [a.purpose for a in rows.fulfilments(item.id, purpose=None)] == ["production", "test"]

def test_a_test_attempt_needs_its_revision(rows: OrderStore, pool: PgPool) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        rows.insert_fulfilment(conn, item.id, quantity=0, outcome="unknown", created_by=BY, purpose="test")

def test_item_inputs_name_every_revisions_inputs(rows: OrderStore) -> None:
    # revision 1 inputs {"params": {"logo": "asset-1"}}, revision 2 inputs {"params": {"logo": "asset-2"}}
    assert {"params": {"logo": "asset-1"}} in list(rows.item_inputs())
```

- [ ] **Step 3: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/test_pg_migrations.py::test_the_revisions_migration_gives_every_model_item_revision_one tests/orders/test_store.py -k "request or revision or design or test_attempt or item_inputs" -v`
Expected: FAIL (no table `order_item_revisions`, no `insert_revision`).

- [ ] **Step 4: Write the migration**

One file, in this order:
- `CREATE TABLE order_item_revisions` (`id text PK DEFAULT gen_random_uuid()::text`, `item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE`, `number integer NOT NULL CHECK (number >= 1)`, `slug`, `commit` (`CHECK (commit ~ '^[0-9a-f]{40}$')`), `inputs jsonb`, `libraries jsonb NOT NULL DEFAULT '[]'`, `template_name`, `render_key` all NOT NULL, `colours jsonb NOT NULL DEFAULT '[]'`, `note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 4000)`, `created_at timestamptz NOT NULL DEFAULT clock_timestamp()`, `created_by jsonb NOT NULL`, `sent_at timestamptz`, `answer text CHECK (answer IN ('accepted','changes_requested','superseded'))`, `answer_note text CHECK (char_length(answer_note) <= 4000)`, `answered_at timestamptz`, `answered_by jsonb`, `CHECK ((answer IS NULL) = (answered_at IS NULL))`, `UNIQUE (item_id, number)`).
- `order_items`: `ADD COLUMN current_revision_id text REFERENCES order_item_revisions (id)`, `ADD COLUMN approval text NOT NULL DEFAULT 'together' CHECK (approval IN ('design_first','together'))`; drop `order_items_kind_check` and add it with `'request'`; drop `order_items_reference` (`20261029T0733Z_order_custom_items.sql`) and add it with a `WHEN 'request'` arm: `requires_printing AND ((slug, commit, inputs, template_name, render_key all NULL AND output_id IS NULL) OR (all NOT NULL))`.
- `order_item_quotes`: `ADD COLUMN revision_id text REFERENCES order_item_revisions (id)`.
- `order_item_fulfilments`: `ADD COLUMN purpose text NOT NULL DEFAULT 'production' CHECK (purpose IN ('production','test'))`, `ADD COLUMN revision_id text REFERENCES order_item_revisions (id)`, `CHECK (purpose = 'production' OR revision_id IS NOT NULL)`.
- `order_attachments`: replace `order_attachments_subject_check` with `'^(order|order_item|order_item_fulfilment|order_item_revision):[^:]+$'`; replace `order_attachments_role_check` adding `render`, `test_print`; replace `order_attachments_attempt_item_check` with `((subject LIKE 'order_item_fulfilment:%' OR subject LIKE 'order_item_revision:%') = (attempt_item_id IS NOT NULL))`; `ADD COLUMN fulfilment_id text`, `CHECK (fulfilment_id IS NULL OR subject LIKE 'order_item_revision:%')`.
- `template_media`: `ADD COLUMN source_commit text`, `ADD COLUMN source_revision_id text` (no foreign key: the order's rows are another feature's).
- Backfill: `INSERT INTO order_item_revisions (item_id, number, slug, commit, inputs, libraries, template_name, render_key, colours, created_at, created_by) SELECT i.id, 1, …, i.colours, i.created_at, o.created_by FROM order_items i JOIN orders o ON o.id = i.order_id WHERE i.kind = 'model'`; then `UPDATE order_items SET current_revision_id` from it; then `UPDATE order_item_quotes q SET revision_id = i.current_revision_id FROM order_items i WHERE i.id = q.order_item_id AND i.kind = 'model'`.

- [ ] **Step 5: Implement the models and store methods** named in Interfaces. `_item_of` (`store.py:173`) builds `reference` only when `slug` is not null (any kind). `_attachment_of` (`store.py:240`) sets `item_id` from `attempt_item_id` and `revision_id` from the subject for an `order_item_revision:` subject, and `fulfilment_id` from the column there. Add `current_revision_id, approval` to `_ITEM`, `revision_id` to `_QUOTE`, `purpose, revision_id` to `_FULFILMENT`, `fulfilment_id` to `_ATTACHMENT`. `costs.py:156` calls `store.fulfilments(item.id, purpose=None)` (job costing reads every attempt, spec §4.4).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_pg_migrations.py tests/orders -v && uv run mypy`
Expected: PASS (the existing orders tests too: nothing calls the new methods yet); mypy clean.

- [ ] **Step 7: Commit**

```bash
git add backend/scadbuddy/migrations backend/scadbuddy/orders/models.py backend/scadbuddy/orders/store.py backend/scadbuddy/orders/costs.py backend/tests/test_pg_migrations.py backend/tests/orders/test_store.py
git commit -m "feat(orders): order_item_revisions and the item's design columns as its copy (#5280)"
```

---

### Task 2: The revision writer: add_item, update_item and add_revision

**Files:**
- Create: `backend/scadbuddy/orders/revisions.py`
- Modify: `backend/scadbuddy/orders/lifecycle.py:788-818,895-1006`
- Modify: `backend/scadbuddy/orders/store.py:946-993`
- Test: `backend/tests/orders/test_revisions.py` (create; reuse `Shop`, `resolved` and `BY` by importing from `tests.orders.test_lifecycle`), `backend/tests/orders/test_store.py`

**Interfaces:**
- Consumes: Task 1's store methods and models; `ResolvedReference` (`orders/reference.py:70`, `.reference`, `.colours`); `Lifecycle._with_filaments` (`lifecycle.py:1008`); `_rederive` (`lifecycle.py:384`); `_open_for_items` (`lifecycle.py:781`).
- Produces:
  - `Lifecycle._revise(self, conn: Conn, order: Order, item: OrderItem, by: Actor, *, reference: Reference, colours: Sequence[ColourSlot], note: str = "", output_id: str | None | UnsetType = UNSET) -> tuple[OrderItem, Revision]`: the one caller of `set_design`. It marks the newest revision `superseded` (`answered_at` now, `answered_by` = `by`) when it is sent and unanswered, inserts the next revision, calls `set_design` (an unset `output_id` keeps the item's when the reference is unchanged, else clears it, as `lifecycle.py:976-979` does today), unaccepts the item's quotes (`store.unaccept_quotes`) and puts a `quoted` or `approved` item back to `draft`, and writes the audit row `revision.added` (`item_id`, `revision_id`, `number`, `superseded`: the id or None).
  - `Lifecycle.add_revision(self, order_id: str, item_id: str, by: Actor, resolved: ResolvedReference | None = None, *, output_id: str | None | UnsetType = UNSET, filaments: Mapping[int, str | None] | None = None, note: str = "") -> Revision`: `resolved` None copies the current revision's reference and colours (a revision for new photos or a new note). Refused: `item_custom` for a custom item, `item_has_no_design` for a request with no revision and no `resolved`, `item_not_open` for a settled item, `item_locked` past `quoted` (`_open_for_items`), `order_closed`/`order_cancelled`.
  - `add_item` (`lifecycle.py:788`) calls `insert_item` then `_revise` for revision 1 in the same transaction; its `item.added` audit row stays.
  - `update_item` (`lifecycle.py:895`) keeps its signature; a call that changes the reference, a slot's filament, or fills the colours of an item with none (`ensure_output`'s fill, `quote.py:386-395`) goes through `_revise` with the new reference and slots (`note=""`); a quantity change alone stays as today. `approval: Approval | None = None` is a new keyword, refused on a custom item (422 `approval`).
  - `OrderStore.update_item` (`store.py:946`) loses its `reference` and `colours` parameters, and `set_item_filaments` (`store.py:989`) goes: after this task `set_design` is the only writer of the design columns past the insert.
  - `orders/revisions.py`: `class RevisionInput(BaseModel)` (`note: str = Field(default="", max_length=4000)`, `filaments: dict[int, str | None] | None = None`, `model_config = ConfigDict(extra="forbid")`); `class RevisionAnswerInput(BaseModel)` (`answer: Literal["accepted", "changes_requested"]`, `note: str = Field(default="", max_length=4000)`).

- [ ] **Step 1: Write the failing tests** in `backend/tests/orders/test_revisions.py`

```python
def test_a_new_item_has_revision_one(shop: Shop) -> None:
    item = shop.item(shop.order())
    [rev] = shop.rows.revisions(item.id)
    assert rev.number == 1 and rev.reference == item.reference and item.current_revision_id == rev.id
    assert "revision.added" in shop.kinds(order)

def test_a_reference_edit_is_the_next_revision(shop: Shop) -> None:
    item = shop.lifecycle.update_item(order.id, item.id, BY, reference=resolved("other"))
    assert [r.number for r in shop.rows.revisions(item.id)] == [1, 2]
    assert item.reference.slug == "other" and item.current_revision_id == shop.rows.revisions(item.id)[1].id

def test_a_filament_change_is_the_next_revision_and_the_same_filaments_are_not(shop: Shop) -> None:
    # filaments={0: fil.id} twice
    assert len(shop.rows.revisions(item.id)) == 2   # not 3

def test_a_quantity_change_makes_no_revision(shop: Shop) -> None:
    assert len(shop.rows.revisions(item.id)) == 1

def test_a_new_revision_drops_the_accepted_quote(shop: Shop) -> None:
    item = shop.quoted(order, item)                     # approved
    shop.lifecycle.add_revision(order.id, item.id, BY, note="thinner walls")
    assert shop.get_item(item).status == "draft"
    assert not any(q.accepted for q in shop.rows.quotes(item.id))

def test_a_revision_without_a_reference_copies_the_current_design(shop: Shop) -> None:
    rev = shop.lifecycle.add_revision(order.id, item.id, BY, note="photo of the test")
    assert rev.number == 2 and rev.reference == item.reference and rev.note == "photo of the test"

def test_a_new_revision_supersedes_a_sent_unanswered_one(shop: Shop) -> None:
    # revision 1 marked sent through rows.stamp_revision(sent_at=NOW)
    shop.lifecycle.add_revision(order.id, item.id, BY)
    first = shop.rows.revisions(item.id)[0]
    assert first.answer == "superseded" and first.answered_by == BY

def test_an_unsent_revision_is_not_superseded(shop: Shop) -> None:
    assert shop.rows.revisions(item.id)[0].answer is None

def test_a_custom_item_takes_no_revision(shop: Shop) -> None:
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, custom.id, BY)
    assert refused.value.slug == "item_custom"

def test_a_revision_is_refused_once_the_order_is_accepted(shop: Shop) -> None:
    order, item = shop.accepted()
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.add_revision(order.id, item.id, BY)
    assert refused.value.slug == "item_locked"

def test_a_source_on_a_custom_item_makes_it_a_model_with_revision_one(shop: Shop) -> None:
    item = shop.lifecycle.update_item(order.id, custom.id, BY, reference=resolved())
    assert item.kind == "model" and [r.number for r in shop.rows.revisions(item.id)] == [1]

def test_approval_is_set_while_the_item_is_open(shop: Shop) -> None:
    assert shop.lifecycle.update_item(order.id, item.id, BY, approval="design_first").approval == "design_first"
```

In `backend/tests/orders/test_store.py` (the acceptance criterion "No route except `Lifecycle.add_revision` writes an item's reference columns. A test checks it."):

```python
def test_only_the_revision_writer_sets_an_items_design() -> None:
    # Spec §4.1: past insert_item's INSERT, set_design is the one UPDATE of the design columns,
    # and Lifecycle._revise its one caller.
    store_writers = {fn for fn, text in _sql_by_function(Path(store.__file__)) if re.search(r"UPDATE order_items SET[^\"]*\b(slug|commit|inputs|render_key|colours)\b", text)}
    assert store_writers == {"set_design"}
    assert {"reference", "colours"}.isdisjoint(inspect.signature(OrderStore.update_item).parameters)
    assert _callers(Path(lifecycle.__file__), "set_design") == {"_revise"}
```

`_sql_by_function(path)` and `_callers(path, attr)` are test helpers over `ast.parse`: the first yields each function's name with the string constants in its body joined, the second the names of the functions whose body has an attribute call `.attr(`.

Update the three existing tests that pass `reference=` to `update_item` (`test_lifecycle.py:363,375,400`, `test_custom_items.py:137`) only if they now fail for a reason the spec changed; they should pass unchanged.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_revisions.py -v`
Expected: FAIL (`add_revision` not defined).

- [ ] **Step 3: Implement** `_revise`, `add_revision`, and the `add_item`/`update_item` changes in `lifecycle.py`, and `orders/revisions.py`. `update_item` computes `slots` as today (`lifecycle.py:961-969`); when `reference is not None`, `refiled`, or the item had no slots and gets some, it calls `_revise` instead of passing `reference`/`colours` to `store.update_item`.

- [ ] **Step 4: Run the orders suite**

Run: `cd backend && uv run pytest tests/orders -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders backend/tests/orders
git commit -m "feat(orders): every design change of an item is its next revision (#5280)"
```

---

### Task 3: Send, answer and the approval modes

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py:1195-1223` (and new methods after `add_revision`)
- Test: `backend/tests/orders/test_revisions.py`

**Interfaces:**
- Consumes: Task 2's `_revise`, `RevisionAnswerInput`; `store.subject_attachments` (`store.py:1712`) with `revision_subject`; `store.stamp_revision`.
- Produces:
  - `Lifecycle._newest(self, conn, item: OrderItem, revision_id: str) -> Revision`: `revision_not_found` (not_found) when it is not the item's; `revision_not_newest` when a higher number exists.
  - `Lifecycle.send_revision(self, order_id: str, item_id: str, revision_id: str, by: Actor) -> Revision`: refused `revision_answered` (answered, `superseded` included), `revision_empty` (no attachment on its subject), `revision_not_quoted` (`together` with no quote naming it), `order_closed`/`order_cancelled`; already sent and unanswered returns it, writing nothing; else stamps `sent_at`, audit `revision.sent`, tells the item.
  - `Lifecycle.answer_revision(self, order_id: str, item_id: str, revision_id: str, body: RevisionAnswerInput, by: Actor) -> Revision`: refused `revision_not_newest`, `revision_not_sent`, `revision_answered`, 422 `note` for `changes_requested` with a blank note. `accepted` on a `together` item also accepts the newest quote naming the revision in the same transaction (refused `revision_not_quoted` without one, `order_on_hold` on hold, `quote_stale` when its quantity is not the item's), through `_accept_quote`. Audit `revision.answered` (`answer`, `note`).
  - `Lifecycle._accept_quote(self, conn, order: Order, item: OrderItem, quote: Quote, by: Actor) -> tuple[Order, OrderItem, list[str]]`: the body of `accept_quote` (`lifecycle.py:1206-1221`), shared by both.
  - `accept_quote` adds two refusals: `quote_stale` when `quote.revision_id` is set and is not `item.current_revision_id` (a NULL `revision_id` reads as current, Review Focus 3); `revision_not_accepted` on a `design_first` item whose quote's revision (or, for NULL, its current one) has `answer != "accepted"`. A custom item is exempt from both.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_revision_is_sent_with_an_attachment(shop: Shop) -> None:
    with pytest.raises(LifecycleError) as empty:
        shop.lifecycle.send_revision(order.id, item.id, rev.id, BY)
    assert empty.value.slug == "revision_empty"
    attach_render(shop, order, item, rev)          # helper: lifecycle.add_attachment(..., revision_id=rev.id, role="render")
    assert shop.lifecycle.send_revision(order.id, item.id, rev.id, BY).sent_at is not None
    assert "revision.sent" in shop.kinds(order)

def test_sending_again_writes_nothing(shop: Shop) -> None:
    assert again.sent_at == sent.sent_at and shop.kinds(order).count("revision.sent") == 1

def test_only_the_newest_revision_is_sent_or_answered(shop: Shop) -> None:
    # revision 1 sent, then revision 2 added
    for call in (send, answer):
        with pytest.raises(LifecycleError) as refused: call(first.id)
        assert refused.value.slug == "revision_not_newest"

def test_an_unsent_revision_is_not_answered(shop: Shop) -> None:
    assert refused.value.slug == "revision_not_sent"

def test_an_answered_revision_is_not_answered_or_sent_again(shop: Shop) -> None:
    assert {send_slug, answer_slug} == {"revision_answered"}

def test_changes_requested_needs_a_note(shop: Shop) -> None:
    assert refused.value.unprocessable and refused.value.slug == "note"

def test_together_accepts_the_revision_and_its_quote(shop: Shop) -> None:
    # approval together, manual quote recorded after revision 1, revision sent with a render
    shop.lifecycle.answer_revision(order.id, item.id, rev.id, RevisionAnswerInput(answer="accepted", note="Ada said yes"), BY)
    assert shop.rows.revision(rev.id).answer == "accepted"
    assert shop.get_item(item).status == "approved" and shop.get(order).status == "quoted"

def test_together_sends_a_revision_only_with_a_quote(shop: Shop) -> None:
    assert refused.value.slug == "revision_not_quoted"

def test_design_first_accepts_the_design_then_the_price(shop: Shop) -> None:
    # approval design_first; revision sent; quote recorded
    with pytest.raises(LifecycleError) as early:
        shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY)
    assert early.value.slug == "revision_not_accepted"
    shop.lifecycle.answer_revision(..., RevisionAnswerInput(answer="accepted"), BY)
    assert shop.get_item(item).status == "quoted"      # the design alone approves nothing
    assert shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY).status == "approved"

def test_a_quote_of_an_earlier_revision_is_stale(shop: Shop) -> None:
    # quote recorded on revision 1, then add_revision
    assert refused.value.slug == "quote_stale"

def test_a_hold_refuses_a_together_accept(shop: Shop) -> None:
    assert refused.value.slug == "order_on_hold" and shop.rows.revision(rev.id).answer is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_revisions.py -v -k "sent or answer or together or design_first or stale or hold"`
Expected: FAIL (`send_revision` not defined). `attach_render` needs Task 6's `revision_id` on `add_attachment`; until then the helper inserts through `rows.insert_attachment` with `subject=revision_subject(rev.id), subject_item_id=item.id` inside `rows.transaction()`.

- [ ] **Step 3: Implement** the methods in Interfaces. `record_manual_quote` (`lifecycle.py:1094`) passes `revision_id=item.current_revision_id` to `insert_quote`; `_custom_price` (`lifecycle.py:859`) passes None.

- [ ] **Step 4: Run the orders suite**

Run: `cd backend && uv run pytest tests/orders -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders backend/tests/orders
git commit -m "feat(orders): send a revision and record the customer's answer, in both approval modes (#5280)"
```

---

### Task 4: Request items, and quotes that name their revision

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py:820-857,1094-1193`
- Modify: `backend/scadbuddy/orders/quote.py:118-129,583-650`
- Modify: `backend/scadbuddy/orders/printing.py:164`
- Test: `backend/tests/orders/test_revisions.py`, `backend/tests/orders/test_quote.py`

**Interfaces:**
- Consumes: Task 2 (`_revise`, `add_revision`), Task 3 (`_accept_quote`).
- Produces:
  - `Lifecycle.add_request_item(self, order_id: str, by: Actor, *, title: str, quantity: int, notes: str = "", sku: str | None = None) -> OrderItem`: kind `request`, no reference, `approval` `design_first`, audit `item.added` with `item_kind="request"`; refused as `add_item` is (`item_locked`, ended).
  - `quote.py` `reference_of(item: OrderItem) -> Reference` (`quote.py:118`): a request with no reference raises 409 `item_has_no_design`; a custom item stays `item_custom`. `quote_check` and `print_check` already call it (`quote.py:196`, `printing.py:164`).
  - `record_manual_quote` refuses a request with no design: `item_has_no_design`.
  - `record_slice_quote(self, order_id, item_id, priced, plates, *, sliced_sha256, sliced_blob_key, by, revision_id: str, quote_id=None) -> Quote` (`lifecycle.py:1154`): refused `revision_not_newest` when `revision_id != item.current_revision_id`; records `revision_id` on the quote.
  - `quote_run` (`quote.py:583`) reads `item.current_revision_id` after `ensure_output` and passes it; on `revision_not_newest` it drops its hold (`_drop`, `quote.py:669`) and answers 409 through `refused`.

- [ ] **Step 1: Write the failing tests**

```python
# test_revisions.py
def test_a_request_has_no_design_until_its_first_revision(shop: Shop) -> None:
    item = shop.lifecycle.add_request_item(order.id, BY, title="A dragon", quantity=1, notes="green, 10 cm")
    assert item.kind == "request" and item.reference is None and item.approval == "design_first"
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.record_manual_quote(order.id, item.id, ManualQuoteInput(unit_price_minor=500), BY)
    assert refused.value.slug == "item_has_no_design"
    with pytest.raises(LifecycleError) as bare:
        shop.lifecycle.add_revision(order.id, item.id, BY)
    assert bare.value.slug == "item_has_no_design"
    rev = shop.lifecycle.add_revision(order.id, item.id, BY, resolved())
    item = shop.get_item(item)
    assert rev.number == 1 and item.kind == "request" and item.reference == rev.reference

def test_a_manual_quote_names_the_current_revision(shop: Shop) -> None:
    assert quote.revision_id == item.current_revision_id

def test_a_quote_from_the_previous_build_reads_as_current(shop: Shop, pool: PgPool) -> None:
    # raw UPDATE order_item_quotes SET revision_id = NULL; accept it
    assert shop.lifecycle.accept_quote(order.id, item.id, quote.id, BY).status == "approved"

# test_quote.py
def test_the_check_refuses_a_request_with_no_design(shop: Shop) -> None:
    with pytest.raises(ApiError) as refused: quote_check(...)
    assert refused.value.type == PROBLEMS + "item_has_no_design"

def test_a_slice_quote_names_its_revision(shop: Shop) -> None:
    # record_slice_quote(..., revision_id=item.current_revision_id)
    assert quote.revision_id == item.current_revision_id

def test_a_slice_quote_of_a_superseded_revision_is_refused(shop: Shop) -> None:
    # revision id read, then add_revision, then record_slice_quote with the old id
    assert refused.value.slug == "revision_not_newest" and shop.rows.quotes(item.id) == []

async def test_the_runs_new_filaments_are_a_revision_the_quote_names(...) -> None:
    # quote_run with fake renders/slicer (as the module's existing run tests do), filaments differing from revision 1
    assert [r.number for r in rows.revisions(item.id)] == [1, 2]
    assert quote.revision_id == rows.revisions(item.id)[1].id
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_revisions.py tests/orders/test_quote.py -v`
Expected: FAIL (`add_request_item` not defined).

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the orders suite**

Run: `cd backend && uv run pytest tests/orders -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders backend/tests/orders
git commit -m "feat(orders): request items and quotes that name their revision (#5280)"
```

---

### Task 5: Test prints are attempts

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py:1324-1376,1481-1580,1651-1720,1722-1809`
- Modify: `backend/scadbuddy/orders/printing.py:131-201,285-352`
- Modify: `backend/scadbuddy/orders/operations.py:271-295,393-428,487-551`
- Modify: `backend/scadbuddy/api/orders.py:1111-1157` (a new route beside it)
- Test: `backend/tests/orders/test_printing.py`, `backend/tests/api/test_order_print.py`

**Interfaces:**
- Consumes: Task 1 (`purpose`, `revision_id`, `fulfilments(purpose=)`), Task 4 (`quote.revision_id`).
- Produces:
  - `claim_print(self, order_id, item_id, by, *, request_id, sliced_sha256, sliced_blob_key, project_id=None, revision_id: str | None = None) -> Fulfilment` (`lifecycle.py:1481`): with `revision_id` the claim is a test attempt: refused when ended or on hold, `item_not_open` for a settled item, `item_has_no_design`, `revision_not_newest` when `revision_id` is not current; NOT refused for an order before `accepted` or an item not `approved`; `copies` 1; the item's status unchanged; audit `print.claimed` with `purpose="test"`. The retry branch (`lifecycle.py:1511-1526`) reads the claim's own purpose.
  - `follow_print` (`lifecycle.py:1722`), `release_print`/`_release_claim` (`lifecycle.py:1651,1697`): for a `test` attempt, the attempt settles and the item never moves (no `update_item` status write, no `forget_projects_seen` change of behaviour).
  - `update_fulfilment` (`lifecycle.py:1324`) of a `test` attempt: allowed before `accepted` (skip `_in_production`'s status check, keep ended and hold), never moves the item. `_attempt_of` (`lifecycle.py:1314`) takes `purpose: FulfilmentPurpose | None = "production"`; `accept_fulfilment` and `unfulfil_item` keep the default, so a test attempt is `fulfilment_not_found` there.
  - `printing.py` `test_print_check(store, outputs, blobs, order_id, item_id, revision_id, schema_of=None) -> dict[str, Any]`: refusals ended, `order_on_hold`, `item_custom`/`item_has_no_design` (`reference_of`), `item_not_open`, `revision_not_found`, `revision_not_newest`, `requote_needed` (no `slice` quote naming the revision, its file gone or another digest, the output not the reference's render); returns the same keys as `print_check` with `"copies": 1`.
  - `operations.py` `item_print_check`/`item_print_run` read `request.get("revision_id")`: set, they call `test_print_check` and pass `revision_id` to `print_item` → `claim_print`.
  - `printing.py` `print_item(..., revision_id: str | None = None)` passes it through.
  - Route `POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/test-print`, body `PrintRunRequest`, `response_model=Fulfilment`, `responses=OPERATION_RESPONSES`, kind `ORDER_ITEM_PRINT`, subject `subject_of(item_id)`, `request_id` `print_request_id(item_id, idempotency_key)`, request carries `"revision_id"`.
  - `delete_item` (`lifecycle.py:1043`) refuses an item with any attempt (`store.fulfilments(item.id, purpose=None)`) with `item_not_draft`.

- [ ] **Step 1: Write the failing tests** in `backend/tests/orders/test_printing.py`

```python
def test_a_test_print_needs_no_accepted_order(shop: Shop, pool: PgPool) -> None:
    claim = shop.lifecycle.claim_print(order.id, item.id, BY, request_id="r1", sliced_sha256=SHA, sliced_blob_key=KEY, revision_id=item.current_revision_id)
    assert claim.purpose == "test" and claim.revision_id == item.current_revision_id and claim.copies == 1
    assert shop.get_item(item).status == before.status and shop.get(order).status == "quoted"

def test_a_test_print_settles_without_moving_the_item(shop: Shop, pool: PgPool) -> None:
    # attach a run, record its outcome "finished", follow_print
    assert followed.outcome == "succeeded" and shop.get_item(item).status == "approved"

def test_a_failed_test_print_does_not_fail_the_item(shop: Shop, pool: PgPool) -> None:
    assert shop.get_item(item).status == "approved"

def test_test_attempts_count_for_nothing(shop: Shop, pool: PgPool) -> None:
    # order accepted; one test attempt succeeded with quantity 1; item quantity 1
    assert shop.get(order).status == "accepted"           # derive's attempted leaves it out
    assert shop.lifecycle.claim_print(...production...).copies == 1
    with pytest.raises(LifecycleError) as refused:
        shop.lifecycle.accept_fulfilment(order.id, item.id, test.id, BY)
    assert refused.value.slug == "fulfilment_not_found"

def test_a_test_print_is_of_the_current_revision(shop: Shop, pool: PgPool) -> None:
    assert refused.value.slug == "revision_not_newest"

def test_a_draft_item_with_a_test_print_is_cancelled_not_deleted(shop: Shop, pool: PgPool) -> None:
    # test claim, then add_revision (item draft), then delete_item
    assert refused.value.slug == "item_not_draft" and shop.rows.fulfilments(item.id, purpose=None)
```

In `backend/tests/api/test_order_print.py`:

```python
def test_a_test_print_runs_from_a_slice_quote_of_its_revision(client: TestClient) -> None:
    # quoted (slice) but not accepted order; POST …/revisions/{rev}/test-print with an Idempotency-Key
    assert answer.status_code == 200 and answer.json()["purpose"] == "test"

def test_a_test_print_without_a_slice_quote_is_requote_needed(client: TestClient) -> None:
    assert answer.status_code == 409 and answer.json()["type"].endswith("/requote_needed")
```

Follow the module's existing fakes for the print run.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_printing.py -v -k test_print` and `uv run pytest tests/api/test_order_print.py -v -k test_print`
Expected: FAIL (`claim_print() got an unexpected keyword argument 'revision_id'`).

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the print and orders suites**

Run: `cd backend && uv run pytest tests/orders tests/api/test_order_print.py tests/test_orders_bambuddy_seam.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/orders backend/scadbuddy/api/orders.py backend/tests
git commit -m "feat(orders): test prints are attempts of a revision that count for nothing (#5280)"
```

---

### Task 6: Attachments on a revision

**Files:**
- Modify: `backend/scadbuddy/orders/lifecycle.py:1043-1074,1949-2012`
- Modify: `backend/scadbuddy/orders/operations.py:553-621`
- Modify: `backend/scadbuddy/api/order_attachments.py:95-113,142-212` (a new route after `add_attempt_attachment`, line 287)
- Test: `backend/tests/orders/test_attachments.py`, `backend/tests/api/test_order_attachments.py`

**Interfaces:**
- Consumes: Task 1 (`revision_subject`, `insert_attachment(subject_item_id=, fulfilment_id=)`), Task 5 (test attempts).
- Produces:
  - `add_attachment(self, order_id, item_id, by, *, attachment_id, kind, role, filename, media_type, size_bytes, sha256, blob_key, caption, fulfilment_id=None, revision_id: str | None = None) -> Attachment` (`lifecycle.py:1949`): with `revision_id`, the subject is `revision_subject(revision_id)`, refused `revision_not_found` unless it is a revision of `item_id` of this order; a `fulfilment_id` beside it must be a `test` attempt of that item whose `revision_id` is this revision (else `fulfilment_not_found`). The audit row carries `revision_id`.
  - `delete_item` also removes every attachment on its revisions' subjects (each with an `attachment.removed` audit row), before `delete_draft_item`.
  - `attachment_check`/`attachment_run` (`operations.py:553,576`) read and pass `request.get("revision_id")`.
  - `_require(orders, order_id, item_id, fulfilment_id=None, revision_id: str | None = None)` and `_upload(..., revision_id: str | None = None)`: the multipart takes an optional `fulfilment_id` field (at most 100 bytes) on the revision route only.
  - Route `POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/attachments` (`_UPLOAD_DOC`), description says roles `render`, `test_print`, `reference`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/orders/test_attachments.py
def test_a_file_on_a_revision_is_listed_under_its_item(shop, ...) -> None:
    att = shop.lifecycle.add_attachment(order.id, item.id, BY, ..., role="render", revision_id=rev.id)
    assert att.subject == f"order_item_revision:{rev.id}" and att.item_id == item.id and att.revision_id == rev.id

def test_another_items_revision_is_refused(shop, ...) -> None:
    assert refused.value.slug == "revision_not_found" and refused.value.not_found

def test_a_test_print_photo_names_its_attempt(shop, ...) -> None:
    assert att.fulfilment_id == test.id and att.revision_id == rev.id

def test_a_production_attempt_is_not_a_revisions_print(shop, ...) -> None:
    assert refused.value.slug == "fulfilment_not_found"

def test_deleting_a_draft_item_removes_its_revisions_files_and_holds(shop, pool) -> None:
    assert shop.rows.attachments(order.id) == [] and not holders_of(pool, att.id)

# tests/api/test_order_attachments.py
def test_a_file_on_a_revision(client: TestClient) -> None:
    answer = attach(client, f"/api/v1/orders/{o}/items/{i}/revisions/{r}/attachments", png(), role="test_print")
    assert answer.status_code == 200 and answer.json()["revision_id"] == r and answer.json()["role"] == "test_print"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/orders/test_attachments.py tests/api/test_order_attachments.py -v -k revision`
Expected: FAIL.

- [ ] **Step 3: Implement** the Interfaces. Replace Task 3's `attach_render` helper body with `lifecycle.add_attachment(..., revision_id=...)`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest tests/orders tests/api/test_order_attachments.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(orders): renders and test-print photos attached to a revision (#5280)"
```

---

### Task 7: Photos onto the model

**Files:**
- Modify: `backend/scadbuddy/library/media.py:73-96`
- Modify: `backend/scadbuddy/library/media_store.py:47-96`
- Modify: `backend/scadbuddy/library/catalogue.py:1736-1776` (and `_overlay_add`)
- Modify: `backend/scadbuddy/library/media_operations.py:85-115`
- Modify: `backend/scadbuddy/api/order_attachments.py` (a new route)
- Test: `backend/tests/test_media_store.py`, `backend/tests/api/test_order_attachments.py`

**Interfaces:**
- Consumes: Task 6 (revision attachments); `orders/attachments.py:168` `blob_path(blobs, key)`; `OrderStore.attachment` (`store.py:1692`, returns `(Attachment, blob_key)`); `library/media.py:123` `sniff_kind(head) -> (kind, extension, content_type) | None`; `MAX_IMAGE_BYTES` (`media.py:67`); `ClaimStore.hold_file` (`operations/claims.py:86`); the `model_media_upload` kind (`media_operations.py:186`).
- Produces:
  - `MediaItem` gains `source_commit: str | None = None`, `source_revision_id: str | None = None` (so `MediaView` and `ModelRecord` carry them).
  - `PostgresMediaStore.items`/`items_for`/`replace` read and write both columns.
  - `Catalogue.add_media(self, slug, upload, caption="", poster=None, *, source_commit: str | None = None, source_revision_id: str | None = None) -> ModelRecord`.
  - The `model_media_upload` request takes optional `"source_commit"`, `"source_revision_id"`; `_add` passes them on.
  - Route `POST /api/v1/orders/{order_id}/attachments/{attachment_id}/to-model`, `response_model=ModelRecord`, `responses=OPERATION_RESPONSES`, `summary="Add a test-print photo to the model's photos"`. It refuses 404 for an unknown attachment, 409 `revision_not_accepted` unless the attachment is `test_print` on a revision whose `answer == "accepted"` (422 `role` for another role or subject), 422 for a type `sniff_kind` does not take, 413 for an image past `MAX_IMAGE_BYTES`, 409 `template_deleted` when the revision's slug is gone. It copies the blob's file into `paths.cache`, holds it with `ClaimStore.hold_file(copy, attachment.sha256)`, and runs `model_media_upload` with subject the slug, `caption` the attachment's, `source_commit` the revision's commit, `source_revision_id` its id. The order's attachment stays.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_media_store.py
def test_a_media_items_source_survives_a_reorder(...) -> None:
    store.replace("demo", [MediaItem(id="a", file="a.png", kind="image", source_commit="b" * 40, source_revision_id="rev-1"), other])
    store.replace("demo", list(reversed(store.items("demo"))))
    assert next(i for i in store.items("demo") if i.id == "a").source_revision_id == "rev-1"

# tests/api/test_order_attachments.py
def test_an_accepted_revisions_print_photo_joins_the_models_photos(client: TestClient) -> None:
    record = client.post(f"/api/v1/orders/{o}/attachments/{a}/to-model", headers={"Idempotency-Key": "k"}).json()
    added = record["media"][-1]
    assert added["source_commit"] == commit and added["source_revision_id"] == r
    assert client.get(f"/api/v1/orders/{o}/attachments/{a}/content").status_code == 200   # the order's copy stays

def test_a_photo_of_a_revision_not_accepted_stays_on_the_order(client: TestClient) -> None:
    assert answer.status_code == 409 and answer.json()["type"].endswith("/revision_not_accepted")
```

Check the `ModelRecord` media field name in `library/catalogue.py` before writing the assertion.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/test_media_store.py tests/api/test_order_attachments.py -v -k "source or model"`
Expected: FAIL.

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the media and attachment suites**

Run: `cd backend && uv run pytest tests/test_media_store.py tests/api/test_media.py tests/api/test_order_attachments.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy backend/tests
git commit -m "feat(orders): add an accepted revision's print photo to the model's photos (#5280)"
```

---

### Task 8: The revision routes and the order read

**Files:**
- Modify: `backend/scadbuddy/api/orders.py:165-207,210-232,254-256,654-687,886-998`
- Test: `backend/tests/api/test_order_revisions.py` (create)

**Interfaces:**
- Consumes: Tasks 2-4 (`add_revision`, `send_revision`, `answer_revision`, `add_request_item`, `update_item(approval=)`); `_resolved` (`api/orders.py:851`); `_run` (`api/orders.py:307`); `RevisionInput`, `RevisionAnswerInput` (`orders/revisions.py`).
- Produces:
  - `ItemCreate.kind` accepts `"request"`: it takes a `title` (required, stripped), `quantity`, `notes`, `sku`, and refuses `slug`, `commit`, `inputs`, `output_id`, `unit_price_minor`, `requires_printing`, `accept` (422) in `_shape` (`api/orders.py:185`); `add_item` calls `add_request_item`.
  - `ItemPatch.approval: Approval | None = None`, passed to `update_item`.
  - `ItemDetail` gains `revisions: list[Revision]`; `get_order` fills it from `store.revisions(item.id)` and lists `fulfilments` with `purpose=None`.
  - `class RevisionCreate(RevisionInput)`: `reference: ReferenceBody | None = None`, `output_id: str | None = None`.
  - `POST /api/v1/orders/{order_id}/items/{item_id}/revisions` → 201 `Revision`, `summary="Add a design revision"`; resolves the reference as `patch_item` does (`api/orders.py:969-979`) when `reference` or `output_id` is given.
  - `POST …/revisions/{revision_id}/send` → `Revision`, `summary="Send a revision to the customer"`, description: phase 1 stamps `sent_at` only.
  - `POST …/revisions/{revision_id}/answer` → `Revision`, body `RevisionAnswerInput`, `summary="Record the customer's answer to a revision"`.
  - Each route documents its refusal slugs in its description, as `reopen_item` does (`api/orders.py:1015-1024`).

- [ ] **Step 1: Write the failing tests** in `backend/tests/api/test_order_revisions.py` (the `client` fixture from `tests/api/conftest.py`, the helpers of `tests/api/test_orders.py`)

```python
def test_a_request_item_takes_its_design_as_revision_one(client: TestClient) -> None:
    item = client.post(f"/api/v1/orders/{o}/items", json={"kind": "request", "title": "A dragon", "quantity": 1, "notes": "green"}).json()
    assert item["kind"] == "request" and item["reference"] is None and item["approval"] == "design_first"
    rev = client.post(f"/api/v1/orders/{o}/items/{item['id']}/revisions", json={"reference": {"slug": slug, "commit": commit, "inputs": inputs}, "note": "first go"})
    assert rev.status_code == 201 and rev.json()["number"] == 1

def test_the_order_read_lists_each_items_revisions(client: TestClient) -> None:
    detail = client.get(f"/api/v1/orders/{o}").json()
    assert [r["number"] for r in detail["items"][0]["revisions"]] == [1]

def test_send_and_answer_are_refused_in_order(client: TestClient) -> None:
    assert client.post(f"{rev_url}/answer", json={"answer": "accepted"}).json()["type"].endswith("/revision_not_sent")
    assert client.post(f"{rev_url}/send").json()["type"].endswith("/revision_empty")

def test_a_request_item_with_no_design_is_not_quoted(client: TestClient) -> None:
    answer = client.post(f"/api/v1/orders/{o}/items/{i}/quotes", json={"unit_price_minor": 500})
    assert answer.status_code == 409 and answer.json()["type"].endswith("/item_has_no_design")

def test_approval_is_patched(client: TestClient) -> None:
    assert client.patch(item_url, json={"approval": "design_first"}).json()["approval"] == "design_first"

def test_a_request_names_no_source(client: TestClient) -> None:
    assert client.post(f"/api/v1/orders/{o}/items", json={"kind": "request", "title": "x", "quantity": 1, "slug": "demo"}).status_code == 422
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest tests/api/test_order_revisions.py -v`
Expected: FAIL (404 on the revisions route).

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the API orders suites and the whole backend checks**

Run: `cd backend && uv run pytest tests/api -k order -v && uv run ruff check . && uv run ruff format --check . && uv run mypy`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/api/orders.py backend/tests/api/test_order_revisions.py
git commit -m "feat(orders): routes for revisions, request items and the approval mode (#5280)"
```

---

### Task 9: Agent tools

**Files:**
- Modify: `agent/src/tools/orders.ts:286-297,391-480,614-652,832-890`
- Modify: `agent/src/tools/coverage.ts` (`NOT_A_TOOL`)
- Test: `agent/test/orders.test.ts`

**Interfaces:**
- Consumes: Task 5-8's routes through the generated `src/api/schema.d.ts` (`pnpm gen:api`).
- Produces:
  - `order_item_revision_add` (risk `write`): input `{order_id, item_id, reference?: reference, output_id?: ID, filaments?: Record<slot, ID | null>, note?: string(max 4000)}`, route `POST /api/v1/orders/{order_id}/items/{item_id}/revisions`; description says a request item's first revision gives it its design, and a new revision supersedes a sent, unanswered one and drops the item's quotes.
  - `order_item_revision_send` (risk `outward`, `summarize`: `Send revision ${revision_id} of item ${item_id} (order ${order_id}) to the customer`): route `POST …/revisions/{revision_id}/send`.
  - `order_get` description names `revisions` and test attempts (`purpose`).
  - `order_item_add` takes `kind: 'request'` with `title` and `notes`; `order_item_update` takes `approval`.
  - `order_item_print` takes `test_revision_id?: ID`: set, it posts `POST …/revisions/{test_revision_id}/test-print` (both routes in `routes`).
  - `order_attach` takes `revision_id?: ID` and `fulfilment_id` beside it, posting to `…/revisions/{revision_id}/attachments`; `attachmentRole` gains `render`, `test_print`.
  - `NOT_A_TOOL` entries: `POST /api/v1/orders/{order_id}/items/{item_id}/revisions/{revision_id}/answer` (reason: the customer's answer, which the shop records by hand in phase 1 and the customer gives on the storefront in phase 4, spec 2026-11-02 §4.3; an agent never answers for the customer) and `POST /api/v1/orders/{order_id}/attachments/{attachment_id}/to-model` (reason: a person's choice in the Revisions panel, spec §4.6; add_model_media adds the agent's own photos).

- [ ] **Step 1: Write the failing tests** in `agent/test/orders.test.ts`, following its existing backend-fake pattern

```ts
test('order_item_revision_add posts the reference and note', async () => {
  expect(calls[0]).toEqual({ method: 'POST', path: '/api/v1/orders/o1/items/i1/revisions', body: { reference: { slug: 'demo', commit: C, inputs: {} }, note: 'v2' } })
})
test('order_item_revision_send is outward', () => {
  expect(tool('order_item_revision_send').risk).toBe('outward')
  expect(tool('order_item_revision_add').risk).toBe('write')
})
test('order_item_print with test_revision_id posts the test print', async () => {
  expect(calls[0].path).toBe('/api/v1/orders/o1/items/i1/revisions/r1/test-print')
})
test('order_attach with revision_id posts to the revision', async () => {
  expect(calls[0].path).toBe('/api/v1/orders/o1/items/i1/revisions/r1/attachments')
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm test -- orders`
Expected: FAIL (no tool `order_item_revision_add`), and `test/coverage.test.ts` FAIL naming the new operations.

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the agent checks**

Run: `cd agent && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS, `coverage.test.ts` included.

- [ ] **Step 5: Commit**

```bash
git add agent/src/tools agent/test/orders.test.ts
git commit -m "feat(agent): order_item_revision_add and order_item_revision_send (#5280)"
```

---

### Task 10: Frontend client and msw mocks

**Files:**
- Modify: `frontend/src/api/types.ts:359-373`, `frontend/src/api/client.ts:2180-2200`
- Modify: `frontend/src/mocks/features/orders/state.ts:60-90,338-354`, `frontend/src/mocks/features/orders/orders.ts:64-66,125-157,550-687,879-937`
- Create: `frontend/src/mocks/features/orders/revisions.ts`
- Test: `frontend/src/mocks/features/orders/revisions.test.ts` (create)

**Interfaces:**
- Consumes: the generated schema (`pnpm gen:api`).
- Produces:
  - `types.ts`: `Revision = Schemas['Revision']`, `RevisionAnswer = Revision['answer']`, `Approval = OrderItem['approval']`.
  - `client.ts` `api` gains `addOrderItemRevision(orderId, itemId, body: Schemas['RevisionCreate']) => Promise<Revision>`, `sendOrderItemRevision(orderId, itemId, revisionId) => Promise<Revision>`, `answerOrderItemRevision(orderId, itemId, revisionId, body: Schemas['RevisionAnswerInput']) => Promise<Revision>`, `addRevisionAttachment(orderId, itemId, revisionId, file: File, opts: { role, caption?, fulfilmentId? })` (as `addOrderAttachment`, a command), `testPrintRevision(orderId, itemId, revisionId, body: PrintRunRequest)` (a command), `revisionPhotoToModel(orderId, attachmentId)` (a command).
  - `state.ts` gains `revisions: Revision[]` (fixtures: one revision 1 per fixture model item; quotes get `revision_id`; items `current_revision_id`, `approval`); `export const revisionsOf = (itemId: string) => …` sorted by number.
  - `orders.ts` `detail()` adds `revisions: revisionsOf(i.id)`; the items `POST` takes `kind: 'request'`; item `PATCH` takes `approval`; a reference or filament change pushes a revision (`pushRevision(order, item, reference, colours, note)` exported from `revisions.ts`).
  - `revisions.ts` exports `handlers` for the five revision routes and `to-model`, mirroring Task 2-7's refusals with `Refused` slugs (`revision_not_newest`, `revision_not_sent`, `revision_answered`, `revision_empty`, `revision_not_quoted`, `revision_not_accepted`, `item_has_no_design`), and `reset`.

- [ ] **Step 1: Write the failing tests** in `revisions.test.ts` (as `orders.test.ts` drives the mocks with `fetch`)

```ts
test('a new revision supersedes a sent one', async () => {
  // POST revisions, attach, send, POST revisions again
  expect(detail.items[0].revisions.map((r) => [r.number, r.answer])).toEqual([[1, null], [2, 'superseded'], [3, null]])
})
test('only the newest revision is answered', async () => {
  expect(res.status).toBe(409)
  expect((await res.json()).type).toBe(`${PROBLEMS}revision_not_newest`)
})
test('together accepts the revision and its quote', async () => {
  expect(item.status).toBe('approved')
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/mocks/features/orders/revisions.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** the Interfaces.

- [ ] **Step 4: Run the frontend checks**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api frontend/src/mocks
git commit -m "feat(frontend): revisions in the API client and the orders mocks (#5280)"
```

---

### Task 11: The Revisions panel

**Files:**
- Create: `frontend/src/components/orders/RevisionsPanel.tsx`, `frontend/src/components/orders/RevisionsPanel.test.tsx`
- Modify: `frontend/src/pages/OrderPage.tsx:441-685` (`ItemDrawer`), `frontend/src/components/orders/PrintItem.tsx:30`, `frontend/src/components/orders/AddItemDialog.tsx:34-64`
- Test: `frontend/e2e/orders.spec.ts`

**Interfaces:**
- Consumes: Task 10's client and mocks; `OrderAttachments`' `FilePick` pattern (`OrderAttachments.tsx:192`); `refusalText` (`components/orders/refusal.ts`); `AddItemDialog`'s model picker (`AddItemDialog.tsx:34` `itemBody`, `:51` `pinnedOf`).
- Produces:
  - `RevisionsPanel({ order, item, attachments, onChanged }: { order: Order; item: OrderItemDetail; attachments: OrderAttachment[]; onChanged: () => void })`, shown in `ItemDrawer` for every item but a `custom` one, above `QuoteEditor`. It lists the revisions newest first: number, note (as text), created time, sent time or "Not sent", the answer and its note, and each revision's attachments (`attachments.filter((a) => a.revision_id === r.id)`, thumbnails as `OrderAttachments` draws them). Controls: an approval select (`Design first` / `Design and price together`, `updateOrderItem({ approval })`, disabled once the order is past `quoted`); "New revision" (a note, an optional new commit and inputs through the `AddItemDialog` picker, or "same design"); on the newest revision, "Add render or photo" (role `render` or `test_print`), "Send to customer", and once sent "Record answer" (Accepted / Changes requested with a required note, saying the shop records it for the customer); "Test print" on the newest revision when it has a slice quote (`PrintItem` with `testRevision`); "Add to model photos" on a `test_print` image of an accepted revision.
  - `PrintItem({ order, item, onChanged, testRevision?: Revision })`: with `testRevision`, offered before `accepted`, labelled "Test print", posting `testPrintRevision`.
  - `ItemDrawer` passes `FulfilmentForm` and `PrintItem` only the `purpose === 'production'` attempts (the order read now lists test attempts too, Task 8); the panel lists each revision's test attempts with their outcome.
  - `AddItemDialog` offers "Design request" beside a model and a custom item (`kind: 'request'`, title and the customer's text).

- [ ] **Step 1: Write the failing component tests** in `RevisionsPanel.test.tsx`

```tsx
test('lists revisions newest first with their answers', async () => {
  expect(screen.getAllByTestId('revision').map((r) => r.dataset.number)).toEqual(['2', '1'])
  expect(within(rows[1]).getByText('Superseded')).toBeInTheDocument()
})
test('a note is shown as text', async () => {
  // note '<b>bold</b>'
  expect(screen.getByText('<b>bold</b>')).toBeInTheDocument()
})
test('send is refused without an attachment and says why', async () => {
  await user.click(screen.getByRole('button', { name: 'Send to customer' }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/attachment/i)
})
test('records the customer asking for changes', async () => {
  // sent revision; Record answer → Changes requested, note "bigger"
  expect(await screen.findByText('Changes requested')).toBeInTheDocument()
})
test('a custom item has no revisions panel', () => {
  expect(screen.queryByTestId('revisions-panel')).toBeNull()
})
test('add to model photos is offered only on an accepted revision', () => {
  expect(screen.queryByRole('button', { name: 'Add to model photos' })).toBeNull()
})
```

Add to `frontend/e2e/orders.spec.ts`:

```ts
test('a design request through a revision to approved (#5280)', async ({ page }) => {
  // add a design request; New revision from a model; add a render; send; record Accepted; quote by hand; accept quote
  // the item's status badge, as the file's other tests read an item's status
  await expect(itemStatus(page, 'A dragon')).toHaveText(/approved/i)
})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/orders/RevisionsPanel.test.tsx`
Expected: FAIL (no module `RevisionsPanel`).

- [ ] **Step 3: Implement** the Interfaces. Copy text is the builder's (spec §12); keep it in the user's words ("Send to customer", "Record the customer's answer").

- [ ] **Step 4: Run the frontend checks and the e2e**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test e2e/orders.spec.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src frontend/e2e
git commit -m "feat(frontend): the Revisions panel on an order's line item (#5280)"
```

---

## Coverage against the spec

| Spec | Task |
|---|---|
| §4.1 one authority, revision 1 on add, next on update | 1, 2 |
| §4.2 `request` kind, CHECK, `item_has_no_design` on quote and print | 1, 4, 5 |
| §4.3 revision columns, `superseded`, attachments subject and roles, sent with one attachment, only the newest, `changes_requested` with a note, quotes name their revision, stale quotes, the run's filaments revision, phase 1 Send | 1, 2, 3, 4, 6 |
| §4.4 test attempts: `purpose`, `revision_id`, no `accepted`, no status, not in `derive`/`accept_fulfilment`/`unfulfil_item`, photos naming the attempt, costing reads all | 1, 5, 6 |
| §4.5 approval mode and both paths to `approved` | 2, 3 |
| §4.6 photos to the model, `source_commit`, `source_revision_id`, the order's copy stays | 1, 7 |
| §4.7 Revisions panel; `order_item_revision_add`, `order_item_revision_send`, revisions in `order_get` | 8, 9, 10, 11 |
| §6.2 `add_revision`, `send_revision`, `answer_revision` (lock, audit row, events) | 2, 3 |
| §6.3 the four slugs | 3, 4 |
