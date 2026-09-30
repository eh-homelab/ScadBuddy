import { serve, upgradeWebSocket } from '@hono/node-server'
import { WebSocketServer } from 'ws'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { mcpAuthSettings } from './auth/authenticate.js'
import { OidcProvider, SettingsOidcConfigRepo } from './auth/oidc.js'
import { FailClosedTokenStore, PostgresTokenStore } from './auth/tokens.js'
import { loadConfig } from './config.js'
import { CredentialStore, SettingsStore } from './credentials.js'
import { connectDatabase } from './db.js'
import { MigrationChecksumError, MigrationLedgerError } from './db/migrations.js'
import { PgEventListener } from './events/pgListener.js'
import { DEFAULT_STATE_DIR, pluginCacheDir } from './harness/options.js'
import { probeChromiumSandbox } from './harness/headlessSandbox.js'
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
import { SessionManager } from './sessions/manager.js'
import { shutdown } from './shutdown.js'
import { harnessTools } from './tools/harness.js'
import { ALL_TOOLS } from './tools/index.js'
import { PendingActionStore } from './tools/pending.js'
import type { ToolServices } from './tools/registry.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the infrastructure variables in config.ts.
const PORT = 8081
/** How often approvals nobody is waiting on are expired (approvals/service.ts). */
const APPROVAL_SWEEP_MS = 30_000
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

// Sessions (#300) and their approvals (#258): started from the assistant
// panel's socket (routes/chat.ts) and the session routes (routes/sessions.ts).
// The approval routes and the expiry sweep also serve approvals left pending
// by a restart.
const sessions =
  database && credentials
    ? new SessionManager({
        sql: database.sql,
        paths,
        ...(settings ? { settings } : {}),
        // ScadBuddy's tools and their tiers (tools/harness.ts). ScadBuddy's own
        // plugin (plugins/scadbuddy) is not loaded: with `tools: []`
        // (harness/options.ts) a query has no Skill or Agent tool to use its
        // skills and subagents (test/harnessWiring.test.ts). The plugins
        // below (remote, packages, the headless browser's vendored one) are.
        ...harnessTools(toolServices),
        // Input hashes are HMACs under a key derived from the KEK, so they
        // compare across restarts (approvals/service.ts BINDING).
        ...(kek.ok ? { approvalHashKey: approvalHashKey(kek.kek) } : {}),
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
        // It may open only this origin, which serves the SPA.
        headlessBrowser: { backendUrl: config.backendUrl, sandbox: chromiumSandbox },
        credential: async () => {
          if (!kek.ok) throw new Error(`no key-encryption key: ${kek.reason}`)
          const credential = await credentials.reveal(kek.kek)
          if (!credential) throw new Error('no Claude credential is configured')
          return credential
        },
      })
    : undefined
// MCP prepare/confirm on ai_approvals (approvals/mcp.ts); with no database,
// the in-memory store above, whose actions are never confirmed.
if (sessions) toolServices.pending = new ApprovalActions(sessions.approvals)
const stopSweeper = sessions?.approvals.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('approval expiry sweep failed:', (err as Error).message),
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
  process.once(signal, () => {
    stopSweeper?.()
    stopRetention?.()
    stopHeartbeat()
    tabs.close()
    // 1001 "going away": the panel reconnects to another replica or after the restart.
    for (const socket of wss.clients) socket.close(1001, 'the agent service is restarting')
    // A peer that never answers the close frame would hold server.close() for
    // ws's 30 s close timeout, past the 10 s deadline.
    setTimeout(() => {
      for (const socket of wss.clients) socket.terminate()
    }, 2_000).unref()
    // Running turns stop; their pending approvals stay pending (approvals/service.ts).
    sessions?.abortAll()
    void shutdown({
      // End the /mcp sessions and the session event streams first: their
      // standing SSE responses would otherwise hold server.close() until the deadline.
      closeSessions: async () => {
        await app.close()
        resources.close()
        await events?.close()
      },
      closeServer: async () => {
        await new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve())))
        await pluginForwarder.close()
      },
      closeDatabase: database ? () => database.close() : undefined,
      timeoutMs: 10_000,
    }).then((result) => {
      if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
      process.exit(result === 'clean' ? 0 : 1)
    })
  })
}
