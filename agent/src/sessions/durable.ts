import {
  type Client,
  WithStartWorkflowOperation,
  WorkflowExecutionAlreadyStartedError,
  WorkflowUpdateFailedError,
} from '@temporalio/client'
import { ApplicationFailure, WorkflowNotFoundError } from '@temporalio/common'
import { context as otelContext, trace } from '@opentelemetry/api'
import type { Sql } from 'postgres'
import { traceparentOf } from '../telemetry/trace.js'
import type { Tier } from '../auth/principal.js'
import type { FlowTurn } from '../tools/registry.js'
import { DurableGate, DurableUnavailable, sessionWorkflowId } from '../gate/durable.js'
import { TASK_QUEUE } from '../temporal/names.js'
import { liveHostQueues } from '../temporal/session/affinity.js'
import { SESSION_QUEUE, SESSION_WORKFLOW } from '../temporal/session/names.js'
import { SessionBlobs, sentImageBlobs } from './blobs.js'
import { type DescribeSession, durableDescriber, endLostTurn, RUNNING_GRACE_MS, type RunningRow } from './durableSweep.js'
import { cancelOrphanedEntries } from '../gate/sweep.js'
import type { AuditSink } from '../audit/log.js'
import type { EventLog } from './eventLog.js'
import type { UserImage } from './images.js'
import { POOL_BUDGET, POOL_COST, SessionError, type SessionRecord, type Turn, type TurnOutcome } from './manager.js'
import { event, type Owner, type SentImage, type SessionStatus } from './protocol.js'

// A session's turns (unified runtime spec 2026-10-13 §5, plan Step 6.3): every session is an
// AgentSession, `session-<id>` on `agent-sessions`; this service hands it the message and reads
// the turn's end from the event log. (The name is from when DurableSession, agent-durable's
// Python workflow, ran them: plan Step 8.1 retired it.)
//
// - send: claim the row (`status = 'running'`, no turn_id or lease: the workflow owns the turn),
//   put the turn's images in ai_session_blobs (the claim check, by name), write `user.turn` and
//   `session.status running`, then update-with-start `send_message` (update id: the turn id),
//   starting the workflow on the first message. Once a turn has run (`durable_offset` moved) it
//   is a plain Update: a workflow removed by retention is refused `closed`, never restarted empty
//   (#2078). Only a definite refusal (the validator's `busy`, a failed Update, an ended workflow)
//   gives the claim back. An Update not accepted in time keeps it: Temporal may already hold the
//   message, which a worker delivers later, so the turn is handed back and `done` follows the log.
// - `done` follows the event log from the turn's own `session.status running` to the status the
//   turn's end writes, and is read only when asked for.
// - interrupt: the `interrupt_requested` flag the turn's replica reads, then the `interrupt`
//   Signal, which is recorded even while no worker runs.
// - handoff: after the owner change is committed, `cancel_input`, best-effort, and the new owner
//   told to the gate; when it goes unanswered the `interrupt` Signal ends the parked calls.
//
// A session's `runtime` column names its workflow type. A new one gets `unified` at insert, and
// one with none (a classic session's) gets it at its first send: its transcript is in
// ai_session_entries, which the turn resumes. A row still `durable` is a DurableSession that
// closed before plan Step 7.2 moved the open ones (six in production on 2026-11-01, all
// archived, none with a transcript): no worker runs its workflow, so a send is refused `closed`
// and a fork of it starts an AgentSession (manager.ts `fork`). No prompt, image or tool content
// is logged or traced here.

export const SEND_MESSAGE_UPDATE = 'send_message'
/** `ai_sessions.runtime`: AgentSession (`unified`), or a closed DurableSession (`durable`, see the header). */
export type SessionRuntime = 'durable' | 'unified'

/** How long a readiness answer is reused (plan 5d Ruling 2): one Temporal call per window, not per start. */
const READY_TTL_MS = 15_000
/** How long "Temporal did not answer" is reused: short, so a blip passes, but an outage costs one deadline per window. */
const UNANSWERED_TTL_MS = 2_000
/**
 * A poller seen longer ago than this is a worker that has gone: Temporal lists pollers
 * for minutes after, but a live worker re-polls at least once a long-poll (about 60 s).
 */
const POLLER_FRESH_MS = 70_000
/** How long the readiness check waits for Temporal before calling it unreachable. */
const READY_DEADLINE_MS = 3_000
/** temporal.api.enums.v1.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW */
const WORKFLOW_TASK_QUEUE = 1
/** temporal.api.enums.v1.TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY */
const ACTIVITY_TASK_QUEUE = 2

/** What the workflow's validator refuses a second turn with (temporal/session/workflow.ts). */
const BUSY = 'busy'
const RUNNING: readonly SessionStatus[] = ['running', 'waiting_approval', 'waiting_input']
export const SETTLED: readonly SessionStatus[] = ['idle', 'failed', 'done']

type WireOwner = { kind: string; id: string; label: string }
type ImageRef = { name: string; mediaType: string }

/** AgentSession's input (temporal/session/models.ts `SessionStart`). */
export type SessionStart = {
  session_id: string
  creator: WireOwner
  owner: WireOwner
  max_turns: number
  system_append: string | null
}

/** The `send_message` Update's argument (temporal/session/models.ts); each turn carries its own append (#2525). */
export type DurableMessage = {
  turn_id: string
  text: string
  author: WireOwner
  images: ImageRef[]
  system_append: string | null
  /** A turn a flow run sent (#2481, `FlowTurn`); absent for any other. */
  flow?: FlowTurn
  /** The user's words, which memory is recalled against. */
  words: string
  /** The sender's trace, so the turn's spans are its children as a classic turn's are. */
  traceparent?: string
}
type SendAnswer = { accepted: boolean; turn_id: string }

/** One turn as SessionManager.send hands it over. */
export type DurableTurn = {
  turnId: string
  /** The user's words: what `user.turn` shows. */
  prompt: string
  /** What the model gets: the words and the page context. */
  text: string
  author: Owner
  images: readonly UserImage[]
  /** What the engine appends to Claude Code's own system prompt (Ruling 15). */
  systemAppend: string
  /** The sender's tiers, for a turn sent through the sessions_* tools (`turn_tiers`, #2320). */
  tiers?: readonly Tier[] | undefined
  /** The flow run and step that send it (#2481, SendOptions.flow). */
  flow?: FlowTurn | undefined
}

export type DurableTurnsDeps = {
  client: Client
  sql: Sql
  events: EventLog
  /** Tests only: a queue other than `agent-sessions`. */
  sessionQueue?: string
  /** Tests only: a queue other than `agent-tools`, where `acquire_worker` runs. */
  toolsQueue?: string
  /**
   * The session host queues (`agent-sessions.<identity>`, spec 2026-10-13 §5.2) that poll now, on
   * any replica: a unified session is ready only while there is one. `liveHostQueues` by default;
   * the API's process (`SCADBUDDY_AGENT_ROLE=api`, §14.3) hosts none itself.
   */
  liveHosts?: () => Promise<ReadonlySet<string>>
  /** Where a Stop records the approvals it cancels on a session whose workflow is gone (#4018). */
  audit?: AuditSink | undefined
  /** How long send waits for the `send_message` Update to be accepted (default 10 s); past it the outcome is unknown and the claim is kept. */
  sendTimeoutMs?: number
  /** How long `cancel_input` may take (DurableGate, default 10 s). */
  cancelTimeoutMs?: number
  /** Tests only: how Stop describes a gone workflow (default durableDescriber). */
  describe?: DescribeSession
  /** Run when a Stop ended a lost turn: what a classic one left parked (SessionManager.endClassicLeftovers). */
  ended?: (sessionId: string) => Promise<unknown>
}

const wire = (o: Pick<Owner, 'kind' | 'id'> & { label?: string }): WireOwner => ({ kind: o.kind, id: o.id, label: o.label ?? '' })

/** A protobuf Timestamp (`seconds` may be a Long) as epoch ms; 0 when absent. */
/** A protobuf timestamp (a poller's `lastAccessTime`) as epoch milliseconds; 0 when absent. */
export function seenAtMs(ts: { seconds?: unknown; nanos?: number | null } | null | undefined): number {
  return ts ? Number(String(ts.seconds ?? 0)) * 1000 + Math.floor((ts.nanos ?? 0) / 1e6) : 0
}

/** The `send_message` Update went unanswered: neither taken nor refused, as far as this service knows. */
class OutcomeUnknown extends Error {}

export class DurableTurns {
  readonly #client: Client
  readonly #sql: Sql
  readonly #events: EventLog
  readonly #audit: AuditSink | undefined
  readonly #ended: ((sessionId: string) => Promise<unknown>) | undefined
  readonly #gate: DurableGate
  readonly #blobs: SessionBlobs
  readonly #sessionQueue: string
  readonly #toolsQueue: string
  readonly #liveHosts: () => Promise<ReadonlySet<string>>
  readonly #sendTimeoutMs: number
  #ready: { until: number; why: string | undefined } | undefined
  readonly #describe: DescribeSession

  constructor(deps: DurableTurnsDeps) {
    this.#client = deps.client
    this.#sql = deps.sql
    this.#events = deps.events
    this.#audit = deps.audit
    this.#ended = deps.ended
    this.#gate = new DurableGate(deps.client, deps.cancelTimeoutMs ? { cancelTimeoutMs: deps.cancelTimeoutMs } : {})
    this.#blobs = new SessionBlobs(deps.sql)
    this.#sessionQueue = deps.sessionQueue ?? SESSION_QUEUE
    this.#toolsQueue = deps.toolsQueue ?? TASK_QUEUE
    this.#liveHosts = deps.liveHosts ?? (() => liveHostQueues(deps.client))
    this.#sendTimeoutMs = deps.sendTimeoutMs ?? 10_000
    this.#describe = deps.describe ?? durableDescriber(deps.client)
  }

  /**
   * Why a new session could not run now, or undefined when it could (plan 5d Ruling 2): a fresh
   * poller on every queue its turns need (spec 2026-10-13 §5.1-5.2): the `agent-sessions`
   * workflow worker; the shared `agent-tools` activity worker, where `acquire_worker` places the
   * turn; and at least one session host, `agent-sessions.<identity>`, on any replica
   * (`liveHosts`). The process that sends may host none: the API's (§14.3, plan Step 6.1) runs no
   * turn. Until there is one, a turn would wait on a queue no one reads, so the manager refuses at
   * start instead.
   *
   * An answer from Temporal is reused for READY_TTL_MS, no answer for UNANSWERED_TTL_MS, so one
   * blip does not refuse starts for long. Only a poller seen within POLLER_FRESH_MS counts:
   * Temporal keeps listing one for minutes after its worker has gone.
   */
  async unready(): Promise<string | undefined> {
    const cached = this.#ready
    if (cached && Date.now() < cached.until) return cached.why
    let why: string | undefined
    let answered = true
    try {
      const needed: [string, number, string][] = [
        [this.#sessionQueue, WORKFLOW_TASK_QUEUE, 'session workflow worker'],
        [this.#toolsQueue, ACTIVITY_TASK_QUEUE, 'agent-tools worker'],
      ]
      for (const [queue, type, what] of needed) {
        if (!(await this.#polled(queue, type))) {
          why = `no ${what} polls Temporal's "${queue}" queue`
          break
        }
      }
      if (!why && (await this.#liveHosts()).size === 0) {
        why = 'no session host worker (agent-sessions.<identity>) polls Temporal, so no replica can run a unified session\'s turns'
      }
    } catch {
      why = 'Temporal did not answer'
      answered = false
    }
    this.#ready = { until: Date.now() + (answered ? READY_TTL_MS : UNANSWERED_TTL_MS), why }
    return why
  }

  /** Whether a worker polled `queue` (of `type`) within POLLER_FRESH_MS. */
  async #polled(queue: string, type: number): Promise<boolean> {
    const { pollers } = await this.#client.withDeadline(Date.now() + READY_DEADLINE_MS, () =>
      this.#client.workflowService.describeTaskQueue({
        namespace: this.#client.options.namespace,
        taskQueue: { name: queue },
        taskQueueType: type,
      }),
    )
    const now = Date.now()
    return (pollers ?? []).some((p) => now - seenAtMs(p.lastAccessTime) < POLLER_FRESH_MS)
  }

  /**
   * Sends one user turn to the session's workflow. Undefined when the row cannot be
   * claimed (another turn runs, the session is done, archived or spent, another
   * owner): the manager says why. Refused when the workflow refuses the message; a message not
   * accepted in time keeps the claim and is still a turn (Ruling 16).
   */
  async send(session: SessionRecord, turn: DurableTurn): Promise<Turn | undefined> {
    const id = session.id
    // The images and their previews by name (#2479): the event and the workflow's message carry only these.
    const { blobs, sent } = sentImageBlobs(turn.images)
    // The status before the claim, which a give-back restores (a refused send keeps `failed`).
    // The lock makes a concurrent claim read the committed `running`, so its guarded UPDATE matches nothing.
    // `archived_at` is tested here too: an archive that commits after the manager's check
    // must not let a turn start on a chat the panel no longer lists (#2118).
    // The runtime is recorded by the first send: AgentSession for a session whose DurableSession
    // ran nothing (a classic session's too, once a classic turn still running on an older replica
    // has let its lease go), else `durable`, which is refused below. `durable` stays right after
    // Step 8.1: such a row's conversation is in its DurableSession's history, not in
    // `ai_session_entries`, so an AgentSession would run on with none of it.
    const [claimed] = await this.#sql.unsafe<{ prior_status: SessionStatus; durable_offset: string; runtime: SessionRuntime }[]>(
      `WITH prior AS (SELECT status AS prior_status, durable_offset FROM ai_sessions WHERE id = $1 FOR UPDATE)
       UPDATE ai_sessions SET status = 'running', turn_tiers = $4, durable_turn_id = $5, updated_at = now(), turn_started_at = now(),
         interrupt_requested = false, turn_id = NULL, lease_until = NULL,
         runtime = coalesce(runtime, CASE WHEN prior.durable_offset = 0 THEN 'unified' ELSE 'durable' END)
       FROM prior
       WHERE id = $1 AND owner_kind = $2 AND owner_id = $3
         AND (turn_id IS NULL OR lease_until <= now())
         AND status NOT IN ('running', 'waiting_approval', 'waiting_input', 'done')
         AND archived_at IS NULL AND ${POOL_COST} < ${POOL_BUDGET}
       RETURNING prior.prior_status, prior.durable_offset, ai_sessions.runtime`,
      // The turn's tool activities run with these, cut to what the owner holds then (#2320).
      [
        id,
        turn.author.kind,
        turn.author.id,
        turn.tiers ? this.#sql.json({ by: { kind: turn.author.kind, id: turn.author.id }, tiers: [...turn.tiers] }) : null,
        // The running turn (#2320).
        turn.turnId,
      ],
    )
    if (!claimed) return undefined
    let seq = 0
    const ranTurn = Number(claimed.durable_offset) > 0
    try {
      if (claimed.runtime === 'durable') {
        // A DurableSession that closed before plan Step 7.2 (see the header): its conversation
        // was never moved, and nothing runs its workflow now.
        throw new SessionError('closed', `session ${id}'s conversation ended with the old session runtime; fork it, or continue in a new chat`)
      }
      await this.#blobs.put(id, blobs).catch(() => {
        throw new SessionError('unavailable', `the images for session ${id} could not be stored; send it again in a moment`)
      })
      const seqs = await this.#events.append(id, [
        event({
          type: 'user.turn',
          sessionId: id,
          turnId: turn.turnId,
          text: turn.prompt,
          author: turn.author,
          ...(sent.length ? { images: sent } : {}),
        }),
        event({ type: 'session.status', sessionId: id, status: 'running' }),
      ])
      seq = seqs.at(-1) ?? 0
      await this.#sendMessage(session, turn, sent, ranTurn)
    } catch (err) {
      if (err instanceof OutcomeUnknown) return this.#turn(id, turn.turnId, seq)
      // The refusal is what the caller hears, whatever the give-back meets.
      await this.#giveBack(id, claimed.prior_status).catch(() => {
        console.warn(`durable send: session ${id}'s claim could not be given back`)
      })
      throw err
    }
    return this.#turn(id, turn.turnId, seq)
  }

  #turn(id: string, turnId: string, seq: number): Turn {
    let done: Promise<TurnOutcome> | undefined
    const outcome = () => this.#outcome(id, seq)
    return {
      turnId,
      // Read only when asked for: a follower per turn nobody waits on would poll for nothing.
      get done() {
        done ??= outcome()
        return done
      },
    }
  }

  /**
   * `ranTurn`: the session's workflow has run a turn (its output moved
   * `durable_offset`), so the message goes to that workflow and never starts one: a
   * workflow not found was removed by retention after it ended, and starting
   * `session-<id>` again would run the session's history-holding chat empty (#2078).
   */
  async #sendMessage(session: SessionRecord, turn: DurableTurn, sent: readonly SentImage[], ranTurn: boolean): Promise<void> {
    const start: SessionStart = {
      session_id: session.id,
      creator: wire(session.creator),
      owner: wire(session.owner),
      max_turns: session.maxTurns,
      system_append: turn.systemAppend,
    }
    // The sender's context, which a classic turn starts its spans in (turnCore.ts `TurnTrace`).
    const traceparent = traceparentOf(trace.getSpan(otelContext.active()))
    const message: DurableMessage = {
      turn_id: turn.turnId,
      text: turn.text,
      author: wire(turn.author),
      // The full images, which the turn reads by name; the previews are the panel's.
      images: sent.map((s) => ({ name: s.name!, mediaType: s.mediaType! })),
      system_append: turn.systemAppend,
      ...(turn.flow ? { flow: { run_id: turn.flow.run_id, call_id: turn.flow.call_id } } : {}),
      words: turn.prompt,
      ...(traceparent ? { traceparent } : {}),
    }
    const operation = new WithStartWorkflowOperation<(start: SessionStart) => Promise<void>>(SESSION_WORKFLOW, {
      workflowId: sessionWorkflowId(session.id),
      taskQueue: this.#sessionQueue,
      args: [start],
      workflowIdConflictPolicy: 'USE_EXISTING',
      // A session whose workflow ended (failed, or terminated) lost its conversation:
      // never started afresh under the same session.
      workflowIdReusePolicy: 'REJECT_DUPLICATE',
    })
    // The turn id as the update id: a re-send of the same turn is the same Update.
    const update = { args: [message] as [DurableMessage], updateId: turn.turnId }
    try {
      await this.#client.withDeadline(Date.now() + this.#sendTimeoutMs, () =>
        ranTurn
          ? this.#client.workflow.getHandle(sessionWorkflowId(session.id)).executeUpdate<SendAnswer, [DurableMessage]>(SEND_MESSAGE_UPDATE, update)
          : this.#client.workflow.executeUpdateWithStart<(start: SessionStart) => Promise<void>, SendAnswer, [DurableMessage]>(
              SEND_MESSAGE_UPDATE,
              { ...update, startWorkflowOperation: operation },
            ),
      )
    } catch (err) {
      const cause = err instanceof WorkflowUpdateFailedError ? err.cause : undefined
      if (cause instanceof ApplicationFailure && cause.type === BUSY) {
        throw new SessionError('busy', `a turn is already running in session ${session.id}; wait for it to finish or interrupt it`)
      }
      // A plain Update to an ended workflow is NOT_FOUND whether or not retention has removed it yet.
      if (err instanceof WorkflowNotFoundError || err instanceof WorkflowExecutionAlreadyStartedError) {
        throw new SessionError('closed', `session ${session.id}'s workflow has ended; continue in a new chat`)
      }
      if (err instanceof WorkflowUpdateFailedError) {
        throw new SessionError('unavailable', `session ${session.id}'s workflow did not take the message; send it again in a moment`)
      }
      // Not accepted in time, or the call itself failed: Temporal may hold the Update
      // already and deliver it when a worker polls, so this is no refusal.
      throw new OutcomeUnknown()
    }
  }

  /** Gives back a claim whose message the workflow refused (Ruling 7), to the status it had. */
  async #giveBack(id: string, prior: SessionStatus): Promise<void> {
    const released = await this.#sql`
      UPDATE ai_sessions SET status = ${prior}, durable_turn_id = NULL, updated_at = now() WHERE id = ${id} AND status = 'running'`
    if (released.count > 0) await this.#events.append(id, [event({ type: 'session.status', sessionId: id, status: prior })])
  }

  /** The turn's end, as `session.finish_turn` wrote it (temporal/session/finishActivity.ts). */
  async #outcome(id: string, afterSeq: number): Promise<TurnOutcome> {
    const controller = new AbortController()
    let result: { costUsd: number; turns: number } | undefined
    let error: { code?: string; message: string } | undefined
    let status: SessionStatus | undefined
    try {
      for await (const { event: e } of this.#events.follow(id, afterSeq, controller.signal)) {
        if (e.type === 'session.result') result = { costUsd: e.costUsd ?? 0, turns: e.turns }
        else if (e.type === 'error' && e.questionId === undefined) error = { ...(e.code ? { code: e.code } : {}), message: e.message }
        else if (e.type === 'session.status' && SETTLED.includes(e.status)) {
          status = e.status
          break
        }
      }
    } finally {
      controller.abort()
    }
    if (error?.code === 'interrupted') return { kind: 'interrupted' }
    if (error?.code === 'turn_failed' || status === 'failed') {
      return { kind: 'failed', message: error?.message ?? 'the turn failed' }
    }
    // The outcome carries the session's own spend, as a classic turn's does: its event's is the
    // lineage's (the meter), which a fork shares with its parent (#823).
    const [own] = await this.#sql<{ cost_usd: number }[]>`SELECT cost_usd FROM ai_sessions WHERE id = ${id}`
    return {
      kind: 'result',
      // A result subtype classic reports for an end that leaves the session idle (AgentSession carries it, turnHost.ts).
      subtype: error?.code === 'error_max_budget_usd' || error?.code === 'error_max_turns' ? error.code : 'success',
      costUsd: own?.cost_usd ?? result?.costUsd ?? 0,
      turns: result?.turns ?? 0,
    }
  }

  /**
   * Stops the session's turn: its parked calls are cancelled first, then the Signal.
   * False when no turn runs. When the workflow is gone the turn is ended here, as the
   * running sweep would end it (#endGone); a first turn whose workflow is not started
   * yet is left to it.
   */
  async interrupt(session: SessionRecord, reason: string): Promise<boolean> {
    const [row] = await this.#sql<(RunningRow & { status: SessionStatus; settled: boolean; runtime: SessionRuntime | null })[]>`
      SELECT id, status, event_seq, durable_offset, runtime,
        updated_at < now() - make_interval(secs => ${RUNNING_GRACE_MS / 1000}) AS settled
      FROM ai_sessions WHERE id = ${session.id}`
    if (!row || !RUNNING.includes(row.status)) return false
    if (row.runtime === 'unified') {
      // The fast path (spec §5.6): the turn's replica reads the flag on its next tick and stops
      // the turn there, a held call included, with no further model request. Its parked
      // entries are not cancelled first (a cancelled one is a refusal the model would answer):
      // the Signal below cancels them as the turn ends, and is the backstop for a missed tick.
      await this.#sql`UPDATE ai_sessions SET interrupt_requested = true WHERE id = ${session.id} AND durable_turn_id IS NOT NULL`
    } else {
      try {
        await this.#gate.cancelInput(session.id, reason)
      } catch (err) {
        if (err instanceof WorkflowNotFoundError) return this.#endGone(row)
        // Unanswered: the Signal stops the turn and its parked calls all the same.
        if (!(err instanceof DurableUnavailable)) throw err
      }
    }
    try {
      await this.#gate.interrupt(session.id, reason)
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return this.#endGone(row)
      if (err instanceof DurableUnavailable) {
        throw new SessionError('unavailable', `session ${session.id}'s workflow could not be reached; interrupt it again in a moment`)
      }
      throw err
    }
    return true
  }

  /**
   * Stop on a `running` row whose workflow is gone: nothing there will end the turn,
   * so it is ended here as DurableRunningSweep would end it, rather than left
   * `running` until the sweep's next pass (#4018). Described first (within the
   * describer's deadline), so the ending says which:
   *   - closed (ended): at once; nothing can start that workflow's turn again.
   *   - not found, or a describe that fails (which the Signal just answered): only
   *     past RUNNING_GRACE_MS, as the sweep. Within it a first send's
   *     update-with-start may still be in flight; ending it would lose the Stop
   *     (no workflow took the Signal) while the workflow it starts runs the turn
   *     behind an `idle` row (#4131 review). The sweep ends it later.
   *   - open again (started meanwhile): its turn is its own; nothing is written.
   */
  async #endGone(row: RunningRow & { settled: boolean }): Promise<boolean> {
    const state = await this.#describe(sessionWorkflowId(row.id)).catch(() => 'not_found' as const)
    if (state === 'open') return false
    // `settled` reads RUNNING_GRACE_MS, as DurableRunningSweep does unless it is given its own
    // `graceMs` (durableSweep.ts, the constructor): a sweep given a longer one would still be
    // waiting on a claim this ends.
    if (state !== 'closed' && !row.settled) return false
    const ended = await endLostTurn(this.#sql, this.#events, row, state)
    if (ended) await this.#ended?.(row.id).catch(() => {})
    // A parked turn's entries (#4018): no run will resolve them now, so they are cancelled here
    // rather than left for PendingInputSweep's next look, ten minutes or more away.
    const cancelled = await cancelOrphanedEntries({ sql: this.#sql, events: this.#events, audit: this.#audit }, { sessionId: row.id })
    return ended || cancelled > 0
  }

  /**
   * Ends the parked calls of a session that has just changed owner. Best-effort: the
   * handoff already applied and is never undone or failed here. When `cancel_input`
   * goes unanswered, the `interrupt` Signal (recorded even while no worker runs) ends
   * them and the turn; a session with no workflow has nothing parked.
   */
  async handoff(session: SessionRecord, reason: string, to: Owner): Promise<void> {
    const [row] = await this.#sql<{ runtime: SessionRuntime | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${session.id}`
    try {
      await this.#gate.cancelInput(session.id, reason)
      // An AgentSession's gate checks each later response against the new owner (§5.6).
      if (row?.runtime === 'unified') {
        await this.#gate.ownerChanged(session.id, to).catch(() => {
          console.warn(`durable handoff: session ${session.id}'s workflow could not be told its new owner; it is told at its next turn`)
        })
      }
      return
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return
    }
    await this.#gate.interrupt(session.id, reason).catch(() => {
      // Payload-free: the parked calls may outlive the handoff until the workflow answers.
      console.warn(`durable handoff: session ${session.id}'s workflow could not be reached; its waiting calls were not ended`)
    })
  }
}
