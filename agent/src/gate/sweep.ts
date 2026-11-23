import { type Client, WorkflowNotFoundError } from '@temporalio/client'
import type { EventLog } from '../sessions/eventLog.js'
import type { SweepRoutes } from './crudPending.js'

// The orphan sweep of ai_pending_input (spec 2026-10-01 §6.6, "The projection"): a row
// is the workflow's to remove (resolve_input), but a run that ended without resolving
// it (terminated, reset, failed) leaves it behind, and the badge would count it
// forever. Each tick, rows older than `minAgeS` whose run was not checked within
// `recheckS` are grouped by run, and each run is described once. A closed run's rows
// are resolved `cancelled` on the API (gate/crudPending.ts, area `pending-input`), each
// entry one transaction with its events, the session's status (`idle` once no run holds
// an entry, plan 5b ruling 13) and an approval's audit row; a row the workflow resolved
// first is not written twice. An open run's rows are stamped `last_checked_at`; a
// describe that fails leaves its rows for a later tick.

export type RunState = 'open' | 'closed'
/** Whether a workflow run is still running; one that is not found is closed. */
export type DescribeRun = (workflowId: string, runId: string) => Promise<RunState>

export const ORPHAN_REASON = "the session's workflow run has ended"

export type SweepDeps = {
  describe: DescribeRun
  events: EventLog
  /** The API's routes, area `pending-input` (gate/crudPending.ts). */
  routes: SweepRoutes
  /** A row younger than this is not described (default 600 s): its workflow is still opening it. */
  minAgeS?: number
  /** An open run is described again only after this long (default 600 s). */
  recheckS?: number
}

export class PendingInputSweep {
  readonly #deps: SweepDeps
  readonly #minAgeS: number
  readonly #recheckS: number

  constructor(deps: SweepDeps) {
    this.#deps = deps
    this.#minAgeS = deps.minAgeS ?? 600
    this.#recheckS = deps.recheckS ?? 600
  }

  /** One pass; returns the rows it removed. */
  async sweep(): Promise<number> {
    const runs = await this.#deps.routes.runs(this.#minAgeS, this.#recheckS)
    let removed = 0
    for (const run of runs) {
      let state: RunState
      try {
        state = await this.#deps.describe(run.workflow_id, run.workflow_run_id)
      } catch {
        continue
      }
      if (state === 'open') {
        await this.#deps.routes.checked(run.workflow_id, run.workflow_run_id)
      } else {
        removed += await this.#cancelRun(run.workflow_id, run.workflow_run_id)
      }
    }
    return removed
  }

  /** Runs `sweep` every `intervalMs`; returns the stop. */
  start(intervalMs: number, options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {}): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    let running = false
    const timer = setInterval(() => {
      if (running) return
      running = true
      // Not until `ready` holds (the API answers).
      ready()
        .then((ok) => (ok ? this.sweep() : 0))
        .catch((err: unknown) => options.onError?.(err))
        .finally(() => {
          running = false
        })
    }, intervalMs)
    timer.unref()
    return () => clearInterval(timer)
  }

  async #cancelRun(workflowId: string, runId: string): Promise<number> {
    return cancelOrphanedEntries(this.#deps, { workflowId, runId })
  }
}

/**
 * Resolves `cancelled` the entries no workflow run will resolve: one ended run's (the sweep), or
 * every entry of a session whose workflow is gone (a Stop, #4018). Each entry is one transaction
 * on the API, an approval's audit row in it; an entry the workflow resolved first is not written
 * twice, and the session goes `idle` once it holds none. `reason` is the outcome's (a
 * migration's own, #4772). Returns the entries it resolved.
 */
export async function cancelOrphanedEntries(
  deps: Pick<SweepDeps, 'events' | 'routes'>,
  of: { workflowId: string; runId: string } | { sessionId: string },
  reason: string = ORPHAN_REASON,
): Promise<number> {
  const { removed, logged } = await deps.routes.cancel(of, reason)
  for (const l of logged) deps.events.committed(l.session_id, l.events, l.seqs)
  return removed
}

/** DescribeRun over a Temporal client: closed unless the run's status is RUNNING. */
export function temporalDescriber(client: Client): DescribeRun {
  return async (workflowId, runId) => {
    try {
      const described = await client.workflow.getHandle(workflowId, runId).describe()
      return described.status.name === 'RUNNING' ? 'open' : 'closed'
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return 'closed'
      throw err
    }
  }
}
