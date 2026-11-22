import type { Hono } from 'hono'
import type { AppDeps, Probe } from '../app.js'
import { DEFAULT_MCP_AUTH, type McpAuthSettings } from '../auth/authenticate.js'

/**
 * A group of HTTP routes. Each `routes/<name>.ts` that exports `route` is registered by
 * `createApp` (found by `routes/index.ts`), so a new route group is a new file, not an
 * edit to `app.ts` (ScadBuddy #508). Dependencies only one group needs are declared in
 * its own file by augmenting `AppDeps`:
 *
 *   declare module '../app.js' { interface AppDeps { widgets?: WidgetRepo | undefined } }
 */
export type RouteModule = {
  /**
   * Adds the group's routes to `app`. `shutdown` aborts when the app closes
   * (`AgentApp.close`), for a group that holds something open, such as a stream.
   */
  register(app: Hono, deps: AppDeps, shutdown: AbortSignal): void
}

/**
 * Whether the API serves every CRUD area the agent wants (api/dataHealth.ts `areasReady`): the
 * routes answer 503 until then. They do not wait for the input-hash key, which an API with no KEK
 * never releases; what hashes a call's input (/mcp, the Temporal workers) waits for it.
 */
export function ready(deps: AppDeps): Probe {
  const { database } = deps
  return database.areasReady ? () => database.areasReady!() : database.ready
}

/** How /mcp is authenticated, read per request; the default with no /mcp endpoint. */
export function mcpAuthOf(deps: AppDeps): () => McpAuthSettings | Promise<McpAuthSettings> {
  return deps.mcp?.authSettings ?? (() => DEFAULT_MCP_AUTH)
}
