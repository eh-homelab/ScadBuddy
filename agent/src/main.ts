import { serve } from '@hono/node-server'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { DEFAULT_MCP_AUTH } from './auth/authenticate.js'
import { FailClosedTokenStore, PostgresTokenStore } from './auth/tokens.js'
import { loadConfig } from './config.js'
import { CredentialStore, SettingsStore } from './credentials.js'
import { connectDatabase } from './db.js'
import { MigrationChecksumError, MigrationLedgerError } from './db/migrations.js'
import { PgEventListener } from './events/pgListener.js'
import { DEFAULT_STATE_DIR, pluginCacheDir } from './harness/options.js'
import { ensureStateDirs, StateDirError } from './harness/stateDirs.js'
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
import { SessionManager } from './sessions/manager.js'
import { shutdown } from './shutdown.js'
import { ALL_TOOLS } from './tools/index.js'
import { PendingActionStore } from './tools/pending.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the infrastructure variables in config.ts.
const PORT = 8081
/** How often approvals nobody is waiting on are expired (approvals/service.ts). */
const APPROVAL_SWEEP_MS = 30_000

const config = loadConfig()

// Before listening: a mounted, empty state volume must get its `claude/` and
// `work/` back, and an unwritable one should stop the pod here, visibly.
try {
  await ensureStateDirs({ stateDir: DEFAULT_STATE_DIR })
} catch (err) {
  console.error(err instanceof StateDirError ? err.message : err)
  process.exit(1)
}

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
const settings = database ? new SettingsStore(database.sql) : undefined
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
// Plugin packages (#297): the pin is in Postgres (`ai_plugin_packages`); the
// files under <state dir>/plugins are a cache, rebuilt from the pin and
// verified against its content hash before each load (plugins/packages/).
// Each session turn loads the enabled ones (`packagePlugins` below).
const pluginPackages = database ? new PackageStore(database.sql) : undefined
const packageInstaller = new PackageInstaller({ fetcher: new GitFetcher(), cacheRoot: pluginCacheDir(paths) })

// One store for Settings (routes/mcpTokens.ts) and /mcp.
const tokens = database ? new PostgresTokenStore(database.sql) : new FailClosedTokenStore()

// Sessions (#300) and their approvals (#258). Nothing starts a session over
// HTTP yet (#266's socket and #251's /mcp do); the approval routes and the
// expiry sweep are live so that approvals left pending by a restart can be
// seen, decided or expired.
const sessions =
  database && credentials
    ? new SessionManager({
        sql: database.sql,
        paths,
        ...(settings ? { settings } : {}),
        // Input hashes are HMACs under a key derived from the KEK, so they
        // compare across restarts (approvals/service.ts BINDING).
        ...(kek.ok ? { approvalHashKey: approvalHashKey(kek.kek) } : {}),
        // Enabled plugins (#297), per turn, through the loopback forwarder.
        ...(plugins
          ? {
              remotePlugins: async () =>
                forwardForRun(await loadEnabledPlugins(plugins, kek.ok ? kek.kek : undefined), pluginForwarder),
            }
          : {}),
        // Enabled plugin packages (#297), materialised from their pins, per turn.
        ...(pluginPackages ? { packagePlugins: () => loadPackagesForRun(pluginPackages, packageInstaller) } : {}),
        credential: async () => {
          if (!kek.ok) throw new Error(`no key-encryption key: ${kek.reason}`)
          const credential = await credentials.reveal(kek.kek)
          if (!credential) throw new Error('no Claude credential is configured')
          return credential
        },
      })
    : undefined
const stopSweeper = sessions?.approvals.startSweeper(APPROVAL_SWEEP_MS, {
  ...(database ? { ready: database.ready } : {}),
  onError: (err) => console.error('approval expiry sweep failed:', (err as Error).message),
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
  tokens: database ? tokens : undefined,
  testConnection: async (credential) => {
    const model = await settings?.get<string>('model')
    return testConnection(credential, { paths, ...(typeof model === 'string' ? { model } : {}) })
  },
  origins: originPolicy(config.publicUrl, config.trustedProxies),
  ...(sessions ? { approvals: sessions.approvals } : {}),
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
    services: {
      backend,
      // MCP prepare/confirm on ai_approvals (approvals/mcp.ts); with no
      // database, an in-memory store whose actions are never confirmed.
      pending: sessions ? new ApprovalActions(sessions.approvals) : new PendingActionStore(),
      pollIntervalMs: 1000,
      renderWaitMs: 10 * 60_000,
      publicBaseUrl: config.publicUrl,
    },
    // Tokens live in `ai_mcp_tokens` (db/migrations/20260928T0734Z_mcp_tokens.sql).
    // Without a database /mcp answers 503 before auth (app.ts), and the
    // fail-closed store only makes sure nothing could verify anyway.
    // TODO(#251 follow-up): the auth mode read from `ai_settings`.
    tokens,
    authSettings: () => DEFAULT_MCP_AUTH,
  },
})

const server = serve({ fetch: app.fetch, hostname: '0.0.0.0', port: PORT }, (info) => {
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
    // Running turns stop; their pending approvals stay pending (approvals/service.ts).
    sessions?.abortAll()
    void shutdown({
      // End the /mcp sessions first: their standing SSE streams would
      // otherwise hold server.close() until the deadline.
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
