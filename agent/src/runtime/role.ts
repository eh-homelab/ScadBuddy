import type { AgentRole } from '../config.js'

// What one agent process starts, by SCADBUDDY_AGENT_ROLE (spec 2026-10-13 §14.3, plan Step 6.1):
// the same image runs as the API pod's HTTP half (`api`), as a worker Deployment's turn runner
// (`worker`), or as both (`all`, the default: dev, tests, and every pod until the worker
// Deployment exists).
//
// `api` still polls its own `agent-tools.<identity>`: a tab's socket is in that process, so the
// browser calls other replicas make for it (`bridge.tab`, #4780) run there, and so do a classic
// session's Monitor commands and, until Step 8.1, a DurableSession's pinned `turn:` calls that it
// listed there. Classic sessions still run in-process wherever SessionManager.send runs, until
// Step 6.3. It polls neither `agent-sessions` nor the shared `agent-tools`, nor a session host
// queue, so it runs no AgentSession turn and no shared-queue activity.

export type RoleParts = {
  /** The HTTP server: the routes, the chat and bridge sockets, /mcp, and the sweeps only it needs. */
  http: boolean
  /** The shared queues: `agent-tools` (every tool, acquire_worker, commands, background tasks) and `agent-sessions` (AgentSession). */
  sharedWorkers: boolean
  /** `agent-sessions.<identity>`: AgentSession's turns on this process's warm cache. */
  sessionHost: boolean
  /** `agent-tools.<identity>`: pinned calls, Monitor commands and `bridge.tab`. Every role. */
  hostWorker: true
}

export function roleParts(role: AgentRole): RoleParts {
  switch (role) {
    case 'api':
      return { http: true, sharedWorkers: false, sessionHost: false, hostWorker: true }
    case 'worker':
      return { http: false, sharedWorkers: true, sessionHost: true, hostWorker: true }
    case 'all':
      return { http: true, sharedWorkers: true, sessionHost: true, hostWorker: true }
  }
}
