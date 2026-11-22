import type { CrudAreas } from './crudAreas.js'

// The agent's data is the API's (#5299 step 12): the agent holds no SQL client, so /healthz's
// `database` and the `ready` its data routes wait for are the API's CRUD areas. Apart from
// main.ts so a test can check the wiring.

/** The agent's data, on the API: `ping` for health, `ready` for the routes. */
export type AgentData = {
  /** Whether the API lists every wanted area and answers them within `timeoutMs`. */
  ping(timeoutMs?: number): Promise<boolean>
  /** Whether the API lists every wanted area. */
  ready(): Promise<boolean>
}

/**
 * The agent's data on `areas`. Until the API lists every wanted area the agent is not ready and
 * the API is not asked. Each change in the API's answer is logged once.
 */
export function dataHealthOf(
  areas: Pick<CrudAreas, 'complete' | 'answers'>,
  log: (message: string) => void = (message) => console.warn(message),
): AgentData {
  // Only for the log: the API's last answer, so each change is logged once.
  let answered = true
  return {
    ready: () => Promise.resolve(areas.complete),
    async ping(timeoutMs?: number) {
      if (!areas.complete) return false
      const now = await areas.answers(timeoutMs)
      if (now !== answered) log(now ? "health: the API answers the agent's CRUD areas again" : "health: the API does not answer the agent's CRUD areas")
      answered = now
      return now
    },
  }
}
