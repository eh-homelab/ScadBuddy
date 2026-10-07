import { serve, upgradeWebSocket } from '@hono/node-server'
import { WebSocketServer } from 'ws'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { mcpAuthSettings } from './auth/authenticate.js'
import { OidcProvider, SettingsOidcConfigRepo } from './auth/oidc.js'
import { approvalGrantCheck, FailClosedTokenStore, liveTokenTiers, PostgresTokenStore } from './auth/tokens.js'
import { loadConfig } from './config.js'
import { CredentialStore, SettingsStore } from './credentials.js'
import { CredentialPool } from './harness/fallback.js'
import { connectDatabase } from './db.js'
import { MigrationChecksumError, MigrationLedgerError } from './db/migrations.js'
import { PgEventListener } from './events/pgListener.js'
import { DEFAULT_STATE_DIR, pluginCacheDir } from './harness/options.js'
import { probeChromiumSandbox } from './harness/headlessSandbox.js'
import { OWN_PLUGIN_DIR } from './harness/ownPlugin.js'
import { ensureStateDirs, StateDirError, sweepBrowserDirs } from './harness/stateDirs.js'
import { testConnection } from './harness/testConnection.js'
import { originPolicy } from './http/origins.js'
import { forwardForRun, PluginForwarder } from './plugins/forwarder.js'
import { GitFetcher } from './plugins/packages/git.js'
import { loadPackagesForRun, PackageInstaller } from './plugins/packages/install.js'
import { PackageStore } from './plugins/packages/store.js'
import { loadEnabledPlugins, PluginStore } from './plugins/registry.js'
import { ResourceHub } from './resources/hub.js'
import { loadKek } from './secrets.js'
import { ApprovalActions } from './approvals/mcp.js'
import { approvalHashKey } from './approvals/service.js'
import { AuditLog } from './audit/log.js'
import { auditedTokenStore } from './audit/writes.js'
import { TabHub } from './bridge/hub.js'
import { PostgresPairingStore } from './bridge/pairings.js'
import { startHeartbeat } from './routes/chat.js'
import { followSessionEvents, SessionEventPublisher } from './sessions/busEvents.js'
import { SessionManager } from './sessions/manager.js'
import { drainRetains } from './memory/hindsight.js'
import { shutdown } from './shutdown.js'
import { shutdownTelemetry, traceListener } from './telemetry/runtime.js'
import { harnessTools } from './tools/harness.js'
import { SessionResources } from './sessions/touched.js'
import { ALL_TOOLS } from './tools/index.js'
import { PendingActionStore } from './tools/pending.js'
import type { ToolServices } from './tools/registry.js'

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
// No turn runs here yet: headless-browser folders left now are from a crash.
await sweepBrowserDirs({ stateDir: DEFAULT_STATE_DIR }).catch((err: unknown) =>
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
// Plugin traffic (connection tests, and each session turn's enabled plugins)
// goes through this loopback forwarder (plugins/forwarder.ts).
const pluginForwarder = await PluginForwarder.start()
const backend = createBackendClient(config.backendUrl)

// The event bus (spec §7, #264): LISTEN on `scadbuddy_events` on a connection
// of its own, retried in the background, feeding MCP resource subscriptions.
const events = config.databaseUrl ? new PgEventListener(config.databaseUrl) : undefined
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
}
// The browser bridge (#254, bridge/hub.ts): the tabs connected over
// /api/v1/ai/bridge, which the browser_* tools drive; MCP clients pair with
// one through `ai_browser_pairings` (spec §8.5).
const tabs = new TabHub({ pairings: database ? new PostgresPairingStore(database.sql) : undefined })
toolServices.browser = tabs
// What each session touched (#931, sessions/touched.ts), from its tool calls.
if (database) {
  toolServices.touched = new SessionResources(database.sql, (err) =>
    console.error('session resources: could not record a call:', (err as Error).message),
  )
}
// Plugin packages (#297): the pin is in Postgres (`ai_plugin_packages`); the
// files under <state dir>/plugins are a cache, rebuilt from the pin and
// verified against its content hash before each load (plugins/packages/).
// Each session turn loads the enabled ones (`packagePlugins` below).
const pluginPackages = database ? new PackageStore(database.sql) : undefined
const packageInstaller = new PackageInstaller({ fetcher: new GitFetcher(), cacheRoot: pluginCacheDir(paths) })

// One store for Settings (routes/mcpTokens.ts) and /mcp. Mint and revoke are
// recorded in the audit log (#258), whichever of the two makes them.
const tokens =
  database && audit ? auditedTokenStore(new PostgresTokenStore(database.sql), audit) : new FailClosedTokenStore()

// Whether the headless browser's Chromium can keep its sandbox in this pod
// (harness/headlessSandbox.ts): probed once, on the first turn that uses the
// browser, and said loudly either way.
let sandboxProbe: Promise<boolean> | undefined
const chromiumSandbox = (): Promise<boolean> =>
  (sandboxProbe ??= probeChromiumSandbox().then((probe) => {
    if (probe.available) console.log(`headless browser: Chromium runs with its sandbox (${probe.detail})`)
    else {
      console.warn(
        `headless browser: Chromium's sandbox is unavailable here, so it runs with --no-sandbox (${probe.detail}); ` +
          'allow user namespaces in the pod to enable it (docs/ai/headless-browser.md, "Sandbox")',
      )
    }
    return probe.available
  }))

// `session.*` on the event bus (#300, sessions/busEvents.ts): every event-log
// append is announced on `scadbuddy_events`, and this replica's LISTEN
// consumer (below) wakes its followers for sessions other replicas write.
const sessionEvents = database ? new SessionEventPublisher(database.sql) : undefined

// Sessions (#300) and their approvals (#258): started from the assistant
// panel's socket (routes/chat.ts), the session routes (routes/sessions.ts)
// and the `sessions_*` tools (tools/sessions.ts).
// The approval routes and the expiry sweep also serve approvals left pending
// by a restart.
const sessions =
  database && credentials
    ? new SessionManager({
        sql: database.sql,
        paths,
        ...(settings ? { settings } : {}),
        // ScadBuddy's tools and their tiers (tools/harness.ts).
        ...harnessTools(toolServices),
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
                forwardForRun(await loadEnabledPlugins(plugins, kek.ok ? kek.kek : undefined), pluginForwarder),
            }
          : {}),
        // Enabled plugin packages (#297), materialised from their pins, per turn.
        ...(pluginPackages ? { packagePlugins: () => loadPackagesForRun(pluginPackages, packageInstaller) } : {}),
        // The headless browser (#349): on for a turn only when the
        // `headless_browser_enabled` setting is true (routes/headlessBrowser.ts).
        // It opens the backend, which serves the SPA; the UI's public origins
        // are rewritten onto it, and SCADBUDDY_BROWSER_ALLOWED_ORIGINS names what
        // else a human may let it open (harness/browserOrigins.ts).
        headlessBrowser: {
          backendUrl: config.backendUrl,
          ...(config.publicUrl ? { publicUrl: config.publicUrl } : {}),
          ...(config.allowedOrigins ? { uiOrigins: config.allowedOrigins } : {}),
          ...(config.browserAllowedOrigins ? { browserAllowedOrigins: config.browserAllowedOrigins } : {}),
          sandbox: chromiumSandbox,
        },
        // The http_request tool (#827): on for a turn unless the
        // `http_request_enabled` setting is false (routes/httpRequest.ts).
        httpRequest: {},
        // Every usable credential in priority order, with fallback; disables,
        // cooldowns, recoveries and fallbacks are audited (#1093).
        credentials: new CredentialPool({ repo: credentials, kek, audit }),
      })
    : undefined
// MCP prepare/confirm on ai_approvals (approvals/mcp.ts); with no database,
// the in-memory store above, whose actions are never confirmed.
if (sessions) toolServices.pending = new ApprovalActions(sessions.approvals)
// #815 §2: a session whose tab is connected again stops waiting for it.
if (sessions) tabs.onSessionTab = (sessionId) => sessions.questions.reconnected(sessionId)
// The `sessions_*` tools (#300) act on the same manager, over /mcp and in-process.
if (sessions) toolServices.sessions = sessions
// The LISTEN consumer that calls EventLog.wake() for other replicas' `session.*`.
const stopSessionWake =
  sessions && events && sessionEvents ? followSessionEvents(events, sessions.events, sessionEvents.replica) : undefined
const stopSweeper = sessions?.approvals.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('approval expiry sweep failed:', (err as Error).message),
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
    const model = await settings?.get<string>('model')
    return testConnection(credential, { paths, ...(typeof model === 'string' ? { model } : {}) })
  },
  origins: originPolicy(config.publicUrl, config.trustedProxies, config.allowedOrigins),
  ...(sessions ? { approvals: sessions.approvals, sessions } : {}),
  ...(audit ? { audit } : {}),
  upgradeWebSocket,
  tabs,
  remoteAddress: (c) => {
    try {
      return getConnInfo(c).remote.address
    } catch {
      return undefined
    }
  },
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
  },
  mcpOidc: { repo: oidcRepo, provider: oidcProvider, publicUrl: config.publicUrl },
})

// The chat socket (routes/chat.ts). A frame is one panel message; 256 KiB
// covers the largest (a 32k-character message plus its page context).
const wss = new WebSocketServer({ noServer: true, maxPayload: 256 * 1024 })
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
  stopReaper?.()
  stopRetention?.()
  // Running turns first, while the panel's socket, the paired tab and the pool
  // are all still up: no new turn starts, running ones may finish, the rest are
  // aborted and record that they were (SessionManager.stopTurns). Aborted
  // turns' pending approvals stay pending (approvals/service.ts).
  await sessions?.stopTurns({ graceMs: TURN_DRAIN_MS, abortWaitMs: TURN_ABORT_WAIT_MS })
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
