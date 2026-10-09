import {
  type Client,
  WithStartWorkflowOperation,
  WorkflowExecutionAlreadyStartedError,
  WorkflowUpdateFailedError,
} from '@temporalio/client'
import { ApplicationFailure, WorkflowNotFoundError } from '@temporalio/common'
import type { Sql } from 'postgres'
import { DurableGate, DurableUnavailable, sessionWorkflowId } from '../gate/durable.js'
import { type SessionImage, imageOfBlock, SessionBlobs } from './blobs.js'
import type { EventLog } from './eventLog.js'
import { previewsOf, type UserImage } from './images.js'
import { POOL_BUDGET, POOL_COST, SessionError, type SessionRecord, type Turn, type TurnOutcome } from './manager.js'
import { event, type Owner, type SessionStatus } from './protocol.js'

// A durable session's turns (spec 2026-10-01 §6, plan 5c PR 3): what SessionManager
// does instead of running the harness when a session's mode is `durable`. The turn
// runs in agent-durable's DurableSession workflow, `session-<id>` on the `agent` queue;
// this service only hands it the message and reads its end from the event log.
//
// - send (Ruling 7): claim the row (`status = 'running'`, no turn_id or lease: the
//   workflow owns the turn), put the turn's images in ai_session_blobs (the claim
//   check the segment runner reads by name, Ruling 5), write `user.turn` and
//   `session.status running`, then update-with-start `send_message`, starting the
//   workflow on the first message. A refused or unanswered Update gives the claim back.
//   No `cancel_input` is sent here (Ruling 8): a send is refused `busy` while a turn
//   runs, and only a running turn has entries.
// - `done` follows the event log from the turn's own `session.status running` to the
//   status `finish_turn` writes, and is read only when asked for.
// - interrupt: `cancel_input`, then the `interrupt` Signal, which is recorded even while
//   no worker runs.
// - handoff: `cancel_input`; while the workflow does not answer it the handoff is
//   refused `unavailable` (retryable), since the parked calls belong to the old owner.
//
// The wire shapes are agent-durable's session/models.py (`SessionStart`, `Message`,
// `ImageRef`, `Owner`). No prompt, image or tool content is logged or traced here.

export const DURABLE_WORKFLOW = 'DurableSession'
export const DURABLE_TASK_QUEUE = 'agent'
export const SEND_MESSAGE_UPDATE = 'send_message'

/** How long a readiness answer is reused (plan 5d Ruling 2): one Temporal call per window, not per start. */
const READY_TTL_MS = 15_000
/** How long "Temporal did not answer" is reused: short, so a blip passes, but an outage costs one deadline per window. */
const UNANSWERED_TTL_MS = 2_000
/** How long the readiness check waits for Temporal before calling it unreachable. */
const READY_DEADLINE_MS = 3_000
/** temporal.api.enums.v1.TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW */
const WORKFLOW_TASK_QUEUE = 1

/** What the workflow's validator refuses a second turn with (models.py, workflow.py). */
const BUSY = 'busy'
const RUNNING: readonly SessionStatus[] = ['running', 'waiting_approval', 'waiting_input']
const SETTLED: readonly SessionStatus[] = ['idle', 'failed', 'done']

type WireOwner = { kind: string; id: string; label: string }
type ImageRef = { name: string; mediaType: string }

/** DurableSession's input (models.py `SessionStart`); the agent's state starts empty. */
export type SessionStart = {
  session_id: string
  creator: WireOwner
  owner: WireOwner
  max_turns: number
  system_append: string | null
}

/** The `send_message` Update's argument (models.py `Message`). */
export type DurableMessage = { turn_id: string; text: string; author: WireOwner; images: ImageRef[] }
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
}

export type DurableTurnsDeps = {
  client: Client
  sql: Sql
  events: EventLog
  /** Tests only: a queue other than `agent`. */
  taskQueue?: string
  /** How long the `send_message` Update may take to be accepted (default 10 s). */
  sendTimeoutMs?: number
  /** How long `cancel_input` may take (DurableGate, default 10 s). */
  cancelTimeoutMs?: number
}

const wire = (o: Pick<Owner, 'kind' | 'id'> & { label?: string }): WireOwner => ({ kind: o.kind, id: o.id, label: o.label ?? '' })

/** The images as the blobs the worker reads; one the panel's checks let through is always a blob. */
function blobsOf(images: readonly UserImage[]): SessionImage[] {
  return images.map((image, i) => {
    const blob = imageOfBlock({ type: 'image', source: { type: 'base64', media_type: image.mediaType, data: image.data } })
    if (!blob) throw new SessionError('invalid', `images[${i}] is not an image the model can take`)
    return blob
  })
}

export class DurableTurns {
  readonly #client: Client
  readonly #sql: Sql
  readonly #events: EventLog
  readonly #gate: DurableGate
  readonly #blobs: SessionBlobs
  readonly #taskQueue: string
  readonly #sendTimeoutMs: number
  #ready: { until: number; why: string | undefined } | undefined

  constructor(deps: DurableTurnsDeps) {
    this.#client = deps.client
    this.#sql = deps.sql
    this.#events = deps.events
    this.#gate = new DurableGate(deps.client, deps.cancelTimeoutMs ? { cancelTimeoutMs: deps.cancelTimeoutMs } : {})
    this.#blobs = new SessionBlobs(deps.sql)
    this.#taskQueue = deps.taskQueue ?? DURABLE_TASK_QUEUE
    this.#sendTimeoutMs = deps.sendTimeoutMs ?? 10_000
  }

  /**
   * Why a new durable session could not run now, or undefined when it could (plan 5d
   * Ruling 2): some agent-durable worker polls the `agent` queue. Until one does, a
   * turn would wait on a queue no one reads, so the manager falls back or refuses at
   * start instead. An answer from Temporal is reused for READY_TTL_MS, no answer for
   * UNANSWERED_TTL_MS, so one blip does not refuse explicit durable starts for long.
   * Temporal keeps listing a poller for some minutes after its worker has gone, so a
   * worker that died just now still counts as ready until then.
   */
  async unready(): Promise<string | undefined> {
    const cached = this.#ready
    if (cached && Date.now() < cached.until) return cached.why
    let why: string | undefined
    let answered = true
    try {
      const { pollers } = await this.#client.withDeadline(Date.now() + READY_DEADLINE_MS, () =>
        this.#client.workflowService.describeTaskQueue({
          namespace: this.#client.options.namespace,
          taskQueue: { name: this.#taskQueue },
          taskQueueType: WORKFLOW_TASK_QUEUE,
        }),
      )
      if (!pollers?.length) why = `no durable session worker (agent-durable) polls Temporal's "${this.#taskQueue}" queue`
    } catch {
      why = 'Temporal did not answer'
      answered = false
    }
    this.#ready = { until: Date.now() + (answered ? READY_TTL_MS : UNANSWERED_TTL_MS), why }
    return why
  }

  /**
   * Sends one user turn to the session's workflow. Undefined when the row cannot be
   * claimed (another turn runs, the session is done or spent, another owner): the
   * manager says why. Refused when the workflow refuses or does not answer.
   */
  async send(session: SessionRecord, turn: DurableTurn): Promise<Turn | undefined> {
    const id = session.id
    const blobs = blobsOf(turn.images)
    const claimed = await this.#sql.unsafe(
      `UPDATE ai_sessions SET status = 'running', updated_at = now()
       WHERE id = $1 AND mode = 'durable' AND owner_kind = $2 AND owner_id = $3
         AND status NOT IN ('running', 'waiting_approval', 'waiting_input', 'done')
         AND ${POOL_COST} < ${POOL_BUDGET}`,
      [id, turn.author.kind, turn.author.id],
    )
    if (claimed.count === 0) return undefined
    let seq: number
    try {
      await this.#blobs.put(id, blobs)
      const seqs = await this.#events.append(id, [
        event({
          type: 'user.turn',
          sessionId: id,
          turnId: turn.turnId,
          text: turn.prompt,
          author: turn.author,
          ...(turn.images.length ? { images: previewsOf(turn.images) } : {}),
        }),
        event({ type: 'session.status', sessionId: id, status: 'running' }),
      ])
      seq = seqs.at(-1) ?? 0
      await this.#sendMessage(session, turn, blobs)
    } catch (err) {
      await this.#giveBack(id)
      throw err
    }
    let done: Promise<TurnOutcome> | undefined
    const outcome = () => this.#outcome(id, seq)
    return {
      turnId: turn.turnId,
      // Read only when asked for: a follower per turn nobody waits on would poll for nothing.
      get done() {
        done ??= outcome()
        return done
      },
    }
  }

  async #sendMessage(session: SessionRecord, turn: DurableTurn, blobs: readonly SessionImage[]): Promise<void> {
    const start: SessionStart = {
      session_id: session.id,
      creator: wire(session.creator),
      owner: wire(session.owner),
      max_turns: session.maxTurns,
      system_append: turn.systemAppend,
    }
    const message: DurableMessage = {
      turn_id: turn.turnId,
      text: turn.text,
      author: wire(turn.author),
      images: blobs.map((b) => ({ name: b.name, mediaType: b.mediaType })),
    }
    const operation = new WithStartWorkflowOperation<(start: SessionStart) => Promise<void>>(DURABLE_WORKFLOW, {
      workflowId: sessionWorkflowId(session.id),
      taskQueue: this.#taskQueue,
      args: [start],
      workflowIdConflictPolicy: 'USE_EXISTING',
      // A session whose workflow ended (failed, or terminated) lost its conversation:
      // never started afresh under the same session.
      workflowIdReusePolicy: 'REJECT_DUPLICATE',
    })
    try {
      await this.#client.withDeadline(Date.now() + this.#sendTimeoutMs, () =>
        this.#client.workflow.executeUpdateWithStart<(start: SessionStart) => Promise<void>, SendAnswer, [DurableMessage]>(
          SEND_MESSAGE_UPDATE,
          { args: [message], startWorkflowOperation: operation },
        ),
      )
    } catch (err) {
      const cause = err instanceof WorkflowUpdateFailedError ? err.cause : undefined
      if (cause instanceof ApplicationFailure && cause.type === BUSY) {
        throw new SessionError('busy', `a turn is already running in session ${session.id}; wait for it to finish or interrupt it`)
      }
      if (err instanceof WorkflowExecutionAlreadyStartedError) {
        throw new SessionError('closed', `session ${session.id}'s workflow has ended; continue in a new chat`)
      }
      throw new SessionError('unavailable', `session ${session.id}'s worker did not take the message; send it again in a moment`)
    }
  }

  /** Gives back a claim whose message the workflow did not take (Ruling 7). */
  async #giveBack(id: string): Promise<void> {
    const released = await this.#sql`
      UPDATE ai_sessions SET status = 'idle', updated_at = now() WHERE id = ${id} AND status = 'running'`
    if (released.count > 0) await this.#events.append(id, [event({ type: 'session.status', sessionId: id, status: 'idle' })])
  }

  /** The turn's end, as finish_turn wrote it (agent-durable session/activities.py). */
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
    return {
      kind: 'result',
      subtype: error?.code === 'error_max_budget_usd' ? 'error_max_budget_usd' : 'success',
      costUsd: result?.costUsd ?? 0,
      turns: result?.turns ?? 0,
    }
  }

  /**
   * Stops the session's turn: its parked calls are cancelled first, then the Signal.
   * False when no turn runs, or the session has no workflow yet.
   */
  async interrupt(session: SessionRecord, reason: string): Promise<boolean> {
    const [row] = await this.#sql<{ status: SessionStatus }[]>`SELECT status FROM ai_sessions WHERE id = ${session.id}`
    if (!row || !RUNNING.includes(row.status)) return false
    try {
      await this.#gate.cancelInput(session.id, reason)
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return false
      // Unanswered: the Signal stops the turn and its parked calls all the same.
      if (!(err instanceof DurableUnavailable)) throw err
    }
    try {
      await this.#gate.interrupt(session.id, reason)
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return false
      if (err instanceof DurableUnavailable) {
        throw new SessionError('unavailable', `session ${session.id}'s workflow could not be reached; interrupt it again in a moment`)
      }
      throw err
    }
    return true
  }

  /** Ends the parked calls before the session changes owner; refused while the workflow does not answer. */
  async handoff(session: SessionRecord, reason: string): Promise<void> {
    try {
      await this.#gate.cancelInput(session.id, reason)
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return // no turn ever ran: nothing parked
      if (err instanceof DurableUnavailable) {
        throw new SessionError(
          'unavailable',
          `session ${session.id}'s workflow did not answer, so its waiting calls could not be ended; try the handoff again in a moment`,
        )
      }
      throw err
    }
  }
}
