import { ApplicationFailure, Context } from '@temporalio/activity'
import type { Sql } from 'postgres'
import type { ServerEvent } from '../sessions/protocol.js'
import { sameOwner, type Owner } from '../sessions/protocol.js'
import { flowOf, type FlowRuns } from './flowRuns.js'
import { NOT_FOR_FLOWS, UNKNOWN_FLOW } from './toolActivities.js'

// A flow's `agent(...)` and `ask_session(...)` (#1057, plan 2026-10-09 §6g): the
// session is started or sent to with the `sessions_start`/`sessions_send` tools, as
// any tool call of the flow (toolActivities.ts, as the run's starter). These two
// activities are the rest, served on `agent-tools` beside the tools and never offered
// as tools: `flow_session_check`, that a flow may talk to the session, and
// `flow_session_turn`, which heartbeats until the turn ends and answers with its last
// message and how many outward calls it made (what a Reset preview counts, §7.4).
//
// A flow talks only to a durable session its starter owns: a classic session's turn
// lives in one agent process, and anyone else's session is not the starter's to drive.

export const FLOW_SESSION_CHECK = 'flow_session_check'
export const FLOW_SESSION_TURN = 'flow_session_turn'
/** A session a flow may not talk to: unknown, classic, or someone else's. */
export const SESSION_REFUSED = 'SessionRefused'
/** The turn ended in an error (interrupted, out of budget, failed); its message says which. */
export const TURN_FAILED = 'TurnFailed'
/** A final message longer than a flow takes back. */
export const TEXT_MAX = 100_000
const POLL_MS = 1_000

export type TurnAnswer = { session_id: string; turn_id: string; text: string; outward_calls: number }

export type FlowSessionDeps = { sql: Sql; flows: FlowRuns | undefined; pollMs?: number }

type Row = { mode: string; owner_kind: Owner['kind']; owner_id: string }

function refused(message: string): ApplicationFailure {
  return ApplicationFailure.nonRetryable(message, SESSION_REFUSED)
}

async function allowed(deps: FlowSessionDeps, what: string, sessionId: unknown): Promise<string> {
  const flow = flowOf(Context.current().info.workflowExecution?.workflowId)
  if (flow === undefined) throw ApplicationFailure.nonRetryable(`${what} serves only flow runs`, NOT_FOR_FLOWS)
  if (typeof sessionId !== 'string' || !/^[0-9a-f-]{36}$/.test(sessionId)) throw refused(`${what}: no session ${String(sessionId)}`)
  // A lookup that throws (the database is away) is left to the activity's retries.
  const starter = deps.flows ? await deps.flows.startedBy(flow) : undefined
  if (starter === undefined) throw ApplicationFailure.nonRetryable(`${what}: no flow run ${flow} is recorded`, UNKNOWN_FLOW)
  const [row] = await deps.sql<Row[]>`SELECT mode, owner_kind, owner_id FROM ai_sessions WHERE id::text = ${sessionId}`
  if (!row || !sameOwner(starter, { kind: row.owner_kind, id: row.owner_id })) {
    throw refused(`no session ${sessionId} of the person who started this run`)
  }
  if (row.mode !== 'durable') throw refused(`session ${sessionId} is classic; a flow talks only to a durable session`)
  return sessionId
}

/** The turn's events from its `user.turn` on, or undefined before it is logged. */
async function turnEvents(sql: Sql, sessionId: string, turnId: string): Promise<ServerEvent[] | undefined> {
  const [start] = await sql<{ seq: string }[]>`
    SELECT seq FROM ai_session_events
     WHERE session_id = ${sessionId} AND event::jsonb->>'type' = 'user.turn' AND event::jsonb->>'turnId' = ${turnId}`
  if (!start) return undefined
  const rows = await sql<{ event: string }[]>`
    SELECT event FROM ai_session_events WHERE session_id = ${sessionId} AND seq > ${start.seq} ORDER BY seq`
  return rows.map((r) => JSON.parse(r.event) as ServerEvent)
}

/** The answer once the turn ended (its `session.result`, written by every end), else undefined. */
function settled(sessionId: string, turnId: string, events: readonly ServerEvent[]): TurnAnswer | undefined {
  let ended = -1
  for (let i = 0; i < events.length; i++) {
    const e = events[i]!
    if (e.type === 'user.turn') break
    if (e.type === 'session.result') {
      ended = i
      break
    }
  }
  if (ended < 0) return undefined
  const failed = events.slice(ended + 1).find((e) => e.type === 'error' || e.type === 'user.turn' || e.type === 'session.status')
  if (failed?.type === 'error') throw ApplicationFailure.nonRetryable(failed.message, TURN_FAILED)
  const texts = new Map<string, string>()
  let last: string | undefined
  let outward = 0
  for (const e of events.slice(0, ended)) {
    if (e.type === 'assistant.text.delta') {
      texts.set(e.messageId, (texts.get(e.messageId) ?? '') + e.delta)
      last = e.messageId
    } else if (e.type === 'tool.call' && e.risk === 'outward') {
      outward++
    }
  }
  const text = last === undefined ? '' : (texts.get(last) ?? '')
  if (text.length > TEXT_MAX) {
    throw ApplicationFailure.nonRetryable(`the turn's answer is ${text.length} characters, past a flow's ${TEXT_MAX}`, TURN_FAILED)
  }
  return { session_id: sessionId, turn_id: turnId, text, outward_calls: outward }
}

export function flowSessionActivities(deps: FlowSessionDeps): Record<string, (args: unknown) => Promise<unknown>> {
  const pollMs = deps.pollMs ?? POLL_MS
  return {
    [FLOW_SESSION_CHECK]: async (args: unknown) => {
      const sessionId = await allowed(deps, FLOW_SESSION_CHECK, (args as { session_id?: unknown } | null)?.session_id)
      return { session_id: sessionId, mode: 'durable' }
    },
    [FLOW_SESSION_TURN]: async (args: unknown) => {
      const { session_id, turn_id } = (args ?? {}) as { session_id?: unknown; turn_id?: unknown }
      const sessionId = await allowed(deps, FLOW_SESSION_TURN, session_id)
      if (typeof turn_id !== 'string' || turn_id === '') throw refused(`${FLOW_SESSION_TURN} takes a turn_id`)
      const context = Context.current()
      for (;;) {
        context.heartbeat()
        const events = await turnEvents(deps.sql, sessionId, turn_id)
        const answer = events && settled(sessionId, turn_id, events)
        if (answer) return answer
        await context.sleep(pollMs)
      }
    },
  }
}
