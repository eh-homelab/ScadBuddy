import { pendingInput } from './routes/pendingInput.js'
import { pendingNotices, NotificationDispatcher, WAITING_POLL_MS } from './notify/dispatcher.js'
import { crudGrantRequests, crudPollLease, pairingRequests, waitingNotices } from './notify/waiting.js'
import { pushHostsOf, SETTING_PUSH_HOSTS } from './notify/pushEgress.js'
import { CrudPushStore } from './notify/pushStore.js'
import { ChannelStore, CrudChannelRows } from './notify/channels.js'
import { PendingChanges } from './gate/changes.js'
import { CrudAttachmentStore } from './attachments/crudStore.js'
import { ATTACHMENT_SWEEP_MS } from './attachments/store.js'
import { serve, upgradeWebSocket } from '@hono/node-server'
import { WebSocketServer } from 'ws'
import path from 'node:path'
import { event } from './sessions/protocol.js'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { AccessResolver } from './access/resolver.js'
import { CrudToolListings, ToolListings } from './access/listings.js'
import { AccessStore, CrudAccessRows } from './access/store.js'
import { createApp } from './app.js'
import { mcpAuthSettings } from './auth/authenticate.js'
import { OidcProvider, SettingsOidcConfigRepo } from './auth/oidc.js'
import { approvalGrantCheck, CrudTokenStore, liveTokenTiers } from './auth/tokens.js'
import { ConfigError, loadConfig } from './config.js'
import { type PolledQueue, queuesPolled, workerHealthApp } from './runtime/health.js'
import { roleParts } from './runtime/role.js'
import { inClusterApi, KubernetesSandboxes } from './sandbox/pods.js'
import { BrowserOrigins, CrudSessionOrigins } from './browser/store.js'
import { CrudCredentials, CrudSettings, SettingsStore } from './credentials.js'
import { CredentialPool } from './harness/fallback.js'
import { managerAgents } from './background/workflowAgents.js'
import { backgroundActivities, managerSender } from './background/activities.js'
import { agentTimezone } from './background/schedule.js'
import { BackgroundTasks } from './background/service.js'
import { BackgroundTaskStore, CrudBackgroundTasks } from './background/store.js'
import { runCronPasses, temporalTaskRunner } from './background/temporal.js'
import { ChangeStream } from './events/changeStream.js'
import { DEFAULT_STATE_DIR, pluginCacheDir } from './harness/options.js'
import { OWN_PLUGIN_DIR } from './harness/ownPlugin.js'
import { ensureStateDirs, removeLegacyBrowserDir, sessionsRoot, StateDirError } from './harness/stateDirs.js'
import { testConnection } from './harness/testConnection.js'
import { originPolicy } from './http/origins.js'
import { forwardForRun, PluginForwarder } from './plugins/forwarder.js'
import { startCredentialGateway, stopCredentialGateway } from './gateway/server.js'
import { CrudPluginSessions, endOwnSessions, replicaIdentity, runPluginSessionSweep } from './plugins/pluginSessions.js'
import { GitFetcher } from './plugins/packages/git.js'
import { localMirrorJobs, packageMirrorActivities, temporalMirrorJobs } from './plugins/packages/mirrorJob.js'
import { loadPackagesForRun, type MirrorJobs, PackageInstaller, packageLoadCheck, verifiedReview, warmPackages } from './plugins/packages/install.js'
import { reviewsByHash } from './access/packageProvider.js'
import { CrudPackageStore } from './plugins/packages/store.js'
import { CrudPackageMirror } from './plugins/packages/mirror.js'
import { CrudHostedPlugins } from './plugins/packages/hosted.js'
import { CrudPluginFeeds } from './plugins/feed.js'
import { BuiltInTools } from './plugins/builtInTools.js'
import { CrudOAuthRows, PluginOAuth } from './plugins/oauthStore.js'
import { CrudPluginStore, loadEnabledPlugins } from './plugins/registry.js'
import { toolSecrets } from './plugins/secrets.js'
import { ResourceHub } from './resources/hub.js'
import { ExchangeCredential, serviceCredentialMiddleware, withServiceCredential } from './api/serviceCredential.js'
import { BLOB_AREAS, CrudAreas, FORGET_AREAS, GATE_AREAS, KEY_AREAS, RELAY_AREAS, SECRET_AREAS, SESSION_AREAS, START_AREAS, TASK_AREAS, TURN_AREAS, WORKSPACE_AREAS, REST_AREAS, SWEEP_AREAS } from './api/crudAreas.js'
import { CrudFetchGrants, InputHashKey } from './api/agentKeys.js'
import { CrudApprovals } from './approvals/crudApprovals.js'
import { CrudAudit } from './audit/crudAudit.js'
import { CrudPending } from './gate/crudPending.js'
import { CrudQuestions } from './gate/crudQuestions.js'
import { CrudEscalations } from './flows/crudEscalations.js'
import { CrudGate } from './gate/crudStore.js'
import { dataHealthOf } from './api/dataHealth.js'
import { STORE_AREAS } from './api/crudStores.js'
import { CrudFlowRunReads } from './flows/backendReads.js'
import { BrowserSessions, CrudSessionLookup } from './auth/browserSession.js'
import { ApprovalActions } from './approvals/mcp.js'
import { sweepErrorText } from './approvals/service.js'
import { AuditLog } from './audit/log.js'
import { auditedTokenStore } from './audit/writes.js'
import { TabHub } from './bridge/hub.js'
import { hostTabs, TemporalTabRelay } from './bridge/relay.js'
import { CrudTabOwnerStore } from './bridge/tabOwners.js'
import { CrudSessionTabStore } from './bridge/sessionTabs.js'
import { CrudTabImageStore } from './bridge/tabImages.js'
import { CrudRelayRows } from './mcp/relayRows.js'
import { SharedMcpSessionRelay } from './mcp/sessionRelay.js'
import { CrudPairingStore } from './bridge/pairings.js'
import { CHAT_FRAME_MAX, startHeartbeat } from './routes/chat.js'
import { storedModel } from './routes/model.js'
import { followSessionEvents, SessionEventPublisher } from './sessions/busEvents.js'
import { CrudPublisher } from './sessions/publish.js'
import { CrudSessionReads } from './sessions/sessionReads.js'
import { CrudSessionPresence } from './sessions/presence.js'
import { resolveTabWaits, SessionManager, type SessionManagerDeps, SETTING_MODEL } from './sessions/manager.js'
import { readStartSettings, wireRuntimeSync } from './sessions/runtimeSync.js'
import { drainRetains, type MemoryOptions } from './memory/hindsight.js'
import { shutdown } from './shutdown.js'
import { shutdownTelemetry, traceListener } from './telemetry/runtime.js'
import { harnessTools } from './tools/harness.js'
import { CrudResourceRows, SessionResources } from './sessions/touched.js'
import { CrudSessionRows, type SessionBrowser } from './sessions/rows.js'
import { CrudTranscriptRows } from './sessions/crudTranscripts.js'
import { CrudEventRows } from './sessions/crudEvents.js'
import { CrudOrderCostRows, crudCostPoster, OrderCosts, touchedSinks } from './sessions/orderCosts.js'
import { ALL_TOOLS } from './tools/index.js'
import { CrudFlowBlobs } from './temporal/flowBlobs.js'
import { CrudFlowRuns } from './temporal/flowRuns.js'
import { flowSessionActivities } from './temporal/flowSessions.js'
import { decidedCalls, gateActivities, RecordedApprovals, RowSessionOwners, type ToolActivityDeps, toolActivities, toolCallActivities } from './temporal/toolActivities.js'
import { finishedToolCalls, toolCallResult, type ToolCallStarter, toolCallStarter } from './tools/activityProjection.js'
import { TASK_QUEUE } from './temporal/names.js'
import { sessionFinishActivities } from './temporal/session/finishActivity.js'
import { CrudTurnRows } from './sessions/turnRows.js'
import { CrudEditRows } from './sessions/edits.js'
import { sessionGateActivities } from './temporal/session/gateActivities.js'
import { acquireActivities, liveHostQueues, releaseActivities } from './temporal/session/affinity.js'
import { startHostWorkers, stopAfter } from './temporal/session/hostWorkers.js'
import { SESSION_DEPLOYMENT, SESSION_SETTINGS_ACTIVITY, SESSION_QUEUE, sessionHostQueue } from './temporal/session/names.js'
import { sessionTurnActivity } from './temporal/session/turnActivity.js'
import { TurnHost } from './sessions/turnHost.js'
import { SessionDirectory } from './workspace/directory.js'
import { readRuntimeSettings, RUNTIME_BOUNDS } from './sessions/runtimeSettings.js'
import { SHARED_PACKAGES, sweepPackageCopies } from './temporal/packageServers.js'
import { forwardPackageServers } from './plugins/packages/mcp.js'
import { pluginMessaging } from './orders/messaging.js'
import { DURABLE_TURNS_DIR, sweepTurnDirs, turnServers } from './temporal/turnServers.js'
import { SessionClients } from './temporal/sessionClients.js'
import { type ListedTools, listTurnTools, turnToolActivities, turnToolActivity } from './temporal/turnTools.js'
import { AgentWorker, workerIdentity } from './temporal/worker.js'
import { FilePayloadStore, CrudPayloadKeys, subjectDataConverter } from './temporal/payloadCodec.js'
import { DurableGate } from './gate/durable.js'
import { CrudHttpBodies, CrudSessionBlobs } from './sessions/crudBlobs.js'
import type { HttpBodiesFor } from './sessions/httpBodies.js'
import { DurableTurns } from './sessions/durable.js'
import { DurableRunningSweep, durableDescriber } from './sessions/durableSweep.js'
import { CrudSubjectKeys } from './sessions/forget.js'
import { PendingInputSweep, temporalDescriber } from './gate/sweep.js'
import { type NativeConnection, Runtime } from '@temporalio/worker'
import { Client, Connection } from '@temporalio/client'
import { fileURLToPath } from 'node:url'
import { AgentCommands } from './operations/run.js'
import { CrudOperations, OperationStore } from './operations/store.js'
import { activityKindsOf, crudUpsertActivityKinds } from './activity/kinds.js'
import { offerHosted, packageKinds } from './plugins/packages/operations.js'
import { operationActivities } from './temporal/operationActivities.js'
import { PendingActionStore } from './tools/pending.js'
import type { ToolServices } from './tools/registry.js'
import { RenderLimiter, settingsRenderLimits } from './tools/renderLimits.js'
import { CrudSessionFiles } from './workspace/crudStore.js'
import { WORKSPACE_SWEEP_MS, type WorkspaceBytesStatus } from './workspace/store.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the infrastructure variables in config.ts.
const PORT = 8081
/** How often approvals nobody is waiting on are expired (approvals/service.ts). */
const APPROVAL_SWEEP_MS = 30_000
/** How often sessions left `running` with nothing behind them are ended (DurableRunningSweep). */
const SESSION_REAP_MS = 30_000
// The shutdown's budget for running turns, inside the pod's 60 s
// terminationGracePeriodSeconds (clusters applications/scadbuddy/scadbuddy.yaml and
// scadbuddy-agent-worker.yaml, a RollingUpdate with maxSurge 1, maxUnavailable 0):
// TURN_DRAIN_MS for them to finish, TURN_ABORT_WAIT_MS for the rest to record that
// they were stopped, then shutdown()'s own 10 s.
const TURN_DRAIN_MS = 12_000
const TURN_ABORT_WAIT_MS = 5_000
/** How often audit rows past their retention are deleted (audit/log.ts). */
const AUDIT_RETENTION_SWEEP_MS = 60 * 60_000

let config: ReturnType<typeof loadConfig>
try {
  config = loadConfig()
} catch (err) {
  // A bad variable (an unknown SCADBUDDY_AGENT_ROLE among them) stops the process, naming it.
  if (!(err instanceof ConfigError)) throw err
  console.error(err.message)
  process.exit(1)
}
// An older deployment's variables, still set: named, never their values (#5299 step 12).
if (config.ignored.length) {
  console.warn(`agent: ignores ${config.ignored.join(', ')}: the agent has no database and no key-encryption key (#5299 step 12)`)
}
// What this process starts (runtime/role.ts, plan Step 6.1): the HTTP half, the turn workers, or both.
const role = roleParts(config.agentRole)

// Before listening: a mounted, empty state volume must get its `claude/` and
// `work/` back, and an unwritable one should stop the pod here, visibly.
try {
  await ensureStateDirs({ stateDir: DEFAULT_STATE_DIR, sessionsDir: config.sessionDir })
} catch (err) {
  console.error(err instanceof StateDirError ? err.message : err)
  process.exit(1)
}
// The removed built-in headless browser's folders, left on the volume by an older image.
await removeLegacyBrowserDir({ stateDir: DEFAULT_STATE_DIR }).catch((err: unknown) =>
  console.error(`cannot remove leftover headless-browser folders: ${String(err)}`),
)

// The agent's service credential (#4975, #5494): its proof, exchanged at the API for a short-lived
// credential that is renewed before it ends. config.ts requires a proof.
const serviceCredential = new ExchangeCredential({
  backendUrl: config.backendUrl,
  proofFile: (config.serviceTokenFile ?? config.serviceBootstrapFile)!,
  proof: config.serviceTokenFile !== undefined ? 'k8s' : 'bootstrap',
})
// The agent has no database and no key-encryption key (#5295, #5299): every store below is the
// API's CRUD routes (`/api/v1/internal/crud/agent/*`), called with the service credential. The API
// seals and opens what is sealed. The areas are read from the start, again with back-off until the
// API lists every one (api/crudAreas.ts); until then the agent is not ready.
const crudFetch = withServiceCredential(globalThis.fetch, serviceCredential, config.backendUrl)
const crudAreas = new CrudAreas(config.backendUrl, { fetch: crudFetch, wanted: [...SECRET_AREAS, ...FORGET_AREAS, ...STORE_AREAS, ...WORKSPACE_AREAS, ...BLOB_AREAS, ...TASK_AREAS, ...GATE_AREAS, ...SESSION_AREAS, ...REST_AREAS, ...TURN_AREAS, ...RELAY_AREAS, ...START_AREAS, ...SWEEP_AREAS, ...KEY_AREAS] })
crudAreas.start()
const crudTarget = { baseUrl: config.backendUrl, fetch: crudFetch }
// The approvals' and the audit log's input-hash key, released by the API (api/agentKeys.ts): a
// hash asked for before then throws KeyNotReleasedError. So this process is not ready until it
// holds the key, and no Temporal worker of it polls before then (`untilReady`): an activity, a route
// and /mcp hash a call's input after it ran. Once released it is kept for the process's life.
const inputHashKey = new InputHashKey(crudTarget)
inputHashKey.start()
const hashKey = (): Buffer => inputHashKey.get()
const untilKey = (): Promise<void> => inputHashKey.released()
// Health's probe of the agent's data (api/dataHealth.ts): ready once the API lists every area and
// has released the key, and up while it answers the areas. The sweeps run only while it is ready.
const dataHealth = dataHealthOf(crudAreas, undefined, () => inputHashKey.isReleased())
// The work this process does once at start waits for the API to list every area.
const untilAreas = (): Promise<void> => crudAreas.whenComplete()
/** Ready: every area listed and the key released; what a Temporal worker waits for before it polls. */
const untilReady = (): Promise<void> => untilAreas().then(untilKey)
// The tool-call gate's routes (#5299 step 10, backend api/crud_agent_gate.py), and its pending
// input and responses beyond its writers (area `pending-input`).
const gateRoutes = new CrudGate(crudTarget)
const pendingRoutes = new CrudPending(crudTarget)
const approvalRoutes = new CrudApprovals(crudTarget)
// A session's images, and the saved http_request bodies (#5299 step 7, backend api/crud_agent_blobs.py).
const sessionImages = new CrudSessionBlobs(config.backendUrl, { fetch: crudFetch })
const httpBodies: HttpBodiesFor = (sessionId) => new CrudHttpBodies(config.backendUrl, sessionId, { fetch: crudFetch })
// The sessions' rows and what their calls touched (#5299 step 9, backend api/crud_agent_sessions.py).
const sessionRows = new CrudSessionRows(config.backendUrl, { fetch: crudFetch })
const resourceRows = new CrudResourceRows(config.backendUrl, { fetch: crudFetch })
// The transcripts, sealed and opened by the API (backend api/crud_agent_transcripts.py).
const transcriptRows = new CrudTranscriptRows(config.backendUrl, { fetch: crudFetch })
// The sessions' event logs (backend api/crud_agent_events.py).
const eventRows = new CrudEventRows(config.backendUrl, { fetch: crudFetch })
// The NOTIFYs sent outside a store's write and a session's last event reads (#6501, backend
// api/crud_agent_publish.py).
const publisher = new CrudPublisher(crudTarget)
const sessionReads = new CrudSessionReads(crudTarget)
// A durable turn's writes of its session's row with their events (#6449, backend api/crud_agent_turns.py).
const turnRows = new CrudTurnRows(config.backendUrl, { fetch: crudFetch })
// An edit of a session with its events (#6449, backend api/crud_agent_edits.py).
const editRows = new CrudEditRows(config.backendUrl, { fetch: crudFetch })
// Which sessions still have a row, for the directory sweeps (#6501, area `session-sweeps`).
const sessionPresence = new CrudSessionPresence(crudTarget)
// The agent's Claude credentials, sealed and opened by the API (area `credentials`).
const credentials = new CrudCredentials(config.backendUrl, { fetch: crudFetch })
// The audit log of AI actions (#258, audit/log.ts). Its input hashes use the
// approvals' key, so a tool call's row carries the same hash as its approval.
// The settings store audits its own writes into it, so it comes second; the
// log reads its retention through the thunk.
const audit = new AuditLog({
  routes: new CrudAudit(crudTarget),
  settings: (): SettingsStore | undefined => settings,
  hashKey,
  onError: (err, entry) => console.error(`audit log: could not record ${entry.kind} ${entry.action}:`, (err as Error).message),
})
const settings = new SettingsStore(new CrudSettings(config.backendUrl, { fetch: crudFetch }), audit)
// The headless browser's approved origins (#982, area `browser-origins`).
const browserOrigins = new BrowserOrigins(settings, new CrudSessionOrigins(crudTarget))
// OIDC for /mcp (#262): the configuration in the agent's settings, one verifier with
// its metadata and JWKS caches for the process.
const oidcRepo = new SettingsOidcConfigRepo(settings, (detail) => console.error(`mcp auth: ${detail}`))
const oidcProvider = new OidcProvider()
// Area `access` (#5299 step 5): the API runs each write's transaction with its warm eviction, and a
// connection test holds a lease row there (#5299 D3).
const access = new AccessStore(new CrudAccessRows(crudTarget), audit)
// What each remote source last listed (#2171): a good connection test stores it.
const listings = new ToolListings(new CrudToolListings(crudTarget))
// Each item's effective access, read at each call (#2171): the turns' http_request and own-plugin switches.
const accessResolver = new AccessResolver(access)
// OAuth sign-in for remote plugins (#780): the forwarder asks it for each request's token. Area
// `plugins` (#5299 step 5): the API seals and opens the header secrets and the sign-ins.
const pluginOAuth = new PluginOAuth(new CrudOAuthRows(crudTarget))
const plugins = new CrudPluginStore(crudTarget, { oauth: pluginOAuth })
// ScadBuddy's own tool sets as plugins (#1953): their overrides, in ai_access (#2171).
const builtInTools = new BuiltInTools(access)
// Plugin traffic (connection tests, and each session turn's enabled plugins)
// goes through this loopback forwarder (plugins/forwarder.ts).
// The upstream sessions' ledger, area `plugin-sessions`.
const pluginSessionRows = new CrudPluginSessions(crudTarget)
const pluginForwarder = await PluginForwarder.start({
  sessions: pluginSessionRows,
  log: (message) => console.error(message),
})
// Every Claude Code process reaches the model through this loopback gateway, which holds the
// credential; the process gets only a token (gateway/server.ts, plan 2026-10-13 Step 9.4).
await startCredentialGateway()
// The API's clock once it lists every area, before a turn can record a session: the pass at
// start ends only this replica's rows older than it (plugins/pluginSessions.ts `endOwnSessions`).
const pluginSessionMark = untilAreas().then(() => pluginSessionRows.mark())
const backend = createBackendClient(config.backendUrl, globalThis.fetch, serviceCredentialMiddleware(serviceCredential))

// #5299 D7: the agent's reads of the backend's tables (the event log, flow runs, their
// decisions and grants) go only through the backend's CRUD routes (areas `events` and
// `flows`, test/retiredTables.test.ts).
const flowReads = new CrudFlowRunReads(config.backendUrl, { fetch: crudFetch })

// The event bus (spec §7, #264) and the agent's other NOTIFY channels: one server-sent event
// stream from the backend's CRUD routes (#5299 D4, events/changeStream.ts), opened with the service
// credential, retried in the background, feeding MCP resource subscriptions. No LISTEN connection.
const events = new ChangeStream(config.backendUrl, { fetch: crudFetch })
// The pending-input tables' change ticks (#815, gate/changes.ts), for the badge's stream.
const pendingChanges = new PendingChanges(events)
// This replica's own queue: a pinned turn tool's calls (#2320), a session's
// Monitor commands, which run where its turns' Bash runs, the browser_* calls other
// replicas make for a tab connected here (#4780), and the BridgeCall workflows this
// replica starts for a tab on another one (#5862).
const HOST_QUEUE = `${TASK_QUEUE}.${replicaIdentity()}`
// browser_* calls for a tab connected to another replica (#4780, bridge/relay.ts): a workflow
// on this replica's HOST_QUEUE whose activity runs on the owner's, through Temporal, sealed.
// Its client is set once made (temporalClient below); without it, such a call is refused.
const tabRelay = new TemporalTabRelay({
  owners: new CrudTabOwnerStore(crudTarget),
  hostQueue: HOST_QUEUE,
  ready: dataHealth.ready,
})
// The rows a process before this one left under the same identity (a container restart).
void tabRelay.releaseAll()
// /mcp requests for a session another replica holds (#2086, mcp/sessionRelay.ts), heard on the
// change stream's `scadbuddy_mcp`. Its directory and messages are area `mcp-relay` (#5299 step 11).
const mcpRelay = role.http
  ? new SharedMcpSessionRelay(new CrudRelayRows(crudTarget), {
      listen: (c, f) => events.listenAlso(c, f),
      onReconnect: (f) => events.onReconnect(f),
    })
  : undefined
// #4975 phase 5 (spec 2026-11-01 §5.6): the signed-in person on /api/v1/ai/*, from the
// backend's area `auth-sessions`; a revocation the backend publishes drops its cache.
const browserSessions = role.http ? new BrowserSessions(new CrudSessionLookup(crudTarget)) : undefined
if (browserSessions) browserSessions.follow(events)
events.start()
const resources = new ResourceHub(events)
// A session's working directory is under SCADBUDDY_SESSION_DIR when it is set (the worker's own emptyDir, plan Step 9.5).
const paths = { stateDir: DEFAULT_STATE_DIR, sessionsDir: config.sessionDir }
// The /mcp auth settings (auth/authenticate.ts `mcpAuthSettings`): `oidc` while
// the `mcp_oidc` setting is enabled (#262), otherwise the `mcp_auth_mode` and
// `mcp_anonymous_cap` keys. One reader for /mcp, per request, and for Settings
// (routes/mcpAuthMode.ts), so both report the same thing. A read that throws
// makes /mcp fail closed (mcp/http.ts).
const authSettings = mcpAuthSettings(settings, (message) => console.warn(`mcp auth: ${message}`), oidcRepo)
// One-off fetches off the asset allowlist (#959), minted by the API (api/agentKeys.ts).
const fetchGrants = new CrudFetchGrants(crudTarget)
// The registry's services (#251), shared by /mcp and every session's
// in-process tools. `pending` is swapped for the approvals' store below once
// the sessions (and so the approval service) exist.
const toolServices: ToolServices = {
  backend,
  pending: new PendingActionStore(),
  pollIntervalMs: 1000,
  renderWaitMs: 10 * 60_000,
  publicBaseUrl: config.publicUrl,
  // The render limits in Settings, read live (tools/renderLimits.ts).
  renderLimiter: new RenderLimiter(settingsRenderLimits(settings)),
  // Settings → Tools & plugins' changed tiers and disabled tools (#1953, #2171), read at each call.
  toolOverrides: builtInTools.registryOverrides(),
  // An approved one-off fetch off the asset allowlist's grant (#959).
  fetchGrant: (host, url) => fetchGrants.mint(host, url),
}
// The browser bridge (#254, bridge/hub.ts): the tabs connected over
// /api/v1/ai/bridge, which the browser_* tools drive; MCP clients pair with
// one through area `pairings` (spec §8.5).
const pairingStore = new CrudPairingStore(crudTarget)
const tabs = new TabHub({
  pairings: pairingStore,
  relay: tabRelay,
  // A durable session's browser_* calls run on whichever replica took the
  // activity, so the tab its chat socket paired is read from the API (#2086).
  sessionTabs: new CrudSessionTabStore(crudTarget),
  // A browser_screenshot's image, stored by the replica holding the tab for the one
  // whose call asked (#254, bridge/tabImages.ts).
  images: new CrudTabImageStore(crudTarget),
})
toolServices.browser = tabs
// What each session touched (#931, sessions/touched.ts), from its tool calls, and the order
// items it revised, whose spend each turn's end reports to the backend's CRUD route with the
// service credential (#5330, sessions/orderCosts.ts). The session's report lock is a lease row
// (area `session-order-costs`).
const orderCosts = new OrderCosts({
  rows: new CrudOrderCostRows(crudTarget),
  post: crudCostPoster(config.backendUrl, crudFetch),
  onError: (err) => console.error('order costs: could not record or report:', (err as Error).message),
})
if (role.http) {
  // What a session's last turn left to report (#5330), at start and hourly.
  const sweep = () => void dataHealth.ready().then((ok) => (ok ? orderCosts.sweep() : undefined))
  sweep()
  setInterval(sweep, 3600_000).unref()
}
toolServices.touched = touchedSinks(
  new SessionResources(resourceRows, (err) => console.error('session resources: could not record a call:', (err as Error).message)),
  orderCosts,
)
// The data keys of the durable payloads and the browser calls, released by the API (#5299 D5,
// #5342): one instance, so forgetting a subject drops its key from the one cache.
const dataKeys = new CrudPayloadKeys(config.backendUrl, { fetch: crudFetch })
// The assistant's workspace (#2329), area `workspace` (#5299 step 7, workspace/crudStore.ts): the
// API seals, stores and opens each file. The workspace_* tools and http_request's
// save_to_workspace write it; a principal's files are swept 24 h after their last write.
const workspace = new CrudSessionFiles(config.backendUrl, { fetch: crudFetch, settings })
toolServices.workspace = workspace
// Where writes go, for GET /api/v1/ai/status: checked at most once a minute (the backend may start after this).
let workspaceBytes: { at: number; status: Promise<WorkspaceBytesStatus> } | undefined
const workspaceBytesStatus = (): Promise<WorkspaceBytesStatus> => {
  if (!workspaceBytes || Date.now() - workspaceBytes.at > 60_000) {
    const status = workspace.bytesStatus()
    workspaceBytes = { at: Date.now(), status }
    // Never cached past a failure: the next status asks again.
    status.catch(() => {
      if (workspaceBytes?.status === status) workspaceBytes = undefined
    })
  }
  return workspaceBytes.status
}
// Once the API lists every area: say where the files go (once, the cause when refused), and move
// the files written before #4789 (or while the backend kept no blobs) to blobs; what is left is
// retried at the next start. Writes and reads ask the API each time and need no wait.
if (role.http) {
  void untilAreas()
    .then(async () => {
      // A status read before the API listed its areas would be served for up to a minute.
      workspaceBytes = undefined
      const status = await workspaceBytesStatus()
      if (status.store !== 'blobs' && status.unreadable_files) {
        console.error(`workspace files: ${status.unreadable_files} file(s) in the blob store cannot be read from here: ${status.reason}`)
      }
      if (status.store === 'refused') {
        console.error(`workspace files: every write is refused, and nothing is kept in plaintext: ${status.reason}`)
        return
      }
      if (status.store === 'postgres') {
        console.log(`workspace files: kept in Postgres: ${status.reason}`)
        return
      }
      console.log(`workspace files: sealed blobs in the backend's store (store_backend ${status.backend})`)
      const { moved } = await workspace.moveToBlobs()
      if (moved) console.log(`workspace files: moved ${moved} file(s) from Postgres to the blob store`)
    })
    .catch((err: unknown) => console.error('workspace files: moving them to the blob store failed; retried at the next start:', (err as Error).message))
}
const stopWorkspaceSweep = !role.http
  ? undefined
  : workspace.startSweeper(WORKSPACE_SWEEP_MS, {
      ready: dataHealth.ready,
      onError: (err) => console.error('workspace sweep failed:', (err as Error).message),
    })
// Plugin packages (#297): the pin is the API's (area `plugin-packages`); the files under
// <state dir>/plugins are a cache, rebuilt from the pin and verified against its content hash
// before each load (plugins/packages/). Each session turn loads the enabled ones (`packagePlugins` below).
const pluginPackages = new CrudPackageStore(crudTarget)
// An archive or npm package's file (#6935) is kept by the API (area `package-mirrors`, one of
// `STORE_AREAS`, so the agent is not ready until the API lists it).
const packageMirror = new CrudPackageMirror(crudTarget)
// A hosted plugin's versions (#6934) are read from the API too (area `hosted-plugins`, one of
// `STORE_AREAS`).
const hostedPlugins = new CrudHostedPlugins(crudTarget)
// A commit pin's mirror job (#7907) fetches with git only there, never on a load: a depth-1
// fetch of the hindsight monorepo took 132 s on a 2-CPU worker pod, past the install fetcher's
// 120 s, so the job's fetcher waits 10 minutes (under the PackageMirror activity's 20).
const MIRROR_FETCH_TIMEOUT_MS = 10 * 60_000
// The PackageMirror workflow when this process has a Temporal client (it is made below), else
// the same job in this process.
const mirrorJobs: { current?: MirrorJobs } = {}
const packageInstaller = new PackageInstaller({
  fetcher: new GitFetcher(),
  mirrorFetcher: new GitFetcher({ timeoutMs: MIRROR_FETCH_TIMEOUT_MS }),
  cacheRoot: pluginCacheDir(paths),
  mirror: () => packageMirror,
  hosted: () => hostedPlugins,
  // A commit pin's files are mirrored too, and recorded on its row (#7907).
  recordMirror: (name, mirror) => pluginPackages.recordMirror(name, mirror),
  mirrorJobs: () => mirrorJobs.current,
})
// Settings → Tools & plugins (#4757): an older stored review's items read from the cached copy
// (read only), once per content hash; and what a turn would make of the enabled packages.
const verifiedPackageReview = reviewsByHash((pin) => verifiedReview(packageInstaller, pin))
const packagesLoadCheck = packageLoadCheck(pluginPackages, packageInstaller)
// hosted_plugin_publish's offer (#6938, spec 2026-11-21 §6.2): in-process, never through the
// UI-guarded package routes. Every path shares toolServices (an in-process turn, /mcp, a ToolCall).
toolServices.hostedPackages = {
  get: (name) => pluginPackages.get(name),
  offer: (name, signal) => offerHosted(pluginPackages, packageInstaller, name, signal),
}
// The cache is this pod's scratch, empty after every deploy: filled at start, so a
// turn does not wait on a whole repository's fetch (#4217).
void untilAreas()
  .then(async () => {
    for (const problem of await warmPackages(pluginPackages, packageInstaller)) {
      console.warn('plugin package warm-up:', problem)
    }
  })
  .catch((err: unknown) => console.error('plugin package warm-up failed:', (err as Error).message))

// One store for Settings (routes/mcpTokens.ts) and /mcp (area `mcp-tokens`). Mint and revoke
// are recorded in the audit log (#258), whichever of the two makes them.
const tokens = auditedTokenStore(new CrudTokenStore(crudTarget), audit)

// `session.*` on the event bus (#300, sessions/busEvents.ts): every event-log
// append is announced on `scadbuddy_events`, and this replica's change-stream
// consumer (below) wakes its followers for sessions other replicas write.
const sessionEvents = new SessionEventPublisher(publisher)

// A session's background tasks (CronCreate, ScheduleWakeup, Monitor, TaskStop), in either mode:
// a recurring cron job a Schedule, any other a `BackgroundTask` workflow, on agent-tools, that
// sends its prompt when due. Their payloads are task ids only, so their client has no codec.
const backgroundClient = config.temporalAddress
  ? new Client({ connection: Connection.lazy({ address: config.temporalAddress }), namespace: config.temporalNamespace })
  : undefined
const backgroundTasks = backgroundClient
  ? new BackgroundTasks({
      store: new BackgroundTaskStore(new CrudBackgroundTasks(config.backendUrl, { fetch: crudFetch })),
      runner: temporalTaskRunner(backgroundClient, TASK_QUEUE),
    })
  : undefined
// Decision Q11 (plan 2026-10-13 Step 9.8): the recurring cron jobs made before Schedules become
// Schedules now, not at their next occurrence; then, hourly, the jobs whose last occurrence was
// missed are ended. Idempotent, so every replica may run it.
if (backgroundClient && backgroundTasks) {
  runCronPasses({ client: backgroundClient, store: backgroundTasks.store, taskQueue: TASK_QUEUE }, { info: console.log, error: console.error })
}
// Sessions (#300) and their approvals (#258): started from the assistant
// panel's socket (routes/chat.ts), the session routes (routes/sessions.ts)
// and the `sessions_*` tools (tools/sessions.ts).
// The approval routes and the expiry sweep also serve approvals left pending
// by a restart.
const runtime = await readStartSettings({ areas: untilAreas, settings }, console.log)
// Automatic Hindsight memory's hook knobs for every turn (SessionManager, #2420).
// Undefined: the defaults.
const memory: MemoryOptions | undefined = undefined

// Bash and PowerShell in a sandbox pod per session (#5864, sandbox/pods.ts): only where turns run,
// and only once the cluster side is set up (SCADBUDDY_SANDBOX_*). Each minute the pods that have
// ended (idle, or past their deadline) are deleted.
const sandboxes = config.sandbox && role.sessionHost ? new KubernetesSandboxes(config.sandbox, inClusterApi(config.sandbox)) : undefined
if (sandboxes) {
  setInterval(() => {
    sandboxes.sweep().catch((err: unknown) => console.error('sandbox: the sweep of ended pods failed:', err instanceof Error ? err.message : String(err)))
  }, 60_000).unref()
}

// How a turn's ScadBuddy call runs as its ToolCall workflow (plan 2026-10-13 Step 9.2), once the
// Temporal client below exists; read by each turn (until then, its calls run in-process).
let toolCallStart: ToolCallStarter | undefined
// And how its plugin and http tools are listed for their proxies (Step 9.3), once turnToolDeps exists.
let turnToolList: NonNullable<SessionManagerDeps['turnTools']> | undefined
const sessions = new SessionManager({
  memory,
  rows: sessionRows,
  resourceRows,
  transcripts: transcriptRows,
  eventRows,
  sessionReads,
  publish: publisher,
  editRows,
  pendingRoutes,
  approvalRoutes,
  questionRoutes: new CrudQuestions(crudTarget),
  escalationRoutes: new CrudEscalations(crudTarget),
  blobs: sessionImages,
  httpBodies,
  browserOrigins,
  flowReads,
  paths,
  settings,
  access: accessResolver,
  // ScadBuddy's tools and their tiers (tools/harness.ts).
  ...harnessTools(toolServices),
  toolCalls: () => toolCallStart,
  // Listed as the agent-tools activities that run their calls list them (Step 9.3), on this
  // replica's own queue for a pinned server; problems are the turn's own to say (pluginCheck).
  turnTools: (context, only) => (turnToolList ? turnToolList(context, only) : Promise.reject(new Error('no Temporal, so no turn tool runs as an activity'))),
  // Settings → Tools & plugins' overrides of the built-in tools (#1953), per turn.
  builtInTools: () => builtInTools.policy(),
  // ScadBuddy's own plugin (#896, harness/ownPlugin.ts): its skills and
  // subagents, with the Skill and Agent tools they need.
  ownPlugin: OWN_PLUGIN_DIR,
  // Input hashes are HMACs under the API's key, so they compare across restarts
  // (approvals/service.ts BINDING).
  approvalHashKey: hashKey,
  // Other agents decide approvals only with their token's grant (spec §6, #300).
  approvalGrants: approvalGrantCheck(tokens),
  // A resumed approval's turn gets no more than its token holds now (#300).
  currentTiers: liveTokenTiers(tokens),
  onAppend: sessionEvents.onAppend,
  // Every tool call a turn makes, and every approval decision (#258).
  audit,
  // Enabled plugins (#297), per turn, through the loopback forwarder, with the session's browser options (#7027).
  remotePlugins: async (browser) =>
    forwardForRun(await loadEnabledPlugins(plugins), pluginForwarder, { record: true, ...(browser ? { browser } : {}) }),
  // Enabled plugin packages (#297), materialised from their pins, per turn.
  packagePlugins: () => loadPackagesForRun(pluginPackages, packageInstaller),
  packageServers: (loaded) => forwardPackageServers(loaded, pluginForwarder, undefined, { record: true, access: accessResolver }),
  // The http_request tool (#827): on for a turn unless `builtin:http` is
  // switched off in ai_access (#2171).
  httpRequest: {},
  // The workspace http_request's save_to_workspace writes, and a fork copies (#2329).
  workspace,
  // Claude Code's CronCreate and the rest, kept past the turn (src/background/).
  ...(backgroundTasks ? { background: { tasks: backgroundTasks, tz: agentTimezone(), commandQueue: HOST_QUEUE } } : {}),
  ...(sandboxes ? { sandbox: sandboxes } : {}),
  // Every usable credential in priority order, with fallback; disables,
  // cooldowns, recoveries and fallbacks are audited (#1093).
  credentials: new CredentialPool({ repo: credentials, audit }),
  // One warm Claude Code per session (unified runtime spec 2026-10-13
  // §5.3, sessions/warm.ts), sized by the runtime settings (sessions/runtimeSettings.ts).
  warm: { maxEntries: runtime.warm_max_per_replica, idleMs: runtime.warm_idle_seconds * 1000, log: (message) => console.log(message) },
})
// A credential, plugin or package change on any replica, and a session handed
// off, marked done, archived or forgotten, evict warm processes everywhere
// (sessions/warmEvict.ts): on the change stream, like the relays above.
const stopWarmSync = wireRuntimeSync({
  settings,
  warm: sessions.warm,
  revalidate: () => sessions.revalidateWarm(),
  events,
  log: console.log,
})
// MCP prepare/confirm on the approvals (approvals/mcp.ts).
toolServices.pending = new ApprovalActions(sessions.approvals)
// #815 §2: a session whose tab is connected again stops waiting for it.
resolveTabWaits(tabs, sessions)
// The `sessions_*` tools (#300) act on the same manager, over /mcp and in-process.
toolServices.sessions = sessions
// The panel's image uploads (#1941, attachments/crudStore.ts): the upload route, the
// chat socket and sessions_send read them; unsent ones expire and are swept.
const attachments = new CrudAttachmentStore(config.backendUrl, sessionImages, { fetch: crudFetch })
toolServices.attachments = attachments
const stopAttachmentSweep = !role.http
  ? undefined
  : attachments.startSweeper(ATTACHMENT_SWEEP_MS, {
      log: (message) => console.error(message),
    })
// The change-stream consumer that calls EventLog.wake() for other replicas' `session.*`.
const stopSessionWake = followSessionEvents(events, sessions.events, sessionEvents.replica)
const stopSweeper = !role.http
  ? undefined
  : sessions.approvals.startSweeper(APPROVAL_SWEEP_MS, {
      ready: dataHealth.ready,
      onError: (err) => console.error('approval expiry sweep failed:', sweepErrorText(err)),
    })
// A flow run's call whose hop at its starting session is over goes to the person (#2482).
const stopEscalationSweeper = !role.http
  ? undefined
  : sessions.escalations.startSweeper(APPROVAL_SWEEP_MS, {
      ready: dataHealth.ready,
      onError: (err) => console.error('escalation hop sweep failed:', (err as Error).message),
    })
// Audit rows older than `audit_retention_days` (the agent's settings) are deleted hourly.
const stopRetention = !role.http
  ? undefined
  : audit.startRetention(AUDIT_RETENTION_SWEEP_MS, {
      ready: dataHealth.ready,
      onError: (err) => console.error('audit retention sweep failed:', (err as Error).message),
    })

// The agent-tools worker (spec 2026-10-01 §4.3, §6.3, #1055): every tool as an
// activity for durable sessions' workflows, and the agent's commands (plugin package
// install and re-pin, AgentOperation), recorded through area `operations`. It needs
// SCADBUDDY_TEMPORAL_ADDRESS. Its SIGTERM is ours: stop() below stops it alongside the
// turns, so the SDK's own signal handling is turned off.
const temporal = config.temporalAddress ? { address: config.temporalAddress } : undefined
if (temporal) Runtime.install({ shutdownSignals: [] })
const operationStore = temporal ? new OperationStore(new CrudOperations(config.backendUrl, { fetch: crudFetch })) : undefined
const commandKinds = packageKinds({ packages: pluginPackages, installer: packageInstaller })
// The agent's activity kinds are its command kinds (spec 2026-10-09 §4), read by the backend.
if (role.http && commandKinds.length > 0) {
  void untilAreas()
    .then(() => crudUpsertActivityKinds(crudTarget, activityKindsOf(commandKinds)))
    .catch((err: unknown) => console.error('activity kinds: could not record them:', (err as Error).message))
}
// Durable subjects' payloads are sealed per subject (spec §6.5): every client and worker
// that encodes or decodes them carries the codec, with each subject's key from the API (#5299
// step 4). A call whose key the API does not release fails and is retried
// (`PayloadKeyUnavailable`): a session or flow payload is never sent unsealed (5c Ruling 13).
const payloadKeys = temporal ? dataKeys : undefined
// A flow call's images (#2477), sealed here under its run's key and kept by the API (area `forget`, #5299 step 6).
const flowBlobs = payloadKeys ? new CrudFlowBlobs(config.backendUrl, payloadKeys, { fetch: crudFetch }) : undefined
// A session's large payloads are files on the data share, by reference (#2243).
const payloadStore = config.payloadDir ? new FilePayloadStore(config.payloadDir) : undefined
const dataConverter = payloadKeys ? subjectDataConverter(payloadKeys, payloadStore) : undefined
// The worker also moves failures' messages into payloads, so the codec seals them (5c Ruling 11).
const workerDataConverter = dataConverter
  ? { ...dataConverter, failureConverterPath: fileURLToPath(new URL('./temporal/failureConverter.js', import.meta.url)) }
  : undefined
// Package copies no turn has used for a week (#2395): the shared ones on the payload
// volume, and this pod's own copies without their http servers. Every hour.
{
  const roots = [path.join(paths.stateDir, 'plugins-local'), ...(config.payloadDir ? [path.join(config.payloadDir, SHARED_PACKAGES)] : [])]
  const sweep = () =>
    void Promise.all(roots.map((root) => sweepPackageCopies(root))).catch((err: unknown) =>
      console.error('package copy sweep failed:', (err as Error).message),
    )
  sweep()
  setInterval(sweep, 3600_000).unref()
}
// Each replica's saved http bodies of durable sessions gone since (#2496), every hour.
{
  const sweepRoot = path.join(paths.stateDir, DURABLE_TURNS_DIR)
  const sweep = () =>
    void sweepTurnDirs(sweepRoot, sessionPresence).catch((err: unknown) => console.error('durable http body sweep failed:', (err as Error).message))
  sweep()
  setInterval(sweep, 3600_000).unref()
}
// The payload keys of sessions whose row is gone (#3317): a durable fork whose workflow could not
// start; and of browser calls a cancel or a lost caller left (#4780). Deleted by the API (area
// `forget`, #5299 step 6), every hour while it is ready.
const keyDeletes = new CrudSubjectKeys(config.backendUrl, { fetch: crudFetch })
if (role.http) {
  const sweep = () =>
    void dataHealth
      .ready()
      .then(async (ok) => (ok ? await keyDeletes.sweep() : 0))
      .catch((err: unknown) => console.error('orphaned payload key sweep failed:', (err as Error).message))
  sweep()
  setInterval(sweep, 3600_000).unref()
}
// What a durable turn offers beside ScadBuddy's own tools (#2320): the servers a
// classic turn would hand Claude Code, built again for every listing and call. One whose
// state lives on this pod (http_request's saved bodies) runs its calls on this replica's
// own queue, HOST_QUEUE (above), served by `hostWorkers` below.
// AgentSession's turns on this replica (unified runtime spec 2026-10-13 §5.2): its own queue,
// `agent-sessions.<identity>`, beside HOST_QUEUE (temporal/session/hostWorkers.ts says why), on
// this replica's warm processes. `acquire_worker` on agent-tools hands it out.
const SESSION_HOST_QUEUE = sessionHostQueue(replicaIdentity())
// A session's owner as a tool call runs (toolActivities.ts), and a flow run's call (#1057).
const sessionOwners = new RowSessionOwners(sessionRows)
const flowRuns = new CrudFlowRuns(flowReads, sessionRows)
// What a durable session's tool runs with: its activities on agent-tools, and a released
// AgentSession turn's approved call (§5.4 step 5), which runs on the turn's own replica.
const durableToolDeps: ToolActivityDeps | undefined = temporal
  ? {
      services: toolServices,
      sessions: sessionOwners,
      approvals: new RecordedApprovals(flowReads, pendingRoutes),
      flows: flowRuns,
      audit,
      currentTiers: liveTokenTiers(tokens),
      // A result's images, for the durable segment's view_tool_images (#2320).
      blobs: sessionImages,
      // A flow call's images, sealed under its run's payload key (#2477).
      ...(flowBlobs ? { flowBlobs } : {}),
      // A package built-in's access per call, `builtin_tier` (#4757).
      access: accessResolver,
    }
  : undefined
// Each session's working directory (workspace/directory.ts, plan Step 9.5): a copy of its workspace
// under SCADBUDDY_SESSION_DIR (the worker's own capped emptyDir), refilled from the workspace at each
// placement. The directories of sessions gone since are swept every hour, by every process that runs turns.
const sessionDirectory = role.sessionHost
  ? new SessionDirectory(workspace, sessionsRoot(paths), { presence: sessionPresence, warm: (id: string) => sessions.warm.has(id) })
  : undefined
// A session's copy goes once its warm process here has: this disk holds the warm sessions' copies only.
if (sessionDirectory) {
  sessions.warm.onClosed((id) =>
    void sessionDirectory.evict(id).catch((err: unknown) => console.error(`session ${id}: its working directory could not be removed:`, (err as Error).message)),
  )
  const sweep = () =>
    void dataHealth
      .ready()
      .then((ok) => (ok ? sessionDirectory.sweep(sessionPresence) : []))
      .catch((err: unknown) => console.error('session directory sweep failed:', (err as Error).message))
  sweep()
  setInterval(sweep, 3600_000).unref()
}
// A parked call holds on its entry's row, woken by `scadbuddy_input` on the change stream (§5.4).
const turnHost =
  role.sessionHost && temporal
    ? new TurnHost({
        core: sessions.turnCore,
        turns: turnRows,
        pendingRoutes,
        images: sessionImages,
        hostQueue: SESSION_HOST_QUEUE,
        listen: (channel: string, onNotify: (payload: string) => void) => events.listenAlso(channel, onNotify),
        ...(durableToolDeps ? { runCall: decidedCalls(ALL_TOOLS, durableToolDeps) } : {}),
        // A restarted turn's calls that finished as ToolCall workflows (Step 9.2), read with the client below.
        finishedCalls: (sessionId: string, turnId: string) =>
          temporalClient ? finishedToolCalls({ reads: sessionReads, client: temporalClient }, sessionId, turnId) : Promise.resolve([]),
        // A `done` summary, posted as a classic turn's (#5003).
        questions: sessions.questions,
        // The session's working directory, a copy of its workspace placed for each turn and written back at its end (Step 9.5).
        ...(sessionDirectory ? { directory: sessionDirectory } : {}),
      })
    : undefined
// The enabled remote plugins and every usable credential's secret, as a classic turn
// reads them, for a durable turn's tools and its automatic memory.
const durableRemotePlugins = async (browser?: SessionBrowser) =>
  forwardForRun(await loadEnabledPlugins(plugins), pluginForwarder, browser ? { browser } : {})
const durableCredentialSecrets = async () => {
  const pool = new CredentialPool({ repo: credentials, audit })
  return (await pool.candidates().catch(() => [])).map((c) => c.credential.secret)
}
// A durable session's MCP client on each remote plugin, kept on the replica its calls
// are pinned to (#2581): a stateful plugin (Playwright MCP) keeps its page between calls.
const turnClients = new SessionClients<ListedTools>()
// What every tool redacts and every upload refuses, in every mode (an in-process turn, a
// ToolCall, /mcp, a background task) (#5480): the saved credentials and every plugin secret
// (plugins/secrets.ts `toolSecrets`, #5483). A turn adds its loaded plugins' current headers on
// top (tools/harness.ts), so a header it signed in with since the last read is covered too. The
// turn's own forwarder URLs never reach Claude Code with the turn's tools proxied (plan
// 2026-10-13 Step 9.3).
const agentSecrets = toolSecrets({
  credentials,
  plugins,
  oauth: pluginOAuth,
  packages: { store: pluginPackages, installer: packageInstaller },
})
toolServices.secrets = agentSecrets.secrets
// The messaging plugin a customer notice goes through (#5660, orders/messaging.ts): the
// bound remote MCP server through the loopback forwarder, as a turn reaches it, in the one
// services object every path shares (an in-process turn, /mcp, a ToolCall, a flow's call).
toolServices.messaging = pluginMessaging({
  remote: durableRemotePlugins,
  packages: async () => {
    const loaded = await loadPackagesForRun(pluginPackages, packageInstaller)
    try {
      const forwarded = await forwardPackageServers(loaded.loaded, pluginForwarder, undefined, { access: accessResolver })
      return { plugins: forwarded.plugins, servers: forwarded.servers, release: () => (forwarded.release(), loaded.release()) }
    } catch (err) {
      loaded.release()
      throw err
    }
  },
})
const turnToolDeps = temporal
  ? {
      sessions: sessionOwners,
      // A flow's tool(name, ...) reaches the same tools, as the run's starter (#2320).
      flows: flowRuns,
      servers: turnServers({
        remotePlugins: durableRemotePlugins,
        access: accessResolver,
        // The headless browser's lists (#982), as a classic turn's.
        browserOrigins,
        credentialSecrets: durableCredentialSecrets,
        // This pod's own disk, as a classic turn's: the calls are pinned here (hostQueue).
        httpRequest: { saveRoot: path.join(paths.stateDir, DURABLE_TURNS_DIR), bodies: httpBodies, audit, files: workspace },
        packages: {
          load: () => loadPackagesForRun(pluginPackages, packageInstaller),
          forward: (loaded) => forwardPackageServers(loaded, pluginForwarder, undefined, { access: accessResolver }),
        },
      }),
      hostQueue: HOST_QUEUE,
      clients: turnClients,
      approvals: new RecordedApprovals(flowReads, pendingRoutes),
      audit,
      currentTiers: liveTokenTiers(tokens),
      blobs: sessionImages,
      // A flow's plugin and http_request calls keep their images as its own tools' do (#2578).
      ...(flowBlobs ? { flowBlobs } : {}),
      report: async (session: string, problems: readonly string[]) => {
        await sessions.events.append(
          session,
          problems.map((message) => event({ type: 'error', sessionId: session, code: 'plugin_unavailable', message })),
        )
      },
    }
  : undefined
if (turnToolDeps) {
  const deps = turnToolDeps
  // The turn says what its listing left out itself (`unlisted`), beside its own assembly's problems.
  turnToolList = (context, only) => listTurnTools({ ...deps, report: undefined }, context, only)
}
// AgentSession's turns are hosted here only with both halves (§5.2): acquire_worker on agent-tools
// hands out SESSION_HOST_QUEUE, so it is offered only where the session host worker below serves it.
const sessionHosting = turnHost && operationStore && turnToolDeps ? { turnHost, warm: sessions.warm } : undefined
/**
 * The session host worker's slots, fixed when it starts: the most warm processes
 * `warm_max_per_replica` may be set to (RUNTIME_BOUNDS), which can change while it runs, plus two
 * for releases. The warm cache's `reserve` is what holds a replica to its setting.
 */
const SESSION_HOST_SLOTS = RUNTIME_BOUNDS.warm_max_per_replica.max + 2
// Routes start commands, and reach durable sessions' gates, through a lazy client: a
// Temporal that is down answers 503. acquire_worker lists the live session hosts with it.
const temporalClient = temporal
  ? new Client({
      connection: Connection.lazy({ address: temporal.address }),
      namespace: config.temporalNamespace,
      ...(dataConverter ? { dataConverter } : {}),
    })
  : undefined
mirrorJobs.current = temporalClient
  ? temporalMirrorJobs(temporalClient)
  : localMirrorJobs({ packages: pluginPackages, installer: packageInstaller })
// This process's agent-tools worker (below), once it starts: a turn here starts its calls eagerly on
// that worker's connection (activityProjection.ts `ToolCallStarterDeps`, #5865).
const eager: { worker: AgentWorker | undefined } = { worker: undefined }
const eagerClients = new WeakMap<NativeConnection, Client>()
const eagerClient = (): Client | undefined => {
  const connection = eager.worker?.connection()
  if (!connection) return undefined
  let client = eagerClients.get(connection)
  if (!client) {
    client = new Client({ connection, namespace: config.temporalNamespace, ...(dataConverter ? { dataConverter } : {}) })
    eagerClients.set(connection, client)
  }
  return client
}
if (temporalClient) {
  const results = { client: temporalClient, blobs: sessionImages }
  toolCallStart = toolCallStarter({ ...results, eager: eagerClient })
  // `tool_call_result`: what a finished call returned, which a restarted turn's message names (Step 9.2).
  toolServices.toolCallResults = (session, toolUseId) => toolCallResult(results, session, toolUseId)
}
// A place in this replica's warm cache, and its session host queue (§5.2): on agent-tools, and on
// the session host queue for a turn another replica (one it was lost on) refers here.
const sessionAcquire =
  sessionHosting && temporalClient
    ? acquireActivities({
        warm: sessionHosting.warm,
        hostQueue: SESSION_HOST_QUEUE,
        identity: replicaIdentity(),
        liveHosts: () =>
          liveHostQueues(temporalClient).catch((err: unknown) => {
            // acquire_worker takes the turn here instead (affinity.ts): said, so a role that may not describe queues is seen.
            console.warn('acquire_worker: could not list the live session hosts:', (err as Error).message)
            throw err
          }),
      })
    : undefined

// One set of background-task activities (src/background/): the shared queue's, and the Monitor command this replica runs.
const background = backgroundTasks
  ? backgroundActivities({
      store: backgroundTasks.store,
      send: managerSender(sessions),
      tz: agentTimezone(),
      paths,
      ...(sessionDirectory ? { directory: sessionDirectory } : {}),
      secrets: toolServices.secrets,
      agents: managerAgents(sessions, { access: accessResolver }),
      workspace,
    })
  : undefined
// The gate's writes go through the API (area `gate`), its events through this replica's event log,
// so they wake its followers at once and go on the bus under its id (#3365).
const gateDb = { events: sessions.events, crud: gateRoutes }
// The queues of `scadbuddy-agent` this process polls, each polled by this build before it is made
// current (worker.ts): read from the workers that started, when the build is made current, so a
// queue whose worker this process never starts stays out and the build is still made current on the other.
const versionedQueues = (): string[] => [
  ...(temporalWorker ? [TASK_QUEUE] : []),
  ...(sessionWorker ? [SESSION_QUEUE] : []),
]
const temporalWorker =
  role.sharedWorkers && temporal && operationStore && turnToolDeps && durableToolDeps
    ? AgentWorker.start({
        before: untilReady,
        address: temporal.address,
        namespace: config.temporalNamespace,
        // Polled as this replica: a poller listing names it (affinity.ts `liveHostQueues`, pluginSessions.ts).
        identity: workerIdentity(replicaIdentity()),
        activities: {
          ...toolActivities(ALL_TOOLS, durableToolDeps),
          // A session turn's calls, each its own ToolCall workflow (plan 2026-10-13 Step 9.2),
          // redacted of the plugin and package tokens through `toolServices.secrets`, as an in-process call is.
          ...toolCallActivities(ALL_TOOLS, durableToolDeps),
          ...gateActivities({
            audit,
            sessions: sessionOwners,
            workspace,
            // A tool that resolves what it names parks that, by name (#4442).
            tools: ALL_TOOLS,
            services: durableToolDeps.services,
            // Resolved with the tiers the owner holds now, as the call will run (#4442 gate review).
            currentTiers: durableToolDeps.currentTiers,
          }),
          // AgentSession's gate (its writers and timers) and its turn end (unified runtime §5.1).
          ...sessionGateActivities(gateDb, settings),
          ...sessionFinishActivities(turnRows, sessions.events, orderCosts, sessionImages),
          ...(sessionAcquire ?? {}),
          // A durable turn's plugin and http_request tools (#2320): listed per turn, and
          // each call the `default` activity `turn:<server>__<tool>`.
          ...turnToolActivities(turnToolDeps),
          default: turnToolActivity(turnToolDeps),
          // A flow's agent(...) and ask_session(...) (#1057, 6g): beside the tools, never one.
          ...flowSessionActivities({ sessions: sessionRows, flows: flowRuns, events: eventRows }),
          ...operationActivities(commandKinds, operationStore),
          // A commit pin's mirror (#7907): the PackageMirror workflow's one activity.
          ...packageMirrorActivities({ packages: pluginPackages, installer: packageInstaller }),
          ...(background ?? {}),
        },
        ...(workerDataConverter ? { dataConverter: workerDataConverter } : {}),
        // The same Deployment and build as agent-sessions (plan Phase 11): Temporal then hands
        // agent-tools' tasks, whoever schedules them, only to the current build's workers. A ToolCall
        // started eagerly (`eager`, above) asks for its first workflow task on this replica's worker
        // instead, and its activity too when the call is not pinned and the worker has a free slot.
        // Temporal grants that to a build a later one has superseded too (an old replica during a
        // rollout): the old build runs the call's first task and activity (measured on a dev server,
        // Server 1.32.0, test/toolCallLatency.temporal.test.ts `SUPERSEDED`). A call it declines goes
        // through matching to the current build; the call's span says which (`scadbuddy.eager`).
        ...(config.buildId ? { deployment: { name: SESSION_DEPLOYMENT, buildId: config.buildId, queues: versionedQueues } } : {}),
        // Bundled by `pnpm build` (scripts/bundle-workflows.mjs).
        workflows: {
          workflowBundle: { codePath: fileURLToPath(new URL('./temporal/workflow-bundle.js', import.meta.url)) },
        },
      })
    : undefined
eager.worker = temporalWorker
// AgentSession (unified runtime spec 2026-10-13 §5.1): every new session runs here (plan Step 6.3).
// Versioned as the Deployment `scadbuddy-agent` (D4) when the build has an id (config.buildId),
// as agent-tools is, else unversioned like the host queues. `session_settings` is a local activity, so it
// is served here. `session_turn` is Task 5.2's, on the host worker.
const sessionWorker =
  role.sharedWorkers && temporal
    ? AgentWorker.start({
        before: untilReady,
        address: temporal.address,
        namespace: config.temporalNamespace,
        taskQueue: SESSION_QUEUE,
        // Polled as this replica, so its /readyz finds it (runtime/health.ts `queuesPolled`).
        identity: workerIdentity(replicaIdentity()),
        activities: { [SESSION_SETTINGS_ACTIVITY]: () => readRuntimeSettings(settings) },
        ...(workerDataConverter ? { dataConverter: workerDataConverter } : {}),
        ...(config.buildId ? { deployment: { name: SESSION_DEPLOYMENT, buildId: config.buildId, queues: versionedQueues } } : {}),
        workflows: {
          workflowBundle: { codePath: fileURLToPath(new URL('./temporal/workflow-bundle.js', import.meta.url)) },
        },
      })
    : undefined
// This replica's own queues (temporal/session/hostWorkers.ts), unversioned (plan Phase 11): a queue
// only one replica polls is no member of a newer build, so once that build is current Temporal
// would hand a versioned one's tasks to no one while its replica still runs. A pinned turn tool's calls
// (#2320), a session's Monitor commands and this replica's BridgeCalls on HOST_QUEUE, and AgentSession's turns on SESSION_HOST_QUEUE, limited to the warm
// cache's largest setting plus two for releases. Their work dies with the pod; a call scheduled after
// that fails at its schedule-to-start (a turn) or schedule-to-close (a pinned call) timeout.
const hostWorkers =
  temporal && operationStore && turnToolDeps
    ? startHostWorkers({
        before: untilReady,
        address: temporal.address,
        namespace: config.temporalNamespace,
        identity: replicaIdentity(),
        pinned: {
          ...turnToolActivities(turnToolDeps),
          default: turnToolActivity(turnToolDeps),
          // A session's Monitor commands, in its directory on this replica.
          ...(background ? { bg_monitor_command: background.bg_monitor_command } : {}),
          // Other replicas' browser_* calls for a tab connected here (#4780).
          // Its tabs are claimed once this worker polls (startHosting, below).
          ...tabRelay.hostActivities(),
        },
        // This replica's own browser_* calls for a tab on another replica (#5862): their
        // workflow tasks never wait on the versioned agent-tools queue's routing.
        pinnedWorkflows: { workflowBundle: { codePath: fileURLToPath(new URL('./temporal/bridge-bundle.js', import.meta.url)) } },
        ...(sessionHosting
          ? {
              sessions: {
                activities: { ...sessionTurnActivity(sessionHosting.turnHost, temporalClient ? { client: temporalClient } : {}), ...releaseActivities(sessionHosting.warm), ...(sessionAcquire ?? {}) },
                // `warm_max_per_replica` may be raised while the replica runs; the slots cannot be.
                slots: SESSION_HOST_SLOTS,
                // The turns' own drain and abort wait (TurnHost.stop), so the worker never cuts one first.
                shutdownGraceMs: TURN_DRAIN_MS + TURN_ABORT_WAIT_MS,
                shutdownForceMs: TURN_DRAIN_MS + TURN_ABORT_WAIT_MS + 1_000,
              },
            }
          : {}),
        ...(workerDataConverter ? { dataConverter: workerDataConverter } : {}),
      })
    : undefined
// A durable session's pending_input Query, respond and cancel_input Updates (spec §6.6), and
// a recorded entry's decision in its row (R5, plan Step 9.7).
if (temporalClient) sessions.durable = new DurableGate(temporalClient, { db: gateDb })
// The browser relay's calls go through Temporal only sealed, each under a key of its own
// that is deleted once the caller has the answer (#4780): so only with the codec. This replica
// claims its tabs only once its host worker polls HOST_QUEUE with bridge.tab: a claim before (or
// with a worker that never started) would send other replicas' calls to wait out the ack timeout
// (#4806 review). `hostTabs` sets the client first; a replica with no Temporal client has none, so
// `startHosting` refuses and logs it once (#4949, #5421). Its poll stops on shutdown, so a worker
// that never polled does not start hosting while the process closes (#5432).
const stopTabHosting = hostTabs(tabRelay, {
  // A browser call's key, gone once its caller has the answer: this process's cached copy first.
  sealing:
    temporalClient && dataConverter && payloadKeys
      ? {
          client: temporalClient,
          dropKey: (subject) => {
            payloadKeys.forget(subject)
            return keyDeletes.dropBridgeKey(subject)
          },
        }
      : undefined,
  worker: hostWorkers?.pinned,
})
// A durable session's turns (plan 5c PR 3): only with the codec, so a session's
// payloads are never sent to Temporal unsealed; without it a durable start is refused.
if (temporalClient && payloadKeys) {
  sessions.durableTurns = new DurableTurns({
    client: temporalClient,
    blobs: sessionImages,
    turns: turnRows,
    events: sessions.events,
    pendingRoutes,
    // A unified session is ready while some replica hosts its turns (§5.2), this one or a worker's (§14.3).
  })
}
// Every 30 s: sessions left `running` whose workflow has ended or never started (#2001).
const stopDurableSweep =
  role.http && temporalClient
    ? new DurableRunningSweep({
        turns: turnRows,
        events: sessions.events,
        describe: durableDescriber(temporalClient),
      }).start(SESSION_REAP_MS, {
        ready: dataHealth.ready,
        onError: (err) => console.error('durable running sweep failed:', (err as Error).message),
      })
    : undefined
// Every 30 s: pending-input entries whose workflow run ended without resolving them.
const stopOrphanSweep =
  role.http && temporalClient
    ? new PendingInputSweep({ describe: temporalDescriber(temporalClient), events: sessions.events, routes: pendingRoutes }).start(APPROVAL_SWEEP_MS, {
        ready: dataHealth.ready,
        onError: (err) => console.error('pending-input orphan sweep failed:', (err as Error).message),
      })
    : undefined
// At start and every 5 min: the upstream MCP sessions of replicas that no longer poll
// agent-tools (plugins/pluginSessions.ts); skipped, and logged, when Temporal cannot say who polls.
const PLUGIN_SESSION_SWEEP_MS = 5 * 60_000
const stopPluginSessionSweep = (() => {
  const deps = { plugins, log: (message: string) => console.error(message) }
  let ownEnded = false
  const sweep = async () => {
    if (!(await dataHealth.ready())) return
    // Once, first: what this hostname's previous incarnation left; nothing of this one's is live yet.
    if (!ownEnded) {
      const mark = await pluginSessionMark
      if (mark) await endOwnSessions(pluginSessionRows, replicaIdentity(), deps, mark)
      ownEnded = mark !== undefined
    }
    await runPluginSessionSweep({ ledger: pluginSessionRows, client: temporalClient, ...deps })
  }
  const run = () => void sweep().catch((err: unknown) => console.error('plugin session sweep failed:', (err as Error).message))
  run()
  const timer = setInterval(run, PLUGIN_SESSION_SWEEP_MS)
  timer.unref()
  return () => clearInterval(timer)
})()
const commands =
  temporalClient && operationStore
    ? new AgentCommands({
        client: temporalClient,
        store: operationStore,
        kinds: commandKinds,
        searchAttributes: config.temporalSearchAttributes,
      })
    : undefined

// Web Push (#815 §3, notify/): each request waiting on the person (an approval, a question
// or an attention request) pushed once to every subscribed browser,
// so a closed tab still alerts. Its keys are sealed by the API (area `notify`, #5299 step 5).
// A pass runs on each pending-input change and once the API lists every area.
const pushStore = role.http ? new CrudPushStore(crudTarget) : undefined
// The out-of-browser channels (#815, notify/channels.ts): their tokens are sealed too.
const notifyChannels = role.http ? new ChannelStore(new CrudChannelRows(crudTarget)) : undefined
const pushDispatcher = pushStore
  ? new NotificationDispatcher({
      store: pushStore,
      notices: async (owner) => pendingNotices(await pendingInput(sessions, owner)),
      // #6399: the flow grants (the API's, through area `notify`) and the pairing requests.
      waiting: waitingNotices([crudGrantRequests(crudTarget), pairingRequests(pairingStore)]),
      pollLease: crudPollLease(crudTarget),
      // RFC 8292 §2.1: who the push service may contact about these messages.
      subject: config.publicUrl ?? 'mailto:scadbuddy@localhost',
      // The push-service allowlist, as Settings last saved it (notify/pushEgress.ts).
      hosts: async () => pushHostsOf(await settings.get<unknown>(SETTING_PUSH_HOSTS)),
      channels: notifyChannels,
      publicUrl: config.publicUrl,
    })
  : undefined
if (pushDispatcher) {
  pendingChanges.subscribe(() => pushDispatcher.wake())
  void untilAreas().then(() => pushDispatcher.wake())
  // Nothing here hears a flow grant or a pairing request change (#6399): a pass now and then.
  setInterval(() => pushDispatcher.wake('waiting'), WAITING_POLL_MS).unref()
}

const app = !role.http
  ? undefined
  : createApp({
      database: dataHealth,
      backend: () => backendReachable(backend),
      workspaceBytes: workspaceBytesStatus,
      credentials,
      access,
      plugins,
      pluginOAuth,
      pluginForwarder,
      listings,
      pluginPackages,
      pluginFeeds: new CrudPluginFeeds(crudTarget),
      packageInstaller,
      verifiedPackageReview,
      packageLoadCheck: packagesLoadCheck,
      settings,
      tokens,
      aiSettings: settings,
      testConnection: async (credential) => {
        // The model Settings chose (routes/model.ts, #1917), as every turn uses it.
        const model = storedModel(await settings.get<unknown>(SETTING_MODEL))
        return testConnection(credential, { paths, ...(model ? { model } : {}) })
      },
      origins: originPolicy(config.publicUrl, config.trustedProxies, config.allowedOrigins),
      approvals: sessions.approvals,
      sessions,
      attachments,
      // The headless browser's lists and approvals (#982, routes/browserOrigins.ts).
      browserOrigins,
      // The panel's Files (#2329, routes/sessionFiles.ts).
      workspace,
      // A file picked from a workspace (#4703) is refused when it holds one of these: every saved
      // Claude credential and every plugin secret, as a turn's upload tools search for them.
      // Read fresh, not through the 30 s cache: a pick is rare, and a secret stored a moment ago counts.
      workspaceSecrets: agentSecrets.fresh,
      // Settings → Orders' messaging binding check and test send (#5660, routes/messagingBinding.ts).
      orderMessaging: toolServices.messaging,
      orderSends: { backend: toolServices.backend, messaging: toolServices.messaging },
      // The invoice page's Import button (#6267, routes/invoiceImport.ts).
      invoiceImport: { backend: toolServices.backend },
      // The panel's background tasks (routes/backgroundTasks.ts).
      ...(backgroundTasks ? { backgroundTasks } : {}),
      audit,
      pendingChanges,
      ...(pushStore && pushDispatcher ? { push: { store: pushStore, dispatcher: pushDispatcher } } : {}),
      ...(notifyChannels ? { notifyChannels } : {}),
      publicUrl: config.publicUrl,
      upgradeWebSocket,
      tabs,
      // A person's session on /api/v1/ai/* (#4975 phase 5), dropped when the backend revokes it.
      ...(browserSessions ? { browserSessions } : {}),
      authMode: config.authMode,
      // The shared agent-tools worker's state; an `api` process has its own host queue's instead.
      ...(temporalWorker ? { temporal: () => temporalWorker.state() } : hostWorkers ? { temporal: () => hostWorkers.pinned.state() } : {}),
      commands,
      remoteAddress: (c) => {
        try {
          return getConnInfo(c).remote.address
        } catch {
          return undefined
        }
      },
      // The tools a flow may call at their tiers now, for the API beside it (#2704, routes/flowTools.ts): loopback only.
      flowTools: { tools: ALL_TOOLS, services: toolServices },
      mcp: {
        tools: ALL_TOOLS,
        resources,
        services: toolServices,
        // The tokens are area `mcp-tokens`. Until the API lists every area /mcp answers 503
        // before auth (app.ts).
        tokens,
        audit,
        authSettings,
        oidc: oidcProvider,
        publicUrl: config.publicUrl,
        relay: mcpRelay,
      },
      mcpOidc: { repo: oidcRepo, provider: oidcProvider, publicUrl: config.publicUrl },
    })
// A worker process (SCADBUDDY_AGENT_ROLE=worker) serves only its health on the same port
// (runtime/health.ts): /readyz once Temporal lists it on every queue it polls.
const health = role.http
  ? undefined
  : workerHealthApp({
      database: dataHealth,
      workers: () =>
        Object.fromEntries(
          (
            [
              [TASK_QUEUE, temporalWorker],
              [SESSION_QUEUE, sessionWorker],
              [HOST_QUEUE, hostWorkers?.pinned],
              [SESSION_HOST_QUEUE, hostWorkers?.sessions],
            ] as const
          ).flatMap(([queue, worker]) => (worker ? [[queue, worker.state()]] : [])),
        ),
      ready: () => {
        if (!temporalClient) return Promise.resolve('SCADBUDDY_TEMPORAL_ADDRESS is needed to run turns')
        // Its workers poll only once the API lists every area and has released the key (`untilReady`).
        if (!crudAreas.complete) return Promise.resolve("the API does not list every one of the agent's CRUD areas yet; its start log says which")
        if (!inputHashKey.isReleased()) return Promise.resolve('the API has not released the input-hash key yet; its start log says why')
        // A worker this process never started (it lacked what its start log names) is said so, not waited for.
        const queues: PolledQueue[] = [
          { name: SESSION_QUEUE, type: 1, started: sessionWorker !== undefined },
          { name: TASK_QUEUE, type: 2, started: temporalWorker !== undefined },
          { name: HOST_QUEUE, type: 2, started: hostWorkers?.pinned !== undefined },
          { name: SESSION_HOST_QUEUE, type: 2, started: hostWorkers?.sessions !== undefined },
        ]
        // Ready once this build is current too (plan Phase 11): a rollout then stops an old
        // replica only after Temporal hands the shared queues' tasks to this build.
        const notCurrent = [sessionWorker, temporalWorker].some((worker) => worker !== undefined && !worker.current())
        if (notCurrent) return Promise.resolve(`build ${config.buildId} is not yet current for ${SESSION_DEPLOYMENT}`)
        return queuesPolled(temporalClient, queues, workerIdentity(replicaIdentity()))
      },
    })

// The chat socket (routes/chat.ts). A frame is one panel message; CHAT_FRAME_MAX
// covers the largest (a 32k-character message with its page context). Images are
// uploaded, not framed (#1941, routes/attachments.ts).
const wss = new WebSocketServer({ noServer: true, maxPayload: CHAT_FRAME_MAX })
const stopHeartbeat = startHeartbeat(wss)

traceListener(PORT)
const server = serve({ fetch: (app ?? health!).fetch, hostname: '0.0.0.0', port: PORT, websocket: { server: wss } }, (info) => {
  console.log(
    `scadbuddy-agent (role ${config.agentRole}) listening on :${info.port}; backend ${config.backendUrl}; ` +
      (app ? `credential writes from ${config.publicUrl ?? 'loopback only (SCADBUDDY_PUBLIC_URL unset)'}` : 'health only'),
  )
})

// Drain the listener first (bounded, see shutdown.ts), then the last publishes,
// then exit: non-zero when the drain timed out and requests were cut.
for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.once(signal, () => void stop())
}

async function stop(): Promise<void> {
  stopTabHosting()
  stopSweeper?.()
  stopEscalationSweeper?.()
  stopOrphanSweep?.()
  stopPluginSessionSweep()
  stopDurableSweep?.()
  stopRetention?.()
  stopAttachmentSweep?.()
  stopWorkspaceSweep?.()
  stopWarmSync()
  crudAreas.stop()
  inputHashKey.stop()
  // Running turns first, while the panel's socket, the paired tab and the API's
  // routes are all still up: no new turn starts, running ones may finish, the rest are
  // aborted and record that they were (SessionManager.stopTurns). Aborted
  // turns' pending approvals stay pending (approvals/service.ts).
  // The agent-tools worker at the same time: it stops polling, its running
  // activities get its 10 s grace and are then cancelled (a fetch's git is killed),
  // and at 15 s it stops regardless: inside the turns' 17 s budget. The session host worker stops
  // with it, with the turns' 17 s as its grace; the pinned host worker only once they end (below).
  // AgentSession turns get
  // TURN_DRAIN_MS as their grace (TurnHost.stop); one still running then is cut and fails
  // ShuttingDown, so its workflow places it on another replica, and one that ended in the grace
  // keeps its end. The warm processes are closed only once those have ended (stopTurns `others`).
  const hostedTurns = turnHost?.stop({ graceMs: TURN_DRAIN_MS, abortWaitMs: TURN_ABORT_WAIT_MS })
  const drained = Promise.all([
    temporalWorker?.stop(),
    hostWorkers?.sessions?.stop(),
    sessionWorker?.stop(),
    sessions.stopTurns({ graceMs: TURN_DRAIN_MS, abortWaitMs: TURN_ABORT_WAIT_MS, ...(hostedTurns ? { others: hostedTurns } : {}) }),
  ])
  // The pinned host worker only once those end (#5862 review, stopAfter): it runs this replica's own
  // BridgeCalls, whose workflow tasks a draining turn's browser_* call needs until the turn ends, and
  // its pinned calls. It runs beside shutdown() below, which closes the tabs first, so its bridge.tab
  // calls end at once ("gone"). Its stop ends within PINNED_SHUTDOWN_FORCE_MS (8 s) of the drain, and
  // shutdown()'s 10 s deadline starts later, so the last step below, which awaits it, never closes
  // the session-event publisher under a pinned call still in its grace (a Monitor's
  // appendEvent). The server's own close does not wait for it.
  const pinnedStopped = hostWorkers ? stopAfter(hostWorkers.pinned, drained) : undefined
  await drained
  // Kept plugin MCP sessions end with their DELETE while the forwarder still routes it.
  await turnClients.closeAll()
  stopSessionWake()
  stopHeartbeat()
  tabs.close()
  // 1001 "going away": the panel reconnects to another replica or after the restart.
  for (const socket of wss.clients) socket.close(1001, 'the agent service is restarting')
  // A peer that never answers the close frame would hold server.close() for
  // ws's 30 s close timeout, past the 10 s deadline.
  setTimeout(() => {
    for (const socket of wss.clients) socket.terminate()
  }, 2_000).unref()
  const result = await shutdown({
    // End the /mcp sessions and the session event streams first: their
    // standing SSE responses would otherwise hold server.close() until the deadline.
    closeSessions: async () => {
      // Memory retains started by the last turns (memory/hindsight.ts), within the same deadline.
      await drainRetains()
      // stopTurns waited for the aborted turns only so long: any still winding
      // down append their final session.status/session.done, and publish them,
      // before the last step runs; within the same deadline (#802).
      await sessions.settled()
      await app?.close()
      resources.close()
      await events.close()
    },
    closeServer: async () => {
      await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
      await pluginForwarder.close()
      await stopCredentialGateway()
    },
    // The session-event publisher closes only now, after the drain: turns
    // stopped above append their final session.status/session.done while they
    // wind down (stopTurns waits for that, but only for so long), and closing
    // it first would swallow that NOTIFY, so another replica's followers would
    // never wake (#715 review; busEvents.ts).
    closeLast: async () => {
      // The pinned host worker's running activities, bounded by its 8 s force time, before the publisher closes.
      // Bounded, not prevented: an activity that ignores its cancel is abandoned at the force time but
      // runs on in-process, so it may still publish after the close (and that publish is lost) before process.exit.
      await pinnedStopped
      sessionEvents.close()
    },
    timeoutMs: 10_000,
  })
  if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
  // The last spans (this shutdown's turns among them), within 2 s of the
  // pod's grace period. Without --import no SDK started and this is a no-op.
  await shutdownTelemetry()
  process.exit(result === 'clean' ? 0 : 1)
}
