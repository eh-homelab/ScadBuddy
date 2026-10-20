# Tool access resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One stored access per item (`ai_access`) and one `AccessResolver` behind every gate in both runtimes and backend flows, so a tool's access is the user's in either direction, the declared tier is only the default, the printer camera is `get_printer_camera` set `off`, and a change applies from the next call, including mid-turn and mid-run. No UI change.

**Architecture:** Two agent migrations add `ai_access` (overrides only, `item = '*'` for a source's `enabled`) and `ai_tool_listings` (created only), and copy every old key and column into `ai_access`. `agent/src/access/` holds the model, the store, pure resolution functions and `AccessResolver`; today's interfaces (`ToolOverrides`, `effectiveTool`, `BuiltInPolicy`, `RemotePlugin.toolTiers`/`disabledTools`, `pluginTierResolver`, `ownPluginEnabled`) stay and are fed from it. The rule is pinned to agent-durable by shared vectors (`agent/test/fixtures/access-vectors.json`), and the classic seam, the durable workflow and `_live_tier` ask per call.

**Tech Stack:** TypeScript (Node 24, Hono, postgres.js, vitest, @temporalio/* pinned exactly), Python 3.14 agent-durable (psycopg, temporalio, pytest), Python 3.12 backend (temporalio, temporal-agent-harness, pytest).

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` (§2 "Tool access", §4.1 to §4.4, §5 item 3, §6). Read it with this plan; the working copy carries #4209's edits.

## Global Constraints

- Agent check: `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- Agent-durable check: `cd agent-durable && ../.github/scripts/agent-durable-pin.sh . && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest`.
- Backend check: `cd backend && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest -n auto`.
- Postgres tests need `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test` (`docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:17`), a role with `CREATEDB`.
- Temporal tests need `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` naming a `temporal` CLI (or `temporal` on `PATH`); backend may use `SCADBUDDY_TEST_TEMPORAL_ADDRESS` with `matching.maxTaskQueuesInDeploymentVersion` raised.
- Agent migrations are NEW files in `agent/src/db/migrations/` named `$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`, slug `[a-z0-9_]`; never edit, rename or remove a merged file.
- `@temporalio/*` stay pinned exactly, all one version; nothing in this plan bumps them.
- A change to `DurableSession` (agent-durable) or `ProjectWorkflow`'s host functions (backend) goes behind `workflow.patched(...)`; `ProjectWorkflow` must replay `backend/tests/fixtures/project_workflow_histories/`, and a new history is added beside them (never re-record one).
- uv: use the image's uv (`uvx uv@0.13.1`, the Dockerfile's `ghcr.io/astral-sh/uv:0.13.1`); never run `uv lock` (local 0.6.16 rewrites `uv.lock`).
- Kill processes only by pid or process group; never `pkill -f` or `killall`.
- Nothing caches access: reads stay per call, or per turn where they are per turn now (tool lists, warm fingerprints); the per-call reads this plan adds are one `ai_access` query each.
- The #2100 and #2088 tests (`agent/test/builtInTools.test.ts:281`, `:410`, `:421` block, `toolActivities*.test.ts`, `agent-durable/tests/test_tool_overrides.py`, `test_session_workflow.py:943-1046`) keep their names and assertions. Only their rigs change (they seed `ai_access` instead of `ai_settings`), and only the tighten-only assertions named in Tasks 4 and 11 are replaced.
- Messages keep their current text where tests match it: `disabled in Settings → Plugins`, `cannot be read`, `raised to "outward" during this turn`.
- Not in this PR: the capabilities API and UI, `label` on tools, filling `ai_tool_listings`, package hooks/skills/subagents items, subagent parking (`ASK_THE_MAIN_AGENT` stays), removing the http-request/printer-camera routes.

## Review Focus

1. **A migrated built-in entry that no longer raises.** Under tighten-only a stored `write` for a tool whose code later became `outward` was ignored; copied as is it would now lower that tool silently. Rule: a row with `updated_by = 'migration:2171'` on a registry source counts only where it raises (Task 3 vector `a migrated row that lowers is ignored`; Task 11 replays it in Python).
2. **The migration on a database with none of the old keys, or with malformed ones.** It must create the tables, copy nothing, and not fail (Task 1 `copies nothing from a database with none of the old keys`, `skips malformed values`).
3. **An `ai_access` row naming a tool that no longer exists, or an access value that is no access.** It must be ignored: never offered, never a crash, never a default changed (Task 3 vectors `a row naming no tool is ignored`, `an unknown access value is ignored`; Task 4 `ignores a stored row naming no tool`).
4. **A tool lowered mid durable turn.** The next call must not park (Task 12 `test_a_tier_lowered_mid_turn_stops_parking_on_the_next_call`), and one raised mid-turn must park (`test_a_tier_raised_mid_turn_parks_the_next_call`).
5. **A remote tool set `off` still reachable.** It must be refused by the forwarder and left out of the turn's listing, from the store's own answer (Task 6 `a tool set off in ai_access is refused by the forwarder`).

Also covered where they belong: the Python port disagreeing with TypeScript (Task 11 opens every vector), the camera off seen by flows and the durable gate (Tasks 5 and 11), a lowered tier mid flow run (Task 13).

---

### Task 1: The two agent migrations

**Files:**
- Create: `agent/src/db/migrations/<stamp1>_access.sql`
- Create: `agent/src/db/migrations/<stamp2>_access_copy.sql`
- Test: `agent/test/access.migration.pg.test.ts`

**Interfaces:**
- Consumes: `migrate`, `MIGRATIONS` (`src/db/migrations.ts`); `throwawayDatabase({ empty: true })` (`test/support/postgres.ts`).
- Produces:
  - `ai_access(source text, item text, access text, updated_at timestamptz NOT NULL DEFAULT now(), updated_by text NOT NULL, PRIMARY KEY (source, item))`, with `CHECK (source ~ '^(builtin|remote|package):.+$')`, `CHECK (access IN ('off','on','read','write','outward'))`, `CHECK (item <> '*' OR access IN ('on','off'))`, `CHECK (item = '*' OR access <> 'on' OR source LIKE 'package:%' OR source = 'builtin:own-plugin')`.
  - `ai_tool_listings(source text PRIMARY KEY CHECK (source ~ '^(remote|package):.+$'), tools jsonb NOT NULL CHECK (jsonb_typeof(tools) = 'array'), listed_at timestamptz NOT NULL DEFAULT now())`.
  - Every copied row has `updated_by = 'migration:2171'`.

- [ ] **Step 1: Name the files.** Run `date -u +%Y%m%dT%H%MZ` for `<stamp1>`. For `<stamp2>`, one minute later so the copy sorts after the tables: `date -u -d '+1 minute' +%Y%m%dT%H%MZ` (GNU `date`, Linux); on macOS/BSD `date`, `date -u -v+1M +%Y%m%dT%H%MZ`. Create both files empty, so the test can name their ids.

- [ ] **Step 2: Write the failing test** (`describe.skipIf(!TEST_DATABASE_URL)`, `beforeEach` migrates everything before `TABLES`, as `imagesByPointer.migration.pg.test.ts` does)

```ts
const TABLES = '<stamp1>_access'
const COPY = '<stamp2>_access_copy'
it('copies nothing from a database with none of the old keys', async () => {
  await migrate(db.sql, MIGRATIONS)
  expect(await db.sql`SELECT * FROM ai_access`).toHaveLength(0)
  expect(await db.sql`SELECT * FROM ai_tool_listings`).toHaveLength(0)
})
it('copies builtin_tools.scadbuddy: tiers, disabled tools as off, browser tools to builtin:browser', async () => {
  await setting('builtin_tools.scadbuddy', {
    tool_tiers: { get_model: 'outward', browser_click: 'outward', send_to_bambuddy: 'write' },
    disabled_tools: ['get_model', 'list_models'],
  })
  await migrate(db.sql, MIGRATIONS)
  expect(await rows()).toEqual([
    { source: 'builtin:browser', item: 'browser_click', access: 'outward', updated_by: 'migration:2171' },
    { source: 'builtin:scadbuddy', item: 'get_model', access: 'off', updated_by: 'migration:2171' }, // disabled wins
    { source: 'builtin:scadbuddy', item: 'list_models', access: 'off', updated_by: 'migration:2171' },
    { source: 'builtin:scadbuddy', item: 'send_to_bambuddy', access: 'write', updated_by: 'migration:2171' },
  ])
})
it('copies each ai_plugins row: enabled as *, its tool_tiers, its disabled_tools as off', async () => {
  // two plugins: 'hs' enabled with {recall: 'read', retain: 'write'} and disabled ['retain']; 'gh' disabled, no tiers
  expect(await rows('remote:%')).toEqual([
    { source: 'remote:gh', item: '*', access: 'off', updated_by: 'migration:2171' },
    { source: 'remote:hs', item: '*', access: 'on', updated_by: 'migration:2171' },
    { source: 'remote:hs', item: 'recall', access: 'read', updated_by: 'migration:2171' },
    { source: 'remote:hs', item: 'retain', access: 'off', updated_by: 'migration:2171' },
  ])
})
it.each([
  ['http_request_enabled', { source: 'builtin:http', item: '*', access: 'off' }],
  ['printer_camera_enabled', { source: 'builtin:scadbuddy', item: 'get_printer_camera', access: 'off' }],
  ['scadbuddy_plugin_enabled', { source: 'builtin:own-plugin', item: '*', access: 'off' }],
])('copies %s only when it is stored false', async (key, row) => { /* false → [row]; true or absent → [] */ })
it('skips malformed values', async () => {
  // builtin_tools.scadbuddy = "x"; then {tool_tiers: [], disabled_tools: {}}; then {tool_tiers: {a: 'none'}, disabled_tools: [3, 'b']}
  // → no rows, then no rows, then only { item: 'b', access: 'off' }
})
it('leaves the old keys and columns in place', async () => { /* ai_settings keys still there; ai_plugins.tool_tiers still a column */ })
it('refuses a source item whose access does not fit it', async () => {
  await migrate(db.sql, MIGRATIONS)
  await expect(db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('builtin:http', '*', 'read', 't')`).rejects.toThrow(/check/)
  await expect(db.sql`INSERT INTO ai_access (source, item, access, updated_by) VALUES ('builtin:scadbuddy', 'get_model', 'on', 't')`).rejects.toThrow(/check/)
})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/access.migration.pg.test.ts`
Expected: FAIL (`relation "ai_access" does not exist`).

- [ ] **Step 4: Write the two migrations**

`<stamp1>_access.sql`: the two tables above, with a header comment naming issue #2171 and spec §4.3. `<stamp2>_access_copy.sql`: plain `INSERT … SELECT … ON CONFLICT (source, item) DO NOTHING`, in this order so a disabled tool wins: disabled tools as `off` first, then tiers. Guard every `jsonb_each_text` / `jsonb_array_elements` with `jsonb_typeof(...) = 'object'` / `'array'` and keep only string elements and values in `('read','write','outward')`. The source of a registry tool is `CASE WHEN item LIKE 'browser\_%' THEN 'builtin:browser' ELSE 'builtin:scadbuddy' END`. `ai_plugins.disabled_tools` is `text[]` (`unnest`); `enabled` becomes `'on'`/`'off'` for every row. A switch is copied only when `value = 'false'::jsonb`. Nothing is deleted or altered.

- [ ] **Step 5: Run it to verify it passes**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/access.migration.pg.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/src/db/migrations/<stamp1>_access.sql agent/src/db/migrations/<stamp2>_access_copy.sql agent/test/access.migration.pg.test.ts
git commit -m "feat(agent): ai_access and ai_tool_listings, with the old settings copied in (#2171)"
```

---

### Task 2: The access model and `AccessStore`

**Files:**
- Create: `agent/src/access/model.ts`, `agent/src/access/store.ts`
- Create: `agent/test/support/memoryAccess.ts`
- Modify: `agent/src/app.ts` (`AppDeps.access`), `agent/src/main.ts:206-215` (construct it)
- Test: `agent/test/accessStore.pg.test.ts`, `agent/test/warmEvict.pg.test.ts` (one case added)

**Interfaces:**
- Consumes: the tables from Task 1; `notifyWarmEvict` (`sessions/warmEvict.ts`); `AuditSink`, `AuditContext` (`audit/log.ts`).
- Produces (`access/model.ts`):
  - `export const TOOL_ACCESS = ['off', 'read', 'write', 'outward'] as const; export type ToolAccess = (typeof TOOL_ACCESS)[number]`
  - `export function isToolAccess(v: unknown): v is ToolAccess`
  - `export const SOURCE_ITEM = '*'`, `export const MIGRATED_BY = 'migration:2171'`
  - `export const BUILTIN_SOURCES = { scadbuddy: 'builtin:scadbuddy', browser: 'builtin:browser', http: 'builtin:http', ownPlugin: 'builtin:own-plugin' } as const`
  - `export function remoteSource(name: string): string` (`remote:<name>`), `packageSource(pkg: string): string`, `packageServerSource(pkg: string, server: string): string` (`package:<pkg>/mcp:<server>`)
  - `export function registrySource(tool: string): string` (`browser_*` → `builtin:browser`, else `builtin:scadbuddy`)
  - `export type AccessRow = { source: string; item: string; access: string; updated_by: string }`
  - `export type StoredOverrides = { tool_tiers: Record<string, RiskTier>; disabled_tools: string[] }` (moved here from `plugins/builtInTools.ts:39`, which re-exports it)
- Produces (`access/store.ts`):
  - `export type AccessReader = { rows(sources: readonly string[]): Promise<AccessRow[]> }`
  - `export type AccessWriter = { setItem(source: string, item: string, access: ToolAccess, context?: AuditContext): Promise<void>; resetItem(source: string, item: string, context?: AuditContext): Promise<void>; setEnabled(source: string, enabled: boolean, context?: AuditContext): Promise<void>; replaceTools(source: string, tools: Readonly<Record<string, ToolAccess>>, context?: AuditContext): Promise<void>; forget(source: string): Promise<void> }`
  - `export class AccessError extends Error { readonly status = 400 }`
  - `export class AccessStore implements AccessReader, AccessWriter { constructor(sql: Sql, audit?: AuditSink) }`
  - Transaction helpers PluginStore uses: `export function writeTools(tx: TransactionSql, source: string, tools: Readonly<Record<string, ToolAccess>>, by: string): Promise<void>`, `writeEnabled(tx: TransactionSql, source: string, enabled: boolean, by: string): Promise<void>`, `forgetSource(tx: TransactionSql, source: string): Promise<void>`, `updatedBy(context?: AuditContext): string` (`<actor.kind>:<actor.id>`, `system` without one).
  - `AppDeps.access?: AccessStore | undefined` in `app.ts` (shared by the plugins, plugin packages, http-request and printer-camera routes).
- Produces (`test/support/memoryAccess.ts`): `memoryAccess(initial?: AccessRow[]): { access: AccessReader & AccessWriter; rows: AccessRow[]; reads: { n: number } }` (`reads.n` counts `rows()` calls) and `rowsFromOverrides(source: string, tool_tiers: Record<string, string>, disabled_tools: string[], updated_by?: string): AccessRow[]` (disabled wins, as the migration copies; `updated_by` defaults to `browser:browser`).

- [ ] **Step 1: Write the failing tests**

`accessStore.pg.test.ts`:
- `stores an override and reads it back by source` (`setItem('builtin:scadbuddy','get_model','read', ctx)` then `rows(['builtin:scadbuddy'])` equals one row with `updated_by: 'browser:browser'`)
- `resetItem deletes the override, so the default applies`
- `setEnabled writes '*' as on or off`
- `replaceTools replaces every tool row of the source and leaves '*'`
- `forget deletes the source's rows and its ai_tool_listings row`
- `refuses on for a tool, a tier for '*', and an unknown source with AccessError` (`status === 400`, nothing stored)
- `audits each write as a settings row` (`kind: 'settings'`, `action: 'ai_access'`, `detail: 'builtin:scadbuddy/get_model = "read"'`)

`warmEvict.pg.test.ts`: `an ai_access write tells every cache` (a LISTEN on `WARM_EVICT_CHANNEL` hears `*` after `setItem`; nothing for a write that rolled back).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/accessStore.pg.test.ts test/warmEvict.pg.test.ts`
Expected: FAIL (`Cannot find module '../src/access/store.js'`).

- [ ] **Step 3: Implement `access/model.ts`, `access/store.ts`, `test/support/memoryAccess.ts`**, and construct `new AccessStore(database.sql, audit)` in `main.ts` beside `SettingsStore`, passed to `createApp` as `access`. Every write runs in one transaction with `notifyWarmEvict(tx, '*')`, then audits as `SettingsStore.set` does (`credentials.ts:643-680`). Item-kind checks: `'*'` takes only `setEnabled`; `setItem` takes a `ToolAccess`; the source must match `^(builtin:(scadbuddy|browser|http|own-plugin)|remote:[a-z][a-z0-9-]*|package:.+)$`.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/accessStore.pg.test.ts test/warmEvict.pg.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access/model.ts agent/src/access/store.ts agent/src/app.ts agent/src/main.ts agent/test/support/memoryAccess.ts agent/test/accessStore.pg.test.ts agent/test/warmEvict.pg.test.ts
git commit -m "feat(agent): AccessStore over ai_access, audited, evicting warm processes (#2171)"
```

---

### Task 3: Resolution rules, `AccessResolver` and the shared vectors

**Files:**
- Create: `agent/src/access/resolve.ts`, `agent/src/access/resolver.ts`
- Create: `agent/test/fixtures/access-vectors.json` (written by the test)
- Test: `agent/test/accessVectors.test.ts`, `agent/test/accessResolver.test.ts`

**Interfaces:**
- Consumes: Task 2's `AccessRow`, `AccessReader`, `StoredOverrides`, `registrySource`, `MIGRATED_BY`, `isToolAccess`; `RiskTier`, `RISK_TIERS` (`harness/permissions.ts`); `ToolOverrides`, `ToolOverride`, `Tool` (`tools/registry.ts`); `ALL_TOOLS`.
- Produces (`access/resolve.ts`, pure, no I/O):
  - `export function toolAccess(declared: RiskTier, row: AccessRow | undefined, migratedRaisesOnly: boolean): ToolAccess`: the row's access when it is a `ToolAccess`; a row whose `updated_by === MIGRATED_BY` on a registry source counts only when `off` or above `declared`; otherwise `declared`.
  - `export function registryOverrides(tools: readonly { name: string; risk: RiskTier }[], rows: readonly AccessRow[]): StoredOverrides`: rows of `builtin:scadbuddy` and `builtin:browser` only, each row taken from the tool's own `registrySource`; `tool_tiers` holds every effective tier that differs from the tool's own (either direction), `disabled_tools` every `off`, sorted; rows naming no tool, or of another source, are ignored.
  - `export function pluginOverrides(source: string, rows: readonly AccessRow[]): { toolTiers: Record<string, RiskTier>; disabledTools: string[] }` (no migrated rule: remote tiers were any direction).
  - `export function enabledByDefault(source: string): boolean` (`builtin:*` true; `remote:*` and `package:*` false).
  - `export function sourceEnabled(source: string, rows: readonly AccessRow[]): boolean` (`'*'` row `on`/`off`, else `enabledByDefault`; a `'*'` row with any other value is ignored).
- Produces (`access/resolver.ts`):
  - `export class AccessResolver { constructor(reader: AccessReader | undefined, tools: readonly Tool[] = ALL_TOOLS); registry(): Promise<StoredOverrides>; registryOverrides(): ToolOverrides; plugin(source: string): Promise<{ toolTiers: Record<string, RiskTier>; disabledTools: string[] }>; enabled(source: string): Promise<boolean>; item(source: string, item: string, declared: RiskTier): Promise<ToolAccess> }`. Without a reader everything is at its default. `registry()` is one `rows([builtin:scadbuddy, builtin:browser])` read; `registryOverrides().of(tool)` reads once per call and answers `{ tier?: effective tier when it differs, disabled }`; a read that throws propagates (the gates fail closed).
- Produces (vectors file): `{ "tool_access": [...], "migrated_by": "migration:2171", "registry_source": { "<tool>": "<source>" }, "registry": [{ "name", "durable_only"?, "tools": [{ "name", "risk", "hitl" }], "rows": [AccessRow], "expect": StoredOverrides }], "sources": [{ "name", "source", "rows": [AccessRow], "expect": boolean }] }`. A `hitl: "answer"` tool is in no Settings source: the TypeScript side drops it before calling `registryOverrides`. `durable_only` marks a vector whose declared tier is no tier (#3882), which TypeScript skips by name.

- [ ] **Step 1: Write the failing tests**

`accessVectors.test.ts` builds the document in code, writes it with `UPDATE_ACCESS_VECTORS=1` (as `payloadCodec.test.ts:94` does), and checks:

```ts
it('match the committed file', () => {
  if (process.env.UPDATE_ACCESS_VECTORS === '1') writeFileSync(FILE, `${JSON.stringify(built, null, 2)}\n`)
  expect(JSON.parse(readFileSync(FILE, 'utf8'))).toEqual(built)
})
it.each(built.registry.filter((v) => !v.durable_only))('registry: $name', (v) => {
  expect(registryOverrides(v.tools.filter((t) => t.hitl !== 'answer'), v.rows)).toEqual(v.expect)
})
it.each(built.sources)('source: $name', (v) => expect(sourceEnabled(v.source, v.rows)).toBe(v.expect))
it('names each tool\'s registry source', () => {
  for (const [tool, source] of Object.entries(built.registry_source)) expect(registrySource(tool)).toBe(source)
})
```

Registry vectors, by name: `no rows: every tool at its own tier`; `a raise of a read tool` (`get_model` read → `outward`); `a lowering of an outward tool` (`send_to_bambuddy` → `write`, `updated_by: 'browser:browser'`); `off disables`; `a tool's own tier is no override`; `a row naming no tool is ignored`; `an unknown access value is ignored` (`'none'`, `'on'`); `a migrated row that lowers is ignored` (`send_to_bambuddy` `write` by `migration:2171` → no entry); `a migrated row that raises counts`; `a migrated off counts`; `browser tools come from builtin:browser` (a `browser_click` row under `builtin:scadbuddy` is ignored); `a row of another source is ignored` (`remote:hs`); `an answer tool is in no source` (`ask_user` hitl `answer` with a row → nothing); `durable_only: a migrated raise of a tool whose tier is no tier counts` (risk `"?"`, row `outward` by migration → `{ tool_tiers: { t: 'outward' } }`).
Source vectors: `http is on by default`, `http off`, `the own plugin off`, `a remote plugin is off by default`, `a remote plugin on`, `a '*' row holding a tier is ignored`.

`accessResolver.test.ts` (over `memoryAccess`):
- `answers defaults without a reader`
- `registryOverrides reads once per call and answers a lowered tier` (`of('send_to_bambuddy')` → `{ tier: 'write', disabled: false }`, `reads.n === 1`)
- `registryOverrides().disabled() lists every off registry tool`
- `item() answers outward for an unknown remote tool and the stored access for a known one`
- `a read that fails rejects` (reader `rows` rejects → `registry()` rejects)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/accessVectors.test.ts test/accessResolver.test.ts`
Expected: FAIL (`Cannot find module '../src/access/resolve.js'`).

- [ ] **Step 3: Implement `access/resolve.ts` and `access/resolver.ts`**, then write the file once with `UPDATE_ACCESS_VECTORS=1 pnpm vitest run test/accessVectors.test.ts`.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/accessVectors.test.ts test/accessResolver.test.ts`
Expected: PASS, and `git status` shows `agent/test/fixtures/access-vectors.json` new.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access/resolve.ts agent/src/access/resolver.ts agent/test/accessVectors.test.ts agent/test/accessResolver.test.ts agent/test/fixtures/access-vectors.json
git commit -m "feat(agent): AccessResolver and the access vectors agent-durable shares (#2171)"
```

---

### Task 4: ScadBuddy's own tools on the resolver, in either direction

Every registry gate reads through `effectiveTool` and `ToolServices.toolOverrides`: `runJudgedByResult` (`tools/registry.ts:504-534`), `/mcp` (`tools/projections.ts:160`, `refreshOfferedTools` `:230-247`), the `agent-tools` activities (`temporal/toolActivities.ts:411-453`), `flow_tool_tier` and `/internal/flows/tools` (`temporal/flowTools.ts:29-59`), the subagent loopback (`routes/subagentMcp.ts:107-134`); and the classic turn's per-turn `BuiltInPolicy` (`plugins/builtInTools.ts:140`, `harness/run.ts:396-404`, read at `sessions/turnRunner.ts:289-291`). Swapping what feeds those two interfaces moves all of them; their code is untouched except `effectiveTool`.

**Files:**
- Modify: `agent/src/tools/registry.ts:117-143` (`ToolOverride` doc, `effectiveTool`)
- Modify: `agent/src/plugins/builtInTools.ts` (header comment `:21-26`; `sanitise` `:70-86` removed; `validateOverrides` `:89-122`; `BuiltInPolicy` doc `:129-138`; `builtInPolicy` `:140-158`; `BuiltInTools` `:184-256`)
- Modify: `agent/src/main.ts:215`, `agent/src/main.ts:259`; `agent/src/routes/plugins.ts:323` (`new BuiltInTools(deps.access)`)
- Test: `agent/test/builtInTools.test.ts`

**Interfaces:**
- Consumes: Task 2 `AccessReader`, `AccessWriter`, `StoredOverrides`, `BUILTIN_SOURCES`, `registrySource`; Task 3 `registryOverrides`, `AccessResolver`.
- Produces:
  - `effectiveTool(tool: Tool, services: Pick<ToolServices, 'toolOverrides'>): Promise<EffectiveTool>` (same signature): `risk = override?.tier ?? tool.risk`; `gated = risk === 'outward' && (tool.gated || tool.risk !== 'outward')`; `disabled = override?.disabled === true`.
  - `class BuiltInTools { constructor(access: (AccessReader & Partial<AccessWriter>) | undefined, sets?: readonly BuiltInToolSet[]) }`, same methods: `overrides(set)` = `registryOverrides(set.tools, await access.rows([...registry sources]))`; `update(set, patch, context)` writes `replaceTools` per registry source (a tool's own tier is not stored; a disabled tool is `off`; disabled wins); `registryOverrides()` delegates to `new AccessResolver(access, …).registryOverrides()`; `policy()` unchanged in shape.
  - `validateOverrides(set, patch, current): StoredOverrides` (same signature): known tools and valid tiers only; a lowered tier is accepted.
  - `BuiltInPolicy.tierOf(harnessName, tier)` answers the stored tier where there is one (either direction), else `tier`; `raised` keeps its name and now lists every changed tier (the warm fingerprint's input, `turnRunner.ts:218`).

- [ ] **Step 1: Move the test rigs onto `ai_access`, and write the new tests**

Rig changes only (no `it` name or `expect` line changes): `memorySettings`/`withOverrides` (`:33-63`) become `memoryAccess(rowsFromOverrides('builtin:scadbuddy', tool_tiers, disabled_tools))`; each `values.set(builtInToolsSetting('scadbuddy'), X)` (`:300`, `:330`, the `rig.set` at `:446`) becomes `access.replaceTools('builtin:scadbuddy', …)` with the same overrides; `rig`'s `reads.n` (`:428-436`) counts `memoryAccess`'s `rows()` calls; `new BuiltInTools({ get: reject, set })` (`:352`) becomes a reader whose `rows` rejects; `app(settings)` (`:120-135`) also passes `access`. `toolActivities.test.ts` and `toolActivities.pg.test.ts` get the same rig change wherever they seed `builtin_tools.scadbuddy`.

Replaced (tighten-only assertions, by spec §6 "lowering a built-in accepted"):
- `ignore a stored entry that would lower a tier or names no tool` (`:74`) becomes `ignores a stored row naming no tool, and keeps a lowered tier`: rows `{write tool: read, nope: outward, read tool: write}` → `{ tool_tiers: { [writeTool.name]: 'read', [readTool.name]: 'write' }, disabled_tools: [] }`.
- the two lowering rows of `answers 400 for %s` (`:170-176`) leave its table; new test `accepts a lowered tier and stores it in ai_access`: PATCH `{ tool_tiers: { [outwardTool.name]: 'write' } }` → 200, `view.tool_tiers` equals `{ [outwardTool.name]: 'write' }`, the store holds `{ source: 'builtin:scadbuddy', item: outwardTool.name, access: 'write' }`.
- `accepts a raised tier and a disabled tool, and stores them in ai_settings` (`:159`) is renamed `… stores them in ai_access`, its `values.get(builtInToolsSetting(...))` assertion now reading the store's rows.

New tests:
- `a lowered outward tool runs over /mcp without a pending approval` (`runToolWithOutcome` of a `defineTool` stand-in with `risk: 'outward'` and no required input, under a `write` row for it → outcome not `refused`, no `pending_approval` in the result)
- `the harness seam allows a lowered outward tool` (`canUseTool(OWN(outward))` → `allow`)
- `the durable activities run a lowered outward tool with no approval recorded` (`activities({ whoami-as-outward: 'write' }, [])` on a `whoami` defined `risk: 'outward'` → `ok`)
- `/internal/flows/tools answers a lowered tier` (`flowToolTiers` → `{ name, tier: 'write', disabled: false }`)
- `a migrated row that lowers is ignored by every gate` (row `updated_by: 'migration:2171'`, `{ outwardTool: 'write' }` → `effectiveTool(...).risk === 'outward'`)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/builtInTools.test.ts test/toolActivities.test.ts test/flowTools.test.ts`
Expected: FAIL (`BuiltInTools` reads `ai_settings`; lowering answered 400).

- [ ] **Step 3: Implement** the changes listed under Produces; `main.ts` builds `new BuiltInTools(access)` and `toolOverrides: builtInTools.registryOverrides()`. Remove `builtInToolsSetting` only if nothing imports it after the rig change; `evictsWarmOnWrite` (`sessions/warmEvict.ts:28-35`) keeps its keys (old routes may still write them for one release).

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/builtInTools.test.ts test/toolActivities.test.ts test/flowTools.test.ts test/subagentMcp.test.ts test/coverage.test.ts`, then `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/toolActivities.pg.test.ts`
Expected: PASS, including `refuses a call raised to outward after the turn read its policy` (#2100) and `refuse a disabled tool raised to outward as disabled, not as unapproved (#2088)`.

- [ ] **Step 5: Commit**

```bash
git add agent/src/tools/registry.ts agent/src/plugins/builtInTools.ts agent/src/main.ts agent/src/routes/plugins.ts agent/test/builtInTools.test.ts agent/test/toolActivities.test.ts agent/test/toolActivities.pg.test.ts agent/test/flowTools.test.ts
git commit -m "feat(agent): ScadBuddy's own tools take their access from ai_access, in either direction (#2171)"
```

---

### Task 5: The printer camera as `get_printer_camera` access `off`

**Files:**
- Delete: `agent/src/tools/switches.ts`
- Modify: `agent/src/tools/registry.ts:111-112` (`switchedOff` removed from `ToolServices`), `:525-526` (the switch step removed from `runJudgedByResult`; the order becomes unreadable, disabled, principal tier, #2100 seam check, prepare/confirm)
- Modify: `agent/src/main.ts:104`, `:256-257`
- Modify: `agent/src/routes/printerCamera.ts:40-90` (reads and writes `deps.access`)
- Test: `git mv agent/test/toolSwitches.test.ts agent/test/printerCameraAccess.test.ts`; `agent/test/flowTools.test.ts`

**Interfaces:**
- Consumes: Task 2 `AccessStore` (`AppDeps.access`), `BUILTIN_SOURCES.scadbuddy`; Task 4's `BuiltInTools.registryOverrides`.
- Produces: `PRINTER_CAMERA_SETTING_PATH` unchanged; `GET` answers `{ enabled: access !== 'off' }` for `('builtin:scadbuddy', 'get_printer_camera')`; `PUT {enabled:false}` → `setItem(…, 'off')`, `PUT {enabled:true}` → `resetItem(…)`; 503 without `deps.access`. `PrinterCameraRouteDeps.settings` becomes `access: AccessStore | undefined`.

- [ ] **Step 1: Rewrite the tests**

In `printerCameraAccess.test.ts`, the `toolSwitches` describe is replaced by:
- `refuses get_printer_camera once it is off, before the backend is reached` (`runToolWithOutcome(tool('get_printer_camera'), …)` → `refused`, detail `/disabled in Settings/`, no request reached msw)
- `refuses get_printer_camera when its access cannot be read` (reader rejects → detail `/cannot be read/`)
- `runs get_printer_camera at its own tier without a database`

The route tests keep their names; their `repo()` becomes `memoryAccess()`, and `GET after PUT false answers enabled false` now asserts the store row `{ item: 'get_printer_camera', access: 'off' }`; `PUT true` asserts the row is gone.

In `flowTools.test.ts`: `flow_tool_tier answers the camera off as disabled` and `/internal/flows/tools lists the camera off as disabled`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/printerCameraAccess.test.ts test/flowTools.test.ts`
Expected: FAIL (the camera is still a switch read from `ai_settings`; flows see it enabled).

- [ ] **Step 3: Implement** as listed under Files and Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/printerCameraAccess.test.ts test/flowTools.test.ts test/builtInTools.test.ts && pnpm typecheck`
Expected: PASS; `grep -rn "switchedOff\|toolSwitches" agent/src` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add -A agent/src/tools/switches.ts agent/src/tools/registry.ts agent/src/main.ts agent/src/routes/printerCamera.ts agent/test/toolSwitches.test.ts agent/test/printerCameraAccess.test.ts agent/test/flowTools.test.ts
git commit -m "feat(agent): the printer camera is get_printer_camera's access, seen by every gate (#2171)"
```

---

### Task 6: Remote MCP plugins on `ai_access`

**Files:**
- Modify: `agent/src/plugins/registry.ts:365-376` (`pluginTierResolver` body), `:400-445` (`SUMMARY_COLUMNS`, `summary`, `openPlugin`), `:505-640` (`create`, `update`, `delete`, `enabled`)
- Test: `agent/test/plugins.pg.test.ts`, `agent/test/plugins.access.pg.test.ts` (new)

**Interfaces:**
- Consumes: Task 2 `writeTools`, `writeEnabled`, `forgetSource`, `updatedBy`, `remoteSource`; Task 3 `pluginOverrides`, `sourceEnabled`.
- Produces (all signatures unchanged): `PluginSummary.enabled`, `.tool_tiers`, `.disabled_tools` and `RemotePlugin.toolTiers`, `.disabledTools` now come from `ai_access` rows of `remote:<name>` (read with the row in one query: a correlated `jsonb_agg` of `ai_access` as `access_rows`); `create`/`update` write them with `writeTools`/`writeEnabled` in the same transaction (disabled wins over a tier), and keep writing `ai_plugins.enabled`/`tool_tiers`/`disabled_tools` too, for one release (`evictsWarmOnWrite`, Task 4, keeps its old key the same way): a rolling deploy's older replica still reads those columns, and a plugin an older replica creates after `<stamp2>_access_copy` ran gets both, so neither reads as off by default; `delete` calls `forgetSource` and still deletes the `ai_plugins` row (its columns go with it). `enabled(kek)` keeps only plugins whose `sourceEnabled` is true. `pluginTierResolver(plugins)` keeps its signature: its answer for a tool is `plugin.toolTiers[tool] ?? 'outward'`, as now. The next release removes the old columns and this dual write.

- [ ] **Step 1: Write the failing tests**

`plugins.pg.test.ts`:
- `stores tool_tiers, disabled_tools and enabled in ai_access, and keeps writing them to ai_plugins for one release` (after `update(name, { enabled: true, tool_tiers: { a: 'read' }, disabled_tools: ['b'] })`: `ai_access` holds `*` on, `a` read, `b` off; `ai_plugins.enabled` is `true`, `.tool_tiers` is `{ a: 'read' }`, `.disabled_tools` is `['b']`)
- `enabled() lists a plugin whose ai_access says on, with its tiers and disabled tools from ai_access` (even where the dual-written `ai_plugins` columns disagree, `ai_access` wins)
- `a plugin switched on before the migration stays on` (seed the old columns, run `MIGRATIONS`, `enabled()` lists it)
- `delete forgets the plugin's access rows`

`plugins.access.pg.test.ts`:
- `a tool set off in ai_access is refused by the forwarder` (a `startFakeMcp()` with tools `a` and `b`; `update(name, { disabled_tools: ['b'] })`; register `(await store.enabled(kek)).plugins[0]` on `PluginForwarder.start()`; `tools/call b` → JSON-RPC error `ScadBuddy: …`; `tools/call a` reaches the fake)
- `turn_tool_tier answers outward for an unlisted tool and the stored tier for a listed one` (through `turnServers` with the store's plugins)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/plugins.pg.test.ts test/plugins.access.pg.test.ts`
Expected: FAIL (`ai_access` holds no `remote:` rows).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/plugins.pg.test.ts test/plugins.access.pg.test.ts test/plugins.routes.test.ts test/plugins.registry.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/registry.ts agent/test/plugins.pg.test.ts agent/test/plugins.access.pg.test.ts
git commit -m "feat(agent): remote MCP plugins' enabled, tiers and disabled tools live in ai_access (#2171)"
```

---

### Task 7: `http_request` and ScadBuddy's own plugin on `ai_access`

**Files:**
- Modify: `agent/src/plugins/packages/builtins.ts:56-60`, `:122-164` (`builtInEnabled`, `ownPluginEnabled`, `BuiltInPackages`)
- Modify: `agent/src/sessions/turnRunner.ts:300-305`, `:410-425`; its deps type gains `access?: AccessResolver`
- Modify: `agent/src/temporal/turnServers.ts:88`, `:218`, `:247` (deps gain `access?: AccessResolver`)
- Modify: `agent/src/routes/httpRequest.ts:50-85`; `agent/src/routes/plugins.ts:324`, `agent/src/routes/pluginPackages.ts:339` (`new BuiltInPackages(deps.access)`); `agent/src/plugins/builtInTools.ts:203`
- Modify: `agent/src/main.ts` (one `new AccessResolver(access)` handed to the turn runner, the turn servers and `forwardPackageServers` callers)
- Test: `agent/test/httpRequestRoutes.test.ts`, `agent/test/builtins.pg.test.ts`, `agent/test/harnessWiring.test.ts`

**Interfaces:**
- Consumes: Task 2 `AccessStore`, `BUILTIN_SOURCES`; Task 3 `AccessResolver.enabled`, `.item`.
- Produces:
  - `builtInEnabled(access: AccessReader | undefined, b: BuiltIn): Promise<boolean>` and `ownPluginEnabled(access: AccessReader | undefined): Promise<boolean>` (source `builtin:own-plugin`); `class BuiltInPackages { constructor(access: AccessReader & AccessWriter) }`, `setEnabled` writes `setEnabled('builtin:own-plugin', …)`.
  - `export function httpAccessTierOf(override: ToolAccess | undefined): TierResolver` in `harness/httpRequest.ts`: the override where it is a tier, else `httpTierOf`. A turn offers `http_request` only when `enabled('builtin:http')` and its item is not `off`.
  - `/api/v1/ai/settings/http-request`: `GET` = `enabled('builtin:http')`; `PUT` = `setEnabled('builtin:http', enabled)`.

- [ ] **Step 1: Write the failing tests**

- `httpRequestRoutes.test.ts`: the existing round trips keep their names on `memoryAccess()`; new `PUT false stores builtin:http '*' off in ai_access`.
- `builtins.pg.test.ts`: `switching ScadBuddy's own plugin off stores builtin:own-plugin '*' off, and ownPluginEnabled answers false`.
- `harnessWiring.test.ts`: `a turn offers no http_request when builtin:http is off`; `a turn offers no http_request when its item is off`; `an http_request item set to outward parks a GET`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/httpRequestRoutes.test.ts test/builtins.pg.test.ts test/harnessWiring.test.ts`
Expected: FAIL (the switches are read from `ai_settings`).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/httpRequestRoutes.test.ts test/builtins.pg.test.ts test/harnessWiring.test.ts test/httpRequest.test.ts test/pluginPackages.test.ts`
Expected: PASS; `grep -rn "SETTING_HTTP_REQUEST\|SETTING_OWN_PLUGIN" agent/src` shows only their definitions.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/builtins.ts agent/src/sessions/turnRunner.ts agent/src/temporal/turnServers.ts agent/src/routes/httpRequest.ts agent/src/routes/plugins.ts agent/src/routes/pluginPackages.ts agent/src/plugins/builtInTools.ts agent/src/harness/httpRequest.ts agent/src/main.ts agent/test/httpRequestRoutes.test.ts agent/test/builtins.pg.test.ts agent/test/harnessWiring.test.ts
git commit -m "feat(agent): http_request and ScadBuddy's own plugin are switched in ai_access (#2171)"
```

---

### Task 8: Package MCP servers' tiers and disabled tools from the resolver

**Files:**
- Modify: `agent/src/plugins/packages/mcp.ts:69-75`, `:125-133`
- Modify: `agent/src/temporal/turnServers.ts:347-352`
- Modify: `agent/src/main.ts:407`, `:615` (pass the resolver)
- Test: `agent/test/pluginPackages.test.ts`, `agent/test/pluginPackages.temporal.test.ts`

**Interfaces:**
- Consumes: Task 2 `packageServerSource`; Task 3 `AccessResolver.plugin`; `pluginTierResolver` (Task 6).
- Produces: `forwardPackageServers(loaded, forwarder, resolve?, options?: Pick<ForwardOptions, 'record'> & { access?: AccessResolver })`: each server's `RemotePlugin.toolTiers`/`disabledTools` (the forwarded one and the `HarnessPlugin` pushed at `:133`) are `await access.plugin(packageServerSource(pkg.name, server))`, `{}`/`[]` without it. In `turnServers.ts` a forwarded package server takes `tierOf: (name) => pluginTierResolver([plugin])(name) ?? 'outward'` and `listingKey: JSON.stringify(plugin.disabledTools)`, as a remote plugin does at `:182-192`.

- [ ] **Step 1: Write the failing tests**

- `pluginPackages.test.ts`: `a package server's tool set read in ai_access is read in a classic turn, and one set off is refused by the forwarder`.
- `pluginPackages.temporal.test.ts`: `turn_tool_tier answers a package server tool's stored tier, and outward when none is stored`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/pluginPackages.test.ts test/pluginPackages.temporal.test.ts`
Expected: FAIL (every package tool answers `outward`; nothing is refused).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/pluginPackages.test.ts test/pluginPackages.temporal.test.ts test/pluginPackageKinds.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/mcp.ts agent/src/temporal/turnServers.ts agent/src/main.ts agent/test/pluginPackages.test.ts agent/test/pluginPackages.temporal.test.ts
git commit -m "feat(agent): package MCP servers take their tools' access from ai_access (#2171)"
```

---

### Task 9: The classic seam decides each call from access as it is now

Spec §4.4 (#4209): a lowered tier stops parking on the turn's next call, a raised one starts. The seam's resolver is per turn today (`harness/run.ts:396-404`); it gains one per-call read, and records what it applied so the in-process server's #2100 check compares against this call's decision.

**Files:**
- Create: `agent/src/access/live.ts`, `agent/src/harness/seamLedger.ts`
- Modify: `agent/src/harness/permissions.ts:208-260` (`makeCanUseTool`, `makePreToolUseHook`)
- Modify: `agent/src/harness/run.ts` (`HarnessRun.live`, passed at `:415`)
- Modify: `agent/src/tools/projections.ts:87-131` (`seamTierOf(tool, toolUseId)`), `agent/src/tools/harness.ts:40-76`
- Modify: `agent/src/sessions/turnRunner.ts` (builds one `SeamLedger` and `liveAccess` per turn)
- Test: `agent/test/accessLive.test.ts`

**Interfaces:**
- Consumes: Task 3 `AccessResolver.item`, `.registry`; Task 2 `registrySource`, `remoteSource`, `packageServerSource`; `toolPrefix`, `packageServerName`.
- Produces:
  - `export type LiveAccess = (harnessName: string) => Promise<ToolAccess | undefined>`; `export function liveAccess(resolver: AccessResolver, turn: { tools: readonly Tool[]; remote: readonly string[]; packageServers: readonly { name: string; pkg: string; server: string }[] }): LiveAccess`: `mcp__scadbuddy__<t>` → its registry access; `mcp__<remote>__<t>` → `item(remoteSource, t, 'outward')`; a package server's tool → `item(packageServerSource, t, 'outward')`; anything else (Skill, Agent, built-ins, http_request with no item row) → `undefined`, so the per-turn resolver stands.
  - `export class SeamLedger { record(toolUseId: string, tier: RiskTier): void; tierOf(toolUseId: string | undefined): RiskTier | undefined }`: one call's worth of entries, never pruned on its own; `turnRunner.ts` builds a fresh one per turn and drops it when the turn ends, so it lives only for the turn and a long one does not grow the entries it holds beyond that turn's own calls.
  - `export type LiveSeam = { access: LiveAccess; ledger?: SeamLedger }`; `makeCanUseTool(tierOf, onDecision?, gate?, guard?, live?: LiveSeam)` and `makePreToolUseHook(…, live?: LiveSeam)`: `off` → deny `<tool> is disabled in Settings → Plugins; the user can turn it back on there`; a tier replaces `tierOf`'s; a read that throws → deny `<tool> is refused: its Settings → Plugins entry cannot be read`; the tier applied is recorded under the call's tool_use id.
  - `HarnessRun.live?: LiveSeam`; `createHarnessServer(…, seamTierOf: (tool: Tool, toolUseId?: string) => Risk)`; `harnessTools(...).mcpServers(…, { builtIn, ledger })` uses `ledger.tierOf(toolUseId) ?? builtIn.tierOf(...)`.

- [ ] **Step 1: Write the failing tests** (`accessLive.test.ts`, over `memoryAccess`, a turn-start `policy()` and `buildHarnessOptions`)

- `a tier lowered mid-turn stops parking on the next call` (turn starts with `outwardTool` stored outward; store lowers it to `write`; `canUseTool(OWN(outwardTool))` → `allow`)
- `a tier raised mid-turn parks the next call` (`readTool` raised to `outward` after the turn's policy; with no gate → deny matching `/approval/`)
- `a tool set off mid-turn is refused at the seam` (`/disabled in Settings/`)
- `a call the seam parked after a mid-turn raise runs once approved` (ledger records `outward` for `toolu_1`; the in-process server's call with `_meta` tool_use id `toolu_1` runs; no `raised to "outward" during this turn`)
- `a remote tool lowered mid-turn stops parking` (`mcp__hs__recall` stored `read`)
- `the seam fails closed when access cannot be read`

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/accessLive.test.ts`
Expected: FAIL (`Cannot find module '../src/access/live.js'`).

- [ ] **Step 3: Implement** as listed under Produces; `turnRunner.ts` passes `live: { access: liveAccess(resolver, …), ledger }` and the same `ledger` to `harnessTools(...).mcpServers`.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/accessLive.test.ts test/builtInTools.test.ts test/harnessWiring.test.ts test/warm.fingerprint.test.ts`
Expected: PASS; `refuses a call raised to outward after the turn read its policy, and runs one the seam parked` (#2100) still passes (no ledger entry, so the turn policy's tier).

- [ ] **Step 5: Commit**

```bash
git add agent/src/access/live.ts agent/src/harness/seamLedger.ts agent/src/harness/permissions.ts agent/src/harness/run.ts agent/src/tools/projections.ts agent/src/tools/harness.ts agent/src/sessions/turnRunner.ts agent/test/accessLive.test.ts
git commit -m "feat(agent): the classic seam reads each call's access as it is now (#2171)"
```

---

### Task 10: The `tool_tier` activity for durable sessions

**Files:**
- Create: `agent/src/temporal/toolTier.ts`
- Modify: `agent/src/temporal/toolActivities.ts:297` (registered beside `FLOW_TOOL_TIER_ACTIVITY`)
- Test: `agent/test/toolTier.test.ts`

**Interfaces:**
- Consumes: `effectiveTool` (Task 4); `sessionOf` (`temporal/turnTools.ts`); `DURABLE_ONLY_NAMES`.
- Produces: `export const TOOL_TIER_ACTIVITY = 'tool_tier'`; `export function toolTierActivity(tools: readonly Tool[], services: Pick<ToolServices, 'toolOverrides'>): (input: unknown) => Promise<{ tier: Risk; disabled: boolean }>`: `{tool}` for a `session-<uuid>` workflow only (`UnknownSession` otherwise), `ToolError` for an unknown or answer tool, non-retryable; one `effectiveTool` read.

- [ ] **Step 1: Write the failing test** (`MockActivityEnvironment` as `builtInTools.test.ts:364-379`)

- `answers a lowered outward tool as write and a raised read tool as outward`
- `answers an off tool as disabled`
- `refuses a flow run's workflow and an unknown tool`

- [ ] **Step 2: Run it to verify it fails**

Run: `cd agent && pnpm vitest run test/toolTier.test.ts`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement** `toolTierActivity` and register it.

- [ ] **Step 4: Run it to verify it passes**

Run: `cd agent && pnpm vitest run test/toolTier.test.ts && pnpm build`
Expected: PASS; build writes `dist/tools.json` unchanged in shape.

- [ ] **Step 5: Commit**

```bash
git add agent/src/temporal/toolTier.ts agent/src/temporal/toolActivities.ts agent/test/toolTier.test.ts
git commit -m "feat(agent): tool_tier, a durable session's per-call access of ScadBuddy's own tools (#2171)"
```

---

### Task 11: The Python port: `gate_settings`, the own plugin and the vectors

**Files:**
- Create: `agent-durable/src/scadbuddy_durable/session/access.py`
- Modify: `agent-durable/src/scadbuddy_durable/session/tools.py:37` (`OVERRIDES_SETTING` removed), `:130-180` (`sanitise_overrides`, `apply_overrides`)
- Modify: `agent-durable/src/scadbuddy_durable/session/activities.py:1-10` (docstring), `:77-96` (`gate_settings`)
- Modify: `agent-durable/src/scadbuddy_durable/session/models.py:230-239` (`GateSettings` comments only: `tool_tiers` is now every changed tier, either direction)
- Modify: `agent-durable/src/scadbuddy_durable/session/runner.py:213`, `:658-680` (own plugin from `ai_access`)
- Test: `agent-durable/tests/test_access_vectors.py` (new), `tests/test_tool_overrides.py`, `tests/test_session_workflow.py:943-1046`, `tests/test_session_runner.py`

**Interfaces:**
- Consumes: `agent/test/fixtures/access-vectors.json` (Task 3), `ai_access` (Task 1).
- Produces (`access.py`):
  - `TOOL_ACCESS = ("off", "read", "write", "outward")`, `MIGRATED_BY = "migration:2171"`, `REGISTRY_SOURCES = ("builtin:scadbuddy", "builtin:browser")`, `OWN_PLUGIN_SOURCE = "builtin:own-plugin"`
  - `@dataclass(frozen=True) class AccessRow: source: str; item: str; access: str; updated_by: str`
  - `def registry_source(name: str) -> str`; `def tool_access(declared: str, row: AccessRow | None, *, migrated_raises_only: bool) -> str`; `def source_enabled(source: str, rows: Sequence[AccessRow]) -> bool`
  - `async def read_rows(conn: psycopg.AsyncConnection[Any], sources: Sequence[str]) -> list[AccessRow]`
- Produces (`tools.py`): `sanitise_overrides(entries: Sequence[ManifestEntry], rows: Sequence[AccessRow]) -> tuple[dict[str, str], list[str]]` (the TypeScript `registryOverrides`, answer tools excluded, a manifest tier that is no tier still takes a stored tier, #3882); `apply_overrides(tools, entries, tool_tiers, disabled, *, per_call: bool = False) -> None`: `needs_approval = name not in disabled and (hitl == "answer" or per_call or (effective == "outward" and (hitl == "approval" or own != "outward")))`, `retry_policy = _ONCE if hitl == "approval" or effective == "outward" else _RETRY`.
- `gate_settings` reads `ai_settings` for the two expiry keys and `read_rows(conn, REGISTRY_SOURCES)` in the same connection; `GateSettings` keeps its fields, so recorded results replay.

- [ ] **Step 1: Write the failing tests**

`test_access_vectors.py` (path as `test_codec.py:33`): `test_every_registry_vector_matches` (parametrized over `registry`, including `durable_only`; `sanitise_overrides` over the vector's tools as `ManifestEntry` with its `hitl`), `test_every_source_vector_matches`, `test_registry_sources_match`.

`test_tool_overrides.py`, replaced (tighten-only): `test_only_raises_of_known_tools_are_kept` becomes `test_lowered_and_raised_tiers_of_known_tools_are_kept`: `builtin:scadbuddy` rows by `browser:browser` of `render_preview` outward (a raise), `print_output` read (a lowering), `ask_user` outward (an answer tool), `no_such_tool` outward, `wait_for_user` `bogus`, plus a `render_preview` row under `remote:hs` → `tiers == {"render_preview": "outward", "print_output": "read"}`, `disabled == []`. `test_a_row_of_the_wrong_shape_overrides_nothing` keeps its name over rows whose access is `"on"`, `"none"` or `""`, which give `({}, [])`. New: `test_a_lowered_outward_tool_never_parks` (`apply_overrides(..., {"print_output": "write"}, set())` → `not needs_approval`, `retry_policy.maximum_attempts == 1`); `test_per_call_parks_every_tool_but_a_disabled_one`. The other tests there keep names and assertions.

`test_session_workflow.py`: `_overrides` (`:946`) writes `ai_access` rows by `rowsFromOverrides`'s rule (disabled wins, `updated_by` `browser:browser`) instead of `ai_settings`. Replaced (tighten-only): `test_an_override_never_lowers_a_tool_below_its_own_tier` becomes `test_an_outward_tool_lowered_to_read_runs_without_parking` (`print_output` read → no `approval.required`, stand-in ran once). New: `test_gate_settings_reads_lowered_raised_and_off_from_ai_access`, `test_the_camera_off_is_refused_by_the_durable_gate_and_never_parks` (a manifest with `get_printer_camera`; row off → `disabled_tools == ["get_printer_camera"]`).

`test_session_runner.py`: `test_the_own_plugin_off_in_ai_access_is_not_loaded`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent-durable && uv run --frozen pytest tests/test_access_vectors.py tests/test_tool_overrides.py`, then `SCADBUDDY_TEST_DATABASE_URL=… SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=… uv run --frozen pytest tests/test_session_workflow.py tests/test_session_runner.py -k "override or lowered or gate_settings or camera or own_plugin"`
Expected: FAIL (`ModuleNotFoundError: scadbuddy_durable.session.access`).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the same command, then `cd agent-durable && uv run --frozen mypy && uv run --frozen ruff check .`
Expected: PASS; `test_a_tool_raised_to_outward_parks_and_runs_once_approved`, `test_a_disabled_tool_is_refused_naming_settings_and_never_parks` and `test_a_turn_an_older_worker_started_parks_by_the_manifest_and_replays` (#2088) pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add agent-durable/src/scadbuddy_durable/session/access.py agent-durable/src/scadbuddy_durable/session/tools.py agent-durable/src/scadbuddy_durable/session/activities.py agent-durable/src/scadbuddy_durable/session/models.py agent-durable/src/scadbuddy_durable/session/runner.py agent-durable/tests/test_access_vectors.py agent-durable/tests/test_tool_overrides.py agent-durable/tests/test_session_workflow.py agent-durable/tests/test_session_runner.py
git commit -m "feat(agent-durable): the gate reads ai_access by the shared vectors, in either direction (#2171)"
```

---

### Task 12: Durable turns decide each call from access as it is now

**Files:**
- Modify: `agent-durable/src/scadbuddy_durable/session/workflow.py:107-125` (constants), `:356-367` (`_run_turn`), `:568-590` (`_park`)
- Modify: `agent-durable/src/scadbuddy_durable/session/tools.py` (`gate_call`)
- Modify: `agent-durable/tests/session_support.py:102-240` (`StandInTools` gains `tool_tier`, answering from `stand_in.tiers`, default the manifest tier, and `stand_in.disabled`)
- Create: `agent-durable/tests/fixtures/durable_session_histories/raised.json` (recorded on the unpatched code in Step 1)
- Test: `agent-durable/tests/test_session_workflow.py`

**Interfaces:**
- Consumes: Task 10's `tool_tier` activity (`{tool}` → `{tier, disabled}`); Task 11 `apply_overrides(..., per_call=)`.
- Produces: `TOOL_ACCESS_PATCH = "tool-access-2171"`, `TOOL_TIER = "tool_tier"`; `async def _tier_now(self, name: str) -> dict[str, Any] | None` (the activity on `TOOLS_QUEUE`, `SHORT`, 3 attempts, `None` on failure); `tools.gate_call(tool: DurableTool, entry: ManifestEntry, tier: str) -> None` (sets `retry_policy` to `_ONCE` for `outward` or `hitl == "approval"`, else `_RETRY`). Under `workflow.patched(TOOL_ACCESS_PATCH)`: `_run_turn` calls `apply_overrides(..., per_call=True)`; `_park` asks `_tier_now` for a registry tool (`name in self._kinds and self._kinds[name] != "answer"`) and, when it answers not outward or disabled, calls `gate_call` and `decide(id, True, approver="scadbuddy")` (the activity refuses a disabled one by name); a failed answer parks (fail toward asking); an outward answer calls `gate_call` and parks as today. This auto-decision takes the same fast path as the existing `_outward` check it extends (`:576-580`): it runs before `build_entry`/`open_input`, so it opens no `ai_pending_input` entry and writes no `ai_audit` row, exactly like any other non-outward call today. The approval log (`ai_audit`, `surface`/`principal_id` on each row) only ever holds entries for calls that parked, so a call auto-let-through here — by a lowered tier, same as one whose static tier was never outward — never appears there to be confused with a person's `approver=responder.get("id")` row (`:876`); only a call that *was* parked and is then resolved (a lowered tier's `_tier_now` is read only before parking, never after) goes through `_resolved`/`_audit` at all. Unpatched histories run the turn-start gates exactly as before.

- [ ] **Step 1: Record the unpatched history.** On the code as Task 11 left it, write `await handle.fetch_history()` of `test_a_tool_raised_to_outward_parks_and_runs_once_approved` to `tests/fixtures/durable_session_histories/raised.json` once (`history.to_json()`), with a module docstring line in `test_session_workflow.py` saying what recorded it and that it is never re-recorded.

- [ ] **Step 2: Write the failing tests**

- `test_a_tier_lowered_mid_turn_stops_parking_on_the_next_call` (the scripted model calls `print_output` twice; between the calls `stand_in.tiers["print_output"] = "write"`; the first parks and is approved, the second runs with no second `approval.required`)
- `test_a_tier_raised_mid_turn_parks_the_next_call` (`render_preview` twice; raised to outward between them; the second parks; its activity is scheduled with `maximum_attempts == 1`)
- `test_a_tool_disabled_mid_turn_is_refused_naming_settings`
- `test_a_turn_from_before_tool_access_replays` (`raised.json` replays on the patched code through the same `Replayer` as `_replays`)

- [ ] **Step 3: Run them to verify they fail**

Run: `cd agent-durable && SCADBUDDY_TEST_DATABASE_URL=… SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=… uv run --frozen pytest tests/test_session_workflow.py -k "mid_turn or before_tool_access"`
Expected: FAIL (the second `print_output` parks; the raised `render_preview` runs unparked and is refused `NotApproved`).

- [ ] **Step 4: Implement** as listed under Produces.

- [ ] **Step 5: Run them to verify they pass**

Run: `cd agent-durable && SCADBUDDY_TEST_DATABASE_URL=… SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=… uv run --frozen pytest tests/test_session_workflow.py`, then `cd agent && SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=… pnpm vitest run test/agentSession.temporal.test.ts` (the recorded `agent_session_histories` still replay)
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent-durable/src/scadbuddy_durable/session/workflow.py agent-durable/src/scadbuddy_durable/session/tools.py agent-durable/tests/session_support.py agent-durable/tests/test_session_workflow.py agent-durable/tests/fixtures/durable_session_histories/raised.json
git commit -m "feat(agent-durable): each call parks by its access as it is now, behind tool-access-2171 (#2171)"
```

---

### Task 13: Backend flows take the agent's tier in either direction

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_tools.py:184-226` (constant), `:846-878` (`_live_tier`)
- Modify: `backend/tests/flows/test_tool_host_function.py:208-230`
- Modify: `backend/tests/flows/test_project_replay.py:1-90` (docstring and `NAMES`)
- Create: `backend/tests/fixtures/project_workflow_histories/lowered.json` (recorded by the new test)

**Interfaces:**
- Consumes: the agent's `flow_tool_tier` answer (Tasks 4 and 5: lowered tiers, the camera as disabled).
- Produces: `FLOW_ACCESS_PATCH = "flow-access-2171"`; `_live_tier(name: str, own: str | None) -> str` (same signature): under `workflow.patched(FLOW_ACCESS_PATCH)` it returns the agent's tier as given; unpatched it keeps the raise-only `max(own, tier)` at `:876-877`. Its docstring says so.

- [ ] **Step 1: Write the failing tests**

Replaced (raise-only): `test_the_agents_tier_never_lowers_the_runs_own` becomes `test_a_tier_lowered_while_the_run_is_under_way_takes_effect_on_its_next_call`: same script and `tool_tiers={"send_to_bambuddy": "outward"}`; after `wait_for_human` is answered with `agent.tiers["send_to_bambuddy"] = "read"`, the run finishes `succeeded` with `[s.outward for s in run.steps if s.fn == "tool:send_to_bambuddy"] == [False]` and `served.calls` holds one `send_to_bambuddy`. With `SCADBUDDY_RECORD_HISTORIES=1` it writes `lowered.json` (as `test_tooled` does).

New: `test_a_tool_lowered_from_outward_runs_without_approval` (`agent.tiers["send_to_bambuddy"] = "write"` before the run) and `test_the_camera_off_is_refused_before_it_parks` (`agent.disabled.add("get_printer_camera")`; result names `disabled`; `run.steps == []`).

`test_project_replay.py`: `NAMES` gains `"lowered"`, and the docstring says it was recorded on #2171's workflow by `test_a_tier_lowered_while_the_run_is_under_way_takes_effect_on_its_next_call`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/flows/test_tool_host_function.py -k "lowered or camera"`
Expected: FAIL (the lowered call still parks).

- [ ] **Step 3: Implement** `FLOW_ACCESS_PATCH` in `_live_tier`; then record once: `SCADBUDDY_RECORD_HISTORIES=1 uv run --frozen pytest tests/flows/test_tool_host_function.py -k lowered_while`.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd backend && SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest tests/flows/test_tool_host_function.py tests/flows/test_project_replay.py tests/flows/test_outward_host_functions.py`
Expected: PASS, every history in `NAMES` replaying, `lowered` included.

- [ ] **Step 5: Commit**

```bash
git add backend/scadbuddy/workflows/flow_tools.py backend/tests/flows/test_tool_host_function.py backend/tests/flows/test_project_replay.py backend/tests/fixtures/project_workflow_histories/lowered.json
git commit -m "feat(flows): a flow's tool call takes the agent's tier in either direction, behind flow-access-2171 (#2171)"
```

---

## Final verification

- [ ] Run the three checks in Global Constraints (agent, agent-durable, backend) with Postgres and Temporal set; all pass.
- [ ] `cd agent && pnpm build && docker build --target agent -t scadbuddy-agent:dev ..` and `docker build --target agent-durable -t scadbuddy-agent-durable:dev ..` from the repo root succeed (the bundled CLI checks).
- [ ] `grep -rn "builtin_tools\.\|http_request_enabled\|printer_camera_enabled\|scadbuddy_plugin_enabled" agent/src agent-durable/src` shows only constants that no gate reads (and `evictsWarmOnWrite`), and `ai_plugins.tool_tiers`/`disabled_tools`/`enabled` are read nowhere.
