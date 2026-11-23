# Per-session browser options for the Playwright MCP (#7027) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A session chooses its browser's user agent, viewport and locale at creation; the agent's
plugin forwarder sends them to our own wrapper around `@playwright/mcp`, which builds that MCP
session's browser context from them.

**Architecture:** The choice is a `browser` JSON column on `ai_sessions`, validated and stored by
the API's CRUD route (the agent has no SQL, #5299) and inherited by a fork. The agent reads it with
the session row and its forwarder adds `X-ScadBuddy-Browser` to every request of that session's
plugin routes. A new image, `scadbuddy-playwright-mcp`, calls `@playwright/mcp`'s
`createConnection(config, contextGetter)` once per MCP session, with a context built from the
header merged over the server's options.

**Tech Stack:** FastAPI + pydantic + psycopg (backend), TypeScript + zod + vitest (agent), React +
vitest (frontend), Node ESM + `node:test` (wrapper), Docker/buildx and GitHub Actions.

**Spec:** issue #7027 (`gh issue view 7027`) and the rulings below.

## Rulings (decided by the owner; do not re-ask)

- **R-7027-1:** A session takes `browser: {user_agent?, viewport?: {width, height}, locale?}` at
  creation (`sessions_start`, the chat panel's new-session settings, a flow's session). It is stored
  on the session row through the API's CRUD routes (the agent has no SQL; #5299), fixed for the
  session's life, and a fork inherits it. Absent = the default desktop Chrome UA.
- **R-7027-2:** Validation in one place on the API: `user_agent` printable ASCII, 1..512 bytes;
  viewport width 320..3840, height 320..2160; `locale` a BCP 47 tag up to 35 chars. Rejected values
  are a 422, never silently clamped.
- **R-7027-3:** The model never sets the header. The agent's forwarder adds `X-ScadBuddy-Browser`
  (JSON of the session's stored options) on the session's MCP requests and strips any client-sent
  copy.
- **R-7027-4:** Our own wrapper image around `@playwright/mcp`'s exported
  `createConnection(config, contextGetter)`, pinned to the same 0.0.85: per MCP session it builds the
  browser context from the header's options merged over the server's config. It keeps `--isolated`,
  the host allowlist, and the Deployment's egress policy and security profile unchanged. It
  re-validates the header (same bounds) and ignores an invalid one with a logged warning, falling
  back to the server default.
- **R-7027-5:** The image is built and published by this repo's CI like the other images (amd64
  only, buildx mirror rules in CLAUDE.md). The clusters change that switches the Deployment to it is
  a SEPARATE PR in eh-homelab/clusters, opened after this one merges and its image exists.

## Measured facts (2026-11-23, on this machine)

- `@playwright/mcp@0.0.85` (`npm pack`): `index.d.ts` declares
  `createConnection(config?: Config, contextGetter?: () => Promise<BrowserContext>): Promise<Server>`;
  `index.js` is `require('playwright-core/lib/coreBundle').tools.createConnection`. Its dependency is
  `playwright-core@1.66.0-alpha-2026-11-09`.
- In that `playwright-core` (`lib/coreBundle.js`, `packages/playwright-core/src/tools/mcp/index.ts`),
  `createConnection` resolves the config, and on the session's first tool call runs
  `create(clientInfo)`: with a `contextGetter`, `context = config.browser.isolated ?
  browser.newContext(...) : browser.contexts()[0]`, where `browser` wraps the getter's context and
  its `newContext` throws. So the wrapper passes `browser.isolated: false` and a getter that returns
  a NEW context from one shared Chromium: each MCP session still gets its own in-memory context
  (what `--isolated` gives the CLI: `decorateMCPCommand` shares one browser and calls
  `browser.newContext(config.browser.contextOptions)` per client). `BrowserBackend.dispose` does not
  close the context, so the wrapper closes it when the session's transport closes.
- `createConnection` returns an MCP `Server` with no transport. The CLI's HTTP layer
  (`tools/utils/mcp/http.ts`) uses `StreamableHTTPServerTransport` from
  `playwright-core/lib/utilsBundle` (an `exports` entry of playwright-core), one per
  `mcp-session-id`, and answers 403 to a `Host` not in `--allowed-hosts`.
- The image `mcr.microsoft.com/playwright/mcp:v0.0.85@sha256:cc70a692…` (amd64 and arm64): entrypoint
  `tini -s -- node /app/cli.js --headless --browser chromium --no-sandbox`, user `node` (1000),
  Node 26.13.1, `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` holding `chromium-1254` (the
  `browsers.json` revision of that playwright-core), npm at `/usr/local/bin/npm`. `/app` has
  `cli.js` and `node_modules/{playwright,playwright-core}` but NO `index.js`, so the wrapper installs
  `@playwright/mcp@0.0.85` itself. `--browser chromium` maps to `channel: 'chrome-for-testing'`
  (`resolveBrowserParam`), `--no-sandbox` to `chromiumSandbox: false`.

## Global Constraints

- Header name `X-ScadBuddy-Browser`; value the JSON of the stored object, keys `user_agent`,
  `viewport` (`{width, height}`), `locale`; only keys that are set.
- Bounds (R-7027-2), the same in the API and the wrapper: `user_agent` matches `^[\x20-\x7E]{1,512}$`;
  `viewport.width` integer 320..3840, `viewport.height` integer 320..2160 (both required when
  `viewport` is given); `locale` matches `^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$` and is at most 35
  characters. No other key. An empty object `{}` is stored as absent (NULL).
- Default user agent when a session has none: the server's `--user-agent`, which the Deployment
  sets to `backend/scadbuddy/profile_fetch/site.py` `USER_AGENT`. Never `HeadlessChrome`.
- The agent never validates bounds itself; it forwards the shape and maps the API's 422 on
  `body.browser…` fields to `SessionError('invalid_browser', …)` with status 422.
- No new SQL client, table grant or cross-service read (root CLAUDE.md "Database access").
- Migrations: a NEW file `backend/scadbuddy/migrations_agent/$(date -u +%Y%m%dT%H%MZ)_session_browser.sql`
  plus its entry in `agent/test/support/expected-migrations.json`; edit no merged file.
- Images: amd64 only on publish; every `docker/setup-buildx-action` step carries the mirror
  `driver-opts` and `buildkitd-config-inline` (copy the `sandbox` job's).
- Wrapper pinned to `@playwright/mcp` `0.0.85` exactly, on the base image digest above; the build
  fails when the base image's `/app/package.json` version differs from the installed package's.
- Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. No stash, amend,
  rebase, force push. Docs cite sources for external facts.

## Review Focus

1. A fork, or a Workflow agent's chat (`startChild`), of a session with options must use the
   parent's options (Task 1 test `test_fork_inherits_browser`; Task 2 manager test).
2. A client (Claude Code, or a plugin echoing headers) sending its own `X-ScadBuddy-Browser`
   must never reach the upstream; a session without options sends no header at all (Task 3 tests).
3. A durable session's kept MCP client (`keep().register()`) must carry the header too, or the
   Playwright session it opens keeps the default UA (Task 3 test on `keep`).
4. A malformed header (not JSON, a bad bound, an unknown key, a 2 KiB value) at the wrapper must
   give the server default and a warning, never a 4xx or a crash (Task 4 tests).
5. A user agent with a non-ASCII or control character (`é`, `\n`) is a 422 at the API, never
   stored and never forwarded (Task 1 parametrized test).

---

### Task 1: The API stores and validates a session's browser options

**Files:**
- Create: `backend/scadbuddy/migrations_agent/<UTC stamp>_session_browser.sql`
- Modify: `agent/test/support/expected-migrations.json` (the new entry, via
  `UPDATE_EXPECTED_MIGRATIONS=1 pnpm exec vitest run test/schema.test.ts` in `agent/`)
- Modify: `backend/scadbuddy/agent_data/sessions.py` (`COLUMNS`, `insert`)
- Modify: `backend/scadbuddy/api/crud_agent_sessions.py` (`SessionBrowser`, `NewSession`, `SessionRow`)
- Test: `backend/tests/crud/test_crud_agent_sessions.py`

**Interfaces:**
- Produces: column `ai_sessions.browser jsonb NULL` with `CHECK (browser IS NULL OR
  jsonb_typeof(browser) = 'object')`; `POST …/crud/agent/sessions` body field `browser` (object or
  absent; absent = the parent's for a row with `parent_id`, else NULL); every row answer (`GET
  …/sessions/{id}`, the list) carries `browser: {...} | null`. A refused value is 422 with
  `errors[].loc` starting `body.browser`.

- [ ] **Step 1: Write the failing tests** in `test_crud_agent_sessions.py`, using the module's
  existing insert helpers: `test_browser_is_stored_and_read` (insert with
  `{"user_agent": "Mozilla/5.0 (iPhone)", "viewport": {"width": 390, "height": 844}, "locale": "en-GB"}`,
  `GET` returns it equal); `test_no_browser_reads_null`; `test_empty_browser_reads_null`
  (`{}` → `null`); `test_fork_inherits_browser` (child with `parent_id` and no `browser` reads the
  parent's; a child that gives its own keeps its own); a parametrized
  `test_browser_out_of_bounds_is_422` over `user_agent` `""`, `"x"*513`, `"é"`, `"a\nb"`; viewport
  width 319 and 3841, height 319 and 2161, width `390.5`, viewport missing `height`; `locale`
  `"en_GB"`, `"e"`, `"a"*36`, `"en--GB"`; an unknown key `{"proxy": "x"}`; each asserts 422, a
  `loc` starting `["body","browser"`, and no row written. Bound edges pass: UA of 512 `"a"`, width
  320/3840, height 320/2160, locale `"zh-Hant-TW"`.

- [ ] **Step 2: Run them to see them fail**

Run (Postgres per CLAUDE.local.md, own container name and port, then
`SCADBUDDY_TEST_DATABASE_URL=… uv run --frozen pytest -n 2 tests/crud/test_crud_agent_sessions.py -k browser`
in `backend/`): FAIL (unknown field / no column).

- [ ] **Step 3: Implement.** Migration: `ALTER TABLE ai_sessions ADD COLUMN browser jsonb` with the
  CHECK above and a comment citing #7027. `sessions.py`: add `browser` to `COLUMNS`; `insert(...,
  browser: dict[str, Any] | None)` writes `CASE WHEN %(browser)s IS NOT NULL THEN %(browser)s WHEN
  %(parent)s IS NOT NULL THEN (SELECT browser FROM ai_sessions WHERE id = %(parent)s) END`
  (mirror how `background_tools` is inherited). `crud_agent_sessions.py`: `class Viewport(_Body)`
  (`width: int = Field(ge=320, le=3840, strict=True)`, `height: int = Field(ge=320, le=2160,
  strict=True)`), `class SessionBrowser(_Body)` (`user_agent: str | None = Field(default=None,
  pattern=r"^[\x20-\x7E]{1,512}$")`, `viewport: Viewport | None = None`, `locale: str | None =
  Field(default=None, max_length=35, pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")`); the bound
  constants named at module top with a comment that the wrapper (`workers/playwright-mcp/options.mjs`)
  repeats them; `NewSession.browser: SessionBrowser | None = None` passed as
  `body.browser.model_dump(exclude_none=True) or None`; `SessionRow.browser: dict[str, Any] | None`.
  Update the module docstring's ``POST …/sessions`` line.

- [ ] **Step 4: Run the tests** (same command, then the whole file): PASS. Then
  `uv run --frozen ruff check . && uv run --frozen ruff format --check . && uv run --frozen mypy`.

- [ ] **Step 5: Commit** `feat(api): store a session's browser options (#7027)`.

### Task 2: The agent takes, stores and shows a session's browser options

**Files:**
- Modify: `agent/src/sessions/rows.ts` (`SessionBrowser` type, `SessionRow.browser`,
  `NewSessionRow.browser`, `CrudSessionRows.insert` and the wire mapping)
- Modify: `agent/test/support/sql/sessionRows.ts` (the SQL fake: column and inheritance)
- Modify: `agent/src/sessions/manager.ts` (`SessionErrorCode` `invalid_browser` → 422,
  `StartOptions.browser`, `insert`, `start`, `SessionRecord.browser` via `record(row)`)
- Modify: `agent/src/tools/sessions.ts` (`sessions_start` input `browser`)
- Modify: `agent/src/routes/sessions.ts` (`StartBody.browser`, `sessionView.browser`, `SessionView`)
- Modify: `agent/src/sessions/clientProtocol.ts` and `agent/src/routes/chat.ts` (`user.message`
  `browser`, used only when it starts a session)
- Test: `agent/test/crudSessions.test.ts`, the manager's start tests, `agent/test/` route/tool tests
  for sessions_start and the chat socket (extend the existing files that test them).

**Interfaces:**
- Consumes: Task 1's route field and 422.
- Produces: `export type SessionBrowser = { user_agent?: string; viewport?: { width: number; height:
  number }; locale?: string }` in `agent/src/sessions/rows.ts`; `SessionRecord.browser:
  SessionBrowser | null`; `TurnRow`/`SessionRow` `browser: SessionBrowser | null`;
  `SessionManager.start(principal, { …, browser?: SessionBrowser })`; zod `SessionBrowserInput`
  (shape only: strings, integers, `z.strictObject`) exported from `rows.ts` or a small
  `agent/src/sessions/browserOptions.ts`, used by the tool, the route and the chat schema;
  `SessionView.browser: SessionBrowser | null`.

- [ ] **Step 1: Failing tests:** `CrudSessionRows.insert` sends `browser` only when given and reads
  it back; a 422 whose `fields` include `body.browser.user_agent` makes `manager.start` throw
  `SessionError` code `invalid_browser`, status 422, message naming the field and the API's detail;
  `sessions_start` with `browser` passes it to `start` and its view shows it; `POST
  /api/v1/ai/sessions` with `browser` answers 201 with `browser` in the view, and 422 for a value
  the API refused; a chat `user.message` without `sessionId` and with `browser` starts the session
  with it, and with a `sessionId` the field is ignored (no error); a fork's view shows the parent's
  options (SQL fake inherits as the API does).
- [ ] **Step 2: Run** `pnpm exec vitest run <those files>` in `agent/` (Postgres for the SQL fake per
  CLAUDE.local.md; `pnpm gen:api` first): FAIL.
- [ ] **Step 3: Implement** to the interfaces above. `fork` and `startChild` pass no `browser`
  (the API inherits through `parent_id`). The tool's description says what each field does and that
  the session keeps them for its life and its forks; no bounds in the agent (the API's 422 carries
  them).
- [ ] **Step 4: Run** the tests, then `pnpm lint && pnpm typecheck`: PASS.
- [ ] **Step 5: Commit** `feat(agent): a session's browser options at its start (#7027)`.

### Task 3: The forwarder sends `X-ScadBuddy-Browser` on the session's plugin requests

**Files:**
- Modify: `agent/src/plugins/forwarder.ts` (`ForwardOptions.browser`, `Route.browserHeader`,
  `send`, `forwardForRun`, `keep`, `pluginFingerprint` input)
- Modify: `agent/src/sessions/manager.ts` (`remotePlugins?: (browser?: SessionBrowser) => …`),
  `agent/src/sessions/turnRunner.ts` (`remote(session.browser ?? undefined)`)
- Modify: `agent/src/temporal/turnServers.ts` (`remotePlugins(browser)`, new dep
  `sessionBrowser?: (sessionId: string) => Promise<SessionBrowser | undefined>`, none for a flow)
- Modify: `agent/src/main.ts` (both wirings; `sessionBrowser` from the session rows' `get`)
- Modify: `agent/CLAUDE.md` (one short paragraph: the header, who sets it, the wrapper image)
- Test: `agent/test/forwarder*.test.ts` (the existing forwarder tests), `turnServers` tests

**Interfaces:**
- Consumes: `SessionBrowser`, `SessionRecord.browser` (Task 2).
- Produces: `export const BROWSER_HEADER = 'x-scadbuddy-browser'` in `forwarder.ts`;
  `forwardForRun(loaded, forwarder, { record?, browser? })`; `ForwardOptions.browser?: SessionBrowser`.

- [ ] **Step 1: Failing tests:** a route registered with `browser` sends
  `x-scadbuddy-browser: {"user_agent":…}` (exact `JSON.stringify` of the object) on POST, GET and
  DELETE through the route; a route without `browser` sends none; a client request carrying its own
  `X-ScadBuddy-Browser` reaches the upstream with the session's value (with `browser`) or none
  (without), never the client's; `forwardForRun(..., { browser })`'s `keep(name).register()` route
  sends it too, and its `fingerprint` differs from one with other options; `turnServers` with a
  `sessionBrowser` dep passes that session's options to `remotePlugins`, and a flow context passes
  undefined.
- [ ] **Step 2: Run** them: FAIL.
- [ ] **Step 3: Implement.** The header goes to every remote plugin route of the run: it carries
  only the session's chosen options, nothing secret, and a plugin that is no browser ignores it
  (say so in the forwarder's header comment, beside the header list). The model has no way to set
  it: it is a route option, and `REQUEST_HEADERS` already drops every other client header.
- [ ] **Step 4: Run** the tests, `pnpm lint`, `pnpm typecheck`: PASS.
- [ ] **Step 5: Commit** `feat(agent): forward a session's browser options to its plugins (#7027)`.

### Task 4: The wrapper image `scadbuddy-playwright-mcp`

**Files:**
- Create: `workers/playwright-mcp/package.json` (private, `"type": "module"`, dependency
  `"@playwright/mcp": "0.0.85"` exactly), `workers/playwright-mcp/package-lock.json` (`npm install
  --package-lock-only --ignore-scripts`)
- Create: `workers/playwright-mcp/options.mjs` (pure: no Playwright import)
- Create: `workers/playwright-mcp/server.mjs`
- Create: `workers/playwright-mcp/options.test.mjs` (`node --test`)
- Modify: `Dockerfile` (stage `playwright-mcp`), `.dockerignore` if it excludes the folder
- Modify: `.github/workflows/ci.yml` (`agent` job: build the target, run its unit test in it, smoke
  it; tear-down), `.github/workflows/build-image.yml` (job `playwright-mcp`, copy of `sandbox`,
  image `ghcr.io/eh-homelab/scadbuddy-playwright-mcp`, cache scope `playwright-mcp` `mode=min`,
  `continue-on-error: true`; header comment lists it)

**Interfaces:**
- Produces (`options.mjs`): `export const HEADER = 'x-scadbuddy-browser'`;
  `export function parseBrowserHeader(value: string | string[] | undefined): { options: object | undefined, warning?: string }`
  (undefined value → `{options: undefined}`; invalid → `{options: undefined, warning}`; valid →
  the parsed object); `export function contextOptionsFor(base: object, options: object | undefined): object`
  (returns `{...base, userAgent, viewport, locale}` with only the keys `options` sets mapped:
  `user_agent`→`userAgent`, `viewport`→`viewport`, `locale`→`locale`);
  `export function parseArgs(argv: string[]): ServerOptions` (flags: `--port`, `--host`,
  `--allowed-hosts` (comma list), `--isolated` (accepted; every session is isolated),
  `--output-dir`, `--user-agent`, `--viewport-size WxH`, `--idle-timeout ms`; any other flag throws).
- Produces (image): entrypoint `tini -s -- node /opt/scadbuddy-playwright-mcp/server.mjs`, user
  1000, so the Deployment's current args work unchanged.

- [ ] **Step 1: Failing tests** in `options.test.mjs`: `header user agent reaches the context options`
  (`contextOptionsFor({userAgent: 'DEFAULT'}, parseBrowserHeader('{"user_agent":"Mobile UA"}').options).userAgent === 'Mobile UA'`);
  viewport and locale map; absent header keeps the base; each invalid value of Task 1's list, a
  non-JSON value, a JSON array, an unknown key, a header over 2048 bytes and a repeated header
  (array) give `options: undefined` and a `warning`; `parseArgs` reads the Deployment's args
  (`--port 8931 --host 0.0.0.0 --isolated --allowed-hosts a:1,b:2 --output-dir /tmp/output
  --user-agent "Mozilla/5.0 …"`) and throws on `--caps vision`.
- [ ] **Step 2: Run** `node --test workers/playwright-mcp/`: FAIL.
- [ ] **Step 3: Implement `options.mjs`**, then `server.mjs`: an `http` server on `--host:--port`;
  a `Host` not in the allowed list (default `localhost:<port>`, `127.0.0.1:<port>`, `[::1]:<port>`)
  is 403, as the CLI does; a request with `mcp-session-id` goes to its transport (404 when unknown);
  a POST without one makes `new StreamableHTTPServerTransport({ sessionIdGenerator: randomUUID,
  onsessioninitialized })` (`playwright-core/lib/utilsBundle`), reads the header once (logging the
  warning with no header value), calls `createConnection({ browser: { browserName: 'chromium',
  isolated: false, launchOptions: { channel: 'chrome-for-testing', headless: true, chromiumSandbox:
  false }, contextOptions: <merged> }, outputDir }, getter)` from `@playwright/mcp`, where the
  getter returns `browser.newContext(<merged>)` on one Chromium launched lazily with those launch
  options and launched again after it disconnects; `server.connect(transport)`; on the transport's
  close, close the server and that session's context. An idle context (no request for
  `--idle-timeout`, default 3 600 000 ms, as the CLI's headless default) is closed; the next call
  gets a new one. SIGTERM/SIGINT close the browser and exit. Header comment: why this exists (#7027),
  the measured facts above with their sources, and that the image and `package.json` move together.
- [ ] **Step 4: Dockerfile stage** `FROM mcr.microsoft.com/playwright/mcp:v0.0.85@sha256:cc70a6922ff856f3aba0fe97d0d94220dc9a7f367f6d332facc6d81d9a91e60e AS playwright-mcp`;
  `WORKDIR /opt/scadbuddy-playwright-mcp`; copy `package.json`, `package-lock.json`; `npm ci
  --omit=dev --ignore-scripts`; a `RUN node -e` that fails unless `/app/package.json`'s version
  equals `@playwright/mcp`'s installed version and `/ms-playwright/chromium-<revision of
  playwright-core/browsers.json>` exists; copy `options.mjs`, `server.mjs`; keep `USER node`;
  `ENTRYPOINT ["/usr/bin/tini","-s","--","node","/opt/scadbuddy-playwright-mcp/server.mjs"]`. Run
  `hadolint` on it.
- [ ] **Step 5: Local smoke** (arm64 here): `docker build --target playwright-mcp -t sb-pwmcp:local .`;
  run it `--read-only --tmpfs /tmp --cap-drop ALL -e HOME=/tmp` with the Deployment's args and a
  default UA `DEFAULT-UA`; an MCP `initialize` POST with `X-ScadBuddy-Browser:
  {"user_agent":"SB-TEST-UA","viewport":{"width":390,"height":844}}`, `notifications/initialized`,
  then `tools/call browser_navigate {"url":"about:blank"}` and `browser_evaluate
  {"function":"() => navigator.userAgent + ' ' + innerWidth"}` → `SB-TEST-UA 390`; a second session
  without the header → `DEFAULT-UA`; a session with an invalid header → `DEFAULT-UA` and a warning
  line in the logs; a wrong `Host` → 403. Write it as `workers/playwright-mcp/smoke.sh <image>` and
  run it; `shellcheck` it.
- [ ] **Step 6: CI.** In `ci.yml`'s `agent` job, after the sandbox steps: build target
  `playwright-mcp` (`load: true`, `cache-from: type=gha,scope=playwright-mcp`), run
  `docker run --rm --entrypoint node <image> --test /opt/scadbuddy-playwright-mcp/options.test.mjs`
  (so copy the test into the image too) and `workers/playwright-mcp/smoke.sh`; add its container to
  the tear-down. In `build-image.yml`, the `playwright-mcp` job. Run `actionlint` and
  `.github/scripts/lint-buildx-mirror.sh`.
- [ ] **Step 7: Commit** `feat(agent): playwright-mcp wrapper with per-session browser options (#7027)`.

### Task 5: The chat panel's new-chat browser settings

**Files:**
- Modify: `frontend/src/agent/chat/protocol.ts` (`user.message` `browser`, same shape as the agent's)
- Modify: `frontend/src/agent/chat/useAgentChat.ts` (`send(text, context, images?, browser?)`; the
  field only when no `activeId`)
- Modify: `frontend/src/components/assistant/AssistantChat.tsx` and a new
  `frontend/src/components/assistant/NewChatBrowser.tsx` (the control)
- Modify: `frontend/src/mocks/agent.ts` (echo it into the mock session)
- Test: `frontend/src/components/assistant/NewChatBrowser.test.tsx`, `useAgentChat` test

**Interfaces:**
- Consumes: the chat socket's `user.message.browser` (Task 2).

- [ ] **Step 1: Load `frontend-design:frontend-design`.** Then failing tests: the control shows only
  for a new chat (no active session); empty, it sends no `browser`; a chosen preset ("Phone": an
  iPhone Safari UA, 390×844, or "Desktop (default)") or typed values send exactly those keys; it
  resets after the chat starts; the panel shows an error the agent answers (`invalid_browser`).
- [ ] **Step 2: Run** `pnpm exec vitest run <files>` (after `pnpm gen:api`): FAIL.
- [ ] **Step 3: Implement** a compact disclosure beside the composer's controls, labelled
  "Browser", with fields User agent, Width, Height, Locale (placeholders say the default), using the
  panel's existing components; no client-side bounds beyond input types (the API decides).
- [ ] **Step 4: Run** the tests, `pnpm lint`, `pnpm typecheck`: PASS.
- [ ] **Step 5: Commit** `feat(frontend): browser options for a new chat (#7027)`.

### Task 6: A flow's `agent()` session takes browser options

**Files:**
- Modify: `backend/scadbuddy/workflows/flow_tools.py` (`_agent_session`, `_start_session`)
- Test: the existing tests of the `agent` host function (find them with `git grep -n "_agent_session\|agent_session" backend/tests`)

**Interfaces:**
- Consumes: the `sessions_start` tool's `browser` input (Task 2: `{user_agent?, viewport?: {width, height}, locale?}`, bounds checked by the API, a refusal is a tool error naming the field).
- Produces: the flow host function `agent(prompt, skills, result_schema, images=None, grant=None, on_outside="escalate", browser=None)`; `browser` (a dict or None) goes into the `sessions_start` body only when it is not None, so a run that does not pass it sends the same body as before and its history replays unchanged (`tests/fixtures/project_workflow_histories/`).

- [ ] **Step 1: Failing tests:** `agent(..., browser={"user_agent": "UA"})` sends `sessions_start` a body with `browser` equal to it; without `browser` the body has no `browser` key (exactly the old body); the project workflow replay test still passes.
- [ ] **Step 2: Run** them: FAIL.
- [ ] **Step 3: Implement.** Extend the docstring (the script author reads it): what `browser` sets, that the session keeps it for its life, that the API refuses out-of-bounds values. No validation in the workflow. `agent_with_images` stays as it is (deprecated).
- [ ] **Step 4: Run** the tests, the replay test, ruff, ruff format --check, mypy: PASS.
- [ ] **Step 5: Commit** `feat(flows): browser options for a flow's agent() session (#7027)`.

### After the tasks

- Whole-branch review; push `feat/7027-session-browser-options`; PR titled
  `feat(agent): per-session browser user agent for the playwright MCP (#7027)`, body listing the
  rulings, `Part of #7027` (the clusters PR says `Fixes`), the owner marker; `gh pr merge --auto
  --squash`; the rulings as a comment on #7027.
- After merge and the image's first publish (make the GHCR package public, a one-time UI step), the
  eh-homelab/clusters PR: `applications/scadbuddy/playwright-mcp.yaml` image to
  `ghcr.io/eh-homelab/scadbuddy-playwright-mcp:sha-<7>@sha256:…`, args unchanged, comment updated.
