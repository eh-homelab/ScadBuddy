import { ApplicationFailure, Context } from '@temporalio/activity'
import type { Sql } from 'postgres'
import { userTurnRows } from '../sessions/eventLog.js'
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
  const { workflowExecution } = Context.current().info
  const flow = flowOf(workflowExecution?.workflowId)
  if (flow === undefined) throw ApplicationFailure.nonRetryable(`${what} serves only flow runs`, NOT_FOR_FLOWS)
  if (typeof sessionId !== 'string' || !/^[0-9a-f-]{36}$/.test(sessionId)) throw refused(`${what}: no session ${String(sessionId)}`)
  // #2258: as flowCaller and turnCaller refuse it, a call without its execution's run id.
  const runId = workflowExecution?.runId
  if (!runId) throw ApplicationFailure.nonRetryable(`${what}: flow run ${flow}'s call carries no workflow execution run id`, UNKNOWN_FLOW)
  // A lookup that throws (the database is away) is left to the activity's retries.
  const starter = deps.flows ? await deps.flows.startedBy(flow, runId) : undefined
  if (starter === undefined) throw ApplicationFailure.nonRetryable(`${what}: no open flow run ${flow} is recorded for this execution`, UNKNOWN_FLOW)
  const [row] = await deps.sql<Row[]>`SELECT mode, owner_kind, owner_id FROM ai_sessions WHERE id::text = ${sessionId}`
  if (!row || !sameOwner(starter, { kind: row.owner_kind, id: row.owner_id })) {
    throw refused(`no session ${sessionId} of the person who started this run`)
  }
  if (row.mode !== 'durable') throw refused(`session ${sessionId} is classic; a flow talks only to a durable session`)
  return sessionId
}

/**
 * The turn's events from its `user.turn` on, read a little at a time (#2277): the first
 * poll that finds the `user.turn` remembers its seq, and every poll after reads only the
 * rows past the last one read, so a long session costs no more to poll than a short one.
 */
function turnReader(sql: Sql, sessionId: string, turnId: string): () => Promise<ServerEvent[] | undefined> {
  let after: string | undefined
  const events: ServerEvent[] = []
  // #2277: a session deleted mid-wait never logs the result; without this the step would
  // wait out the whole turn timeout (backend workflows/flow_tools.py). Asked in the same
  // query as each read, so a poll is one round trip.
  const gone = () => refused(`session ${sessionId} is gone; its turn ${turnId} will not finish`)
  return async () => {
    if (after === undefined) {
      const [start] = await sql<{ here: boolean; seq: string | null }[]>`
        SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = ${sessionId}) AS here, e.seq
          FROM (SELECT 1) AS one
          LEFT JOIN ai_session_events e ON ${userTurnRows(sql, sessionId, turnId)}`
      if (!start?.here) throw gone()
      if (start.seq === null) return undefined
      after = start.seq
    }
    const rows = await sql<{ here: boolean; seq: string | null; event: string | null }[]>`
      SELECT EXISTS (SELECT 1 FROM ai_sessions WHERE id::text = ${sessionId}) AS here, e.seq, e.event
        FROM (SELECT 1) AS one
        LEFT JOIN LATERAL (
          SELECT seq, event FROM ai_session_events WHERE session_id = ${sessionId} AND seq > ${after} ORDER BY seq
        ) e ON true`
    if (!rows[0]?.here) throw gone()
    for (const r of rows) {
      if (r.seq === null || r.event === null) continue
      events.push(JSON.parse(r.event) as ServerEvent)
      after = r.seq
    }
    return events
  }
}

/** How far `settled` has looked for the turn's end, across polls. */
type Scan = { from: number; ended?: number; stopped?: boolean }

/**
 * The answer once the turn ended (its `session.result`, written by every end), else undefined.
 * Its text is the deltas logged before that result. A delta that `follow_session` was still
 * writing when the session workflow gave up waiting for it (agent-durable session/workflow.py
 * FOLLOW_DRAIN, 30 s, then the activity is cancelled) can land after the result and is not
 * read (#2277): the answer then ends early. Waiting for the session to leave `running` would
 * not close that, since a cancelled write may still commit after it.
 */
function settled(sessionId: string, turnId: string, events: readonly ServerEvent[], scan: Scan): TurnAnswer | undefined {
  // Only the events this poll added are looked at, as turnReader reads only new rows.
  if (scan.ended === undefined && !scan.stopped) {
    for (; scan.from < events.length; scan.from++) {
      const e = events[scan.from]!
      if (e.type === 'user.turn') {
        // The next turn began with no result for this one: it never settles.
        scan.stopped = true
        break
      }
      if (e.type === 'session.result') {
        scan.ended = scan.from
        break
      }
    }
  }
  const ended = scan.ended
  if (ended === undefined) return undefined
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
      const read = turnReader(deps.sql, sessionId, turn_id)
      const scan: Scan = { from: 0 }
      for (;;) {
        context.heartbeat()
        const events = await read()
        const answer = events && settled(sessionId, turn_id, events, scan)
        if (answer) return answer
        await context.sleep(pollMs)
      }
    },
  }
}
