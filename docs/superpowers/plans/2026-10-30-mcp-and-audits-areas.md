# MCP access and Audits areas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fill the last two Settings areas as spec §3.2 lists them (MCP access: Auth mode, Tokens, OIDC sign-in, Connect snippet; Audits: AI activity in tabs, with the `/activity` link), and retire every pointer to the old one-page names, so PR 7 of epic #4758 closes #781.

**Architecture:** The connect snippet leaves `McpTokensSection` and becomes the section `mcp-connect`. A token just minted reaches it through a small external store, `lib/mcpMintedToken.ts`, written only by the Tokens card, as `lib/mcpAuthChanges.ts` does for the auth mode. The Audits section holds a Radix `Tabs` over a list of logs with one entry, "AI activity". The log moves to `components/audits/`, and its tests move with it. The shell gets one more area notice: an agent-served area whose sections all hid themselves while the agent answers `not_configured`. Both `navigate` descriptions (the tab's catalogue and the agent's `browser_navigate`, which feeds classic turns, `/mcp` and the durable manifest) name every area route. The docs say the new names.

**Tech Stack:** React 19, react-router, Vite `import.meta.glob`, vitest + Testing Library + msw, Playwright on `pnpm preview`, shadcn/Radix `Tabs` (`src/components/ui/tabs.tsx`); agent: TypeScript, vitest.

**Spec:** `docs/superpowers/specs/2026-10-18-settings-and-tool-access-design.md` §3.1–§3.3, §3.5, §5 item 7, §6 "Frontend". Issue #781. Prior plan, whose rules still hold: `docs/superpowers/plans/2026-10-19-settings-areas.md`.

## Global Constraints

- Node 24; `cd frontend && corepack enable && pnpm install --frozen-lockfile` (the same in `agent/`).
- Run `pnpm gen:api` once in `frontend/` and in `agent/` before you run single files with `pnpm exec vitest run <file>`. It needs uv and the backend tree. `schema.d.ts` and `backend/openapi.json` are gitignored: never commit them.
- Before the last commit: `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm exec playwright test`, then `cd agent && pnpm lint && pnpm typecheck && pnpm test`.
- vitest under load can time out. Re-run a timed-out test on an idle machine before you debug it.
- msw mocks stay under `src/mocks/features/`. This plan adds none: `mcpTokens.ts`, `mcpOidc` and `audit.ts` already serve every route used here.
- A section is `frontend/src/pages/settings/sections/<id>.tsx` exporting `{ id, area, title, order, Component }` (plus `hideWhenEmpty` when its body can be empty). Never edit a list to add one (spec §3.3).
- No section title may equal a heading or a region name inside it (from the 2026-10-19 plan). This is why the Audits section stays "Audit log" around the "AI activity" card, and why Tokens stays "Tokens" around "MCP access tokens".
- The area nav and the section sub-nav stay plain links (the 2026-10-19 plan: "never Radix Tabs"). The Audits log tabs are inside one section, so they are real Radix tabs.
- No extra warnings or marks on settings controls (#4208). The one new notice is area-level and only says where to look.
- Every tool works in every mode: the `navigate` change goes in `agent/src/tools/browser.ts` (classic harness, `/mcp`, and `dist/tools.json` for durable turns) and in `frontend/src/agent/catalog.ts` (the tab and WebMCP).
- Every section on the MCP access and Audits areas keeps its `ai.available` gate (#251). The shell's one notice (spec §3.2) covers `unavailable`/`unreachable`, and Task 4 covers `not_configured`.
- Old links keep working (spec §3.1): `LEGACY_ANCHORS` keeps `assistant-mcp` → `mcp/mcp-auth-mode` and `assistant-audit` → `audits/audit-log`. No section id changes, so the table gets no new rows.
- Conventional-commit titles. Each commit ends with the session's trailers. The PR body says `Fixes #781` and cites sources (#259).
- Ordering against PR 5 (#5001, `feat/2171-package-items`, open): it touches none of this plan's frontend or agent files. Its only shared file is `docs/ai/operating.md`, in hunks at lines 854–1386 of main. Task 6 edits lines 451, 628, 1077 and 1293, which do not overlap those hunks. So there is no ordering constraint: if #5001 merges first, rebase onto main (fetch + reset per the stacked-PR rule, never a carry-down push), and Task 6's line numbers move but its text matches stay the same.

## Decisions (recorded here, from the spec and #781)

1. **Audits is the Settings area `audits`, not a `/audits` page.** #781 named a separate page as the leading option. Spec §3.2 (written later, and folding in #781) puts it in Settings. No `/audits` route was ever shipped, so there is nothing to redirect from.
2. **Tabs: one section, `audit-log`, with a tab per log.** The logs are `AUDIT_LOGS: readonly [AuditLog, ...AuditLog[]]` (a non-empty tuple; `AuditLog` is `{ value: string; label: string; Component: ComponentType }`) in `components/audits/logs.ts`, with one entry `{ value: 'ai-activity', label: 'AI activity', Component: AiActivityLog }`. A second log is one more entry. The selected tab is local state (default: the first entry), not in the URL. The `/activity` link sits above the tabs, outside any tab panel.
3. **Connect snippet: its own section `mcp-connect`, order 40, title "Connect snippet", `hideWhenEmpty`, gated on `ai.available`.** It reads `public_url` from `useLoadedSettings()`, and it reads the auth mode itself (`api.getMcpAuth()` keyed on `useMcpAuthVersion()`, as `McpTokensSection` does). A token just minted still fills it until Done (today's #1910 behaviour). The token comes through `lib/mcpMintedToken.ts`. That store is cleared on Done and when the Tokens card unmounts, so the plaintext lives no longer than it does today. The snippet's hint changes from "Create a token below" to "Create a token under Tokens".
4. **The AI activity log moves** from `components/assistant/AiAuditSection.tsx` to `components/audits/AiActivityLog.tsx` (export `AiActivityLog`), and its test moves with it (`git mv`). #781 says the tests move with it. Behaviour does not change.
5. **"Old routes retired"** means this: no code, test name or doc still points at a pre-area name. That covers "Settings → AI activity", "MCP access tokens" as a section name, "MCP authentication", "MCP sign-in (OIDC)" and the old file path. Old *addresses* are not removed: they stay redirects (spec §3.1). No `/settings#…` hash outside `registry.ts` names an old anchor on main (checked with grep at `0d040c6ce`).
6. **Empty agent-served area.** The spec's one notice is for `unavailable`/`unreachable`. With `not_configured`, every MCP access section hides itself, which leaves a blank area. So the shell shows one notice in that case: "Nothing here until the assistant service is set up." followed by a link "Status & credentials" to `/settings/assistant#assistant-credentials` that says what it needs. The notice shows only when the area is agent-served, the state is `not_configured`, and every section of the area is `hideWhenEmpty` and empty. The PR 2 test "shows no notice … while the assistant is not_configured" stays true for an area that still shows something.

## Review Focus

1. A token minted, then the user moves to another area and back: the Connect snippet still holds it until Done. Once Settings is left, nothing on the page shows it. Test: Task 1 `clears the minted token when the Tokens card unmounts`.
2. The auth mode saved in Auth mode while Connect snippet is on screen: the snippet drops or adds the `Authorization` header without a reload. Test: Task 1 `follows a saved auth mode`.
3. An old bookmark `/settings#assistant-audit` lands on `/settings/audits#audit-log` with the AI activity tab selected. Test: Task 6 Playwright `old anchors land on their areas`.
4. MCP access opened while the agent answers `not_configured`: the area is not blank. Test: Task 4.
5. The agent asked to "open the audit log" in a durable session: `browser_navigate`'s description names `/settings/audits`. Test: Task 5 (the agent test reads the same `browserTools` the manifest is built from).

## File map

| File | Change |
|---|---|
| `frontend/src/lib/mcpMintedToken.ts` | Create: `setMintedMcpToken(token: string \| null): void`, `useMintedMcpToken(): string \| null` (`useSyncExternalStore`, the shape of `mcpAuthChanges.ts`). |
| `frontend/src/components/McpTokensSection.tsx` | Drop `<McpConnectSnippet>` and the `publicUrl` prop. Publish `minted?.token` to the store; clear it on Done and on unmount. |
| `frontend/src/components/McpTokensSection.test.tsx` | Move the two snippet tests out (to `mcp-connect.test.tsx`). Add the store tests. |
| `frontend/src/components/McpConnectSnippet.tsx` (+ test) | Hint text "Create a token under Tokens, then paste it where the snippet says <your token>." |
| `frontend/src/pages/settings/sections/mcp-access-tokens.tsx` | `<McpTokensSection />` without `publicUrl`; drop `useLoadedSettings`. |
| `frontend/src/pages/settings/sections/mcp-connect.tsx` (+ `.test.tsx`) | Create: the Connect snippet section. |
| `frontend/src/components/audits/AiActivityLog.tsx` (+ `.test.tsx`) | Moved from `components/assistant/AiAuditSection.tsx` (+ test). |
| `frontend/src/pages/settings/sections/audit-log.tsx` (+ test) | the tabs (hidden while the assistant is unavailable). |
| `frontend/src/components/audits/logs.ts` | `AUDIT_LOGS`. |
| `frontend/src/pages/settings/SettingsShell.tsx` (+ test) | The not-set-up notice. |
| `frontend/src/pages/settings/registry.test.ts` | The §3.2 table gains `mcp-connect`. A new test ties `AREAS` to the `navigate` description. |
| `frontend/src/agent/catalog.ts`, `agent/src/tools/browser.ts`, `agent/test/browserTools.test.ts` | `navigate` names every area route. |
| `frontend/e2e/ai-audit.spec.ts`, `mcp-tokens.spec.ts`, `settings.spec.ts` | The tab, the snippet section, the old anchors. |
| `docs/ai/operating.md`, `security.md`, `claude-plugin.md`, `agent-sessions.md`, `docs/user-guide.md` | The new names and the moved path. |

---

### Task 1: Connect snippet as its own section

**Files:**
- Create: `frontend/src/lib/mcpMintedToken.ts`, `frontend/src/pages/settings/sections/mcp-connect.tsx`, `frontend/src/pages/settings/sections/mcp-connect.test.tsx`
- Modify: `frontend/src/components/McpTokensSection.tsx:134-139,186-191,296`, `frontend/src/components/McpTokensSection.test.tsx:72-96`, `frontend/src/components/McpConnectSnippet.tsx:76`, `frontend/src/components/McpConnectSnippet.test.tsx:26,33`, `frontend/src/pages/settings/sections/mcp-access-tokens.tsx`, `frontend/src/pages/settings/registry.test.ts` (the §3.2 table test)

**Interfaces:**
- Produces: `setMintedMcpToken(token: string | null): void`; `useMintedMcpToken(): string | null`; section `mcp-connect` (`area: 'mcp'`, `title: 'Connect snippet'`, `order: 40`, `hideWhenEmpty: true`); `McpTokensSection()` takes no props.

- [ ] **Step 1: Write the failing tests.**
  - `mcp-connect.test.tsx` (mock `useAiAvailability` as `mcp-access-tokens.test.tsx` does, `available: true`; render with `renderSettingsSection(<><McpAccessTokens /><McpConnect /></>, { route: '/settings/mcp' })`, `public_url` from the settings mock, or `server.use` a `GET /settings` with `public_url: 'https://scadbuddy.example'`):
    - `it('fills the snippet with a token just minted, until Done (#1910)')`: the moved body of `McpTokensSection.test.tsx:72-86`. `getByTestId('mcp-snippet-claude-code')` holds `"'https://scadbuddy.example/mcp' --header 'Authorization: Bearer <your token>'"`. After Create, it holds `` `Authorization: Bearer ${token}` ``. After Done, it holds `<your token>`, and `document.body.textContent` does not contain the token.
    - `it('leaves the header out while MCP authentication is off (#1910)')`: `setMcpAuthMode('disabled')`. The snippet is exactly `"claude mcp add --transport http scadbuddy 'https://scadbuddy.example/mcp'"`.
    - `it('follows a saved auth mode')`: start `bearer`, then `setMcpAuthMode('disabled')` and `act(() => mcpAuthChanged())`. The snippet loses `Authorization`.
    - `it('clears the minted token when the Tokens card unmounts')`: mint, `unmount()` the Tokens card only (render it under a toggle), then expect the snippet to hold `<your token>`.
    - `it('hides itself, and asks the agent nothing, while the assistant is unavailable')`: `available: false`. The `mcp-connect-body` element has no children, and `vi.spyOn(api, 'getMcpAuth')` is not called.
  - `McpConnectSnippet.test.tsx:26,33`: the regex becomes `/Create a token under Tokens/`.
  - `registry.test.ts` `holds the spec §3.2 table…`: the `mcp` row becomes `['mcp-auth-mode', 'mcp-access-tokens', 'mcp-oidc', 'mcp-connect']`.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/settings/sections/mcp-connect.test.tsx src/components/McpConnectSnippet.test.tsx src/pages/settings/registry.test.ts`. Expected: FAIL (module `./mcp-connect` not found; the old hint text).
- [ ] **Step 3: Implement.** Write `mcpMintedToken.ts` in the shape of `mcpAuthChanges.ts`. In `McpTokensSection`: add an effect `setMintedMcpToken(minted?.token ?? null)` on `[minted]`, with a cleanup on unmount that calls `setMintedMcpToken(null)`. Remove the snippet and the `publicUrl` prop. In `mcp-connect.tsx`: `<Section id title>{ai.available && <McpConnectSnippet publicUrl={settings.public_url} authMode={auth.data?.mode ?? null} token={useMintedMcpToken() ?? undefined} />}</Section>`, with the auth read inside a child component so nothing is asked while the gate is closed. Then delete `McpTokensSection.test.tsx:72-96` (moved).
- [ ] **Step 4: Run** the Step 2 command plus `src/components/McpTokensSection.test.tsx src/pages/settings/sections/mcp-access-tokens.test.tsx`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): the Connect snippet is its own MCP access section (#781)`.

### Task 2: Move the AI activity log to `components/audits/`

**Files:**
- Move: `frontend/src/components/assistant/AiAuditSection.tsx` → `frontend/src/components/audits/AiActivityLog.tsx`; `AiAuditSection.test.tsx` → `components/audits/AiActivityLog.test.tsx`
- Modify: `frontend/src/pages/settings/sections/audit-log.tsx` (import)

**Interfaces:**
- Produces: `AiActivityLog(): JSX.Element | null` (was `AiAuditSection`; it still renders nothing while `!ai.available`); `AUDIT_PAGE` unchanged.

- [ ] **Step 1: `git mv` both files.** Rename the export and the test's import and `describe('AiActivityLog', …)`. Fix the one relative import that changes: `./Tip` → `../assistant/Tip`. The doc comment's "Settings → \"AI activity\"" becomes "Settings → Audits → AI activity".
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/components/audits/AiActivityLog.test.tsx src/pages/settings/sections/audit-log.test.tsx && pnpm typecheck`. Expected: PASS, with every test name as it was.
- [ ] **Step 3: Commit** `refactor(frontend): the AI activity log lives under components/audits (#781)`.

### Task 3: Audits area tabs

**Files:**
- Modify: `frontend/src/pages/settings/sections/audit-log.tsx`, `frontend/src/pages/settings/sections/audit-log.test.tsx`

**Interfaces:**
- Consumes: `AiActivityLog` (Task 2).
- Produces: `export const AUDIT_LOGS: readonly [AuditLog, ...AuditLog[]]` in `components/audits/logs.ts` (new file).

- [ ] **Step 1: Write the failing tests** in `audit-log.test.tsx`:
  - `it('shows the logs as tabs, even with one (#781)')`: `getByRole('tablist', { name: 'Audit logs' })` holds tabs whose names are exactly `['AI activity']`. That tab has `aria-selected="true"`. `getByRole('tabpanel', { name: 'AI activity' })` contains `findByRole('list', { name: 'AI activity entries' })`.
  - `it('builds one tab per entry of AUDIT_LOGS')`: `AUDIT_LOGS.map((l) => l.value)` equals `['ai-activity']`.
  - The existing `links to the Activity page…` test also asserts that the link is not inside the tabpanel (`within(tabpanel).queryByRole('link', { name: 'Activity page' })` is null).
  - `shows the Activity link while the assistant is unavailable` stays as it is.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/settings/sections/audit-log.test.tsx`. Expected: FAIL (no tablist).
- [ ] **Step 3: Implement.** Put `<Tabs defaultValue={AUDIT_LOGS[0].value}>` after the Activity paragraph, with a `<TabsList aria-label="Audit logs">` holding one `TabsTrigger` per entry and one `TabsContent` per entry rendering `<entry.Component />`. The section's doc comment cites #781 for "tabs even with one".
- [ ] **Step 4: Run** the Step 2 command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(frontend): the Audits area shows its logs as tabs (#781)`.

### Task 4: An agent-served area with nothing to show says why

**Files:**
- Modify: `frontend/src/pages/settings/SettingsShell.tsx:278-291`, `frontend/src/pages/settings/SettingsShell.test.tsx`

- [ ] **Step 1: Write the failing test** `it('says where to look when every section of an agent-served area hid itself while the assistant is not_configured')`. Add `{ id: 'ghost-mcp', area: 'mcp', title: 'Ghost MCP', order: 10, Component: GhostMcp, hideWhenEmpty: true }` (a `Section` with `{null}`) to a local list. Set `availability` to `{ available: false, state: 'not_configured' }`. Render at `/settings/mcp`. `findByRole('status')` has the text `Nothing here until the assistant service is set up. Status & credentials says what it needs.`, and its link `Status & credentials` has `href` `/settings/assistant#assistant-credentials`. Then render the same at `/settings/tools` with `fakes` (Packages is shown): `queryByText(/Nothing here until/)` is null.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/settings/SettingsShell.test.tsx`. Expected: FAIL on the new test only.
- [ ] **Step 3: Implement.** For each area: `area.servedByAgent && ai.state === 'not_configured' && areaSections.length > 0 && areaSections.every((s) => s.hideWhenEmpty && collapse?.empty.has(s.id))`. When that holds, render the notice as `<p role="status">` beside the existing one, with a `Link`.
- [ ] **Step 4: Run** the Step 2 command. Expected: PASS, with the four availability tests from PR 2 unchanged.
- [ ] **Step 5: Commit** `feat(frontend): an empty agent-served Settings area says where to look (#781)`.

### Task 5: `navigate` knows every Settings area, in every mode

**Files:**
- Modify: `frontend/src/agent/catalog.ts:42`, `agent/src/tools/browser.ts:291`, `frontend/src/pages/settings/registry.test.ts`, `agent/test/browserTools.test.ts`

- [ ] **Step 1: Write the failing tests.**
  - `registry.test.ts` `it('names every area in the navigate tool\'s description')`: import `TOOLS` from `../../agent/catalog`. For each `AREAS` id, `TOOLS.navigate.description` contains `` `"/settings/${id}"` ``.
  - `browserTools.test.ts`, in `describe('the browser_* tools match the tab catalogue…')`: `it('name every Settings area in navigate, as the tab does')`. With `const AREAS = ['general', 'printing', 'library', 'assistant', 'tools', 'mcp', 'audits', 'system']`, both `tool('browser_navigate').description` (the file's `ALL_TOOLS` helper, `:64`) and `(await frontendBridgeCatalog()).TOOLS.navigate.description` contain each `` `"/settings/${a}"` ``.
- [ ] **Step 2: Run** `cd frontend && pnpm exec vitest run src/pages/settings/registry.test.ts` and `cd agent && pnpm exec vitest run test/browserTools.test.ts`. Expected: FAIL on the new tests.
- [ ] **Step 3: Implement.** In both descriptions, after the examples sentence, add exactly: `'Settings areas: "/settings/general", "/settings/printing", "/settings/library", "/settings/assistant", "/settings/tools" (tools & plugins), "/settings/mcp" (MCP access), "/settings/audits" (the AI activity log) and "/settings/system"; "#<section>" opens one section. '` Keep the agent's lower-case "go" after `PREFIX`. Its sentence order matches the tab's.
- [ ] **Step 4: Run** the Step 2 commands, then `cd agent && pnpm build` (it writes `dist/tools.json`, which the durable worker declares from; not committed). Expected: PASS.
- [ ] **Step 5: Commit** `feat(agent): navigate names every Settings area, in the tab and the agent (#781)`.

### Task 6: Retire the old names; Playwright on the area URLs

**Files:**
- Modify: `frontend/e2e/ai-audit.spec.ts`, `frontend/e2e/mcp-tokens.spec.ts`, `frontend/e2e/settings.spec.ts`
- Modify (docs): `docs/ai/operating.md:451,628,1077,1293`, `docs/ai/security.md:50-52,1071-1072`, `docs/ai/claude-plugin.md:13,55`, `docs/ai/agent-sessions.md:161`, `docs/user-guide.md:476`

- [ ] **Step 1: Write the failing e2e tests.**
  - `ai-audit.spec.ts`: after `goto('/settings/audits')`, expect `page.getByRole('tab', { name: 'AI activity' })` to have `aria-selected` `true`. The rest is unchanged. Rename the describe to `'AI activity in Settings → Audits (#258, #781)'`.
  - `mcp-tokens.spec.ts`: `test('the Connect snippet section fills in a token just minted, until Done (#1910, #781)')`. On `/settings/mcp`, `page.locator('#mcp-connect').getByTestId('mcp-snippet-claude-code')` contains `<your token>`. After `createToken(page, 'Snippet')`, it contains the token. After Done, it contains `<your token>` again.
  - `settings.spec.ts`: `test('old anchors land on their areas (#781)')` with the pairs `['/settings#assistant-audit', '/settings/audits#audit-log']`, `['/settings#assistant-mcp', '/settings/mcp#mcp-auth-mode']`. For each, `goto`, then `expect(page).toHaveURL(…)`. For the first, the `AI activity` tab is selected.
- [ ] **Step 2: Run** `cd frontend && pnpm build && pnpm exec playwright test e2e/ai-audit.spec.ts e2e/mcp-tokens.spec.ts e2e/settings.spec.ts e2e/mcp-auth.spec.ts`. Expected: PASS once Tasks 1–3 are in. If a test fails, fix the spec's locator, not the app, unless the app is wrong.
- [ ] **Step 3: Edit the docs.** Each edit is a text pointer only (spec §3.5 "Text pointers say the new names"):
  - "Settings → **AI activity**" → "Settings → Audits → **AI activity**" (`operating.md:1293`, `security.md:1071`). `security.md:1072`'s link → `frontend/src/components/audits/AiActivityLog.tsx`.
  - "Settings → MCP access → **MCP access tokens**" / "\"MCP access tokens\"" → "Settings → MCP access → **Tokens**" (`operating.md:451`, `security.md:50`, `claude-plugin.md:13,55`, `agent-sessions.md:161`). Add to `claude-plugin.md:13` that **Connect snippet** in the same area gives the Claude Code and Claude Desktop setup.
  - "\"MCP sign-in (OIDC)\"" → "**OIDC sign-in**" (`operating.md:628`). "**MCP authentication**" → "**Auth mode**" (`operating.md:1077`).
  - `user-guide.md:476`: "**AI activity** is the log of what the assistant did, under **Settings → Audits**."
  - Then check that nothing old is left: `grep -rn 'Settings → \*\*AI activity\|Settings → "AI activity\|AiAuditSection\|"MCP sign-in (OIDC)"' docs README.md frontend/src agent/src plugins | grep -v docs/superpowers` prints nothing.
- [ ] **Step 4: Run** the whole check from Global Constraints (frontend and agent). Expected: PASS.
- [ ] **Step 5: Commit** `docs: Settings → MCP access and Audits by their area names; e2e on the area URLs (#781)`.
