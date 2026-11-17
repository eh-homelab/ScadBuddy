# Declared pages: shop-written back-office pages on Host API v2

Status: proposed, 2026-11-17. Issue #4254 (phase 6 of epic #4248). The owner approves this
spec before the issue closes. Design only; no code.

This spec amends the template-pipelines spec (`2026-09-27-template-pipelines-design.md`,
"the pipelines spec", §4 template UI and §9 trust). The orders spec
(`2026-10-18-orders-design.md`, "the orders spec") §9 fixed its shape, and §11 phase 6
names it. It follows the flows specs (`2026-10-01-durable-printing-agents-flows-design.md`,
"the flows spec", and `2026-10-11-flow-grants-and-git-design.md`) for versions in git. It
follows the storefront spec (`2026-11-02-storefront-design.md` §5.1) and the root
`CLAUDE.md` "Database access (#5295)" for where data lives and who touches SQL.

Every fact about the code was read from the tree at `origin/main` `41e438599` on
2026-11-17. A `file:line` is for that commit. Nothing here was measured on a live system.
A choice marked **(call)** is the author's; the owner may overturn it.

## 1. What declared pages are for now

The orders spec §9 meant declared pages for "a branded order form or a customer-facing
status page a shop writes for itself over the orders API". On 2026-11-07 the orders
session and the storefront session deferred this issue, because the storefront epic
#4962 delivers both of those (comment on #4254):

- #5282: listings, customize, estimate and drafts.
- #5283: submit, accounts, the order link and the customer's order page.

So this spec gives declared pages a smaller job. A declared page is a **back-office tool
or dashboard that the shop writes over the API**. Examples:

- a print-farm board that shows the printers, the queue and today's orders on one screen;
- an intake checklist for a craft fair, kept as records, that starts a flow per row;
- a margin dashboard over `GET /orders/reports/profit` with the shop's own grouping.

A declared page is **back-office only**. It is never served on the storefront's public
host. A later spec must decide otherwise, and §8 says what that spec would need.

### 1.1 Goals

- One way to add a back-office page without a ScadBuddy release: a module, a JSON file,
  versions in git.
- The loader, the shadow root, the 15 s deadline and the file checks that template UIs
  have, reused, not copied.
- A subject-neutral host (Host API v2) that gives a page lists, forms, records, flow runs
  and realtime, so a page does not re-implement them.
- A small, typed record store for data that has no table of its own, reached only through
  CRUD routes on the API.

### 1.2 Non-goals

- Pages on the storefront host, or any page a customer sees.
- A page that runs on the server. A page is browser code only.
- A second way to do what orders, customers or the storefront do. Those stay first-party.
- New lockdowns of back-office routes (§7.3).

## 2. Facts this rests on

| # | Fact | Where |
|---|---|---|
| P1 | The template UI loader is module-agnostic: a dynamic `import()` of a versioned URL, an open shadow root, the app's sheets adopted, one 15 s deadline over load and mount, and a failure the shell reports. A late mount's cleanup runs at once. | `frontend/src/template-ui/TemplateUi.tsx:17` (`MOUNT_TIMEOUT_MS`), `:80-148`; `loadModule.ts:1-14`; `styles.ts:75` (`adoptAppStyles`) |
| P2 | The served-file checks: a suffix allowlist with no HTML, no dotfile or empty segment, no path escape after `resolve()` (a symlink out too), the same 404 for every failure, `nosniff`, `default-src 'none'; sandbox`, `Cross-Origin-Resource-Policy: same-origin`, an ETag, and `immutable` only for an exported commit. | `backend/scadbuddy/api/template_ui.py:33-95` (`UI_MEDIA_TYPES`, `UI_FILE_HEADERS`, `_ui_file`, `_serve`) |
| P3 | Host API v1 is one template's inputs: `inputs.get/set/subscribe`, `schema`, `files.url`, `generate`, `openPrint`, `presets`, `describe`. The host mounts majors `[current, current - 1]`; `UI_API_CURRENT` is 1. | `frontend/src/template-ui/types.ts:4-9`, `:36-60`; `host.ts:85` (`createHost`); pipelines spec §4.3 |
| P4 | The host elements are `sb-param`, `sb-preview` and `sb-generate`. Each is a custom element whose content React renders through a portal. | `frontend/src/template-ui/elements.ts:1`, `:49`; `TemplateUi.tsx:164-176` |
| P5 | A template UI module is not sandboxed. It runs in the page, under the page CSP, so it can call any `/api/v1` route as the viewer. | pipelines spec §9; `backend/scadbuddy/api/static.py:58-68` (`PAGE_CSP`: `script-src 'self'`, `connect-src 'self'` and Plaid's two hosts) |
| P6 | `FlowHistory` is a `GitRepo` under `data/flows`: `<name>/flow.py` and `<name>/inputs.json`, one commit per version; `<name>/flow.json` holds the display name and description, each change its own commit. Writers hold a Postgres lock that carries HEAD, because every replica shares the repo over NFS. | `backend/scadbuddy/flows/repository.py:1-20`, `:221`; `core/gitrepo.py:232`, `:276`; `library/repo_lock.py:180` (`flows_coordinator`); `core/paths.py:80` |
| P7 | The flow inputs subset: a flat object of `string`, `integer`, `number`, `boolean`, or an array of one of them, with `enum` and bounds, and `additionalProperties: false`. The API checks a schema (`schema_problems`) and checks and fills values (`resolve_inputs`). A `pattern` needs a `maxLength`, and patterns have a per-value and per-call time budget, because they run in the API process. | `backend/scadbuddy/flows/inputs.py:1-17`, `:40-48`, `:77`, `:281` |
| P8 | The frontend draws that subset as a form. | `frontend/src/lib/flowInputs.ts:35` (`inputFields`); `components/flows/inputsForm.tsx:10` (`useInputs`) |
| P9 | Phase 1 (#4249, closed) extracted `DataTable`, `FilterBar` and `RecordDrawer`; the orders, customers and activity pages use them. | `frontend/src/components/records/DataTable.tsx:66`, `FilterBar.tsx:133`, `RecordDrawer.tsx:27`; `backend/CLAUDE.md` "orders phase 1" |
| P10 | The realtime socket takes topics that match one closed pattern; frames carry the event kind and ids, never content; the client re-reads through REST. | `backend/scadbuddy/api/realtime.py:1-6`, `:161` (`MAX_TOPICS`), `:181-215` |
| P11 | A run starts at `POST /api/v1/workflows/{definition_id}/runs` with an `Idempotency-Key`, as the caller. Its outward calls still need a grant or a person's approval. | `backend/scadbuddy/api/flows.py:715-725`; flows spec §7 |
| P12 | A browser request acts as the browser principal, with the signed-in person when there is one; an agent acts by the author header. No body names a principal. | `backend/scadbuddy/orders/actor.py:1-25`; `core/authorship.py:126` (`current_person`); orders spec Ruling 12 |
| P13 | The storefront edge is a separate process. It mounts no `/api/v1` route and never proxies one. It serves `storefront.html` and only the chunks in that entry's import closure; every other path, the back office's `index.html` and chunks included, is 404. A lint rule stops the storefront from importing back-office code. | `backend/scadbuddy/storefront/app.py:1`; `storefront/static.py:1-12`; `frontend/eslint.config.js:7-15`; storefront spec §5.1 |
| P14 | The customer accepts a quote on the storefront: `POST /api/store/v1/order/quotes/{id}/accept` on the edge, `…/crud/storefront/order/quotes/{id}/accept` on the CRUD service, which runs `Lifecycle.accept_quote` as the customer actor. The accept that leaves every open item `approved` accepts the order as the customer (`_accepted_by_the_last`). On a storefront order the shop's own accept is refused `customer_accepts`. | `backend/scadbuddy/storefront/routes.py:649-658`; `api/crud_storefront.py:859-877`; `orders/storefront/order_page.py:896-915`; `orders/lifecycle.py:4531-4550`, `:4599-4621`, `:4662`; storefront spec §6.1-§6.2; #5283, #6153 (both closed) |
| P15 | Only the CRUD service (today's API) touches SQL. No untrusted code runs in a pod that holds a database secret. New data that crosses services goes through a CRUD route. | root `CLAUDE.md` "Database access (#5295)"; `docs/security/trust-boundaries.md` "The rule" |
| P16 | Template UI modules run in the browser, with a shadow root and the served-file headers as their guard, and cannot reach SQL. | `docs/security/trust-boundaries.md` "Where untrusted code runs" |
| P17 | A dict read from a `jsonb` column has no key order (Postgres stores the keys shortest first). | `backend/CLAUDE.md` (the `storefront_listings.params` note, #6302) |

## 3. The page

### 3.1 Files and versions

A page lives in a git repository of its own, `data/pages`, as flows live in `data/flows`
(P6):

```
data/pages/
  <name>/page.json        the declaration (§3.2)
  <name>/ui/index.js      the module, and any file it imports under ui/
  _types/<type>.json      a record type's schema (§5.1)
```

- `<name>` is a slug, `[a-z0-9][a-z0-9-]{0,62}`. `_types` and `_host` (§9) cannot
  be page names, because a name never starts with `_`.
- `PageHistory(GitRepo)` (`backend/scadbuddy/pages/repository.py`) is `FlowHistory`'s
  shape: one commit per version, an optional message and body, and a Postgres lock that
  carries HEAD (`pages_repo_head`, a migration, and `repo_lock.py` `pages_coordinator`).
  The off-box push (`git_remote/`, #4974) pushes the `models` and `flows` repositories
  only (`git_remote/component.py:29-31`); phase 2 adds `pages` to that map.
- A version is the whole `<name>/` tree at one commit. A version never changes.
- A page is deleted by a commit that removes `<name>/`. A restore brings it back as a new
  version, as for flows.

### 3.2 The declaration

```json
{
  "api": 2,
  "title": "Print-farm board",
  "nav": true,
  "module": "ui/index.js",
  "records": ["fair_intake"],
  "flows": ["order-fulfilment"]
}
```

- `api`: the host major the module was written for. A page needs `2` (§4).
- `title`: the shell's heading and the nav label, 1 to 60 characters.
- `nav`: whether the shell lists the page in its navigation. A page is always reachable
  at `/p/<name>`.
- `module`: a file under `ui/`, as a path from the page's directory, default
  `ui/index.js`. The file routes of §3.3 take a path relative to `ui/`, so the shell
  drops the `ui/` prefix before it builds the URL, as `TemplateUi.tsx:108` does for a
  template.
- `records`: the record types (§5) the page reads or writes. The host gives the page
  `host.records.<type>` for these types only.
- `flows`: the flows the page may start through `host.flows.start`. **(call)** This list
  is a declaration for the reader and for `host.describe`, not a guard: the module can
  still call the start route with `host.api.request` (§7.2).

The route is always `/p/<name>`. The orders spec §9 put `"route"` in the file. This spec
drops it: a second name for the same page adds a check and no use.

The API checks `page.json` when a version is registered: a JSON object, the keys above
only, each flow name a page-style slug (`[a-z0-9][a-z0-9-]{0,62}`), each record type
name a type slug (`[a-z][a-z0-9_]{0,62}`, §5.1), each named record type present at that commit, the
module present under `ui/` with a servable suffix (P2). A failure is 422 and writes no
commit.

### 3.3 Routes

All are under `/api/v1`, on the API (the CRUD service), with the viewer's own auth (§7.1).

- `GET /pages` — each page's name, title, `nav`, newest version and commit, and its
  mountable version (§7.4).
- `GET /pages/{name}` — the declaration at the mountable version, the mountable commit,
  and every version with its author and whether a person confirmed it. The mountable
  version is the newest version that a person wrote or confirmed (§7.4).
- `POST /pages` — a new version: `{name, files: {path: text}, message?, body?}`, with an
  `Idempotency-Key`. The text files sent replace every text file of the `<name>/` tree
  (a text file not sent is deleted). Text files only (the suffixes of P2 except the
  images and the font). The binary files (images and fonts) carry over unchanged.
- `PUT /pages/{name}/files/{path}` and `DELETE …/files/{path}` — one binary file, as
  bytes, under the same checks. Each call is a version of its own.
- `POST /pages/{name}/versions/{commit}/confirm` — a person confirms a version the
  assistant wrote (§7.4). An agent author is refused 403, as `auth/routes.py` refuses
  agents on its `person` routes.
- `POST /pages/{name}/restore` `{commit}` — a new version with that commit's tree.
- `DELETE /pages/{name}` — a delete commit.
- `GET /pages/{name}/ui/{path}` — a file of the live tree, `no-cache`.
- `GET /pages/{name}/versions/{commit}/ui/{path}` — a file at a commit: from the live tree
  while it holds exactly that commit's `ui/` with nothing uncommitted, else from an export
  of the commit, `immutable` (the pipelines spec §4.2, #1481).

Both file routes use the checks of P2 unchanged. Phase 1 (§10) moves `UI_MEDIA_TYPES`,
`UI_FILE_HEADERS`, `_ui_file` and `_serve` from `api/template_ui.py` to
`core/ui_files.py`, so the template routes and the page routes share one copy.

The bus gets `page.changed` `{name}` on the collection topic `pages` and the topic
`page:<name>` (P10), so the nav and an open page see a new version. `api/realtime.py`
gains `pages` in `COLLECTION_TOPICS` and `page:<name>` in `_TOPIC`.

### 3.4 Mounting

The shell gains one route, `/p/:name/*`. It reads `GET /pages/{name}`, loads
`/api/v1/pages/{name}/versions/{commit}/ui/{path}` for the mountable commit (§3.3), with
`{path}` the declaration's `module` less its `ui/` prefix, and mounts it.

Phase 1 extracts the loader from `TemplateUi.tsx` into `template-ui/UiMount.tsx`:

```ts
function UiMount(props: {
  url: string                       // the versioned module URL
  makeHost: () => HostHandle        // the template's or the page's host
  context: MountContext             // { slot, version, theme, api, path? }
  failureKey: string                // the module's path, for the error text
  onFailure: (failure: UiFailure) => void
}): JSX.Element
```

It keeps all of P1: the shadow root (`mode: 'open'`, so the host elements can render into
it), `adoptAppStyles`, the 15 s `MOUNT_TIMEOUT_MS` over load and mount together, a mount
that resolves after the deadline cleaned up at once, a cleanup that throws logged and
never reported as a failure, and one element per mount (the `key`). `TemplateUi` becomes
`UiMount` plus the v1 host and the generated-form fallback. `PageUi` is `UiMount` plus
the v2 host.

A page has no generated form to fall back to. On a failure the shell shows an error
panel: the file, the error, the version, and a link to the page's versions so a person
can restore an older one.

`MountContext` gains `path`: the part of the URL after `/p/<name>/`. A page that has
views of its own reads it and calls `host.navigate(path)`; the shell owns the history.

## 4. Host API v2

Host API v2 is subject-neutral. A page gets it alone. A template whose `ui` declares
`api: 2` gets v1's members and v2's members on one object (§4.3).

```ts
interface HostV2 {
  readonly api: 2
  /** The one way to the API: same origin, under /api/v1, as the viewer. */
  api: {
    request<T = unknown>(path: string, init?: RequestInit & { key?: string }): Promise<T>
  }
  /** One member per type named in page.json `records` (§5). */
  records: Record<string, RecordsOf>
  flows: {
    start(name: string, inputs?: Json, opts?: { key?: string }): Promise<{ runId: string }>
    run(runId: string): Promise<FlowRunView>
    subscribe(runId: string, fn: (run: FlowRunView) => void): () => void
  }
  realtime: { subscribe(topic: string, fn: (event: RealtimeEvent) => void): () => void }
  files: { url(path: string): string }
  navigate(path: string): void
  describe(fn: () => string): void
}

interface RecordsOf {
  schema(): Promise<RecordTypeView>
  list(query?: { cursor?: string; limit?: number; where?: Record<string, Scalar> }): Promise<RecordPage>
  get(id: string): Promise<RecordView>
  put(body: Json, opts?: { id?: string; version?: number; key?: string }): Promise<RecordView>
  delete(id: string, opts?: { version?: number }): Promise<void>
  subscribe(fn: (change: { id: string; deleted?: true }) => void): () => void
}
```

### 4.1 What each member does

- **`api.request`**. `path` must start with `/` and not with `//`. The host prefixes
  `/api/v1` and sends it through the shell's own client (`frontend/src/api/client.ts`
  `send`), so the page gets the app's problem errors (`ApiError`), its offline handling
  and its cursor tracking. `key` becomes an `Idempotency-Key`. The host adds a W3C
  `traceparent` and a header `X-ScadBuddy-Page: <name>@<commit>`. The API records that
  header as a span attribute and on the activity rows the request starts. It is a label
  for the reader, not a proof: the module could send any header with `fetch` (P5). The
  types come from the generated schema (`frontend/src/api/schema.d.ts`); the authoring
  guide (§9) ships them as a `.d.ts` for page authors.
- **`records.<type>`**. A thin client over §5's routes. `subscribe` follows the topic
  `records:<type>` and calls back with ids only; the page re-reads (P10).
- **`flows.start`** calls `POST /workflows/{id}/runs` for the newest version of the flow
  named, with `key` as its `Idempotency-Key` (a fresh one when none is given). The run
  is the viewer's (P11, P12). Its outward calls still ask a person or use a grant.
  `flows.run` reads `GET /workflow-runs/{id}`. `flows.subscribe` follows
  `workflow-run:<id>` and re-reads.
- **`realtime.subscribe`** takes any topic the socket takes (P10). The host shares the
  shell's one socket. A page cannot open a second one through the host.
- **`files.url`** gives the versioned URL of a file under the page's `ui/`.
- **`navigate`** changes the part of the URL after `/p/<name>/`.
- **`describe`** gives the assistant a summary of what the page shows, as v1's does.

Host elements for pages (P4 pattern; phase 4):

- `<sb-table>` wraps `DataTable`. The page sets the properties `columns` (`{key, label,
  align?, format?}`, where `format` is one of `text`, `number`, `money`, `date`,
  `status`) and `rows`. It fires `open` with the row's key. A column renders text only;
  no page code runs inside the React tree.
- `<sb-filter-bar>` wraps `FilterBar`: the property `spec` (search, selects, chip groups)
  and the event `change` with the values.
- `<sb-drawer>` wraps `RecordDrawer`: the attribute `open`, a `title`, and slots for the
  body, the actions and the footer.
- `<sb-record-form type="fair_intake">` draws a record type with the flow inputs form
  (P8) and fires `submit` with the values. With the attribute `record`, it loads and
  saves that record, and sends its `version` (§5.3).

The elements give the page the app's own look, through the sheets that `adoptAppStyles`
already copies into the shadow root (pipelines spec §4.2, #736).

### 4.2 Versions of the host

`UI_API_CURRENT` becomes 2. The host then mounts majors 2 and 1 (P3). The rules:

- A template's `{api: 1}` module keeps working, with the v1 host, unchanged.
- A template's `{api: 2}` module gets one object with v1's members (`inputs`, `schema`,
  `generate`, `openPrint`, `presets`) and v2's. `files.url` and `describe` mean the same
  in both. `records` is empty unless the template's `model.json` `ui` names
  `records`, under the same rules as a page.
- A page needs `api: 2`. A page with `api: 1` fails to mount with "a page needs host API
  2". A page has no inputs to give a v1 module.

## 5. Records

A record type is the generic "backend service" a page gets without code: a typed, flat
store, read and written through CRUD routes on the API only.

### 5.1 A record type

A record type is a JSON Schema in the flow inputs subset (P7), in
`data/pages/_types/<type>.json`, versioned in the pages repository. `<type>` is a slug
with `_` in place of `-`, `[a-z][a-z0-9_]{0,62}`.

- `POST /record-types` `{type, schema, message?}` registers a new version. The API runs
  `flows/inputs.py` `schema_problems` on it. The subset has no nested object, so a record
  is flat: scalars and arrays of scalars. A link to another record is a `string` id; the
  store does not check it (§6).
- A new version must accept every stored row of the type. The registration runs in one
  Postgres transaction that takes the type's lock exclusively (§5.4). In it, the API
  checks each stored body against the new schema and refuses with 409
  `records_do_not_fit` and the first 20 failing ids. Then it commits the version to git,
  writes the `record_types` row (§5.2), and commits the transaction, which releases the
  lock. So a version
  can add an optional field, widen a bound or add an `enum` value; it cannot rename a
  field that rows hold. **(call)** A type holds at most 10,000 rows, so this check stays
  bounded.
- `GET /record-types` and `GET /record-types/{type}` return the newest schema and its
  commit.

A type belongs to no page. Two pages that name it share its rows: an intake form and a
dashboard over the same intake.

### 5.2 The table

A migration (`<ts>_records.sql`) adds:

```sql
CREATE TABLE record_types (
  type        text        PRIMARY KEY,
  commit      text        NOT NULL,   -- the newest version's commit in data/pages
  schema      jsonb       NOT NULL,   -- that version's schema, the one writes check
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE records (
  type          text        NOT NULL,
  id            uuid        NOT NULL,
  body          jsonb       NOT NULL,
  version       integer     NOT NULL DEFAULT 1,
  schema_commit text        NOT NULL,   -- the type's commit that checked this body
  created_at    timestamptz NOT NULL DEFAULT now(),
  created_by    text        NOT NULL,   -- the actor (P12), never from a body
  updated_at    timestamptz NOT NULL DEFAULT now(),
  updated_by    text        NOT NULL,
  page          text,                   -- the X-ScadBuddy-Page label, when sent
  PRIMARY KEY (type, id)
);
CREATE INDEX records_newest ON records (type, created_at DESC, id);
```

Git keeps every version of a type's schema. `record_types` keeps the newest one, so a
write reads the schema it checks against in its own transaction (§5.4). A git commit
whose `record_types` row was never written (the API stopped between the two) is
repaired at the next start, under the same rules as a registration: the repair takes the
type's lock exclusively (§5.4) and checks every stored row against that commit's schema.
Writes went on against the old schema meanwhile, so a row may no longer fit. If every row
fits, the repair writes the `record_types` row. If one does not, the repair leaves
`record_types` on the old commit, writes a git commit that puts back the schema in force,
and logs the type and the first 20 failing ids. So `record_types` is the schema in force,
and no version is in force before every stored row fitted it under the lock.

The fit check (§5.1) runs `resolve_inputs` on up to 10,000 bodies while it holds the
exclusive lock, and each call has its own patterns budget (P7). So the check has one
total deadline, `FIT_CHECK_DEADLINE_S`, 10 s **(call)**. Past it, the registration (or
the repair) stops, writes nothing, and answers 409 `fit_check_too_slow` (the repair logs
it and puts back the schema in force, as for a row that does not fit). Writes to the type
wait at most that long. This is not
`flows/reconcile.py`'s repair, which indexes a commit and re-checks nothing. A registration whose
transaction fails after its git commit, while the API still runs, starts the same repair
at once and answers 503.

`records_newest` serves the list's order and cursor. A `where` filter is a scan of the
type's rows in that index: a type holds at most 10,000 rows (§5.1), so the scan is
bounded, and no index on `body` is added. **(call)**

`records/store.py` reads and writes it on the shared `scadbuddy-rows` pool
(`rows/component.py`; `backend/CLAUDE.md` "Postgres pools", #5226). No other process
reads or writes the table, and nothing grants it to any role (P15).

### 5.3 Routes and the check at the route

All under `/api/v1`, on the API, with the viewer's own auth:

- `GET /records/{type}?cursor=&limit=&where.<field>=<value>` — a page, newest first,
  50 by default, 200 at most. `where` matches a scalar field by equality only; the field
  must be in the schema (else 422).
- `GET /records/{type}/{id}`.
- `POST /records/{type}` — a new record, with an `Idempotency-Key`.
- `PUT /records/{type}/{id}` with `If-Match: <version>` — a replace. A version that is no
  longer current is 412 `record_changed`. With no `If-Match`, 428.
- `DELETE /records/{type}/{id}` with `If-Match`.

The check, in the API, on every write, before any SQL write:

1. The type exists (404 `record_type_not_found`).
2. The body is at most 64 KiB as sent (413).
2a. A create only: the type holds fewer than 10,000 rows (§5.1), else 409
   `record_type_full`. The create counts the rows after it locks the type's
   `record_types` row `FOR UPDATE`, inside the shared lock of §5.4, so two creates of one
   type count one after the other and cannot both pass at 9,999. Replaces and deletes do
   not take that row lock.
3. The body is checked and filled by `flows/inputs.py` `resolve_inputs` against the
   type's newest schema: unknown keys refused, types, bounds, enums and patterns checked
   within the patterns' time budget, defaults filled (P7). A failure is 422 with one
   error per field, each with its JSON Pointer.
4. Then the insert or update, with `schema_commit` set to that schema's commit, in the
   transaction of §5.4, with the bus event `record.changed` `{type, id, deleted?}` on the topics
   `records:<type>` and `record:<type>:<id>` (P10; the topic pattern gains both).

### 5.4 Writes and a new schema do not race

Every record write (create, replace, delete) runs in one transaction that first takes
the type's lock in shared mode: `pg_advisory_xact_lock_shared` on a key derived from
`records:<type>`. Under it, the write reads the `record_types` row, checks the body
against that schema (§5.3 step 3), and writes the row with that row's `commit` as
`schema_commit`. A registration takes the same key with `pg_advisory_xact_lock`
(exclusive), so it waits for the writes in flight, and writes wait for it. The
registration writes `record_types` and commits before it releases the lock (§5.1). So a
write checks against the schema that was newest when it took the lock, and no row that
a registration checked can change before the new schema is in force. Step 3 runs inside
the lock, but its patterns stay within the time budget of P7, so a write holds the
shared lock for at most that budget plus the insert.

### 5.5 Reads

A read returns the body with its keys in the schema's order, not the stored order (P17),
plus `id`, `version`, `schema_commit` and the four stamps.

The agent gets the same routes as tools (`record_list`, `record_get` and
`record_type_get` at `read`; `record_put`, `record_delete` and `record_type_register` at
`write`), and `page_list`, `page_get`, `page_register`
at `read` and `write`, through the API like every agent tool.

## 6. Why orders is first-party React, not a declared page

The orders spec §9 decided this. The record store above makes the reason concrete:

- **Money.** An order's amounts are integer minor units with a currency exponent per
  ISO 4217 (`orders/currency.py`), and every price comes from one formula
  (`orders/pricing.py`, `orders/costs.py`). The inputs subset has `number` and
  `integer`, with no currency and no rounding rule.
- **State machines.** `Lifecycle` checks each transition under the order's row lock,
  derives the status past `accepted`, writes the audit row and publishes after commit
  (`backend/CLAUDE.md`, `orders/lifecycle.py`). A record has a version for compare and
  set, and no transitions.
- **Foreign keys.** Items, quotes, attempts, shipments and payments point at each other
  and at blobs held by `blob_refs`. A record's link is an unchecked string id.

So the orders, customers and profit pages stay React pages over `DataTable`, `FilterBar`
and `RecordDrawer` (P9). A declared page reads orders through `host.api.request`, as any
module can, and offers the same three components as host elements (§4.1).

## 7. Trust and where data lives

### 7.1 The viewer's own auth, and no SQL outside the CRUD service

- A page module is untrusted, shop-written code. It runs in the browser only. No server
  process runs it, so it never runs in a pod that holds a database secret (P15, P16).
- Every `host.*` call is an HTTP request from the browser to `/api/v1` on the same
  origin, sent with the viewer's own credentials: the browser's cookie, under whatever
  `SCADBUDDY_AUTH_MODE` the API runs. A page never gets a service credential, a token or
  a secret. The host has no member that returns one.
- The API is the CRUD service. Each page route, record-type route and record route is a
  CRUD route: it checks its input itself (§5.3) and never trusts the page to have
  checked it. The record check runs in the API process, as the flow inputs check already
  does (P7); a schema is data, not code.
- The `records` table is the API's own. No worker, agent or storefront process reads or
  writes it, and no role gets a grant on it. If another service later needs records, it
  calls the routes with its service credential (root `CLAUDE.md` "Database access").
- Serving a page's files reads bytes from git. It runs nothing.

### 7.2 What a page can reach

A page reaches exactly what a template UI module reaches today: every `/api/v1` route,
as the viewer (P5). `connect-src 'self'` stops it from sending data to another origin
with `fetch`. The host adds no capability. It adds a place to put the module, versions,
and members that spare the module from re-writing lists, forms and realtime. So
`page.json`'s `flows` list, and the `X-ScadBuddy-Page` label, are for the reader and the
trace, not a guard.

### 7.3 What this spec keeps, and what it does not add

Kept from template UIs (pipelines spec §4.2, §9):

- the shadow root, for style isolation;
- the 15 s deadline over load and mount, and a late mount cleaned up;
- the served-file checks and headers of P2: no HTML, so no document of the app's origin
  is ever written by a page; `default-src 'none'; sandbox` on a file opened directly;
  `nosniff`; `same-origin` resource policy;
- the page CSP of `api/static.py`, unchanged.

Not added: no iframe sandbox, no per-route allowlist for pages, no person-only mark on
the page or record routes except the confirm of §7.4, which an agent must not make for
itself. The back office is reached only through the LAN gateway and
Pomerium (`docs/security/trust-boundaries.md` "Zones"). The owner decided not to add
lockdowns of routes reachable only there.

### 7.4 Who writes a page

A person writes a version in the Pages editor or with `POST /pages`. The assistant writes
one with `page_register`, at `write`, as it writes a flow with `register_flow`. A version
records its author as a flow version does (`flows/reconcile.py` `created_by`), and the
versions list shows it. **(call)** A version the assistant wrote mounts only after a
person opens its file list and confirms it once, as the pipelines spec §9 asks for a
URL-imported template's `ui/`. Until then `/p/<name>` shows the newest version a person
wrote or confirmed, and a banner. Reason: a page runs with the viewer's reach, which is
wider than the assistant's tier, so this keeps the tiers meaningful. It is a step in the
page shell, not a lockdown of a route.

## 8. Back office only

Declared pages are never served on the storefront's public host. This holds by
construction today (P13):

- The edge mounts no `/api/v1` route, so `/api/v1/pages/…` and `/api/v1/records/…`
  are 404 on the public host.
- The edge serves only `storefront.html`'s import closure. `PageUi`, the v2 host and the
  host elements live in the back-office entry, so the edge never serves their chunks.
- `frontend/eslint.config.js` stops the storefront from importing back-office code;
  phase 2 adds `pages` and `template-ui` to its list.
- The storefront's router has no `/p/:name` route.

Phase 2 adds a test in the edge's tests that asks the edge for `/api/v1/pages`,
`/api/v1/records/x` and a page's module URL and gets 404 for each.

A later spec that wants a page on the public host must follow storefront spec §5.1: the
edge holds no database secret, every read and write goes through
`/api/v1/internal/crud/storefront/*` with the edge's service credential, and the CRUD
service scopes every read to the visitor. A shop-written module on the public host would
also need a sandbox decision that this spec does not make.

## 9. Authoring

- The authoring guide (`get_authoring_guide`) gains a "Pages" section: the layout, the
  declaration, Host API v2, the host elements, the record routes, and the limits.
- A `.d.ts` for Host API v2 and the API's types, generated with the frontend's
  `gen:api`, is served at `GET /pages/_host/host.d.ts` for editors (`_host` is never a page
  name, §3.1) and the assistant.
- `models/CLAUDE.md`'s template checks do not cover pages. A page has no `verify.sh`.

## 10. Phases

Each phase is one PR series with a plan under `docs/superpowers/plans/`. Each lands
usable on its own. None starts before the owner approves this spec and lifts the
2026-11-07 deferral.

1. **The loader and the file checks, shared** (frontend and backend refactor; no
   behaviour change). `template-ui/UiMount.tsx` from `TemplateUi.tsx`;
   `core/ui_files.py` from `api/template_ui.py`. The tests of both move with them, and
   `TemplateUi.test.tsx` passes unchanged.
2. **Pages** (backend, frontend, agent). `PageHistory`, `pages_repo_head`, `pages` in the off-box push, the routes of
   §3.3, `page.changed` and its two topics in `api/realtime.py`, the shell's `/p/:name/*` and nav, `PageUi`, Host API v2 without
   `records` (`api.request`, `flows`, `realtime`, `files`, `navigate`, `describe`),
   `UI_API_CURRENT` 2, the assistant's confirm (§7.4), agent `page_list`, `page_get`,
   `page_register`, mocks under `src/mocks/features/pages/`, the edge's 404 test and the
   lint list (§8).
3. **Records** (backend, frontend, agent). The `record_types` and `records` migration,
   record types in `_types/` with the routes of §5.1 and their reconcile, `records/store.py`,
   the routes of §5.3 with the check at the route and the lock of §5.4,
   `record.changed` and its topics, `host.records`, agent `record_*` and
   `record_type_*`, mocks.
4. **Host elements** (frontend). `<sb-table>`, `<sb-filter-bar>`, `<sb-drawer>`,
   `<sb-record-form>`; a template's `api: 2` with `records`.
5. **Authoring** (docs, agent). The guide's "Pages" section, the served `host.d.ts`,
   and one sample page in the tests' fixtures that a Playwright test mounts.

## 11. The orders spec's open decision, settled

The orders spec §12 left one question to §9: whether a "customer approved the quote"
step (`quoted` → `accepted`, made by the customer through a declared page) belongs in
the lifecycle.

The storefront settled it, without a declared page (P14):

- The customer accepts a quote on the order page of the storefront (#5283). The edge
  forwards the accept to the CRUD service, which runs `Lifecycle.accept_quote` as the
  customer actor (`orders/storefront/order_page.py:896-915`).
- The accept that leaves every open item `approved` accepts the order as the customer, in
  the same transaction (`orders/lifecycle.py` `_accepted_by_the_last`, `:4662`;
  storefront spec §6.2).
- On a storefront order the shop cannot accept for the customer except with "Accept for
  the customer", which records a reason; any other shop accept is refused
  `customer_accepts` (`orders/lifecycle.py:4605-4621`; storefront spec §6.1).
- The price the customer accepts is firm: it comes from a slice through the back
  office's quote path, never from volume (#6153; `orders/storefront/slice_price.py`).

So the step is in the lifecycle, and declared pages do not add a second path to it. A
declared page that calls an accept route acts as the viewer, the shop, and the lifecycle
refuses it on a storefront order as above. The orders spec §12 bullet now carries a note
that points here.

## 12. Questions for the owner

1. Lift the deferral? This spec gives pages a back-office job (§1). If no such page is
   wanted yet, the issue can stay deferred with this spec as its design.
2. The assistant's page versions (§7.4): a person confirms each one once before it
   mounts (proposed), or they mount at once, with only the author shown?
3. Record types shared by every page (proposed, §5.1), or owned by one page?
4. The caps: 10,000 rows per type and 64 KiB per body (§5.1, §5.3).
