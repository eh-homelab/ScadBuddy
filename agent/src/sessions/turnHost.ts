import { randomUUID } from 'node:crypto'
import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { ApplicationFailure } from '@temporalio/common'
import type { Sql, TransactionSql } from 'postgres'
import { turnCostEntry } from '../audit/turn.js'
import type { SessionTurnInput, SessionTurnResult } from '../temporal/session/models.js'
import { SESSION_GONE, SHUTTING_DOWN_FAILURE } from '../temporal/session/names.js'
import type { SessionBlobs } from './blobs.js'
import { userPrompt } from './images.js'
import { type ClaimedSession, runningDurableTurn, type TurnOutcome } from './manager.js'
import { scrubForLog } from './sdkEvents.js'
import { CoreTurn, type TallyInput, tallyTurn, type TurnCoreDeps, type TurnTally } from './turnCore.js'
import type { WarmEntry } from './warm.js'

// An AgentSession's turn on this replica (unified runtime spec 2026-10-13 §5.2, §5.5): what
// the `session_turn` activity runs (temporal/session/turnActivity.ts). The turn is a classic
// one (turnCore.ts, as SessionManager.runTurn runs it) on the session's warm Claude Code:
//
//   1. the claim: the row runs exactly this turn (`durable_turn_id`, DurableTurns.send's), and
//      this placement of it (`turn_attempt`, the workflow's run id and cycle);
//   2. assemble (TurnRunner), acquire the warm process, push the message;
//   3. the events, as a classic turn writes them (sdkEvents.ts);
//   4. the turn's cost as it goes (FENCE_MS, and each finished request), so a placement that
//      dies before its result still counts against the budget; at its end the cost and turns
//      as a classic turn reckons them, as increments over what it wrote; the closing events
//      (`tail`) are the workflow's `session.finish_turn`.
//
// Placements (§5.2 "Recovery"). A placement the workflow gave up on may still run here (its
// heartbeat stalled, it ran past `turn_max_seconds`). Each of its event appends and transcript
// appends checks, in its own transaction, that the row is still this placement's
// (`turn_attempt`), so none lands once another has claimed the turn. Every FENCE_MS it also reads
// the row, and once another placement holds it, or the turn is no longer the row's, it is cut
// off: its process closed. Its spend is always written: its requests are its own, never the
// next placement's, so the budget counts every replica that ran the turn, once. A new placement
// here for the same session cuts an older one first, so the warm process is free.
//
// A `restarted` continuation (§5.5) is decided from the transcript, not from the workflow's
// word: the first placement marks the main transcript's last entry (`turn_mark`), and a later
// one resumes with RESTARTED_PROMPT only when a user entry landed after it (the message went
// in, and Claude Code wrote it); otherwise it sends the message itself. Parks (§5.4) are Task
// 5.3's: until then an outward call, a question or a tab wait is refused with NO_PARKS.
//
// A shutdown (`stop`) gives the running turns their grace, then cuts the rest: only those fail
// with the retryable ShuttingDown, which places the turn on another replica; a turn that ended
// on its own in the grace (a reply, a refusal) keeps its end.

export const RESTARTED_PROMPT =
  'The agent service restarted during this turn. Calls that were in flight are marked as interrupted; check before repeating any of them. Continue the task.'
/** What a call that would park reads until the workflow can hold it (Task 5.3). */
export const NO_PARKS = 'this session cannot wait for an approval or an answer yet, so the call was not run'
/** How often a running placement checks that it still holds the turn, and writes what it has spent. */
export const FENCE_MS = 1_000

/** What `session_turn` heartbeats: whether the message has gone into Claude Code. Advisory (logs, history); the transcript decides a restart. */
export type TurnBeat = { pushed: boolean }

/** Why a placement's activity was cancelled: the workflow's interrupt, the worker stopping, or the workflow no longer waiting on it (timed out). */
export type CancelReason = 'interrupt' | 'shutdown' | 'gone'

export type TurnControl = {
  heartbeat: (beat: TurnBeat) => void
  cancelled: AbortSignal
  /** Read when `cancelled` fires. */
  cancelReason: () => CancelReason
}

/** Why a placement was cut off: another holds the row, the turn ended, a newer one here, the shutdown. */
type Cut = 'moved' | 'ended' | 'replaced' | 'shutdown'
const CUT_TEXT: Record<Cut, string> = {
  moved: 'the turn was placed on another worker',
  ended: 'the turn ended without this placement',
  replaced: 'a newer placement of the session took over',
  shutdown: 'the agent service is shutting down',
}

export type TurnHostDeps = {
  /** The replica's turn machinery (SessionManager.turnCore): its warm processes, events, audit. */
  core: TurnCoreDeps
  sql: Sql
  /** The message's images, by the names DurableTurns.send stored them under; and a tool result's, stored under the fence. */
  images: Pick<SessionBlobs, 'get' | 'putIf'>
  /** This replica's session host queue (`agent-sessions.<identity>`), what each result names. */
  hostQueue: string
  /** Tests only: the fence's period (FENCE_MS). */
  fenceMs?: number
}

type Running = { attempt: string; cut: (why: Cut) => void; done: Promise<void> }

const within = (ms: number, work: Promise<unknown>) => Promise.race([work, new Promise((resolve) => setTimeout(resolve, ms).unref())])

export class TurnHost {
  readonly #deps: TurnHostDeps
  #stopping = false
  /** The placement running here for each session. */
  readonly #running = new Map<string, Running>()

  constructor(deps: TurnHostDeps) {
    this.#deps = deps
  }

  /** The service is shutting down: no turn is cut yet (`stop` does that past the grace). */
  drain(): void {
    this.#stopping = true
  }

  get stopping(): boolean {
    return this.#stopping
  }

  /** How many placements run here now. */
  get active(): number {
    return this.#running.size
  }

  /**
   * SIGTERM (main.ts): the running turns get `graceMs` to end on their own; the rest are cut
   * (each then fails ShuttingDown, and its workflow places it elsewhere) and get `abortWaitMs`
   * to write what they spent.
   */
  async stop({ graceMs, abortWaitMs }: { graceMs: number; abortWaitMs: number }): Promise<void> {
    this.drain()
    const ended = () => Promise.all([...this.#running.values()].map((r) => r.done))
    await within(graceMs, ended())
    if (this.#running.size === 0) return
    for (const r of this.#running.values()) r.cut('shutdown')
    await within(abortWaitMs, ended())
  }

  async run(input: SessionTurnInput, control: TurnControl): Promise<SessionTurnResult> {
    const id = input.sessionId
    const attempt = input.attempt ?? randomUUID()
    // An older placement of the session still here (cut at its time cap, its activity not yet told): it goes first.
    const before = this.#running.get(id)
    if (before) {
      before.cut('replaced')
      await within(10_000, before.done)
    }
    let finished!: () => void
    const done = new Promise<void>((resolve) => {
      finished = resolve
    })
    let cut: Cut | undefined
    const entries: WarmEntry[] = []
    const controller = new AbortController()
    const cutOff = (why: Cut) => {
      if (cut !== undefined) return
      cut = why
      controller.abort(new Error(CUT_TEXT[why]))
      // A shutdown leaves the process to end as a stopped turn's does; any other cut is a placement nobody waits on any more.
      if (why !== 'shutdown') for (const entry of entries) void this.#deps.core.warm.cut(entry, CUT_TEXT[why])
    }
    const self: Running = { attempt, cut: cutOff, done }
    this.#running.set(id, self)
    try {
      return await this.#run(input, attempt, control, controller, () => cut, cutOff, entries)
    } finally {
      if (this.#running.get(id) === self) this.#running.delete(id)
      finished()
    }
  }

  async #run(
    input: SessionTurnInput,
    attempt: string,
    control: TurnControl,
    controller: AbortController,
    cutBy: () => Cut | undefined,
    cutOff: (why: Cut) => void,
    entries: WarmEntry[],
  ): Promise<SessionTurnResult> {
    const { sessionId: id, turnId } = input
    const sql = this.#deps.sql
    const mark = await this.#claim(id, turnId, attempt)
    const claim = mark === undefined ? undefined : await runningDurableTurn(sql, id, turnId)
    if (mark === undefined || !claim) throw ApplicationFailure.nonRetryable(`session ${id} is not running turn ${turnId}`, SESSION_GONE)
    const { session, principal } = claim
    // §5.5: resumed with the restart message only once the turn's prompt is in the transcript.
    const restarted = input.continuation?.kind === 'restarted' && (!input.message || (await this.#landed(id, mark)))
    const text = restarted ? RESTARTED_PROMPT : (input.message?.text ?? '')
    if (!text) throw ApplicationFailure.nonRetryable(`turn ${turnId} of session ${id} has no message`, SESSION_GONE)
    const images = restarted ? [] : await this.#images(id, input.message?.images ?? [])
    const prompt = images.length ? userPrompt(text, images) : text

    const onCancel = () => {
      const why = control.cancelReason()
      if (why === 'interrupt') controller.abort(new Error('the turn was interrupted'))
      else cutOff(why === 'shutdown' ? 'shutdown' : 'ended')
    }
    control.cancelled.addEventListener('abort', onCancel, { once: true })
    if (control.cancelled.aborted) onCancel()
    /** Spoken for: nothing more of this placement is written once it is cut off (a shutdown's still is). */
    const fenced = () => {
      const why = cutBy()
      return why !== undefined && why !== 'shutdown'
    }
    const spend = new Spend(sql, id)
    /**
     * This placement still holds its turn: checked, row locked, in each write's own transaction.
     * The turn as well as the attempt: an attempt id is the run's and the cycle's, so the next
     * turn's first placement in the same run carries this one's, and the turn ended leaves it.
     */
    const holds = async (tx: TransactionSql, lock: 'share' | 'update') => {
      const rows = lock === 'share'
        ? await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_attempt = ${attempt} AND durable_turn_id = ${turnId} FOR SHARE`
        : await tx`SELECT 1 FROM ai_sessions WHERE id = ${id} AND turn_attempt = ${attempt} AND durable_turn_id = ${turnId} FOR UPDATE`
      return rows.length > 0
    }
    const events = this.#deps.core.events
    const core: CoreTurn = new CoreTurn(this.#deps.core, {
      session,
      turnId,
      principal,
      userText: input.message?.text ?? text,
      signal: controller.signal,
      interrupt: () => controller.abort(new Error('the turn was interrupted')),
      gates: () => ({
        approvalGate: () => Promise.resolve({ approved: false, message: NO_PARKS }),
        questionGate: () => Promise.resolve({ answered: false, message: NO_PARKS }),
        waitForTab: () => Promise.resolve({ back: false, message: NO_PARKS }),
      }),
      // The prompt goes in right after the acquire.
      onAcquired: ({ entry }) => {
        entries.push(entry)
        // Its transcript appends only while this placement holds the turn.
        this.#deps.core.warm.fence(entry, (tx) => holds(tx, 'share'))
        if (fenced()) void this.#deps.core.warm.cut(entry, 'the placement was cut off')
        control.heartbeat({ pushed: true })
      },
      onMessage: (message) => {
        if (message.type === 'stream_event' && message.event.type === 'message_stop') spend.flush(core.runningUsd())
      },
      mayWrite: () => !fenced(),
      // A tool result's images, and its events, only while this placement holds the turn, checked with the write.
      storeImages: (images) => this.#deps.images.putIf(id, images, (tx) => holds(tx, 'share')),
      appendEvents: async (batch) => {
        const written = await sql.begin(async (tx) => ((await holds(tx, 'update')) ? await events.append(id, batch, tx) : undefined))
        if (written) events.committed(id, batch, written)
        return written !== undefined
      },
    })
    // The fence: still the row's placement and turn, and what it has spent so far written.
    const fence = setInterval(() => {
      void this.#holds(id, turnId, attempt).then(
        (holds) => {
          if (holds !== 'yes') cutOff(holds)
          else spend.flush(core.runningUsd())
        },
        () => {},
      )
    }, this.#deps.fenceMs ?? FENCE_MS)
    fence.unref()
    let outcome: TurnOutcome = { kind: 'failed', message: 'the turn could not finish' }
    let result: SDKResultMessage | undefined
    try {
      const run = await core.run(prompt)
      clearInterval(fence)
      result = run.result
      const stopped = controller.signal.aborted ? (controller.signal.reason instanceof Error ? controller.signal.reason.message : 'the turn was interrupted') : undefined
      const end: TallyInput = {
        stopped,
        interrupted: stopped,
        result,
        refused: result && stopped === undefined ? run.refused : undefined,
        failure: run.failure,
        secrets: run.secrets,
        turnUsd: run.spentUsd,
        cutUsd: stopped !== undefined || !result ? run.cutUsd : 0,
      }
      const tally = tallyTurn(session, end)
      outcome = tally.outcome
      await this.#write(session, turnId, tally, end, spend, core.runningUsd())
      const why = cutBy()
      if (why === 'shutdown') throw ApplicationFailure.retryable(CUT_TEXT.shutdown, SHUTTING_DOWN_FAILURE, { pushed: entries.length > 0 })
      if (why !== undefined) throw ApplicationFailure.nonRetryable(`session ${id}'s placement ${attempt} was cut off: ${CUT_TEXT[why]}`, SESSION_GONE)
      return this.#result(tally, run.secrets)
    } catch (err) {
      core.traced.fail(err)
      throw err
    } finally {
      clearInterval(fence)
      control.cancelled.removeEventListener('abort', onCancel)
      core.end(outcome, result)
    }
  }

  /**
   * Takes the turn for this placement: `turn_attempt` is it from now on, and the turn's mark
   * (the main transcript's last entry before its first placement) is set once. The mark, or
   * undefined when the row does not run this turn.
   */
  async #claim(id: string, turnId: string, attempt: string): Promise<number | undefined> {
    const [row] = await this.#deps.sql<{ turn_mark: string }[]>`
      UPDATE ai_sessions SET
        turn_attempt = ${attempt},
        turn_mark = CASE WHEN turn_mark_turn = ${turnId} THEN turn_mark ELSE (
          SELECT coalesce(max(e.id), 0) FROM ai_session_entries e WHERE e.session_id = ai_sessions.id::text AND e.subpath = ''
        ) END,
        turn_mark_turn = ${turnId}
      WHERE id = ${id} AND durable_turn_id = ${turnId} AND status IN ('running', 'waiting_approval', 'waiting_input')
      RETURNING turn_mark::text`
    return row ? Number(row.turn_mark) : undefined
  }

  /** Whether a user entry (the turn's prompt, or a tool result after it) landed in the main transcript after the mark. */
  async #landed(id: string, mark: number): Promise<boolean> {
    const [row] = await this.#deps.sql<{ landed: boolean }[]>`
      SELECT EXISTS (
        SELECT 1 FROM ai_session_entries
        WHERE session_id = ${id} AND subpath = '' AND id > ${mark}
          AND (entry_type = 'user' OR (entry_type IS NULL AND entry LIKE '%"type":"user"%'))
      ) AS landed`
    return row?.landed === true
  }

  /** Whether the row still runs this turn on this placement: `moved` when another placement holds it, `ended` when the turn is over. */
  async #holds(id: string, turnId: string, attempt: string): Promise<'yes' | 'moved' | 'ended'> {
    const [row] = await this.#deps.sql<{ turn_attempt: string | null; durable_turn_id: string | null }[]>`
      SELECT turn_attempt, durable_turn_id FROM ai_sessions WHERE id = ${id}`
    if (!row) return 'ended'
    if (row.turn_attempt !== attempt) return 'moved'
    return row.durable_turn_id === turnId ? 'yes' : 'ended'
  }

  async #images(id: string, refs: readonly { name: string }[]) {
    const images = []
    for (const ref of refs) {
      const blob = await this.#deps.images.get(id, ref.name)
      if (blob) images.push(blob)
    }
    return images
  }

  /**
   * The turn's spend and turns on the row (manager.ts `finish`'s columns), as increments over
   * what it wrote as it ran, and its audit row. Whether or not the row still runs this turn or
   * this placement: a placement the workflow ended (its StartToClose) or replaced spent what it
   * spent, on requests no other placement made, and the budget counts it.
   */
  async #write(session: ClaimedSession, turnId: string, tally: TurnTally, end: TallyInput, spend: Spend, runningUsd: number): Promise<void> {
    // With a result, Claude Code's price; without one (cut, failed), what its requests cost at the
    // table's prices, the finished ones too, which no result will ever price.
    const spent = end.result ? tally.costUsd - session.ownCostUsd : Math.max(tally.costUsd - session.ownCostUsd, runningUsd)
    const estimated = end.result ? end.cutUsd : spent
    const wrote = await spend.settled()
    const [written] = await this.#deps.sql<{ id: string }[]>`
      UPDATE ai_sessions
      SET cost_usd = cost_usd + ${spent - wrote}, turns = turns + ${tally.turns - session.turns},
          unpriced_cost_usd = unpriced_cost_usd + ${estimated - wrote}, updated_at = now()
      WHERE id = ${session.id}
      RETURNING id`
    if (!written) return
    // #1922: what the turn cost, as its own audit row (audit/turn.ts turnCostEntry).
    await this.#deps.core.audit?.record(
      turnCostEntry(
        { sessionId: session.id, turnId, actor: session.owner },
        tally.outcome,
        { costUsd: spent, priced: end.result !== undefined, estimatedUsd: estimated },
        end.secrets,
      ),
    )
  }

  /** The end as the workflow writes it: classic's closing events (`tail`, scrubbed) and error code (turnCore.ts `tallyTurn`). */
  #result(tally: TurnTally, secrets: readonly string[]): SessionTurnResult {
    const hostQueue = this.#deps.hostQueue
    const tail = tally.tail.map((e) => scrubForLog(e, secrets))
    const o = tally.outcome
    if (o.kind === 'interrupted') return { status: 'interrupted', hostQueue, tail }
    if (o.kind === 'result' && o.subtype === 'success') return { status: 'done', hostQueue, tail }
    if (o.kind === 'result' && o.subtype === 'error_max_budget_usd') return { status: 'budget_exhausted', hostQueue, tail }
    const said = tail.find((e) => e.type === 'error')
    const code = said?.type === 'error' ? said.code : undefined
    const error = said?.type === 'error' ? said.message : o.kind === 'failed' ? o.message : `the turn stopped (${o.subtype})`
    return { status: tally.status === 'failed' ? 'failed' : 'done', hostQueue, error, tail, ...(code !== undefined ? { code } : {}) }
  }
}

/**
 * What a placement has spent so far, on the row as it goes: each write adds what the turn's
 * requests cost since the last (CoreTurn.runningUsd, the table's prices), to `cost_usd` and to
 * `unpriced_cost_usd` (Claude Code has priced none of it yet), whether or not the row is still
 * this placement's. The end (`TurnHost#write`) replaces it with the turn's own reckoning.
 */
class Spend {
  readonly sql: Sql
  readonly id: string
  #written = 0
  #chain: Promise<void> = Promise.resolve()

  constructor(sql: Sql, id: string) {
    this.sql = sql
    this.id = id
  }

  flush(runningUsd: number): void {
    this.#chain = this.#chain.then(async () => {
      const delta = runningUsd - this.#written
      if (delta <= 0) return
      const [row] = await this.sql<{ id: string }[]>`
        UPDATE ai_sessions SET cost_usd = cost_usd + ${delta}, unpriced_cost_usd = unpriced_cost_usd + ${delta}, updated_at = now()
        WHERE id = ${this.id}
        RETURNING id`
      if (row) this.#written += delta
    }).catch(() => {})
  }

  /** What was written, once the writes in flight have landed. */
  async settled(): Promise<number> {
    await this.#chain
    return this.#written
  }
}
