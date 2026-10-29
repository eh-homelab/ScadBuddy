# Tools & plugins page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent serves every source and item the assistant can use as one tree (`/api/v1/ai/capabilities`), the Tools & plugins area edits it with one shared row (an access select, the default marked, Reset, a one-line confirmation when lowering, details with the schema and raw JSON), and every registry tool carries a human `label`. Closes #2171 and #778.

**Architecture:** `agent/src/access/capabilities.ts` assembles the tree from a list of providers (`CapabilityProvider`): one for the four built-in sources over `AccessStore`/`AccessResolver` (PR 3), one for remote MCP plugins over `PluginRepo` and a new `ToolListings` store (`ai_tool_listings`, created in PR 3, filled here). `routes/capabilities.ts` serves the tree, a source, an item's details, the writes and a remote source's refresh. The frontend gets an `aiCapabilities` client, an msw feature mock, and `components/capabilities/` (row, confirmation, details, source card); the Built-in and Remote MCP plugins sections of `/settings/tools` move onto them. Plugin packages stay as they are; PR 5 adds a third provider.

**Tech Stack:** TypeScript (Node 24, Hono 4, zod 4, `@modelcontextprotocol/sdk`, postgres.js, vitest); React 19, Vite, vitest + Testing Library + msw, Playwright, shadcn/Radix primitives in `frontend/src/components/ui`.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` (§4.1, §4.2, §4.6, §4.7, §4.8, §5 item 4, §6). Issues #2171, #778. Read both issues with the spec: #778 fixes the remote rows' details (title, full description, parameters from `inputSchema`, `outputSchema`, annotations marked as server claims, View JSON of the unmodified `tools/list` entry).

**Base:** written against main at `ca6aa2108` (2026-10-29), after PR 2 (#4847, `/settings/:area`, `frontend/src/pages/settings/`) and PR 3 (#4838, `ai_access`, `ai_tool_listings`, `agent/src/access/*`). Line references are main's at `ca6aa2108`.

## Global Constraints

- Agent check: `cd agent && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- Frontend check: `cd frontend && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`; Playwright `pnpm exec playwright test`. Run `pnpm gen:api` once before a single `pnpm exec vitest run <file>`.
- Postgres tests need `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test` (`docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:17`), a role with `CREATEDB`.
- No agent migration: `ai_access` and `ai_tool_listings` exist (`agent/src/db/migrations/20261026T2200Z_access.sql`). If one turns out to be needed, it is a NEW file `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql` sorting after `20261026T2201Z_access_copy.sql`; never edit a merged one.
- Access values: a tool takes `off`, `read`, `write`, `outward`; a hook, skill or subagent takes `off`, `on` (spec §4.1). One select per item, never a tier select plus a separate switch.
- The declared tier is the default, never a floor. Lowering (choosing an access that ranks below the item's default) opens one confirmation line and that is the whole guard. No `USER_ONLY` (`frontend/src/agent/dom.ts:182`) on any access select, Reset, source switch or the confirmation; no `fixed` item beyond what is technically forced (a tool Claude Code renames, `harnessToolName`; tools colliding on one name); no extra warning for lowering an `outward` tool (#4208, closed by the user). New source switches use `SwitchField` with `userOnly={false}`.
- Access belongs to the tool: nothing in this plan reads or writes access per caller or per mode. The gates of PR 3 already read `ai_access` per call in classic, durable, flows and `/mcp`; this PR adds no gate.
- Writes stay settings writes the user makes: GET behind `uiReadProblem`, writes behind `uiRequestProblem` (`agent/src/routes/guard.ts`), JSON bodies only. `AccessStore` audits and sends the warm eviction (`AccessStore.write`, `access/store.ts:227-268`); remote writes go through `PluginRepo`, which dual-writes `ai_plugins` for one release and evicts (`plugins/registry.ts:574-643`).
- Rolling deploy: the old keys and columns are still written for one release (#4828 drops them). Never write `ai_access` for a `remote:` source except through `PluginRepo`, and never for a built-in source except through `AccessStore`.
- Every agent route an agent might call is in `AGENT_ROUTES` (`agent/src/tools/coverage.ts:400`) with a tool or a reason of more than 20 characters; `test/coverage.test.ts` checks each is served.
- Plugin descriptions, schemas and raw JSON are untrusted server text: render them as text only (no `dangerouslySetInnerHTML`, no markdown), and cap them on the agent before they are stored.
- Images are never involved; no base64 anywhere. Durable state is Postgres (`ai_tool_listings` is `jsonb`).
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed. The agent's routes are not in the OpenAPI document; their frontend types are written out in `frontend/src/api/capabilities.ts`, as `api/aiPlugins.ts` does.
- msw: new mocks are `frontend/src/mocks/features/capabilities.ts`, exporting `handlers` and `reset` (auto-discovered, #508).
- Commits: conventional titles (`feat(agent):`, `feat(frontend):`, `test(frontend):`, `docs:`), each ending with the session's attribution trailer.
- Not in this PR (PR 5, #4757): package sources and their items, package MCP server listing and access, hooks/skills/subagents on and off, the `builtin:browser` and package-server `*` switches, the derived-hash copy. Not in this PR (PR 7, #781): the MCP access and Audits areas, retiring the old routes.

## Review Focus

1. **A remote tool with a stored access that the last listing no longer has** (renamed or removed on the server, or a plugin never listed). Expected: it still shows as a row marked "Not in the last listing" with its access and Reset, never silently dropped while still enforced. Test: Task 4 `keeps an item that has a row but is not in the listing, marked missing`.
2. **A tool name that needs encoding** (`files.list`, `a/b`, a package server source `package:p/mcp:s` in PR 5). Expected: one path segment each way; the source and item arrive decoded. Test: Task 5 `routes an encoded source and item as one segment each`.
3. **A migrated row that lowered a registry tool** (PR 3's `migration:2171` rule ignores it). Expected: the page shows the tool at its default, not at the ignored row's tier, and choosing the default deletes the row. Test: Task 3 `shows a migrated row that lowers as the default, and choosing the default deletes it`.
4. **Cancelling the lowering confirmation, or Escape.** Expected: nothing is sent and the select shows the access it had. Test: Task 7 `cancelling the confirmation sends nothing and keeps the access`.
5. **A refresh against a plugin that is down or answers huge listings.** Expected: a failed refresh keeps the stored listing and its `listed_at`; an oversized description, schema or raw entry is capped, not stored whole. Tests: Task 4 `a failed refresh keeps the last listing`; Task 2 `caps an oversized tool before it is stored`.

Also covered where they belong: `http_request`'s by-method default (Task 3, Task 7), a fixed renamed/colliding remote tool refused with 409 (Task 4), an access an item kind does not take refused with 400 (Task 3).

---

## File map

Agent (new): `src/access/listings.ts` (`ListedTool`, `toListedTool`, `ToolListings`), `src/access/capabilities.ts` (views, `CapabilityProvider`, `Capabilities`), `src/access/providers.ts` (`builtInProviders`, `remoteProvider`), `src/routes/capabilities.ts`.
Agent (modified): `src/tools/registry.ts`, `src/tools/projections.ts`, every `src/tools/*.ts` with a tool, `src/plugins/testConnection.ts`, `src/plugins/registry.ts`, `src/routes/plugins.ts`, `src/tools/coverage.ts`, `src/main.ts`.
Frontend (new): `src/api/capabilities.ts`, `src/mocks/features/capabilities.ts`, `src/components/capabilities/{access.ts,schema.ts,ItemRow.tsx,ItemDetails.tsx,LowerConfirm.tsx,SourceCard.tsx,useCapabilities.ts}` and tests.
Frontend (modified): `src/api/aiPlugins.ts`, `src/pages/settings/sections/{builtin,remote}.tsx`, `src/components/settings/RemotePlugins.tsx`, `src/mocks/aiPlugins.ts`, `src/mocks/features/builtInToolSets.ts`; deleted: `src/components/{HttpRequestSetting,PrinterCameraSetting}.tsx` and their tests, `src/mocks/features/{httpRequest,printerCamera}.ts`.

---

### Task 1: `label` on every tool

**Files:**
- Modify: `agent/src/tools/registry.ts:240-301` (`ToolSpec.label`), `:303-327` (`Tool.label`), `:345-399` (`defineTool`)
- Modify: `agent/src/tools/projections.ts:138` (in-process), `:184-186` (`/mcp` `registerTool`)
- Modify: every file under `agent/src/tools/` that calls `defineTool` (29 files, 240 calls at `ca6aa2108`), plus `browser.ts:97` (`forwarded`'s `Forwarded<S>` gains `label`) and `sessions.ts:278` (`decideTool` labels its two tools)
- Test: `agent/test/coverage.test.ts`, `agent/test/mcp.test.ts`

**Interfaces:**
- Produces: `ToolSpec.label: string` (required); `Tool.label: string`; `Tool.annotations.title === label`. `/mcp` `tools/list` serves `title: label` at the top level and in `annotations`; the in-process server serves it in `annotations`. `Tool.title(args)` (#782, the panel's per-call title) is unchanged.

- [ ] **Step 1: Write the failing tests** in `coverage.test.ts`, a new `describe('tool labels')` over `DURABLE_TOOLS` (`tools/manifest.ts:31`):
  - `gives every tool a label`: `label.trim().length > 0`, `≤ 48` characters, first character upper case, no `_`, no trailing `.`, `label !== name`.
  - `labels are unique` (`new Set(labels).size === DURABLE_TOOLS.length`).
  - In `describe('openapi coverage over /mcp tools/list')`: `serves each label as the tool's title` (every listed tool's `title` and `annotations.title` equal the registry tool's `label`).
  - `mcp.test.ts`: `the in-process server's tools carry their label as annotations.title`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/coverage.test.ts test/mcp.test.ts`
Expected: FAIL (`label` is undefined).

- [ ] **Step 3: Implement.** Add the field, then write each label by hand (`pnpm typecheck` lists every tool without one). Style: sentence case, the action in plain words, proper names as written (`Bambuddy`, `3MF`, `README`, `MCP`, `OpenSCAD`). Examples: `send_to_bambuddy` "Send to Bambuddy", `get_printer_camera` "See a printer's camera", `list_models` "List models", `render_model` "Render a model", `confirm_action` "Confirm an approved action", `sessions_approve` "Approve a pending action", `browser_set_param` "Set a parameter in your tab", `http_request` is not a registry tool (Task 3 labels it). Duplicate labels across files are a test failure: tell them apart by what they act on.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm typecheck && pnpm vitest run test/coverage.test.ts test/mcp.test.ts test/builtInTools.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/tools agent/test/coverage.test.ts agent/test/mcp.test.ts
git commit -m "feat(agent): every tool has a label, served as its MCP title (#2171)"
```

---

### Task 2: Tool listings: the full `tools/list` entry, capped, stored

**Files:**
- Create: `agent/src/access/listings.ts`
- Modify: `agent/src/plugins/testConnection.ts:28-56` (`PluginToolView`), `:71-105` (`describeTools`)
- Modify: `agent/src/routes/plugins.ts:269-297` (the test route stores a good listing; the reveal/egress/test steps move to an exported `testAndList`)
- Test: `agent/test/listings.pg.test.ts` (new), `agent/test/plugins.registry.test.ts` (`describe('tool names as Claude Code sees them')`), `agent/test/plugins.routes.test.ts`

**Interfaces:**
- Consumes: `ai_tool_listings(source, tools jsonb, listed_at)` (PR 3); `remoteSource` (`access/model.ts`).
- Produces (`access/listings.ts`):
  - `export const LISTING_CAPS = { tools: 512, description: 8_192, schema: 65_536, raw: 65_536 } as const` (characters of JSON text).
  - `export type ListedTool = { name: string; title: string | null; description: string | null; annotations: Record<string, unknown> | null; input_schema: Record<string, unknown> | null; output_schema: Record<string, unknown> | null; raw: Record<string, unknown> | null; omitted: ('description' | 'input_schema' | 'output_schema' | 'raw')[] }`
  - `export function toListedTool(tool: Record<string, unknown>): ListedTool | undefined`: `undefined` without a string `name`; `title` from `title`, else `annotations.title`, when a string; each part over its cap is `null` (description cut to the cap) and named in `omitted`.
  - `export class ToolListings { constructor(sql: Sql); get(source: string): Promise<{ tools: ListedTool[]; listed_at: string } | undefined>; getMany(sources: readonly string[]): Promise<Map<string, { tools: ListedTool[]; listed_at: string }>>; put(source: string, tools: readonly ListedTool[]): Promise<void> }` (upsert, `listed_at = now()`; only the first `LISTING_CAPS.tools`). A row whose `tools` fail to parse reads as no listing.
- Produces (`testConnection.ts`): `PluginToolView` gains `title: string | null`, `input_schema`, `output_schema`, `raw` (`ListedTool`'s fields); `describeTools(plugin, tools: readonly Record<string, unknown>[])` reads them through `toListedTool`.
- Produces (`routes/plugins.ts`): `export async function testAndList(deps: Pick<PluginRouteDeps, 'plugins' | 'kek' | 'testPlugin' | 'resolveHost'> & { listings?: ToolListings }, name: string): Promise<{ status: 200 | 400 | 404 | 409; body: PluginTest | { detail: string } }>`; an `ok` test is stored with `listings.put(remoteSource(name), …)`, a failed one stores nothing. `PluginRouteDeps.listings?: ToolListings`; `AppDeps.listings?: ToolListings`, built in `main.ts` beside `AccessStore` (`main.ts:216`).

- [ ] **Step 1: Write the failing tests**
  - `listings.pg.test.ts`: `stores a listing and reads it back with listed_at`; `put replaces the last listing`; `caps an oversized tool before it is stored` (a 9,000-character description is cut to 8,192; a 70,000-character `inputSchema` is `null` and `omitted` names `input_schema`; a 600-tool listing keeps 512); `drops an entry without a name`; `getMany reads several sources in one query`; `forgetSource deletes the listing` (PR 3's `forgetSource`, `access/store.ts:61-64`).
  - `plugins.registry.test.ts`: `keeps title, input and output schema and the raw entry of each tool` (`describeTools` of `{ name: 'recall', title: 'Recall', inputSchema: {…}, outputSchema: {…}, x_new: 1 }` → `raw.x_new === 1`, `title === 'Recall'`); `takes annotations.title when there is no title`.
  - `plugins.routes.test.ts`: `a good connection test stores its listing`; `a failed connection test stores nothing`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/listings.pg.test.ts test/plugins.registry.test.ts test/plugins.routes.test.ts`
Expected: FAIL (`Cannot find module '../src/access/listings.js'`).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `pnpm vitest run test/plugins.e2e.test.ts test/plugins.untrusted.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access/listings.ts agent/src/plugins/testConnection.ts agent/src/routes/plugins.ts agent/src/main.ts agent/test/listings.pg.test.ts agent/test/plugins.registry.test.ts agent/test/plugins.routes.test.ts
git commit -m "feat(agent): a remote plugin's tools/list is stored in ai_tool_listings, capped (#778)"
```

---

### Task 3: The capabilities model and the built-in sources

**Files:**
- Create: `agent/src/access/capabilities.ts`, `agent/src/access/providers.ts`
- Test: `agent/test/capabilities.test.ts`

**Interfaces:**
- Consumes: Task 1 `Tool.label`; PR 3 `AccessReader`/`AccessWriter`, `AccessError`, `BUILTIN_SOURCES`, `registrySource`, `isRegistrySource`, `toolAccess`, `sourceEnabled` (`access/resolve.ts`); `ALL_TOOLS`; `HTTP_TOOL`, `HTTP_TOOL_NAME` (`harness/httpRequest.ts:80-82`); `BROWSER_ORIGINS_PATH` (`routes/browserOrigins.ts:46`); `z.toJSONSchema`.
- Produces (`access/capabilities.ts`):
  - `export type Access = ToolAccess | 'on'`; `export type ItemKind = 'tool' | 'hook' | 'skill' | 'subagent' | 'builtin'`; `export const ITEM_ACCESS: Record<ItemKind, readonly Access[]>` (`tool` and `builtin`: `off, read, write, outward`; the rest `off, on`).
  - `export type ItemView = { id: string; kind: ItemKind; name: string; harness_name: string; label: string; description: string | null; default: Access | null; default_note: string | null; suggested: Access | null; access: Access | null; overridden: boolean; missing: boolean; fixed: { access: Access; reason: string } | null }`. `default: null` means the default depends on the call, and `default_note` says how; `access: null` means no row (the default applies).
  - `export type SourceKind = 'builtin' | 'remote' | 'package' | 'package_server'`
  - `export type SourceView = { id: string; kind: SourceKind; title: string; enabled: boolean; switchable: boolean; options_route: string | null; listed_at: string | null; items: ItemView[]; children: SourceView[] }`
  - `export type ItemDetails = { item: ItemView; input_schema: Record<string, unknown> | null; output_schema: Record<string, unknown> | null; annotations: Record<string, unknown> | null; raw: Record<string, unknown> | null; omitted: string[] }`
  - `export class CapabilityError extends Error { constructor(message: string, readonly status: 400 | 404 | 409) }`
  - `export interface CapabilityProvider { owns(source: string): boolean; list(): Promise<SourceView[]>; get(source: string): Promise<SourceView | undefined>; setEnabled(source: string, enabled: boolean, context: AuditContext): Promise<void>; setItem(source: string, item: string, access: Access, context: AuditContext): Promise<void>; resetItem(source: string, item: string, context: AuditContext): Promise<void>; details(source: string, item: string): Promise<ItemDetails | undefined>; refresh?(source: string): Promise<{ test: PluginTest; source: SourceView }> }`
  - `export class Capabilities { constructor(providers: readonly CapabilityProvider[]); tree(): Promise<SourceView[]>; source(id: string): Promise<SourceView>; setEnabled(id, enabled, context): Promise<SourceView>; setItem(id, item, access, context): Promise<SourceView>; resetItem(id, item, context): Promise<SourceView>; details(id, item): Promise<ItemDetails>; refresh(id): Promise<{ test: PluginTest; source: SourceView }> }`. It dispatches to the first provider that `owns` the id (404 `no source "<id>"` otherwise), checks `access` against `ITEM_ACCESS[item.kind]` (400), refuses a `fixed` item (409, its reason), refuses `setEnabled` on a source that is not `switchable` (409), and treats `setItem` with the item's `default` as `resetItem`. Each write answers the source as it now stands.
- Produces (`access/providers.ts`): `export function builtInProviders(deps: { access: AccessReader & AccessWriter; tools?: readonly Tool[] }): CapabilityProvider` serving, in this order:
  - `builtin:scadbuddy` "ScadBuddy tools": every tool whose `registrySource` is it; `switchable: false`.
  - `builtin:browser` "Your browser tab": the `browser_*` tools; `options_route: BROWSER_ORIGINS_PATH`; `switchable: false` (its `*` is PR 5's).
  - `builtin:http` "HTTP requests": one item `http_request`, label "Make an HTTP request", `default: null`, `default_note: 'read for GET and HEAD, outward (asks first) for anything else'`; `switchable: true`.
  - `builtin:own-plugin` "ScadBuddy skills and subagents": `items: []` (PR 5 fills them); `switchable: true`.
  A registry item's `default` is its `risk`, its `access` is `toolAccess(risk, row, true)`, `overridden` is `access !== risk`; `harness_name` is `mcp__scadbuddy__<name>`. `enabled` is `sourceEnabled`. Writes are `AccessStore.setItem`/`resetItem`/`setEnabled`. `details` of a registry tool: `input_schema = z.toJSONSchema(z.object(tool.shape), { io: 'input', unrepresentable: 'any' })`, `annotations`, and `raw` as `/mcp` lists it (`{ name, title, description, inputSchema, annotations }`).

- [ ] **Step 1: Write the failing tests** (`capabilities.test.ts`, over `memoryAccess` and three `defineTool` stand-ins: `get_x` read, `put_x` write, `send_x` outward, plus `browser_click` write):
  - `lists the four built-in sources in order, each tool at its declared tier`
  - `a lowered outward tool reads back lowered, overridden` (`setItem('builtin:scadbuddy','send_x','write')` → item `{ access: 'write', default: 'outward', overridden: true }`, store row `access: 'write'`)
  - `a raised read tool reads back raised`
  - `off is stored and shown as off`
  - `setting an item to its default deletes its row`
  - `resetItem deletes the row`
  - `shows a migrated row that lowers as the default, and choosing the default deletes it` (row `send_x` `write` by `migration:2171` → `access: 'outward'`, `overridden: false`; `setItem(…, 'outward')` → no row)
  - `browser tools live in builtin:browser, with the origins route`
  - `http_request has a by-call default; any tier is stored` (`default: null`, `default_note` set; `setItem('builtin:http','http_request','outward')` stores a row; `resetItem` removes it)
  - `switches builtin:http off and on`; `refuses to switch builtin:scadbuddy (409)`
  - `refuses on for a tool, and a tier for nothing (400)`; `refuses an unknown source and an unknown registry tool (404)`
  - `details give each registry tool a JSON schema` (every `ALL_TOOLS` tool: `details(...)` resolves with a non-null `input_schema` of `type: 'object'`, no throw)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/capabilities.test.ts`
Expected: FAIL (`Cannot find module '../src/access/capabilities.js'`).

- [ ] **Step 3: Implement** `capabilities.ts` and the built-in provider.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/capabilities.test.ts test/accessVectors.test.ts test/accessResolver.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access/capabilities.ts agent/src/access/providers.ts agent/test/capabilities.test.ts
git commit -m "feat(agent): the capabilities tree, with ScadBuddy's built-in sources (#2171)"
```

---

### Task 4: Remote MCP plugins as capability sources

**Files:**
- Modify: `agent/src/plugins/registry.ts:216-225` (`PluginRepo.setToolAccess`), the `PluginStore` class (`:517-660`)
- Modify: `agent/src/access/providers.ts` (`remoteProvider`)
- Test: `agent/test/capabilities.remote.test.ts` (new, a memory `PluginRepo`), `agent/test/plugins.pg.test.ts`

**Interfaces:**
- Consumes: Task 2 `ToolListings`, `ListedTool`, `testAndList`; Task 3 `CapabilityProvider`, `ItemView`; `harnessToolName`, `toolPrefix`, `reservedNameProblem` (`plugins/registry.ts`); `describeTools` (`testConnection.ts:71`).
- Produces:
  - `PluginRepo.setToolAccess(name: string, tool: string, access: ToolAccess | null): Promise<PluginSummary>`: in one transaction under `SELECT … FOR UPDATE`, reads the stored tiers and disabled tools, applies the one change (`off` → disabled, a tier → `tool_tiers`, `null` → neither), then writes as `update` does: the `ai_plugins` columns first, then `writeTools`, then `notifyWarmEvict` (same order as `registry.ts:630-641`). 404 `PluginError` for an unknown plugin.
  - `export function remoteProvider(deps: { plugins: PluginRepo; listings: ToolListings; refresh: (name: string) => Promise<{ status: number; body: PluginTest | { detail: string } }> }): CapabilityProvider` owning `remote:<name>`. A source: `title` = name, `enabled` = `PluginSummary.enabled`, `switchable: true`, `listed_at` from the listing. Items: one per listed tool, through `describeTools({ name, toolTiers, disabledTools }, listing)`: `label` = `title ?? name`; `default: 'outward'`; `suggested: 'read'` on `readOnlyHint === true`; `fixed: { access: 'outward', reason: 'Claude Code renames this tool, so it cannot take a tier' }` for `renamed`, `{ access: 'off', reason: 'Shares its name with <others>, so both are hidden' }` for `collision`; then one item `missing: true` for each stored tool not in the listing (label = name, description null). Without a listing, only the stored tools. `setEnabled` → `plugins.update(name, { enabled })`; `setItem`/`resetItem` → `setToolAccess`; `details` from the listing (`raw` unmodified); `refresh` → `deps.refresh(name)`; a non-200 answer is a `CapabilityError` with its status and detail, and the stored listing stays.

- [ ] **Step 1: Write the failing tests**
  - `capabilities.remote.test.ts`: `lists a remote plugin's listed tools with label, description and suggestion`; `a plugin never listed shows only its stored tools`; `keeps an item that has a row but is not in the listing, marked missing`; `a renamed tool is fixed outward and a colliding pair fixed off, and writes to them are refused (409)`; `setItem off then read moves the tool between disabled_tools and tool_tiers`; `switching the source on calls update({ enabled: true })`; `refresh stores the new listing and answers the source`; `a failed refresh keeps the last listing` (the fake answers `{ status: 400, body: { detail: 'down' } }` → `CapabilityError` 400 `down`; `listed_at` unchanged); `details answer the raw entry as listed`.
  - `plugins.pg.test.ts`: `setToolAccess writes ai_plugins and ai_access, so an older replica reads the same` (after `setToolAccess(name,'b','off')` and `(name,'a','read')`: `ai_plugins.tool_tiers` `{ a: 'read' }`, `disabled_tools` `['b']`, and the `remote:<name>` rows match); `setToolAccess null removes the tool from both`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/capabilities.remote.test.ts test/plugins.pg.test.ts`
Expected: FAIL (`remoteProvider` and `setToolAccess` do not exist).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/plugins.access.pg.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/registry.ts agent/src/access/providers.ts agent/test/capabilities.remote.test.ts agent/test/plugins.pg.test.ts
git commit -m "feat(agent): remote MCP plugins as capability sources, listed and refreshed (#778)"
```

---

### Task 5: The capabilities routes, and access set through them honoured at every gate

**Files:**
- Create: `agent/src/routes/capabilities.ts`
- Modify: `agent/src/tools/coverage.ts:400-433` (`AGENT_ROUTES`), `agent/test/coverage.test.ts:112-114` (the path substitution)
- Modify: `agent/src/main.ts` (nothing new to construct beyond Task 2's `listings`; the route builds `Capabilities` from `deps`)
- Test: `agent/test/capabilities.routes.test.ts` (new), `agent/test/capabilities.gates.test.ts` (new)

**Interfaces:**
- Consumes: Tasks 2-4; `uiReadProblem`, `uiRequestProblem`, `UI_ACTOR`, `ready`, `RouteModule`.
- Produces (`routes/capabilities.ts`, `export const CAPABILITIES_PATH = '/api/v1/ai/capabilities'`, `export const route: RouteModule`):
  - `GET /api/v1/ai/capabilities` → `{ sources: SourceView[] }`
  - `GET …/{source}` → `SourceView`
  - `PUT …/{source}` body `{ enabled: boolean }` → `SourceView`
  - `PUT …/{source}/items/{item}` body `{ access: Access }` → `SourceView`
  - `DELETE …/{source}/items/{item}` → `SourceView`
  - `GET …/{source}/items/{item}` → `ItemDetails`
  - `POST …/{source}/refresh` (no body) → `{ test: PluginTest; source: SourceView }`; one refresh per source at a time (429 otherwise, as the test route does with its `testing` set (`routes/plugins.ts:266-270`)).
  - 503 `{ detail }` without `deps.access` or while not ready; `CapabilityError`/`AccessError`/`PluginError` → their status with `{ detail }`; a zod failure → 400. Without `deps.plugins` the remote provider is left out (the built-ins still answer). The audit context is `{ actor: UI_ACTOR, surface: 'http', clientIp }`.
  - `AGENT_ROUTES` gains all seven, each with a reason: reads "Settings → Tools & plugins for the user; an agent learns its own tools from its tool list, with each tier", writes "Changes what the assistant may do: a settings write the user makes; the agent cannot change its own access (spec §4.1)", refresh "Connects to the plugin with its stored secret to list its tools for the Settings page; the user runs it".

- [ ] **Step 1: Write the failing tests**
  - `capabilities.routes.test.ts`: `lists the built-in sources and the remote plugins`; `answers 503 without the database`; `refuses a write from another origin (403) and a non-JSON body (403)`; `refuses a read across sites (403)`; `PUT an item lowers it and answers the source`; `DELETE resets it`; `PUT a source switches it`; `answers 400 for an access the item does not take, 404 for an unknown source, 409 for a fixed item`; `routes an encoded source and item as one segment each` (`PUT /api/v1/ai/capabilities/remote%3Ahs/items/files.list` reaches `remote:hs`/`files.list`; `GET …/package%3Ap%2Fmcp%3As` answers JSON 404 `no source "package:p/mcp:s"`, not Hono's text 404); `refresh answers the test and the source, and a second refresh while one runs answers 429`; `audits a built-in write as the UI user` (the store's audit row `actor: UI_ACTOR`).
  - `capabilities.gates.test.ts` (#2171 acceptance, through the route and then each gate, `memoryAccess` behind both):
    - `a built-in outward tool lowered to write in Settings runs over /mcp without a pending approval` (`runToolWithOutcome` of an `outward` stand-in: no `pending_approval`)
    - `… is allowed by the classic seam` (`makeCanUseTool` → `allow`)
    - `… runs in the durable agent-tools activity with no approval` (`runAsActivity` → `ok`)
    - `… answers write from /internal/flows/tools`
    - `a remote tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call` (#778: `startFakeMcp()` with `a` and `b`, `PUT …/remote:hs/items/b {access:'off'}`, register `(await plugins.enabled(kek)).plugins[0]` on a `PluginForwarder`; `tools/list` names only `a`; `tools/call b` is a JSON-RPC error)
  - `coverage.test.ts`: `lists only routes the app serves` substitutes `{source}` with `builtin%3Ascadbuddy` and `{item}` with `get_model`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/capabilities.routes.test.ts test/capabilities.gates.test.ts test/coverage.test.ts`
Expected: FAIL (404 on every capabilities path).

- [ ] **Step 3: Implement** the route module and the coverage entries.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/capabilities.routes.test.ts test/capabilities.gates.test.ts test/coverage.test.ts test/routes.test.ts`, then the full agent check (Global Constraints).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/routes/capabilities.ts agent/src/tools/coverage.ts agent/src/main.ts agent/test/capabilities.routes.test.ts agent/test/capabilities.gates.test.ts agent/test/coverage.test.ts
git commit -m "feat(agent): /api/v1/ai/capabilities: every source and item, read and set (#2171)"
```

---

### Task 6: Frontend client, mock and access rules

**Files:**
- Create: `frontend/src/api/capabilities.ts`, `frontend/src/mocks/features/capabilities.ts`, `frontend/src/components/capabilities/access.ts`, `frontend/src/components/capabilities/schema.ts`
- Modify: `frontend/src/api/aiPlugins.ts:227` (export `request` as `aiRequest`), `frontend/src/mocks/aiPlugins.ts:189` (export `toolsFor`)
- Test: `frontend/src/api/capabilities.test.ts`, `frontend/src/components/capabilities/access.test.ts`, `frontend/src/components/capabilities/schema.test.ts`

**Interfaces:**
- Consumes: Task 5's routes and shapes (written out, as `api/aiPlugins.ts` does).
- Produces (`api/capabilities.ts`): the types `Access`, `ItemKind`, `ItemView`, `SourceKind`, `SourceView`, `ItemDetails`, `RefreshResult` (as Task 3/5), and `export const aiCapabilities = { list(): Promise<SourceView[]>; source(id): Promise<SourceView>; setEnabled(id, enabled): Promise<SourceView>; setItem(id, item, access): Promise<SourceView>; resetItem(id, item): Promise<SourceView>; details(id, item): Promise<ItemDetails>; refresh(id): Promise<RefreshResult> }`, each path segment `encodeURIComponent`ed.
- Produces (`access.ts`, pure):
  - `export const ACCESS_RANK: Record<Access, number> = { off: 0, read: 1, on: 1, write: 2, outward: 3 }`
  - `export function choicesOf(item: ItemView): { value: Access | 'default'; label: string }[]`: `ITEM_ACCESS` of its kind, the default's label suffixed ` (default)`, `outward` labelled `outward (asks first)`; for `default: null`, a first choice `{ value: 'default', label: 'By call (default)' }`.
  - `export function shownAccess(item: ItemView): Access | 'default'` (`fixed.access`, else `access` when overridden, else `default ?? 'default'`).
  - `export function lowers(item: ItemView, next: Access): boolean`: `ACCESS_RANK[next] < ACCESS_RANK[item.default ?? 'outward']` and `next !== shownAccess(item)`.
  - `export function summarize(source: SourceView): string` ("115 tools, 3 changed"; "1 tool"; "No tools listed yet" for an empty remote source; counts `overridden`).
  - `export function matches(item: ItemView, filter: string): boolean` (case-insensitive over `label`, `name`, `description`).
- Produces (`schema.ts`): `export type Param = { name: string; type: string; required: boolean; description: string | null; enum: string[] | null; default: string | null }`; `export function parametersOf(schema: unknown): Param[]` (`properties` and `required` of an object schema; `type` from `type`, else `enum` → `enum`, else `anyOf`/`oneOf` types joined with ` | `, else `any`; `default` as JSON text; anything that is not an object schema → `[]`).
- Produces (mock): state seeded with `builtin:scadbuddy` (`list_models` read, `save_model` write, `send_to_printer` outward, `get_printer_camera` read), `builtin:browser` (`browser_click` write, `options_route`), `builtin:http`, `builtin:own-plugin`, and one `remote:<name>` per `remotePluginList()` (items from a listing filled by refresh with `toolsFor`; `recall` suggested read, `files.list` fixed outward). Writes follow Task 3's rules (setting the default resets; 400/404/409 as the agent). `reset()` re-seeds.

- [ ] **Step 1: Write the failing tests**
  - `access.test.ts`: `marks the default and words outward`; `offers By call first for an item whose default depends on the call`; `lowering is below the default, raising is not` (`outward` default: `write` and `off` lower, `outward` does not; `read` default: `write` does not lower; `null` default: `read` lowers); `choosing what is shown is no lowering`; `summarizes changed items`; `filters by label, name and description`.
  - `schema.test.ts`: `lists properties with type, required, description, enum and default`; `joins anyOf types`; `answers no parameters for a schema that is not an object`.
  - `capabilities.test.ts` (msw): `encodes a source and an item into one segment each` (request URL ends `/capabilities/remote%3Ahindsight/items/files.list`); `round-trips a lowered item`; `a refresh fills a remote source's items`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm gen:api && pnpm exec vitest run src/api/capabilities.test.ts src/components/capabilities`
Expected: FAIL (modules not found).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 vitest command.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api frontend/src/mocks frontend/src/components/capabilities
git commit -m "feat(frontend): the capabilities client, its mock and the access rules (#2171)"
```

---

### Task 7: The shared row, the confirmation and the details

**Files:**
- Create: `frontend/src/components/capabilities/ItemRow.tsx`, `LowerConfirm.tsx`, `ItemDetails.tsx`, `SourceCard.tsx`, `useCapabilities.ts`
- Test: `frontend/src/components/capabilities/ItemRow.test.tsx`, `SourceCard.test.tsx`, `ItemDetails.test.tsx`

**Interfaces:**
- Consumes: Task 6 `aiCapabilities`, `choicesOf`, `shownAccess`, `lowers`, `summarize`, `matches`, `parametersOf`; `ChoiceSelect`, `SwitchField` (`components/settings/form.tsx`), `AlertDialog*`, `Collapsible*`, `Card`, `Input`.
- Produces:
  - `useCapabilities(filter: (s: SourceView) => boolean): { sources: SourceView[]; loading: boolean; error: Error | null; replace(source: SourceView): void; reload(): Promise<void> }` (one `aiCapabilities.list()` per mount).
  - `<ItemRow source={id} item={ItemView} onSaved={(s: SourceView) => void} />`: label, description (`line-clamp-2`), raw `harness_name` small and muted (`sb-num text-muted-foreground`); a `ChoiceSelect` `aria-label={`Access of ${item.name}`}` over `choicesOf(item)`, disabled when `fixed` (its reason shown beside it); "Reset" (`aria-label={`Reset ${item.name}`}`) only when `overridden`; a suggestion line when `suggested` differs from the access ("server suggests read"); "Not in the last listing" when `missing`. A choice applies at once (`setItem`, or `resetItem` for the default or `'default'`); a `lowers` choice first opens `LowerConfirm`. The row shows its own saving state and error.
  - `<LowerConfirm open label from to onConfirm onCancel />`: an `AlertDialog` titled `Lower ${label}?`, one line, by `to`: `It will run without asking first.` (`read`/`write` from `outward`), `It will be able to change things without asking.` (`write` from below), `The assistant will not be able to use it.` (`off`); buttons Cancel and `Lower to ${to}`. Nothing else: no checkbox, no extra warning (#4208).
  - `<ItemDetails source item />`: a disclosure (`Details of ${item.name}`) that loads `aiCapabilities.details` on first open: title, full description (`whitespace-pre-wrap`), parameters table from `parametersOf(input_schema)`, output schema's parameters when present, annotations as `key: value` with the line "Claims made by the server, not checked by ScadBuddy." for remote sources, any `omitted` part named ("too large to keep"), and "View JSON" toggling a read-only `<pre>` of `JSON.stringify(raw, null, 2)` with a Copy button. All text nodes; nothing parsed as HTML or markdown.
  - `<SourceCard source onChange headerExtra? children? />`: a foldable card (`aria-label={`Source ${source.title}`}`), folded by default: title, `SwitchField userOnly={false}` "Enabled" when `switchable`, `summarize(source)`, a filter `Input` (`aria-label={`Filter ${source.title}`}`) when it has more than 8 items, the rows, then `children` (options such as the browser origins, or a remote card's endpoint controls), then nested `source.children` as `SourceCard`s.

- [ ] **Step 1: Write the failing tests**
  - `ItemRow.test.tsx`: `shows label, description and the raw name, with the default marked`; `raising applies at once with no confirmation`; `lowering an outward tool asks one line, and confirming saves it`; `cancelling the confirmation sends nothing and keeps the access` (also Escape); `choosing the default resets the item`; `Reset appears only when overridden and resets`; `a fixed item's select is disabled and says why`; `By call (default) for http_request, and read asks to lower`; `shows the server's suggestion and a missing item`; `leaves every access control to the agent too` (`isUserOnly` is false for the select, Reset, and the confirmation's buttons, #4208).
  - `SourceCard.test.tsx`: `folds to its summary, and opens to its rows`; `the switch enables a switchable source and is absent otherwise`; `filters rows by label, name and description`; `nests child sources`.
  - `ItemDetails.test.tsx`: `loads details on first open and lists parameters`; `View JSON shows the raw entry unmodified`; `renders a description with markup as text` (`<img src=x onerror=…>` appears as text, no `img` element); `names an omitted part`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/capabilities`
Expected: FAIL (components not found).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/capabilities src/components/ui/ui-guard.test.ts src/agent/userOnly.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/capabilities
git commit -m "feat(frontend): one row for every tool: access, default, reset, lowering confirmation, details (#2171, #778)"
```

---

### Task 8: The Built-in section on the capabilities API

**Files:**
- Modify: `frontend/src/pages/settings/sections/builtin.tsx`
- Delete: `frontend/src/components/HttpRequestSetting.tsx`, `HttpRequestSetting.test.tsx`, `PrinterCameraSetting.tsx`, `PrinterCameraSetting.test.tsx`, `frontend/src/mocks/features/httpRequest.ts`, `printerCamera.ts`; in `frontend/src/api/client.ts` the four `get/put{HttpRequest,PrinterCamera}Setting` calls (`:2563-2570`, `:2692-2699`), and the `HttpRequestSetting` (`api/types.ts:476`) and `PrinterCameraSetting` (`:506`) interfaces
- Test: `frontend/src/pages/settings/sections/builtin.test.tsx` (new)

**Interfaces:**
- Consumes: Task 7 `useCapabilities`, `SourceCard`; `BrowserOriginsSetting` (unchanged, as the browser card's `children`); `useAiAvailability`; `setWebMcpEnabled`, `useWebMcpEnabled`.
- Produces: the section (`id 'builtin'`, area `tools`, order 10, unchanged) renders, with the agent available, `SourceCard`s for `builtin:scadbuddy`, `builtin:browser` (children: `<BrowserOriginsSetting />`) and `builtin:http`, then a card "This browser's agent (WebMCP)" holding today's WebMCP `SwitchField` and its help text unchanged, badged "This browser only". `builtin:own-plugin` is not shown here (its switch stays on the Packages section's built-in card until PR 5). Without the agent, only the WebMCP card.

- [ ] **Step 1: Write the failing tests** (`builtin.test.tsx`, `renderSettingsSection`):
  - `shows ScadBuddy's tools, the browser tab with its origin lists, and HTTP requests`
  - `the printer camera is a row: switching get_printer_camera off stores off` (`Access of get_printer_camera` → `off`, confirm → the mock holds `off`)
  - `lowers send_to_printer to write after the confirmation`
  - `turns HTTP requests off with the source switch`
  - `keeps the WebMCP switch, this browser only` (and `src/agent/webmcp.test.tsx` `the Settings toggle` passes unchanged)
  - `shows only WebMCP while the assistant is unavailable`

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/pages/settings/sections/builtin.test.tsx`
Expected: FAIL (no `Access of …` selects).

- [ ] **Step 3: Implement** the section and the deletions; `grep -rn "HttpRequestSetting\|PrinterCameraSetting\|getHttpRequestSetting\|getPrinterCameraSetting" frontend/src` prints nothing.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/pages/settings src/agent/webmcp.test.tsx && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src/pages/settings/sections/builtin.tsx frontend/src/pages/settings/sections/builtin.test.tsx frontend/src/components frontend/src/mocks/features frontend/src/api
git commit -m "feat(frontend): the Built-in section edits each tool's access; the camera is a row (#2171)"
```

---

### Task 9: The Remote MCP plugins section on the capabilities API

**Files:**
- Modify: `frontend/src/components/settings/RemotePlugins.tsx` (remove `BuiltInToolTable` `:57-125`, `ToolTable` `:127-143`, `BuiltInCard` `:169-235`, `ToolReview` `:335-420`; `RemoteCard` `:426-582` nests a `SourceCard`; `RemotePluginsPanel` `:584-636` lists remote plugins only)
- Modify: `frontend/src/api/aiPlugins.ts` (drop `updateBuiltIn` and `BuiltInToolSetPatch`; `BuiltInToolSet` stays for filtering `GET /plugins`), `frontend/src/mocks/features/builtInToolSets.ts` (drop its tighten-only `PATCH` handler and doc line)
- Modify: `frontend/src/pages/settings/sections/remote.tsx` (unchanged shape; it keeps rendering `RemotePluginsPanel`)
- Test: `frontend/src/components/settings/RemotePlugins.test.tsx`

**Interfaces:**
- Consumes: Task 7 `SourceCard`, `useCapabilities((s) => s.kind === 'remote')`; Task 6 `aiCapabilities.refresh`, `.source`.
- Produces: each `RemoteCard` (aria-label `Plugin endpoint <name>`, kept) shows the endpoint, auth, problem and usable badges as today, then the plugin's `SourceCard` (`remote:<name>`) with its switch, summary and rows, and in `headerExtra` the buttons Test connection and Remove. Test connection calls `aiCapabilities.refresh('remote:<name>')`, replaces the source, and shows the test's line (`testSummary`, kept) as a `role="status"` line folded under its summary (#3048), `truncated` warned as today. A new plugin's card fetches `aiCapabilities.source('remote:<name>')`; a removed one leaves both lists. The register form, the Remove dialog and their `USER_ONLY` marks stay as they are. The intro text drops "raise a tool's tier": "Remote MCP servers (Streamable HTTP over HTTPS) whose tools the assistant may use. Registered disabled: test the connection, set each tool's access, then enable it."

- [ ] **Step 1: Rewrite the tests**: keep `lists endpoints…`, `says when a stored endpoint is not loaded…`, `shows no problem…`, `registers an endpoint…`, `shows a refusal`, `removes an endpoint after confirming`. Replace `tests the connection, reviews tool tiers…`, `folds the tool review…`, `lists the built-in tool sets first…`, `offers only tiers at or above…` and `marks every write user-only` with:
  - `tests the connection, then lists each tool with its access (renamed tools fixed outward)`
  - `sets a tool off, which replaces the Hidden column` (no `Show …` switch exists; `Access of retain` → `off`, confirm; the mock holds `off`)
  - `lowering asks once, raising does not`
  - `enables the plugin with its source switch`
  - `folds the test line to its status (#3048)`
  - `lists no built-in tool sets here`
  - `marks the endpoint's own writes user-only and leaves access controls unmarked` (Test connection, Remove, Add endpoint, Header value user-only; the switch and every `Access of` select not)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/settings/RemotePlugins.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/settings src/pages/settings && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/settings/RemotePlugins.tsx frontend/src/components/settings/RemotePlugins.test.tsx frontend/src/api/aiPlugins.ts frontend/src/mocks frontend/src/pages/settings/sections/remote.tsx
git commit -m "feat(frontend): remote plugins' tools on the shared rows, off replacing Hidden (#778)"
```

---

### Task 10: Playwright and the docs

**Files:**
- Modify: `frontend/e2e/plugin-packages.spec.ts:125-138` (`tests a plugin endpoint and reviews its tool tiers`)
- Create: `frontend/e2e/tools-and-plugins.spec.ts`
- Modify: `docs/ai/operating.md:1148-1188` (the "Tool access" section), `README.md:1245-1266` (the plugin bullets and routes table)

**Interfaces:**
- Consumes: Tasks 8 and 9 (labels and aria names as produced there).

- [ ] **Step 1: Write the specs**
  - `plugin-packages.spec.ts`: the test becomes `tests a plugin endpoint and sets its tools' access`: Test connection → status `connected; 3 tools`; `Access of recall` shows `outward (asks first) (default)` with the suggestion line; `Access of files.list` disabled; `Access of retain` → `write` → the dialog `Lower retain?` → `Lower to write`; the source switch on; the badge `Enabled`.
  - `tools-and-plugins.spec.ts`: `lowers a built-in outward tool after one confirmation and resets it` (at `/settings/tools`, the ScadBuddy tools card, filter `send`, `Access of send_to_printer` → `write`, confirm, row shows Reset, Reset → back to `outward (asks first) (default)`); `opens a tool's details and its raw JSON`; `the agent's bridge may set an access too` (`bridgeCall(page, 'click', { role: 'combobox', name: 'Access of list_models' })` is not refused, #4208).

- [ ] **Step 2: Run them**

Run: `cd frontend && pnpm build && pnpm exec playwright test e2e/plugin-packages.spec.ts e2e/tools-and-plugins.spec.ts e2e/settings.spec.ts`
Expected: PASS.

- [ ] **Step 3: Docs.** `operating.md`: add a subsection "The capabilities API" listing the seven routes (Task 5), the guards, that remote writes go through `ai_plugins` for one release, the listing caps (`LISTING_CAPS`), and that a lowered item asks one confirmation in the UI and nothing more. `README.md`: the remote-plugin bullets say a tool is set `off`/`read`/`write`/`outward` in Settings → Tools & plugins, the old `tool_tiers`/`disabled_tools` fields stay on `PATCH /plugins/{name}` for one release, and the routes table gains `/api/v1/ai/capabilities`. Every external fact links its source (#259).

- [ ] **Step 4: Run the full checks** (agent and frontend, Global Constraints).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e/plugin-packages.spec.ts frontend/e2e/tools-and-plugins.spec.ts docs/ai/operating.md README.md
git commit -m "test(frontend): Tools & plugins end to end; docs: the capabilities API (#2171)"
```

---

## How PR 5 adds packages without reshaping this

- A third `CapabilityProvider`, `packageProvider`, is appended to the list `routes/capabilities.ts` builds; it `owns` `package:<name>` and `package:<name>/mcp:<server>`. Nothing in `Capabilities`, the routes or the frontend changes shape.
- A package's `SourceView` has `kind: 'package'`, its items are `hook`, `skill`, `subagent` and `builtin` (`ITEM_ACCESS` already gives them `off`/`on` or the four tiers), and its MCP servers are `children` with `kind: 'package_server'`, listed through Task 2's `ToolListings` and refreshed through the same `refresh` hook.
- The ids already route: Task 5 pins an encoded `package:p/mcp:s` as one segment.
- `AccessStore.setItem` accepts only a `ToolAccess` today (`checkTool`, `access/store.ts:36-39`); PR 5 widens it to `on` for package items, which the table's CHECK already allows.
- The frontend: the Packages section renders `useCapabilities((s) => s.kind === 'package')` with the same `SourceCard`, whose `children` already nest.

## Self-review

**Spec coverage**

| Spec item | Task |
|---|---|
| §4.1 one select per item, default not a floor, lowering asks one line | 3 (either direction), 6 (`lowers`), 7 (`LowerConfirm`) |
| §4.1 only renamed/colliding fixed, reason shown | 4 (`fixed`), 7 (disabled select with reason) |
| §4.2 sources: `builtin:scadbuddy`, `builtin:browser` (origins on its card), `builtin:http`, `builtin:own-plugin`, `webmcp`, `remote:<name>` | 3, 4, 8 |
| §4.2 the camera is `get_printer_camera`'s access | 8 (a row; the switch component deleted) |
| §4.2 package sources and items | PR 5 (see "How PR 5 adds packages") |
| §4.3 `ai_tool_listings` keeps name, title, description, annotations | 2 (plus #778's schemas and raw, capped) |
| §4.6 GET tree with `id, kind, name, harness_name, label, description, default, access, fixed`; source `enabled, switchable`, options route, `listed_at` | 3, 5 |
| §4.6 PUT source, PUT item, DELETE item, POST refresh reusing `testPlugin` | 4, 5 |
| §4.6 user-only settings writes (guard), audited, NOTIFY eviction | 5 (guards), `AccessStore`/`PluginRepo` (audit, eviction) |
| §4.6 old `PATCH /plugins` tier fields and switch routes answer from `ai_access` for one release | unchanged (PR 3 made them read `ai_access`); the UI stops calling them (8, 9) |
| §4.7 required `label`, MCP `title`, coverage test fails without one | 1 |
| §4.7 remote title, else `annotations.title`, else name | 2 (`toListedTool`), 4 (`label`) |
| §4.7 every row shows label, description, raw name | 7 |
| §4.8 three sections; foldable card with switch, summary, filter; nested children; shared row; Test connection kept and folding | 7, 8, 9 |
| §6 frontend: rows and lowering confirmation, msw under `src/mocks/features/`, Playwright | 6, 7, 10 |
| §6 agent: lowering a built-in accepted, honoured at each gate | 5 (`capabilities.gates.test.ts`) |
| #778 `none` replaces Hidden; tool absent from list and refused on call | 9 (no Show column), 5 (forwarder list and call) |
| #778 details: title, description, parameters, output schema, annotations as claims, View JSON, rendered as text | 2, 7 |
| #2171 acceptance 1 (built-in outward set to write runs without approval in chat, /mcp, durable) | 5 |
| #2171 acceptance 2-3 (package servers, hooks/skills/subagents) | PR 5 (#4757) |
| #2171 acceptance 4 (friendly name and description on every row) | 1, 4, 7 |

**Open points, with the resolution this plan takes (proposed, mark for review):**

1. *Proposed:* `webmcp` is not in the API: it lives in this browser's localStorage, so the Built-in section renders it beside the API's sources, badged "This browser only". It keeps its #254 user-only mark (`webmcp.test.tsx` stays): it is not an `ai_access` item, and #4208's rule covers the access controls.
2. *Proposed:* `http_request`'s default depends on the method, so its item has `default: null` with a `default_note`, and the select offers "By call (default)"; any tier chosen is stored, and lowering is judged against `outward`. `http_response_read` (always `read`) is not an item.
3. *Proposed:* each choice applies at once (one PUT per item), with no Save button and no section dirty state; the old tables' "Save tool settings" goes. A write answers the source, so the summary stays right.
4. *Proposed:* choosing an item's default deletes its row (as `validateOverrides` treats "its own tier is no override"); for a remote tool that means choosing `outward` resets it.
5. *Proposed:* item details are a separate read (`GET …/items/{item}`), so the tree stays small with 200-plus registry tools; a registry tool's schema is `z.toJSONSchema` of its input shape.
6. *Proposed:* listings also keep `input_schema`, `output_schema` and the raw entry (#778), capped by `LISTING_CAPS` (512 tools; 8 KiB description; 64 KiB per schema and per raw entry), with what was dropped named in `omitted`.
7. *Proposed:* Test connection on the page is the refresh route; the old `POST /plugins/{name}/test` also stores a good listing, so either fills the page.
8. *Proposed:* remote item writes go through `PluginRepo.setToolAccess`, not `AccessStore`, so the `ai_plugins` columns an older replica reads stay right for the release before #4828; they are not audited, as remote plugin writes are not today.
9. *Proposed:* `builtin:own-plugin` is in the API (switchable, no items yet), but its switch stays on the Packages section's built-in card until PR 5 gives it items, so the page never shows two switches for it.
10. *Proposed:* the `builtin:browser` source is listed with `switchable: false` until PR 5 adds its `*` switch.
11. *Proposed:* the agent's own routes get `AGENT_ROUTES` reasons, not tools: an agent cannot change its own access (§4.1), and it reads its tiers from its tool list.
12. *Proposed:* the PR body says `Fixes #2171` and `Fixes #778`, as spec §5 item 4 says, and names #4757 for #2171's acceptance bullets 2 and 3.
