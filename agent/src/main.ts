import { serve } from '@hono/node-server'
import { getConnInfo } from '@hono/node-server/conninfo'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { loadConfig } from './config.js'
import { CredentialStore, SettingsStore } from './credentials.js'
import { connectDatabase } from './db.js'
import { DEFAULT_STATE_DIR } from './harness/options.js'
import { ensureStateDirs, StateDirError } from './harness/stateDirs.js'
import { testConnection } from './harness/testConnection.js'
import { loadKek } from './secrets.js'
import { shutdown } from './shutdown.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the three variables in config.ts.
const PORT = 8081

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
// A missing or malformed file is not fatal; /healthz and Settings say why.
const kek = await loadKek(config.secretKeyFile)
if (!kek.ok) console.error(`secret key: ${kek.reason}; saving Claude credentials is disabled`)

const database = config.databaseUrl
  ? connectDatabase(config.databaseUrl, {
      onMigrationError: (err) => console.error('database migrations failed:', (err as Error).message),
    })
  : undefined
// Migrate in the background: a database that is down at start-up is retried
// by the next /healthz or API call instead of stopping the pod.
void database?.ready()
const credentials = database ? new CredentialStore(database.sql) : undefined
const settings = database ? new SettingsStore(database.sql) : undefined
const backend = createBackendClient(config.backendUrl)
const paths = { stateDir: DEFAULT_STATE_DIR }

const app = createApp({
  database,
  backend: () => backendReachable(backend),
  kek,
  credentials,
  testConnection: async (credential) => {
    const model = await settings?.get<string>('model')
    return testConnection(credential, { paths, ...(typeof model === 'string' ? { model } : {}) })
  },
  remoteAddress: (c) => {
    try {
      return getConnInfo(c).remote.address
    } catch {
      return undefined
    }
  },
})

const server = serve({ fetch: app.fetch, hostname: '0.0.0.0', port: PORT }, (info) => {
  console.log(
    `scadbuddy-agent listening on :${info.port}; backend ${config.backendUrl}; ` +
      `database ${database ? 'configured' : 'not configured (AI disabled)'}`,
  )
})

// Drain the listener first (bounded, see shutdown.ts), then close the pool,
// then exit: non-zero when the drain timed out and requests were cut.
for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.once(signal, () => {
    void shutdown({
      closeServer: () =>
        new Promise<void>((resolve, reject) => server.close((err) => (err ? reject(err) : resolve()))),
      closeDatabase: database ? () => database.close() : undefined,
      timeoutMs: 10_000,
    }).then((result) => {
      if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
      process.exit(result === 'clean' ? 0 : 1)
    })
  })
}
