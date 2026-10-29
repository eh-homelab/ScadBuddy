# Package items Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A plugin package's hooks, skills, subagents, allowed built-ins and MCP server tools each get an access the user sets in Settings → Tools & plugins, honoured the same way in classic and durable turns; the `builtin:browser` and package-server `*` switches are enforced at every gate; ScadBuddy's own skills and subagents move onto the page. Closes #4757.

**Architecture:** The access model learns on/off items (`agent/src/access/items.ts`) and two more switches, in TypeScript and Python with the shared vectors. Each turn builds one *derived copy* of every loaded package (and of the own plugin when something in it is off): its http servers stripped, its off hooks, skills and subagents left out, named and checked by the copy's own hash, so both runtimes' existing hash checks hold it. A Skill, Agent or Task call naming an `off` item is refused per call in both runtimes (classic: the permission seam; durable: the segment runner's own PreToolUse hook), by one rule the shared vectors pin; Bash, PowerShell and the other allowed built-ins are decided per call by their access in both. Hooks change per turn in both (technically forced: no SDK option drops one plugin hook from a running query, spec §2). A third capabilities provider serves `package:<name>` and `package:<name>/mcp:<server>` sources; the Packages section nests the shared `SourceCard`.

**Tech Stack:** TypeScript (Node 24, Hono 4, zod 4, `@modelcontextprotocol/sdk`, postgres.js, vitest, Temporal TS SDK); Python 3.14 (temporalio, psycopg, pytest); React 19, Vite, vitest + Testing Library + msw, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` (§2 "Package contents reach a turn", "Disabling one hook, skill or subagent"; §4.1, §4.2, §4.4, §4.6, §4.7, §4.8; §5 item 5; §6). Issue #4757 with its three comments (the package-server `*`, the `builtin:browser` `*` in every runtime, the capabilities API reflecting a source's `*`, and `toolWrite`'s refusal of `on`). PR 4's plan `docs/superpowers/plans/2026-10-29-tools-and-plugins-page.md`, "How PR 5 adds packages without reshaping this".

**Base:** branch `feat/2171-package-items`, rebased onto `origin/main` after PR 4 merged (#4993, squash `9f5ea1f1c`). Line references are valid at that HEAD (PR 4's squash leaves every cited file as it was at `4a27fd5fd`).

## Global Constraints

- Agent check: `cd agent && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`.
- agent-durable check: `cd agent-durable && ../.github/scripts/agent-durable-pin.sh . && uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy && uv run --frozen pytest` (uv ≥ 0.9.0; use the image's uv, `uvx uv@<Dockerfile version>`, never the local 0.6.16, which rewrites `uv.lock`).
- Frontend check: `cd frontend && corepack enable && pnpm install --frozen-lockfile && pnpm lint && pnpm typecheck && pnpm test && pnpm build`; Playwright `pnpm exec playwright test`. Run `pnpm gen:api` once before a single `pnpm exec vitest run <file>`.
- Postgres tests: `SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test`, a role with `CREATEDB`. Temporal tests: `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` or `temporal` on `PATH`.
- No agent migration is needed: `ai_access` already allows `on` on `package:%` and `builtin:own-plugin` items and any `*` on a package-server source; `ai_tool_listings` already takes `package:` sources (`agent/src/db/migrations/20261026T2200Z_access.sql`). If one turns out to be needed it is a NEW file `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one.
- Access values: a tool and an allowed built-in take `off`, `read`, `write`, `outward`; a hook, skill or subagent takes `off`, `on` (spec §4.1). One select per item.
- Lowering asks one confirmation line, and that is the whole guard. No `USER_ONLY` on any access select, Reset, source switch (the package's and the own plugin's included) or the confirmation; no `fixed` item beyond what is technically forced (tools or package servers colliding on one name; a renamed tool takes only `off`/`outward`); no extra warning (#4208). Install, Approve (and its `allow_refused` confirmation), Fetch re-pin, Approve re-pin, Discard re-pin and Remove keep their `USER_ONLY` marks: they decide which code is trusted, not an item's access.
- Every tool works in every mode: an item has the same access in a classic and a durable turn, a flow run and `/mcp`, and a change applies from the next call in both. The one thing that changes only per turn, in both runtimes alike, is a hook: no SDK option drops one plugin hook from a running query (spec §2), so that is technically forced. Where the runtimes differ in how, the plan gives each its own lever; it never documents a gap.
- New durable state lives in Postgres (`ai_access`, `ai_tool_listings`); package copies on disk are derived caches, re-hashed before use, never trusted as found.
- Temporal: the agent-durable `DurableSession` change (Bash and PowerShell by access) goes behind `workflow.patched("builtin-access-4757")` and replays `agent-durable/tests/fixtures/durable_session_histories/`. The segment runner's Skill/Agent hook is inside an activity and needs no patch; a listing's `may_park` is an activity result, so changing it needs none either. No change to `AgentOperation` or the backend's workflows.
- Rolling deploy loses no write: every write of this PR lands in `ai_access` (or `ai_plugin_packages`), which both old and new replicas read. The new switches and items have no older state to mirror into; an older replica ignores them until it is replaced (see "Open points" 3). Never fold a `*` switch into the legacy keys `access/legacy.ts` writes (the mirror trigger would copy it back as per-tool rows). A new activity an older agent may not serve (`builtin_tier`) is called with at most 3 attempts and parks on failure. Rows an older replica's re-pin approval leaves for items its pin lost are pruned by the next new-code write to that package (Task 7 `pruneStaleRows`, run by every `PackageStore` write); no sweep.
- Generated API files (`backend/openapi.json`, both `schema.d.ts`) are never committed.
- Commits: conventional titles (`feat(agent):`, `feat(agent-durable):`, `feat(frontend):`, `test(frontend):`, `docs:`), each ending with the session's trailers `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_0132q9E8kdzKHMu5F5oHC8QQ`.
- Not in this PR: subagents parking and durable subagents getting every tool (PR 6, #2660); the MCP access and Audits areas (PR 7, #781); dropping the legacy keys (#4828).

## Review Focus

1. **Two turns at once with different access to one pin** (a skill turned off between them). Expected: both copies exist side by side, each named by its own hash; neither overwrites the other, and the earlier turn's copy still hashes to what it was handed. Test: Task 4 `two variants of one pin coexist`.
2. **The own plugin in a repository checkout**, where `agent/plugins/scadbuddy/skills` and `agents` are symlinks (`ownPlugin.ts:7-14`). Expected: its derived copy dereferences them (`hashTree` refuses links) and loads. Test: Task 4 `derives a copy of a directory whose skills are symlinks`.
3. **Two allowed packages naming `Bash`, one at `write` and one at `off`, or one at `write` and one unset.** Expected: the stricter wins in both runtimes (`off` ≻ `outward` ≻ `write` ≻ `read`, an unset package counting as `outward`): `off` offers Bash in neither runtime, `write` with unset asks first in both. Tests: Task 1 `the stricter built-in access wins`; Task 5 `A names Bash at write, B names it unset: outward in both runtimes`, `one package off, one write: Bash offered in neither runtime`; Task 6 `test_bash_off_is_refused_and_lowered_runs_unasked`.
4. **A hooks file rewritten with its keys reordered or reindented, and one whose hook URL changed.** Expected: the first keeps every hook's id (and override); the second gives that hook a new id, back at `on`, marked "New since you last reviewed" on the pending re-pin. Tests: Task 3 `a hook's id ignores key order and whitespace`, `a hook's id changes with its url`; Task 7 `an override survives a re-pin that keeps the hook, and a changed hook comes back on`; Task 11 `marks a hook new since you last reviewed`.
5. **A package whose stored review predates hook ids and descriptions** (installed before this PR). Expected: its hooks, skills and subagents still list with ids and descriptions, read from its verified cached copy, and switching one works. Test: Task 9 `a review stored without hook ids reads them from the verified copy`.

Also covered where they belong: a package server or remote plugin tool raised to `outward` mid-turn parking on its next durable call (Task 2); a skill switched off mid-turn refused on its next call in both runtimes (Tasks 5, 6, one `calls` vector set); a subagent named by its frontmatter `name` rather than its file (Tasks 3, 5, 6); a pending re-pin review without hook ids keeping the overrides (Task 7); package servers named alike through `harnessToolName` (`a.b` and `a_b`) fixed off with the reason (Tasks 2, 9); an item under a switched-off source shown `off` with its own access kept (Task 8); an unapproved package's switch answering 409 (Task 9); a refresh of a package server that is down keeping the last listing (Task 9).

---

## File map

Agent (new): `src/access/items.ts`, `src/plugins/packages/derived.ts`, `src/plugins/packages/listing.ts`, `src/access/packageProvider.ts`.
Agent (modified): `src/access/{resolve,resolver,store,legacy,live,capabilities,providers}.ts`, `src/plugins/packages/{vet,mcp,builtins,store,install}.ts`, `src/temporal/{turnPackages,turnServers,turnTools,toolTier,toolActivities}.ts`, `src/sessions/turnRunner.ts`, `src/routes/{capabilities,pluginPackages}.ts`, `src/main.ts`; `test/fixtures/access-vectors.json` (regenerated).
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
  - `builtin(name: string, packages: readonly string[]): Promise<ToolAccess>`: for each package given, its `package:<p>` row `builtin:<name>`, or `outward` when it has none; the `strictest` of those (`outward` for an empty list). The caller passes the packages that name the tool.
  - `packageAccess(sources: readonly string[]): Promise<Map<string, PackageAccess>>` with `export type PackageAccess = { off: { skills: Set<string>; subagents: Set<string>; hooks: Set<string>; builtins: Set<string> }; builtinTiers: Map<string, ToolAccess> }`, one read for every source given.
- Produces (`store.ts`): `AccessWriter.setItem(source, item, access: Access, context?)`; `checkItem(source, item, access)` refuses (400) `on` on a tool, a tier on a hook/skill/subagent, and an on/off item outside `package:` and `builtin:own-plugin`.
- Python: `source_enabled` is true by default for `builtin:` and for a `package:…/mcp:…` source; `sanitise_overrides` disables every `browser_*` entry while `builtin:browser` `*` is `off` (read with the registry rows `gate_settings` already reads, `access.REGISTRY_SOURCES`).

- [ ] **Step 1: Write the failing tests**
  - `accessResolver.test.ts`: `a package server is on until switched off`; `builtin:browser switched off disables every browser tool in registry() and registryOverrides().disabled()`; `registryOverrides().of a browser tool answers disabled while its source is off`; `switchOn reads an off row as off and anything else as on`; `the stricter built-in access wins` (`write` and `off` → `off`; `read` and `outward` → `outward`; `write` and unset → `outward`; no package → `outward`); `packageAccess reads every package's items in one query`.
  - `accessStore.pg.test.ts`: `stores on for a package skill and the own plugin's subagent`; `refuses on for a tool, a tier for a hook, and on outside a package (400)`.
  - `access.settingsMirror.pg.test.ts`: `switching builtin:browser off writes no browser tool into builtin_tools.scadbuddy` (the legacy key unchanged, no `migration:2171` rows appear).
  - `capabilities.gates.test.ts`: `builtin:browser switched off hides browser tools from /mcp`, `… answers disabled from /internal/flows/tools`, `… answers disabled from the durable TOOL_TIER_ACTIVITY`, `… answers disabled from the subagent loopback` (`routes/subagentMcp.ts:107`).
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
- Modify: `agent/src/temporal/turnServers.ts:375-387` (package servers get `mayPark`, `fixedTier`, `keep`, `listingKey`), `:209` (remote plugins: `mayPark: () => true`, fixing PR 3's case)
- Modify: `agent/src/temporal/turnTools.ts:81-142` (`TurnServer.offTools`), its listing (`:520-540`) and call path (leave out and refuse an `off` tool)
- Test: `agent/test/turnPackages.test.ts`, `agent/test/turnTools.test.ts`, `agent/test/plugins.access.pg.test.ts`

**Interfaces:**
- Consumes: Task 1 `AccessResolver.enabled` (with the package-server default), `packageServerSource`; `pluginFingerprint` (`forwarder.ts:709`).
- Produces (`mcp.ts`):
  - `export type PackageServerConfig = { pkg: string; server: string; name: string; plugin: RemotePlugin; address: string }`
  - `export async function packageServerConfigs(loaded: readonly LoadedPackage[], resolve: Resolver, access?: Pick<AccessResolver, 'plugin' | 'enabled'>): Promise<{ configs: PackageServerConfig[]; problems: string[] }>`: every http server that passes today's checks, its tiers from `access.plugin`, left out (no problem) while `access.enabled(packageServerSource(pkg, server))` is false; servers whose `packageServerName` is equal (`a.b` and `a_b`, `s.x` and `s_x`) are all left out with one problem each: `plugin package <pkg>: MCP server "<server>" is named <name>, as <others> are, so none of them is offered`.
  - `export function collidingServerNames(loaded: readonly LoadedPackage[]): Map<string, { pkg: string; server: string }[]>` (names with more than one owner, http and local servers alike; pure).
  - `export type ServerAccess = { enabled: boolean; toolTiers: Record<string, RiskTier>; disabledTools: string[] }`; `export async function serverAccess(loaded, access?): Promise<Map<string, ServerAccess>>` (by `packageServerSource`, one read for every declared server, http and local).
  - `PackageServers.keep(name: string): { fingerprint: string; register(): Registration } | undefined`, as `forwardForRun`'s (`forwarder.ts:734-740`).
- Produces (`turnTools.ts`): `TurnServer.offTools?: ReadonlySet<string>` (bare tool names): left out of the listing and refused on call, as the forwarder does for an http server's disabled tools.
- Produces (`turnPackages.ts`): `localPackageServers(loaded, problems, access: ReadonlyMap<string, ServerAccess>)`: a switched-off stdio server is not offered; each tool's `tierOf` is its stored tier, else `outward`; its `off` tools are `offTools`. Its tools are still not listed on the page (listing starts the process, open point 9).
- Produces (`turnServers.ts`): every forwarded package server and every remote plugin: `mayPark: () => true` (a tool listed at `read` and raised to `outward` mid-turn must still be asked about; only a `may_park` tool ever is, `turnTools.ts:104-108`), `fixedTier: (n) => tierOf(n)` for the transcript only (browser navigate excepted, as `:210`); package servers also `listingKey: JSON.stringify(plugin.disabledTools)` and `keep` from `forwardedPackages.keep(plugin.name)`. The workflow's `_outward` (`turn_tool_tier`, read at the call) then decides each call.

- [ ] **Step 1: Write the failing tests**
  - `turnPackages.test.ts`: `a package server switched off is not forwarded and its tools are not listed`; `a package server is forwarded with no row (on by default)`; `servers named alike through harnessToolName are none of them offered, each said` (packages `a.b` and `a_b`, each with server `s`); `a local server named like another package's is not started`; `a local server switched off is not started`; `a local server's tool takes its stored tier, and an off one is neither listed nor run`.
  - `turnTools.test.ts`: `a package server tool raised to outward mid-turn parks on its next durable call`; `a remote plugin tool raised to outward mid-turn parks on its next durable call`; `a package server's read tool shows its fixed tier in the transcript`; `a package server's MCP session is kept across calls like a remote plugin's (#2581)`.
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
git add agent/src/plugins/packages/mcp.ts agent/src/temporal/turnPackages.ts agent/src/temporal/turnServers.ts agent/src/temporal/turnTools.ts agent/test/turnPackages.test.ts agent/test/turnTools.test.ts agent/test/plugins.access.pg.test.ts
git commit -m "feat(agent): package MCP servers switch off, refuse colliding names, and every plugin tool may park in durable (#4757)"
```

---

### Task 3: Vetting: hook ids, where each item lives, its names and descriptions

**Files:**
- Modify: `agent/src/plugins/packages/vet.ts:69` (`ReviewHook`), `:73-97` (`PackageReview.described`), `:322-333` (`hookHandlers` keeps the group's `matcher`), `:340-384` (`skillNames`, `markdownIn` also answer paths), `:432-458`, `:482-500`
- Modify: `frontend/src/api/aiPlugins.ts` (`ReviewHook.id?`, `matcher?`; `PackageReview.described?`)
- Test: `agent/test/pluginPackages.test.ts`, `agent/test/pluginPackageKinds.test.ts`

**Interfaces:**
- Produces (`vet.ts`):
  - `export function hookId(event: string, matcher: unknown, handler: unknown): string`: the first 16 hex of the sha256 of the canonical JSON of `{ event, matcher, handler }` (keys sorted at every level, no whitespace, numbers as `JSON.stringify` prints them; not `gate/canonical.ts`, which refuses numbers, and a hook's `timeout` is one).
  - `ReviewHook = { event: string; type: string; url?: string; id?: string; matcher?: string }` (`id` always set by this vetting; optional only for reviews stored before it).
  - `PackageReview.described?: Record<string, string | null>`: each item's id (`skill:<name>` for a skill or command, `subagent:<name>`) to its frontmatter `description`, so a skill and a subagent of one name keep their own (Task 3 review I1). Descriptions, matchers, frontmatter names and hook events are cut to `LISTING_CAPS` (`description`, `title`, `name`), each cut named in `PackageReview.cut`; a frontmatter `name` past the name cap is an allowable refusal (I4).
  - `PackageReview.aliases?: Record<string, string[]>`: per kind, a frontmatter `name` that differs from its item's name to every item declaring it, both as item ids (`subagent:deep-review` → [`subagent:review:deep`]; I3: one map, first wins, let an off item be called). Claude Code's `subagent_type` is the frontmatter `name` (agent-durable `subagents.py:107` keys by it), which a package may set apart from the file name.
  - Commands are skill items: Claude Code invokes a slash command through the Skill tool (re-measured in the SDK 0.3.310 bundled CLI, Claude Code 2.1.310, `strings -n 20 node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude | grep 'invoke it via the Skill tool'`: "If it's a slash command, invoke it via the Skill tool", and frontmatter `disable-model-invocation`: "If true, the model cannot invoke this via the Skill tool; only users can type the slash command"). A command `<pkg>:<cmd>` is item `skill:<cmd>`; a skill and a command of one name are one item.
  - `export function packageItems(root: string, manifest: Record<string, unknown>): { skills: Map<string, string[]>; subagents: Map<string, string>; hooks: { id: string; event: string; where: string }[]; aliases: Map<string, string[]> }`: each skill's directory and each command's file or manifest key (paths relative to `root`; a skill and a command of one name both listed), each subagent's file, each hook with the config it is in (`declaredConfigs(...).hooks[].where`), and the frontmatter names. Task 4 deletes and rewrites by it, and gets four path forms (the `PackageItems` comment): a skill at `.` (the root is the skill; never delete it), an inline command `inlineCommandAt(<name>)` (compare, never parse: the key is untrusted), a hook in `plugin.json hooks` (inline in the manifest), and a manifest hook path normalised relative to the root (`./hooks/x.json` is `hooks/x.json`).

- [ ] **Step 1: Write the failing tests** (`pluginPackages.test.ts`)
  - `gives each hook an id from its event, matcher and handler`
  - `a hook's id ignores key order and whitespace`
  - `a hook's id changes with its url`
  - `two byte-identical hooks share an id` (one access covers both; open point 6)
  - `records each skill's and subagent's frontmatter description`
  - `packageItems finds a manifest-named skill directory and a nested subagent file` (`agents/review/deep.md` is `review:deep`)
  - `lists a command as a skill item, one with a skill of its name`
  - `records a subagent's frontmatter name where it differs from its file`

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/pluginPackages.test.ts test/pluginPackageKinds.test.ts`
Expected: FAIL.

- [ ] **Step 3: Re-measure, then implement.** Confirm the command-through-Skill strings in the pinned binary (`agent/package.json` pins SDK 0.3.310): `strings -n 20 node_modules/@anthropic-ai/claude-agent-sdk-linux-x64/claude | grep "invoke it via the Skill tool"`. If they are gone, commands are not items: drop the two command bullets and say so in the PR. Then implement as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/pluginPackages.pg.test.ts`, and `cd frontend && pnpm gen:api && pnpm typecheck`.
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
  - `export type Omitted = { skills: ReadonlySet<string>; subagents: ReadonlySet<string>; hooks: ReadonlySet<string>; servers: ReadonlySet<string> }` (item names: skill and subagent names as Task 3's maps key them, hook ids, and the local servers switched off); `export const NOTHING_OMITTED: Omitted`.
  - `export async function derivedPackage(dir: string, contentHash: string | undefined, root: string, omitted: Omitted = NOTHING_OMITTED): Promise<{ dir: string; contentHash: string | undefined }>`: `dir` and `contentHash` unchanged when it declares no http server and nothing it holds is omitted (the own plugin has no pin hash, and in a checkout `hashTree` refuses its links, so it passes `undefined`; only a copy is hashed); else a copy (`cp(dir, tmp, { recursive: true, dereference: true })`) with its http servers stripped (today's rules), each omitted skill's directory and subagent's file removed (and its entry in a manifest list), each omitted hook's handler removed from its config (a group left with no handler goes, an event left with no group goes), each omitted local server removed from its MCP config, then hashed; the folder is `root/<that hash's hex>`. A folder already there is kept only while it hashes to that; the rename race as today (`mcp.ts:224-229`). It fails when the copy still declares an http server or an omitted item.

- [ ] **Step 1: Write the failing tests** (`derivedPackage.test.ts`)
  - `answers the package itself when nothing changes`
  - `strips http servers, keeping local ones` (moved), `strips a manifest whose mcpServers is a list` (moved), `makes the copy again when the one there was changed` (moved)
  - `omits a disabled skill, command, subagent, hook and local server, and nothing else` (the copy's `vetPackage` review lacks exactly those)
  - `answers the own plugin as it is with no pin hash, and hashes only a copy`
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

### Task 5: Each turn's filtered copies, the call rule and built-ins by access (agent)

**Files:**
- Modify: `agent/src/access/items.ts` (`offCallItem`), `agent/src/access/live.ts` (`LiveTurn.plugins`, `LiveTurn.builtins`; local package servers in `LiveTurn.packageServers`)
- Modify: `agent/src/sessions/turnRunner.ts:311-339` (one `packageAccess` read per turn; the own plugin and each package loaded as `derivedPackage`), `:349-358` (`liveAccess` gets the turn's plugins, built-ins and local servers), `:464-469`, `:585` (an `off` built-in is not offered), and the run's `disallowedTools` (each `off` tool of a local package server, `mcp__plugin_<pkg>_<server>__<tool>`)
- Modify: `agent/src/temporal/turnPackages.ts:54-81` (`sharePackage(pkg, root, localRoot, omitted, builtinsOff)`)
- Modify: `agent/src/temporal/turnServers.ts:301-370` (reads `packageAccess` once per listing or call over every loaded package; the turn's one `builtinsOff`; the `builtin` server's `tierOf` from `builtin(name, packagesNaming(name))`, its `named` without `builtinsOff`)
- Modify: `agent/src/temporal/toolTier.ts` (`BUILTIN_TIER_ACTIVITY`), `agent/src/temporal/toolActivities.ts:330` (registers it), `agent/src/main.ts:498-499`, `:704-707` (deps)
- Modify: `agent/test/accessVectors.test.ts` (the `calls` vectors), `agent/test/fixtures/access-vectors.json` (regenerated)
- Test: `agent/test/accessLive.test.ts`, `agent/test/turnRunner.packages.test.ts` (new), `agent/test/turnPackages.test.ts`, `agent/test/toolActivities.test.ts`

**Interfaces:**
- Consumes: Task 1 `packageAccess`, `switchOn`, `builtin`, `pluginOfName`, `skillItem`, `subagentItem`, `strictest`; Task 2 `serverAccess`; Task 3 `PackageReview.aliases`; Task 4 `derivedPackage`, `Omitted`; `OWN_PLUGIN_DIR`, `OWN_PLUGIN_NAME`.
- Produces:
  - `export type CallPlugin = { name: string; source: string; aliases: Readonly<Record<string, readonly string[]>> }` and `export function offCallItem(tool: string, input: unknown, plugins: readonly CallPlugin[], rows: readonly AccessRow[]): string | undefined` (`items.ts`, pure): for `Skill` the `input.skill`, for `Agent`/`Task` the `input.subagent_type`; a qualified name (`<plugin>:<name>`) is looked up in its plugin, an unqualified one in every plugin; the items a name reaches are its direct item (`skill:<name>` for `Skill`, `subagent:<name>` for `Agent`/`Task`) and every target of the plugin's `aliases[<that id>]` (per kind, many-valued); the answer is the first of those whose row is `off` (fail closed: any one off refuses the call), else `undefined`. agent-durable ports it (Task 6); the `calls` vectors pin both.
  - `LiveTurn.plugins: readonly CallPlugin[]` (each loaded package `package:<name>`, the own plugin `scadbuddy` → `builtin:own-plugin`); `liveAccess` reads those sources' rows per call and answers `off` when `offCallItem` names an item, else leaves the call to the turn's tier.
  - `LiveTurn.builtins: readonly { name: string; packages: readonly string[] }[]` (the turn's allowed packages that name each built-in): `liveAccess(name)` answers `resolver.builtin(name, packages)`.
  - `export function omittedOf(access: PackageAccess | undefined, servers: ReadonlyMap<string, ServerAccess>): Omitted`.
  - `export function builtinsOffOf(loaded: readonly LoadedPackage[], access: ReadonlyMap<string, PackageAccess>): Set<string>`: the built-ins whose `strictest` over the packages that name them (unset counting `outward`) is `off`. One set for the turn; classic filters `packages.builtinTools` by it, durable passes it to every `sharePackage`.
  - `sharePackage(pkg, root, localRoot, omitted = NOTHING_OMITTED, builtinsOff: ReadonlySet<string> = new Set()): Promise<TurnPackage>` (the folder and `content_hash` the derived copy's; `builtin_tools` without `builtinsOff`).
  - `export const BUILTIN_TIER_ACTIVITY = 'builtin_tier'`; `builtinTier(deps: { access: Pick<AccessResolver, 'builtin'> }): (args: { tool: string; packages: string[] }) => Promise<{ tier: ToolAccess }>`: `builtin(tool, packages)`. The caller names the packages (durable: the listing's packages that name the tool), so both runtimes ask about the same set.

- [ ] **Step 1: Write the failing tests**
  - `accessLive.test.ts`: `refuses a Skill call naming a skill set off mid-turn`; `refuses an Agent call naming a subagent set off, qualified or not`; `refuses an Agent call naming a subagent by its frontmatter name`; `refuses a Skill call naming a command set off`; `leaves a Skill call naming an on skill to the turn's tier`; `a classic Bash call takes the stricter access of the packages naming it`; `A names Bash at write, B names it unset: outward in both runtimes` (here, and in `toolActivities.test.ts` through `builtin_tier`); `a lowered Bash runs at its access` (`write` → no park); `a local package server's tool takes its stored tier`.
  - `accessVectors.test.ts`: new `calls` vectors over `offCallItem`: `a qualified skill off`, `a qualified skill of another plugin`, `an unqualified subagent off in one plugin`, `a subagent by its frontmatter name`, `Task as Agent`, `a tool that is not Skill, Agent or Task`, `an input without the field`.
  - `turnRunner.packages.test.ts` (over a stub `AccessResolver`, a temp package and `OWN_PLUGIN_DIR`): `a disabled skill is absent from the package Claude Code loads`; `a disabled hook is absent from the hooks Claude Code loads`; `the own plugin is loaded as a copy without its disabled subagent`; `nothing off loads the package and the own plugin as they are`; `one package off, one write: Bash offered in neither runtime` (here for classic, and in `turnPackages.test.ts` for the durable listing); `a local server switched off is not in the copy, and an off tool is disallowed`.
  - `turnPackages.test.ts`: `shares the filtered copy under its own hash, with that hash` (the `TurnPackage.content_hash` hashes the shared folder, and differs from the pin's); `one package off, one write: Bash offered in neither runtime`; `the builtin turn tools take each tool's access`.
  - `toolActivities.test.ts`: `builtin_tier answers the stricter access of the packages it is given, an unset one outward`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/accessLive.test.ts test/accessVectors.test.ts test/turnRunner.packages.test.ts test/turnPackages.test.ts test/toolActivities.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces; regenerate the vectors (`UPDATE_ACCESS_VECTORS=1`). Remove `withoutRemoteServers` once `turnRunner.ts:330` and `turnPackages.ts:55` call `derivedPackage`.

- [ ] **Step 4: Run them to verify they pass**

Run: the Step 2 command, then `pnpm vitest run test/harnessWiring.test.ts test/pluginPackages.e2e.test.ts test/builtins.durable.e2e.test.ts` (the last with Temporal and Postgres) and `pnpm build`.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src agent/test
git commit -m "feat(agent): each turn loads packages and the own plugin without what is off; Skill, Agent and built-ins by access per call (#4757)"
```

---

### Task 6: agent-durable: the own plugin's items, the per-call Skill/Agent rule, and Bash and PowerShell by access

**Files:**
- Modify: `agent-durable/src/scadbuddy_durable/session/access.py` (`off_call_item`, the port of Task 5's `offCallItem`)
- Modify: `agent-durable/src/scadbuddy_durable/session/runner.py:384-392` (`TurnSettings.own_off`), `:748-761` (read from the rows already read), `:1075-1092` (the own plugin as a filtered copy; `_subagent_tools` without `off` subagents; the call hook merged with `calls.hooks()`)
- Modify: `agent-durable/src/scadbuddy_durable/session/workflow.py:106-114` (`BUILTIN_TIER`, `BUILTIN_ACCESS_PATCH = "builtin-access-4757"`), `:600-627` (`_park`: an engine-parked built-in asks its access first)
- Modify: `agent-durable/src/scadbuddy_durable/session/models.py:166-175` (`TurnPackage.aliases`), `agent/src/temporal/turnTools.ts:215-222` and `agent/src/temporal/turnPackages.ts:80` (`aliases` in the listing)
- Test: `agent-durable/tests/test_access_vectors.py`, `agent-durable/tests/test_session_runner.py`, `agent-durable/tests/test_session_workflow.py`, `agent-durable/tests/test_packages.py`

**Interfaces:**
- Consumes: Task 1's item ids (`skill:<name>`, `subagent:<name>`); Task 5's `calls` vectors and `builtin_tier` (`{tool, packages}` → `{tier}`, on `agent-tools`); `TurnPackage` (`models.py:166`); `hash_tree` (`package_hash.py`).
- Produces:
  - `def off_call_item(tool: str, tool_input: Any, plugins: Sequence[CallPlugin], rows: Sequence[AccessRow]) -> str | None` in `access.py`, with `CallPlugin(name, source, aliases)`: the same rule as `offCallItem`, held to it by the `calls` vectors.
  - The segment runner's call hook: in `run()`, the turn's plugins (`scadbuddy` → `builtin:own-plugin` while it is loaded; each `context.packages[].name` → `package:<name>`; aliases from the agent's `TurnPackage`, see below), and a `PreToolUse` `HookMatcher(matcher="Skill|Agent|Task", hooks=[…])` merged into `extra["hooks"]` (`merged_hooks`, `:697`) whose callback reads `access.read_rows(conn, sources)` per call and answers `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "<name> is off in Settings → Tools & plugins"}}` when `off_call_item` names an item, and the same deny with `"tool access could not be read"` when the read fails (fail closed); `{}` otherwise. The plugin keeps a hook's decision for these tools (`_guard_hooks` drops it only for the durable prefix and `tool_activities`, `temporalio/claude_agent_sdk/_runner.py` at the `uv.lock` pin), and `_defer_hook` gives them none, so the deny stands.
  - `TurnPackage.aliases: dict[str, list[str]] = {}` in `models.py` and `aliases` in the agent's `TurnPackage` (`turnTools.ts:215`), filled by `sharePackage` from the review (Task 3); a listing result, so no patch.
  - `TurnSettings.own_off: frozenset[str] = frozenset()`: the `builtin:own-plugin` items whose row is `off`.
  - `def own_plugin_copy(plugin_dir: str, off: frozenset[str], root: str) -> str` in `runner.py`: `plugin_dir` when nothing is off; else a copy (`shutil.copytree(..., symlinks=False)`) without `skills/<name>/` for each `skill:<name>` and `agents/<name>.md` for each `subagent:<name>` (matched by file stem or frontmatter `name`), placed at `root/<hash_tree hex>` (kept when one there hashes to it; atomic rename). `root` is a new `SegmentRunner` argument, default `os.path.join(tempfile.gettempdir(), "scadbuddy-own-plugin")`.
  - The inline subagent definitions (`subagents.with_server(self._agents, …)`, keyed by frontmatter `name`, `subagents.py:107`) leave out each `off` one.
  - `_park`: for `name in ENGINE_PARKED` and `workflow.patched(BUILTIN_ACCESS_PATCH)`: `builtin_tier` with `{"tool": name, "packages": [p.name for p in self._packages if name in p.builtin_tools]}`, `RetryPolicy(maximum_attempts=3, non_retryable_error_types=["UnknownSession", "ToolError"])` as `_outward` has it (`workflow.py:883-895`); `off` refuses the call (`self._turn.replaced[call_id]` = `"<name> is off in Settings → Tools & plugins"`, then `decide(False)`), `read`/`write` decide `True` with no entry, `outward` or a failure parks as today.

- [ ] **Step 1: Write the failing tests**
  - `test_access_vectors.py`: `test_calls` over every `calls` vector.
  - `test_session_runner.py` (against `fake_anthropic.py`): `test_a_skill_set_off_mid_segment_is_refused_on_its_next_call`; `test_a_subagent_set_off_mid_segment_is_refused_on_its_next_call`; `test_a_call_is_refused_when_access_cannot_be_read`; `test_own_plugin_copy_leaves_out_off_skill_and_subagent`; `test_own_plugin_loads_as_it_is_with_nothing_off`; `test_off_subagent_is_not_defined_inline`.
  - `test_packages.py`: `test_a_filtered_copy_hashing_to_its_listing_loads` (the test builds the filtered copy itself, removing one skill from a fixture package, and lists it under its own `hash_tree`, as `test_the_runner_loads_only_a_copy_that_hashes_to_its_pin`, `:47`, does; hash parity with the agent stays pinned by `test_the_hash_is_the_agents`, `:31`).
  - `test_session_workflow.py`: `test_bash_off_is_refused_and_lowered_runs_unasked` (a fake `builtin_tier` answering `off`, then `write`, then `outward`: refused, run with no `ai_pending_input` row, parked); `test_builtin_tier_failing_parks_after_three_attempts`; `test_a_turn_from_before_builtin_access_replays` (a new history `tests/fixtures/durable_session_histories/bash.json`, recorded on the base commit, of a turn whose allowed package's Bash call parked, replays; as `test_a_turn_from_before_tool_access_replays`, `:1221`), and that test still passes.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent-durable && uv run --frozen pytest tests/test_access_vectors.py tests/test_session_runner.py tests/test_session_workflow.py tests/test_packages.py`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces.

- [ ] **Step 4: Run them to verify they pass**

Run: the agent-durable check (Global Constraints), and `cd agent && pnpm vitest run test/turnPackages.test.ts test/evals.durable.e2e.test.ts` (the second with Temporal and Postgres).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent-durable/src agent-durable/tests agent/src/temporal agent/test
git commit -m "feat(agent-durable): Skill and Agent refused per call by access; the own plugin without what is off; Bash and PowerShell by access (#4757)"
```

---

### Task 7: A package's rows over its lifecycle: install, re-pin, remove

**Files:**
- Modify: `agent/src/plugins/packages/store.ts:262-309` (`approve`), `:205-222` (`create`), `:326-331` (`delete`), `:311-324` (`setEnabled`), `:223-248` (`setPending`), `:250-260` (`discardPending`) (each calls `pruneStaleRows`)
- Test: `agent/test/pluginPackages.pg.test.ts`

**Interfaces:**
- Consumes: Task 3 `ReviewHook.id`, `PackageReview.skills`/`agents`/`builtin_tools`/`mcp_servers`; `forgetSource` (`access/store.ts:61`).
- Produces: in the same transaction, with `notifyWarmEvict` as today:
  - `create` deletes every `ai_access` and `ai_tool_listings` row of `package:<name>` and of each `package:<name>/mcp:%` source (`starts_with(source, 'package:<name>/mcp:')`, never `LIKE`: names may hold `_`), so a re-install starts at the defaults;
  - `delete` deletes the same;
  - `async function pruneStaleRows(tx: TransactionSql, name: string, review: PackageReview): Promise<void>`: deletes the `package:<name>` rows whose item the current review no longer has (a hook id, `skill:`, `subagent:`, `builtin:`) and the rows and listings of servers it no longer declares; it deletes no hook row while any of the review's hooks has no `id` (a review from an older replica or from before this PR: it cannot tell a kept hook from a lost one).
  - every `approve` (of a pending pin or the current one), `setEnabled`, `setPending` and `discardPending` runs it on the review that is current after the write, so rows an older replica's approval left behind go with the next new-code write to that package; there is no sweep.

- [ ] **Step 1: Write the failing tests** (`pluginPackages.pg.test.ts`)
  - `an override survives a re-pin that keeps the hook, and a changed hook comes back on`
  - `a re-pin drops the rows of a skill and a server the new pin no longer has`
  - `removing a package forgets its items, its servers' rows and their listings`
  - `a re-install starts at the defaults`
  - `a package named a_b keeps its rows when a.b is removed`
  - `a pending review without hook ids keeps the hook overrides`
  - `rows an older replica's approval left are pruned by the next write to the package`

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
git commit -m "feat(agent): a package's access rows follow its pin: kept across a re-pin, pruned of what it lost (#4757)"
```

---

### Task 8: Capabilities: a source's switch on its items, a write per kind, the browser switch and the own plugin's items

**Files:**
- Modify: `agent/src/access/capabilities.ts:23-44` (`ItemView.own_access`), `:92-176` (`Capabilities` applies the source switch to every view it answers)
- Modify: `agent/src/access/providers.ts:21-27` (`builtin:browser` `switchable: true`), `:80-85`, `:294-297` (`toolWrite` goes), `:97-160` (the own plugin's items)
- Modify: `agent/src/plugins/packages/builtins.ts:64` (export `reviewOf`)
- Modify: `frontend/src/api/capabilities.ts` (`ItemView.own_access`), `frontend/src/components/capabilities/access.ts:23-27` (`shownAccess` reads `own_access`)
- Test: `agent/test/capabilities.test.ts`, `agent/test/capabilities.remote.test.ts`, `frontend/src/components/capabilities/access.test.ts`

**Interfaces:**
- Consumes: Task 1 `itemKindOf`, `skillItem`, `subagentItem`, `AccessWriter.setItem(… Access …)`; Task 3 `PackageReview.described`; `reviewOf` (`builtins.ts:64`, exported).
- Produces:
  - `ItemView.own_access: Access | null`: the item's own access (what its select shows and edits, and what Reset resets). `access` is the effective one: `off` while the item's source, or the source it is a child of, is switched off, with `reason` `Off while "<source title>" is switched off` (unless a stronger reason is set). `overridden` stays about the item's own row.
  - `Capabilities` sets these for every provider in `tree`, `source` and each write's answer, children included; providers answer `own_access === access`.
  - A write per kind: `setItem` passes the `Access` through (the store checks it, Task 1); a remote tool refuses `on` with 400 in `Capabilities` already (`ITEM_ACCESS`).
  - `builtin:own-plugin` items: one per review skill (`skill:<name>`, kind `skill`) and subagent (`subagent:<name>`, kind `subagent`): `name` `<name>`, `harness_name` the review's `scadbuddy:<name>`, `label` `<name>`, `description` from `described[<item id>]`, `default: 'on'`, `access` the row's `on`/`off`.

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
- Produces (`packageProvider.ts`): `export function packageProvider(deps: { packages: PackageRepo; access: AccessReader & AccessWriter; listings: Pick<ToolListings, 'get' | 'getMany'>; verifiedReviewOf: (pin: PackagePin) => Promise<PackageReview>; refresh: (pkg: string, server: string) => ReturnType<typeof listPackageServer> }): CapabilityProvider`, owning `package:<name>` and `package:<name>/mcp:<server>`:
  - a package source: `kind: 'package'`, `title` the name, `enabled` the row's `enabled`, `switchable` its `approved`; items, in order: hooks (`hook:<event>:<id>`, kind `hook`, `name` the id, `label` `<event> hook (<type>)`, `description` its URL or null), skills (`skill:<name>`), subagents (`subagent:<name>`), each `default: 'on'` (commands are skill items, Task 3); then, for an `allow_refused` pin, its built-ins (`builtin:<Tool>`, kind `builtin`, `default: 'outward'`, `description` `"Claude Code's <Tool>; where several packages name it, the strictest access applies"`); `children`: one `package_server` source per declared server, http or (for an `allow_refused` pin) local.
  - a server source: `title` `<server>`, `enabled` `sourceEnabled` (on by default), `switchable: true`, `listed_at` its listing's, items `listedItems` over its rows and listing (harness names `mcp__plugin_<pkg>_<server>__<tool>`). A local server is never listed (listing starts the package's process): its items are its stored rows only, `listed_at: null`, and `refresh` answers 409 `"<id>" is started only by a turn, so its tools are not listed here`; its unset tools stay at `outward` (open point 9); a colliding server's items each `fixed: { access: 'off', reason: 'Its server is named <name>, as <others> are, so none of them is offered' }`.
  - `setEnabled` of a package → `PackageRepo.setEnabled` (its 409 for an unapproved pin passes through); of a server → `AccessStore.setEnabled`; items → `AccessStore.setItem`/`resetItem`; `details` of a server tool from its listing, of anything else the item with no schema; `refresh` of a server → `deps.refresh`, a package's → 409 `"<id>" lists no tools to refresh`.
  - `verifiedReviewOf` is used (and cached per content hash, in-process) only when a stored review has a hook without `id` or no `described`; `main.ts` passes one over `installer.acquire` + `vetPackage`. (Named apart from `builtins.ts` `reviewOf`, Task 8.) The pending re-pin's review, which `GET /plugin-packages` serves, gets the same fill: `PackageView.pending.review` passes through it in `routes/pluginPackages.ts` when its hooks lack ids, so Task 11 compares ids that exist.

- [ ] **Step 1: Write the failing tests**
  - `capabilities.packages.test.ts` (a memory `PackageRepo`, `memoryAccess`, a stub `verifiedReviewOf`): `lists a package with its hooks, skills, subagents and servers`; `lists an allowed package's built-ins, outward by default`; `switches a skill off and back on, deleting the row`; `a hook off is stored by its id`; `an unapproved package's switch answers 409`; `switching a package goes through PackageRepo.setEnabled`; `a server is on by default and switches off`; `a server tool takes a tier; a renamed one off or outward`; `colliding servers are fixed off with the reason`; `a local server switches off and shows its stored tools; refresh answers 409`; `a review stored without hook ids reads them from the verified copy`; `refresh lists a server and stores its tools`; `a failed refresh keeps the last listing`; `refresh of a package answers 409`.
  - `capabilities.routes.test.ts`: `lists packages after the remote plugins`; `routes package%3Ap%2Fmcp%3As to its server source` (the PR 4 test now answers 200 for a known server).
  - `capabilities.gates.pg.test.ts`: `a package server tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call`; `a package skill set off in Settings is absent from the next turn's copy` (classic `derivedPackage` through `turnRunner`'s deps); `a package server switched off in Settings is not forwarded`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd agent && pnpm vitest run test/capabilities.packages.test.ts test/capabilities.routes.test.ts`, `SCADBUDDY_TEST_DATABASE_URL=… pnpm vitest run test/capabilities.gates.pg.test.ts`
Expected: FAIL.

- [ ] **Step 3: Implement** as listed under Produces; `main.ts` builds `verifiedReviewOf` and passes `pluginForwarder` and the installer to the route deps.

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
- Produces: each `PackageCard` (aria-label `Plugin package <name>`, kept) shows its badges, source, review and trust controls as today, then `SourceCard` for `package:<name>`: its switch (the one enable control, unmarked; `switchable` only once approved), its hooks, skills, subagents and built-ins as rows, and its servers nested with their tools and a "List tools" button (`aiCapabilities.refresh`, unmarked: it lists what the approved review showed). A switch change updates the card's Enabled badge. In a pending re-pin's review, a hook whose `id` the package's capabilities source does not hold (its hook items, which Task 9 fills from the verified copy for an older review) is badged "New since you last reviewed"; a pending hook with no `id` is never badged. The built-in plugin is no longer listed here. Intro: "Plugin packages the assistant may load. Install and approve a pin here; then set each hook, skill, subagent, built-in and server tool."

- [ ] **Step 1: Rewrite the tests**: keep every install, approve, re-pin, file and refusal test; replace `lists the built-in plugins first…`, `keeps a built-in and a package stored under its name apart` (the shadowed package keeps its "Not loaded" notice), `shows ScadBuddy's own skills and subagents on its card…` and `reads a built-in plugin's files` with:
  - `lists only installed packages; ScadBuddy's own is in Built-in`
  - `enables an approved package with its source switch, not user-only`
  - `offers no switch before approval`
  - `turns a skill off after the confirmation`
  - `sets a server tool's access after listing its tools`
  - `marks a hook new since you last reviewed`
  - `marks no hook new when the current review predates hook ids`
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

- [ ] **Step 3: Docs.** `operating.md` §9: the derived copy (named by its own hash, re-hashed by both runtimes), what each item kind leaves out, the hook id rule, rows kept across a re-pin and dropped with what the pin lost, Skill, Agent and Task refused per call in both runtimes (the classic seam, the segment runner's hook; one `calls` vector set), hooks per turn in both and why, Bash/PowerShell and the other built-ins by access per call in both, local servers' switch and stored tools, colliding server names, `pruneStaleRows`. "Tool access": item ids (`hook:<event>:<id>`, `skill:`, `subagent:`, `builtin:`), `package:…/mcp:…` on by default, `builtin:browser`'s switch at every gate, the stricter built-in rule (an unset package counts as `outward`), commands as skill items, `own_access`. The API table: package sources and their refresh. `README.md`: a package's items are set in Settings → Tools & plugins → Packages. Every external fact links its source (#259).

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
| §4.4 a Skill or Agent call naming a disabled one refused (spec text updated to the ruling: per call in both runtimes) | 5 (classic seam), 6 (segment runner hook) |
| §5.5 package MCP listing (`ai_tool_listings`) and access (`toolTiers`/`disabledTools` from the resolver) | 2, 9 |
| §4.2 hook ids `hook:<event>:<sha256 first 16>`; survives an unchanged re-pin; changed or new resets and is flagged; stale overrides deleted with the re-pin | 3, 7, 11 |
| §4.2 `allow_refused` built-ins per tool name, the stricter wins | 1, 5, 6 |
| §4.4 stdio servers of allowed packages take the tier through `tierOf` (`turnPackages.ts:196`) | 2 (switch, stored tiers, names), 4 (stripped when off), 5 (classic per tool), 9 (source card); only their listing waits (open point 9) |
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
| #4757 brief: the `builtin:browser` and package-server `*` switches in every runtime (classic seam, `/mcp`, durable gate and activities, flows, the forwarder, the subagent loopback), defined and shown in the capabilities API | 1, 2, 8, 9 |
| Ruling: per-call Skill/Agent/Task refusal in durable, one `calls` vector set | 5, 6 |
| Ruling: `mayPark: () => true` for package server and remote plugin tools in durable | 2 |
| Ruling: local (stdio) servers' switch and stored access in both runtimes | 2, 4, 5, 9 |
| Ruling: `builtin_tier` at most 3 attempts, then park; an unset package counts as `outward` | 1, 5, 6 |
| Ruling: commands through the Skill tool are items | 3 |
| Pre-flight C1-C4, D1-D6, minors, M1-M3 | 1 (C1), 2 (D1, M1, M3), 4 (C4, M1), 5 (C2, C3, D2, D3, minor names), 6 (D4, R1, row 22), 7 (D5, R5), 9 (row 23, D6, M1), 11 (D6), 12 (spec text) |
| Spec §4.2 table's `hook:<event>#<n>` against its own paragraph | fixed in the spec in this commit (`hook:<event>:<id>`), and §2/§4.4's durable sentences, to the ruling |

**Open points, with the resolution this plan takes (proposed unless marked ruled):**

1. *Proposed:* a package's own switch stays `ai_plugin_packages.enabled` (approval-gated by the table's CHECK), served as the package source's `enabled`; no `package:<name>` `*` row is read or written. Only its servers' `*` live in `ai_access`, on by default.
2. *Ruled (controller), measured:* item ids are `skill:<name>`, `subagent:<name>` (`<name>` as the review has it after `<pkg>:`, nested agents `dir:name`; a frontmatter `name` that differs is an alias), `builtin:<Tool>`, and the spec's `hook:<event>:<id>`. Claude Code invokes slash commands through the Skill tool (bundled CLI strings, Task 3, re-measured on the pinned binary there), so a package's commands are `skill:` items too.
3. *Proposed:* rolling deploy. Every write of this PR lands in `ai_access` or `ai_plugin_packages`, so none is lost. An older replica does not yet enforce the new switches and items (it has no state to mirror into: the legacy keys hold no source switch, and folding the browser switch into `builtin_tools.scadbuddy` would come back through the mirror trigger as per-tool `off` rows that outlive switching it on). They apply on every replica once the rollout ends. `legacy.ts` therefore uses `itemOverrides`, never the folded `registryOverrides`. `builtin_tier` takes at most 3 attempts and parks on failure (*ruled*), so a durable turn on an older agent still asks. Rows an older replica's re-pin approval leaves are pruned by the next new-code write to that package (`pruneStaleRows`, Task 7), the simpler of that and a sweep (*ruled*: pick one).
4. *Ruled:* "the stricter wins" for a built-in orders `off` ≻ `outward` ≻ `write` ≻ `read`, and a package that names the tool with no row counts as `outward`. Both runtimes ask about the same packages: those of the turn that name the tool.
5. *Proposed:* "new since you last reviewed" is shown on the pending re-pin's review, compared with the hook ids the package's capabilities source serves (filled from the verified copy for an older review); a pending hook with no id is never marked. Nothing new is stored.
6. *Proposed:* a hook's id hashes `{ event, matcher, handler }`, so two byte-identical hooks under one event and matcher share an id and one access; they cannot be told apart by content.
7. *Proposed:* a review stored before this PR is read again from the pin's verified cached copy (`verifiedReviewOf`, cached per content hash) only when it lacks hook ids or descriptions; no backfill migration. `pruneStaleRows` deletes no hook row while a review lacks ids.
8. *Ruled (controller):* a Skill, Agent or Task call naming an `off` skill (or command) or subagent is refused per call in both runtimes: the classic permission seam and the segment runner's own `PreToolUse` `HookMatcher("Skill|Agent|Task")`, which reads `ai_access` per call, denies a call naming an `off` item and denies on a read error. One rule (`offCallItem` / `off_call_item`) is pinned by the new `calls` vectors. The per-turn copy also leaves the item out of the next turn in both. Bash and PowerShell are decided per call in durable through `builtin_tier` (behind `builtin-access-4757`), the other built-ins per call through the `builtin` turn server's `tierOf`, and in classic through the seam. Hooks alone change per turn, in both runtimes: no SDK option drops one plugin hook from a running query (spec §2), so that is technically forced.
9. *Ruled (controller):* an allowed package's local (stdio) servers get their `*` switch and their stored per-tool access in both runtimes (durable `localPackageServers`, classic: stripped from the copy when off, `disallowedTools` and the seam per tool). Only their listing is not offered on the page, since it would start the package's process outside a turn; their unlisted tools stay at the `outward` default until set. Listing a local server's tools from a turn is a follow-up issue, filed with the PR.
10. *Proposed:* a package server's tools are listed on "List tools" (the refresh route), not at approval or per turn; until then the server card says "No tools listed yet" and its stored rows still show, as a never-tested remote plugin's do.
11. *Proposed:* the own plugin's old switch route (`PATCH /plugin-packages/scadbuddy`) keeps answering for one release (#4828); the page no longer calls it.
12. *Proposed:* the PR body says `Fixes #4757`.
13. *Ruled (controller):* in durable, every remote plugin and package server tool is listed `may_park: true` (fixing PR 3's remote case too), with `fixedTier` for the transcript only, so a tool raised to `outward` mid-turn parks on its next call, as in a classic turn.
