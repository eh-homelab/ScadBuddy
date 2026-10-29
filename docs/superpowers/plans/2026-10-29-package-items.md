# Package items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A plugin package's hooks, skills, subagents, allowed built-ins and MCP server tools each get an access the user sets in Settings → Tools & plugins, honoured the same way in classic and durable turns; the `builtin:browser` and package-server `*` switches are enforced at every gate; ScadBuddy's own skills and subagents move onto the page. Closes #4757.

**Architecture:** The access model learns on/off items (`agent/src/access/items.ts`) and two more switches, in TypeScript and Python with the shared vectors. Each turn builds one *derived copy* of every loaded package (and of the own plugin when something in it is off): its http servers stripped, its off hooks, skills and subagents left out, named and checked by the copy's own hash, so both runtimes' existing hash checks hold it. Classic turns add a seam backstop for Skill and Agent; durable turns get the same copy, and Bash/PowerShell decided per call by their access. A third capabilities provider serves `package:<name>` and `package:<name>/mcp:<server>` sources; the Packages section nests the shared `SourceCard`.

**Tech Stack:** TypeScript (Node 24, Hono 4, zod 4, `@modelcontextprotocol/sdk`, postgres.js, vitest, Temporal TS SDK); Python 3.14 (temporalio, psycopg, pytest); React 19, Vite, vitest + Testing Library + msw, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` (§2 "Package contents reach a turn", "Disabling one hook, skill or subagent"; §4.1, §4.2, §4.4, §4.6, §4.7, §4.8; §5 item 5; §6). Issue #4757 with its three comments (the package-server `*`, the `builtin:browser` `*` in every runtime, the capabilities API reflecting a source's `*`, and `toolWrite`'s refusal of `on`). PR 4's plan `docs/superpowers/plans/2026-10-29-tools-and-plugins-page.md`, "How PR 5 adds packages without reshaping this".

**Base:** branch `feat/2171-package-items` from PR 4's head `4a27fd5fd` (#4993, not merged). Line references are that commit's.

## Global Constraints

- Agent check: `cd agent && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- agent-durable check: `cd agent-durable && ../.github/scripts/agent-durable-pin.sh . && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` (uv ≥ 0.9.0; use the image's uv, `uvx uv@<Dockerfile version>`, never the local 0.6.16, which rewrites `uv.lock`).
- Frontend check: `cd frontend && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`; Playwright `pnpm exec playwright test`. Run `pnpm gen:api` once before a single `pnpm exec vitest run <file>`.
- Postgres tests: `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`, a role with `CREATEDB`. Temporal tests: `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` or `temporal` on `PATH`.
- No agent migration is needed: `ai_access` already allows `on` on `package:%` and `builtin:own-plugin` items and any `*` on a package-server source; `ai_tool_listings` already takes `package:` sources (`agent/src/db/migrations/20261026T2200Z_access.sql`). If one turns out to be needed it is a NEW file `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.
- Access values: a tool and an allowed built-in take `off`, `read`, `write`, `outward`; a hook, skill or subagent takes `off`, `on` (spec §4.1). One select per item.
- Lowering asks one confirmation line, and that is the whole guard. No `USER_ONLY` on any access select, Reset, source switch (the package's and the own plugin's included) or the confirmation; no `fixed` item beyond what is technically forced (tools or package servers colliding on one name; a renamed tool takes only `off`/`outward`); no extra warning (#4208). Install, Approve (and its `allow_refused` confirmation), Fetch re-pin, Approve re-pin, Discard re-pin and Remove keep their `USER_ONLY` marks: they decide which code is trusted, not an item's access.
- Every tool works in every mode: an item has the same access in a classic and a durable turn, a flow run and `/mcp`. Where the runtimes differ in how (the durable engine has no hook that reads a call's input), the plan gives each its own lever; it never documents a gap.
- New durable state lives in Postgres (`ai_access`, `ai_tool_listings`); package copies on disk are derived caches, re-hashed before use, never trusted as found.
- Temporal: the agent-durable `DurableSession` change goes behind `workflow.patched("builtin-access-4757")` and replays `agent-durable/tests/fixtures/durable_session_histories/`. No change to `AgentOperation` or the backend's workflows.
- Rolling deploy loses no write: every write of this PR lands in `ai_access` (or `ai_plugin_packages`), which both old and new replicas read. The new switches and items have no older state to mirror into; an older replica ignores them until it is replaced (see "Open points" 3). Never fold a `*` switch into the legacy keys `access/legacy.ts` writes (the mirror trigger would copy it back as per-tool rows).
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed.
- Commits: conventional titles (`feat(agent):`, `feat(agent-durable):`, `feat(frontend):`, `test(frontend):`, `docs:`), each ending with the session's trailers `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_0132q9E8kdzKHMu5F5oHC8QQ`.
- Not in this PR: subagents parking and durable subagents getting every tool (PR 6, #2660); the MCP access and Audits areas (PR 7, #781); dropping the legacy keys (#4828).

## Review Focus

1. **Two turns at once with different access to one pin** (a skill turned off between them). Expected: both copies exist side by side, each named by its own hash; neither overwrites the other, and the earlier turn's copy still hashes to what it was handed. Test: Task 4 `two variants of one pin coexist`.
2. **The own plugin in a repository checkout**, where `agent/plugins/scadbuddy/skills` and `agents` are symlinks (`ownPlugin.ts:7-14`). Expected: its derived copy dereferences them (`hashTree` refuses links) and loads. Test: Task 4 `derives a copy of a directory whose skills are symlinks`.
3. **Two allowed packages naming `Bash`, one at `write` and one at `off`** (or `read` and `outward`). Expected: the stricter wins in both runtimes (`off` ≻ `outward` ≻ `write` ≻ `read`). Tests: Task 1 `the stricter built-in access wins`; Task 5 `a classic Bash call takes the stricter access of the packages naming it`; Task 6 `test_bash_off_is_refused_and_lowered_runs_unasked`.
4. **A hooks file rewritten with its keys reordered or reindented, and one whose hook URL changed.** Expected: the first keeps every hook's id (and override); the second gives that hook a new id, back at `on`, marked "New since you last reviewed" on the pending re-pin. Tests: Task 3 `a hook's id ignores key order and whitespace`, `a hook's id changes with its url`; Task 7 `an override survives a re-pin that keeps the hook, and a changed hook comes back on`; Task 11 `marks a hook new since you last reviewed`.
5. **A package whose stored review predates hook ids and descriptions** (installed before this PR). Expected: its hooks, skills and subagents still list with ids and descriptions, read from its verified cached copy, and switching one works. Test: Task 9 `a review stored without hook ids reads them from the verified copy`.

Also covered where they belong: package servers named alike through `harnessToolName` (`a.b` and `a_b`) fixed off with the reason (Tasks 2, 9); an item under a switched-off source shown `off` with its own access kept (Task 8); a Skill call naming a disabled skill refused by the classic seam mid-turn (Task 5); an unapproved package's switch answering 409 (Task 9); a refresh of a package server that is down keeping the last listing (Task 9).

---

## File map

Agent (new): `src/access/items.ts`, `src/plugins/packages/derived.ts`, `src/plugins/packages/listing.ts`, `src/access/packageProvider.ts`.
Agent (modified): `src/access/{model,resolve,resolver,store,legacy,live,capabilities,providers}.ts`, `src/plugins/packages/{vet,mcp,builtins,store,install}.ts`, `src/temporal/{turnPackages,turnServers,toolTier,toolActivities}.ts`, `src/sessions/turnRunner.ts`, `src/routes/{capabilities,pluginPackages}.ts`, `src/main.ts`; `test/fixtures/access-vectors.json` (regenerated).
agent-durable (modified): `src/scadbuddy_durable/session/{access,tools,runner,workflow,subagents}.py`.
Frontend (modified): `src/api/{capabilities,aiPlugins}.ts`, `src/components/capabilities/access.ts`, `src/mocks/features/capabilities.ts`, `src/mocks/aiPlugins.ts`, `src/pages/settings/sections/{builtin,packages}.tsx`, `src/components/settings/PluginPackages.tsx`. E2e: `e2e/tools-and-plugins.spec.ts`, `e2e/plugin-packages.spec.ts`. Docs: `docs/ai/operating.md` (§9 and "Tool access"), `README.md`.

---

### Task 1: The access model: on/off items and the two `*` switches, in both languages

**Files:**
- Create: `agent/src/access/items.ts`
- Modify: `agent/src/access/resolve.ts:23-35` (`registryOverrides` folds the browser switch; the per-tool rule moves to `itemOverrides`), `:51-53` (`enabledByDefault`), `agent/src/access/legacy.ts:30` (calls `itemOverrides`), `agent/src/access/resolver.ts:32-44` (`registryOverrides().of` reads `switchedItem` for a browser tool), new methods; `agent/src/access/store.ts:36-39`, `:147-151` (`setItem` takes an `Access`, checked per item kind); `agent/src/access/live.ts:34-38` (package servers `switched: true`)
- Modify: `agent-durable/src/scadbuddy_durable/session/access.py:56-66` (`source_enabled`), `tools.py:148-167` (`sanitise_overrides` folds the browser switch)
- Modify: `agent/test/accessVectors.test.ts` (new vectors), `agent/test/fixtures/access-vectors.json` (regenerated)
- Test: `agent/test/accessResolver.test.ts`, `agent/test/accessStore.pg.test.ts`, `agent/test/access.settingsMirror.pg.test.ts`, `agent/test/capabilities.gates.test.ts`, `agent-durable/tests/test_access_vectors.py`, `agent-durable/tests/test_tool_overrides.py`

**Interfaces:**
- Produces (`access/items.ts`):
  - `export type SwitchAccess = 'on' | 'off'`
  - `export const skillItem = (name: string) => \`skill:${name}\``; `subagentItem`, `builtinItem` likewise (`subagent:`, `builtin:`); `export const hookItem = (event: string, id: string) => \`hook:${event}:${id}\`` where `id` is 16 hex.
  - `export function itemKindOf(item: string): 'tool' | 'hook' | 'skill' | 'subagent' | 'builtin'` (by prefix; anything else is a tool).
  - `export const BUILTIN_STRICTNESS: readonly ToolAccess[] = ['read', 'write', 'outward', 'off']`; `export function strictest(accesses: readonly ToolAccess[]): ToolAccess | undefined`.
  - `export function pluginOfName(qualified: string): { plugin: string; name: string } | undefined` (`"<plugin>:<name>"`, split at the first `:`).
- Produces (`resolve.ts`): `export function itemOverrides(tools, rows): StoredOverrides` (today's `registryOverrides`, per tool only; `legacy.ts` uses it); `registryOverrides(tools, rows)` now also puts every `browser_*` tool in `disabled_tools` while `sourceEnabled('builtin:browser', rows)` is false. `enabledByDefault(source)` is true for `builtin:*` and for `package:<pkg>/mcp:<server>` (a server is on while its package is approved and enabled; the package's own switch is `ai_plugin_packages.enabled`).
- Produces (`resolver.ts`):
  - `switchOn(source: string, item: string): Promise<boolean>` (`false` only for an `off` row; `true` without a reader)
  - `builtin(name: string, packages: readonly string[]): Promise<ToolAccess>` (the `strictest` of the `package:<p>` rows `builtin:<name>`; `outward` when none)
  - `packageAccess(sources: readonly string[]): Promise<Map<string, PackageAccess>>` with `export type PackageAccess = { off: { skills: Set<string>; subagents: Set<string>; hooks: Set<string>; builtins: Set<string> }; builtinTiers: Map<string, ToolAccess> }`, one read for every source given.
- Produces (`store.ts`): `AccessWriter.setItem(source, item, access: Access, context?)`; `checkItem(source, item, access)` refuses (400) `on` on a tool, a tier on a hook/skill/subagent, and an on/off item outside `package:` and `builtin:own-plugin`.
- Python: `source_enabled` is true by default for `builtin:` and for a `package:…/mcp:…` source; `sanitise_overrides` disables every `browser_*` entry while `builtin:browser` `*` is `off` (read with the registry rows `gate_settings` already reads, `access.REGISTRY_SOURCES`).

- [ ] **Step 1: Write the failing tests**
  - `accessResolver.test.ts`: `a package server is on until switched off`; `builtin:browser switched off disables every browser tool in registry() and registryOverrides().disabled()`; `registryOverrides().of a browser tool answers disabled while its source is off`; `switchOn reads an off row as off and anything else as on`; `the stricter built-in access wins` (`write` and `off` → `off`; `read` and `outward` → `outward`; none → `outward`); `packageAccess reads every package's items in one query`.
  - `accessStore.pg.test.ts`: `stores on for a package skill and the own plugin's subagent`; `refuses on for a tool, a tier for a hook, and on outside a package (400)`.
  - `access.settingsMirror.pg.test.ts`: `switching builtin:browser off writes no browser tool into builtin_tools.scadbuddy` (the legacy key unchanged, no `migration:2171` rows appear).
  - `capabilities.gates.test.ts`: `builtin:browser switched off hides browser tools from /mcp`, `… answers disabled from /internal/flows/tools`, `… answers disabled from the durable TOOL_TIER_ACTIVITY`.
  - `accessVectors.test.ts`: new `registry` vectors `browser switched off disables the browser tools`, `browser switched on keeps their rows`; new `sources` vectors `a package server is on by default`, `a package server off`, `a package is off by default`.
  - `test_tool_overrides.py`: `test_browser_switch_off_disables_browser_tools`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/accessResolver.test.ts test/accessVectors.test.ts test/capabilities.gates.test.ts`, `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/accessStore.pg.test.ts test/access.settingsMirror.pg.test.ts`, `cd agent-durable && uv run --frozen pytest tests/test_tool_overrides.py tests/test_access_vectors.py`
Expected: FAIL (`switchOn` undefined; browser tools listed; vectors differ).

- [ ] **Step 3: Implement** as listed under Produces; regenerate with `UPDATE_ACCESS_VECTORS=1 pnpm vitest run test/accessVectors.test.ts`. Remove the two "until #4757" comments (`resolver.ts:37`, `live.ts:36`).

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, then `pnpm vitest run test/builtInTools.test.ts test/accessLive.test.ts test/printerCameraAccess.test.ts` (#2088, #2100 unchanged).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access agent/test agent-durable/src/scadbuddy_durable/session/access.py agent-durable/src/scadbuddy_durable/session/tools.py agent-durable/tests
git commit -m "feat(agent,agent-durable): on/off items, and the browser and package-server switches at every gate (#4757)"
```

---

### Task 2: Package MCP servers: their switch, their names, and durable parity

**Files:**
- Modify: `agent/src/plugins/packages/mcp.ts:73-152` (`forwardPackageServers`; its per-server checks move to `packageServerConfigs`), `:43-55` (`PackageServers.keep`)
- Modify: `agent/src/temporal/turnPackages.ts:185-201` (`localPackageServers`: the same name check)
- Modify: `agent/src/temporal/turnServers.ts:375-387` (package servers get `mayPark`, `fixedTier`, `keep`, `listingKey` as remote plugins do at `:196-230`)
- Test: `agent/test/turnPackages.test.ts`, `agent/test/turnTools.test.ts`, `agent/test/plugins.access.pg.test.ts`

**Interfaces:**
- Consumes: Task 1 `AccessResolver.enabled` (with the package-server default), `packageServerSource`; `pluginFingerprint` (`forwarder.ts:709`).
- Produces (`mcp.ts`):
  - `export type PackageServerConfig = { pkg: string; server: string; name: string; plugin: RemotePlugin; address: string }`
  - `export async function packageServerConfigs(loaded: readonly LoadedPackage[], resolve: Resolver, access?: Pick<AccessResolver, 'plugin' | 'enabled'>): Promise<{ configs: PackageServerConfig[]; problems: string[] }>`: every http server that passes today's checks, its tiers from `access.plugin`, left out (no problem) while `access.enabled(packageServerSource(pkg, server))` is false; servers whose `packageServerName` is equal (`a.b` and `a_b`, `s.x` and `s_x`) are all left out with one problem each: `plugin package <pkg>: MCP server "<server>" is named <name>, as <others> are, so none of them is offered`.
  - `export function collidingServerNames(loaded: readonly LoadedPackage[]): Map<string, { pkg: string; server: string }[]>` (names with more than one owner, http and local servers alike; pure).
  - `PackageServers.keep(name: string): { fingerprint: string; register(): Registration } | undefined`, as `forwardForRun`'s (`forwarder.ts:734-740`).
- Produces (`turnServers.ts`): each forwarded package server: `listingKey: JSON.stringify(plugin.disabledTools)`, `mayPark: (n) => browser?.covers(n) === true || tierOf(n) === 'outward'`, `fixedTier: (n) => tierOf(n)` (browser navigate excepted, as `:210`), `keep` from `forwardedPackages.keep(plugin.name)`.

- [ ] **Step 1: Write the failing tests**
  - `turnPackages.test.ts`: `a package server switched off is not forwarded and its tools are not listed`; `a package server is forwarded with no row (on by default)`; `servers named alike through harnessToolName are none of them offered, each said` (packages `a.b` and `a_b`, each with server `s`); `a local server named like another package's is not started`.
  - `turnTools.test.ts`: `a package server's read tool is listed may_park false with its fixed tier`; `a package server's MCP session is kept across calls like a remote plugin's (#2581)`.
  - `plugins.access.pg.test.ts`: `a package server set off in ai_access is absent from the classic seam's access` (`liveAccess` answers `off` for its tool).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/turnPackages.test.ts test/turnTools.test.ts && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/plugins.access.pg.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, then `pnpm vitest run test/turnToolSessions.test.ts test/pluginPackages.temporal.test.ts`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/mcp.ts agent/src/temporal/turnPackages.ts agent/src/temporal/turnServers.ts agent/test/turnPackages.test.ts agent/test/turnTools.test.ts agent/test/plugins.access.pg.test.ts
git commit -m "feat(agent): package MCP servers switch off, refuse colliding names, and park and keep as remote plugins do (#4757)"
```

---

### Task 3: Vetting: hook ids, where each item lives, and descriptions

**Files:**
- Modify: `agent/src/plugins/packages/vet.ts:69` (`ReviewHook`), `:73-97` (`PackageReview.described`), `:322-333` (`hookHandlers` keeps the group's `matcher`), `:340-384` (`skillNames`, `markdownIn` also answer paths), `:432-458`, `:482-500`
- Modify: `frontend/src/api/aiPlugins.ts` (`ReviewHook.id?`, `matcher?`; `PackageReview.described?`)
- Test: `agent/test/pluginPackages.test.ts`, `agent/test/pluginPackageKinds.test.ts`

**Interfaces:**
- Produces (`vet.ts`):
  - `export function hookId(event: string, matcher: unknown, handler: unknown): string`: the first 16 hex of the sha256 of the canonical JSON of `{ event, matcher, handler }` (keys sorted at every level, no whitespace, numbers as `JSON.stringify` prints them; not `gate/canonical.ts`, which refuses numbers, and a hook's `timeout` is one).
  - `ReviewHook = { event: string; type: string; url?: string; id?: string; matcher?: string }` (`id` always set by this vetting; optional only for reviews stored before it).
  - `PackageReview.described?: Record<string, string | null>`: each `skills`/`agents` entry (`"<pkg>:<name>"`) to its frontmatter `description`.
  - `export function packageItems(root: string, manifest: Record<string, unknown>): { skills: Map<string, string>; subagents: Map<string, string>; hooks: { id: string; event: string; where: string }[] }`: each skill's directory and each subagent's file (paths relative to `root`), and each hook with the config it is in (`declaredConfigs(...).hooks[].where`). Task 4 deletes and rewrites by it.

- [ ] **Step 1: Write the failing tests** (`pluginPackages.test.ts`)
  - `gives each hook an id from its event, matcher and handler`
  - `a hook's id ignores key order and whitespace`
  - `a hook's id changes with its url`
  - `two byte-identical hooks share an id` (one access covers both; open point 6)
  - `records each skill's and subagent's frontmatter description`
  - `packageItems finds a manifest-named skill directory and a nested subagent file` (`agents/review/deep.md` is `review:deep`)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/pluginPackages.test.ts test/pluginPackageKinds.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/pluginPackages.pg.test.ts`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/vet.ts agent/test/pluginPackages.test.ts agent/test/pluginPackageKinds.test.ts frontend/src/api/aiPlugins.ts
git commit -m "feat(agent): a package review names each hook by its content and keeps skill and subagent descriptions (#4757)"
```

---

### Task 4: The derived copy, named by its own hash

**Files:**
- Create: `agent/src/plugins/packages/derived.ts`
- Modify: `agent/src/plugins/packages/mcp.ts:174-234` (`withoutRemote` moves to `derived.ts`; `withoutRemoteServers` becomes `derivedPackage(dir, root)` with no omissions, and goes once its two callers move in Task 5)
- Test: `agent/test/derivedPackage.test.ts` (new); `agent/test/turnPackages.test.ts:358-396` (the three strip tests move here)

**Interfaces:**
- Consumes: Task 3 `packageItems`; `hashTree` (`hash.ts`), `touch` (`mcp.ts:195`), `declaredConfigs`.
- Produces (`derived.ts`):
  - `export type Omitted = { skills: ReadonlySet<string>; subagents: ReadonlySet<string>; hooks: ReadonlySet<string> }` (item names: skill and subagent names as Task 3's maps key them, hook ids); `export const NOTHING_OMITTED: Omitted`.
  - `export async function derivedPackage(dir: string, contentHash: string, root: string, omitted: Omitted = NOTHING_OMITTED): Promise<{ dir: string; contentHash: string }>`: `dir` and `contentHash` unchanged when it declares no http server and nothing it holds is omitted; else a copy (`cp(dir, tmp, { recursive: true, dereference: true })`) with its http servers stripped (today's rules), each omitted skill's directory and subagent's file removed (and its entry in a manifest list), each omitted hook's handler removed from its config (a group left with no handler goes, an event left with no group goes), then hashed; the folder is `root/<that hash's hex>`. A folder already there is kept only while it hashes to that; the rename race as today (`mcp.ts:224-229`). It fails when the copy still declares an http server or an omitted item.

- [ ] **Step 1: Write the failing tests** (`derivedPackage.test.ts`)
  - `answers the package itself when nothing changes`
  - `strips http servers, keeping local ones` (moved), `strips a manifest whose mcpServers is a list` (moved), `makes the copy again when the one there was changed` (moved)
  - `omits a disabled skill, subagent and hook, and nothing else` (the copy's `vetPackage` review lacks exactly those three)
  - `names the copy by its own hash, not the pin's` (`path.basename(dir) === contentHash.slice(7)`, and `hashTree(dir).hash === contentHash`)
  - `two variants of one pin coexist` (omit skill `a`, then skill `b`, concurrently: two folders; each still hashes to its answer)
  - `derives a copy of a directory whose skills are symlinks` (a temp dir whose `skills` links elsewhere; the copy holds regular files and hashes)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/derivedPackage.test.ts`
Expected: FAIL (`Cannot find module '../src/plugins/packages/derived.js'`).

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd agent && pnpm vitest run test/derivedPackage.test.ts test/turnPackages.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/derived.ts agent/src/plugins/packages/mcp.ts agent/test/derivedPackage.test.ts agent/test/turnPackages.test.ts
git commit -m "fix(agent): a package's derived copy is named by its own hash, and leaves out what is off (#4757)"
```

---

### Task 5: Each turn's filtered copies, the seam backstop and built-ins by access (agent)

**Files:**
- Modify: `agent/src/sessions/turnRunner.ts:311-339` (one `packageAccess` read per turn; the own plugin and each package loaded as `derivedPackage`), `:349-358` (`liveAccess` gets the turn's plugins and built-ins), `:464-469`, `:585` (an `off` built-in is not offered)
- Modify: `agent/src/access/live.ts` (`LiveTurn.plugins`, `LiveTurn.builtins`)
- Modify: `agent/src/temporal/turnPackages.ts:54-81` (`sharePackage(pkg, root, localRoot, omitted, builtinsOff)`)
- Modify: `agent/src/temporal/turnServers.ts:301-370` (reads `packageAccess` once per listing or call; passes it to `sharePackage`; the `builtin` server's `tierOf` from `builtin(name, packages)`, its `named` without `off` ones)
- Modify: `agent/src/temporal/toolTier.ts` (`BUILTIN_TIER_ACTIVITY`), `agent/src/temporal/toolActivities.ts:330` (registers it), `agent/src/main.ts:498-499`, `:704-707` (deps)
- Test: `agent/test/accessLive.test.ts`, `agent/test/turnRunner.packages.test.ts` (new), `agent/test/turnPackages.test.ts`, `agent/test/toolActivities.test.ts`

**Interfaces:**
- Consumes: Task 1 `packageAccess`, `switchOn`, `builtin`, `pluginOfName`, `skillItem`, `subagentItem`; Task 4 `derivedPackage`, `Omitted`; `OWN_PLUGIN_DIR`, `OWN_PLUGIN_NAME`.
- Produces:
  - `LiveTurn.plugins: readonly { name: string; source: string }[]` (each loaded package `package:<name>`, the own plugin `scadbuddy` → `builtin:own-plugin`); `liveAccess` answers `off` for `Skill` when `input.skill` names an `off` skill, and for `Agent`/`Task` when `input.subagent_type` names an `off` subagent: a qualified name (`<plugin>:<name>`) by its plugin, an unqualified one when any loaded plugin holds it off.
  - `LiveTurn.builtins: readonly { name: string; packages: readonly string[] }[]`: `liveAccess(name)` answers `resolver.builtin(name, packages)`.
  - `export function omittedOf(access: PackageAccess | undefined): Omitted`.
  - `sharePackage(pkg, root, localRoot, omitted = NOTHING_OMITTED, builtinsOff: ReadonlySet<string> = new Set()): Promise<TurnPackage>` (the folder and `content_hash` the derived copy's; `builtin_tools` without `builtinsOff`).
  - `export const BUILTIN_TIER_ACTIVITY = 'builtin_tier'`; `builtinTier(deps: { access: Pick<AccessResolver, 'builtin'>; packages: Pick<PackageRepo, 'enabledPins'> }): (args: { tool: string }) => Promise<{ tier: ToolAccess }>` (the packages: every enabled pin with `allowRefused`).

- [ ] **Step 1: Write the failing tests**
  - `accessLive.test.ts`: `refuses a Skill call naming a skill set off mid-turn`; `refuses an Agent call naming a subagent set off, qualified or not`; `leaves a Skill call naming an on skill to the turn's tier`; `a classic Bash call takes the stricter access of the packages naming it`; `a lowered Bash runs at its access` (`write` → no park).
  - `turnRunner.packages.test.ts` (over a stub `AccessResolver`, a temp package and `OWN_PLUGIN_DIR`): `a disabled skill is absent from the package Claude Code loads`; `a disabled hook is absent from the hooks Claude Code loads`; `the own plugin is loaded as a copy without its disabled subagent`; `nothing off loads the package and the own plugin as they are`; `a built-in set off is not in the turn's tools`.
  - `turnPackages.test.ts`: `shares the filtered copy under its own hash, with that hash` (the `TurnPackage.content_hash` hashes the shared folder, and differs from the pin's); `leaves a built-in set off out of builtin_tools`; `the builtin turn tools take each tool's access`.
  - `toolActivities.test.ts`: `builtin_tier answers the stricter access of the enabled allowed packages, outward with none`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/accessLive.test.ts test/turnRunner.packages.test.ts test/turnPackages.test.ts test/toolActivities.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces. Remove `withoutRemoteServers` once `turnRunner.ts:330` and `turnPackages.ts:55` call `derivedPackage`.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `pnpm vitest run test/harnessWiring.test.ts test/pluginPackages.e2e.test.ts test/builtins.durable.e2e.test.ts` (the last with Temporal and Postgres) and `pnpm build`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/test
git commit -m "feat(agent): each turn loads packages and the own plugin without what is off, in both runtimes (#4757)"
```

---

### Task 6: agent-durable: the own plugin's items, and Bash and PowerShell by access

**Files:**
- Modify: `agent-durable/src/scadbuddy_durable/session/runner.py:384-392` (`TurnSettings.own_off`), `:748-761` (read from the rows already read), `:1075-1088` (the own plugin as a filtered copy; `_subagent_tools` without `off` subagents)
- Modify: `agent-durable/src/scadbuddy_durable/session/workflow.py:106-114` (`BUILTIN_TIER`, `BUILTIN_ACCESS_PATCH = "builtin-access-4757"`), `:600-627` (`_park`: an engine-parked built-in asks its access first)
- Test: `agent-durable/tests/test_session_runner.py`, `agent-durable/tests/test_session_workflow.py`, `agent-durable/tests/test_packages.py`

**Interfaces:**
- Consumes: Task 1's item ids (`skill:<name>`, `subagent:<name>`); Task 5 `builtin_tier` (`{tool}` → `{tier}`, on `agent-tools`); `hash_tree` (`package_hash.py`).
- Produces:
  - `TurnSettings.own_off: frozenset[str] = frozenset()`: the `builtin:own-plugin` items whose row is `off`.
  - `def own_plugin_copy(plugin_dir: str, off: frozenset[str], root: str) -> str` in `runner.py`: `plugin_dir` when nothing is off; else a copy (`shutil.copytree(..., symlinks=False)`) without `skills/<name>/` for each `skill:<name>` and `agents/<name>.md` for each `subagent:<name>`, placed at `root/<hash_tree hex>` (kept when one there hashes to it; atomic rename). `root` is a new `SegmentRunner` argument, default `os.path.join(tempfile.gettempdir(), "scadbuddy-own-plugin")`.
  - The inline subagent definitions (`subagents.with_server(self._agents, …)`) leave out each `off` one.
  - `_park`: for `name in ENGINE_PARKED` and `workflow.patched(BUILTIN_ACCESS_PATCH)`: `builtin_tier`; `off` refuses the call (`self._turn.replaced[call_id]` = `"<name> is off in Settings → Tools & plugins"`, then `decide(False)`), `read`/`write` decide `True` with no entry, `outward` or no answer parks as today.

- [ ] **Step 1: Write the failing tests**
  - `test_session_runner.py`: `test_own_plugin_copy_leaves_out_off_skill_and_subagent`; `test_own_plugin_loads_as_it_is_with_nothing_off`; `test_off_subagent_is_not_defined_inline`.
  - `test_packages.py`: `test_a_filtered_copy_hashing_to_its_listing_loads` (the agent's derived copy, written with the TypeScript fixture of Task 5, hashes under `hash_tree` to the `content_hash` it was listed with).
  - `test_session_workflow.py`: `test_bash_off_is_refused_and_lowered_runs_unasked` (a fake `builtin_tier` answering `off`, then `write`, then `outward`: refused, run with no `ai_pending_input` row, parked); `test_a_turn_from_before_builtin_access_replays` (a new history `tests/fixtures/durable_session_histories/bash.json`, recorded on the base commit, of a turn whose allowed package's Bash call parked, replays; as `test_a_turn_from_before_tool_access_replays`, `:1221`), and that test still passes.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent-durable && uv run --frozen pytest tests/test_session_runner.py tests/test_session_workflow.py tests/test_packages.py`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the agent-durable check (Global Constraints), and `cd agent && pnpm vitest run test/evals.durable.e2e.test.ts` with Temporal and Postgres.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent-durable/src agent-durable/tests
git commit -m "feat(agent-durable): the own plugin without what is off, and Bash and PowerShell by their access (#4757)"
```

---

### Task 7: A package's rows over its lifecycle: install, re-pin, remove

**Files:**
- Modify: `agent/src/plugins/packages/store.ts:262-309` (`approve`), `:205-222` (`create`), `:326-331` (`delete`)
- Test: `agent/test/pluginPackages.pg.test.ts`

**Interfaces:**
- Consumes: Task 3 `ReviewHook.id`, `PackageReview.skills`/`agents`/`builtin_tools`/`mcp_servers`; `forgetSource` (`access/store.ts:61`).
- Produces: in the same transaction, with `notifyWarmEvict` as today:
  - `create` deletes every `ai_access` and `ai_tool_listings` row of `package:<name>` and of each `package:<name>/mcp:%` source (`starts_with(source, 'package:<name>/mcp:')`, never `LIKE`: names may hold `_`), so a re-install starts at the defaults;
  - `delete` deletes the same;
  - `approve` of a pending pin deletes the `package:<name>` rows whose item the new review no longer has (a hook id, `skill:`, `subagent:`, `builtin:`) and the rows and listings of servers it no longer declares.

- [ ] **Step 1: Write the failing tests** (`pluginPackages.pg.test.ts`)
  - `an override survives a re-pin that keeps the hook, and a changed hook comes back on`
  - `a re-pin drops the rows of a skill and a server the new pin no longer has`
  - `removing a package forgets its items, its servers' rows and their listings`
  - `a re-install starts at the defaults`
  - `a package named a_b keeps its rows when a.b is removed`

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/pluginPackages.pg.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/plugins/packages/store.ts agent/test/pluginPackages.pg.test.ts
git commit -m "feat(agent): a package's access rows follow its pin: kept across a re-pin, dropped with what it lost (#4757)"
```

---

### Task 8: Capabilities: a source's switch on its items, a write per kind, the browser switch and the own plugin's items

**Files:**
- Modify: `agent/src/access/capabilities.ts:23-44` (`ItemView.own_access`), `:92-176` (`Capabilities` applies the source switch to every view it answers)
- Modify: `agent/src/access/providers.ts:21-27` (`builtin:browser` `switchable: true`), `:80-85`, `:294-297` (`toolWrite` goes), `:97-160` (the own plugin's items)
- Modify: `frontend/src/api/capabilities.ts` (`ItemView.own_access`), `frontend/src/components/capabilities/access.ts:23-27` (`shownAccess` reads `own_access`)
- Test: `agent/test/capabilities.test.ts`, `agent/test/capabilities.remote.test.ts`, `frontend/src/components/capabilities/access.test.ts`

**Interfaces:**
- Consumes: Task 1 `itemKindOf`, `skillItem`, `subagentItem`, `AccessWriter.setItem(… Access …)`; Task 3 `PackageReview.described`; `reviewOf` (`builtins.ts:64`, exported).
- Produces:
  - `ItemView.own_access: Access | null`: the item's own access (what its select shows and edits, and what Reset resets). `access` is the effective one: `off` while the item's source, or the source it is a child of, is switched off, with `reason` `Off while "<source title>" is switched off` (unless a stronger reason is set). `overridden` stays about the item's own row.
  - `Capabilities` sets these for every provider in `tree`, `source` and each write's answer, children included; providers answer `own_access === access`.
  - A write per kind: `setItem` passes the `Access` through (the store checks it, Task 1); a remote tool refuses `on` with 400 in `Capabilities` already (`ITEM_ACCESS`).
  - `builtin:own-plugin` items: one per review skill (`skill:<name>`, kind `skill`) and subagent (`subagent:<name>`, kind `subagent`): `name` `<name>`, `harness_name` the review's `scadbuddy:<name>`, `label` `<name>`, `description` from `described`, `default: 'on'`, `access` the row's `on`/`off`.

- [ ] **Step 1: Write the failing tests**
  - `capabilities.test.ts`: `a browser tool reads off while builtin:browser is switched off, keeping its own access`; `switches builtin:browser off and on`; `lists the own plugin's skills and subagents with their descriptions`; `switches an own-plugin skill off and back to its default (the row goes)`; `refuses a tier for a skill and on for a tool (400)`.
  - `capabilities.remote.test.ts`: `a remote tool reads off while its plugin is switched off`.
  - `access.test.ts`: `shows the item's own access while its source is off`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/capabilities.test.ts test/capabilities.remote.test.ts`; `cd frontend && pnpm gen:api && pnpm exec vitest run src/components/capabilities/access.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, then `pnpm vitest run test/capabilities.routes.test.ts` (agent) and `pnpm exec vitest run src/components/capabilities` (frontend).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/access agent/src/plugins/packages/builtins.ts agent/test frontend/src/api/capabilities.ts frontend/src/components/capabilities
git commit -m "feat(agent): every item shows its source's switch; the browser switch and the own plugin's items (#4757)"
```

---

### Task 9: Capabilities: packages and their MCP servers

**Files:**
- Create: `agent/src/access/packageProvider.ts`, `agent/src/plugins/packages/listing.ts`
- Modify: `agent/src/access/providers.ts:200-277` (`remoteItem`/`remoteView`'s listed-tool items move to an exported `listedItems` the package provider shares)
- Modify: `agent/src/routes/capabilities.ts:61-75` (appends `packageProvider` when `deps.pluginPackages` is set), `agent/src/routes/pluginPackages.ts:330` (`packageInstaller` type gains `acquire`)
- Test: `agent/test/capabilities.packages.test.ts` (new), `agent/test/capabilities.routes.test.ts`, `agent/test/capabilities.gates.pg.test.ts`

**Interfaces:**
- Consumes: Task 1 `AccessStore`, item ids, `sourceEnabled`; Task 2 `packageServerConfigs`, `collidingServerNames`; Task 3 review ids and `described`; Task 8 `own_access`; `ToolListings`, `toStoredTools`; `testPlugin`; `PackageRepo.list`/`get`/`pinOf`/`setEnabled`; `PackageInstaller.acquire`.
- Produces (`listing.ts`): `export async function listPackageServer(deps: { packages: Pick<PackageRepo, 'pinOf' | 'get'>; installer: Pick<PackageInstaller, 'acquire'>; forwarder: PluginForwarder; listings: ToolListings; resolve?: Resolver; test?: typeof testPlugin }, pkg: string, server: string): Promise<{ status: 200 | 404 | 409 | 429 | 502; body: PluginTest | { detail: string } }>`: one at a time per source (429); 404 for an unknown package or server; 409 for an unapproved pin (`approve the pin before listing its servers`) or a colliding or refused server (its problem); acquires the pin (released after), tests it through `testPlugin` on its checked address, and on `ok` stores `test.stored` under `package:<pkg>/mcp:<server>`; a failed test stores nothing.
- Produces (`packageProvider.ts`): `export function packageProvider(deps: { packages: PackageRepo; access: AccessReader & AccessWriter; listings: Pick<ToolListings, 'get' | 'getMany'>; reviewOf: (pin: PackagePin) => Promise<PackageReview>; refresh: (pkg: string, server: string) => ReturnType<typeof listPackageServer> }): CapabilityProvider`, owning `package:<name>` and `package:<name>/mcp:<server>`:
  - a package source: `kind: 'package'`, `title` the name, `enabled` the row's `enabled`, `switchable` its `approved`; items, in order: hooks (`hook:<event>:<id>`, kind `hook`, `name` the id, `label` `<event> hook (<type>)`, `description` its URL or null), skills (`skill:<name>`), subagents (`subagent:<name>`), each `default: 'on'`; then, for an `allow_refused` pin, its built-ins (`builtin:<Tool>`, kind `builtin`, `default: 'outward'`, `description` `"Claude Code's <Tool>; where several packages name it, the strictest access applies"`); `children`: one `package_server` source per declared http server.
  - a server source: `title` `<server>`, `enabled` `sourceEnabled` (on by default), `switchable: true`, `listed_at` its listing's, items `listedItems` over its rows and listing (harness names `mcp__plugin_<pkg>_<server>__<tool>`); a colliding server's items each `fixed: { access: 'off', reason: 'Its server is named <name>, as <others> are, so none of them is offered' }`.
  - `setEnabled` of a package → `PackageRepo.setEnabled` (its 409 for an unapproved pin passes through); of a server → `AccessStore.setEnabled`; items → `AccessStore.setItem`/`resetItem`; `details` of a server tool from its listing, of anything else the item with no schema; `refresh` of a server → `deps.refresh`, a package's → 409 `"<id>" lists no tools to refresh`.
  - `reviewOf` is used (and cached per content hash, in-process) only when a stored review has a hook without `id` or no `described`; `main.ts` passes one over `installer.acquire` + `vetPackage`.

- [ ] **Step 1: Write the failing tests**
  - `capabilities.packages.test.ts` (a memory `PackageRepo`, `memoryAccess`, a stub `reviewOf`): `lists a package with its hooks, skills, subagents and servers`; `lists an allowed package's built-ins, outward by default`; `switches a skill off and back on, deleting the row`; `a hook off is stored by its id`; `an unapproved package's switch answers 409`; `switching a package goes through PackageRepo.setEnabled`; `a server is on by default and switches off`; `a server tool takes a tier; a renamed one off or outward`; `colliding servers are fixed off with the reason`; `a review stored without hook ids reads them from the verified copy`; `refresh lists a server and stores its tools`; `a failed refresh keeps the last listing`; `refresh of a package answers 409`.
  - `capabilities.routes.test.ts`: `lists packages after the remote plugins`; `routes package%3Ap%2Fmcp%3As to its server source` (the PR 4 test now answers 200 for a known server).
  - `capabilities.gates.pg.test.ts`: `a package server tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call`; `a package skill set off in Settings is absent from the next turn's copy` (classic `derivedPackage` through `turnRunner`'s deps); `a package server switched off in Settings is not forwarded`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/capabilities.packages.test.ts test/capabilities.routes.test.ts`, `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/capabilities.gates.pg.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces; `main.ts` builds `reviewOf` and passes `pluginForwarder` and the installer to the route deps.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 commands, then the agent check (Global Constraints).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/test
git commit -m "feat(agent): plugin packages and their MCP servers as capability sources, listed and refreshed (#4757)"
```

---

### Task 10: Frontend: the mock and the Built-in section's browser switch and own plugin

**Files:**
- Modify: `frontend/src/mocks/features/capabilities.ts:27-33` (browser `switchable: true`; `builtin:own-plugin` items), the source switch on items (`own_access`, as Task 8), package sources read from `mocks/aiPlugins.ts` on every request
- Modify: `frontend/src/mocks/aiPlugins.ts` (export `packageList()`, `setPackageEnabled(name, enabled)`; the seed review's hooks carry `id`, its skills and agents `described`)
- Modify: `frontend/src/pages/settings/sections/builtin.tsx:19-25` (`SHOWN` gains `builtin:own-plugin`, after HTTP)
- Test: `frontend/src/pages/settings/sections/builtin.test.tsx`, `frontend/src/api/capabilities.test.ts`

**Interfaces:**
- Consumes: Task 8's shapes; PR 4's `SourceCard`, `useCapabilities`.
- Produces: the Built-in section shows four API sources then WebMCP; the browser card has its switch (unmarked); the own plugin's card lists its skills and subagents with on/off selects. The mock's package sources follow Task 9's shape, with `enabled` and approval from the package state, so `GET /plugin-packages` and the capabilities agree.

- [ ] **Step 1: Write the failing tests**
  - `builtin.test.tsx`: `switches the browser tab off; its tools read off`; `shows ScadBuddy's skills and subagents with their descriptions`; `turns a ScadBuddy subagent off after the confirmation`; `the own plugin's switch is on its card, not user-only`.
  - `capabilities.test.ts`: `a package server's id is one encoded segment` (`setItem('package:greeter/mcp:docs', 'search', 'read')` → `PUT …/package%3Agreeter%2Fmcp%3Adocs/items/search`).

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/pages/settings/sections/builtin.test.tsx src/api/capabilities.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `pnpm exec vitest run src/components/capabilities src/agent/userOnly.test.tsx`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/mocks frontend/src/pages/settings/sections/builtin.tsx frontend/src/pages/settings/sections/builtin.test.tsx frontend/src/api/capabilities.test.ts
git commit -m "feat(frontend): the browser tab's switch and ScadBuddy's skills and subagents in Built-in (#4757)"
```

---

### Task 11: Frontend: the Packages section on the shared cards

**Files:**
- Modify: `frontend/src/components/settings/PluginPackages.tsx:495-569` (`BuiltInParts` and `BuiltInCard` go), `:571-769` (`PackageCard` nests the package's `SourceCard`; its Enable/Disable button `:652-665` goes), `:109-148` (`ReviewParts` marks new hooks), `:771-829` (`PluginPackagesPanel` lists installed packages only, matching each to its capabilities source)
- Modify: `frontend/src/pages/settings/sections/packages.tsx` (intro text)
- Test: `frontend/src/components/settings/PluginPackages.test.tsx`

**Interfaces:**
- Consumes: Task 10's mock; `useCapabilities((s) => s.kind === 'package')`, `SourceCard`.
- Produces: each `PackageCard` (aria-label `Plugin package <name>`, kept) shows its badges, source, review and trust controls as today, then `SourceCard` for `package:<name>`: its switch (the one enable control, unmarked; `switchable` only once approved), its hooks, skills, subagents and built-ins as rows, and its servers nested with their tools and a "List tools" button (`aiCapabilities.refresh`, unmarked: it lists what the approved review showed). A switch change updates the card's Enabled badge. In a pending re-pin's review, a hook whose `id` the current review lacks is badged "New since you last reviewed". The built-in plugin is no longer listed here. Intro: "Plugin packages the assistant may load. Install and approve a pin here; then set each hook, skill, subagent, built-in and server tool."

- [ ] **Step 1: Rewrite the tests**: keep every install, approve, re-pin, file and refusal test; replace `lists the built-in plugins first…`, `keeps a built-in and a package stored under its name apart` (the shadowed package keeps its "Not loaded" notice), `shows ScadBuddy's own skills and subagents on its card…` and `reads a built-in plugin's files` with:
  - `lists only installed packages; ScadBuddy's own is in Built-in`
  - `enables an approved package with its source switch, not user-only`
  - `offers no switch before approval`
  - `turns a skill off after the confirmation`
  - `sets a server tool's access after listing its tools`
  - `marks a hook new since you last reviewed`
  - `marks install, approve, re-pin and remove user-only and leaves every access control unmarked` (replacing `marks every install, approve, enable, re-pin and remove control user-only`)

- [ ] **Step 2: Run them to verify they fail**

Run: `cd frontend && pnpm exec vitest run src/components/settings/PluginPackages.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: `cd frontend && pnpm exec vitest run src/components/settings src/pages/settings src/components/assistant/SkillMenu.test.tsx && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/settings/PluginPackages.tsx frontend/src/components/settings/PluginPackages.test.tsx frontend/src/pages/settings/sections/packages.tsx
git commit -m "feat(frontend): each package's hooks, skills, subagents, built-ins and server tools on the shared rows (#4757)"
```

---

### Task 12: Playwright and the docs

**Files:**
- Modify: `frontend/e2e/plugin-packages.spec.ts:18` (`installs, reviews, approves…`: enable through the source switch), `frontend/e2e/tools-and-plugins.spec.ts`
- Modify: `docs/ai/operating.md:824-` (§9 Plugin packages), `:1172-1214` ("Tool access"), the capabilities API table; `README.md` (the plugin-package bullets)

- [ ] **Step 1: Write the specs**
  - `plugin-packages.spec.ts`: the install test enables the approved package with `Enabled` on `Source greeter`, then sets skill `greet` off (`Lower greet?` → `Lower to off`), reloads and sees it off.
  - `tools-and-plugins.spec.ts`: `switches the browser tab off and on`; `turns a ScadBuddy subagent off and resets it`; `the agent's bridge may set a package skill's access` (#4208, through `bridgeCall` only).

- [ ] **Step 2: Run them**

Run: `cd frontend && pnpm build && pnpm exec playwright test e2e/plugin-packages.spec.ts e2e/tools-and-plugins.spec.ts e2e/settings.spec.ts`
Expected: PASS.

- [ ] **Step 3: Docs.** `operating.md` §9: the derived copy (named by its own hash, re-hashed by both runtimes), what each item kind leaves out, the hook id rule, rows kept across a re-pin and dropped with what the pin lost, the classic Skill/Agent backstop and the durable copy as the one lever, Bash/PowerShell by access per call in both runtimes, colliding server names. "Tool access": item ids (`hook:<event>:<id>`, `skill:`, `subagent:`, `builtin:`), `package:…/mcp:…` on by default, `builtin:browser`'s switch at every gate, the stricter built-in rule, `own_access`. The API table: package sources and their refresh. `README.md`: a package's items are set in Settings → Tools & plugins → Packages. Every external fact links its source (#259).

- [ ] **Step 4: Run the full checks** (agent, agent-durable, frontend; Global Constraints).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e docs/ai/operating.md README.md
git commit -m "test(frontend): package items end to end; docs: package items and the switches (#4757)"
```

---

## Self-review

**Spec and issue coverage**

| Item | Task |
|---|---|
| §5.5 the derived-hash copy fix (`mcp.ts:198-221`: folder named by the pin hash, variants overwrite) | 4 |
| §5.5 / §4.4 the per-turn filtered copy in both runtimes, hashed and sent with its hash | 4, 5 (classic, durable share), 6 (own plugin in agent-durable) |
| §4.4 classic PreToolUse backstop for Skill and Agent; durable's copy the only lever | 5 |
| §5.5 package MCP listing (`ai_tool_listings`) and access (`toolTiers`/`disabledTools` from the resolver) | 2, 9 |
| §4.2 hook ids `hook:<event>:<sha256 first 16>`; survives an unchanged re-pin; changed or new resets and is flagged; stale overrides deleted with the re-pin | 3, 7, 11 |
| §4.2 `allow_refused` built-ins per tool name, the stricter wins | 1, 5, 6 |
| §4.4 stdio servers of allowed packages take the tier through `tierOf` (`turnPackages.ts:196`) | 2 (names), 5 (built-ins); a stdio server's tools: open point 9 |
| §4.7 skills and subagents take their frontmatter description, added to the review | 3, 8, 9 |
| §4.8 a package card nests its servers, hooks, skills, subagents and built-ins | 11 |
| §6 a filtered copy omits a disabled skill, subagent and hook in both runtimes; two variants of one pin coexist | 4, 5, 6 |
| §6 a hook override survives an unchanged re-pin and resets for a changed hook | 7 |
| #4757 comment 1: package server `*` defaults on while approved; enforced in `forwardPackageServers` and durable `turnServers`; switched off not forwarded, tools not listed | 1, 2 |
| #4757 comment 2: `builtin:browser` `*` in `effectiveTool`, durable, flows and `/mcp`, not only the classic seam | 1 |
| #4757 comment 3: an item's effective access reflects its source's `*`; `toolWrite` refuses `on` | 8 |
| Brief: switches defined and shown in the capabilities API | 8 (browser), 9 (packages, servers) |
| Brief: PR 3's deferred `mayPark`/`fixedTier`/keep parity for package servers in durable `turnServers.ts` | 2 |
| Brief: package server name collisions through `harnessToolName` (`a.b`/`a_b`) | 2, 9 |
| Brief: `builtin:own-plugin`'s switch moves from the Packages card into the page | 8, 10, 11 |

**Open points, with the resolution this plan takes (proposed, mark for review):**

1. *Proposed:* a package's own switch stays `ai_plugin_packages.enabled` (approval-gated by the table's CHECK), served as the package source's `enabled`; no `package:<name>` `*` row is read or written. Only its servers' `*` live in `ai_access`, on by default.
2. *Proposed:* item ids are `skill:<name>`, `subagent:<name>` (`<name>` as the review has it after `<pkg>:`, nested agents `dir:name`), `builtin:<Tool>`, and the spec's `hook:<event>:<id>`. Commands are not items (spec §4.2 lists none).
3. *Proposed:* rolling deploy. Every write of this PR lands in `ai_access` or `ai_plugin_packages`, so none is lost. An older replica does not yet enforce the new switches and items (it has no state to mirror into: the legacy keys hold no source switch, and folding the browser switch into `builtin_tools.scadbuddy` would come back through the mirror trigger as per-tool `off` rows that outlive switching it on). They apply on every replica once the rollout ends. `legacy.ts` therefore uses `itemOverrides`, never the folded `registryOverrides`.
4. *Proposed:* "the stricter wins" for a built-in orders `off` ≻ `outward` ≻ `write` ≻ `read`: off refuses, outward asks a person first (and admits fewer principals).
5. *Proposed:* "new since you last reviewed" is shown on the pending re-pin's review (the frontend compares its hook ids with the current review's), which is where spec §4.2 says the person sees it; nothing new is stored.
6. *Proposed:* a hook's id hashes `{ event, matcher, handler }`, so two byte-identical hooks under one event and matcher share an id and one access; they cannot be told apart by content.
7. *Proposed:* a review stored before this PR is read again from the pin's verified cached copy (`reviewOf`, cached per content hash) only when it lacks hook ids or descriptions; no backfill migration.
8. *Proposed:* durable Bash and PowerShell park in the engine as today and ask a new `builtin_tier` activity per call (behind `builtin-access-4757`): `off` refuses, `read`/`write` run unasked, so a change applies from the next call as in a classic turn. A skill, subagent or hook turned off mid-turn applies in a durable turn from its next turn (the copy is per turn; spec §4.4 names it the only lever), and in a classic turn from the next call through the seam.
9. *Proposed:* an allowed package's local (stdio) servers keep `outward` for every tool (`turnPackages.ts:196`), and their tools are not listed on the page: listing would start the package's own process outside a turn. Their names take part in the collision check. Raise as a follow-up if access per stdio tool is wanted.
10. *Proposed:* a package server's tools are listed on "List tools" (the refresh route), not at approval or per turn; until then the server card says "No tools listed yet" and its stored rows still show, as a never-tested remote plugin's do.
11. *Proposed:* the own plugin's old switch route (`PATCH /plugin-packages/scadbuddy`) keeps answering for one release (#4828); the page no longer calls it.
12. *Proposed:* the PR body says `Fixes #4757`.
