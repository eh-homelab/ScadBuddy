# Inventory phase 1: entities, attachments and links on everything — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Any ScadBuddy row (an order, a spool, a printer, a supplier, …) can carry file and
media attachments and links (support, wiki, manual, purchase, …) through one table pair, one
route family, one UI panel and one set of agent tools.

**Architecture:** A new `scadbuddy/entities/` package holds the entity registry (`kind:ref`
references, one `entities` row per referenced owner), the link store and the generic
attachment store. The existing `order_attachments` table is renamed to `entity_attachments`
and gains `entity_id`, so the order code keeps its behaviour and becomes the first user of
the generic table. Print attachments, whose bytes Bambuddy holds, are listed through a
read adapter. Uploads reuse `orders/attachments.py` (`sniff`, `clean_filename`, `keep`) and
the operations/claims pattern of `order_attachment_add`.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Postgres 18, Temporal (housekeeping),
React + TypeScript (frontend), TypeScript agent tools.

**Spec:** `docs/superpowers/specs/2026-11-21-inventory-and-purchasing-design.md` §6.1–6.3,
§9 phase 1.

## Global Constraints

- Only the CRUD service (API process) touches SQL; agent tools call CRUD routes (CLAUDE.md
  "Database access").
- New migration files only, named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.
- Bytes live in the blob store with a `blob_refs` hold; never base64 in JSON (#2479).
- Phase 1 touches no filament *code* (`filament/*.py`, purchasing, catalog, `bb_sync_*`). Adding
  a panel to a filament page is allowed (Task 6).
- Wire form of a reference: `<kind>:<ref>`, `kind` matching `^[a-z_]+$`, `ref` non-empty, no `:`
  in `kind`.
- Known link kinds: `support`, `wiki`, `manual`, `video`, `datasheet`, `firmware`,
  `purchase`, `other` (open text: any `^[a-z_]{1,32}$` is accepted).
- Known attachment roles: `reference`, `issue`, `finished`, `packed`, `timelapse`, `render`,
  `test_print`, `receipt`, `invoice`, `manual`, `photo`, `label`, `other`.
- Conventional-commit titles; PR body `Fixes #N` (one issue per task group under a new epic).
- Load `frontend-design` before Task 6.

## Review Focus

1. A reference to an owner that does not exist (`spool:<random uuid>`): every write answers
   404 `entity_not_found`, and no `entities` row is created. (Task 3, Task 4 tests.)
2. A malformed reference (`spool`, `:1`, `Spool:1`, `spool:`) answers 422 `bad_entity_ref`;
   an unknown kind (`banana:1`) answers 422 `unknown_entity_kind`. The ref is everything after
   the first `:`, so `library_file:a:b` is valid with ref `a:b`. (Task 1, Task 3 tests.)
3. The same bytes attached to two entities: one blob, two holds; deleting one attachment keeps
   the blob for the other. (Task 3 test.)
4. An owner row deleted later (a spool deleted): the housekeeping sweep removes its entity,
   links and attachments and releases the blob holds; an order's entity is never swept while
   the order exists. (Task 5 test.)
5. A link URL that is not `http`/`https` (`javascript:alert(1)`): 422 `bad_link_url`; the UI
   renders links with `rel="noopener noreferrer"` and `target="_blank"`. (Task 4, Task 6 tests.)

---

### Task 1: Entity registry and migration

**Files:**
- Create: `backend/scadbuddy/migrations/<ts>_entities.sql`
- Create: `backend/scadbuddy/entities/__init__.py`, `backend/scadbuddy/entities/refs.py`,
  `backend/scadbuddy/entities/registry.py`
- Test: `backend/tests/entities/test_refs.py`, `backend/tests/entities/test_registry.py`

Migration (one file):
- `CREATE TABLE entities (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), kind text NOT NULL
  CHECK (kind ~ '^[a-z_]{1,40}$'), ref text NOT NULL CHECK (length(ref) BETWEEN 1 AND 200),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE (kind, ref))`.
- `CREATE TABLE entity_links (id uuid PK DEFAULT gen_random_uuid(), entity_id uuid NOT NULL
  REFERENCES entities (id) ON DELETE CASCADE, url text NOT NULL CHECK (url ~* '^https?://'
  AND length(url) <= 2000), kind text NOT NULL CHECK (kind ~ '^[a-z_]{1,32}$'), title text
  NOT NULL DEFAULT '' CHECK (length(title) <= 200), note text NOT NULL DEFAULT '' CHECK
  (length(note) <= 1000), position integer NOT NULL DEFAULT 0, source text NOT NULL DEFAULT
  'manual', created_at timestamptz NOT NULL DEFAULT clock_timestamp(), created_by jsonb NOT
  NULL)`; index `(entity_id, position)`.
- `ALTER TABLE order_attachments RENAME TO entity_attachments` (and its indexes/constraints
  `order_attachments_*` → `entity_attachments_*`); `ADD COLUMN entity_id uuid REFERENCES
  entities (id) ON DELETE RESTRICT`; insert one `entities` row per distinct `subject`
  (`split_part(subject, ':', 1)`, substring after the first `:`), set `entity_id`, then
  `SET NOT NULL`; `ALTER COLUMN order_id DROP NOT NULL`; drop the subject CHECK and add
  `CHECK (subject ~ '^[a-z_]+:.+$')`; drop the role CHECK and add `CHECK (role ~
  '^[a-z_]{1,32}$')`; index `(entity_id, created_at)`.

**Interfaces:**
- Produces (`entities/refs.py`):
  - `@dataclass(frozen=True) class EntityRef: kind: str; ref: str` with `__str__` →
    `"kind:ref"` and `@classmethod parse(cls, text: str) -> EntityRef` (splits at the first
    `:`; raises `BadEntityRef(text)`).
  - `class BadEntityRef(ValueError)`.
- Produces (`entities/registry.py`):
  - `@dataclass(frozen=True) class EntityKind: name: str; table: str; id_column: str;
    id_cast: str = "text"` — the owner's table, used for existence checks and the sweep.
  - `KINDS: dict[str, EntityKind]` with, in phase 1: `order` (`orders.id`), `order_item`
    (`order_items.id`), `order_item_fulfilment` (`order_item_fulfilments.id`),
    `order_item_revision` (`order_item_revisions.id`), `spool` (`filament_spools.id`, uuid),
    `spool_type` (`filament_spool_types.id`, uuid), `supplier` (`filament_suppliers.id`,
    uuid), `invoice` (`filament_invoices.id`, uuid), `location` (`filament_locations.id`,
    uuid), `printer` (`printers.id`, uuid), `part` (`parts.id`), `customer`
    (`customers.id`). Check each table/column name against its migration before writing it.
  - `class UnknownEntityKind(LookupError)`, `class EntityNotFound(LookupError)`.
  - `class EntityStore(pool)`: `exists(conn, ref: EntityRef) -> bool` (owner row exists);
    `ensure(conn, ref) -> UUID` (raises `UnknownEntityKind` / `EntityNotFound`; `INSERT …
    ON CONFLICT (kind, ref) DO UPDATE SET kind = EXCLUDED.kind RETURNING id`); `find(conn,
    ref) -> UUID | None`; `orphans(conn, limit: int) -> list[tuple[UUID, EntityRef]]`
    (entities whose owner row is gone; one `NOT EXISTS` query per kind).

- [ ] **Step 1: Write failing tests**
  - `test_refs.py`: `EntityRef.parse("spool:abc") == EntityRef("spool", "abc")`;
    `parse("library_file:a:b").ref == "a:b"`; `parse("spool")`, `parse(":x")`,
    `parse("Spool:x")`, `parse("spool:")` raise `BadEntityRef`; `str(EntityRef("order","o1")) == "order:o1"`.
  - `test_registry.py` (Postgres fixture as `tests/orders/test_attachments.py` uses):
    `ensure` on an existing order returns the same UUID twice; `ensure(EntityRef("spool",
    str(uuid4())))` raises `EntityNotFound` and inserts nothing; `ensure(EntityRef("banana",
    "1"))` raises `UnknownEntityKind`; `ensure(EntityRef("spool", "not-a-uuid"))` raises
    `EntityNotFound` (the cast failure is caught, not a 500); after deleting a spool row,
    `orphans()` lists its entity; the migration backfill: an `order_attachments` row written
    before the migration (insert via the pre-migration schema in a migration test, as
    `tests/test_migrations*.py` does) has `entity_id` pointing at `order_item:<id>`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/entities -v` — Expected: FAIL (module missing).
- [ ] **Step 3: Write the migration and the two modules.**
- [ ] **Step 4: Run** `cd backend && uv run pytest tests/entities tests/orders/test_attachments.py -v` — Expected: entities tests PASS; order attachment tests FAIL only on the table name (fixed in Task 2).
- [ ] **Step 5: Commit** `feat(entities): entity references and the entities table`

### Task 2: Orders write the generic table

**Files:**
- Modify: `backend/scadbuddy/orders/store.py` (every `order_attachments` SQL string,
  `insert_attachment` ~l.3020), `backend/scadbuddy/orders/models.py` (role type),
  `backend/scadbuddy/auth/routes.py`, `backend/scadbuddy/api/filament_purchasing.py`,
  `backend/scadbuddy/main.py` (any `order_attachments` table reference — grep; module names
  such as `api/order_attachments.py` stay)
- Test: existing `backend/tests/orders/test_attachments.py`, `test_attachment_blobs.py`,
  `tests/api/test_order_attachments.py`, `tests/api/test_order_revisions.py`,
  `tests/api/test_limits.py` (table name only)

**Interfaces:**
- Consumes: `EntityStore.ensure`, `EntityRef` (Task 1).
- Produces: `OrderStore.insert_attachment(...)` unchanged signature; it now calls
  `EntityStore.ensure(conn, EntityRef.parse(subject))` and writes `entity_id`.
  `orders.models.AttachmentRole` stays the order's own `Literal` (orders still refuse roles
  they do not know); the table no longer enforces it.

- [ ] **Step 1: Add a failing test** in `tests/orders/test_attachments.py`:
  `test_attachment_row_has_its_entity` — after `add_attachment` on an item, the row's
  `entity_id` equals `EntityStore.find(conn, EntityRef("order_item", item.id))`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/orders/test_attachments.py -v` — Expected: FAIL.
- [ ] **Step 3: Rename the table in every SQL string** (`grep -rn order_attachments backend/scadbuddy`) and set `entity_id` in `insert_attachment`. The `attempt_item_id` move at `store.py` ~l.2283 rewrites `subject` to `order_item:<id>`: it must also set `entity_id` to that item's entity (ensure it first).
- [ ] **Step 4: Run** `cd backend && uv run pytest tests/orders tests/api/test_order_attachments.py tests/api/test_order_revisions.py tests/api/test_limits.py tests/storefront -n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `refactor(orders): order attachments live in entity_attachments`

### Task 3: Generic attachments: store, operation and routes

**Files:**
- Create: `backend/scadbuddy/entities/attachments.py`, `backend/scadbuddy/entities/component.py`,
  `backend/scadbuddy/api/entity_attachments.py`
- Modify: `backend/scadbuddy/orders/operations.py` (register the new operation beside
  `ORDER_ATTACHMENT_ADD`, or a new `entities/operations.py` registered the same way —
  follow how `ORDER_ATTACHMENT_ADD` is registered), `backend/scadbuddy/main.py` (router,
  component, `BodySizeGate` upload class as `OrderAttachmentUpload` is at ~l.2533)
- Test: `backend/tests/entities/test_attachments.py`, `backend/tests/api/test_entity_attachments.py`

**Interfaces:**
- Consumes: `EntityStore`, `EntityRef`, `KINDS` (Task 1); `orders.attachments.sniff`,
  `clean_filename`, `keep`, `blob_path`, `BLOB_FILE`; `BlobRefs`; the claims/operation
  helpers used by `api/order_attachments.py` (`run_operation`, `Claimed`, `IdempotencyKey`).
- Produces (`entities/attachments.py`):
  - `ENTITY_ATTACHMENT_HOLDER = "attachment"` — the same holder kind as orders
    (`orders/store.py` `ATTACHMENT_HOLDER`), so one attachment id is one holder however it
    was written. Import it from `orders/store.py`; do not define a second constant.
  - `class EntityAttachment(BaseModel)`: `id, entity: str ("kind:ref"), kind, role,
    filename, media_type, size_bytes, sha256, caption, created_at, created_by, source:
    Literal["store", "bambuddy"]` (`bambuddy` = a print attachment, Task 3 step 3c).
  - `class EntityAttachmentStore(pool, entities: EntityStore, refs: BlobRefs)`:
    `add(conn, ref, *, attachment_id, kind, role, filename, media_type, size_bytes, sha256,
    blob_key, caption, by) -> EntityAttachment` (ensure + insert + `blob_refs` hold, one
    transaction); `list(ref) -> list[EntityAttachment]` (oldest first);
    `get(attachment_id) -> tuple[EntityAttachment, str] | None` (with blob key);
    `update(attachment_id, *, caption: str | None, role: str | None) -> EntityAttachment`;
    `delete(conn, attachment_id) -> None` (drop hold, delete row).
  - `ORDER_KINDS = {"order", "order_item", "order_item_fulfilment", "order_item_revision"}`.
- Routes (`api/entity_attachments.py`, prefix `/entities`, tag `entities`):
  - `POST /entities/{ref}/attachments` — multipart upload, operation `entity_attachment_add`,
    fields `file`, `role` (default `other`), `caption`. For a ref in `ORDER_KINDS` answer 409
    `use_order_route` naming the existing order route (order rules — revision roles, carts,
    locks — stay in `OrderLifecycle.add_attachment`).
  - `GET /entities/{ref}/attachments` → `{"attachments": [EntityAttachment]}`. For
    `print_archive:<archive id>` it reads `print_attachments` through
    `bambuddy/attachments.py`'s store and maps each row (`source = "bambuddy"`,
    `media_type` from the filename, no `sha256`: make `sha256` `str | None`).
    `print_archive` is a read-only kind: not in `KINDS`, so writes answer 422.
  - `GET /entities/{ref}/attachments/{id}/content` and `/thumbnail` — copy the serving rules
    of `api/order_attachments.py` (`INLINE`, `THUMBNAILED`, `NOSNIFF`, `IMMUTABLE`, the
    sandbox CSP, `Range`); move the shared helpers into `api/attachment_serving.py` and make
    `api/order_attachments.py` import them (no copy).
  - `PATCH /entities/{ref}/attachments/{id}` body `{caption?, role?}`; `DELETE …/{id}` → 204.
    Both 404 when the attachment is not that entity's.
  - Errors: 422 `bad_entity_ref`, 422 `unknown_entity_kind`, 404 `entity_not_found`, 404
    `attachment_not_found`, 422 `bad_role` (role not matching `^[a-z_]{1,32}$`).

- [ ] **Step 1: Write failing tests**
  - `tests/entities/test_attachments.py`: add to `spool:<id>` then `list` returns it with
    `entity == "spool:<id>"`; the same sha256 added to a second spool → one blob key, two
    `blob_refs` rows; `delete` of one leaves the other's hold; `add` to a missing spool raises
    `EntityNotFound` and leaves no `entities` row.
  - `tests/api/test_entity_attachments.py` (as `tests/api/test_order_attachments.py` drives
    uploads): PNG upload to `supplier:<id>` → 200, `kind == "image"`; GET content serves
    `image/png` with `X-Content-Type-Options: nosniff`; an HTML upload is served as
    `application/octet-stream` with `Content-Disposition: attachment`; `POST` to
    `order:<id>` → 409 `use_order_route`; GET list for `order_item:<id>` returns the
    attachments the order route added; `spool:<random uuid>` → 404 `entity_not_found`;
    `banana:1` → 422 `unknown_entity_kind`; `spool` → 422 `bad_entity_ref`; PATCH role
    `"Bad Role"` → 422 `bad_role`; GET list `print_archive:<id>` maps a seeded
    `print_attachments` row with `source == "bambuddy"`.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/entities tests/api/test_entity_attachments.py -v` — Expected: FAIL.
- [ ] **Step 3a: Implement the store and component.**
- [ ] **Step 3b: Move the serving helpers to `api/attachment_serving.py`; implement the routes and the operation.**
- [ ] **Step 3c: Implement the `print_archive` read adapter.**
- [ ] **Step 4: Run** `cd backend && uv run pytest tests/entities tests/api/test_entity_attachments.py tests/api/test_order_attachments.py -n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): attachments on any entity`

### Task 4: Links

**Files:**
- Create: `backend/scadbuddy/entities/links.py`, `backend/scadbuddy/api/entity_links.py`
- Modify: `backend/scadbuddy/entities/component.py`, `backend/scadbuddy/main.py`
- Test: `backend/tests/api/test_entity_links.py`

**Interfaces:**
- Produces:
  - `class EntityLink(BaseModel)`: `id: UUID, entity: str, url: str, kind: str, title: str,
    note: str, position: int, source: str, created_at, created_by`.
  - `class LinkInput(BaseModel)`: `url: str` (max 2000, must parse with scheme `http` or
    `https` and a host; else 422 `bad_link_url`), `kind: str = "other"`
    (`^[a-z_]{1,32}$`), `title: str = ""` (≤200), `note: str = ""` (≤1000),
    `position: int | None = None` (None = after the last).
  - `class EntityLinkStore(pool, entities)`: `list(ref) -> list[EntityLink]` (by position,
    then created_at); `add(ref, body: LinkInput, by) -> EntityLink`; `update(ref, link_id,
    body: LinkPatch) -> EntityLink` (a person's update sets `source = "manual"`);
    `delete(ref, link_id) -> None`; `add_from_source(conn, ref, body, source: str) ->
    EntityLink | None` (for imports in later phases: skips when a `manual` link with the
    same URL exists).
- Routes: `GET/POST /entities/{ref}/links`, `PATCH/DELETE /entities/{ref}/links/{id}`; same
  error codes as Task 3 plus `link_not_found`, `bad_link_url`. Writes to order kinds are
  allowed (links have no order rules).

- [ ] **Step 1: Write failing tests**: add a `support` link to `printer:<id>` → listed;
  `javascript:alert(1)`, `ftp://x`, `http://` (no host) → 422 `bad_link_url`; a 2001-char
  URL → 422; PATCH title keeps id and sets `source == "manual"` on a row seeded with
  `source = "bambu_store"`; DELETE → 204 then list empty; a link on `spool:<random uuid>` →
  404 `entity_not_found`; position: three adds then PATCH the third to `position: 0` →
  list order is third, first, second.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/api/test_entity_links.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement store and routes.**
- [ ] **Step 4: Run** the same command — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): links on any entity`

### Task 5: Sweep entities whose owner is gone

**Files:**
- Modify: `backend/scadbuddy/workflows/housekeeping.py` (a new step beside the
  `print_attachments` step; follow its pattern), the housekeeping activity module it calls
- Create: `backend/tests/fixtures/housekeeping_histories/entity_orphans.json` (record it the
  way the other histories there were recorded — see `tests/test_housekeeping_replay.py`)
- Test: `backend/tests/entities/test_sweep.py`, `backend/tests/test_housekeeping_replay.py`

**Interfaces:**
- Consumes: `EntityStore.orphans`, `EntityAttachmentStore.delete`.
- Produces: `sweep_orphan_entities(entities, attachments, *, limit: int = 500) -> int`
  (entities removed). For each orphan: delete its attachments (releasing holds), then the
  entity row (links cascade). Kinds in `ORDER_KINDS` are skipped (their attachments use
  `ON DELETE RESTRICT` on `order_id` and the order code deletes them). This is the phase 1
  form of spec D4: owner stores do not call `drop_entity` yet, because phase 1 touches no
  filament code; later phases add the call in each store they own.

- [ ] **Step 1: Write failing test**: a spool with one attachment and one link; delete the
  spool row; `sweep_orphan_entities` returns 1, the attachment row and its `blob_refs` hold
  are gone, the link is gone; an order item's entity with its owner present is untouched.
- [ ] **Step 2: Run** `cd backend && uv run pytest tests/entities/test_sweep.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement the sweep and wire the housekeeping step; record the replay history.**
- [ ] **Step 4: Run** `cd backend && uv run pytest tests/entities tests/test_housekeeping_replay.py -n auto` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): sweep entities whose owner is gone`

### Task 6: One attachments panel and one links panel

Load `frontend-design` first.

**Files:**
- Create: `frontend/src/components/entities/AttachmentsPanel.tsx`,
  `frontend/src/components/entities/LinksPanel.tsx`, `frontend/src/components/entities/api.ts`,
  `frontend/src/mocks/features/entities.ts` (register it where the other feature mocks are)
- Modify: `frontend/src/pages/filament/SpoolPage.tsx`, `TypePage.tsx`, `SupplierPage.tsx`
  (add both panels), `frontend/src/pages/PartsPage.tsx` (both panels per part, in the part's
  detail/expanded row)
- Reuse: `frontend/src/components/orders/attachmentMedia.ts` (media type → preview), moved
  to `components/entities/attachmentMedia.ts` with `OrderAttachments.tsx` importing it.
- Test: `AttachmentsPanel.test.tsx`, `LinksPanel.test.tsx`

**Interfaces:**
- Consumes: Task 3/4 routes via the generated client (`pnpm gen:api`).
- Produces: `<AttachmentsPanel entity="spool:<id>" />` (list with thumbnails, upload with role
  and caption, caption/role edit, delete with confirm; read-only when every item has
  `source === "bambuddy"` or the kind is an order kind, with a link to the order page for
  adding) and `<LinksPanel entity="…" />` (list grouped by kind with known-kind labels, add
  / edit / delete / reorder; each link `target="_blank" rel="noopener noreferrer"`).

- [ ] **Step 1: Write failing tests**: AttachmentsPanel lists two mocked attachments, uploads a
  PNG (msw handler receives multipart with `role`), deletes after confirm; LinksPanel adds a
  `support` link, shows the server's `bad_link_url` message for `javascript:x`, and renders
  the link with `rel="noopener noreferrer"`.
- [ ] **Step 2: Run** `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/entities` — Expected: FAIL.
- [ ] **Step 3: Implement the panels, mocks and page placements.**
- [ ] **Step 4: Run** `cd frontend && pnpm typecheck && pnpm exec vitest run src/components/entities src/pages/filament/SpoolPage.test.tsx src/pages/filament/SupplierPage.test.tsx src/pages/filament/TypePage.test.tsx src/pages/PartsPage.test.tsx src/components/orders` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(entities): attachments and links panels`

### Task 7: Agent tools

**Files:**
- Create: `agent/src/tools/entities.ts`
- Modify: `agent/src/tools/index.ts` (register), the plugin's tool list and skills if tools
  are listed there (`plugins/CLAUDE.md`; bump `plugin.json` version as
  `check-plugin-version.sh` requires)
- Test: `agent/test/tools/entities.test.ts` (follow the nearest existing tool test, for
  example the orders tools' test)

**Interfaces:**
- Produces tools (CRUD route calls with the agent's service credential, results wrapped as
  untrusted data like the other tools): `entity_attachment_list {entity}`,
  `entity_attachment_update {entity, attachment_id, caption?, role?}` (outward: pending
  action), `entity_attachment_delete {entity, attachment_id}` (outward),
  `entity_link_list {entity}`, `entity_link_add {entity, url, kind?, title?, note?}`
  (outward), `entity_link_update {entity, link_id, …}` (outward), `entity_link_delete`
  (outward). Upload by the agent is out of scope for phase 1.

- [ ] **Step 1: Write failing tests**: each tool calls the expected route and method;
  `entity_link_add` returns a pending action, not a write; a 404 `entity_not_found` becomes
  a tool error naming the entity.
- [ ] **Step 2: Run** `cd agent && pnpm exec vitest run test/tools/entities.test.ts` — Expected: FAIL.
- [ ] **Step 3: Implement and register the tools.**
- [ ] **Step 4: Run** `cd agent && pnpm typecheck && pnpm exec vitest run test/tools` — Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): entity attachment and link tools`

## Deviations from the spec (for the owner)

- §6.2 says `print_attachments` rows move into the generic table. Their bytes are held by
  Bambuddy (archive photos, library files), not the blob store, so phase 1 lists them through
  a read adapter (`print_archive:<id>`, Task 3) and leaves the table where it is. Moving them
  is a later, separate change if it is still wanted.
- §6.1 D4 has each owner store delete its entity. Phase 1 touches no filament code, so a
  housekeeping sweep (Task 5) removes orphaned entities instead; later phases add the direct
  call in the stores they change.

## Later phases

Phases 2–7 of the spec get one plan each, written when the phase before them is merged
(phases 2 and 3 also wait for the filament epic's purchasing PRs).
