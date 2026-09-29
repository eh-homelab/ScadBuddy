import type { Hono } from 'hono'
import type { AppDeps, Probe } from '../app.js'

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

/** The database's `ready`, or always-false with no database: the routes then answer 503. */
export function ready(deps: AppDeps): Probe {
  return deps.database ? deps.database.ready : () => Promise.resolve(false)
}
