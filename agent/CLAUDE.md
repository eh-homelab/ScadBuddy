# Agent service — notes for agents

These notes cover `agent/`; paths are repo-relative; the root CLAUDE.md has the repo-wide rules (migrations, CI, PR conventions, generated API files).

## Database access

The rule is in the root CLAUDE.md, "Database access (#5295)". The agent sidecar and the
agent workers have no SQL client and read no KEK (#5299 step 12): no `agent/src` file
loads `postgres` or another Postgres driver (`postgres` is a devDependency only), and every
store is its `Crud…` client over the API's `/api/v1/internal/crud/agent/*` routes (class
`crud.agent`, #5330). `test/retiredTables.test.ts` is the guard: it fails on a driver import
in `src` and on SQL that names a backend table the API took back. The tests keep SQL
implementations of the stores as test-only fakes (`test/support/sql/`, over
`test/support/database.ts`). The API owns the `ai_*` tables, applies their migrations at its
start (#5299 D6; root CLAUDE.md "Migrations (#491)"), and revokes the agent's old role
`scadbuddy_agent` at every start (`backend/scadbuddy/auth/revoke.py` `RETIRED_ROLES`, PR
#6906). eh-homelab/clusters#3965 took `SCADBUDDY_DATABASE_URL` and the KEK mount out of the sidecar
and the agent-worker Deployment and dropped the role. Claude Code, and any plugin package
approved with `allow_refused`, runs in the agent's container, which holds no database URL and
no KEK; the sidecar still shares the API's pod, whose API container holds both (#5296;
`docs/ai/security.md`, "Plugin packages").

`src/config.ts` ignores `SCADBUDDY_DATABASE_URL`, `SCADBUDDY_SECRET_KEY_FILE` and
`SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` (`IGNORED_VARS`, `config.ignored`) with one warning:
`agent: ignores <names>: the agent has no database and no key-encryption key (#5299 step 12)`.
Never add a backend table read, an `ai_*` grant, a trigger on another service's table or an SQL
client here; add a CRUD route that requires a service credential instead. Its 8081 routes get a
service-credential guard in #5297 part 2; prod never runs `enforce` (the owner's decision of
2026-11-17). Claude Code, and any plugin package approved with `allow_refused`, still run in a
pod that holds the `agent` credential and the Temporal client (D10; `docs/ai/security.md`,
"Plugin packages").

The service credential (`src/api/serviceCredential.ts`, wired in `main.ts`) is required:
`SCADBUDDY_SERVICE_TOKEN_FILE` or `SCADBUDDY_SERVICE_BOOTSTRAP_FILE` (neither, or both, is a
`ConfigError`). `ExchangeCredential` exchanges the proof at `POST /api/v1/auth/service-token`
for a 15-minute credential and renews it at half-life and after a 401; it reads no view (a 404
is a failed exchange, #6501). A failed exchange is tried again after 5 s doubling to 60 s; a
refused connection (`src/api/backendUp.ts` `backendNotUp`, #5868) is tried again at the next
call and logged once as "waiting for the backend". With no token held, a backend call
(`withServiceCredential`, `serviceCredentialMiddleware`) waits for the next exchange when it ends
within `CREDENTIAL_WAIT_MS` (15 s, counted from the first answer; #6647), so a call can take that
much longer in a back-off; the call's own signal ends the wait.

Readiness: `src/api/crudAreas.ts` `CrudAreas` reads `GET …/crud/agent/areas` from the start
(back-off 1 s doubling to 30 s) until every wanted area is listed, and until then the agent is
not ready: `/mcp` and the data routes answer 503. It is not ready either until the API has
released the input-hash key (`InputHashKey.isReleased`, `dataHealthOf`'s `keyReleased`), but only
`/mcp`, health, the workers and the pending-input respond (an answer is audited with its hash,
503 with `retry`; the chat socket's `question.answer` gets an `unavailable` error to send again,
`routes/chat.ts` `errorEvent`) wait for the key, not the other data routes (`AgentData.areasReady`,
`routes/module.ts` `ready`), so Settings still answers on an API with no KEK: a tool
call hashes its input for the audit log after it ran, so no Temporal worker polls before then
(`AgentWorker`'s `before`), the worker's `/readyz` names the missing areas or the key, and `ai` says
`unavailable (waiting for the API's input-hash key)` once the API holds its KEK. `/healthz`'s `database` is `ok` once the API
lists and answers the areas (`src/api/dataHealth.ts` `dataHealthOf`, `CrudAreas.answers`),
else `unreachable`, and `ai` then says `unavailable (the API does not answer the agent's CRUD
areas)` (`src/aiStatus.ts`). There is no migration wait: the tests' ledger wait is
`test/support/schema.ts`, and their file list `test/support/expected-migrations.json`. Backend
`api/crud_agent_start.py` (#6501; test `test/crudStart.test.ts`) serves area `auth-sessions`.
A route's answer counts only with `X-ScadBuddy-Crud: 1`; any other failure takes the read's
error path (a lookup failure the activity retries, a 500, a resync). A GET, or a call marked
`idempotent`, whose connection drops (`crudRead.ts` `TRANSIENT_KINDS`, not a refusal, which its caller
waits out) is sent again after `RETRY_DELAYS_MS`; a write never is. A turn that still fails on one is placed again
(`BackendUnreachable`, #6994). Each area, its client
and its backend module:

- `auth-sessions`: a signed-in person's session (`src/auth/browserSession.ts`,
  `CrudSessionLookup`; only the cookie's hash travels, at most `LOOKUPS_IN_FLIGHT` at once, the
  rest waiting up to `LOOKUP_WAIT_MS`, then failing as no session).
- `keys` (backend `api/crud_agent_keys.py`): the approvals' input-hash key (`GET …/agent/keys/input-hash`,
  `src/api/agentKeys.ts` `InputHashKey`) and the one-off fetch grants (`POST …/agent/fetch-grants`,
  `CrudFetchGrants`). Both keys are derived from the API's KEK. The input-hash key is
  released once and kept for the process's life (every replica hashes with one key); the
  grants' key stays on the API.
- `flows` (#5299 D7, `api/crud_agent.py`): a run's starter, its decision checks, a grant in force, a run's queue and
  steps, the waiting runs and the parent-hop approvals (`src/flows/backendReads.ts`
  `CrudFlowRunReads` over `src/api/crudRead.ts` `crudRead`, used by `temporal/flowRuns.ts`,
  `flows/queue.ts`, `flows/escalations.ts` and `temporal/toolActivities.ts`). The API revokes
  everything the agent's old role `scadbuddy_agent` held, these tables among it, at every
  start (`revoke.py` `RETIRED_ROLES`).
- `settings`, `credentials`, `payload-keys` (#5299 step 4, D5, #5342; `SECRET_AREAS`):
  `CrudSettings` (a compare-and-set sends what its check read as `expect`; the API sends the
  warm-evict and runtime NOTIFYs in the write's transaction); `CrudCredentials` (the API seals
  and opens with its KEK, the routes and the turn's pool read its key through the repo's
  `opener`, and a turn's credentials come from `…/credentials/reveal`, audited per call);
  `src/temporal/payloadCodec.ts` `CrudPayloadKeys` (a subject's data key, made under the
  subject's lock and never for a forgotten subject, audited as kind `payload_key` once per
  subject in 10 minutes, cached 30 s here).
- `forget` (#5299 step 6, `api/crud_agent_forget.py`): `src/sessions/forget.ts`
  `CrudSubjectKeys.forget` (under the subject's lock, the tombstone, a flow run's images and the
  key, a session's forks first taking their ranges of its transcript, events and images,
  backend `transcripts.py` `copy_fork_ranges`; an API with no KEK whose forks take transcript
  entries refuses it `forks-take-ranges`, and the forget throws `ForksNeedKeys` and deletes
  nothing more), a browser call's key and the hourly key sweep, and a flow run's images
  (`src/temporal/flowBlobs.ts` `CrudFlowBlobs`, multipart bytes; the tombstone check and the
  cap in the API's transaction).
- `workspace` (#5299 step 7, `api/crud_agent_workspace.py`): `src/workspace/crudStore.ts`
  `CrudSessionFiles`. The API seals, stores and opens each file and re-wraps principals' keys;
  a write's row, blob hold and `workspace.file` events are one transaction there, and the
  reconcile one transaction under `RECONCILE_LOCK`. This process sends and gets plaintext only.
- `session-blobs`, `attachments`, `http-bodies` (#5299 step 7, `api/crud_agent_blobs.py`):
  a session's images and PDFs (`src/sessions/crudBlobs.ts` `CrudSessionBlobs`; the sessions
  route serves a PDF with `Content-Disposition: attachment`; a durable tool call's images
  replaced whole; a placement's write fenced by `TurnFence`, checked `FOR SHARE`; a queued
  message's images deleted once it leaves the queue unrun or is refused, only what its own
  write made, #6413), the staged attachments (`CrudAttachmentStore`) and the saved
  http_request bodies (`CrudHttpBodies`, read by range). Bytes are multipart parts and answer
  bodies.
- `background-tasks`, `operations` (#5299 step 8, `api/crud_agent_tasks.py`):
  `CrudBackgroundTasks` (tasks, a Monitor's events, a Workflow's journal and child chats) and
  `CrudOperations` (`ai_operations`; the API writes the activity row in its transaction).
  Bodies go through `crudRead.ts` `wellFormed`: the API refuses a lone surrogate.
- `sessions`, `session-resources`, `transcripts`, `session-events`, `session-order-costs`
  (#5299 step 9, `api/crud_agent_sessions.py`, `_transcripts.py`, `_events.py`,
  `_order_costs.py`): `CrudSessionRows` (one read of the row answers the record, a turn's claim
  and `poolRemainingUsd`; an insert's owner limit and a fork's tombstone check run under their
  locks), `CrudResourceRows`, `src/sessions/crudTranscripts.ts` `CrudTranscriptRows` (sealed and
  opened by the API under the session's data key with the agent's AAD, so entries travel as
  JSON text; an append is one route per batch, `appendBatches`, at most 1000 entries and about
  30 MiB, with the tombstone check and the fence `AppendFence` `{ attempt, turnId }`; a fork's
  transcript, head, tail check `moved` and user-entry check are one snapshot each),
  `src/sessions/crudEvents.ts` `CrudEventRows` (an append; a turn's append fenced on its
  placement, `appendFenced`; the reads, a fork's through its chain in one snapshot; `turnEvents`;
  `forkLog` under the parent's payload-key lock). `TranscriptStore` (`src/sessions/store.ts`) keeps the per-session
  append order and `EventLog` its followers, wakes and 1 s polls (D4). A session id goes into a
  path only as a lowercase UUID, encoded (`rows.ts` `sessionPath`).
- `session-turns`, `session-edits` (#6449, `api/crud_agent_turns.py`, `_edits.py`):
  `CrudTurnRows` is one route per write, its events in its transaction and its fence checked
  there: a send's claim and give-back, the queueable read, the turn's state read, a
  placement's claim (the only writer of `turn_attempt`), its spend (each write under a key of
  its own, sent again under that key when its answer was lost), the turn's end only while the
  row runs that turn (`durable_turn_id`) with the queue's head claimed in the same
  transaction, and the running sweep. A lost claim's answer is read back and given back
  (`durable.ts` `#claim`). `CrudEditRows`: under the row's lock and only while the principal
  owns it, the rename, done and archive, the warm eviction, an archive's `done` summaries; the
  edit is sent untold first, and a newly done session not `told` is refused `untold` only
  after every other check passed, so a refusal, a 503 or a lost answer ends no workflow. An
  archive is not told first: once it is written, its workflow is sent `end`, best-effort
  (#7031), and the first send after an unarchive starts a new run (`durable.ts`,
  `ALLOW_DUPLICATE`).
- `gate`, `pending-input`, `audit`, `approvals`, `questions`, `escalations` (#5299 step 10,
  `GATE_AREAS`, `api/crud_agent_gate.py` and siblings): `src/gate/crudStore.ts` `CrudGate`
  (`openInput`, `resolveInput`, `denyInput`, `decideInput`, `readInput`, `grantInput`; the API
  writes the rows, events, status, audit row and `scadbuddy_input` NOTIFY in one transaction
  and answers the events, `logged`; `decideInput` runs the validator there, and with `live` the
  API first answers the row's run, the store asks Temporal, then asks again naming it; a
  request id goes in the body or the query, never the path); `src/gate/crudPending.ts`
  `CrudPending` (the projection's reads, the orphan sweep, a Stop's cancels, `TurnHost`'s
  held-call read and span mark, fenced on `turn_attempt` and `durable_turn_id`);
  `src/audit/crudAudit.ts` (a refused or unanswered write goes to `onError`);
  `src/approvals/crudApprovals.ts` (access checks here first; the decision's audit row in its
  transaction); `src/gate/crudQuestions.ts` (the attention count only while this process holds
  the lease `attention-rate`, 15 s, refused on a lost lease or an API error; a turn's `done`
  post lands only while the turn holds the session, with its cap, events and audit rows in one
  transaction); `src/flows/crudEscalations.ts` (who may answer is checked here; a delivery's
  claim is committed before its turn starts and handed back, `unclaim`, when it does not).
- `publish`, `session-reads` (#6501, `api/crud_agent_publish.py`): `CrudPublisher` (a
  `session.*` bus event, a handoff's warm eviction) and `CrudSessionReads` (a session's open
  calls, a child's reasoning).
- `session-sweeps`, `forget-rows` (#6501, `api/crud_agent_sweeps.py`):
  `src/sessions/presence.ts` `CrudSessionPresence` (pages of `LIVE_IDS_MAX`; a failed read
  throws, so a sweep removes nothing it could not check) and `forget.ts` `CrudForgetRows`
  (refused 409 `not-forgotten` with no tombstone).
- The small stores (#5299 step 5, `src/api/crudStores.ts` `STORE_AREAS`,
  `api/crud_agent_stores.py`, `_notify.py`): `activity-kinds`, `browser-origins`
  (`CrudSessionOrigins`), `bridge-tabs` (`CrudSessionTabStore`, `CrudTabOwnerStore`,
  `CrudTabImageStore`), `pairings` (only the code's hash leaves this process), `mcp-tokens`
  (a mint and a verify send the hash only) and `notify` (`CrudPushStore`, `CrudChannelRows`: the
  API seals and opens the VAPID key, the subscriptions' keys and the channels' tokens). Test
  `test/crudStores.test.ts`.
- The plugin stores (#5299 step 5, `api/crud_agent_access.py`, `_plugins.py`, `_packages.py`):
  `access` (`CrudAccessRows`; `CrudToolListings`, whose connection test holds the lease
  `tool-listing:<source>`, `crudStores.ts` `CrudLeases`, renewed every 30 s, and stores its
  listing only while the lease holds, else 409 `lease-lost`), `plugin-sessions`, `plugins`
  (`CrudPluginStore`, `CrudOAuthRows`: the API seals and opens the header secrets and the
  sign-ins; a refresh holds the lease `plugin-oauth:<plugin>`) and `plugin-packages`;
  `plugin-feeds` and `hosted-plugins` (`src/plugins/feed.ts` `CrudPluginFeeds`, #6933): a
  marketplace feed's packages and a hosted version's zip, which `src/routes/pluginFeeds.ts`
  serves to outside Claude Code at `/api/v1/ai/plugin-feeds/{feed}/marketplace.json` and
  `…/plugins/{name}/versions/{N}/{zip_sha256}.zip` behind /mcp's gates and auth (`mcpAuthResolver`,
  `mcpTransportProblem`, `authenticate`), only approved pins, each zip URL on the origin the
  `marketplace.json` request came on (`effectiveRequest`; Claude Code sends the marketplace's
  `headers` only to that origin), a zip request reading only its own package;
  `GET /api/v1/ai/plugin-feeds/{feed}` is Settings' read (its `marketplace_url` is on the request's origin too); /mcp and the feed's three bearer-checked reads (marketplace.json, versions, archives) are reached on the LAN host and on the public URL, where the ingress does no sign-in for them (clusters#4017, #4368); Settings' feed routes take no token and stay behind the ingress's sign-in. #6936: a commit pin of any source
  (a bare `marketplace.json` URL's git entry too) is listed with its `sha`; a bytes pin (archive,
  npm) as an `archive` at `…/plugins/{name}/archives/{sha256}.zip` (the served zip's sha256),
  served from its mirror (`package-mirrors`): a zip pin as its exact bytes, a tarball as a
  stored zip of its approved files under the plugin root (`plugins/packages/zip.ts`, the same
  bytes on every replica). `ArchiveMaker` keeps each digest and the last zips (64 MiB) per
  process, runs at most `ARCHIVE_BUILDS_AT_ONCE` (3) mirror reads at once for feed reads and
  downloads, and keeps a failed one for `ARCHIVE_FAILURE_KEPT_MS` (60 s, the feed's `max-age`;
  a missing mirror is not kept); `PUT`/`DELETE …/{feed}/packages/{name}` are Settings' UI-only
  writes, 503 `not-served` while the API does not serve them yet (a rolling deploy).
- `mcp-relay` (#5299 step 11, `api/crud_agent_mcp.py`): `src/mcp/relayRows.ts` `CrudRelayRows`
  (the /mcp session directory and messages; a message's row and its `scadbuddy_mcp` NOTIFY
  commit in one transaction). `test/mcpSessions.pg.test.ts` runs the relay against the
  backend's port.
- `changes` (#5299 D4, `api/crud_agent_changes.py`, `agent_changes/hub.py`):
  `src/events/changeStream.ts` `ChangeStream`, the agent's one source of NOTIFYs, a server-sent
  event stream `GET …/agent/changes` carrying the six channels (`scadbuddy_events`, `_input`,
  `_pending`, `_mcp`, `_warm_evict`, `_settings`). A dropped stream is opened again with
  back-off and `Last-Event-ID`; the API replays its `events` log after it, or says `resync`,
  and `onReconnect` re-reads what had no log row. It needs no area wait: the 1 s polls cover a
  gap (D4). Tests hear their own NOTIFYs through `test/support/pgListener.ts`.

A session's successful `order_item_revision_add` (or `_after_acceptance`) is a
`session-order-costs` row (`src/sessions/orderCosts.ts` `OrderCosts`, a `TouchedSink` beside
`SessionResources`); each turn's end (`temporal/session/finishActivity.ts`, a retry too, not
awaited) reports the session's `cost_usd` since its last report, split equally between its
orders still to report, to `POST …/crud/agent/orders/{id}/assistant-cost` (5 s each, 15 s in
all), one report per session at a time across replicas under the lease
`order-cost:<session id>`, renewed while the report runs; each write is refused `lease-lost`
once its token no longer holds it. `sweep` at start and hourly reports what a session's last
turn left, 20 sessions at most, oldest `tried_at` first; a share under one minor unit is
closed out with nothing carried once its session is over (`done`, `failed`, or `idle` with no
activity for `IDLE_OVER_AFTER`, an hour). `record` waits at most `RECORD_WAIT_MS` (#1076).
Only the route's own 400 or 422, 404 (the order gone) or 409 (no rate reaches the currency, a
visitor's unsubmitted cart), or an order id of `.` or `..` (refused before it is sent, #7221)
drops that order's report. Anything else (401 or 403 with no live
credential, 429, a 5xx, a transport failure, a non-CRUD answer) keeps it with the share it
sent (`attempted_usd`), sent again at the next turn's end, so a lost answer loses no spend.

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
mode branch in `agent/src`, `frontend/src` or `backend/scadbuddy` (`.ts`, `.tsx`, `.py`), a name of the session mode that is gone (its
`retired` list, #7323; a `*.test.ts(x)` file may name one), the SDK's `query` imported outside `harness/run.ts` and
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
  reads only infrastructure variables (`ENV_VARS`): `SCADBUDDY_BACKEND_URL`, the
  service-credential proof (`SCADBUDDY_SERVICE_TOKEN_FILE` or `_BOOTSTRAP_FILE`, required),
  `SCADBUDDY_PUBLIC_URL` (the same variable the backend reads; the one origin allowed to
  write), `SCADBUDDY_AGENT_TRUSTED_PROXIES` (CIDRs whose `X-Forwarded-*` are believed) and
  the backend's `SCADBUDDY_TEMPORAL_ADDRESS`, `_NAMESPACE` and `_SEARCH_ATTRIBUTES`; it
  ignores `IGNORED_VARS` with a warning ("Database access"). No AI env vars; AI settings
  are the API's, through area `settings`.
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
  `RESTARTED_PROMPT` once its message is in the transcript (§5.5); before that, each call left
  with no result gets one in the transcript (`sessions/restartedCalls.ts`, #7874: a ToolCall's own,
  else "the service restarted while this call ran"), and a shutdown's cut drops what Claude Code
  writes after its interrupt (`warm.ts` `seal`) and leaves its ToolCalls running. Every tool call
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
  `/api/v1/ai/sessions` in `chat.ts` and `sessions.ts`, backed by `SessionManager`, and
  `invoiceImport.ts` (`POST /api/v1/ai/filament/invoice-import`, #6267: it starts a session with no
  prompt, claims the invoice's `import_run_id` through the backend's conditional PATCH, then sends the
  import prompt, `src/filament/invoiceImportPrompt.ts`, with the invoice file attached as an image or a
  PDF document block, built in `src/sessions/images.ts` `userPrompt`; the tools
  `filament_bambu_colour` and `filament_invoice_candidates` are in `src/tools/filamentPurchasing.ts`). A
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
  - Data: the agent has no database ("Database access" above); the API owns the `ai_*`
    tables and applies their migrations (#5299 D6). The tests make their schemas with
    `test/support/migrations.ts` (`test/support/postgres.ts` `migrateSchema`), and
    `test/schema.test.ts` checks the list against the API's runner. Secrets are
    envelope-encrypted by the API under its KEK; `src/secrets.ts` keeps the format the
    vectors pin (`test/fixtures/secret-vectors.json`); the AAD binds each value to its row and
    to the columns that say where it is sent (for the credential: `kind` and `base_url`).
    Comparable tokens are stored hashed instead.
  - Poller reads (#7306): every read of a task queue's pollers goes through
    `src/temporal/pollers.ts` `queuePollers`; tests use `test/support/temporal.ts`
    `anyPoller` or `pollerIdentities`. ESLint's `no-restricted-syntax` refuses any
    reference to `describeTaskQueue` anywhere else, except an object literal's key (a test
    stub's) (`eslint.config.js`, `test/lintRules.test.ts`). DescribeTaskQueue reports one
    partition's pollers, and a worker can poll only the non-root ones, so it asks the root and
    `/_sys/<queue>/1..3`. `TASK_QUEUE_PARTITIONS` (4) must equal the server's
    `matching.numTaskqueueReadPartitions`: Temporal's default is
    `GlobalDefaultNumTaskQueuePartitions = 4`
    ([shared_constants.go](https://github.com/temporalio/temporal/blob/v1.32.1/common/dynamicconfig/shared_constants.go)),
    and prod's `scadbuddy-temporal-v2-dynamicconfig` does not set it (read 2026-11-24). A
    change to that setting changes the constant in the same PR. One failed or slow partition
    fails the whole read: the plugin sweep then skips its run, placement takes the turn on
    its own replica, and readiness answers "Temporal did not answer" (fails closed).
  - Temporal (#1055, spec 2026-10-01 §6.3): with `SCADBUDDY_TEMPORAL_ADDRESS`
    `src/temporal/worker.ts` runs one worker on `agent-tools`, a version of
    `scadbuddy-agent` with `agent-sessions` when the build has an id (#2726 Phase 11; the
    host queues stay unversioned, `agent/test/temporal.versioning.temporal.test.ts`):
    every `ALL_TOOLS` entry as an activity under its name (`toolActivities.ts`,
    `runToolWithOutcome` with `gate: 'workflow'`, only for a flow run's `flow-<id>` (`flowRuns.ts`,
    #1057: as the run's starter, in no session; a gated call needs the backend's
    `workflow_run_decisions` approval; no browser tools); a session's call is `tool:<name>` in its
    ToolCall workflow, and a bare one from any other workflow is refused `UnknownSession`, #7323), a turn's other tools (#2320, plan Step 9.3,
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
    the agent's commands, recorded in `ai_operations` (`src/operations/`, area `operations`, #5299 step 8). A change to
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
    attention limits (`questions.ts`, `doneSummary.ts`, `waiting.ts`; #5110 folded the
    old question stores in) and the orphan sweep (`sweep.ts`). The workflow's half is
    `temporal/session/gate.ts` (`SessionGate`) and `gateActivities.ts`. `routes/pendingInput.ts` serves
    both reads and `respond`. `input.requested` / `input.resolved` are logged and never
    sent on the chat socket (`routes/chat.ts` `sentToPanel`).
  - Durable payloads (#1056 phase 5c, spec §6.5): `src/temporal/payloadCodec.ts` seals
    every payload of a `session-<uuid>` / `flow-<uuid>` workflow under that subject's
    data key (`ai_payload_keys`, sealed under the API's KEK, which re-wraps them at its start
    on rotation). The agent-tools worker and the service's Temporal client carry it; the API
    releases each key (`CrudPayloadKeys`, area `payload-keys`, #5299 step 4, #5342); a call
    whose key the API does not release is `PayloadKeyUnavailable`. `src/sessions/forget.ts`
    `forgetSubject` (key row, then the workflow, then the rows, all through the API, above) is
    the one way a durable subject is deleted; operators run
    `node dist/forget-subject.js session-<uuid>`. A fork reads its parent's transcript
    through the cut (`ai_sessions.fork_parent_id`, `fork_cut`, plan Step 6.5), and its event
    log and images through `fork_event_cut` (#4212; its own `session.started` is seq 1, its own
    later events come after the cut), so forgetting a parent first copies each fork's range
    into it, the transcript under the fork's key (backend `agent_data/transcripts.py`
    `copy_fork_ranges`), and an API with no KEK refuses it when a fork takes transcript
    entries (`fork_cut` above 0; a fork of a closed DurableSession takes none). The API
    answers every read through the fork chain in one snapshot, stopping at the least cut below
    each ancestor (`LEAST(bound, cut)`: backend `agent_data/transcripts.py`, `events.py`,
    `api/crud_agent_blobs.py`); a new chain read must do the same. When it is made, a fork
    copies none of its parent's transcript, event or image rows; its workspace file rows are
    copied, naming the same bytes (`SessionFiles.copy`). A gated tool's activity runs only
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
    and its end write back by sha256 (`src/workspace/directory.ts`). The rows, the bytes and
    their holds are the API's (area `workspace`, above; backend `agent_data/workspace.py`): the
    bytes (#4789) are a blob of the content store, sealed under a per-file key wrapped under
    the session's data key, held in `blob_refs` as `(ai_session_file, <workspace>/<path>)`
    before the row that names them and released after it is gone, and the hourly reconcile
    (one replica at a time, `RECONCILE_LOCK`) releases what cascades, `forgetSubject` and
    crashes left and holds a row no hold names; no trigger.
    `test/workspaceBlobs.backend.e2e.test.ts` runs it against the real API on moto
    (`backend/tests/support/workspace_backend.py`). Only on `store_backend` local do the bytes
    stay in Postgres, one `ai_session_file_bytes` row per sha256 that every file with those
    bytes names, so a fork shares them (#4212); on any other backend a write that cannot be
    sealed and stored is refused `unavailable`, never kept in plaintext. Rows of
    `ai_session_file_bytes` are shared and swept: the API's writer locks them `FOR KEY SHARE`
    first, inserts only the missing ones and locks again, in a loop; `INSERT … ON CONFLICT DO
    NOTHING` takes no lock on the existing row, and the table's CHECK (sha256 of the bytes) runs
    before the conflict check (`ExecInsert`,
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
    tokens stay sealed (opened by the API, area `plugins`, #5299 step 5) and never reach Claude Code, and the forwarder asks the plugin's `oauth` token
    source for each request's header; a refused refresh marks the grant for "Sign in again". Claude Code renames tool-name characters outside
    `[A-Za-z0-9_-]` to `_` (`harnessToolName`); only such names take a tier, and
    colliding tools are hidden. Unlisted plugin tools are `outward`.
    A session's browser options (#7027, `ai_sessions.browser`) go to every remote plugin
    route of its turns as `X-ScadBuddy-Browser` (`forwarder.ts` `BROWSER_HEADER`, their JSON;
    none when the session has none): a classic turn's from `ClaimedSession.browser`, a durable
    turn's and its kept client's from the row its owner is read from (`RowSessionOwners`, into
    `TurnContext.browser`; a listing's from the session the turn holds), never a second read; a
    flow's never. Only the forwarder sets it; a client's own is dropped. The playwright-mcp
    wrapper (`workers/playwright-mcp/`) reads it when an MCP session starts; other plugins
    ignore it.
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
  - The inventory (#7067, `docs/features/inventory.md`): `tools/inventory.ts`, 18 `inventory_*` tools
    (kinds, items, offers and price points, lots, units, moves), `read` for the lists and gets and
    `write` for the rest, `inventory_lot_recost` included (Ruling 8); each answer carries `ui_path`
    from `tools/inventoryLinks.ts`, whose vectors `test-fixtures/inventory/ui-paths.json` the frontend's
    `lib/inventoryLinks.ts` shares. `part_list` and `part_stock_add` (`tools/orders.ts`) are aliases of
    `inventory_item_list` and `inventory_lot_receive`, and go after one release. Tests:
    `test/inventory.test.ts`, `test/inventoryLinks.test.ts`.
  - The storefront (#6131, design 2026-11-13 §6): `tools/storefront.ts`
    `storefront_listing_draft` (`write`: a listing from an output or a model, unpublished,
    with the Storefront page's smart defaults in `tools/storefrontDraft.ts`, a copy of the
    frontend's `listingDraft.ts` tested against `test-fixtures/storefront/sell-draft-vectors.json`;
    a colour the customer can choose is taken out of the title; or, with `library_file_id`, a
    file listing, #6710 spec 2026-11-21 §11.3: the body the editor saves for a new listing of that
    file, `fileTitle`, `fileColours` over `declaredSlots`, `platePictures`, which the vectors' file
    case pins, but `materials`, which is the offered filaments' (`draftMaterials`) where the page
    leaves the shop's default; for a pipeline template, #5701 spec 2026-11-17 §9, an output's own
    inputs and their version go with the draft, `soldDesign`, which the vectors' `source` cases
    pin, a parameter the pipeline sets is neither offered nor locked, and a draft from a model
    sets no inputs and says in `notes` to set up the design in the editor, as a draft from an
    output that holds none of them does; a version whose pipeline declares no inputs says it
    cannot be listed instead, #7879) and
    `storefront_listing_publish` (`outward`). The listings' other routes stay the page's.
  - Plugin packages (#297; skills, agents, hooks from git or a marketplace entry, or,
    #6935, a bare `marketplace.json` URL's entry, an archive or an npm package) live
    in `ai_plugin_packages` (`src/plugins/packages/`, routes
    `src/routes/pluginPackages.ts`). Postgres holds the pin (a commit, or for an archive
    or npm package the file's sha256, `pin_kind` `bytes`, plus the content hash);
    `<state dir>/plugins/` is only a cache, re-hashed before every load and
    rebuilt from the pin's mirror in the API's area `package-mirrors` (`mirror.ts`), never
    upstream: a bytes pin's file, or a commit pin's files as a deterministic zip (#7907,
    `zip.ts`; `mirror_sha256`, or `pending_mirror_sha256` for a re-pin), used only when
    its files hash to the pin. A load (turn, warm-up, file read, header secrets) never runs
    git (the owner's decision of 2026-11-26 on #7907): with no usable mirror it leaves the
    package out (`MirrorNotReadyError`: "its mirror is not ready yet", "its mirror failed its
    check (…)" or "its mirror could not be read (…)") and starts the
    mirror job (`mirrorJob.ts`): on Temporal the `PackageMirror` workflow on `agent-tools`
    (`temporal/packageMirror.ts`, id `package-mirror-<name>-<commit>-<16 of the hash>`,
    `USE_EXISTING` + `ALLOW_DUPLICATE`, so a run open is joined and a finished one can run
    again), else in this process. Its activity reads the pin from the row, keeps a recorded
    mirror that reads back, else fetches with `MIRROR_FETCH_TIMEOUT_MS` (10 min), checks
    the hash, and writes and records the zip (`makeMirror`); a hash mismatch, or a zip
    past `MIRROR_MAX_BYTES` (24 MiB, the API's `MAX_MIRROR_BYTES`), is `PackageMirrorRefused`,
    never retried. The zip is unpacked with the content hash's caps (`COMMIT_MIRROR_LIMITS`:
    2,000 files, 5 MiB each, 20 MiB in all), not a download's (`unpack.ts`
    `UNPACK_LIMITS`), so every package that can be pinned loads. Install and re-pin put
    and record their own zip (`recordPreparedMirror`) and start the job only when that
    failed; approving a commit pin with no mirror starts it. Downloads go through
    the egress check at every hop (`download.ts`), archives are unpacked by
    `unpack.ts` (no links, devices or paths out). Install stores the pin unapproved;
    approving needs the exact commit (or `bytes_sha256`, or `version`) and hash. `vet.ts` adds rules
    on top of `harness/plugins.ts`. Tests use local git repos (`test/support/gitRepo.ts`),
    and archives built byte by byte (`test/support/archives.ts`) on a local HTTP server
    (`test/pluginPackagesBytes.test.ts`). A hosted plugin (#6934, source `{ kind: 'hosted',
    name }`, `pin_kind` `version`) is read over the API's area `hosted-plugins`
    (`hosted.ts` `CrudHostedPlugins`): its zip checked against the version's sha256 and its
    files against the content hash, then vetted; an install of a hosted source re-pins the
    installed hosted package instead (`operations.ts` `offerHosted`), and its view's
    `not_offered` names a published version no pin names (`test/pluginPackagesHosted.test.ts`). The
    assistant's `hosted_plugin_*` tools (#6938, `src/tools/hostedPlugins.ts`) write a draft over the
    API's `api` routes; `hosted_plugin_publish` (`outward`) publishes, then offers in-process through
    `ToolServices.hostedPackages` (`offerHosted`), never the UI-guarded routes. A package's http MCP servers go
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
    runs on the replica that ran the turn, its own queue, or, where a sandbox is set up, in the
    session's pod as a background slot (`sandbox/slots.ts`) that the activity on that queue
    follows (`background/sandboxSource.ts`, #5916), as does a sandboxed Bash or PowerShell with
    `run_in_background` (a `monitor` task with `spec.background` `shell`, #7310), its events in
    `ai_background_events`; a Workflow's
    script runs in QuickJS (`workflowRunner.ts`, never Node's `vm`), each `agent()` a child chat
    (`workflowAgents.ts`, a chat with no background built-in of its own), its results journalled in `ai_workflow_calls`; every turn that has them
    gets them as the in-process `builtin` server in place of Claude Code's, which would end
    with its process; the panel's list and cancel are `routes/backgroundTasks.ts`).
  - Tests never call Anthropic: `test/support/fakeAnthropic.ts` is a local Messages API
    (streaming SSE) that the real SDK and bundled CLI are pointed at as a gateway
    (`test/run.test.ts`). Postgres tests (`test/pg.test.ts`, over the test-only SQL fakes in
    `test/support/sql/`) skip unless `SCADBUDDY_TEST_DATABASE_URL` is set, as in the backend;
    the `agent` CI job sets it. The design is
  `docs/superpowers/specs/2026-09-27-ai-integration-design.md` (issue #250).
  The 09-22 design spec's "No database" statement (`2026-09-22-scadbuddy-design.md`
  §4, "Architecture") describes the backend container; the
  AI spec (#250, PR #303) adds Postgres (#241) for the system as a whole, and the
  09-27 template-pipelines spec makes Postgres and Temporal required.

## See also

- The vector fixtures are opened by `agent-durable/CLAUDE.md`. Generated API files: root CLAUDE.md.
- Backend contracts (the flows tools endpoint, orders, activity): `backend/CLAUDE.md`. Plugin source: `plugins/CLAUDE.md`.
- Migrations rule: root CLAUDE.md, "Migrations (#491)".
