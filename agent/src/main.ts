import { serve } from '@hono/node-server'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'
import { shutdown } from './shutdown.js'

// Fixed rather than configurable: the listening port is part of the pod
// contract with the ingress (spec §4.2), not something to tune per deploy, and
// spec §9 keeps the environment surface to the three variables in config.ts.
const PORT = 8081

const config = loadConfig()
const database = config.databaseUrl ? connectDatabase(config.databaseUrl) : undefined
const backend = createBackendClient(config.backendUrl)

const app = createApp({
  database,
  backend: () => backendReachable(backend),
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
