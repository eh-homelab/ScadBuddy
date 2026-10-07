import { z } from 'zod'
import type { QuestionGate, UserQuestion } from './questions.js'

// Attention requests (#815): the agent's `request_user_attention` tool, an
// `answer`-kind entry at the session's tool-call gate (durable-agents spec
// §6.6, PR #1070). It is not a system of its own: the call parks on the turn's
// QuestionGate like ask_user, stored in `ai_questions` with `kind =
// 'attention'`, shown on the panel's question card and counted by the badge
// (GET /api/v1/ai/pending-input). The user acknowledges it by answering.
//
// THE TIMER NEVER ANSWERS FOR THE USER. Unlike a question, an attention request
// has a timer (`timeout_s`, default 300 s), and what it does is #815's rule:
//   - `proceed` (the default): the call returns `timed_out`, and the agent goes
//     on with work that needs no approval. Any outward call it then makes still
//     parks for its own approval (approvals/service.ts), so a timeout never lets
//     anything outward run;
//   - `wait`: it stays parked to the ceiling (WAIT_CEILING_S), then does what
//     `stop` does;
//   - `stop`: the turn ends, as an interrupt ends it.
// #815's `approval_pending` reason is refused as malformed: an approval is
// already its own entry, with its own card, and must never time out to proceed.
//
// `done` IS NOT A WAIT (#815 §4, the "done" summary). The agent posts it when it
// finishes work the user stepped away from, and the call returns at once: there
// is no timer, no quick reply the model would read, and the row outlives its turn
// so the badge still counts it when the user comes back. The user dismisses it
// on its card. ScadBuddy adds its own record of what the turn touched
// (questions/doneSummary.ts), split around the attention request nobody
// answered, so what was done unattended is read first.

export const ATTENTION_TOOL_NAME = 'request_user_attention'

export const ATTENTION_REASONS = ['tab_disconnected', 'question', 'blocked', 'done'] as const
export type AttentionReason = (typeof ATTENTION_REASONS)[number]

export const ON_TIMEOUT = ['proceed', 'wait', 'stop'] as const
export type OnTimeout = (typeof ON_TIMEOUT)[number]

/** #815's default window. */
export const DEFAULT_TIMEOUT_S = 300
/** Bounds on `timeout_s`, as `approval_expiry_seconds` (spec §6.6); out of bounds is refused, never clamped. */
export const TIMEOUT_MIN_S = 10
export const TIMEOUT_MAX_S = 86_400
/** How long `wait` keeps the call parked before it stops the turn (spec §6.6, "Timeouts"). */
export const WAIT_CEILING_S = TIMEOUT_MAX_S
/** The message as the card shows it; the questions' own bound. */
export const MESSAGE_MAX = 2_000

/** The quick replies the card offers when the agent names none. */
export const DEFAULT_REPLIES = ["I'm here", 'Carry on without me'] as const
/** A tab wait's reply that means the tab is back (sessions/manager.ts waitForTab). */
export const IM_BACK = "I'm back"
/**
 * Replies that say only "the tab is back": a reconnect that resolved the row first
 * already did what they ask (questions/service.ts answer). Any other reply is words
 * the model would never read, so it is not one of these.
 */
export const BACK_REPLIES: readonly string[] = [IM_BACK, DEFAULT_REPLIES[0]]

/**
 * A `done` card's replies. The panel shows one Dismiss button, which answers with
 * the first; the card still carries two because every card is a question, which
 * has at least two options (harness/questions.ts OPTIONS_MIN).
 */
export const DONE_REPLIES = ['Dismiss', 'Got it'] as const

/** What the card's header says for each reason. */
const HEADERS: Record<AttentionReason, string> = {
  tab_disconnected: 'Tab disconnected',
  question: 'Question',
  blocked: 'Blocked',
  done: 'Done',
}

export const AttentionInputSchema = z.strictObject({
  reason: z.enum(ATTENTION_REASONS),
  message: z.string().trim().min(1).max(MESSAGE_MAX),
  options: z
    .array(z.string().trim().min(1).max(200))
    .min(2)
    .max(4)
    .refine((o) => new Set(o).size === o.length, 'each option must be different')
    .optional(),
  timeout_s: z.number().int().min(TIMEOUT_MIN_S).max(TIMEOUT_MAX_S).default(DEFAULT_TIMEOUT_S),
  on_timeout: z.enum(ON_TIMEOUT).default('proceed'),
})
export type AttentionInput = z.output<typeof AttentionInputSchema>

/**
 * The shape the tool is declared with. The SDK's MCP server validates a call
 * against it first, and on SDK 0.3.283 a `.default()` field the model left out
 * failed there ("expected nonoptional, received undefined"; the calls in
 * test/attention.sdk.test.ts omit them; 0.3.287 fills it, #1540), so the
 * defaults are left to `parseAttention`.
 * `reason` is a string here so `approval_pending` reaches parseAttention's own
 * explanation rather than a bare enum error.
 */
export const ATTENTION_TOOL_SHAPE = {
  reason: z.string().describe(`One of ${ATTENTION_REASONS.join(', ')}.`),
  message: z.string().describe('Shown to the user: what you need, in a sentence or two.'),
  options: z.array(z.string()).optional().describe('Two to four quick replies; the user may type their own instead.'),
  timeout_s: z.number().optional().describe(`Seconds to wait, ${TIMEOUT_MIN_S} to ${TIMEOUT_MAX_S}; default ${DEFAULT_TIMEOUT_S}.`),
  on_timeout: z.enum(ON_TIMEOUT).optional().describe("What happens when nobody replies; default 'proceed'."),
}

/** What the gate needs to know about an attention request, beside its card. */
export type AttentionSpec =
  | {
      reason: Exclude<AttentionReason, 'done'>
      onTimeout: OnTimeout
      /** Seconds until the timer fires: `timeout_s`, or WAIT_CEILING_S for `wait`. */
      timeoutS: number
      /**
       * Called once the request is recorded and shown (questions/service.ts `gate`).
       * `signal` aborts once the wait stops waiting for it (its timer, an abort): a
       * check still running then must not act, or it could end a later wait's request.
       */
      onParked?: (signal: AbortSignal) => Promise<void>
    }
  /** A done summary: posted, never waited on (see above). */
  | { reason: 'done' }

/** What a `done` request refuses: each is about waiting for a reply, and nobody waits on one. */
const NOT_FOR_DONE = ['options', 'timeout_s', 'on_timeout'] as const

/** The request's input, or why it is refused (and nothing parks). */
export function parseAttention(input: unknown): { ok: true; input: AttentionInput } | { ok: false; error: string } {
  if (typeof input === 'object' && input !== null && (input as { reason?: unknown }).reason === 'approval_pending') {
    return {
      ok: false,
      error:
        "reason 'approval_pending' is not an attention request: an outward call already waits for its own approval, " +
        'which the user is shown and which never times out to proceed',
    }
  }
  if (typeof input === 'object' && input !== null && (input as { reason?: unknown }).reason === 'done') {
    const named = NOT_FOR_DONE.filter((k) => (input as Record<string, unknown>)[k] !== undefined)
    if (named.length > 0) {
      return {
        ok: false,
        error:
          `reason 'done' takes no ${named.join(', ')}: a done summary does not wait for a reply. ` +
          'Post it with a message only, then finish your turn.',
      }
    }
  }
  const parsed = AttentionInputSchema.safeParse(input)
  return parsed.success ? { ok: true, input: parsed.data } : { ok: false, error: z.prettifyError(parsed.error) }
}

/** The card the panel shows: one question, the message, and the quick replies. */
export function attentionCard(input: AttentionInput): UserQuestion {
  return {
    question: input.message,
    header: HEADERS[input.reason],
    multiSelect: false,
    options: (input.reason === 'done' ? DONE_REPLIES : (input.options ?? DEFAULT_REPLIES)).map((label) => ({ label, description: '' })),
  }
}

export function attentionSpec(input: AttentionInput): AttentionSpec {
  if (input.reason === 'done') return { reason: 'done' }
  return {
    reason: input.reason,
    onTimeout: input.on_timeout,
    timeoutS: input.on_timeout === 'wait' ? WAIT_CEILING_S : input.timeout_s,
  }
}

/** The result the model reads when the user answered: their choice or their words, JSON-quoted. */
export function answeredText(answer: string): string {
  return `answered: the user replied ${JSON.stringify(answer)}. Continue with that in mind.`
}

/**
 * The result the model reads when `proceed`'s timer fired. Not an error: the
 * user was told and did not answer, and the agent may go on, within limits
 * that the gate enforces anyway.
 */
export function timedOutText(seconds: number): string {
  return (
    `timed_out: the user did not reply within ${seconds} s. Carry on with work that needs no approval only ` +
    '(for example render_model instead of browser_render, or save a preset instead of setting values in the tab). ' +
    'A timeout never approves anything: a print, send, delete, or settings or credential write still waits for ' +
    "the user's explicit approval. When you finish, post request_user_attention with reason 'done' saying what " +
    'you did while they were away and what still needs them.'
  )
}

/** The result the model reads when its `done` summary was posted. */
export const POSTED_TEXT =
  "posted: the user sees your summary on the Assistant badge until they dismiss it, with ScadBuddy's own list " +
  'of what this turn created, changed or deleted. Nobody is waiting on you and no reply will reach you: finish your turn.'

/** The result the model reads when the session's tab came back (#815 §2): not a reply, but the wait is over. */
export const RECONNECTED_TEXT =
  'reconnected: the ScadBuddy tab is connected again (the user has not replied). A browser_* call that failed was ' +
  'not run since, and the page may have reloaded or changed while the tab was away: re-check it (browser_status, ' +
  'then browser_snapshot) before calling a browser_* tool that changes it again.'

/** What Claude Code puts in an MCP call's `_meta` (measured on 2.1.283 and 2.1.287; questions.ts). */
const TOOL_USE_ID_META = 'claudecode/toolUseId'

const ExtraSchema = z.object({
  signal: z.instanceof(AbortSignal),
  _meta: z.object({ [TOOL_USE_ID_META]: z.string().min(1).optional() }).loose().optional(),
})

type ToolResult = { content: { type: 'text'; text: string }[]; isError?: boolean }

const text = (t: string, isError = false): ToolResult => ({ content: [{ type: 'text', text: t }], ...(isError ? { isError } : {}) })

/** request_user_attention's handler (exported for tests). `tool` is its harness name. */
export async function attentionHandler(gate: QuestionGate, tool: string, args: unknown, extra: unknown): Promise<ToolResult> {
  const context = ExtraSchema.safeParse(extra)
  if (!context.success) {
    return text(`The user was not asked: the call's context is not as expected (${z.prettifyError(context.error)}).`, true)
  }
  const toolUseId = context.data._meta?.[TOOL_USE_ID_META]
  if (toolUseId === undefined) return text('The user was not asked: the call has no tool_use id.', true)
  const parsed = parseAttention(args)
  if (!parsed.ok) return text(`The user was not asked: ${parsed.error}`, true)
  const card = attentionCard(parsed.input)
  const attention = attentionSpec(parsed.input)
  try {
    const verdict = await gate({ tool, questions: [card], toolUseId, signal: context.data.signal, attention })
    if (verdict.answered) return text(answeredText(verdict.answers[card.question] ?? ''))
    if (verdict.posted) return text(POSTED_TEXT)
    if (verdict.timedOut && attention.reason !== 'done') return text(timedOutText(attention.timeoutS))
    if (verdict.reconnected) return text(RECONNECTED_TEXT)
    return text(verdict.message, true)
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err)
    return text(`The user was not reached: the request could not complete (${why}).`, true)
  }
}

/** The tool's description, as the model reads it. */
export const ATTENTION_DESCRIPTION =
  'Get the user\'s attention and wait for their reply: shown in the ScadBuddy panel and counted on the Assistant ' +
  'badge. Use it when you cannot go on without them (`blocked`, a `question` that has no fixed choices). ' +
  'Do not use it after a browser_* call finds no tab: that call already waits for the tab and asks the user ' +
  'itself, and its result says what they chose. ' +
  '`done` is different: post it when you finish work the user stepped away from (above all after a timed_out ' +
  'request), with a message saying what you did and what still needs them. It returns at once and waits for ' +
  'nothing, takes no options, timeout_s or on_timeout, and stays on the badge until the user dismisses it. ' +
  '`options` are up to four quick replies; the user may also type their own. After `timeout_s` (default 300) ' +
  '`on_timeout` decides: `proceed` (default) returns timed_out and you carry on with work that needs no ' +
  'approval only; `wait` keeps waiting up to a day; `stop` ends your turn. A timeout never approves anything.'
