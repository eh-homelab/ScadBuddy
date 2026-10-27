import { ATTACHMENT_SWEEP_MS, AttachmentStore } from './attachments/store.js'
import { serve, upgradeWebSocket } from '@hono/node-server'
import { WebSocketServer } from 'ws'
import path from 'node:path'
import { event } from './sessions/protocol.js'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { mcpAuthSettings } from './auth/authenticate.js'
import { OidcProvider, SettingsOidcConfigRepo } from './auth/oidc.js'
import { approvalGrantCheck, FailClosedTokenStore, liveTokenTiers, PostgresTokenStore } from './auth/tokens.js'
import { loadConfig } from './config.js'
import { BrowserOrigins } from './browser/store.js'
import { CredentialStore, SettingsStore } from './credentials.js'
import { CredentialPool } from './harness/fallback.js'
import { nestedQueries } from './builtins/nested.js'
import { managerAgents } from './background/workflowAgents.js'
import { backgroundActivities, managerSender } from './background/activities.js'
import { agentTimezone } from './background/schedule.js'
import { BackgroundTasks } from './background/service.js'
import { BackgroundTaskStore } from './background/store.js'
import { temporalTaskRunner } from './background/temporal.js'
import { connectDatabase } from './db.js'
import { MigrationChecksumError, MigrationLedgerError } from './db/migrations.js'
import { PgEventListener } from './events/pgListener.js'
import { DEFAULT_STATE_DIR, pluginCacheDir } from './harness/options.js'
import { OWN_PLUGIN_DIR } from './harness/ownPlugin.js'
import { ensureStateDirs, removeLegacyBrowserDir, StateDirError } from './harness/stateDirs.js'
import { testConnection } from './harness/testConnection.js'
import { originPolicy } from './http/origins.js'
import { forwardForRun, PluginForwarder } from './plugins/forwarder.js'
import { endOwnSessions, PluginSessions, replicaIdentity, runPluginSessionSweep, startMark } from './plugins/pluginSessions.js'
import { GitFetcher } from './plugins/packages/git.js'
import { loadPackagesForRun, PackageInstaller, warmPackages } from './plugins/packages/install.js'
import { PackageStore } from './plugins/packages/store.js'
import { BuiltInTools } from './plugins/builtInTools.js'
import { loadEnabledPlugins, PluginStore } from './plugins/registry.js'
import { ResourceHub } from './resources/hub.js'
import { loadKek } from './secrets.js'
import { sealBackfill } from './sessions/sealBackfill.js'
import { ApprovalActions } from './approvals/mcp.js'
import { approvalHashKey, sweepErrorText } from './approvals/service.js'
import { AuditLog } from './audit/log.js'
import { auditedTokenStore } from './audit/writes.js'
import { TabHub } from './bridge/hub.js'
import { PgTabRelay } from './bridge/pgRelay.js'
import { TemporalTabRelay } from './bridge/relay.js'
import { PostgresTabOwnerStore } from './bridge/tabOwners.js'
import { PostgresSessionTabStore } from './bridge/sessionTabs.js'
import { PgMcpSessionRelay } from './mcp/sessionRelay.js'
import { PostgresPairingStore } from './bridge/pairings.js'
import { CHAT_FRAME_MAX, startHeartbeat } from './routes/chat.js'
import { storedModel } from './routes/model.js'
import { followSessionEvents, SessionEventPublisher } from './sessions/busEvents.js'
import { resolveTabWaits, SessionManager, SETTING_MODEL } from './sessions/manager.js'
import { poolRemainingUsd } from './sessions/turnShared.js'
import { readStartSettings, wireRuntimeSync } from './sessions/runtimeSync.js'
import { drainRetains, type MemoryOptions } from './memory/hindsight.js'
import { shutdown } from './shutdown.js'
import { shutdownTelemetry, traceListener } from './telemetry/runtime.js'
import { harnessTools } from './tools/harness.js'
import { SessionResources } from './sessions/touched.js'
import { ALL_TOOLS } from './tools/index.js'
import { PgFlowBlobs } from './temporal/flowBlobs.js'
import { PgFlowRuns } from './temporal/flowRuns.js'
import { flowSessionActivities } from './temporal/flowSessions.js'
import { decidedCalls, gateActivities, PgApprovalRecords, PgSessionOwners, type ToolActivityDeps, toolActivities } from './temporal/toolActivities.js'
import { TASK_QUEUE } from './temporal/names.js'
import { sessionFinishActivities } from './temporal/session/finishActivity.js'
import { sessionGateActivities } from './temporal/session/gateActivities.js'
import { acquireActivities, liveHostQueues, releaseActivities } from './temporal/session/affinity.js'
import { startHostWorkers } from './temporal/session/hostWorkers.js'
import { SESSION_DEPLOYMENT, SESSION_SETTINGS_ACTIVITY, SESSION_QUEUE, sessionHostQueue } from './temporal/session/names.js'
import { sessionTurnActivity } from './temporal/session/turnActivity.js'
import { TurnHost } from './sessions/turnHost.js'
import { readRuntimeSettings, RUNTIME_BOUNDS } from './sessions/runtimeSettings.js'
import { memoryActivities } from './temporal/memoryActivities.js'
import { SHARED_PACKAGES, sweepPackageCopies } from './temporal/turnPackages.js'
import { forwardPackageServers } from './plugins/packages/mcp.js'
import { subagentKey } from './temporal/subagentToken.js'
import { pgRunning } from './routes/subagentMcp.js'
import { DURABLE_TURNS_DIR, sweepTurnDirs, turnServers } from './temporal/turnServers.js'
import { SessionClients } from './temporal/sessionClients.js'
import { type ListedTools, turnToolActivities, turnToolActivity } from './temporal/turnTools.js'
import { DURABLE_TOOLS } from './tools/manifest.js'
import { PgAnswers } from './gate/answers.js'
import { AgentWorker, workerIdentity } from './temporal/worker.js'
import {
  dropPayloadKey,
  FilePayloadStore,
  PgPayloadKeys,
  rewrapPayloadKeys,
  subjectDataConverter,
  sweepBridgeKeys,
} from './temporal/payloadCodec.js'
import { DurableGate } from './gate/durable.js'
import { SessionBlobs } from './sessions/blobs.js'
import { DURABLE_TASK_QUEUE, DurableTurns } from './sessions/durable.js'
import { DurableRunningSweep, durableDescriber } from './sessions/durableSweep.js'
import { sweepOrphanedSessionKeys } from './sessions/forget.js'
import { PendingInputSweep, temporalDescriber } from './gate/sweep.js'
import { Runtime } from '@temporalio/worker'
import { Client, Connection } from '@temporalio/client'
import { fileURLToPath } from 'node:url'
import { AgentCommands } from './operations/run.js'
import { OperationStore } from './operations/store.js'
import { activityKindsOf, upsertActivityKinds } from './activity/kinds.js'
import { packageKinds } from './plugins/packages/operations.js'
import { operationActivities } from './temporal/operationActivities.js'
import { PendingActionStore } from './tools/pending.js'
import type { ToolServices } from './tools/registry.js'
import { RenderLimiter, settingsRenderLimits } from './tools/renderLimits.js'
import { SessionFiles, WORKSPACE_SWEEP_MS } from './workspace/store.js'
import { toolSwitches } from './tools/switches.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the infrastructure variables in config.ts.
const PORT = 8081
/** How often approvals nobody is waiting on are expired (approvals/service.ts). */
const APPROVAL_SWEEP_MS = 30_000
/** How often sessions whose turn died with its lease are ended (SessionManager.reapExpired). */
const SESSION_REAP_MS = 30_000
// The shutdown's budget for running turns, inside the pod's 30 s
// terminationGracePeriodSeconds (clusters, strategy Recreate): TURN_DRAIN_MS
// for them to finish, TURN_ABORT_WAIT_MS for the rest to record that they were
// stopped, then shutdown()'s own 10 s.
const TURN_DRAIN_MS = 12_000
const TURN_ABORT_WAIT_MS = 5_000
/** How often audit rows past their retention are deleted (audit/log.ts). */
const AUDIT_RETENTION_SWEEP_MS = 60 * 60_000

const config = loadConfig()

// Before listening: a mounted, empty state volume must get its `claude/` and
// `work/` back, and an unwritable one should stop the pod here, visibly.
try {
  await ensureStateDirs({ stateDir: DEFAULT_STATE_DIR })
} catch (err) {
  console.error(err instanceof StateDirError ? err.message : err)
  process.exit(1)
}
// The removed built-in headless browser's folders, left on the volume by an older image.
await removeLegacyBrowserDir({ stateDir: DEFAULT_STATE_DIR }).catch((err: unknown) =>
  console.error(`cannot remove leftover headless-browser folders: ${String(err)}`),
)

// Read once at start: rotating the key means restarting the pod (spec §9).
// A missing or malformed file is not fatal; /healthz and Settings say why
// (without the path or errno, which only this log names).
const kek = await loadKek(config.secretKeyFile)
if (!kek.ok) console.error(`secret key: ${kek.detail ?? kek.reason}; saving Claude credentials is disabled`)

// Rotation (spec §9, "Rotating it re-wraps the data keys only"): mount the new
// key as SCADBUDDY_SECRET_KEY_FILE and the old one as
// SCADBUDDY_SECRET_KEY_PREVIOUS_FILE; once migrations have applied, every row
// sealed under the old key is re-wrapped under the new one. Then drop the old.
const previousKek = config.previousSecretKeyFile
  ? await loadKek(config.previousSecretKeyFile, 'SCADBUDDY_SECRET_KEY_PREVIOUS_FILE')
  : undefined
if (previousKek && !previousKek.ok) {
  console.error(`previous secret key: ${previousKek.detail ?? previousKek.reason}; nothing will be re-wrapped`)
}

const database = config.databaseUrl
  ? connectDatabase(config.databaseUrl, {
      onMigrationError: (err) => {
        console.error('database migrations failed:', (err as Error).message)
        // An edited migration or an unreadable ledger is not a transient failure: stop, visibly.
        if (err instanceof MigrationChecksumError || err instanceof MigrationLedgerError) process.exit(1)
      },
      afterMigrate: async (sql) => {
        if (!previousKek?.ok || !kek.ok) return
        const { rewrapped, failed } = await new CredentialStore(sql).rewrapFrom(previousKek.kek, kek.kek)
        if (rewrapped || failed) {
          console.log(
            `secret key rotation: re-wrapped ${rewrapped} credential(s) from key ${previousKek.kek.id} to ${kek.kek.id}` +
              (failed ? `; ${failed} could not be opened with the previous key and were left as they are` : ''),
          )
        }
        const payloadKeys = await rewrapPayloadKeys(sql, previousKek.kek, kek.kek)
        if (payloadKeys.rewrapped || payloadKeys.failed) {
          console.log(
            `secret key rotation: re-wrapped ${payloadKeys.rewrapped} payload key(s)` +
              (payloadKeys.failed ? `; ${payloadKeys.failed} could not be opened with the previous key` : ''),
          )
        }
        const plugins = await new PluginStore(sql).rewrapFrom(previousKek.kek, kek.kek)
        if (plugins.rewrapped || plugins.failed) {
          console.log(
            `secret key rotation: re-wrapped ${plugins.rewrapped} plugin secret(s)` +
              (plugins.failed ? `; ${plugins.failed} could not be opened with the previous key` : ''),
          )
        }
      },
    })
  : undefined
// Migrate in the background: a database that is down at start-up is retried
// by the next /healthz or API call instead of stopping the pod.
void database?.ready()
const credentials = database ? new CredentialStore(database.sql) : undefined
// The audit log of AI actions (#258, audit/log.ts). Its input hashes use the
// approvals' key, so a tool call's row carries the same hash as its approval.
// The settings store audits its own writes into it, so it comes second; the
// log reads its retention through the thunk.

const audit = database
  ? new AuditLog({
      sql: database.sql,
      settings: (): SettingsStore | undefined => settings,
      ...(kek.ok ? { hashKey: approvalHashKey(kek.kek) } : {}),
      onError: (err, entry) =>
        console.error(`audit log: could not record ${entry.kind} ${entry.action}:`, (err as Error).message),
    })
  : undefined
const settings: SettingsStore | undefined = database ? new SettingsStore(database.sql, audit) : undefined
// OIDC for /mcp (#262): the configuration in `ai_settings`, one verifier with
// its metadata and JWKS caches for the process.
const oidcRepo = settings
  ? new SettingsOidcConfigRepo(settings, (detail) => console.error(`mcp auth: ${detail}`))
  : undefined
const oidcProvider = new OidcProvider()
const plugins = database ? new PluginStore(database.sql) : undefined
// ScadBuddy's own tool sets as plugins (#1953): their overrides, in ai_settings.
const builtInTools = new BuiltInTools(settings)
// Plugin traffic (connection tests, and each session turn's enabled plugins)
// goes through this loopback forwarder (plugins/forwarder.ts).
const pluginForwarder = await PluginForwarder.start({
  ...(database ? { sessions: new PluginSessions(database.sql) } : {}),
  log: (message) => console.error(message),
})
// The database's clock the moment it is ready, before a turn can record a session: the pass at
// start ends only this replica's rows older than it (plugins/pluginSessions.ts `endOwnSessions`).
const pluginSessionMark = database ? database.ready().then((ok) => (ok ? startMark(database.sql) : undefined)) : undefined
const backend = createBackendClient(config.backendUrl)

// The event bus (spec §7, #264): LISTEN on `scadbuddy_events` on a connection
// of its own, retried in the background, feeding MCP resource subscriptions.
const events = config.databaseUrl ? new PgEventListener(config.databaseUrl) : undefined
// This replica's own queue: a pinned turn tool's calls (#2320), a classic session's
// Monitor commands, which run where its turns' Bash runs, and the browser_* calls other
// replicas make for a tab connected here (#4780).
const HOST_QUEUE = `${TASK_QUEUE}.${replicaIdentity()}`
// browser_* calls for a tab connected to another replica (#4780, bridge/relay.ts): an
// activity on the owner's HOST_QUEUE, through Temporal, sealed. Its client is set once made
// (temporalClient below), only with the KEK. For one release (#4868) the Postgres relay
// (#1916, bridge/pgRelay.ts) runs beside it, for replicas still on the old build: on the
// listener's connection, made before start(), so its first LISTEN covers it.
const tabRelay = database
  ? new TemporalTabRelay({
      owners: new PostgresTabOwnerStore(database.sql),
      hostQueue: HOST_QUEUE,
      ready: database.ready,
      fallback: events ? new PgTabRelay(database.sql, { listen: (c, f) => events.listenAlso(c, f) }) : undefined,
    })
  : undefined
// The rows a process before this one left under the same identity (a container restart).
void tabRelay?.releaseAll()
// /mcp requests for a session another replica holds (#2086, mcp/sessionRelay.ts), on
// the listener's connection: made before start(), so its first LISTEN covers it.
const mcpRelay = database && events ? new PgMcpSessionRelay(database.sql, { listen: (c, f) => events.listenAlso(c, f) }) : undefined
events?.start()
const resources = new ResourceHub(events)
const paths = { stateDir: DEFAULT_STATE_DIR }
// The /mcp auth settings (auth/authenticate.ts `mcpAuthSettings`): `oidc` while
// `ai_settings.mcp_oidc` is enabled (#262), otherwise the `mcp_auth_mode` and
// `mcp_anonymous_cap` keys. One reader for /mcp, per request, and for Settings
// (routes/mcpAuthMode.ts), so both report the same thing. A read that throws
// makes /mcp fail closed (mcp/http.ts).
const authSettings = mcpAuthSettings(settings, (message) => console.warn(`mcp auth: ${message}`), oidcRepo)
// The registry's services (#251), shared by /mcp and every session's
// in-process tools. `pending` is swapped for the ai_approvals store below once
// the sessions (and so the approval service) exist.
const toolServices: ToolServices = {
  backend,
  pending: new PendingActionStore(),
  pollIntervalMs: 1000,
  renderWaitMs: 10 * 60_000,
  publicBaseUrl: config.publicUrl,
  // The render limits in Settings, read live (tools/renderLimits.ts).
  ...(settings ? { renderLimiter: new RenderLimiter(settingsRenderLimits(settings)) } : {}),
  // The tools Settings can turn off (#1911): read from ai_settings at each call.
  switchedOff: toolSwitches(settings),
  // Settings → Plugins' raised tiers and disabled tools (#1953), read at each call.
  toolOverrides: builtInTools.registryOverrides(),
}
// The browser bridge (#254, bridge/hub.ts): the tabs connected over
// /api/v1/ai/bridge, which the browser_* tools drive; MCP clients pair with
// one through `ai_browser_pairings` (spec §8.5).
const tabs = new TabHub({
  pairings: database ? new PostgresPairingStore(database.sql) : undefined,
  relay: tabRelay,
  // A durable session's browser_* calls run on whichever replica took the
  // activity, so the tab its chat socket paired is read from Postgres (#2086).
  sessionTabs: database ? new PostgresSessionTabStore(database.sql) : undefined,
})
toolServices.browser = tabs
// What each session touched (#931, sessions/touched.ts), from its tool calls.
if (database) {
  toolServices.touched = new SessionResources(database.sql, (err) =>
    console.error('session resources: could not record a call:', (err as Error).message),
  )
}
// The assistant's workspace (#2329, workspace/store.ts): a session's files, or an
// /mcp caller's own outside a session, in ai_session_files. The workspace_* tools
// and http_request's save_to_workspace write it; a principal's files are swept
// 24 h after their last write.
const workspace = database ? new SessionFiles(database.sql, { settings }) : undefined
if (workspace) toolServices.workspace = workspace
// What workspace_read redacts from text in every mode (classic, durable, /mcp):
// each saved Claude credential's secret, read at the call (a classic turn adds its
// plugin tokens, tools/harness.ts). None without the KEK, which opens them.
if (credentials && kek.ok) {
  const key = kek.kek
  toolServices.secrets = async () =>
    (await credentials.revealAll(key)).flatMap((row) => (row.credential ? [row.credential.secret] : []))
}
const stopWorkspaceSweep = workspace?.startSweeper(WORKSPACE_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('workspace sweep failed:', (err as Error).message),
})
// Plugin packages (#297): the pin is in Postgres (`ai_plugin_packages`); the
// files under <state dir>/plugins are a cache, rebuilt from the pin and
// verified against its content hash before each load (plugins/packages/).
// Each session turn loads the enabled ones (`packagePlugins` below).
const pluginPackages = database ? new PackageStore(database.sql) : undefined
const packageInstaller = new PackageInstaller({ fetcher: new GitFetcher(), cacheRoot: pluginCacheDir(paths) })
// The cache is this pod's scratch, empty after every deploy: filled at start, so a
// turn does not wait on a whole repository's fetch (#4217).
if (database && pluginPackages) {
  void database
    .ready()
    .then(async (ok) => {
      if (!ok) {
        console.warn('plugin package warm-up skipped: the database is not ready; the first turn fetches instead')
        return
      }
      for (const problem of await warmPackages(pluginPackages, packageInstaller)) {
        console.warn('plugin package warm-up:', problem)
      }
    })
    .catch((err: unknown) => console.error('plugin package warm-up failed:', (err as Error).message))
}

// One store for Settings (routes/mcpTokens.ts) and /mcp. Mint and revoke are
// recorded in the audit log (#258), whichever of the two makes them.
const tokens =
  database && audit ? auditedTokenStore(new PostgresTokenStore(database.sql), audit) : new FailClosedTokenStore()

// `session.*` on the event bus (#300, sessions/busEvents.ts): every event-log
// append is announced on `scadbuddy_events`, and this replica's LISTEN
// consumer (below) wakes its followers for sessions other replicas write.
const sessionEvents = database ? new SessionEventPublisher(database.sql) : undefined

// A session's background tasks (CronCreate, ScheduleWakeup, Monitor, TaskStop), in either mode:
// each a `BackgroundTask` workflow on agent-tools that sends its prompt when due. Its
// payloads are task ids only, so its client has no codec.
const backgroundTasks =
  config.temporalAddress && database
    ? new BackgroundTasks({
        store: new BackgroundTaskStore(database.sql),
        runner: temporalTaskRunner(
          new Client({ connection: Connection.lazy({ address: config.temporalAddress }), namespace: config.temporalNamespace }),
          TASK_QUEUE,
        ),
      })
    : undefined
// Sessions (#300) and their approvals (#258): started from the assistant
// panel's socket (routes/chat.ts), the session routes (routes/sessions.ts)
// and the `sessions_*` tools (tools/sessions.ts).
// The approval routes and the expiry sweep also serve approvals left pending
// by a restart.
const runtime = await readStartSettings({ database, settings }, console.log)
// The data keys that seal a session's transcript (D7) and, below, its durable payloads: one
// instance, so forgetting a subject drops its key from the one cache.
const dataKeys = database && kek.ok ? new PgPayloadKeys(database.sql, kek.kek, { previous: previousKek?.ok ? previousKek.kek : undefined }) : undefined
if (database && !dataKeys) console.warn('sessions: no secret key, so no session transcript can be written; sessions cannot run')
// Seals the transcripts written before sealing, in the background once migrations have applied; a failure is retried at the next start.
if (database && dataKeys) {
  void database
    .ready()
    .then(async (ready) => {
      if (!ready) return
      const { sealed, deleted } = await sealBackfill(database.sql, dataKeys)
      if (sealed || deleted) console.log(`session transcripts: sealed ${sealed} row(s); deleted ${deleted} of forgotten sessions`)
    })
    .catch((err: unknown) => console.error('session transcripts: sealing the existing rows failed; retried at the next start:', (err as Error).message))
} else if (database) {
  console.warn('session transcripts: no secret key, so the existing rows are not sealed')
}
// Automatic Hindsight memory's hook knobs: one value for classic turns (SessionManager)
// and durable ones (memoryActivities), so a knob set here applies to both (#2420).
// Undefined: the defaults.
const memory: MemoryOptions | undefined = undefined

const sessions =
  database && credentials
    ? new SessionManager({
        memory,
        sql: database.sql,
        paths,
        ...(dataKeys ? { payloadKeys: dataKeys } : {}),
        ...(settings ? { settings } : {}),
        // ScadBuddy's tools and their tiers (tools/harness.ts).
        ...harnessTools(toolServices),
        // Settings → Plugins' overrides of the built-in tools (#1953), per turn.
        builtInTools: () => builtInTools.policy(),
        // ScadBuddy's own plugin (#896, harness/ownPlugin.ts): its skills and
        // subagents, with the Skill and Agent tools they need.
        ownPlugin: OWN_PLUGIN_DIR,
        // Input hashes are HMACs under a key derived from the KEK, so they
        // compare across restarts (approvals/service.ts BINDING).
        ...(kek.ok ? { approvalHashKey: approvalHashKey(kek.kek) } : {}),
        // Other agents decide approvals only with their token's grant (spec §6, #300).
        approvalGrants: approvalGrantCheck(tokens),
        // A resumed approval's turn gets no more than its token holds now (#300).
        currentTiers: liveTokenTiers(tokens),
        ...(sessionEvents ? { onAppend: sessionEvents.onAppend } : {}),
        // Every tool call a turn makes, and every approval decision (#258).
        ...(audit ? { audit } : {}),
        // Enabled plugins (#297), per turn, through the loopback forwarder.
        ...(plugins
          ? {
              remotePlugins: async () =>
                forwardForRun(await loadEnabledPlugins(plugins, kek.ok ? kek.kek : undefined), pluginForwarder, { record: true }),
            }
          : {}),
        // Enabled plugin packages (#297), materialised from their pins, per turn.
        ...(pluginPackages ? { packagePlugins: () => loadPackagesForRun(pluginPackages, packageInstaller) } : {}),
        packageServers: (loaded) => forwardPackageServers(loaded, pluginForwarder, undefined, { record: true }),
        // The http_request tool (#827): on for a turn unless the
        // `http_request_enabled` setting is false (routes/httpRequest.ts).
        httpRequest: {},
        // The workspace http_request's save_to_workspace writes, and a fork copies (#2329).
        ...(workspace ? { workspace } : {}),
        // Claude Code's CronCreate and the rest, kept past the turn (src/background/).
        ...(backgroundTasks ? { background: { tasks: backgroundTasks, tz: agentTimezone(), commandQueue: HOST_QUEUE } } : {}),
        // Every usable credential in priority order, with fallback; disables,
        // cooldowns, recoveries and fallbacks are audited (#1093).
        credentials: new CredentialPool({ repo: credentials, kek, audit }),
        // One warm Claude Code per classic session (unified runtime spec 2026-10-13
        // §5.3, sessions/warm.ts), sized by the runtime settings (sessions/runtimeSettings.ts).
        warm: { maxEntries: runtime.warm_max_per_replica, idleMs: runtime.warm_idle_seconds * 1000, log: (message) => console.log(message) },
      })
    : undefined
// A credential, plugin or package change on any replica, and a session handed
// off, marked done, archived or forgotten, evict warm processes everywhere
// (sessions/warmEvict.ts): on the event bus's connection, like the relays above.
const stopWarmSync =
  sessions && settings && events
    ? wireRuntimeSync({
        settings,
        warm: sessions.warm,
        revalidate: () => sessions.revalidateWarm(),
        events,
        log: console.log,
      })
    : undefined
// MCP prepare/confirm on ai_approvals (approvals/mcp.ts); with no database,
// the in-memory store above, whose actions are never confirmed.
if (sessions) toolServices.pending = new ApprovalActions(sessions.approvals)
// #815 §2: a session whose tab is connected again stops waiting for it.
if (sessions) resolveTabWaits(tabs, sessions)
// The `sessions_*` tools (#300) act on the same manager, over /mcp and in-process.
if (sessions) toolServices.sessions = sessions
// The panel's image uploads (#1941, attachments/store.ts): the upload route, the
// chat socket and sessions_send read them; unsent ones expire and are swept.
const attachments = sessions && database ? new AttachmentStore(database.sql) : undefined
if (attachments) toolServices.attachments = attachments
const stopAttachmentSweep = attachments?.startSweeper(ATTACHMENT_SWEEP_MS, {
  log: (message) => console.error(message),
})
// The LISTEN consumer that calls EventLog.wake() for other replicas' `session.*`.
const stopSessionWake =
  sessions && events && sessionEvents ? followSessionEvents(events, sessions.events, sessionEvents.replica) : undefined
const stopSweeper = sessions?.approvals.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('approval expiry sweep failed:', sweepErrorText(err)),
})
// Questions past `question_expiry_seconds` whose turn is gone (spec §6.6): cancelled, never answered.
const stopQuestionSweeper = sessions?.questions.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('question expiry sweep failed:', (err as Error).message),
})
// A flow run's call whose hop at its starting session is over goes to the person (#2482).
const stopEscalationSweeper = sessions?.escalations.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('escalation hop sweep failed:', (err as Error).message),
})
// Now and every 30 s: sessions whose turn died without finishing (a SIGKILL,
// or a restart that closed the pool under it) say so and stop claiming to run.
const stopReaper = sessions?.startReaper(SESSION_REAP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('session lease reaper failed:', (err as Error).message),
})
// Audit rows older than `audit_retention_days` (ai_settings) are deleted hourly.
const stopRetention = audit?.startRetention(AUDIT_RETENTION_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('audit retention sweep failed:', (err as Error).message),
})

// The agent-tools worker (spec 2026-10-01 §4.3, §6.3, #1055): every tool as an
// activity for durable sessions' workflows, and the agent's commands (plugin package
// install and re-pin, AgentOperation). It needs the database (a call runs as its
// session's owner; commands are recorded in ai_operations) and
// SCADBUDDY_TEMPORAL_ADDRESS. Its SIGTERM is ours: stop() below stops it alongside the
// turns, so the SDK's own signal handling is turned off.
const temporal = config.temporalAddress && database ? { address: config.temporalAddress, sql: database.sql } : undefined
if (config.temporalAddress && !database) console.error('agent-tools worker: not started, it needs SCADBUDDY_DATABASE_URL')
if (temporal) Runtime.install({ shutdownSignals: [] })
const operationStore = temporal ? new OperationStore(temporal.sql) : undefined
const commandKinds = pluginPackages ? packageKinds({ packages: pluginPackages, installer: packageInstaller }) : []
// The agent's activity kinds are its command kinds (spec 2026-10-09 §4), read by the backend.
if (database && commandKinds.length > 0) {
  void database
    .ready()
    .then(async (ok) => {
      if (ok) await upsertActivityKinds(database.sql, activityKindsOf(commandKinds))
    })
    .catch((err: unknown) => console.error('activity kinds: could not record them:', (err as Error).message))
}
// Durable subjects' payloads are sealed per subject (spec §6.5): every client and worker
// that encodes or decodes them carries the codec, which needs the KEK. Without one, a
// session or flow payload is never sealed here, so no durable session is started (5c Ruling 13).
const payloadKeys = temporal ? dataKeys : undefined
// A session's large payloads are files on the data share, by reference (#2243).
const payloadStore = config.payloadDir ? new FilePayloadStore(config.payloadDir) : undefined
const dataConverter = payloadKeys ? subjectDataConverter(payloadKeys, payloadStore) : undefined
if (temporal && !payloadKeys) {
  console.warn('agent-tools worker: no secret key, so durable payloads cannot be sealed; no durable session is started')
}
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
if (database) {
  const sweepRoot = path.join(paths.stateDir, DURABLE_TURNS_DIR)
  const sweep = () =>
    void sweepTurnDirs(sweepRoot, database.sql).catch((err: unknown) =>
      console.error('durable http body sweep failed:', (err as Error).message),
    )
  sweep()
  setInterval(sweep, 3600_000).unref()
}
// The payload keys of sessions whose row is gone (#3317): a durable fork whose workflow could not
// start; and of browser calls a cancel or a lost caller left (#4780). Every hour, once the database is ready.
if (database) {
  const sweep = () =>
    void database
      .ready()
      .then(async (ok) => (ok ? (await sweepOrphanedSessionKeys(database.sql)) + (await sweepBridgeKeys(database.sql)) : 0))
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
// What a durable session's tool runs with: its activities on agent-tools, and a released
// AgentSession turn's approved call (§5.4 step 5), which runs on the turn's own replica.
const durableToolDeps: ToolActivityDeps | undefined = temporal
  ? {
      services: toolServices,
      sessions: new PgSessionOwners(temporal.sql),
      approvals: new PgApprovalRecords(temporal.sql),
      flows: new PgFlowRuns(temporal.sql),
      audit,
      answers: new PgAnswers(temporal.sql),
      currentTiers: liveTokenTiers(tokens),
      // A result's images, for the durable segment's view_tool_images (#2320).
      blobs: new SessionBlobs(temporal.sql),
      // A flow call's images, sealed under its run's payload key (#2477).
      ...(payloadKeys ? { flowBlobs: new PgFlowBlobs(temporal.sql, payloadKeys) } : {}),
    }
  : undefined
// A parked call holds on its entry's row, woken by `scadbuddy_input` on the event bus's connection (§5.4).
const turnHost =
  temporal && sessions
    ? new TurnHost({
        core: sessions.turnCore,
        sql: temporal.sql,
        images: new SessionBlobs(temporal.sql),
        hostQueue: SESSION_HOST_QUEUE,
        ...(events ? { listen: (channel: string, onNotify: (payload: string) => void) => events.listenAlso(channel, onNotify) } : {}),
        ...(durableToolDeps ? { runCall: decidedCalls(DURABLE_TOOLS, durableToolDeps) } : {}),
      })
    : undefined
// The enabled remote plugins and every usable credential's secret, as a classic turn
// reads them, for a durable turn's tools and its automatic memory.
const durableRemotePlugins = plugins
  ? async () => forwardForRun(await loadEnabledPlugins(plugins, kek.ok ? kek.kek : undefined), pluginForwarder)
  : undefined
const durableCredentialSecrets = async () => {
  if (!credentials) return []
  const pool = new CredentialPool({ repo: credentials, kek, audit })
  return (await pool.candidates().catch(() => [])).map((c) => c.credential.secret)
}
// A durable session's MCP client on each remote plugin, kept on the replica its calls
// are pinned to (#2581): a stateful plugin (Playwright MCP) keeps its page between calls.
const turnClients = new SessionClients<ListedTools>()
const turnToolDeps = temporal
  ? {
      sessions: new PgSessionOwners(temporal.sql),
      // A flow's tool(name, ...) reaches the same tools, as the run's starter (#2320).
      flows: new PgFlowRuns(temporal.sql),
      servers: turnServers({
        ...(durableRemotePlugins ? { remotePlugins: durableRemotePlugins } : {}),
        ...(settings ? { settings } : {}),
        // The headless browser's lists (#982), as a classic turn's.
        browserOrigins: new BrowserOrigins(temporal.sql, settings),
        credentialSecrets: durableCredentialSecrets,
        // This pod's own disk, as a classic turn's: the calls are pinned here (hostQueue).
        httpRequest: { saveRoot: path.join(paths.stateDir, DURABLE_TURNS_DIR), sql: temporal.sql, audit, ...(workspace ? { files: workspace } : {}) },
        // Shared with agent-durable on the payload volume, which its runner loads them from.
        ...(pluginPackages
          ? {
              packages: {
                load: () => loadPackagesForRun(pluginPackages, packageInstaller),
                root: config.payloadDir ? path.join(config.payloadDir, SHARED_PACKAGES) : undefined,
                localRoot: path.join(paths.stateDir, 'plugins-local'),
                forward: (loaded) => forwardPackageServers(loaded, pluginForwarder),
              },
            }
          : {}),
        // An allowed package's other built-ins (#2393), on the session's workspace and credential.
        ...(workspace && credentials
          ? {
              builtins: {
                files: workspace,
                audit,
                // A durable session's Monitor commands run in agent-durable, where its Bash does.
                ...(backgroundTasks ? { background: { tasks: backgroundTasks, tz: agentTimezone(), commandQueue: DURABLE_TASK_QUEUE } } : {}),
                nested: (context: { session: string; flow?: string }) =>
                  nestedQueries({
                    paths,
                    credentials: new CredentialPool({ repo: credentials, kek, audit }),
                    ...(context.flow ? {} : { sessionId: context.session }),
                    // Counted in the session's cost, which its budget reads, as a classic turn counts Claude Code's own calls.
                    ...(context.flow
                      ? {}
                      : {
                          spent: async (cost: number) => {
                            await temporal.sql`UPDATE ai_sessions SET cost_usd = cost_usd + ${cost} WHERE id = ${context.session}`
                          },
                          remaining: () => poolRemainingUsd(temporal.sql, context.session),
                        }),
                  }),
              },
            }
          : {}),
      }),
      hostQueue: HOST_QUEUE,
      clients: turnClients,
      approvals: new PgApprovalRecords(temporal.sql),
      audit,
      currentTiers: liveTokenTiers(tokens),
      blobs: new SessionBlobs(temporal.sql),
      // A flow's plugin and http_request calls keep their images as its own tools' do (#2578).
      ...(payloadKeys ? { flowBlobs: new PgFlowBlobs(temporal.sql, payloadKeys) } : {}),
      ...(sessions
        ? {
            report: async (session: string, problems: readonly string[]) => {
              await sessions.events.append(
                session,
                problems.map((message) => event({ type: 'error', sessionId: session, code: 'plugin_unavailable', message })),
              )
            },
          }
        : {}),
    }
  : undefined
// AgentSession's turns are hosted here only with both halves (§5.2): acquire_worker on agent-tools
// hands out SESSION_HOST_QUEUE, so it is offered only where the session host worker below serves it.
const sessionHosting = turnHost && sessions && operationStore && turnToolDeps ? { turnHost, warm: sessions.warm } : undefined
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
const background =
  backgroundTasks && sessions
    ? backgroundActivities({
        store: backgroundTasks.store,
        send: managerSender(sessions),
        tz: agentTimezone(),
        paths,
        secrets: toolServices.secrets,
        agents: managerAgents(sessions),
        workspace,
      })
    : undefined
const temporalWorker =
  temporal && operationStore && turnToolDeps && durableToolDeps
    ? AgentWorker.start({
        address: temporal.address,
        namespace: config.temporalNamespace,
        // Polled as this replica: a poller listing names it (affinity.ts `liveHostQueues`, pluginSessions.ts).
        identity: workerIdentity(replicaIdentity()),
        activities: {
          ...toolActivities(DURABLE_TOOLS, durableToolDeps),
          ...gateActivities({
            audit,
            sessions: new PgSessionOwners(temporal.sql),
            workspace,
            // A tool that resolves what it names parks that, by name (#4442).
            tools: DURABLE_TOOLS,
            services: durableToolDeps.services,
            // Resolved with the tiers the owner holds now, as the call will run (#4442 gate review).
            currentTiers: durableToolDeps.currentTiers,
          }),
          // AgentSession's gate (its writers and timers) and its turn end (unified runtime §5.1, flagged off until phase 6).
          ...sessionGateActivities(temporal.sql),
          ...(sessions ? sessionFinishActivities(temporal.sql, sessions.events) : {}),
          ...(sessionAcquire ?? {}),
          // A durable turn's plugin and http_request tools (#2320): listed per turn, and
          // each call the `default` activity `turn:<server>__<tool>`.
          ...turnToolActivities(turnToolDeps),
          default: turnToolActivity(turnToolDeps),
          // A durable turn's automatic Hindsight memory (#2320): recall at its start,
          // retain at its end, as a classic turn's memory hooks do.
          ...(sessions && durableRemotePlugins
            ? memoryActivities({
                sessions: new PgSessionOwners(temporal.sql),
                events: sessions.events,
                remotePlugins: durableRemotePlugins,
                credentialSecrets: durableCredentialSecrets,
                audit,
                settings,
                memory,
              })
            : {}),
          // A flow's agent(...) and ask_session(...) (#1057, 6g): beside the tools, never one.
          ...flowSessionActivities({ sql: temporal.sql, flows: new PgFlowRuns(temporal.sql) }),
          ...operationActivities(commandKinds, operationStore),
          ...(background ?? {}),
        },
        ...(workerDataConverter ? { dataConverter: workerDataConverter } : {}),
        // Bundled by `pnpm build` (scripts/bundle-workflows.mjs).
        workflows: {
          workflowBundle: { codePath: fileURLToPath(new URL('./temporal/workflow-bundle.js', import.meta.url)) },
        },
      })
    : undefined
// AgentSession (unified runtime spec 2026-10-13 §5.1): new durable sessions run here only
// while `unified_sessions` is on; flagged off, nothing is ever started on this queue.
// Versioned as the Deployment `scadbuddy-agent` (D4) when the build has an id (config.buildId),
// else unversioned like the agent's other workers. `session_settings` is a local activity, so it
// is served here. `session_turn` is Task 5.2's, on the host worker.
const sessionWorker =
  temporal && settings
    ? AgentWorker.start({
        address: temporal.address,
        namespace: config.temporalNamespace,
        taskQueue: SESSION_QUEUE,
        activities: { [SESSION_SETTINGS_ACTIVITY]: () => readRuntimeSettings(settings) },
        ...(workerDataConverter ? { dataConverter: workerDataConverter } : {}),
        ...(config.buildId ? { deployment: { name: SESSION_DEPLOYMENT, buildId: config.buildId } } : {}),
        workflows: {
          workflowBundle: { codePath: fileURLToPath(new URL('./temporal/workflow-bundle.js', import.meta.url)) },
        },
      })
    : undefined
// This replica's own queues (temporal/session/hostWorkers.ts): a pinned turn tool's calls
// (#2320) and a classic session's Monitor commands on HOST_QUEUE, and AgentSession's turns on SESSION_HOST_QUEUE, limited to the warm
// cache's largest setting plus two for releases. Their work dies with the pod; a call scheduled after
// that fails at its schedule-to-start (a turn) or schedule-to-close (a pinned call) timeout.
const hostWorkers =
  temporal && operationStore && turnToolDeps
    ? startHostWorkers({
        address: temporal.address,
        namespace: config.temporalNamespace,
        identity: replicaIdentity(),
        pinned: {
          ...turnToolActivities(turnToolDeps),
          default: turnToolActivity(turnToolDeps),
          // A classic session's Monitor commands, in its directory on this replica.
          ...(background ? { bg_monitor_command: background.bg_monitor_command } : {}),
          // Other replicas' browser_* calls for a tab connected here (#4780).
          ...(tabRelay ? tabRelay.activities() : {}),
        },
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
// A durable session's pending_input Query, respond and cancel_input Updates (spec §6.6).
if (sessions && temporalClient) sessions.durable = new DurableGate(temporalClient)
// The browser relay's calls go through Temporal only sealed, each under a key of its own
// that is deleted once the caller has the answer (#4780): so only with the codec.
if (tabRelay && temporalClient && dataConverter && payloadKeys && database) {
  const keys = payloadKeys
  tabRelay.useClient({ client: temporalClient, dropKey: (subject) => dropPayloadKey(database.sql, keys, subject) })
}
// A durable session's turns (plan 5c PR 3): only with the codec, so a session's
// payloads are never sent to Temporal unsealed; without it a durable start is refused.
if (sessions && temporalClient && payloadKeys && database) {
  sessions.durableTurns = new DurableTurns({
    client: temporalClient,
    sql: database.sql,
    events: sessions.events,
    audit,
    // This replica's host for AgentSession turns, which a unified session's readiness checks (§5.2).
    ...(hostWorkers?.sessions ? { sessionHostQueue: SESSION_HOST_QUEUE } : {}),
  })
}
// Every 30 s, beside the lease reaper: durable sessions left `running` whose workflow
// has ended or never started (#2001); a durable turn takes no lease.
const stopDurableSweep =
  sessions && temporalClient && database
    ? new DurableRunningSweep({ sql: database.sql, events: sessions.events, describe: durableDescriber(temporalClient) }).start(
        SESSION_REAP_MS,
        {
          ready: database.ready,
          onError: (err) => console.error('durable running sweep failed:', (err as Error).message),
        },
      )
    : undefined
// Every 30 s: ai_pending_input rows whose workflow run ended without resolving them.
const stopOrphanSweep =
  sessions && temporalClient
    ? new PendingInputSweep({ sql: database!.sql, describe: temporalDescriber(temporalClient), events: sessions.events, audit }).start(
        APPROVAL_SWEEP_MS,
        {
          ...(database ? { ready: database.ready } : {}),
          onError: (err) => console.error('pending-input orphan sweep failed:', (err as Error).message),
        },
      )
    : undefined
// At start and every 5 min: the upstream MCP sessions of replicas that no longer poll
// agent-tools (plugins/pluginSessions.ts); skipped, and logged, when Temporal cannot say who polls.
const PLUGIN_SESSION_SWEEP_MS = 5 * 60_000
const stopPluginSessionSweep =
  database && plugins
    ? (() => {
        const deps = { plugins, kek: kek.ok ? kek.kek : undefined, log: (message: string) => console.error(message) }
        let ownEnded = false
        const sweep = async () => {
          if (!(await database.ready())) return
          // Once, first: what this hostname's previous incarnation left; nothing of this one's is live yet.
          if (!ownEnded) {
            const mark = await pluginSessionMark
            if (mark) await endOwnSessions(database.sql, replicaIdentity(), deps, mark)
            ownEnded = mark !== undefined
          }
          await runPluginSessionSweep({ sql: database.sql, client: temporalClient, ...deps })
        }
        const run = () => void sweep().catch((err: unknown) => console.error('plugin session sweep failed:', (err as Error).message))
        run()
        const timer = setInterval(run, PLUGIN_SESSION_SWEEP_MS)
        timer.unref()
        return () => clearInterval(timer)
      })()
    : undefined
const commands =
  temporalClient && operationStore
    ? new AgentCommands({
        client: temporalClient,
        store: operationStore,
        kinds: commandKinds,
        searchAttributes: config.temporalSearchAttributes,
      })
    : undefined

const app = createApp({
  database,
  backend: () => backendReachable(backend),
  kek,
  credentials,
  plugins,
  pluginForwarder,
  pluginPackages,
  packageInstaller,
  settings,
  tokens: database ? tokens : undefined,
  aiSettings: settings,
  testConnection: async (credential) => {
    // The model Settings chose (routes/model.ts, #1917), as every turn uses it.
    const model = storedModel(await settings?.get<unknown>(SETTING_MODEL))
    return testConnection(credential, { paths, ...(model ? { model } : {}) })
  },
  origins: originPolicy(config.publicUrl, config.trustedProxies, config.allowedOrigins),
  ...(sessions ? { approvals: sessions.approvals, sessions } : {}),
  ...(attachments ? { attachments } : {}),
  // The headless browser's lists and approvals (#982, routes/browserOrigins.ts).
  ...(database ? { browserOrigins: new BrowserOrigins(database.sql, settings) } : {}),
  // The panel's Files (#2329, routes/sessionFiles.ts).
  ...(workspace ? { workspace } : {}),
  // The panel's background tasks (routes/backgroundTasks.ts).
  ...(backgroundTasks ? { backgroundTasks } : {}),
  ...(audit ? { audit } : {}),
  upgradeWebSocket,
  tabs,
  ...(temporalWorker ? { temporal: () => temporalWorker.state() } : {}),
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
  // A durable segment's subagents' ScadBuddy tools (#2320, routes/subagentMcp.ts): loopback only.
  ...(database && kek.ok
    ? {
        subagentMcp: {
          sessions: new PgSessionOwners(database.sql),
          running: pgRunning(database.sql),
          currentTiers: liveTokenTiers(tokens),
          keys: () => [subagentKey(kek.kek), ...(previousKek?.ok ? [subagentKey(previousKek.kek)] : [])],
          tools: ALL_TOOLS,
          services: toolServices,
          ...(audit ? { audit } : {}),
        },
      }
    : {}),
  mcp: {
    tools: ALL_TOOLS,
    resources,
    services: toolServices,
    // Tokens live in `ai_mcp_tokens` (db/migrations/20260928T0734Z_mcp_tokens.sql).
    // Without a database /mcp answers 503 before auth (app.ts), and the
    // fail-closed store only makes sure nothing could verify anyway.
    tokens,
    ...(audit ? { audit } : {}),
    authSettings,
    oidc: oidcProvider,
    publicUrl: config.publicUrl,
    relay: mcpRelay,
  },
  mcpOidc: { repo: oidcRepo, provider: oidcProvider, publicUrl: config.publicUrl },
})

// The chat socket (routes/chat.ts). A frame is one panel message; CHAT_FRAME_MAX
// covers the largest (a 32k-character message with its page context). Images are
// uploaded, not framed (#1941, routes/attachments.ts).
const wss = new WebSocketServer({ noServer: true, maxPayload: CHAT_FRAME_MAX })
const stopHeartbeat = startHeartbeat(wss)

traceListener(PORT)
const server = serve({ fetch: app.fetch, hostname: '0.0.0.0', port: PORT, websocket: { server: wss } }, (info) => {
  console.log(
    `scadbuddy-agent listening on :${info.port}; backend ${config.backendUrl}; ` +
      `database ${database ? 'configured' : 'not configured (AI disabled)'}; ` +
      `credential writes from ${config.publicUrl ?? 'loopback only (SCADBUDDY_PUBLIC_URL unset)'}`,
  )
})

// Drain the listener first (bounded, see shutdown.ts), then close the pool,
// then exit: non-zero when the drain timed out and requests were cut.
for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.once(signal, () => void stop())
}

async function stop(): Promise<void> {
  stopSweeper?.()
  stopQuestionSweeper?.()
  stopEscalationSweeper?.()
  stopOrphanSweep?.()
  stopPluginSessionSweep?.()
  stopDurableSweep?.()
  stopReaper?.()
  stopRetention?.()
  stopAttachmentSweep?.()
  stopWorkspaceSweep?.()
  stopWarmSync?.()
  // Running turns first, while the panel's socket, the paired tab and the pool
  // are all still up: no new turn starts, running ones may finish, the rest are
  // aborted and record that they were (SessionManager.stopTurns). Aborted
  // turns' pending approvals stay pending (approvals/service.ts).
  // The agent-tools worker at the same time: it stops polling, its running
  // activities get its 10 s grace and are then cancelled (a fetch's git is killed),
  // and at 15 s it stops regardless: inside the turns' 17 s budget. Both host workers stop
  // with it, the session host worker with the turns' 17 s as its grace. AgentSession turns get
  // the classic turns' grace (TurnHost.stop); one still running then is cut and fails
  // ShuttingDown, so its workflow places it on another replica, and one that ended in the grace
  // keeps its end. The warm processes are closed only once those have ended (stopTurns `others`).
  const hostedTurns = turnHost?.stop({ graceMs: TURN_DRAIN_MS, abortWaitMs: TURN_ABORT_WAIT_MS })
  await Promise.all([
    temporalWorker?.stop(),
    hostWorkers?.stop(),
    sessionWorker?.stop(),
    sessions?.stopTurns({ graceMs: TURN_DRAIN_MS, abortWaitMs: TURN_ABORT_WAIT_MS, ...(hostedTurns ? { others: hostedTurns } : {}) }),
  ])
  // Kept plugin MCP sessions end with their DELETE while the forwarder still routes it.
  await turnClients.closeAll()
  stopSessionWake?.()
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
      // before closeDatabase runs; within the same deadline (#802).
      await sessions?.settled()
      await app.close()
      resources.close()
      await events?.close()
    },
    closeServer: async () => {
      await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
      await pluginForwarder.close()
    },
    // The session-event publisher closes only now, after the drain: turns
    // stopped above append their final session.status/session.done while they
    // wind down (stopTurns waits for that, but only for so long), and closing
    // it first would swallow that NOTIFY, so another replica's followers would
    // never wake (#715 review; busEvents.ts).
    closeDatabase: database
      ? async () => {
          sessionEvents?.close()
          await database.close()
        }
      : undefined,
    timeoutMs: 10_000,
  })
  if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
  // The last spans (this shutdown's turns among them), within 2 s of the
  // pod's grace period. Without --import no SDK started and this is a no-op.
  await shutdownTelemetry()
  process.exit(result === 'clean' ? 0 : 1)
}
