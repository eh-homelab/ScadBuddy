import type { Owner } from '../sessions/protocol.js'
import { OPTIONS_MAX, OPTIONS_MIN, PREVIEW_MAX, QUESTION_TEXT_MAX, QUESTIONS_MAX, utf8Length } from './limits.js'
import type { GateEntry } from './validate.js'

// A parked durable call as the gate keeps it (spec 2026-10-01 §6.6, "Reads"), ported whole
// from agent-durable's gate/entry.py: `buildEntry` is what the session workflow runs when
// the agent's pending calls gain one, giving the workflow state the validator reads and
// what `open_input` writes. An `approval` carries its summary and input hash, never its
// input, and no prompt. An `answer` carries its prompt on purpose, with the turn's secrets
// redacted as the classic QuestionService redacts them, at most PROMPT_MAX bytes: a longer
// one is refused to the model as malformed, without parking.
//
// The workflow holds no secret to redact the model's arguments with (it never holds the
// credential), so an `answer` call's input is checked against the tool's declared shape
// before any of it is kept: every string bounded, `reason` and `on_timeout` from their
// sets. Anything else is refused to the model, never parked, and a refusal names the
// field, never its value. Free of Node imports: the workflow bundle loads it.
//
// Lengths are UTF-16 code units, as the classic zod schemas count them (Python counted
// code points; the two differ only for characters outside the BMP).

export const PROMPT_MAX = 16 * 1024
export const ASK_USER = 'ask_user'
export const WAIT_FOR_USER = 'wait_for_user'
/** harness/attention.ts DEFAULT_REPLIES: an attention request's options when it names none. */
export const DEFAULT_REPLIES: readonly string[] = ["I'm here", 'Carry on without me']
export const REDACTED = '[redacted]'
export const DEFAULT_TIMEOUT_S = 300
/** A tab wait the tab's return ended (#5003; questions/service.ts `reconnected`'s words): its response row's `reason` and `response`. */
export const RECONNECTED_REASON = 'the ScadBuddy tab is connected again'
export const RECONNECTED_RESPONSE = { reconnected: true } as const

const LABEL_MAX = 200
const HEADER_MAX = 200
const MESSAGE_MAX = 2_000
export const TIMEOUT_MIN_S = 10
const TIMEOUT_MAX_S = 86_400
/** An attention request's reasons, each with its card header (attention.ts HEADERS). */
const WAIT_HEADERS: Record<string, string> = { tab_disconnected: 'Tab disconnected', question: 'Question', blocked: 'Blocked' }
/** Only `proceed` until a durable turn's stop is offered (spec §6.6 Timeouts); `BuildEntryArgs.onTimeout` lets in more. */
const ON_TIMEOUT = ['proceed']
/** What an AgentSession turn's attention request may name besides (harness/attention.ts ON_TIMEOUT): its turn ends it (#5003). */
export const TURN_ON_TIMEOUT = ['wait', 'stop']
const WAIT_KEYS = ['reason', 'message', 'options', 'timeout_s', 'on_timeout']

/** The call's input is not the tool's declared shape: refused, never parked. */
export class MalformedAnswerInputError extends Error {
  override name = 'MalformedAnswerInputError'
}

/** The prompt is over PROMPT_MAX bytes: the call is refused, never parked. */
export class PromptTooLargeError extends MalformedAnswerInputError {
  override name = 'PromptTooLargeError'
}

type Card = { question: string; header: string; multiSelect: boolean; options: { label: string; description: string; preview?: string }[] }

export type BuiltEntry = {
  entry: GateEntry
  prompt: string
  /** The card the panel shows (question.asked's `questions`), redacted; null for an approval. */
  questions: Card[] | null
  /** An attention request's {reason, on_timeout}; null otherwise. */
  attention: { reason: string; on_timeout: string } | null
  /** An attention request's own timer, in seconds; null otherwise. */
  timeoutS: number | null
}

export function redact(text: string, secrets: readonly string[]): string {
  let out = text
  for (const secret of secrets) if (secret && secret.length >= 4) out = out.split(secret).join(REDACTED)
  return out
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

function text(value: unknown, field: string, low: number, high: number, trim = false): string {
  if (typeof value !== 'string') throw new MalformedAnswerInputError(`${field} must be a string`)
  const out = trim ? value.trim() : value
  if (out.length < low || out.length > high) throw new MalformedAnswerInputError(`${field} must be ${low} to ${high} characters`)
  return out
}

function checkQuestion(q: unknown, n: number): Card {
  const where = `questions[${n}]`
  if (!isRecord(q)) throw new MalformedAnswerInputError(`${where} must be an object`)
  const question = text(q.question, `${where}.question`, 1, QUESTION_TEXT_MAX)
  const header = text(q.header, `${where}.header`, 0, HEADER_MAX)
  const multi = q.multiSelect
  if (typeof multi !== 'boolean') throw new MalformedAnswerInputError(`${where}.multiSelect must be true or false`)
  const options = q.options
  if (!Array.isArray(options) || options.length < OPTIONS_MIN || options.length > OPTIONS_MAX) {
    throw new MalformedAnswerInputError(`${where}.options must be ${OPTIONS_MIN} to ${OPTIONS_MAX}`)
  }
  const checked = options.map((o: unknown, m) => {
    const at = `${where}.options[${m}]`
    if (!isRecord(o)) throw new MalformedAnswerInputError(`${at} must be an object`)
    const option: Card['options'][number] = {
      label: text(o.label, `${at}.label`, 1, LABEL_MAX),
      description: text(o.description, `${at}.description`, 0, QUESTION_TEXT_MAX),
    }
    if ('preview' in o) option.preview = text(o.preview, `${at}.preview`, 0, PREVIEW_MAX)
    if (multi && option.label.includes(',')) throw new MalformedAnswerInputError(`${at}.label of a multiSelect question has a comma`)
    return option
  })
  if (new Set(checked.map((o) => o.label)).size !== checked.length) {
    throw new MalformedAnswerInputError(`${where}.options need their own labels`)
  }
  return { question, header, multiSelect: multi, options: checked }
}

function checkAsk(input: Record<string, unknown>): Card[] {
  const keys = Object.keys(input)
  if (keys.length !== 1 || keys[0] !== 'questions') throw new MalformedAnswerInputError('ask_user takes only `questions`')
  const questions = input.questions
  if (!Array.isArray(questions) || questions.length < 1 || questions.length > QUESTIONS_MAX) {
    throw new MalformedAnswerInputError(`questions must be 1 to ${QUESTIONS_MAX} questions`)
  }
  const checked = questions.map((q: unknown, n) => checkQuestion(q, n))
  if (new Set(checked.map((q) => q.question)).size !== checked.length) throw new MalformedAnswerInputError('each question must be different')
  return checked
}

type Wait = { reason: string; message: string; options: string[]; timeoutS: number; onTimeout: string }

function checkWait(input: Record<string, unknown>, more?: (onTimeout: string) => boolean): Wait {
  if (!Object.keys(input).every((k) => WAIT_KEYS.includes(k))) {
    throw new MalformedAnswerInputError(`wait_for_user takes only ${[...WAIT_KEYS].sort().join(', ')}`)
  }
  const reason = input.reason
  if (typeof reason !== 'string' || !Object.hasOwn(WAIT_HEADERS, reason)) {
    throw new MalformedAnswerInputError(`reason must be one of ${Object.keys(WAIT_HEADERS).join(', ')}`)
  }
  const message = text(input.message, 'message', 1, MESSAGE_MAX, true)
  let options = [...DEFAULT_REPLIES]
  if (input.options !== undefined && input.options !== null) {
    const raw = input.options
    if (!Array.isArray(raw) || raw.length < 2 || raw.length > 4) throw new MalformedAnswerInputError('options must be 2 to 4 replies')
    options = raw.map((o: unknown, n) => text(o, `options[${n}]`, 1, LABEL_MAX, true))
    if (new Set(options).size !== options.length) throw new MalformedAnswerInputError('each option must be different')
  }
  const timeout = Object.hasOwn(input, 'timeout_s') ? input.timeout_s : DEFAULT_TIMEOUT_S
  if (typeof timeout !== 'number' || !Number.isInteger(timeout) || timeout < TIMEOUT_MIN_S || timeout > TIMEOUT_MAX_S) {
    throw new MalformedAnswerInputError(`timeout_s must be a whole number, ${TIMEOUT_MIN_S} to ${TIMEOUT_MAX_S}`)
  }
  const onTimeout = Object.hasOwn(input, 'on_timeout') ? input.on_timeout : 'proceed'
  if (typeof onTimeout !== 'string' || !(ON_TIMEOUT.includes(onTimeout) || (more !== undefined && TURN_ON_TIMEOUT.includes(onTimeout) && more(onTimeout)))) {
    throw new MalformedAnswerInputError(`on_timeout must be one of ${(more ? [...ON_TIMEOUT, ...TURN_ON_TIMEOUT] : ON_TIMEOUT).join(', ')}`)
  }
  return { reason, message, options, timeoutS: timeout, onTimeout }
}

function cardQuestion(q: Card, secrets: readonly string[]): Card {
  return {
    question: redact(q.question, secrets),
    header: redact(q.header, secrets),
    multiSelect: q.multiSelect,
    options: q.options.map((o) => ({
      label: redact(o.label, secrets),
      description: redact(o.description, secrets),
      ...(o.preview !== undefined ? { preview: redact(o.preview, secrets) } : {}),
    })),
  }
}

export type BuildEntryArgs = {
  requestId: string
  kind: 'approval' | 'answer'
  tool: string
  toolInput: unknown
  inputHash: string | null
  requestedBy: Pick<Owner, 'kind' | 'id'>
  sessionOwner: Pick<Owner, 'kind' | 'id'>
  sessionCreator: Pick<Owner, 'kind' | 'id'>
  secrets?: readonly string[]
  /** A flow run's call (#2482): decided by the person, never an approval grant. */
  inRun?: boolean
  /**
   * Whether a `wait_for_user` call may name `on_timeout` `wait` or `stop` (TURN_ON_TIMEOUT): only
   * where the turn, not the workflow, acts on the timeout (AgentSession, #5003). Asked only for such
   * a call, so a workflow can put its answer behind `patched()` without marking any other history.
   */
  onTimeout?: (onTimeout: string) => boolean
}

export function buildEntry(args: BuildEntryArgs): BuiltEntry {
  const secrets = args.secrets ?? []
  const entryOf = (extra: Pick<GateEntry, 'questions' | 'options'> = {}): GateEntry => ({
    id: args.requestId,
    kind: args.kind,
    state: 'pending',
    inputHash: args.kind === 'approval' ? args.inputHash : null,
    requestedBy: args.requestedBy,
    sessionOwner: args.sessionOwner,
    sessionCreator: args.sessionCreator,
    ...extra,
    inRun: args.inRun ?? false,
  })
  if (args.kind === 'approval') return { entry: entryOf(), prompt: '', questions: null, attention: null, timeoutS: null }
  if (!isRecord(args.toolInput)) throw new MalformedAnswerInputError(`${args.tool} takes an object`)
  let prompt: string
  let card: Card[]
  let entry: GateEntry
  let attention: BuiltEntry['attention'] = null
  let timeoutS: number | null = null
  if (args.tool === ASK_USER) {
    card = checkAsk(args.toolInput).map((q) => cardQuestion(q, secrets))
    prompt = card.map((q) => q.question).join('\n')
    entry = entryOf({ questions: card.map((q) => ({ question: q.question, multiSelect: q.multiSelect })) })
  } else if (args.tool === WAIT_FOR_USER) {
    const wait = checkWait(args.toolInput, args.onTimeout)
    prompt = redact(wait.message, secrets)
    const options = wait.options.map((o) => redact(o, secrets))
    card = [{ question: prompt, header: WAIT_HEADERS[wait.reason]!, multiSelect: false, options: options.map((label) => ({ label, description: '' })) }]
    entry = entryOf({ options })
    attention = { reason: wait.reason, on_timeout: wait.onTimeout }
    timeoutS = wait.timeoutS
  } else {
    throw new Error(`${args.tool} is not an answer tool`)
  }
  if (utf8Length(prompt) > PROMPT_MAX) {
    throw new PromptTooLargeError(`the ${args.tool} prompt is over ${PROMPT_MAX} bytes; ask a shorter question`)
  }
  return { entry, prompt, questions: card, attention, timeoutS }
}
