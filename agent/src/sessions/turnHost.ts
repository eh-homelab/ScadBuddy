import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { ApplicationFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import { turnCostEntry } from '../audit/turn.js'
import type { SessionTurnInput, SessionTurnResult } from '../temporal/session/models.js'
import { SESSION_GONE } from '../temporal/session/names.js'
import type { SessionBlobs } from './blobs.js'
import { userPrompt } from './images.js'
import { type ClaimedSession, runningDurableTurn, type TurnOutcome } from './manager.js'
import { CoreTurn, type TallyInput, tallyTurn, type TurnCoreDeps, type TurnTally } from './turnCore.js'

// An AgentSession's turn on this replica (unified runtime spec 2026-10-13 §5.2, §5.5): what
// the `session_turn` activity runs (temporal/session/turnActivity.ts). The turn is a classic
// one (turnCore.ts, as SessionManager.runTurn runs it) on the session's warm Claude Code:
//
//   1. the claim: the row runs exactly this turn (`durable_turn_id`, DurableTurns.send's);
//   2. assemble (TurnRunner), acquire the warm process, push the message;
//   3. the events, as a classic turn writes them (sdkEvents.ts);
//   4. the turn's cost and turns, as increments, so a turn cut by a lost worker and resumed
//      elsewhere, or cut at its time cap, counts what every attempt spent; its end
//      (`session.result`, the error classic would write, the status) is the workflow's
//      `session.finish_turn`, from the code this returns.
//
// A `restarted` continuation (§5.5) resumes the transcript with RESTARTED_PROMPT in place of
// the message, which the cut attempt already sent. Parks (§5.4) are Task 5.3's: until then an
// outward call, a question or a tab wait is refused with NO_PARKS, never left waiting.

export const RESTARTED_PROMPT =
  'The agent service restarted during this turn. Calls that were in flight are marked as interrupted; check before repeating any of them. Continue the task.'
/** What a call that would park reads until the workflow can hold it (Task 5.3). */
export const NO_PARKS = 'this session cannot wait for an approval or an answer yet, so the call was not run'

/** What `session_turn` heartbeats: whether the message has gone into Claude Code, so a resumed turn knows to send it or not. */
export type TurnBeat = { pushed: boolean }

export type TurnHostDeps = {
  /** The replica's turn machinery (SessionManager.turnCore): its warm processes, events, audit. */
  core: TurnCoreDeps
  sql: Sql
  /** The message's images, by the names DurableTurns.send stored them under. */
  images: Pick<SessionBlobs, 'get'>
  /** This replica's session host queue (`agent-sessions.<identity>`), what each result names. */
  hostQueue: string
}

export class TurnHost {
  readonly #deps: TurnHostDeps
  #stopping = false

  constructor(deps: TurnHostDeps) {
    this.#deps = deps
  }

  /** The service is shutting down: a turn cut from now on is the next replica's to resume. */
  drain(): void {
    this.#stopping = true
  }

  get stopping(): boolean {
    return this.#stopping
  }

  async run(input: SessionTurnInput, heartbeat: (beat: TurnBeat) => void, cancelled: AbortSignal): Promise<SessionTurnResult> {
    const { sessionId: id, turnId } = input
    const claim = await runningDurableTurn(this.#deps.sql, id, turnId)
    if (!claim) throw ApplicationFailure.nonRetryable(`session ${id} is not running turn ${turnId}`, SESSION_GONE)
    const { session, principal } = claim
    const restarted = input.continuation?.kind === 'restarted'
    const text = restarted ? RESTARTED_PROMPT : (input.message?.text ?? '')
    if (!text) throw ApplicationFailure.nonRetryable(`turn ${turnId} of session ${id} has no message`, SESSION_GONE)
    const images = restarted ? [] : await this.#images(id, input.message?.images ?? [])
    const prompt = images.length ? userPrompt(text, images) : text

    const controller = new AbortController()
    const onCancel = () => controller.abort(new Error('the turn was interrupted'))
    cancelled.addEventListener('abort', onCancel, { once: true })
    if (cancelled.aborted) onCancel()
    const core = new CoreTurn(this.#deps.core, {
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
      onAcquired: () => heartbeat({ pushed: true }),
    })
    let outcome: TurnOutcome = { kind: 'failed', message: 'the turn could not finish' }
    let result: SDKResultMessage | undefined
    try {
      const run = await core.run(prompt)
      result = run.result
      const stopped = controller.signal.aborted ? 'the turn was interrupted' : undefined
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
      await this.#write(session, turnId, tally, end)
      return this.#result(tally)
    } catch (err) {
      core.traced.fail(err)
      throw err
    } finally {
      cancelled.removeEventListener('abort', onCancel)
      core.end(outcome, result)
    }
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
   * The turn's spend and turns on the row (manager.ts `finish`'s columns), as increments, and its
   * audit row. Whether or not the row still runs this turn: an attempt the workflow gave up on
   * (its StartToClose, a lost heartbeat) ends after `session.finish_turn`, and what it spent is
   * still the session's.
   */
  async #write(session: ClaimedSession, turnId: string, tally: TurnTally, end: TallyInput): Promise<void> {
    const spent = tally.costUsd - session.ownCostUsd
    const [written] = await this.#deps.sql<{ id: string }[]>`
      UPDATE ai_sessions
      SET cost_usd = cost_usd + ${spent}, turns = turns + ${tally.turns - session.turns},
          unpriced_cost_usd = unpriced_cost_usd + ${end.cutUsd}, updated_at = now()
      WHERE id = ${session.id}
      RETURNING id`
    if (!written) return
    // #1922: what the turn cost, as its own audit row (audit/turn.ts turnCostEntry).
    await this.#deps.core.audit?.record(
      turnCostEntry(
        { sessionId: session.id, turnId, actor: session.owner },
        tally.outcome,
        { costUsd: spent, priced: end.result !== undefined, estimatedUsd: end.cutUsd },
        end.secrets,
      ),
    )
  }

  /** The end as the workflow writes it: classic's error code and status (turnCore.ts `tallyTurn`) carried in `code`. */
  #result(tally: TurnTally): SessionTurnResult {
    const hostQueue = this.#deps.hostQueue
    const o = tally.outcome
    if (o.kind === 'interrupted') return { status: 'interrupted', hostQueue }
    if (o.kind === 'result' && o.subtype === 'success') return { status: 'done', hostQueue }
    if (o.kind === 'result' && o.subtype === 'error_max_budget_usd') return { status: 'budget_exhausted', hostQueue }
    const said = tally.tail.find((e) => e.type === 'error')
    const code = said?.type === 'error' ? said.code : undefined
    const error = said?.type === 'error' ? said.message : o.kind === 'failed' ? o.message : `the turn stopped (${o.subtype})`
    return { status: tally.status === 'failed' ? 'failed' : 'done', hostQueue, error, ...(code !== undefined ? { code } : {}) }
  }
}
