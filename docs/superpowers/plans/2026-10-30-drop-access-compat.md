# Drop #2171's Tool-Access Compat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the rolling-deploy compat that PR 3 of #4758 (#4756, PR #4838, merged 2026-10-27) added for tool access. After this change, `ai_access` is the only tool-access state that anything reads or writes (#4828).

**Architecture:** A new agent migration drops the two mirror triggers and their functions, and deletes the four old `ai_settings` keys. The agent stops its dual writes: `AccessStore` stops writing the old keys (`access/legacy.ts` goes), and `PluginStore` stops writing the old `ai_plugins` columns. `warmEvict` stops naming the old keys. The `ai_plugins` columns `enabled`, `tool_tiers` and `disabled_tools` stay in this release, unread and unwritten. A follow-up migration drops them in the next release (D2). The resolver's "raises only" rule for `migration:2171` rows stays (D1).

**Tech Stack:** Agent: TypeScript, Hono, postgres.js, vitest. agent-durable: Python 3.14, psycopg, pytest. SQL migrations in `agent/src/db/migrations/`.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` §4.3 ("a later migration drops them"), the issue #4828 and its comment (the `PluginRepo.update` re-attribution). Background: `docs/superpowers/plans/2026-10-19-tool-access-resolver.md` (PR 3) and `agent/src/db/migrations/20261026T2201Z_access_copy.sql` (what this plan undoes).

## Decisions

- **D1. Keep the "raises only" rule. Do not rewrite `updated_by`.** The copy migration copied every `builtin_tools.scadbuddy` tier, also the lowered tiers that the old code ignored. `toolAccess` ignores a `migration:2171` registry row unless it is `off` or above the declared tier. If those rows became plain rows (a rewrite to `migration:2171-final`, or a deleted rule), each copied lowered tier would start to apply. Example: a tool declared `outward` with a copied `read` row would run with no approval. That is fail-open. SQL cannot tell which rows lower a tier, because the declared tiers are in the code (`ALL_TOOLS`), not in the database. The rule is always fail-closed: it can only keep a tier at the declared tier, raise it, or turn it off. After the triggers go, no code writes `migration:2171`, so the rule applies to a closed set of rows. So the rule, `MIGRATED_BY`, `migrated_raises_only` and their vectors in `agent/test/fixtures/access-vectors.json` stay unchanged. Only their comments change (Task 5). The spec (§4.1) and `docs/ai/security.md` already describe the rule as permanent.
- **D2. The `ai_plugins` columns are dropped in the next release, not in this one.** The previous build (every build since #4838) still selects `enabled, tool_tiers, disabled_tools` in `PluginStore.list`/`get` (`SUMMARY_COLUMNS`) and writes them in `create` and `writeAccess`. If this migration dropped the columns, every remote-plugin read and write on a previous-build replica would fail with 500 for the whole rollout. A rollback to the previous build would also break the plugin pages. In this release, the code stops reading and writing the columns. The columns are `NOT NULL` with defaults (`20260928T0735Z_plugins.sql`), so an insert that leaves them out is valid. Task 5 files the follow-up issue for the drop.
- **D3. The rollout of this release.** The migration can run while previous-build replicas still serve:
  - A previous-build replica writes `ai_access` itself and never reads the old keys or columns. The triggers only mirrored writes from pre-#4838 replicas, and none of those are left.
  - Its dual writes go on until it is replaced. They write the old keys again and the columns. Nothing reads them, so they are inert. The follow-up migration (D2) deletes the keys again, after the last such replica is gone.
  - It takes the `ai_access legacy keys` advisory lock, and this build does not. That cannot deadlock: only one side waits on the lock.
  - Rolling back to the previous build stays safe. Rolling back past #4838 is not supported any more: a pre-#4838 build reads the deleted keys as absent, which means on (fail-open for the http switch, the camera and the own plugin). Task 5 documents this.
  - `DROP TRIGGER` takes an `ACCESS EXCLUSIVE` lock on `ai_plugins` and `ai_settings` for a moment, bounded by the migrator's `lock_timeout`. If the lock is not free in time, the start fails and the pod retries. No other ordering is needed.
  - agent-durable and the backend read none of the old state. In agent-durable only one test changes. The backend (`backend/scadbuddy/flows` reads tiers from the agent's `GET /internal/flows/tools`) and the frontend (its `enabled`/`tool_tiers`/`disabled_tools` are API fields the agent computes from `ai_access`) do not change.
- **D4. Out of scope:** retiring the old routes: `PATCH /plugins/{name}` with tier fields, `PUT /settings/http-request`, `PUT /settings/printer-camera`, and the built-in `PATCH /plugin-packages/scadbuddy`. Spec §4.6 and epic #4758 give them to PR 7 (#781). They write `ai_access` only. `docs/ai/operating.md` ties the built-in PATCH and the remote writes through `setToolAccess` to "#4828". Task 5 changes those references. Remote writes keep going through `PluginRepo.setToolAccess`, which keeps the plugin-row lock and the 256-entry caps.

## Global Constraints

- New migration only: `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_access_drop_mirrors.sql`, made when Task 1 runs. Its timestamp must be later than `20261030T0841Z`. Never edit `20261026T2201Z_access_copy.sql` or any other merged file (CLAUDE.md "Migrations"). Tests find the file by `m.id.endsWith('_access_drop_mirrors')`.
- Fail closed: no change may make a tool's effective access lower than it is now (D1).
- Every tool works in every mode: classic and durable resolve the same rows. After the change, `agent-durable/tests/test_access_vectors.py` must pass unchanged.
- `agent/test/fixtures/access-vectors.json`, `secret-vectors.json` and `payload-vectors.json` stay byte-identical.
- Agent Postgres tests need `SCADBUDDY_TEST_DATABASE_URL`, with a role that has `CREATEDB`. Use a Postgres of your own, not one that another session's run is using (NOTIFY cross-talk). For example: `docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:18`. Below, `PG=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`.
- Run `cd agent && pnpm install --frozen-lockfile && pnpm gen:api` once before the first single-file vitest run.
- Commits: conventional titles, never amend, end each message with the session trailers.

## Review Focus

1. A previous-build replica writes `http_request_enabled = false` after the migration. Expected: `ai_access` does not change, and `builtin:http` stays as `ai_access` says. Test `an old-key write after the drop changes no access row` (Task 1).
2. A previous-build replica sets `ai_plugins.enabled = true` after the migration. Expected: the plugin stays off. Test `an old-column write after the drop changes no access row` (Task 1).
3. A URL-only `PluginStore.update` while the old columns differ from `ai_access` (the issue comment). Expected: every row keeps its `updated_by` and `updated_at`. Test `a URL-only update keeps every access row's author and time (#4828)` (Task 1).
4. Copied rows survive the migration. Expected: every `migration:2171` row keeps its access and author, so a copied lowered tier is still ignored. Test `leaves every migration:2171 row as it is` (Task 1).
5. Other `ai_settings` keys survive the delete. Expected: `model` and `builtin_tools.claude` stay. Test `deletes the four old keys and no other` (Task 1).

## File Map

| File | Change |
|---|---|
| `agent/src/db/migrations/<TS>_access_drop_mirrors.sql` | Create: drops both triggers and the three functions, deletes the four keys |
| `agent/test/access.dropMirrors.pg.test.ts` | Create: the migration's tests (Review Focus 1, 2, 4, 5) |
| `agent/test/access.migration.pg.test.ts` | Modify: "leaves the old keys and columns in place" migrates only through the copy |
| `agent/test/plugins.pg.test.ts` | Modify: no `DISABLE TRIGGER`; drop the two trigger-dependent tests; add Review Focus 3; old-column assertions become the defaults (Task 3) |
| `agent/test/plugins.access.pg.test.ts` | Modify: no `DISABLE TRIGGER` |
| `agent-durable/tests/test_session_runner.py` | Modify: `test_the_own_plugin_off_in_ai_access_is_not_loaded` with no `DISABLE TRIGGER` |
| `agent/src/access/legacy.ts` | Delete |
| `agent/src/access/store.ts` | Modify: no `writeLegacy`, no advisory lock, no `registryTools` parameter |
| `agent/src/main.ts` | Modify: `new AccessStore(database.sql, audit)` |
| `agent/test/access.settingsMirror.pg.test.ts` | Delete (one test moves to `accessStore.pg.test.ts`) |
| `agent/test/accessStore.pg.test.ts` | Modify: drop the lock-order test; add the moved test and "writes none of the old keys" |
| `agent/src/plugins/registry.ts` | Modify: `Row`, `SUMMARY_COLUMNS`, `create`, `writeAccess` stop using the three columns |
| `agent/src/sessions/warmEvict.ts` | Modify: `evictsWarmOnWrite` names `model` and `memory_recall_max_query_chars` only |
| `agent/src/harness/httpRequest.ts`, `agent/src/plugins/packages/builtins.ts`, `agent/src/plugins/builtInTools.ts` | Modify: delete `SETTING_HTTP_REQUEST`, `SETTING_OWN_PLUGIN`, `builtInToolsSetting` |
| `agent/test/warmEvict.pg.test.ts` | Modify |
| `agent/src/access/model.ts`, `agent/src/access/resolve.ts`, `agent-durable/src/scadbuddy_durable/session/access.py` | Modify: comments only (D1) |
| `docs/ai/operating.md`, `docs/ai/security.md` | Modify |

---

### Task 1: The migration drops the mirrors and the old keys

**Files:**
- Create: `agent/src/db/migrations/<TS>_access_drop_mirrors.sql`
- Create: `agent/test/access.dropMirrors.pg.test.ts`
- Modify: `agent/test/access.migration.pg.test.ts` (the test `leaves the old keys and columns in place`)
- Modify: `agent/test/plugins.pg.test.ts:193-229`, `agent/test/plugins.access.pg.test.ts:52,73`
- Modify: `agent-durable/tests/test_session_runner.py:731-748`

**Interfaces:**
- Produces: the migration id `<TS>_access_drop_mirrors`. After it runs, `ai_plugins` and `ai_settings` have no user triggers, and the functions `ai_plugins_mirror_access()`, `ai_settings_mirror_access()` and `ai_legacy_registry_rows(jsonb, jsonb)` do not exist.

- [ ] **Step 1: Write the failing tests in `agent/test/access.dropMirrors.pg.test.ts`**

Use the setup of `access.migration.pg.test.ts`: `throwawayDatabase({ empty: true })`, then `migrate(db.sql, MIGRATIONS.filter((m) => m.id < DROP))`, where `DROP = MIGRATIONS.find((m) => m.id.endsWith('_access_drop_mirrors'))!.id`. In `beforeEach`, assert that `DROP` exists and that `DROP > '20261026T2201Z_access_copy'`. Each test seeds the data, runs `migrate(db.sql, MIGRATIONS)`, then asserts:

```ts
it('drops both mirror triggers and their functions', async () => {
  await migrate(db.sql, MIGRATIONS)
  expect(await db.sql`SELECT tgname FROM pg_trigger WHERE tgrelid IN ('ai_plugins'::regclass, 'ai_settings'::regclass) AND NOT tgisinternal`).toEqual([])
  expect(await db.sql`SELECT proname FROM pg_proc WHERE proname IN ('ai_plugins_mirror_access', 'ai_settings_mirror_access', 'ai_legacy_registry_rows')`).toEqual([])
})
it('deletes the four old keys and no other', async () => {
  // seed: builtin_tools.scadbuddy {tool_tiers:{}}, printer_camera_enabled false, http_request_enabled false,
  // scadbuddy_plugin_enabled false, model "claude-x", builtin_tools.claude {}
  // after: SELECT key FROM ai_settings ORDER BY key  ->  ['builtin_tools.claude', 'model']
})
it('leaves every migration:2171 row as it is', async () => {
  // seed builtin_tools.scadbuddy { tool_tiers: { send_to_bambuddy: 'read', get_model: 'outward' }, disabled_tools: ['list_models'] }
  // before migrating DROP; snapshot SELECT source, item, access, updated_by, updated_at FROM ai_access; equal after
})
it('an old-key write after the drop changes no access row', async () => {
  // after migrate: INSERT ai_settings http_request_enabled=false, scadbuddy_plugin_enabled=false,
  // printer_camera_enabled=false, builtin_tools.scadbuddy {disabled_tools:['get_model']}
  // expect(await db.sql`SELECT * FROM ai_access`).toHaveLength(0)
})
it('an old-column write after the drop changes no access row', async () => {
  // after migrate: INSERT ai_plugins (name, kind, url, enabled, tool_tiers, disabled_tools)
  //   VALUES ('hs', 'remote_mcp', 'https://example.test/mcp', true, '{"a":"read"}', '{b}')
  // expect no ai_access row; (await new PluginStore(db.sql).enabled(undefined)).plugins -> []
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/access.dropMirrors.pg.test.ts`
Expected: FAIL at `beforeEach`, because `DROP` is undefined.

- [ ] **Step 3: Write the migration**

Make the file name with `date -u +%Y%m%dT%H%MZ`. In the file, put a header comment that says what it drops and why, and that the `ai_plugins` columns go next release (D2). Then, with no `IF EXISTS` (the ledger guarantees the copy file ran):

```sql
DROP TRIGGER ai_plugins_mirror_access ON ai_plugins;
DROP TRIGGER ai_settings_mirror_access ON ai_settings;
DROP FUNCTION ai_plugins_mirror_access();
DROP FUNCTION ai_settings_mirror_access();
DROP FUNCTION ai_legacy_registry_rows(jsonb, jsonb);
DELETE FROM ai_settings
WHERE key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled');
```

- [ ] **Step 4: Run them to see them pass**

Same command. Expected: 5 passed.

- [ ] **Step 5: Change the tests that need the triggers**

- `access.migration.pg.test.ts`, `leaves the old keys and columns in place`: migrate `MIGRATIONS.filter((m) => m.id <= COPY)`, not `MIGRATIONS`.
- `plugins.pg.test.ts`, `enabled() lists a plugin whose ai_access says on…`: delete the `ALTER TABLE … DISABLE TRIGGER` line. The comment becomes "The old columns disagree: ai_access wins, either way."
- `plugins.pg.test.ts`: delete `a plugin an older replica creates after the copy reads as on with its tiers` and `an older replica's switch-on is seen, and a new replica's partial update keeps it`. Review Focus 2 replaces them.
- `plugins.access.pg.test.ts:52` and `:73`: delete the `DISABLE TRIGGER` lines.
- `test_session_runner.py::test_the_own_plugin_off_in_ai_access_is_not_loaded`: delete the `ALTER TABLE ai_settings DISABLE TRIGGER` line. Keep the `_setting(…, "scadbuddy_plugin_enabled", "false")` line: it is now a stray old key, as a previous-build replica writes one. The docstring becomes: "#2171: the own plugin's switch is its ai_access row (builtin:own-plugin, item '*'), as a classic turn reads it. A stray old ai_settings key does not decide (#4828)."

- [ ] **Step 6: Add Review Focus 3 to `plugins.pg.test.ts`, beside `setToolAccess attributes only the changed row…`**

```ts
it("a URL-only update keeps every access row's author and time (#4828)", async () => {
  await store.create({ name: 'mem', url: 'https://hs.example/mcp', tool_tiers: { a: 'read' }, disabled_tools: ['b'] }, kek)
  // The old columns disagree with ai_access, as after an older replica's write.
  await db.sql`UPDATE ai_plugins SET tool_tiers = '{"a":"outward"}', disabled_tools = '{}' WHERE name = 'mem'`
  await db.sql`UPDATE ai_access SET updated_by = 'browser:browser', updated_at = now() - interval '1 day' WHERE source = 'remote:mem'`
  const before = await db.sql`SELECT item, access, updated_by, updated_at::text AS t FROM ai_access WHERE source = 'remote:mem' ORDER BY item`
  await store.update('mem', { url: 'https://hs2.example/mcp' }, kek)
  expect(await db.sql`SELECT item, access, updated_by, updated_at::text AS t FROM ai_access WHERE source = 'remote:mem' ORDER BY item`).toEqual(before)
})
```

This test is red without the migration: the old code writes the columns from `ai_access`, and the trigger then gives the rows to `migration:2171`. If the test is green with the old code, the setup is wrong. Fix the setup before you continue.

- [ ] **Step 7: Run the changed suites**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/access.dropMirrors.pg.test.ts test/access.migration.pg.test.ts test/plugins.pg.test.ts test/plugins.access.pg.test.ts`
Expected: all pass. `access.settingsMirror.pg.test.ts` now fails. Task 2 deletes it.

Run: `cd agent-durable && SCADBUDDY_TEST_DATABASE_URL=$PG SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=$(command -v temporal) uv run --frozen pytest tests/test_session_runner.py -k own_plugin_off_in_ai_access -v`
Expected: PASS.

- [ ] **Step 8: Commit**

`git add` the new migration, the new test, and the four changed test files. Message: `feat(agent): drop the tool-access mirror triggers and the old ai_settings keys (#4828)`.

### Task 2: `AccessStore` stops writing the old keys

**Files:**
- Delete: `agent/src/access/legacy.ts`, `agent/test/access.settingsMirror.pg.test.ts`
- Modify: `agent/src/access/store.ts`, `agent/src/main.ts:247-248`, `agent/test/accessStore.pg.test.ts`

**Interfaces:**
- Produces: `new AccessStore(sql: Sql, audit?: AuditSink)`. The third parameter goes. `RegistryTool`, `LEGACY_SOURCES` and `legacySettings` no longer exist.

- [ ] **Step 1: Write the failing test in `accessStore.pg.test.ts`**

```ts
it('an access write writes none of the old ai_settings keys (#4828)', async () => {
  const tools = new BuiltInTools(store)
  await tools.update(tools.named(OWN_PLUGIN_NAME)!, { tool_tiers: { get_model: 'outward' }, disabled_tools: ['list_models'] }, ctx)
  await store.setItem(SCAD, 'get_printer_camera', 'off', ctx)
  await store.setEnabled('builtin:http', false, ctx)
  await store.setEnabled('builtin:own-plugin', false, ctx)
  expect(await db.sql`SELECT key FROM ai_settings WHERE key IN ('builtin_tools.scadbuddy', 'printer_camera_enabled', 'http_request_enabled', 'scadbuddy_plugin_enabled')`).toEqual([])
})
```

For this test, build the store as `new AccessStore(db.sql, audit, ALL_TOOLS)` (import `ALL_TOOLS` from `../src/tools/index.js`), so that the old code writes the keys and the test is red. In Step 3, change it back to the two-argument form.

Move `a built-in patch while builtin:browser is off writes no browser tool rows and no legacy mirror of them` from `access.settingsMirror.pg.test.ts` to this file. Rename it `a built-in patch while builtin:browser is off writes no browser tool rows`. Keep its `ai_access` assertion and delete the `builtin_tools.scadbuddy` assertion.

- [ ] **Step 2: Run to see it fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/accessStore.pg.test.ts -t "old ai_settings keys"`
Expected: FAIL, because the query returns four keys.

- [ ] **Step 3: Remove the dual write**

In `store.ts`, delete:
- `writeLegacy`;
- `legacySources`;
- the `pg_advisory_xact_lock(hashtext('ai_access legacy keys'))` line;
- the `legacy.js` import;
- the `registryTools` field and its constructor parameter.

`write` keeps its order: the `skipUnchanged` lock read, `notifyWarmEvict`, then `runEdit`. Delete `legacy.ts`. In `main.ts`, delete the comment on line 247 and use `new AccessStore(database.sql, audit)`. In `resolve.ts:38`, the `itemOverrides` comment loses "What the legacy key holds (access/legacy.ts)". Delete `access.settingsMirror.pg.test.ts`. In `accessStore.pg.test.ts`, delete `replaceToolsOf takes the old keys' lock before it locks rows…`.

- [ ] **Step 4: Run to see it pass, and typecheck**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/accessStore.pg.test.ts && pnpm typecheck && pnpm lint`
Expected: pass. Also `git grep -n -E "legacySettings|LEGACY_SOURCES|registryTools|ai_access legacy keys" -- agent` prints nothing.

- [ ] **Step 5: Commit**

Message: `refactor(agent): AccessStore writes ai_access only, not the old ai_settings keys (#4828)`.

### Task 3: `PluginStore` stops writing and reading the old columns

**Files:**
- Modify: `agent/src/plugins/registry.ts:394-420, 558-572, 692-718`
- Modify: `agent/test/plugins.pg.test.ts` (the tests that use `oldColumns`, and the first access test at `:169`)

**Interfaces:**
- Consumes: Task 1 (with no trigger, a column write has no effect).
- Produces: `Row` without `enabled`, `tool_tiers`, `disabled_tools`. `SUMMARY_COLUMNS` without them. `PluginSummary` does not change: it still comes from `access_rows`.

- [ ] **Step 1: Write the failing assertions**

In `plugins.pg.test.ts`, define `const DEFAULTS = { enabled: false, tool_tiers: {}, disabled_tools: [] }`.
- `stores tool_tiers, disabled_tools and enabled in ai_access…` becomes `stores tool_tiers, disabled_tools and enabled in ai_access only; the old ai_plugins columns keep their defaults`. The `old` expectation becomes `DEFAULTS`.
- `setToolAccess writes ai_plugins and ai_access, so an older replica reads the same` becomes `setToolAccess writes ai_access only`. Each `oldColumns` expectation becomes `DEFAULTS`. The `files.list` check becomes `expect(await oldColumns('mem')).toEqual(DEFAULTS)`.
- `setToolAccess null removes the tool from both` becomes `setToolAccess null removes the tool's row`, with `DEFAULTS`.

- [ ] **Step 2: Run to see them fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/plugins.pg.test.ts`
Expected: the three renamed tests FAIL with the dual-written values, for example `tool_tiers: { a: 'read' }`.

- [ ] **Step 3: Stop the dual write**

In `registry.ts`:
- `create` inserts `(name, kind, url, auth_header, secret_sealed, dek_sealed, kek_id, last4)` only, and loses the "old columns too" comment.
- `writeAccess` replaces its column `UPDATE` with `UPDATE ai_plugins SET updated_at = now() WHERE name = ${name}`, and its doc comment loses the old-column sentence.
- `Row` loses the three fields and their comment.
- `SUMMARY_COLUMNS` loses them.
- `ALL_COLUMNS` stays `*`: it reads no access column by name, so it still works after the drop in D2.
- In `setToolAccess`, the comment "writeTools (and the mirror trigger before it)" becomes "writeTools".

- [ ] **Step 4: Run to see them pass**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/plugins.pg.test.ts test/plugins.access.pg.test.ts test/warmEvict.pg.test.ts && pnpm typecheck`
Expected: pass. `git grep -n -E "tool_tiers|disabled_tools|\benabled\b" -- agent/src/plugins/registry.ts` shows only API fields, validators and patch handling. No SQL names the columns.

- [ ] **Step 5: Commit**

Message: `refactor(agent): plugins' access lives in ai_access only, not ai_plugins' old columns (#4828)`.

### Task 4: `warmEvict` stops naming the old keys

**Files:**
- Modify: `agent/src/sessions/warmEvict.ts:20-36`, `agent/test/warmEvict.pg.test.ts`
- Modify: `agent/src/harness/httpRequest.ts:85-89`, `agent/src/plugins/packages/builtins.ts:20-21`, `agent/src/plugins/builtInTools.ts:47-49`

**Interfaces:**
- Produces: `evictsWarmOnWrite(key)` is true only for `'model'` and `'memory_recall_max_query_chars'`. `SETTING_HTTP_REQUEST`, `SETTING_OWN_PLUGIN` and `builtInToolsSetting` are deleted. Their only importer is `warmEvict.pg.test.ts`. An `ai_access` write sends its own eviction (`AccessStore.write`, `PluginStore.writeAccess`).

- [ ] **Step 1: Write the failing test**

In `warmEvict.pg.test.ts`, `names every Settings key a turn is assembled by, and no other` loops over `[SETTING_MODEL, SETTING_RECALL_MAX_QUERY_CHARS]` for `true`. Add:

```ts
for (const old of ['http_request_enabled', 'scadbuddy_plugin_enabled', 'builtin_tools.scadbuddy', 'builtin_tools.claude', 'printer_camera_enabled']) {
  expect(evictsWarmOnWrite(old), old).toBe(false)
}
```

In `a Settings write a turn is assembled by tells every replica to evict everything`, delete the `setMany({ [builtInToolsSetting('claude')]: … })` half. Delete the three imports.

- [ ] **Step 2: Run to see it fail**

Run: `cd agent && pnpm exec vitest run test/warmEvict.pg.test.ts -t evictsWarmOnWrite`
Expected: FAIL on `http_request_enabled`.

- [ ] **Step 3: Change `evictsWarmOnWrite`**

Make the change in `evictsWarmOnWrite` and its doc comment. Delete the three exports and their comments.

- [ ] **Step 4: Run to see it pass**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm exec vitest run test/warmEvict.pg.test.ts && pnpm typecheck && pnpm lint`
Expected: pass.

- [ ] **Step 5: Commit**

Message: `refactor(agent): warm eviction no longer names the old tool-access keys (#4828)`.

### Task 5: The rule's comments, the docs, the follow-up issue and the full check

**Files:**
- Modify: `agent/src/access/model.ts:15-16`, `agent/src/access/resolve.ts:9-13`, `agent-durable/src/scadbuddy_durable/session/access.py:23-24, 50-53`
- Modify: `docs/ai/operating.md` (lines 883, 1380-1390, 1421-1425), `docs/ai/security.md:752-768`

- [ ] **Step 1: Comments (D1)**

`MIGRATED_BY` in both runtimes: "`updated_by` of the rows that migration 20261026T2201Z_access_copy.sql copied. A closed set: nothing has written it since <TS>_access_drop_mirrors.sql dropped the mirror triggers (#4828)." Add one sentence to the `toolAccess` / `tool_access` doc: "Kept for good: a copied tier the old code ignored must not start to lower a tool's gate (#4828)." Change no code.

- [ ] **Step 2: Docs**

- `operating.md` "The old keys no longer decide" bullet: the copy wrote `migration:2171` rows. `<TS>_access_drop_mirrors.sql` dropped both triggers and deleted the four keys. The agent writes only `ai_access`. The `ai_plugins` columns stay, unread and unwritten, until the follow-up issue's migration. Rolling back the agent past #4838 is not supported (the reason from D3). Link both migrations.
- `operating.md:883`: "(#4828)" becomes "(#781)".
- `operating.md:1423`: remote writes go through `PluginRepo.setToolAccess` under the plugin's row lock. Delete "for one release (until #4828), so the `ai_plugins` columns an older replica reads stay right".
- `security.md` "Old keys" bullet: the same facts as `operating.md`, short.
- Use Simplified Technical English.

- [ ] **Step 3: Check that no old reference is left**

Run: `git grep -n -E "#4828|mirror trigger|DISABLE TRIGGER|dual.?writ|legacy\.ts|for one release \(#2171\)" -- agent agent-durable docs/ai ':!agent/src/db/migrations'`
Expected: only the new comments that name #4828 as the change that dropped the compat.

- [ ] **Step 4: Run the full suites (CLAUDE.md commands)**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm lint && pnpm typecheck && SCADBUDDY_TEST_DATABASE_URL=$PG pnpm test && pnpm build`
Expected: all pass. If a vitest file times out under load, run it again before you debug it (Known flakes).

Run: `cd agent-durable && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && SCADBUDDY_TEST_DATABASE_URL=$PG SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=$(command -v temporal) uv run --frozen pytest`
Expected: all pass, and `tests/test_access_vectors.py` passes unchanged.

Run: `git diff --stat origin/main -- agent/test/fixtures/`
Expected: no output.

- [ ] **Step 5: File the follow-up issue (D2)**

Make the issue with `gh issue create`. Use the title `chore(agent): drop ai_plugins' old access columns (#4828 follow-up)`. In the body, give the scope:
- a new migration with `ALTER TABLE ai_plugins DROP COLUMN enabled, DROP COLUMN tool_tiers, DROP COLUMN disabled_tools`, and the same four-key `DELETE` again, for the keys that previous-build replicas wrote after this migration;
- `plugins.pg.test.ts`: drop `oldColumns` and `DEFAULTS`, and change the "before the access migration" test so that it migrates through the copy before it inserts with the old columns;
- `plugins.access.pg.test.ts` and `plugins.pg.test.ts`: drop the column `UPDATE`s;
- "Do it only after every agent replica runs the #4828 build."

Link it as a sub-issue of #4758. Note its number in the PR body.

- [ ] **Step 6: Commit**

Message: `docs(agent): tool access after the compat drop; the copied rows' rule stays (#4828)`.
