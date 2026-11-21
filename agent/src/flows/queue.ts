import type { Sql } from 'postgres'
import { flowRequestId, parseRequestId } from '../gate/ids.js'
import { PgSessionReads, type SessionReads } from '../sessions/sessionReads.js'
import type { FlowRunReads, QueueHop, StepRow, WaitingOn } from './backendReads.js'
import type { EscalationRoutes } from './crudEscalations.js'

// A flow run's one queue (#2482, spec 2026-10-11 §4.5): everything of the run that waits
// for a decision, from the three places it is recorded, in one read.
//
//   - the run's own parked calls: `workflow_runs.waiting_on`, which the backend's
//     `ProjectWorkflow` projects;
//   - its child sessions' parked calls from flow turns: `ai_pending_input` rows with
//     the run's `flow_run_id`;
//   - its grant while it is requested: `flow_grants`.
//
// The backend's tables are read through its CRUD routes (backendReads.ts, #5299 D7).
//
// Each entry keeps its own id and is answered where it is today: a `flow:` entry by the
// backend's `POST /workflow-runs/{id}/decide`, a `durable:` one by
// `POST /api/v1/ai/pending-input/{id}`, a grant by `POST /workflow-runs/{id}/grant/approve`.
//
// Context beside each entry, none of it a call's raw input (10-01 §6.6): the step (for a
// child, the run's step that drives its session), why it is outside the grant, its hops,
// and for a child's call the child's reasoning, the text its model wrote in that turn
// before the call. That text is model output: every reader shows it as untrusted data.

/** How much of a child's reasoning is kept: its last 2 KiB (spec §4.5), in characters. */
export const REASONING_MAX = 2048

/**
 * How far back in a child's event log the call and its turn's start are looked for: its
 * last this many events, an index range on the log's key, never a scan of a long session.
 * A call parks as it is logged, so it is near the end; a turn longer than this shows the
 * text from the window's start.
 */
export const REASONING_SCAN = 2000

export type QueueStep = { seq: number; fn: string }
export type { QueueHop }

type Common = {
  /** Its request id, as the route that answers it takes it; the grant's id for a grant. */
  id: string
  hop: 'parent' | 'person' | null
  hop_until: string | null
  /** Why the run's grant did not admit it. */
  reason: string | null
  hops: QueueHop[]
  step: QueueStep | null
}

export type QueueEntry =
  | (Common & {
      source: 'flow'
      kind: 'approval' | 'answer'
      call_id: string
      fn: string
      prompt: string | null
      since: string | null
      /** Whether the starting session narrowed it into a proposal (`GET /workflow-runs/{id}/proposals/{call}`). */
      proposed: boolean
    })
  | (Common & {
      source: 'child'
      kind: 'approval' | 'answer'
      session_id: string
      tool: string
      /** The scrubbed summary, never the call's input. */
      summary: string
      input_hash: string | null
      created_at: string
      expires_at: string
      /** Past its timer, its worker not yet resolved it: no one can decide it now (as `PendingProjection`'s). */
      expiring: boolean
      /** The child's text before the call, untrusted; empty when none was written. */
      reasoning: string
    })
  | { source: 'grant'; id: string; status: 'requested'; created_at: string }

export type RunQueue = {
  run_id: string
  name: string
  status: string
  /** The session that started the run, when one did. */
  starter_session: string | null
  entries: QueueEntry[]
}

/** The run's step of `call_id` (a flow-own call's), or null. */
export function ownStep(steps: readonly StepRow[], callId: string): QueueStep | null {
  const s = steps.find((x) => x.call_id === callId)
  return s ? { seq: s.seq, fn: s.fn } : null
}

/** The run's step that drives `sessionId` (its latest running `agent`/`ask_session`), or null. */
export function childStep(steps: readonly StepRow[], sessionId: string): QueueStep | null {
  const mine = steps.filter((x) => x.session_id === sessionId)
  const s = mine.filter((x) => x.status === 'running').at(-1) ?? mine.at(-1)
  return s ? { seq: s.seq, fn: s.fn } : null
}

/**
 * The text a child's model wrote in its turn before the call `requestId` names: the
 * `assistant.text.delta` events since the turn's `user.turn`, up to the call's
 * `tool.call` (spec §4.5), the last `REASONING_MAX` characters. Empty when nothing was
 * written, or the call is no durable session's; undefined when its `tool.call` is not in
 * the log's last `scan` events (not logged yet: `follow_session` writes it a moment
 * after the call parks).
 */
export async function childReasoning(reads: Pick<SessionReads, 'reasoningDeltas'>, requestId: string, scan = REASONING_SCAN): Promise<string | undefined> {
  const parsed = parseRequestId(requestId)
  if (parsed?.store !== 'durable') return ''
  const rows = await reads.reasoningDeltas(parsed.sessionId, parsed.toolUseId, scan)
  if (rows === undefined) return undefined
  const parts: string[] = []
  let last: string | null | undefined
  for (const r of rows) {
    if (!r.delta) continue
    if (parts.length > 0 && r.message === last) parts[parts.length - 1] += r.delta
    else parts.push(r.delta)
    last = r.message
  }
  const text = parts.join('\n\n')
  return text.length > REASONING_MAX ? text.slice(text.length - REASONING_MAX) : text
}

type OwnEntry = Extract<QueueEntry, { source: 'flow' }>

/** A run's child entry (`ai_pending_input`), as `forRun` reads it. */
type ChildRow = {
  request_id: string
  kind: 'approval' | 'answer'
  session_id: string
  tool: string
  summary: string
  input_hash: string | null
  hop: 'parent' | 'person' | null
  hop_until: Date | string | null
  outside_reason: string | null
  hops: QueueHop[] | null
  created_at: Date
  expires_at: Date
  expiring: boolean
}

function ownEntry(run: { id: string; workflow_run_id: string }, steps: readonly StepRow[], w: WaitingOn): OwnEntry {
  return {
    source: 'flow',
    id: flowRequestId(run.id, run.workflow_run_id, w.call_id),
    kind: w.kind,
    call_id: w.call_id,
    fn: w.fn,
    prompt: w.prompt ?? null,
    since: w.since ?? null,
    hop: w.hop ?? null,
    hop_until: w.hop_until ?? null,
    reason: w.reason ?? null,
    proposed: w.proposed === true,
    hops: w.hops ?? [],
    step: ownStep(steps, w.call_id),
  }
}

/**
 * A hop's deadline for the queue. A deny's claim holds it at `infinity` (an Invalid Date from the
 * SQL), which is no time to show: the entry is read with none. The panel then says "with this
 * session" and the run page's card "With the session that started the run", each with no time.
 */
const iso = (d: Date | string | null): string | null =>
  d === null ? null : typeof d === 'string' ? d : Number.isNaN(d.getTime()) ? null : d.toISOString()

export class FlowQueue {
  readonly #sql: Sql
  readonly #reads: FlowRunReads

  readonly #routes: { crud: Pick<EscalationRoutes, 'runEntries'>; use: () => boolean } | undefined
  readonly #sessionReads: Pick<SessionReads, 'reasoningDeltas'>

  /**
   * `reads`: the backend's flow tables, through its CRUD routes (backendReads.ts). `routes`: a
   * run's child entries through the API once it lists area `escalations` (#5299 step 10).
   * `sessionReads`: a child's reasoning (`gated` in main.ts; SQL on `sql` when omitted).
   */
  constructor(
    sql: Sql,
    reads: FlowRunReads,
    routes?: { crud: Pick<EscalationRoutes, 'runEntries'>; use: () => boolean },
    sessionReads: Pick<SessionReads, 'reasoningDeltas'> = new PgSessionReads(sql),
  ) {
    this.#sql = sql
    this.#reads = reads
    this.#routes = routes
    this.#sessionReads = sessionReads
  }

  async #children(runId: string): Promise<ChildRow[]> {
    if (this.#routes?.use()) {
      const rows = await this.#routes.crud.runEntries(runId)
      // An ISO time as the SQL's Date gave it; `infinity` (a deny's claim) is no time: `iso` shows none.
      const at = (s: string | null) => (s === null || Number.isNaN(Date.parse(s)) ? null : new Date(s).toISOString())
      return rows.map((r) => ({ ...r, hop_until: at(r.hop_until), hops: (r.hops as QueueHop[] | null) ?? null }))
    }
    return await this.#sql<ChildRow[]>`
      SELECT request_id, kind, session_id::text, tool, summary, input_hash, hop, hop_until, outside_reason, hops,
             created_at, expires_at, (expires_at <= now()) AS expiring
        FROM ai_pending_input WHERE flow_run_id = ${runId} ORDER BY created_at, request_id`
  }

  /** The run's one queue, or undefined when there is no such run (or no flows on this deployment). */
  async forRun(runId: string): Promise<RunQueue | undefined> {
    const run = await this.#reads.queue(runId)
    if (!run) return undefined
    const steps = run.steps
    const entries: QueueEntry[] = []
    for (const w of run.waiting_on) entries.push(ownEntry(run, steps, w))
    const children = await this.#children(run.id)
    // Side by side: the run page and the starter's panel both poll this.
    const reasonings = await Promise.all(children.map((c) => childReasoning(this.#sessionReads, c.request_id)))
    for (const [i, c] of children.entries()) {
      entries.push({
        source: 'child',
        id: c.request_id,
        kind: c.kind,
        session_id: c.session_id,
        tool: c.tool,
        summary: c.summary,
        input_hash: c.input_hash,
        hop: c.hop,
        hop_until: iso(c.hop_until),
        reason: c.outside_reason,
        hops: c.hops ?? [],
        created_at: c.created_at.toISOString(),
        expires_at: c.expires_at.toISOString(),
        expiring: c.expiring,
        step: childStep(steps, c.session_id),
        reasoning: reasonings[i] ?? '',
      })
    }
    const grant = run.requested_grant
    if (grant) entries.push({ source: 'grant', id: grant.id, status: 'requested', created_at: grant.created_at.toISOString() })
    return {
      run_id: run.id,
      name: run.name,
      status: run.status,
      starter_session: run.starter_session,
      entries,
    }
  }

  /**
   * Every open run's own parked calls, for the person's aggregate read (spec §4.5: the
   * badge counts them, grouped by run). A child's are in `ai_pending_input` already.
   * The 200 most recently moved runs: past that, the longest-waiting ones are left out.
   * `updated_at` dates an entry that records no `since`.
   */
  async ownEntries(): Promise<{ run: { id: string; name: string; updated_at: string }; starter_session: string | null; entry: OwnEntry }[]> {
    const runs = await this.#reads.waitingRuns()
    return runs.flatMap((run) =>
      run.waiting_on.map((w) => ({
        run: { id: run.id, name: run.name, updated_at: run.updated_at.toISOString() },
        starter_session: run.starter_session,
        entry: ownEntry(run, run.steps, w),
      })),
    )
  }

  /** The steps of `runId`, for a delivery's context; empty when it has none. */
  async steps(runId: string): Promise<StepRow[]> {
    return await this.#reads.steps(runId)
  }
}
