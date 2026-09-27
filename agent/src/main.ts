import { serve } from '@hono/node-server'
import { backendReachable, createBackendClient } from './api/backend.js'
import { createApp } from './app.js'
import { loadConfig } from './config.js'
import { connectDatabase } from './db.js'

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

// SIGTERM is what Kubernetes sends on rollout; close the listener and the pool
// so in-flight requests finish instead of being cut.
for (const signal of ['SIGTERM', 'SIGINT'] as const) {
  process.once(signal, () => {
    server.close()
    void (database?.close() ?? Promise.resolve()).finally(() => process.exit(0))
  })
}
