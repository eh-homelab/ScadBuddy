import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import {
  isGrpcDeadlineError,
  type Client,
  WithStartWorkflowOperation,
  WorkflowExecutionAlreadyStartedError,
  WorkflowUpdateFailedError,
} from '@temporalio/client'
import { ApplicationFailure, WorkflowNotFoundError } from '@temporalio/common'
import type { Sql } from 'postgres'
import type { Tier } from '../auth/principal.js'
import type { FlowTurn } from '../tools/registry.js'
import { DurableGate, DurableUnavailable, sessionWorkflowId } from '../gate/durable.js'
import { TASK_QUEUE } from '../temporal/names.js'
import { SESSION_QUEUE, UNIFIED_SESSIONS_SETTING, SESSION_WORKFLOW } from '../temporal/session/names.js'
import { SessionBlobs, sentImageBlobs } from './blobs.js'
import { type DescribeSession, durableDescriber, endLostTurn, RUNNING_GRACE_MS, type RunningRow } from './durableSweep.js'
import { cancelOrphanedEntries } from '../gate/sweep.js'
import type { AuditSink } from '../audit/log.js'
import type { EventLog } from './eventLog.js'
import type { UserImage } from './images.js'
import { POOL_BUDGET, POOL_COST, SessionError, type SessionRecord, type Turn, type TurnOutcome } from './manager.js'
import { event, type Owner, type SentImage, type SessionStatus } from './protocol.js'

// A durable session's turns (spec 2026-10-01 §6, plan 5c PR 3): what SessionManager
// does instead of running the harness when a session's mode is `durable`. The turn
// runs in agent-durable's DurableSession workflow, `session-<id>` on the `agent` queue;
// this service only hands it the message and reads its end from the event log.
//
// - send (Ruling 7): claim the row (`status = 'running'`, no turn_id or lease: the
//   workflow owns the turn), put the turn's images in ai_session_blobs (the claim
//   check the segment runner reads by name, Ruling 5), write `user.turn` and
//   `session.status running`, then update-with-start `send_message` (update id: the
//   turn id), starting the workflow on the first message. Once a turn has run
//   (`durable_offset` moved) it is a plain Update: a workflow removed by retention is
//   refused `closed`, never restarted empty (Ruling 18). Only a definite refusal (the
//   validator's `busy`, a failed Update, an ended workflow) gives the claim back. An
//   Update not accepted in time keeps it: Temporal may already hold the message, which a
//   worker delivers later, so the turn is handed back and `done` follows the log.
//   No `cancel_input` is sent here (Ruling 8): a send is refused `busy` while a turn
//   runs, and only a running turn has entries.
// - `done` follows the event log from the turn's own `session.status running` to the
//   status `finish_turn` writes, and is read only when asked for.
// - interrupt: `cancel_input`, then the `interrupt` Signal, which is recorded even while
//   no worker runs.
// - fork (#2688): the parent's conversation is read from its workflow (`fork_point`, a
//   page at a time), and the child's workflow is started with a copy of it as the
//   agent's state, sealed under the child's own key like every payload of `session-<child>`.
// - handoff: after the owner change is committed, `cancel_input`, best-effort; when
//   it goes unanswered the `interrupt` Signal ends the parked calls (and the turn).
//
// Which workflow runs a session is its `runtime` column, fixed by its first send (unified
// runtime spec 2026-10-13 §5.1): the setting `unified_sessions = true` starts a NEW
// session's workflow as AgentSession on `agent-sessions`; every other session, and every one
// that has run a turn already, is a DurableSession on `agent`, for life. The gate calls, the
// Updates after the first and the signals are by workflow id, the same for both types.
//
// The wire shapes are agent-durable's session/models.py (`SessionStart`, `Message`,
// `ImageRef`, `Owner`). No prompt, image or tool content is logged or traced here.

export const DURABLE_WORKFLOW = 'DurableSession'
export const DURABLE_TASK_QUEUE = 'agent'
export const SEND_MESSAGE_UPDATE = 'send_message'
/** `ai_sessions.runtime`: the workflow type a session runs as. */
export type SessionRuntime = 'durable' | 'unified'
export const FORK_POINT_QUERY = 'fork_point'
/** The most characters of entries one `fork_point` page carries: under Temporal's 512 KiB warning. */
const FORK_PAGE_CHARS = 384 * 1024
/** How often reading the parent's pages starts again when a turn commits while they are read. */
const FORK_READS = 3

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

/** What the workflow's validator refuses a second turn with (models.py, workflow.py). */
const BUSY = 'busy'
const RUNNING: readonly SessionStatus[] = ['running', 'waiting_approval', 'waiting_input']
export const SETTLED: readonly SessionStatus[] = ['idle', 'failed', 'done']

type WireOwner = { kind: string; id: string; label: string }
type ImageRef = { name: string; mediaType: string }

/** A tool result the plugin holds for Claude (its `ToolOutcome`), passed through as it is. */
type ToolOutcome = { content: unknown; is_error: boolean; blocks: unknown[] | null }

/**
 * What the agent still owes at its checkpoint (#4173): the results its next segment
 * delivers (a stopped turn's paused calls), whether that segment continues in a copy of
 * the session, and the calls that already ran, as the plugin's `AgentState` keeps them.
 */
export type Owed = { pending: Record<string, ToolOutcome>; fork_next: boolean; recent_call_ids: string[] }

/**
 * The part of the plugin's `AgentState` a fork seeds (#2688): the Claude session, where it
 * was last committed, its transcript entries as JSON text, as the workflow keeps them, and
 * what it still owes there (#4173).
 */
export type ForkedAgent = { session_id: string; checkpoint: string; conversation: string[] } & Owed

/** DurableSession's input (models.py `SessionStart`); the agent's state starts empty, or a fork's copy. */
export type SessionStart = {
  session_id: string
  creator: WireOwner
  owner: WireOwner
  max_turns: number
  system_append: string | null
  agent?: ForkedAgent
}

/** One page of `fork_point` (models.py `ForkPoint`). */
type ForkPointPage = { session_id: string | null; checkpoint: string | null; busy: boolean; total: number; entries: string[] } & Partial<Owed>

/** A durable session's committed conversation, for a fork. */
export type ForkSource = {
  /** The Claude session the entries belong to, and the entry it was last committed at. */
  claudeSessionId: string
  checkpoint: string
  /** A turn runs: what it has written past the checkpoint is not in `entries`' committed part yet. */
  busy: boolean
  entries: SessionStoreEntry[]
  /** What the agent owes at `checkpoint` (the first page's): only a child cut there takes it. */
  owed: Owed
}

/** The `send_message` Update's argument (models.py `Message`); each turn carries its own append (#2525). */
export type DurableMessage = {
  turn_id: string
  text: string
  author: WireOwner
  images: ImageRef[]
  system_append: string | null
  /** A turn a flow run sent (#2481, models.py `FlowTurn`); absent for any other. */
  flow?: FlowTurn
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
  /** Tests only: a queue other than `agent`. */
  taskQueue?: string
  /** Tests only: a queue other than `agent-sessions`. */
  sessionQueue?: string
  /** Tests only: a queue other than `agent-tools`, where `acquire_worker` runs. */
  toolsQueue?: string
  /**
   * This replica's session host queue (`agent-sessions.<identity>`, spec 2026-10-13 §5.2), when it
   * serves AgentSession's turns; without it a unified session is not ready here.
   */
  sessionHostQueue?: string
  /** Where a Stop records the approvals it cancels on a session whose workflow is gone (#4018). */
  audit?: AuditSink | undefined
  /** How long send waits for the `send_message` Update to be accepted (default 10 s); past it the outcome is unknown and the claim is kept. */
  sendTimeoutMs?: number
  /** How long `cancel_input` may take (DurableGate, default 10 s). */
  cancelTimeoutMs?: number
  /** Tests only: how Stop describes a gone workflow (default durableDescriber). */
  describe?: DescribeSession
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
  readonly #gate: DurableGate
  readonly #blobs: SessionBlobs
  readonly #taskQueue: string
  readonly #sessionQueue: string
  readonly #toolsQueue: string
  readonly #sessionHostQueue: string | undefined
  readonly #sendTimeoutMs: number
  readonly #ready = new Map<SessionRuntime, { until: number; why: string | undefined }>()
  readonly #describe: DescribeSession

  constructor(deps: DurableTurnsDeps) {
    this.#client = deps.client
    this.#sql = deps.sql
    this.#events = deps.events
    this.#audit = deps.audit
    this.#gate = new DurableGate(deps.client, deps.cancelTimeoutMs ? { cancelTimeoutMs: deps.cancelTimeoutMs } : {})
    this.#blobs = new SessionBlobs(deps.sql)
    this.#taskQueue = deps.taskQueue ?? DURABLE_TASK_QUEUE
    this.#sessionQueue = deps.sessionQueue ?? SESSION_QUEUE
    this.#toolsQueue = deps.toolsQueue ?? TASK_QUEUE
    this.#sessionHostQueue = deps.sessionHostQueue
    this.#sendTimeoutMs = deps.sendTimeoutMs ?? 10_000
    this.#describe = deps.describe ?? durableDescriber(deps.client)
  }

  /** The runtime a session that has run nothing would get at its first send: the `unified_sessions` flag (spec 2026-10-13 §5.1). */
  async newRuntime(): Promise<SessionRuntime> {
    const [row] = await this.#sql<{ value: unknown }[]>`SELECT value FROM ai_settings WHERE key = ${UNIFIED_SESSIONS_SETTING}`
    return row?.value === true ? 'unified' : 'durable'
  }

  /**
   * Why a new durable session of `runtime` could not run now, or undefined when it could (plan
   * 5d Ruling 2): a fresh poller on every queue its turns need. Until there is one, a turn
   * would wait on a queue no one reads, so the manager falls back or refuses at start instead.
   *
   * - `durable` (DurableSession): agent-durable's workflow worker on `agent`.
   * - `unified` (AgentSession, spec 2026-10-13 §5.1-5.2): the `agent-sessions` workflow worker;
   *   the shared `agent-tools` activity worker, where `acquire_worker` places the turn; and this
   *   replica's own `agent-sessions.<identity>`, the turn's host. Another replica's host queue
   *   cannot be listed without knowing its identity; this one's stands for the build serving turns.
   *
   * An answer from Temporal is reused for READY_TTL_MS, no answer for UNANSWERED_TTL_MS, so one
   * blip does not refuse explicit durable starts for long. Only a poller seen within
   * POLLER_FRESH_MS counts: Temporal keeps listing one for minutes after its worker has gone.
   */
  async unready(runtime: SessionRuntime = 'durable'): Promise<string | undefined> {
    const cached = this.#ready.get(runtime)
    if (cached && Date.now() < cached.until) return cached.why
    let why: string | undefined
    let answered = true
    try {
      if (runtime === 'durable') {
        if (!(await this.#polled(this.#taskQueue, WORKFLOW_TASK_QUEUE))) {
          why = `no durable session worker (agent-durable) polls Temporal's "${this.#taskQueue}" queue`
        }
      } else if (!this.#sessionHostQueue) {
        why = 'this agent service runs no session host worker (agent-sessions.<identity>), so it cannot place a unified session\'s turns'
      } else {
        const needed: [string, number, string][] = [
          [this.#sessionQueue, WORKFLOW_TASK_QUEUE, 'session workflow worker'],
          [this.#toolsQueue, ACTIVITY_TASK_QUEUE, 'agent-tools worker'],
          [this.#sessionHostQueue, ACTIVITY_TASK_QUEUE, 'session host worker'],
        ]
        for (const [queue, type, what] of needed) {
          if (!(await this.#polled(queue, type))) {
            why = `no ${what} polls Temporal's "${queue}" queue`
            break
          }
        }
      }
    } catch {
      why = 'Temporal did not answer'
      answered = false
    }
    this.#ready.set(runtime, { until: Date.now() + (answered ? READY_TTL_MS : UNANSWERED_TTL_MS), why })
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
    // The runtime is recorded by the first send: the flag for a session that has run nothing, else DurableSession.
    const flagged = (await this.newRuntime()) === 'unified'
    const [claimed] = await this.#sql.unsafe<{ prior_status: SessionStatus; durable_offset: string; runtime: SessionRuntime }[]>(
      `WITH prior AS (SELECT status AS prior_status, durable_offset FROM ai_sessions WHERE id = $1 FOR UPDATE)
       UPDATE ai_sessions SET status = 'running', turn_tiers = $4, durable_turn_id = $5, updated_at = now(),
         interrupt_requested = false,
         runtime = coalesce(runtime, CASE WHEN prior.durable_offset = 0 AND $6::boolean THEN 'unified' ELSE 'durable' END)
       FROM prior
       WHERE id = $1 AND mode = 'durable' AND owner_kind = $2 AND owner_id = $3
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
        flagged,
      ],
    )
    if (!claimed) return undefined
    let seq = 0
    try {
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
      await this.#sendMessage(session, turn, sent, Number(claimed.durable_offset) > 0, claimed.runtime)
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
  async #sendMessage(session: SessionRecord, turn: DurableTurn, sent: readonly SentImage[], ranTurn: boolean, runtime: SessionRuntime): Promise<void> {
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
      // The full images, which the segment runner reads by name (Ruling 5); the previews are the panel's.
      images: sent.map((s) => ({ name: s.name!, mediaType: s.mediaType! })),
      system_append: turn.systemAppend,
      ...(turn.flow ? { flow: { run_id: turn.flow.run_id, call_id: turn.flow.call_id } } : {}),
    }
    const unified = runtime === 'unified'
    const operation = new WithStartWorkflowOperation<(start: SessionStart) => Promise<void>>(unified ? SESSION_WORKFLOW : DURABLE_WORKFLOW, {
      workflowId: sessionWorkflowId(session.id),
      taskQueue: unified ? this.#sessionQueue : this.#taskQueue,
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

  /**
   * The parent's committed conversation (#2688), read from its workflow a page at a time.
   * Undefined when it has none: no workflow (no turn ever ran, or retention removed it),
   * or nothing committed yet. A turn that commits while the pages are read changes the
   * count, and the read starts again.
   */
  async forkSource(sessionId: string): Promise<ForkSource | undefined> {
    // Phase 6 forks an AgentSession session (its transcript is in ai_session_entries, not its workflow).
    const [row] = await this.#sql<{ runtime: SessionRuntime | null }[]>`SELECT runtime FROM ai_sessions WHERE id = ${sessionId}`
    if (row?.runtime === 'unified') {
      throw new SessionError('invalid', `session ${sessionId} runs on the unified runtime, which cannot be forked yet; this is not supported until its fork lands`)
    }
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    const page = (start: number) =>
      this.#client.withDeadline(Date.now() + this.#sendTimeoutMs, () =>
        handle.query<ForkPointPage, [{ start: number; limit: number }]>(FORK_POINT_QUERY, { start, limit: FORK_PAGE_CHARS }),
      )
    try {
      for (let read = 0; read < FORK_READS; read++) {
        const first = await page(0)
        if (!first.session_id || !first.checkpoint || first.total === 0) return undefined
        const texts = [...first.entries]
        let moved = false
        while (texts.length < first.total) {
          const next = await page(texts.length)
          if (next.total !== first.total || next.checkpoint !== first.checkpoint || next.entries.length === 0) {
            moved = true
            break
          }
          texts.push(...next.entries)
        }
        if (moved) continue
        return {
          claudeSessionId: first.session_id,
          checkpoint: first.checkpoint,
          busy: first.busy,
          entries: texts.map((t) => JSON.parse(t) as SessionStoreEntry),
          // A worker from before #4173 sends none: nothing owed it can say.
          owed: { pending: first.pending ?? {}, fork_next: first.fork_next ?? false, recent_call_ids: first.recent_call_ids ?? [] },
        }
      }
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return undefined
      // #3739: the cause is the one thing that says why a fork keeps failing, so it is
      // logged; the caller hears only which of the two it was.
      console.warn(`durable fork: session ${sessionId}'s workflow could not be read: ${String(err)}`)
      if (isGrpcDeadlineError(err)) {
        throw new SessionError('unavailable', `no durable session worker answered for session ${sessionId} in time to fork it; try again in a moment`)
      }
      throw new SessionError('unavailable', `session ${sessionId}'s workflow could not be read to fork it; try again in a moment`)
    }
    throw new SessionError('busy', `session ${sessionId} kept moving while it was read; fork it again once its turn ends`)
  }

  /**
   * Starts a fork's workflow with the conversation it carries (#2688), before its first
   * message: that message's update-with-start then finds it running (`USE_EXISTING`).
   * Refused `unavailable` when Temporal does not take it in time.
   */
  async startFork(child: SessionRecord, agent: ForkedAgent): Promise<void> {
    const start: SessionStart = {
      session_id: child.id,
      creator: wire(child.creator),
      owner: wire(child.owner),
      max_turns: child.maxTurns,
      // Each message carries the append as Settings stand when it is sent (#2525).
      system_append: null,
      agent,
    }
    // Its workflow is a DurableSession whatever the flag says, so its first send must not pick the type again.
    await this.#sql`UPDATE ai_sessions SET runtime = 'durable' WHERE id = ${child.id}`
    try {
      await this.#client.withDeadline(Date.now() + this.#sendTimeoutMs, () =>
        this.#client.workflow.start<(start: SessionStart) => Promise<void>>(DURABLE_WORKFLOW, {
          workflowId: sessionWorkflowId(child.id),
          taskQueue: this.#taskQueue,
          args: [start],
          workflowIdReusePolicy: 'REJECT_DUPLICATE',
        }),
      )
    } catch {
      // Taken or not, a fork whose workflow is in doubt is not handed out: its first
      // message would start it empty.
      await this.#client.workflow
        .getHandle(sessionWorkflowId(child.id))
        .terminate('the fork was not completed')
        .catch(() => {})
      throw new SessionError('unavailable', `the fork of session ${child.id} could not be started; try again in a moment`)
    }
  }

  /** Gives back a claim whose message the workflow refused (Ruling 7), to the status it had. */
  async #giveBack(id: string, prior: SessionStatus): Promise<void> {
    const released = await this.#sql`
      UPDATE ai_sessions SET status = ${prior}, durable_turn_id = NULL, updated_at = now() WHERE id = ${id} AND status = 'running'`
    if (released.count > 0) await this.#events.append(id, [event({ type: 'session.status', sessionId: id, status: prior })])
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
      // A result subtype classic reports for an end that leaves the session idle (AgentSession carries it, turnHost.ts).
      subtype: error?.code === 'error_max_budget_usd' || error?.code === 'error_max_turns' ? error.code : 'success',
      costUsd: result?.costUsd ?? 0,
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
