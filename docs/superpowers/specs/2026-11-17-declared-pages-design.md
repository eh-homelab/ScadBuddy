# Declared pages: shop-written back-office pages on Host API v2

Status: approved by the owner, 2026-11-18 (§12). The build stays deferred. Issue #4254
(phase 6 of epic #4248). Design only; no code.

This spec amends the template-pipelines spec (`2026-09-27-template-pipelines-design.md`,
"the pipelines spec", §4 template UI and §9 trust). The orders spec
(`2026-10-18-orders-design.md`, "the orders spec") §9 fixed its shape, and §11 phase 6
names it. It follows the flows specs (`2026-10-01-durable-printing-agents-flows-design.md`,
"the flows spec", and `2026-10-11-flow-grants-and-git-design.md`) for versions in git. It
follows the storefront spec (`2026-11-02-storefront-design.md` §5.1) and the root
`CLAUDE.md` "Database access (#5295)" for where data lives and who touches SQL.

Every fact about the code was read from the tree at `origin/main` `41e438599` on
2026-11-17. A `file:line` is for that commit. The facts P18 to P21, which §5.6 rests on,
were read from `origin/main` `71227b21d` on 2026-11-18, and cite a file and a function.
Nothing here was measured on a live system. A choice marked **(call)** is the author's;
the owner may overturn it. The owner's decisions are in §12.

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
| P5 | A template UI module is not sandboxed. It runs in the page, under the page CSP, so it can call any `/api/v1` route as the viewer. | pipelines spec §9; `backend/scadbuddy/api/static.py:58-68` (`PAGE_CSP`: `script-src 'self'`, one inline-script hash, Plaid's `link-initialize.js` and Cloudflare Web Analytics' beacon; `connect-src 'self'` and Plaid's two API hosts) |
| P6 | `FlowHistory` is a `GitRepo` under `data/flows`: `<name>/flow.py` and `<name>/inputs.json`, one commit per version; `<name>/flow.json` holds the display name and description, each change its own commit. Writers hold a Postgres lock that carries HEAD, because every replica shares the repo over NFS. | `backend/scadbuddy/flows/repository.py:1-20`, `:221`; `core/gitrepo.py:232`, `:276`; `library/repo_lock.py:180` (`flows_coordinator`); `core/paths.py:80` |
| P7 | The flow inputs subset: a flat object of `string`, `integer`, `number`, `boolean`, or an array of one of them, with `enum` and bounds, and `additionalProperties: false`. The API checks a schema (`schema_problems`) and checks and fills values (`resolve_inputs`). A `pattern` needs a `maxLength`, and patterns have a per-value and per-call time budget, because they run in the API process. | `backend/scadbuddy/flows/inputs.py` (`resolve_inputs`); the schema check and the pattern budget moved to `backend/scadbuddy/core/input_schema.py` (#5701) |
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
| P18 | A runtime setting is a field of `Settings`. Every field that is not a bootstrap field is env-seeded: `SCADBUDDY_<FIELD>` seeds it, a value saved in Settings wins, and a reset puts back the environment's value or the default. Each field has its bounds as `Field` constraints, and `check_value` checks a saved value against the same bounds. A field with a floor and no ceiling is allowed (`read_max_triangles`, "there is no ceiling"). | `backend/scadbuddy/core/settings.py` `Settings`, `ENV_SEEDED`, `check_value`, `env_var` |
| P19 | `PUT /settings` takes a `SettingsPatch`, which lists each env-seeded field and checks it with `check_value` (`_in_bounds`); the store keeps one `settings` row per field. `APPLIES` says when a change takes effect: a `live` field is put into `AppState.settings` on every replica on each `settings.changed`. A new env-seeded field must have an `APPLIES` entry, a patch field and a doc, or a test fails. | `backend/scadbuddy/library/settings_store.py` `SettingsPatch`, `SettingsStore.save`; `core/settings.py` `APPLIES`; `api/runtime.py` `apply_runtime`; `api/settings.py` `put_settings` route; `core/setting_docs.py`; `tests/test_settings_coverage.py`, `tests/test_setting_docs.py` |
| P20 | A check reads a live setting from the process's running copy, with no SQL read: the upload cap is `state.settings.media_upload_max_bytes` (`library/attachment_operations.py` `attachment_kinds`, its `_download`). A check that needs the store's own value reads `settings_store.snapshot().runtime` (`orders/storefront/prepare.py` `_facts`, `storefront_prepare_cap`). | as named |
| P21 | No agent tool writes `PUT /settings`: the agent's coverage list names it as a route with no tool, because the runtime settings are an operator decision made in Settings. | `agent/src/tools/coverage.ts` (the `PUT /api/v1/settings` entry) |

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

- `GET /pages` — each page's name, title, `nav`, and newest version and commit.
- `GET /pages/{name}` — the declaration at the newest version, the newest commit, and
  every version with its author (§7.4). The shell mounts the newest version, whoever
  wrote it.
- `POST /pages` — a new version: `{name, files: {path: text}, message?, body?}`, with an
  `Idempotency-Key`. The text files sent replace every text file of the `<name>/` tree
  (a text file not sent is deleted). Text files only (the suffixes of P2 except the
  images and the font). The binary files (images and fonts) carry over unchanged.
- `PUT /pages/{name}/files/{path}` and `DELETE …/files/{path}` — one binary file, as
  bytes, under the same checks. Each call is a version of its own.
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
`/api/v1/pages/{name}/versions/{commit}/ui/{path}` for the newest commit (§3.3), with
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
  delete(id: string, opts: { version: number }): Promise<void>
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
- **`records.<type>`**. A thin client over §5's routes. `put` with no `id` is a create
  (`POST`). `put` with an `id` is a replace and needs `version`: without it the host
  throws a `TypeError` before any request, so a page never sends a replace with no
  `If-Match` (which the route would refuse 428, §5.3). `delete` needs `version` the
  same way. The host never reads the current version for the page, because that would
  overwrite a change the page has not seen. `subscribe` follows the topic
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
  `core/input_schema.py` `schema_problems` on it (without `objects`, #5701). The subset then has no nested object, so a record
  is flat: scalars and arrays of scalars. A link to another record is a `string` id; the
  store does not check it (§6).
- A new version must accept every stored row of the type. The registration runs in one
  Postgres transaction that takes the type's lock exclusively (§5.4). In it, the API
  checks each stored body against the new schema and refuses with 409
  `records_do_not_fit` and the first 20 failing ids. Then it commits the version to git,
  writes the `record_types` row (§5.2), and commits the transaction, which releases the
  lock. So a version
  can add an optional field, widen a bound or add an `enum` value; it cannot rename a
  field that rows hold. A type holds at most `records_max_rows_per_type` rows (§5.6,
  default 10,000), and the check has a deadline (§5.2), so this check stays bounded.
- `GET /record-types` and `GET /record-types/{type}` return the newest schema and its
  commit.

A type belongs to no page. Every page that names it shares its rows: an intake form and
a dashboard over the same intake. The owner decided this on 2026-11-18 (§12, decision 3).

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

The fit check (§5.1) runs `resolve_inputs` on every stored body of the type (at most
`records_max_rows_per_type`) while it holds the exclusive lock, and each call has its
own patterns budget (P7). So the check has one total deadline, the setting
`records_fit_check_deadline_seconds` (§5.6, default 10 s). Past it, the registration (or
the repair) stops, writes nothing, and answers 409 `fit_check_too_slow` (the repair logs
it and puts back the schema in force, as for a row that does not fit). Writes to the type
wait at most that long. This is not
`flows/reconcile.py`'s repair, which indexes a commit and re-checks nothing. A registration whose
transaction fails after its git commit, while the API still runs, starts the same repair
at once and answers 503.

`records_newest` serves the list's order and cursor. A `where` filter is a scan of the
type's rows in that index: a type holds at most `records_max_rows_per_type` rows (§5.6),
so the scan is bounded, and no index on `body` is added. **(call)** An operator who raises
that setting far past its default makes this scan longer by the same factor; an index on
`body` is then a later change, not part of this spec.

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
2. The body is at most `records_max_body_bytes` as sent (§5.6, default 64 KiB), else
   413.
2a. A create only: the type holds fewer than `records_max_rows_per_type` rows (§5.6,
   default 10,000), else 409 `record_type_full`. The create counts the rows after it
   locks the type's `record_types` row `FOR UPDATE`, inside the shared lock of §5.4, so
   two creates of one type count one after the other and cannot both pass at one row
   below the limit. Replaces and deletes do not take that row lock.
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

### 5.6 The limits are settings

The owner decided that the three record limits are runtime settings with no maximum
(§12, decision 4). Each default is the value this spec had before.

| Setting | Environment variable | Default | Bounds | Read by |
|---|---|---|---|---|
| `records_max_rows_per_type` | `SCADBUDDY_RECORDS_MAX_ROWS_PER_TYPE` | 10000 | `int`, `gt=0`, no ceiling | a create's count (§5.3 step 2a) |
| `records_max_body_bytes` | `SCADBUDDY_RECORDS_MAX_BODY_BYTES` | 65536 (64 KiB) | `int`, `gt=0`, no ceiling | every create and replace (§5.3 step 2) |
| `records_fit_check_deadline_seconds` | `SCADBUDDY_RECORDS_FIT_CHECK_DEADLINE_SECONDS` | 10.0 | `float`, `gt=0`, `allow_inf_nan=False`, no ceiling | a registration and a repair (§5.2) |

Where they live. Phase 3 adds the three as fields of `core/settings.py` `Settings`. They
are not bootstrap fields, so they are env-seeded (P18): the environment variable seeds
each one, a value saved in Settings wins, and a reset puts back the environment's value
or the default. Phase 3 also adds them to `library/settings_store.py` `SettingsPatch`,
which checks each saved value with `check_value` (P19), to `APPLIES` as `live`, to
`core/setting_docs.py`, and to `frontend/src/pages/settings/fields.ts` `RUNTIME_FIELDS`
in a new `records` section. The doc text of `records_max_rows_per_type` in
`core/setting_docs.py` says that a value far past the default makes each `where` filter
scan longer, and that an index on `body` is a later change (§5.2; an issue to file), so
an operator does not look in Settings for an index that does not exist. The bounds are `Field` constraints on `Settings`, so a
deployment's environment variable and a value saved in Settings meet the same check.
`allow_inf_nan=False` refuses an infinite or NaN deadline: that is a check of the value,
not a maximum.

How each check reads them. Each check reads the setting from the process's running copy,
`state.settings`, as the upload cap does (P20). This adds no SQL read to a record write. A
check reads its value once, at its start, and keeps it to its end:

- A create or a replace reads `records_max_body_bytes` before it reads the body. It counts
  the bytes as it reads them and answers 413 at the first byte past the limit.
- A create reads `records_max_rows_per_type` before its transaction, then counts under the
  row lock of §5.3 step 2a.
- A registration or a repair reads `records_fit_check_deadline_seconds` when it takes the
  exclusive lock.

A saved change reaches every replica on the next `settings.changed` (P19), so the next
check uses it with no restart. A check in flight keeps the value it read.

What "no maximum" means. `check_value` refuses 0, a negative value and a value that is not
a number, with a 422 that names the environment variable. It takes any larger value. The
costs of a large value are the operator's call, and the setting's doc says them:

- More rows per type makes the fit check (§5.2) and a `where` scan (§5.2) longer. A
  registration can then hit the deadline and answer 409 `fit_check_too_slow`; the
  operator raises the deadline too.
- A larger body makes the API hold a larger body in memory while it checks it.
- A longer deadline makes writes to the type wait longer while a registration holds the
  exclusive lock (§5.4). A client may give up first. The registration still finishes or
  stops on its own, and a re-read of `GET /record-types/{type}` shows which.

A lower value never deletes or changes data. A type with more rows than a lowered row
limit keeps them; its creates are refused `record_type_full` until deletes bring it under
the limit, and its replaces and deletes still work. A stored body larger than a lowered
body limit stays readable; a replace of it must fit the new limit.

Who changes them. A person, in Settings, through `PUT /settings`. No agent tool writes
that route (P21). A page module can call `PUT /settings` as the viewer, as any back-office
module can (§7.2). That is the viewer's reach, not a new one.

Tests (phase 3):

- `tests/test_settings_coverage.py` and `tests/test_setting_docs.py` cover the three
  fields with no change to those tests. They fail when a field has no `APPLIES` entry, no
  patch field or no doc (P19).
- `check_value` refuses 0 and -1 for each field, and `inf` and `nan` for the deadline,
  each with its environment variable in the message. It accepts `10**9` rows, `10**9`
  bytes and `10**6` seconds, so no ceiling is set by mistake.
- A body of exactly `records_max_body_bytes` is accepted; one byte more is 413. A lowered
  limit applies to the next write after `PUT /settings`, with no restart, on the pattern
  of `tests/api/test_settings_runtime.py`.
- With `records_max_rows_per_type` at 2, the third create is 409 `record_type_full`, and a
  replace and a delete of a stored row still pass. A raised limit then admits the create.
  Two creates sent at once at one row below the limit: one passes and one is 409.
- With the limit lowered below the stored count, every row stays readable.
- With `records_fit_check_deadline_seconds` set very low over enough stored rows, a
  registration answers 409 `fit_check_too_slow`, writes no git commit and leaves
  `record_types` unchanged. The repair at start does the same and logs it.
- Frontend: the `records` section shows the three fields with their defaults and saves a
  changed value (`pages/settings/sections/*.test.tsx` pattern).

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
the page or record routes, and no step in which a person confirms a version (§7.4). The
back office is reached only through the LAN gateway and
Pomerium (`docs/security/trust-boundaries.md` "Zones"). The owner decided not to add
lockdowns of routes reachable only there.

### 7.4 Who writes a page

A person writes a version in the Pages editor or with `POST /pages`. The assistant writes
one with `page_register`, at `write`, as it writes a flow with `register_flow`. A version
records its author as a flow version does (`flows/reconcile.py` `created_by`), and the
versions list shows it.

No person confirms a version the assistant wrote. The owner decided this on 2026-11-18
(§12, decision 2). The newest version mounts at once, whoever wrote it. The page shell
shows the mounted version's commit and author in its heading, as information, not as a
gate.

The safeguards are these:

- **PR review.** Every version is a git commit, so a person can read its diff. The
  off-box push (phase 2, §3.1) copies the `pages` repository to the shop's remote, where
  the shop can review it with its own tools. The sample page of phase 5 (§10) is in this
  repo, so it goes through the repo's PR review and its `claude-review` gate.
- **Peer sessions.** Another assistant session reads a version with `page_get` and its
  files with the file routes, and reviews it before the shop relies on it.
- **Local checks.** These run automatically on every new version, whoever wrote it, and
  a failure writes no commit:
  - the declaration check of §3.2 (422): the keys, the slugs, each named record type
    present, the module present under `ui/` with a servable suffix;
  - the file checks of P2 on every path the version writes (no HTML, no dotfile, no path
    escape, the suffix allowlist);
  - for a record type version, `schema_problems` on the schema (§5.1) and the fit check
    of every stored row under the exclusive lock, within its deadline (§5.2);
  - on every record write the page later makes, the check at the route (§5.3): the type,
    the body limit, the row limit, and `resolve_inputs`.

  At mount, the loader keeps its 15 s deadline and reports a failure with a link to the
  versions, so a person can restore an older one (§3.4).

Nothing unsafe depended on the removed step:

- **Database access.** The rule of P15 never rested on the confirm. A page is browser
  code. No server process runs it, so it never runs in a pod that holds a database secret.
  It reaches data only through CRUD routes on the API, which check every input themselves
  (§7.1). The `records` table stays the API's own, with no grant.
- **Data integrity.** The checks above run in the API on every version and every write.
  They do not trust the page, and no person's confirm ever replaced them.
- **The assistant's tier.** This property is weaker, and the spec says so. The old step
  kept an assistant-written module from running with a person's reach until a person had
  seen it. Now a module the assistant wrote at `write` runs with the reach of each person
  who opens it (§7.2), as a module a person wrote does. The design still holds three
  things. First, a page runs only while a person has it open in a browser: no schedule,
  worker or flow mounts a page, so nothing runs it unattended. Second, the assistant's own
  tools keep their tiers: `page_register` is at `write`, and its outward tools still
  return a pending action for a person to approve. Third, every action is traced to its
  version: the commit and author show in the shell, the host labels each request with
  `X-ScadBuddy-Page: <name>@<commit>` on its span and activity rows (§4.1), and a restore
  (§3.3) brings back an older version. The label is for the reader, not a proof (§7.2).
  The back office is reached only through the LAN gateway and Pomerium (§7.3), so the
  people who open a page are the shop's own.

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
  declaration, Host API v2, the host elements, the record routes, and the limits with
  their settings and defaults (§5.6).
- A `.d.ts` for Host API v2 and the API's types, generated with the frontend's
  `gen:api`, is served at `GET /pages/_host/host.d.ts` for editors (`_host` is never a page
  name, §3.1) and the assistant.
- `models/CLAUDE.md`'s template checks do not cover pages. A page has no `verify.sh`.

## 10. Phases

Each phase is one PR series with a plan under `docs/superpowers/plans/`. Each lands
usable on its own. The owner approved this spec on 2026-11-18 and kept the build deferred
(§12, decision 1). No phase starts until the owner lifts the 2026-11-07 deferral.

1. **The loader and the file checks, shared** (frontend and backend refactor; no
   behaviour change). `template-ui/UiMount.tsx` from `TemplateUi.tsx`;
   `core/ui_files.py` from `api/template_ui.py`. The tests of both move with them, and
   `TemplateUi.test.tsx` passes unchanged.
2. **Pages** (backend, frontend, agent). `PageHistory`, `pages_repo_head`, `pages` in the off-box push, the routes of
   §3.3, `page.changed` and its two topics in `api/realtime.py`, the shell's `/p/:name/*` and nav, `PageUi`, Host API v2 without
   `records` (`api.request`, `flows`, `realtime`, `files`, `navigate`, `describe`),
   `UI_API_CURRENT` 2, the version's commit and author in the shell's heading (§7.4),
   agent `page_list`, `page_get`, `page_register`, mocks under `src/mocks/features/pages/`,
   the edge's 404 test and the lint list (§8). Its tests include: a version the assistant
   wrote mounts at once, with its author shown; a version that fails the declaration
   check or the file checks is 422 and writes no commit (§7.4 "Local checks").
3. **Records** (backend, frontend, agent). The `record_types` and `records` migration,
   record types in `_types/` with the routes of §5.1 and their reconcile, `records/store.py`,
   the routes of §5.3 with the check at the route and the lock of §5.4,
   `record.changed` and its topics, `host.records`, agent `record_*` and
   `record_type_*`, mocks. The three limit settings of §5.6 in `Settings`,
   `SettingsPatch`, `APPLIES`, `setting_docs.py` and the Settings page's `records`
   section, with the tests §5.6 lists.
4. **Host elements** (frontend). `<sb-table>`, `<sb-filter-bar>`, `<sb-drawer>`,
   `<sb-record-form>`; a template's `api: 2` with `records`.
5. **Authoring** (docs, agent). The guide's "Pages" section, the served `host.d.ts`,
   and one sample page in the tests' fixtures that a Playwright test mounts.

## 11. The orders spec's open decision, settled

The orders spec §12 left one question to §9: whether a "customer approved the quote"
step (`quoted` → `accepted`, made by the customer through a declared page) belongs in
the lifecycle.

The storefront settled it, without a declared page (P14). The customer accepts on two
paths, and both run as the customer actor:

- At checkout. When every cart line has a firm price, `Lifecycle.submit_order` records a
  `storefront` quote for each line (`_firm_quotes`), then accepts each quote and the
  order as the customer, in the submit's transaction (`orders/lifecycle.py:918-1028`,
  the accept at `:1016-1021`; called from `orders/storefront/submit.py` `submit_draft`,
  `:138`, `:194`; #6153). The customer never opens the order page for these quotes.
- On the order page of the storefront (#5283), for any quote that checkout did not
  accept. The edge forwards the accept to the CRUD service, which runs
  `Lifecycle.accept_quote` as the customer actor (`orders/storefront/order_page.py:896-915`).
- The accept that leaves every open item `approved` accepts the order as the customer, in
  the same transaction (`orders/lifecycle.py` `_accepted_by_the_last`, `:4662`;
  storefront spec §6.2).
- On a storefront order the shop cannot accept for the customer except with "Accept for
  the customer", which records a reason; any other shop accept is refused
  `customer_accepts` (`orders/lifecycle.py:4605-4621`; storefront spec §6.1).
- A firm line's price comes from a slice through the back office's quote path
  (`quote.py` `slice_and_price`), never from volume (#6153;
  `orders/storefront/slice_price.py`). #6581 (open, for #6574) makes the storefront's
  slice use the same request builder as a back-office quote (`bambuddy/choices.py`
  `opening_choices`); once it lands, the slice request matches too, not only the pricing.
  A by-hand line has no firm price: the shop quotes it in the back office, and the
  customer accepts that quote on the order page.

So the step is in the lifecycle, and declared pages do not add a second path to it. A
declared page that calls an accept route acts as the viewer, the shop, and the lifecycle
refuses it on a storefront order as above. This is not a guard against the page: "Accept
for the customer" is a shop route, so a page can still call it through `host.api.request`
(§7.2), as any back-office module can. The lifecycle then records it as the shop's accept
for the customer, with its reason, in the audit row `item.accepted_for_customer`
(`orders/lifecycle.py` `accept_for_customer`, `:4687-4700`). The orders spec §12 bullet now
carries a note that points here.

## 12. Decisions (owner, 2026-11-18)

The owner answered the open questions in a comment on PR #6575
(<https://github.com/eh-homelab/ScadBuddy/pull/6575#issuecomment-6971188522>).

1. **Merge the spec; the build stays deferred.** This spec is the design of #4254. No
   phase of §10 starts until the owner lifts the 2026-11-07 deferral.
2. **No person confirms each assistant-written version.** A version mounts at once,
   whoever wrote it. The safeguards are PR review, peer sessions and local checks. §7.4
   lists the checks that still run automatically on every new version, and says how the
   design still holds each security property.
3. **Record types are shared by every page.** A type belongs to no page; every page that
   names it shares its rows (§5.1).
4. **The limits are runtime settings with no maximum.** 10,000 rows per type, 64 KiB per
   body and the 10 s fit check are the defaults of `records_max_rows_per_type`,
   `records_max_body_bytes` and `records_fit_check_deadline_seconds` (§5.6).
