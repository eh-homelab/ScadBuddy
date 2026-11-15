# Agent service — notes for agents

These notes cover `agent/`; paths are repo-relative; the root CLAUDE.md has the repo-wide rules (migrations, CI, PR conventions, generated API files).

## Database access

The rule is in the root CLAUDE.md, "Database access (#5295)". Today the agent sidecar and
the agent workers connect as the database owner (`SCADBUDDY_DATABASE_URL`), keep the
`ai_*` tables, and run SQL on two backend tables in the gate's transaction (`flow_grants`,
`flow_grant_uses`: `src/gate/store.ts` `grantInput`, under the grant row's share lock, until
the gate moves to the CRUD service, #5299 step 10; `backend/scadbuddy/auth/caller_grants.py`
`AGENT_GRANTS`). The agent's other reads of backend tables (the event log, flow runs, their
decisions and grants) go only through CRUD routes (#5299 D7, below); the API revokes its
role's grants on `events`, `workflow_runs` and `workflow_run_decisions` at every start
(`backend/scadbuddy/auth/revoke.py` `RETIRED_GRANTS`), which binds nothing while the agent
connects as the owner (until #5299 step 12); `test/retiredTables.test.ts` is the guard. The workspace files' `blob_refs` holds go through CRUD routes
(#5299, below), no longer a trigger. `src/db/grants.ts`
grants the API's role its `ai_*` reads and writes, and revokes what it once granted the
projects worker's role `scadbuddy_projects` (#5298; the worker holds no database).
Claude Code, and any plugin package approved with `allow_refused`, runs in the same pod
as the owner URL and the KEK (`docs/ai/security.md`, "Plugin packages").

Under the rule the agent holds no database secret. Its data (sessions, transcripts,
approvals, credentials, payload keys) goes behind CRUD routes, the cross-service reads
become routes, and Claude Code and plugin code run where no database secret exists
(#5299). Its 8081 routes move to `enforce` (#5297). Never add a backend table read, a new
`ai_*` grant or a trigger on another service's table; add a CRUD route that requires a
service credential instead.

The agent's service credential (`src/api/serviceCredential.ts`, wired in `main.ts`): with
`SCADBUDDY_SERVICE_TOKEN_FILE` or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` (both is a `ConfigError`)
`ExchangeCredential` exchanges that proof at the backend's `POST /api/v1/auth/service-token`
for a 15-minute credential, renews it at half-life and after a 401, and falls back to the view
on a 404 while it has a database. A failed exchange is tried again after 5 s doubling to 60 s,
but a refused connection (the backend not up yet, `src/api/backendUp.ts` `backendNotUp`, #5868)
is tried again at the next call and logged once as "waiting for the backend". Without a proof `ServiceCredential` reads the sealed one from
`service_credentials_agent`, as before (#5494).

The agent's own CRUD routes (`/api/v1/internal/crud/agent/`, class `crud.agent`, #5330): the
holds of the workspace files' blobs (`workspace-blob-refs`, #5299; see the workspace under
"Layout"), the assistant's spend on an order, and its reads of the backend's tables (#5299
D7): area `events` (the LISTEN gap replay's head and gap, `src/events/eventLogReads.ts`, used
by `pgListener.ts`) and area `flows` (a run's starter, its decision checks, a grant in force,
a run's queue and steps, the waiting runs, the parent-hop approvals and which of them still
wait: `src/flows/backendReads.ts` `CrudFlowRunReads`, used by `temporal/flowRuns.ts`,
`flows/queue.ts`, `flows/escalations.ts` and `temporal/toolActivities.ts`
`PgApprovalRecords`), all over `src/api/crudRead.ts` `crudRead`. There is no SQL fallback:
`test/retiredTables.test.ts` fails on agent SQL that names `events`, `workflow_runs` or
`workflow_run_decisions`, and the Postgres tests answer the flow reads with
`test/support/sqlFlowRunReads.ts` (the backend's queries on the test database). Its settings, Claude credentials and payload data keys (#5299 step 4, D5;
#5342; `SECRET_AREAS`): area `settings` (`src/credentials.ts` `SettingsStore`'s `remote`,
`CrudSettings`: a compare-and-set sends what its check read as `expect`, and the API sends the
warm-evict and runtime NOTIFYs in the write's transaction, so the LISTEN side is unchanged),
area `credentials` (`CrudCredentials`, `gatedCredentials`: the API seals and opens with the
KEK, the routes and the turn's pool read the API's key through the repo's `opener`, and a turn's
credentials come from `…/credentials/reveal`, audited per call) and area `payload-keys`
(`src/temporal/payloadCodec.ts` `CrudPayloadKeys`, `gatedPayloadKeys`: a subject's data key,
made under the subject's lock and never for a forgotten subject, audited as kind `payload_key`
(a release once per subject in 10 minutes), cached 30 s here).
None needs a KEK in this process once listed. Area `forget` (#5299 step 6, `FORGET_AREAS`;
backend `api/crud_agent_forget.py`): `forgetSubject`'s key transaction (`src/sessions/forget.ts`
`CrudSubjectKeys.forget`: under the subject's lock, the tombstone, a flow run's images and the
key; a session with forks is refused `forks-take-ranges` and forgotten in the agent's own
transaction, whose `copyForkRanges` opens and seals its transcript, until the sessions move,
#5299 step 9), a browser call's key dropped and the hourly key sweep (`gatedKeyDeletes`), and a
flow run's images (`src/temporal/flowBlobs.ts` `CrudFlowBlobs`, `gatedFlowBlobs`: sealed here,
sent as multipart bytes, the tombstone check and the cap in the API's transaction).
Area `workspace` (#5299 step 7, `WORKSPACE_AREAS`; backend `api/crud_agent_workspace.py`):
the workspace files (`src/workspace/crudStore.ts` `CrudSessionFiles`, `gatedWorkspaceFiles`
over the `WorkspaceFiles` interface `SessionFiles` also takes): the API seals, stores and opens
each file and re-wraps principals' keys, a write's row, blob hold and `workspace.file` events
are one transaction there (`WriteOptions.log`, `DeleteOptions.log`), and the reconcile is one
transaction under `RECONCILE_LOCK`; this process sends and gets plaintext only.
Areas `session-blobs`, `attachments` and `http-bodies` (#5299 step 7, `BLOB_AREAS`; backend
`api/crud_agent_blobs.py`): a session's images (`src/sessions/crudBlobs.ts` `CrudSessionBlobs`
behind the `SessionImages` interface; a durable tool call's images replaced whole, and a turn
placement's write fenced by `TurnFence`, checked `FOR SHARE` in the API's transaction), the
staged attachments (`src/attachments/crudStore.ts` `CrudAttachmentStore` behind `Attachments`)
and the saved http_request bodies (`CrudHttpBodies`, read by range), each chosen per call by
`gated` in `main.ts`; the bytes are multipart parts and answer bodies. `copyForkBlobs` stays
SQL, in the forget's transaction, until #5299 step 9.
A session's background tasks, a Monitor's events
and a Workflow's journal and child chats (area `background-tasks`, `src/background/store.ts`
`BackgroundTaskStore`'s `remote`, `CrudBackgroundTasks`) and the command record `ai_operations`
(area `operations`, `src/operations/store.ts` `CrudOperations`; the API writes the operation's
activity row in its transaction) are #5299 step 8 (`TASK_AREAS`, backend
`api/crud_agent_tasks.py`); their bodies go through `crudRead.ts` `wellFormed`, since the API
refuses a lone surrogate. These ten have a rolling-deploy fallback:
`src/api/crudAreas.ts` `CrudAreas` reads `GET …/crud/agent/areas` from the start with back-off
(1 s doubling to 30 s) until every one is listed, and until then each stays on SQL, for an API
pod older than the routes. The small stores (#5299 step 5, `src/api/crudStores.ts`
`STORE_AREAS`, backend `api/crud_agent_stores.py` and `api/crud_agent_notify.py`) take the same
fallback, chosen per call by `gated` (a store's SQL class until its area is listed, its `Crud…`
class after): area `activity-kinds` (`activity/kinds.ts` `crudUpsertActivityKinds`),
`browser-origins` (`browser/store.ts` `BrowserOrigins`'s `remote`, `CrudSessionOrigins`),
`bridge-tabs` (`CrudSessionTabStore`, `CrudTabOwnerStore`, `CrudTabImageStore`: an image's bytes
are the body either way, `crudCall`), `pairings` (`CrudPairingStore`: only the code's hash
leaves this process; the API holds the advisory lock and the `FOR UPDATE`), `mcp-tokens`
(`CrudTokenStore`: a mint and a verify send the token's hash only, expiry by the database's
clock) and `notify` (`notify/pushStore.ts` `CrudPushStore`, `notify/channels.ts`
`CrudChannelRows` through `ChannelStore`'s `remote`: the API seals and opens the VAPID key,
the subscriptions' keys and the channels' tokens, and re-wraps them at a rotation). With no
KEK here Web Push and the channels work once `notify` is listed (`available`); the test is
`test/crudStores.test.ts`. The plugin stores take the same fallback (#5299 step 5, backend
`api/crud_agent_access.py`, `api/crud_agent_plugins.py`, `api/crud_agent_packages.py`): area
`access` (`access/store.ts` `AccessStore`'s `remote`, `CrudAccessRows`: the model is checked
and the audit row written here, the API runs the write with its warm eviction; and
`access/listings.ts` `ToolListings`'s `remote`, `CrudToolListings`: a connection test holds
the lease `tool-listing:<source>` on the API, `api/crudStores.ts` `CrudLeases`, renewed every
30 s, in place of the session advisory lock, and its listing is stored only while the lease
holds, else 409 `lease-lost`), `plugin-sessions` (`plugins/pluginSessions.ts`
`CrudPluginSessions`, a `PluginSessionRows`, the sweep's ledger too), `plugins`
(`plugins/registry.ts` `CrudPluginStore`, `plugins/oauthStore.ts` `PluginOAuth`'s `remote`,
`CrudOAuthRows`: the API seals and opens the header secrets and the sign-ins with its KEK,
and a token refresh holds the lease `plugin-oauth:<plugin>`) and `plugin-packages`
(`plugins/packages/store.ts` `CrudPackageStore`: the API runs each write with its prune and
warm eviction). With no KEK here the plugins and their sign-ins work once `plugins` is listed;
the tests are `test/crudAccess.test.ts`, `test/crudPlugins.test.ts` and
`test/crudPackages.test.ts`. During a roll an agent pod on SQL still takes the old advisory
lock, and the API's lease acquire tries that lock too, so a lease is `busy` while such a pod
tests the plugin; such a pod does not read the lease, so it can start a test while a lease
holds (two tests of one plugin at once, the last listing kept; backend `agent_data/leases.py`). A route's
answer counts only with `X-ScadBuddy-Crud: 1`; any other failure takes the read's existing
error path (a lookup failure the activity retries, a 500, a resync). A session's successful `order_item_revision_add` (or
`_after_acceptance`) is an `ai_session_order_costs` row (`src/sessions/orderCosts.ts`
`OrderCosts`, a `TouchedSink` beside `SessionResources`); each turn's end
(`temporal/session/finishActivity.ts`, a retry too, not awaited: it never holds up the next
turn) reports the session's `cost_usd` since its last report, split equally between its
orders still to report, to `POST …/crud/agent/orders/{id}/assistant-cost` (5 s each, 15 s
for the turn's reports in all, one report per session at a time across replicas, by an
advisory lock on a `database.locks` connection, never the stores' pool, and none while every
lock connection is out; `sweep` at start and hourly reports what a session's last turn left,
20 sessions at most, the ones tried longest ago, `tried_at`, first; a share under one minor
unit is closed out with nothing carried once its session is over: `done`, `failed`, or `idle`
with no session activity, `updated_at`, for an hour, `IDLE_OVER_AFTER`; if it works again,
that spend folds into its next turn's share, split equally across the orders that turn revises). `record` waits at most `RECORD_WAIT_MS` for its row (#1076). Only the
route's own 400 or 422 (a path or body it cannot take), 404 (the order gone: an item gone
falls back to the order) or 409 (a currency no rate reaches, a visitor's unsubmitted cart) drops
that order's report, its share in no entry. Anything else (401 or 403 while there
is no live credential, 429, a 5xx, a transport failure, a non-CRUD answer) keeps it with the
share it sent (`attempted_usd`), sent again as it was at the next turn's end, so a lost
answer loses no spend; an answer with no entry (under one minor unit) or the deadline keeps
it too.

## Commands

Agent service (`agent/`, Node 24, pnpm via corepack; the `agent` CI job):

```bash
cd agent
corepack enable
pnpm install --frozen-lockfile
pnpm lint && pnpm typecheck && pnpm test && pnpm build
docker build --target agent -t scadbuddy-agent:dev .   # checks the bundled Claude Code binary
```

`pnpm exec tsc` and `pnpm exec vitest` skip `gen:api`: after a fetch of main, TS2820/TS2345
errors that name an `Operation` or an API path in `src/tools/*.ts` mean `src/api/schema.d.ts` is
stale. Run `pnpm gen:api`.

Tests never call Anthropic. `test/run.test.ts` runs the bundled Claude Code binary
against a local fake Anthropic endpoint; `test/pg.test.ts` needs
`SCADBUDDY_TEST_DATABASE_URL` (e.g. `docker run -d -e POSTGRES_PASSWORD=postgres
-e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:18`, then
`SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm test`).
With it set, `pnpm test`'s globalSetup migrates a template database once and each
Postgres test copies it (`test/support/postgres.ts`, #2018), so the test role needs
`CREATEDB`, as the backend's does: without it the whole suite fails at setup, not
only the Postgres tests.
Evals (`agent/evals/`, `docs/ai/evals.md`): each scenario is a session on the one runtime,
an AgentSession that an `api` process starts and a `worker` process runs (`evals/durable.ts`
`startEvalStack`: Temporal, a throwaway Postgres, both roles in-process; plan 2026-10-13
Step 6.6). `test/evals.durable.e2e.test.ts` replays each scenario against the fake endpoint
in `pnpm test` (it skips without Temporal or Postgres); `pnpm evals` runs them live with the
credential saved in Settings (or `SCADBUDDY_EVAL_ANTHROPIC_API_KEY`, CI only; the
manual `ai-evals.yml` workflow) and skips cleanly without one. The render scenario
runs live only against a real backend (`SCADBUDDY_EVAL_BACKEND_URL`, the image; it
skips without one), and its render is checked `verify.sh`-style on the saved output's
3MF; replies with suggestions are scored on their citations (`evals/citations.ts`,
#1924).
`.github/scripts/lint-one-runtime.sh` (the `lint` job) fails on a second turn path: a session
mode branch in `agent/src`, the SDK's `query` imported outside `harness/run.ts` and
`harness/testConnection.ts`, or `agent-durable/` importing or depending on the Claude Agent
SDK or naming `DurableSession` (plan Step 8.1).

`agent/test/fixtures/secret-vectors.json` is written by `agent/test/secretVectors.test.ts`
(`UPDATE_SECRET_VECTORS=1`) and opened by `agent-durable/tests`: a change to
`agent/src/secrets.ts`'s format or `credentials.ts`'s AAD needs new vectors, and the port must open them.
`agent/test/fixtures/payload-vectors.json` does the same for the payload codec
(`agent/test/payloadCodec.test.ts`, `UPDATE_PAYLOAD_VECTORS=1`; `agent-durable/tests/test_codec.py`
opens every one and seals it again byte for byte).


## Layout

- `agent/` — the AI agent service (#261), TypeScript on the Claude Agent SDK, shipped
  as the Dockerfile's `agent` target and run as a sidecar container of the API pod (role
  `api`) and as the `scadbuddy-agent-worker` Deployment (role `worker`, clusters#3014). `src/config.ts`
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_DATABASE_URL`,
  `SCADBUDDY_BACKEND_URL`, `SCADBUDDY_SECRET_KEY_FILE`,
  `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (rotation), `SCADBUDDY_PUBLIC_URL` (the same
  variable the backend reads; the one origin allowed to write),
  `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed) and the
  backend's `SCADBUDDY_TEMPORAL_ADDRESS`, `_NAMESPACE` and `_SEARCH_ATTRIBUTES`. No AI
  env vars; AI settings live in the database.
  `SCADBUDDY_AGENT_ROLE` (#2726 Step 6.1; `all` by default, `api`, `worker`) picks what
  `main.ts` starts (`src/runtime/role.ts` `roleParts`): `api` is the HTTP half and polls
  only its own `agent-tools.<hostname>` (browser calls for its tabs), so it runs no
  `AgentSession` turn; `worker` runs the turn workers and serves only
  `src/runtime/health.ts` (`/healthz`, `/readyz`). `test/agentRole.temporal.test.ts` runs
  one of each on one dev server; a change to what a turn reaches in-process must keep it.
  One runtime (#2726, spec 2026-10-13, plan `docs/superpowers/plans/2026-10-13-unified-agent-runtime.md`):
  every session is an `AgentSession` workflow, `session-<id>` on `agent-sessions`
  (`src/temporal/session/workflow.ts`, deployment `scadbuddy-agent`, AUTO_UPGRADE: a change goes
  behind `patched()`, and `test/fixtures/agent_session_histories/` replays). Each turn is its
  `session_turn` activity on the session host queue `agent-sessions.<hostname>` that
  `acquire_worker` reserved a warm place on (`affinity.ts`, §5.2), run by
  `sessions/turnHost.ts` over `turnCore.ts` and `turnRunner.ts` (the turn's assembly) on the
  replica's warm Claude Code process (`sessions/warm.ts` `WarmSessions`, sized by the
  `session-runtime` settings, `runtimeSettings.ts`; a process is recycled when its fingerprint,
  transcript tail or budget changes). A park holds the process for `park_hold_seconds`, then
  closes it and the activity returns `released`; once every held call is decided, the turn is
  placed again with a `parked` continuation that runs each approved call once (§5.4). A turn
  lost with its pod is placed again with a `restarted` continuation, which resumes with
  `RESTARTED_PROMPT` once its message is in the transcript (§5.5). Every tool call
  of a turn on Temporal is a `ToolCall` workflow of its own, `session-<id>-call-<tool_use_id>`
  (`temporal/toolCall.ts`, `tools/activityProjection.ts`, plan Steps 9.2 and 9.3; the
  `tools_as_activities` switch is gone, Step 9.9), started with `requestEagerStart` on the turn's
  replica's own agent-tools worker connection while it polls, once its build has been made current (AgentWorker
  `connection`, #5865; it stays so when a later build is promoted, and Temporal grants an eager start
  to such a superseded build, whose worker then runs the call: measured on a dev server, Server
  1.32.0, `toolCallLatency.temporal.test.ts` `SUPERSEDED`; the call's span attribute `scadbuddy.eager` says
  `started`, `declined`, `failed` or `fallback`): the first workflow task comes back in the start's answer
  without waiting on matching, and the activity is dispatched eagerly to the same worker when it is
  on agent-tools and the worker has a free slot (a pinned server's call goes to `agent-tools.<hostname>`
  through matching). Both need the server's `system.enableEagerWorkflowStart` and
  `system.enableActivityEagerExecution`, on by default at Temporal v1.32.1
  ([constants.go](https://github.com/temporalio/temporal/blob/v1.32.1/common/dynamicconfig/constants.go))
  and not overridden in prod's `scadbuddy-temporal-v2-dynamicconfig` (read 2026-11-10);
  `test/toolCallLatency.temporal.test.ts`
  times each phase around the activity when asked (`TOOL_CALL_BENCH_REPORT=1` prints them,
  `TOOL_CALL_BENCH_ASSERT=1` fails a median overhead over 1 s) and always checks how each call was started; without Temporal the calls run in the turn's
  process. A session's working directory is `<SCADBUDDY_SESSION_DIR>/<id>/` (see the workspace
  below); a turn whose transcript last ran in another directory (a fork's first) is told so in
  its prompt (`sessions/directoryNote.ts`, #5867). Spans: `agent.turn` (with `scadbuddy.continuation` on a continued placement),
  `agent.turn.spawn` (`warm.outcome`), `agent.turn.release`, the turn's span of each call with
  `scadbuddy.eager` (above), and each ToolCall's
  `agent.tool/<name>` with `scadbuddy.task_queue`, which `deploy/grafana/scadbuddy-agent.json`
  charts; a flow run's call (a `flow-<id>` workflow's ScadBuddy tool or `turn:` activity) has the
  same `agent.tool/<name>` span, with `scadbuddy.flow_run_id`, `scadbuddy.tool_use_id`,
  `scadbuddy.tier` (once known), `scadbuddy.outcome` and `scadbuddy.task_queue` (#6123; a call that
  throws before it names an outcome reads `error`, `flowCallSpan`); and a browser call to another replica's tab, `agent.bridge.call` (`bridge/relay.ts`),
  whose `scadbuddy.bridge.task_timeouts` counts its BridgeCall's workflow tasks that timed out
  (Temporal started them and never delivered them, #5939), read from the history (once the workflow
  closes) for an aborted call and for one that took at least `BRIDGE_WORKFLOW_TASK_TIMEOUT_MS`,
  and logged when above 0.
  `src/app.ts` is the Hono server (`/healthz` and `/mcp`). Every other route group is a
  `src/routes/<name>.ts` that exports `route` (`routes/module.ts`) and is found without
  an edit to `app.ts`: among them `credentials.ts` (`/api/v1/ai/credentials`),
  `status.ts` (`/api/v1/ai/status`), and the assistant's WebSocket `/api/v1/ai/chat` and
  `/api/v1/ai/sessions` in `chat.ts` and `sessions.ts`, backed by `SessionManager`. A
  dependency only that group needs is declared in its own file, by augmenting `AppDeps`
  (#508); one several groups share (`sessions`, `upgradeWebSocket`) stays in `app.ts`.
  A route module imports only types from `app.ts` (`routes/index.ts` loads them while
  `app.ts` loads). Every route that must know "is this the UI's origin"
  (credential writes, `/mcp`, and the chat socket and session routes under `/api/v1/ai/*`) uses the one allowlist in
  `src/http/origins.ts`, never an `Origin == Host` comparison (DNS rebinding makes
  those equal). `src/harness/options.ts` builds every query's SDK options
  (`tools: []`, `settingSources: []`) and `src/harness/run.ts` runs every `query()` on
  top of it (never the credential in Claude Code's env: each run leases a token on the
  loopback credential gateway, `src/gateway/server.ts` and `tokens.ts`, plan Step 9.4, which
  adds the credential and meters the spend, so the env carries only `ANTHROPIC_BASE_URL` and
  that token; `maxTurns`, `maxBudgetUsd`,
  abort, the tier seam in `src/harness/permissions.ts` as both `canUseTool` and a
  `PreToolUse` hook; outward calls in a session PARK in `canUseTool` on the session
  workflow's gate until a human decides (`sessions/turnHost.ts`, the Update `open_input`;
  its `ai_pending_input` row is the request's record, plan Step 9.7), #258; `/mcp`'s outward
  calls are `src/approvals/service.ts`'s `ai_approvals` rows, run by `confirm_action`; outside
  a session they are denied as "needs approval"; the built-in `AskUserQuestion`, given
  only to sessions the browser user owns, parks there too until the user answers in the
  panel, via `src/harness/questions.ts` and the session workflow's gate (an `ai_pending_input`
  entry, the request's record, plan Step 9.7), #940, and never outlives its turn; the agent's
  `request_user_attention` tool, #815, `src/harness/attention.ts`, parks on the same gate as a
  `wait_for_user` entry, with a timer that never answers: `proceed` returns `timed_out`, `wait` and
  `stop` end the turn; its `done` reason waits for nothing and outlives its turn, an `ai_questions`
  row (`src/gate/questions.ts`), shown beside the badge's waiting count (not in it) until dismissed, at most
  `DONE_POSTS_PER_TURN` a turn, carrying `src/gate/doneSummary.ts`'s record of what the turn
  touched, unattended actions first; `GET /api/v1/ai/pending-input`, `src/routes/pendingInput.ts`, is
  the one read of every parked call, approvals and answers, that the badge counts, and
  `POST /api/v1/ai/pending-input/{request_id}` the one respond route the panel answers
  any of them through, refusing a stale id, a resolved entry or a body of the wrong kind;
  a browser_* call that finds no tab in such a session parks the same way as a
  `tab_disconnected` request, resolved `reconnected` when the bridge sees the session's
  tab again, `sessions/manager.ts` `waitForTab`, `bridge/hub.ts` `onSessionTab`; in an
  AgentSession turn both park as the workflow's `wait_for_user` entry, `sessions/turnHost.ts`,
  and the hub's process ends a tab wait with the Update `tab_reconnected`, #5003);
  `src/api/backend.ts` is the `openapi-fetch` client over the generated
  `src/api/schema.d.ts`.
  Tracing (#988): `src/telemetry.ts` is the `node --import` entry (Dockerfile `CMD`,
  `pnpm start`) that registers the OTel ESM hook, then `src/telemetry/setup.ts` starts
  the SDK (standard `OTEL_*` variables only; incoming HTTP only). `main.ts` imports
  only the SDK-free `src/telemetry/runtime.ts`, so with `OTEL_SDK_DISABLED=true` no SDK
  package is loaded (#1351).
  `src/telemetry/scrub.ts` strips exception messages, query strings and user agents
  before export; `src/telemetry/turn.ts` (`TurnTrace`) ends a turn's spans at every
  park and opens `agent.turn.resume` under the decision (`ai_approvals.traceparent`,
  `decision_traceparent`). Only `api/backend.ts`'s middleware injects `traceparent`;
  never add trace context to another outgoing call. Tests share one provider
  (`test/support/tracing.ts` `testTracing`).
  `src/tools/` is the tool registry (#251): one `defineTool`
  per tool, projected in-process for the harness and over `/mcp` (`src/mcp/http.ts`,
  auth in `src/auth/`); every `/api/v1` operation needs a tool or a
  `src/tools/coverage.ts` entry, or `test/coverage.test.ts` fails.
  - Database: the agent owns the `ai_*` tables (today's state; #5295 moves them behind
    CRUD routes and takes the agent's database secret away, #5299). The agent does not
    migrate them: the API applies their files at its start (root CLAUDE.md "Migrations
    (#491)", #5299 D6), and `ready()` (`src/db.ts`) is true once `ai_migrations` holds
    every file in `src/db/expected-migrations.json` (`src/db/schema.ts` `awaitMigrations`;
    one read a call, so a route that asks fails fast; a row with other SQL than listed is
    `MigrationChecksumError`, and `main.ts` exits on it). Work a process does once at start
    waits for `untilReady()`, which calls `ready()` again 1 s apart, doubling to 30 s,
    until it is true, so an API that migrates late drops none of it; the agent logs
    `waiting for the API to apply the agent's database migrations` once a wait; meanwhile
    `/healthz` says `unavailable (waiting for the API to apply the database migrations)`
    (`waitingForMigrations`). The
    tests make their schemas with the agent's old runner, `test/support/migrations.ts`
    (`test/support/postgres.ts` `migrateSchema`, the `schema` option of
    `connectDatabase`), and `test/schema.test.ts` checks the list against both runners. Secrets are
    envelope-encrypted with `src/secrets.ts` under the KEK in
    `SCADBUDDY_SECRET_KEY_FILE` (32 random bytes, base64; spec §9); the AAD binds each
    value to its row and to the columns that say where it is sent (for the credential:
    `kind` and `base_url`). Comparable tokens are stored hashed instead.
  - Temporal (#1055, spec 2026-10-01 §6.3): with `SCADBUDDY_TEMPORAL_ADDRESS` (and the
    database) `src/temporal/worker.ts` runs one worker on `agent-tools`, a version of
    `scadbuddy-agent` with `agent-sessions` when the build has an id (#2726 Phase 11; the
    host queues stay unversioned, `agent/test/temporal.versioning.temporal.test.ts`):
    every `ALL_TOOLS` entry as an activity under its name (`toolActivities.ts`,
    `runToolWithOutcome` with `gate: 'workflow'`, only for a `session-<id>` workflow (as the
    session's owner with its turn's tiers; `mode` is not read, since every session is an
    AgentSession from plan Step 6.3), or a flow run's `flow-<id>` (`flowRuns.ts`, #1057: as the
    run's starter, in no session; a gated call needs the backend's `workflow_run_decisions`
    approval; no browser tools)), a turn's other tools (#2320, plan Step 9.3,
    `turnTools.ts`, built by `turnServers.ts`: remote plugins
    through the forwarder, packages' http MCP servers and `http_request`): `listTurnTools` lists them per turn as
    `<server>__<tool>`, `turn_tool_tier` tiers a call from its input before it may park,
    and each call is the worker's `default` activity `turn:<server>__<tool>`, which builds
    the servers again and checks tier, principal and approval as it runs (a `flow-<id>`
    workflow's call too: as the run's starter, approved only by `workflow_run_decisions`,
    with no browser servers; a server whose
    state lives on the pod, `http_request` and the browser, is `pinned`: its calls run on
    `agent-tools.<hostname>`, which each agent also polls); and `AgentOperation` (`workflows.ts`, bundled by
    `pnpm build` into `dist/temporal/workflow-bundle.js`), the §4.2 command shape for
    the agent's commands, recorded in `ai_operations` (`src/operations/`; through the API's CRUD
    routes once it lists area `operations`, #5299 step 8). A change to
    `AgentOperation` goes behind `patched()`; `test/fixtures/agent_operation_histories/`
    replays. Temporal tests (`test/*temporal*.test.ts`)
    skip unless `SCADBUDDY_TEST_TEMPORAL_DEV_SERVER` names a Temporal CLI (or
    `temporal` is on `PATH`); the `agent` CI job installs the Dockerfile's pinned one.
    The `@temporalio/*` packages are pinned exactly, all one version.
  - The tool-call gate (#1056 phase 5b, spec 2026-10-01 §6.6, plan
    `docs/superpowers/plans/2026-10-08-durable-phase-5b-tool-call-gate.md`) is
    `src/gate/`: request ids (`ids.ts`: `approval:`, `question:`, `durable:<session>:<run>:<tool_use_id>`),
    the `respond` validator shared with agent-durable (`validate.ts`; `role.ts`;
    `test/fixtures/pending-input-vectors.json` pins the two languages together),
    the entry shapes and the projection read (`projection.ts`, `ai_pending_input`), the
    record's writers (`store.ts`: `ai_pending_input.gate` is an entry's record and
    `decideInput` decides it in one transaction, then the Signal `input_decided` wakes the
    workflow, which also re-reads it through `gate.read_input`, plan Step 9.7), the Temporal
    client of a session's gate handlers (`durable.ts` `DurableGate`), the `done` summaries and
    attention limits (`questions.ts`, `doneSummary.ts`, `waiting.ts`; `gate/classic.ts` and
    `questions/` were folded in, #5110) and the orphan sweep (`sweep.ts`). The workflow's half is
    `temporal/session/gate.ts` (`SessionGate`) and `gateActivities.ts`. `routes/pendingInput.ts` serves
    both reads and `respond`. `input.requested` / `input.resolved` are logged and never
    sent on the chat socket (`routes/chat.ts` `sentToPanel`).
  - Durable payloads (#1056 phase 5c, spec §6.5): `src/temporal/payloadCodec.ts` seals
    every payload of a `session-<uuid>` / `flow-<uuid>` workflow under that subject's
    data key (`ai_payload_keys`, sealed under the KEK; the API re-wraps them at its start on
    rotation). The agent-tools worker and the service's Temporal client carry it; once the
    API lists area `payload-keys` the API releases each key (`CrudPayloadKeys`, #5299 step 4,
    #5342), so no KEK is needed here, and until then it opens them with its own
    (`gatedPayloadKeys`; with neither, a call that needs one is `PayloadKeyUnavailable`). `src/sessions/forget.ts` `forgetSubject` (key row, then the workflow, then
    the rows; the key row through the API once it lists area `forget`, above) is the one way a durable subject is deleted; operators run
    `node dist/forget-subject.js session-<uuid>`. A fork reads its parent's transcript
    through the cut (`ai_sessions.fork_parent_id`, `fork_cut`; `src/sessions/store.ts`,
    plan Step 6.5), and its event log and images through `fork_event_cut` (#4212:
    `src/sessions/eventLog.ts` `read`, `blobs.ts` `get`; its own `session.started` is seq 1,
    its own later events come after the cut), so forgetting a parent first copies each fork's
    range into it, the transcript under the fork's key (`copyForkRanges`, `inherited` rows),
    and refuses without the KEK when a fork takes transcript entries (`fork_cut` above 0; a
    fork of a closed DurableSession takes none). Every read through the fork chain stops at
    the least cut below each ancestor (`LEAST(bound, cut)`: `eventLog.ts` `forkChain`,
    `store.ts` `chainRows`, `blobs.ts` `getThroughChain`); a new chain read must do the same.
    When it is made, a fork copies none of its parent's transcript, event or image rows; its workspace file rows
    are copied, naming the same bytes (`SessionFiles.copy`). A gated tool's activity runs only
    with an `approved` `ai_input_responses` row for its own request id.
  - DurableSession, the retired Python turn (plan 2026-10-13 Steps 7.2 and 8.1, #4772): its
    open sessions were moved to AgentSession on 2026-11-01 (their conversations in
    `ai_session_entries`). A row still `runtime = durable` closed before that and has no
    transcript: a send to it is refused `closed` (`sessions/durable.ts`), and a fork of it is
    an AgentSession with the panel's history and no model context (`manager.ts` `fork`).
  - The workspace (#2329, `docs/ai/operating.md` §13): `src/workspace/store.ts`
    `SessionFiles` over `ai_session_files`, keyed by `workspace` (`session:<id>`, gone
    with its session, copied by a fork; or `principal:<kind>:<id>` outside a session,
    swept 24 h after its last write) and a checked `path`; caps
    `workspace_file_max_bytes`/`workspace_max_bytes` and `WORKSPACE_MAX_FILES` (5,000). Reached as
    `ToolServices.workspace` (so in-process, `/mcp` and the tool activities alike),
    with `workspaceOf(ctx)`; the `workspace_*` tools are `src/tools/workspace.ts`, and
    `http_request`'s `save_to_workspace` gets the session's from `sessions/manager.ts`.
    Never a file on the pod; a session's working directory, `<SCADBUDDY_SESSION_DIR>/<id>/`
    (each worker's own capped emptyDir, Step 9.5; not the NAS share, whose backup cannot leave
    the plaintext copies out), is a copy of it that each turn's placement fills and each park
    and its end write back by sha256 (`src/workspace/directory.ts`). The bytes (#4789) are a blob of the backend's content store,
    sealed under a per-file key wrapped under the session's data key (`src/workspace/sealing.ts`,
    `blobs.ts`; backend `api/internal_workspace.py` mints the URLs; the `blob_refs` holder
    `(ai_session_file, <workspace>/<path>)` goes through the agent's CRUD routes, backend
    `api/crud_agent.py` `…/crud/agent/workspace-blob-refs`, #5299: `SessionFiles` holds before
    the row that names a blob and releases after the row is gone, and `reconcile`, on the
    sweeper's tick hourly (a failed or `no_connection` run is tried again at the next tick),
    on one replica at a time (`RECONCILE_LOCK` on a `database.locks` connection), releases what cascades, `forgetSubject` and crashes left and holds
    a row no hold names; no trigger; once the API lists area `workspace` all of this runs on the
    API instead, above;
    `test/workspaceBlobs.backend.e2e.test.ts` runs it against the real API on moto,
    `backend/tests/support/workspace_backend.py`). Only on `store_backend` local do they stay
    in Postgres, one `ai_session_file_bytes` row per sha256 that every file with those bytes
    names, so a fork shares them (#4212); on any other backend a write that cannot be sealed and stored is refused
    `unavailable`, never kept in plaintext. Rows of `ai_session_file_bytes` are shared and swept: a
    writer locks them `FOR KEY SHARE` first, inserts only the missing ones and locks again, in a
    loop (`putBytes`, `copy`); `INSERT … ON CONFLICT DO NOTHING` takes no lock on the existing row,
    and the table's CHECK (sha256 of the bytes) runs before the conflict check (`ExecInsert`,
    [nodeModifyTable.c](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/executor/nodeModifyTable.c)). Uploads take `workspace_path` + `workspace_sha256` instead of
    inline content (`src/tools/workspaceUploads.ts`: binary streamed by
    `SessionFiles.chunks` into the multipart route, text decoded as UTF-8).
    The panel's Files (part 3) are `routes/sessionFiles.ts`
    (list, download as an attachment, multipart upload, delete; the session routes'
    guards, writes owner-only, each write a `workspace.file` event) and the caps' route
    `routes/workspaceSettings.ts`; the frontend is `components/assistant/SessionFiles.tsx`
    with msw `src/mocks/features/sessionFiles.ts` (component tests stub the upload:
    jsdom's `File` cannot pass undici's multipart parser). An `/mcp` client puts a file
    into its principal workspace with `PUT /mcp/workspace/<path>` (#4550,
    `src/mcp/uploads.ts`, mounted by `mountMcp` behind `/mcp`'s gates and auth; `write`
    tier, `disabled` mode refused), raw bytes answered with `{name, mediaType, size, sha256}`.
  - Plugins given to the harness are vetted by `src/harness/plugins.ts`: anything that
    starts a process (command hooks, stdio MCP servers, LSP servers, monitors) or runs
    plugin code in Claude Code (a hooks file's `modules`, on by default since Claude
    Code 2.1.287, and `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS` no longer turns it off) is
    refused, because it would inherit the credential env. A hooks file may set only
    `$schema`, `description` and `hooks` besides, so a new loader key is refused too.
    The one way past it is a plugin package approved with `allow_refused` (store.ts,
    column `allow_refused`, bound to that pin's approval): it loads as it is through
    `allowedPluginPaths`, unchecked, with the built-ins it names (`builtinTools`, each
    call `outward`; the session's own, `SESSION_BUILTINS`, are never among them and keep
    their item's tier) and skill shell injection on (with `Bash` offered, Claude Code
    2.1.287 runs an injection without asking the seam). A name that is not a safe path segment
    (`SAFE_NAME_RE`; a reserved name is allowable), paths outside the package, symlinks,
    the hash and the egress check stay fatal (`vet.ts` `fatal`).
  - Remote MCP plugins (#297) live in `ai_plugins` (`src/plugins/registry.ts`, routes
    `src/routes/plugins.ts` under `/api/v1/ai/plugins`). Claude Code never gets a
    plugin's URL or secret: it gets `http://127.0.0.1:<port>/p/<token>` on the loopback
    forwarder (`src/plugins/forwarder.ts`), which pins the checked address, refuses
    redirects and 401/OAuth discovery, and adds the header (Claude Code's own MCP client
    follows both with the header). A plugin with no header may be signed in with OAuth
    instead (#780, `src/plugins/oauth.ts`, `oauthStore.ts`, `ai_plugin_oauth`): only
    Settings starts discovery (`POST …/:name/oauth/start`, then the browser's
    `/api/v1/ai/plugin-oauth/authorize` and `/callback`, `routes/pluginOAuth.ts`), the
    tokens stay sealed (opened by the API once it lists area `plugins`, #5299 step 5) and never reach Claude Code, and the forwarder asks the plugin's `oauth` token
    source for each request's header; a refused refresh marks the grant for "Sign in again". Claude Code renames tool-name characters outside
    `[A-Za-z0-9_-]` to `_` (`harnessToolName`); only such names take a tier, and
    colliding tools are hidden. Unlisted plugin tools are `outward`.
  - Customer messages (#5660, spec `2026-11-08-order-notify-customer-design.md`):
    `src/orders/messaging.ts` `pluginMessaging` opens the backend setting
    `orders_customer_messaging`'s source (a remote plugin, or a package's http MCP server,
    both through the forwarder) and calls its tool; an `McpError` is `failed`, a timeout, a
    closed connection or another error is `unknown`. `order_notify_customer` (`read`) composes
    and answers `channel` and `send_ready`; `order_messages_list` (`read`) lists the order's
    messages, whose ids `resend_of` takes; `order_notify_customer_send` (`outward`) parks the
    exact message as `approved`, claims an `order_messages` row through the backend's
    `POST /api/v1/orders/{id}/messages` (`Idempotency-Key`, the call's `idempotencyKey`),
    sends, then posts the outcome. A replayed claim sends again only when the binding maps an
    `idempotency_key` argument; otherwise a fresh `sending` row is answered as in flight (no
    outcome posted) and one that reads `unknown` is recorded `unknown`, never resent by itself.
    A template the storefront mails to the order itself (the check's `storefront_mail`) is
    refused `storefront_mail` before it parks unless `also_when_storefront_mails` is set (the
    approval line then says so), and a claim that comes back `skipped` sends nothing.
    `routes/messagingBinding.ts` checks a binding for Settings → Orders and sends a test
    message (a person's session only, kept in the audit log as `messaging.test_send`).
    `routes/orderMessages.ts` is the order page's "Send again": a preview answers the message
    composed now and its digest, and the send takes only the template, `resend_of`,
    `also_when_storefront_mails` and the digest, composes again and refuses `message_changed`
    on a difference (a person's session only; `tools/orders.ts` `previewPersonSend`,
    `personSend`). Both refuse `storefront_mail` without the flag, which the page sends once
    the person says yes to "Send it here too?" or presses "Send here too"; the send asks the
    check under its own key first, so a retry of a send already claimed is answered from that
    claim and refused nothing, `message_changed` included (#5978). The claimed row is the record:
    an ended one is answered with no plugin, binding or contact; one with no outcome is sent
    again only when the binding maps `idempotency_key`, with the row's own recipients and text,
    and needs the binding (none is `no_messaging_plugin`) but no contact.
  - The storefront (#6131, design 2026-11-13 §6): `tools/storefront.ts`
    `storefront_listing_draft` (`write`: a listing from an output or a model, unpublished,
    with the Storefront page's smart defaults in `tools/storefrontDraft.ts`, a copy of the
    frontend's `listingDraft.ts` tested against `test-fixtures/storefront/sell-draft-vectors.json`;
    a colour the customer can choose is taken out of the title) and
    `storefront_listing_publish` (`outward`). The listings' other routes stay the page's.
  - Plugin packages (#297; skills, agents, hooks from git or a marketplace entry) live
    in `ai_plugin_packages` (`src/plugins/packages/`, routes
    `src/routes/pluginPackages.ts`). Postgres holds the pin (commit + content hash);
    `<state dir>/plugins/` is only a cache, re-hashed before every load and
    re-fetched from the pin. Install stores the pin unapproved; approving needs the
    exact commit and hash. `vet.ts` adds rules on top of `harness/plugins.ts`. Tests
    use local git repos (`test/support/gitRepo.ts`). Their MCP servers' http ones go
    through the forwarder (`plugins/packages/mcp.ts`), and on Temporal each call is a
    `turn:<server>__<tool>` ToolCall.
  - Claude Code's own built-ins (plan 2026-10-13 Step 9.6): every session offers `Read`,
    `Glob`, `Grep` and the task list (`TaskCreate`, `TaskGet`, `TaskList`, `TaskUpdate`) at
    `read`, and `Write`, `Edit` and `NotebookEdit` at `write`, native in its Claude Code
    process, each an item of the access source `builtin:claude-code`
    (`src/access/nativeBuiltins.ts`; `resolver.ts` `native`, `live.ts`). Their paths are
    confined to the session's working directory by the seam (`src/harness/confine.ts`, an
    `InputGuard` in both canUseTool and the PreToolUse hook: `..`, absolute paths and links
    out are refused, and so is any path, Glob pattern or Grep glob starting with `~`, which
    Claude Code expands to HOME after the seam for Glob and Grep, measured on 2.1.310; with
    no working directory the file tools are not offered). `Read` and `Grep` may also reach the
    session's own `tool-results` directory in the Claude Code config dir, where Claude Code
    saves an oversized tool result (#5542, `projectDirName`), and, in a resumed turn, the same
    directory under the SDK's resume config dir `<os.tmpdir()>/claude-resume-<randomUUID>`
    (#6117, read from SDK 0.3.314 `sdk.mjs`; the rest of that dir, its transcript and copied
    credentials included, stays refused). `Bash` and `PowerShell` are `off`: Claude Code's sandbox cannot run in the
    worker pod (no bwrap, and `unshare --mount` is refused under AppArmor; measured
    2026-11-01). Where the deployment sets `SCADBUDDY_SANDBOX_*` (all five or none, `config.ts`
    `sandbox`), every turn gets the session's own `Bash` and `PowerShell` instead
    (`mcp__builtin__Bash`, `builtins/sandboxShell.ts`, `outward` by default under the same two
    items), each command run in the session's sandbox pod (#5864, `src/sandbox/`: `pods.ts` makes
    one pod per session through the Kubernetes API and sweeps the ended ones, `client.ts` syncs
    the session directory in and out around each command, `server.ts` is the pod's exec server
    and `egressProxy.ts` the pods' CONNECT proxy, both the Dockerfile's `sandbox` target, which
    carries only Node's built-ins; `docs/ai/security.md`, "Bash and PowerShell in a sandbox
    pod"). Tests run the exec server in-process (`test/support/sandbox.ts`). An allowed package's `builtinTools`
    only switch on one that is `off` by default, at its package tier (`outward` by default),
    never one a person switched off (the source, or the item's row); they no longer decide
    what exists. The task list is `$CLAUDE_CONFIG_DIR/tasks/<session id>/` (measured
    on SDK 0.3.310), linked to the session directory's `_claude/tasks/`
    (`stateDirs.ts` `linkTaskList`), so it moves with the workspace.
    **Breaking for package authors:** the `builtin__<Name>` file and task tools of the
    retired durable turn, and their `/workspace` paths, are gone; a skill names Claude
    Code's own `Read`, `Write` and the rest, with paths in the session directory (its cwd).
    The agent still provides `WebFetch` and `WebSearch` (egress-checked, decision Q2) and the
    background ones in place of Claude Code's (`src/builtins/server.ts`, in-process
    `builtin` server, `mcp__builtin__<Name>`), in every turn, not only one whose package names
    them (#2726 R2): each is an item of `builtin:claude-code` too (`nativeBuiltins.ts`
    `SESSION_BUILTINS`, `turnBuiltins`), `WebFetch` and `WebSearch` at `outward`, `CronList` at
    `read`, the rest at `write`; a session a flow run started or forked, a Workflow agent's chat,
    a session one of those started or forked through `sessions_*`, and a fork of any of them
    (`ai_sessions.background_tools` false, `ClaimedSession.backgroundTools`) get no background one;
    nor does a turn a flow run, or such a session, sent (`ai_sessions.turn_flow`), nor any turn
    on an agent with no `deps.background`. A
    `Monitor`'s `ws` source is `outward`, and its `command` source runs a shell where Bash runs,
    so a turn gives it a command queue only while `Bash` is on, at Bash's tier if stricter
    (`monitorAccess`, `sessionTierOf`). `CronCreate`, `CronDelete`,
    `CronList`, `ScheduleWakeup`, `Monitor`, `Workflow` and `TaskStop` are the session's own
    (`src/background/`: `ai_background_tasks`; a recurring cron is a Temporal Schedule
    `bgtask-<session>-<id>` whose `BackgroundFire` sends the prompt when the session is idle
    (`background/temporal.ts`, plan Step 9.8, its older jobs converted at start by
    `convertCronTasks`), and a wakeup or a one-shot cron one `BackgroundTask` workflow on
    `agent-tools`; a Monitor's command
    runs on the replica that ran the turn, its own queue, and never where a sandbox is set up
    (#5864: its description there offers `ws` only), its events in
    `ai_background_events`; a Workflow's
    script runs in QuickJS (`workflowRunner.ts`, never Node's `vm`), each `agent()` a child chat
    (`workflowAgents.ts`, a chat with no background built-in of its own), its results journalled in `ai_workflow_calls`; every turn that has them
    gets them as the in-process `builtin` server in place of Claude Code's, which would end
    with its process; the panel's list and cancel are `routes/backgroundTasks.ts`).
  - Tests never call Anthropic: `test/support/fakeAnthropic.ts` is a local Messages API
    (streaming SSE) that the real SDK and bundled CLI are pointed at as a gateway
    (`test/run.test.ts`). Postgres tests (`test/pg.test.ts`) skip unless
    `SCADBUDDY_TEST_DATABASE_URL` is set, as in the backend; the `agent` CI job sets it. The design is
  `docs/superpowers/specs/2026-09-27-ai-integration-design.md` (issue #250).
  The 09-22 design spec's "No database" statement (`2026-09-22-scadbuddy-design.md`
  §4, "Architecture") describes the backend container; the
  AI spec (#250, PR #303) adds Postgres (#241) for the system as a whole, and the
  09-27 template-pipelines spec makes Postgres and Temporal required.

## See also

- The vector fixtures are opened by `agent-durable/CLAUDE.md`. Generated API files: root CLAUDE.md.
- Backend contracts (the flows tools endpoint, orders, activity): `backend/CLAUDE.md`. Plugin source: `plugins/CLAUDE.md`.
- Migrations rule: root CLAUDE.md, "Migrations (#491)".
