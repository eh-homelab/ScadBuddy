import { crudOf, type CrudTarget } from '../api/crudStores.js'
import { sessionPath } from './rows.js'

// Two reads of a session's own event log (never its fork chain) that no store owned (#5299,
// #6501): the calls with a `tool.call` and no `tool.result` (tools/activityProjection.ts, a
// restarted turn's finished calls and a forget's terminations) and the text a child's model
// wrote before a call (flows/queue.ts `childReasoning`), from the API's
// `…/agent/sessions/{id}/open-calls` and `…/reasoning` (tools/go/core/internal/publish,
// agent_data/events.py).

export type OpenCall = { id: string; name: string }
export type Delta = { message: string | null; delta: string | null }

/** The most characters of a tool_use id the API takes (backend `TOOL_USE_ID_MAX`): a longer one names no logged call. */
export const TOOL_USE_ID_MAX = 500
/** The longest turn id the API takes (tools/go/core/internal/events `TURN_ID_MAX`). */
export const TURN_ID_MAX = 500
/** The widest window one reasoning read scans (backend `REASONING_SCAN_MAX`). */
export const SCAN_MAX = 10_000

export interface SessionReads {
  /** The open calls, oldest first; with `turnId`, those after that turn's last `user.turn` (all of them when it is not logged). */
  openCalls(sessionId: string, turnId?: string): Promise<OpenCall[]>
  /**
   * The `assistant.text.delta` events since the last `user.turn` before the call's `tool.call`, up
   * to it, oldest first, within the log's last `scan` events; undefined when that `tool.call` is
   * not among them.
   */
  reasoningDeltas(sessionId: string, toolUseId: string, scan: number): Promise<Delta[] | undefined>
}

/** Over the API's CRUD routes (area `session-reads`). */
export class CrudSessionReads implements SessionReads {
  readonly #call: ReturnType<typeof crudOf>

  constructor(target: CrudTarget) {
    this.#call = crudOf(target)
  }

  /** The path of session `id`'s read `tail`, or a throw for an id that is no UUID. */
  #path(id: string, tail: string): string {
    const path = sessionPath(id)
    if (path === undefined) throw new Error(`session ${JSON.stringify(id)} is not a session id`)
    return `${path}/${tail}`
  }

  async openCalls(sessionId: string, turnId?: string): Promise<OpenCall[]> {
    // An empty turn id, or one past the API's bound (`TURN_ID_MAX`), is no logged turn: every open call.
    const query = turnId === undefined || turnId === '' || turnId.length > TURN_ID_MAX ? '' : `?${new URLSearchParams({ turn_id: turnId }).toString()}`
    const { calls } = await this.#call<{ calls: OpenCall[] }>(`session ${sessionId}'s open calls`, this.#path(sessionId, `open-calls${query}`))
    return calls
  }

  async reasoningDeltas(sessionId: string, toolUseId: string, scan: number): Promise<Delta[] | undefined> {
    const path = this.#path(sessionId, 'reasoning')
    if (toolUseId.length > TOOL_USE_ID_MAX) return undefined
    const query = new URLSearchParams({ tool_use_id: toolUseId, scan: String(Math.min(Math.max(Math.trunc(scan), 0), SCAN_MAX)) })
    const answer = await this.#call<{ logged: boolean; deltas: Delta[] }>(`session ${sessionId}'s reasoning`, `${path}?${query.toString()}`)
    return answer.logged ? answer.deltas : undefined
  }
}
