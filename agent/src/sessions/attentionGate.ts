import { RECONNECTED_REASON, redact, TIMEOUT_MIN_S, WAIT_FOR_USER } from '../gate/entry.js'
import type { AttentionSpec } from '../harness/attention.js'
import { ATTENTION_TOOL, type QuestionRequest, type QuestionVerdict } from '../harness/questions.js'
import type { QuestionService } from '../questions/service.js'
import type { OpenInputArgs } from '../temporal/session/models.js'

// An AgentSession turn's attention requests that wait (#5003, spec 2026-10-13 §5.4):
// request_user_attention, and a browser call's wait for its tab (sessions/manager.ts
// `waitForTab`, built on this gate as on classic's), each the gate's `wait_for_user` entry,
// opened through the session's workflow and held on its row (sessions/turnHost.ts, whose park
// and hold these are). What each call reads is classic's (questions/service.ts `gate`,
// harness/attention.ts):
//   - a reply is the answer; `reconnected` (the row's response, gate/entry.ts
//     RECONNECTED_RESPONSE) is the tab's return, never an answer;
//   - the entry's timer (the workflow's) answers `timed_out`: `proceed` returns that, `stop`
//     and `wait` end the turn as an interrupt does;
//   - #815 §5's rate limits refuse it before anything parks (QuestionService.attentionLimit);
//   - #1308: one card per disconnected tab. The model's own `tab_disconnected` request and a
//     browser call's tab wait join the other side's open one, waiting on its row to their own
//     deadline; that row ending without the user (its timer, its withdrawal) does not end the
//     joiner, which asks again on its own. Claude Code runs one assistant message's MCP calls
//     one after another (2.1.296, measured in test/sessionPark.temporal.test.ts), so only calls
//     in parallel (a subagent's) meet here.
// A wait whose timer ends within the turn's hold is held to it, plus ATTENTION_GRACE_MS, so the
// default tab wait (TAB_WAIT_S, as long as the default hold) ends in the turn.

/** How long past its own timer an attention request is held, for the workflow to write its end (resolve_input). */
export const ATTENTION_GRACE_MS = 30_000
/** A tab wait's call id is this plus the browser call's tool_use id: an entry of its own, never that call's (whose approval may have parked under its id). */
export const TAB_WAIT_CALL = 'tab:'

/** An entry's response row, as resolve_input (or a grant, or the flow's refusal) wrote it. */
export type HeldRow = { outcome: string; response: { answers?: unknown; reconnected?: unknown } | null; reason: string | null; responder: { kind?: string } | null }

/** The one reply an entry's response row carries, if it was answered. */
export function replyOf(row: HeldRow | undefined): string | undefined {
  const answers = Array.isArray(row?.response?.answers) ? row.response.answers.filter((a): a is string => typeof a === 'string') : []
  return row?.outcome === 'answered' ? answers[0] : undefined
}

type Wait = Exclude<AttentionSpec, { reason: 'done' }>
type Response = HeldRow

export type AttentionGateDeps = {
  session: { id: string }
  turnId: string
  /** The turn's own signal: a Stop, a cut. */
  stop: AbortSignal
  secrets: () => readonly string[]
  /** Opens the call's entry through the workflow (`open_input`) and holds on its row (TurnHost's park). */
  park: (
    call: OpenInputArgs['call'],
    signal: AbortSignal,
    options: { holdMs?: number; opened?: (requestId: string) => void },
  ) => Promise<{ requestId: string; row?: Response; early?: unknown }>
  /** Holds on an entry's response row until `signal` aborts (TurnHost's hold). */
  hold: (requestId: string, signal: AbortSignal) => Promise<Response>
  /** How long a held call holds the turn's process, and the release that ends every hold. */
  holdMs: number
  released: AbortSignal
  /** What a released call reads (it also ends the turn: an MCP tool's result cannot interrupt it). */
  release: () => QuestionVerdict
  /** Ends the turn as a Stop does, saying why. */
  endTurn: (why: string) => void
  questions?: Pick<QuestionService, 'attentionLimit'>
}

/** The turn's gate for the attention requests that wait: one per placement, so a join finds this placement's cards. */
export function attentionGate(deps: AttentionGateDeps): (request: QuestionRequest, attention: Wait) => Promise<QuestionVerdict> {
  const { session, turnId, stop, secrets, endTurn } = deps
  /** This placement's open tab waits, by side (`true`: the model's own request; `false`: a browser call's): the other side joins one. */
  const tabCards = new Map<boolean, string>()
  const turnStopped: QuestionVerdict = { answered: false, message: 'The user did not answer: the turn stopped first.' }
  /** What an attention request's call reads once its row (its own, or the one it joined) says how it ended. */
  const verdictOf = (row: Response, question: string, attention: Wait): QuestionVerdict => {
    const reply = replyOf(row)
    if (reply !== undefined) return { answered: true, answers: { [question]: reply } }
    if (row.outcome === 'cancelled' && row.response?.reconnected === true) return { answered: false, reconnected: true, message: row.reason ?? RECONNECTED_REASON }
    if (row.outcome === 'timed_out') {
      const reason = row.reason ?? `nobody replied in time (on_timeout: ${attention.onTimeout})`
      if (attention.onTimeout === 'proceed') return { answered: false, timedOut: true, message: reason }
      // `wait` waited to its ceiling and then does what `stop` does. Never an answer, never an approval.
      const why = `nobody replied to the attention request in ${attention.timeoutS} s (on_timeout: ${attention.onTimeout})`
      endTurn(`the turn ended: ${why}`)
      return { answered: false, message: `The user did not reply: ${why}. Your turn ends here.` }
    }
    return { answered: false, message: `The user did not answer: ${row.reason ?? 'the question was cancelled'}.` }
  }
  /**
   * request_user_attention, or a browser call's wait for its tab (see the header): its card,
   * its timer, its ends. `until` (performance.now ms): a joiner asking again on its own keeps
   * the deadline it was first asked with, as classic's does (questions/service.ts `gate`).
   */
  const attend = async (request: QuestionRequest, attention: Wait, until?: number): Promise<QuestionVerdict> => {
    if (stop.aborted) return { answered: false, message: 'The turn is stopping; the question was not asked.' }
    const card = request.questions[0]
    if (!card) return { answered: false, message: 'The user was not asked: the request has no message.' }
    const own = request.tool === ATTENTION_TOOL
    const deadline = until ?? performance.now() + attention.timeoutS * 1000
    if (attention.reason === 'tab_disconnected') {
      // #1308, #815 §5: one card per disconnected tab. The model's own request and a browser
      // call's wait join an open one of the other side rather than open a second.
      const joined = tabCards.get(!own)
      if (joined !== undefined) return join(request, attention, joined, deadline)
    }
    const left = Math.ceil((deadline - performance.now()) / 1000)
    if (left <= 0) return verdictOf({ outcome: 'timed_out', response: null, reason: null, responder: null }, card.question, attention)
    const limited = await deps.questions?.attentionLimit({ sessionId: session.id, turnId, toolUseId: request.toolUseId, tool: request.tool, reason: attention.reason, own })
    if (limited !== undefined) return { answered: false, message: limited }
    // Redacted as the classic QuestionService redacts the card; the workflow checks the rest (gate/entry.ts).
    const hidden = secrets()
    // A joiner asking again has what is left of its own timer, at least the gate's floor (gate/entry.ts TIMEOUT_MIN_S).
    const timeoutS = until === undefined ? attention.timeoutS : Math.max(left, TIMEOUT_MIN_S)
    const input = {
      reason: attention.reason,
      message: redact(card.question, hidden),
      options: card.options.map((o) => redact(o.label, hidden)),
      timeout_s: timeoutS,
      on_timeout: attention.onTimeout,
    }
    const timerMs = timeoutS * 1000
    // The check `onParked` makes, beside the hold: it stops with the wait, and what it settles is read from the row.
    const checking = new AbortController()
    let tabCard: string | undefined
    let parked
    try {
      parked = await deps.park(
        { id: own ? request.toolUseId : `${TAB_WAIT_CALL}${request.toolUseId}`, name: WAIT_FOR_USER, input },
        AbortSignal.any([stop, request.signal]),
        {
          ...(timerMs <= deps.holdMs ? { holdMs: timerMs + ATTENTION_GRACE_MS } : {}),
          opened: (rid) => {
            if (attention.reason === 'tab_disconnected' && !tabCards.has(own)) {
              tabCard = rid
              tabCards.set(own, rid)
            }
            void attention.onParked?.(checking.signal, rid).catch(() => {})
          },
        },
      )
    } catch (err) {
      if (deps.released.aborted) return deps.release()
      if (stop.aborted || request.signal.aborted) return turnStopped
      return { answered: false, message: `The user was not reached: the request could not be recorded (${err instanceof Error ? err.message : String(err)}).` }
    } finally {
      checking.abort()
      if (tabCard !== undefined && tabCards.get(own) === tabCard) tabCards.delete(own)
    }
    const { row, early } = parked
    if (!row) {
      return { answered: false, message: early ? 'The user was not asked: the request is not the shape request_user_attention takes.' : 'The user did not answer: the request is no longer recorded.' }
    }
    return verdictOf(row, card.question, attention)
  }
  /**
   * A tab wait joined to the other side's open card (`requestId`): it waits on that row to its
   * own deadline. Only a reply or the tab's return on it ends this call; that row ending without
   * the user (its own timer, its withdrawal) does not, and this call asks again on its own.
   */
  const join = async (request: QuestionRequest, attention: Wait, requestId: string, deadline: number): Promise<QuestionVerdict> => {
    const question = request.questions[0]!.question
    const due = new AbortController()
    const timer = setTimeout(() => due.abort(new Error('due')), Math.max(0, deadline - performance.now()))
    timer.unref()
    const checking = new AbortController()
    void attention.onParked?.(checking.signal, requestId).catch(() => {})
    let row: Response
    try {
      row = await deps.hold(requestId, AbortSignal.any([stop, request.signal, deps.released, due.signal]))
    } catch {
      if (deps.released.aborted) return deps.release()
      if (stop.aborted || request.signal.aborted) return turnStopped
      // Its own timer: its own wait ends, and the row stays the other side's.
      return verdictOf({ outcome: 'timed_out', response: null, reason: null, responder: null }, question, attention)
    } finally {
      clearTimeout(timer)
      checking.abort()
    }
    if (replyOf(row) !== undefined || row.response?.reconnected === true) return verdictOf(row, question, attention)
    return attend(request, attention, deadline)
  }
  return (request, attention) => attend(request, attention)
}
