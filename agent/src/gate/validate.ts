import { ANSWER_MAX, QUESTION_TEXT_MAX, QUESTIONS_MAX } from '../harness/questions.js'
import { type Owner, sameOwner } from '../sessions/protocol.js'

// The tool-call gate's validator (durable-agents spec §6.6, "Responses"): what a
// response to a parked call must be, checked against what the gate knows of the
// entry and nothing else, so it is deterministic. The classic respond route runs it
// before it writes (routes/pendingInput.ts); agent-durable's gate/validate.py is the
// same function, which DurableSession's `respond` Update runs as its validator. The
// shared vectors (test/fixtures/pending-input-vectors.json) run against both, so the
// two cannot drift. Who the caller is (its `role`) is the route's to decide
// (role.ts); this checks only whether the kind accepts that role.
//
// Checks, in this order (the first that fails is the refusal):
//   stale      no such entry is parked
//   resolved   it has ended (decided, answered, timed out, cancelled)
//   resolving  a response or its timer is being recorded (durable only)
//   too_large  the response is over RESPONSE_MAX bytes of JSON
//   forbidden  the kind does not accept the role: `approval` takes browser and grant,
//              `answer` the browser user only
//   self       a grant holder deciding a call of its own: one it asked for, or in a
//              session it owns or started (approval only)
//   malformed  the response is not the entry's shape
//   input_mismatch  an approval's `input_hash` was sent and is not the entry's

/**
 * A response's cap, derived from the largest answer the panel can send rather than
 * §6.6's flat 16 KiB, which is smaller than one ANSWER_MAX answer (plan 5b ruling 1).
 * The largest valid body is a question's: QUESTIONS_MAX answers of ANSWER_MAX, each
 * keyed by a question text of QUESTION_TEXT_MAX. Those bounds count UTF-16 code
 * units, and a code unit is at most 6 bytes of JSON (a `\uXXXX` escape); 1 KiB covers
 * the rest of the body.
 */
export const RESPONSE_MAX = QUESTIONS_MAX * (QUESTION_TEXT_MAX + ANSWER_MAX) * 6 + 1024

export type Role = 'browser' | 'grant' | 'owner'

/** What the gate knows of a parked call. */
export type GateEntry = {
  id: string
  kind: 'approval' | 'answer'
  state: 'pending' | 'resolving' | 'resolved'
  /** An approval's input hash. */
  inputHash: string | null
  /** Who the call was made for. */
  requestedBy: Pick<Owner, 'kind' | 'id'> | null
  /** The session's owner and starter when the call parked. */
  sessionOwner: Pick<Owner, 'kind' | 'id'> | null
  sessionCreator: Pick<Owner, 'kind' | 'id'> | null
  /** A question's questions, in order. */
  questions?: readonly { question: string; multiSelect: boolean }[]
  /** An attention request's options. */
  options?: readonly string[]
}

export type RespondRequest = { requestId: string; response: unknown; responder: Pick<Owner, 'kind' | 'id'>; role: Role }

export const REFUSALS = ['stale', 'resolved', 'resolving', 'forbidden', 'self', 'malformed', 'too_large', 'input_mismatch'] as const
export type RefusalCode = (typeof REFUSALS)[number]

export const REFUSAL_STATUS: Record<RefusalCode, 400 | 403 | 404 | 409 | 413> = {
  stale: 404,
  resolved: 409,
  resolving: 409,
  forbidden: 403,
  self: 403,
  malformed: 400,
  too_large: 413,
  input_mismatch: 409,
}

export class RespondRefusal extends Error {
  override name = 'RespondRefusal'
  readonly code: RefusalCode
  constructor(code: RefusalCode, message: string) {
    super(message)
    this.code = code
  }
}

/** What a valid response says: an approval's decision, or an answer's answers (one per question, or the one reply). */
export type ValidResponse = { decision: 'approve' | 'deny' } | { answers: string[] }

const ALLOWED: Record<GateEntry['kind'], readonly Role[]> = { approval: ['browser', 'grant'], answer: ['browser'] }

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const isAnswer = (v: unknown): v is string => typeof v === 'string' && v.length >= 1 && v.length <= ANSWER_MAX

/** The UTF-8 length of the response's JSON. */
export function responseBytes(response: unknown): number {
  return Buffer.byteLength(JSON.stringify(response) ?? 'null', 'utf8')
}

function malformed(message: string): RespondRefusal {
  return new RespondRefusal('malformed', message)
}

function onlyKeys(body: Record<string, unknown>, allowed: readonly string[]): boolean {
  return Object.keys(body).every((k) => allowed.includes(k))
}

export function validateRespond(entry: GateEntry | undefined, request: RespondRequest): ValidResponse {
  const id = request.requestId
  if (!entry || entry.id !== id) throw new RespondRefusal('stale', `no pending input ${id}: it is stale or was never asked`)
  if (entry.state === 'resolved') throw new RespondRefusal('resolved', `${id} is no longer waiting for a response`)
  if (entry.state === 'resolving') throw new RespondRefusal('resolving', `${id} is being resolved; it can no longer be answered`)
  if (responseBytes(request.response) > RESPONSE_MAX) {
    throw new RespondRefusal('too_large', `a response may be at most ${RESPONSE_MAX} bytes`)
  }
  if (!ALLOWED[entry.kind].includes(request.role)) {
    throw new RespondRefusal(
      'forbidden',
      entry.kind === 'answer'
        ? "only the user in the ScadBuddy panel answers the agent's questions"
        : 'outward actions need a human approval in the ScadBuddy UI, or another agent with a per-token approval grant',
    )
  }
  if (entry.kind === 'approval' && request.role !== 'browser') {
    const own = [entry.requestedBy, entry.sessionOwner, entry.sessionCreator].some((o) => o !== null && sameOwner(o, request.responder))
    if (own) {
      throw new RespondRefusal(
        'self',
        "an approval grant is for approving another agent's outward actions, never your own; ask the user in the ScadBuddy UI",
      )
    }
  }
  const body = request.response
  if (!isRecord(body) || (body.kind !== 'approval' && body.kind !== 'answer')) {
    throw malformed('a response is {"kind": "approval", …} or {"kind": "answer", …}')
  }
  if (entry.kind === 'approval') {
    if (body.kind !== 'approval') throw malformed(`${id} is an approval: respond with {"kind": "approval", "decision": …}`)
    if (!onlyKeys(body, ['kind', 'decision', 'input_hash'])) throw malformed('an approval takes only "decision" and "input_hash"')
    if (body.decision !== 'approve' && body.decision !== 'deny') throw malformed('"decision" must be "approve" or "deny"')
    const hash = body.input_hash
    if (hash !== undefined && (typeof hash !== 'string' || !/^[0-9a-f]{64}$/.test(hash))) {
      throw malformed('"input_hash" must be 64 lowercase hex characters')
    }
    if (hash !== undefined && hash !== entry.inputHash) {
      throw new RespondRefusal(
        'input_mismatch',
        `${id} is for a different input than the one you were shown; the call needs a new approval`,
      )
    }
    return { decision: body.decision }
  }
  if (body.kind !== 'answer') throw malformed(`${id} asks for an answer: respond with {"kind": "answer", …}`)
  if (!onlyKeys(body, ['kind', 'answers', 'choice', 'text'])) throw malformed('an answer takes only "answers", "choice" or "text"')
  if (entry.options !== undefined) {
    const { choice, text } = body
    if (body.answers !== undefined || (choice === undefined) === (text === undefined)) {
      throw malformed(`${id} is an attention request: respond with exactly one of "choice" or "text"`)
    }
    if (choice !== undefined) {
      if (!isAnswer(choice) || !entry.options.includes(choice)) {
        throw malformed(`"choice" must be one of ${JSON.stringify(entry.options)}; use "text" for your own words`)
      }
      return { answers: [choice] }
    }
    if (!isAnswer(text) || !text.trim()) throw malformed(`"text" must be 1 to ${ANSWER_MAX} characters, not only spaces`)
    return { answers: [text] }
  }
  const asked = entry.questions ?? []
  const given = body.answers
  if (!isRecord(given) || body.choice !== undefined || body.text !== undefined) {
    throw malformed(`${id} is a question: respond with "answers", one per question, keyed by its text`)
  }
  const keys = Object.keys(given)
  if (keys.length !== asked.length || !asked.every((q) => Object.hasOwn(given, q.question))) {
    throw malformed(`"answers" must answer exactly these questions: ${JSON.stringify(asked.map((q) => q.question))}`)
  }
  // A multi-select's picks join as the panel's card joins them, so only a multi-select
  // takes a list, and no pick may contain the separator (#1357).
  const answers = asked.map((q) => {
    const a = given[q.question]
    if (typeof a === 'string') {
      if (!isAnswer(a)) throw malformed(`each answer must be 1 to ${ANSWER_MAX} characters`)
      return a
    }
    if (!Array.isArray(a) || a.length === 0 || !a.every(isAnswer)) {
      throw malformed(`${JSON.stringify(q.question)} takes an answer: a string, or for a multi-select a list of picks`)
    }
    if (!q.multiSelect) throw malformed(`${JSON.stringify(q.question)} takes one answer: send a string, not a list`)
    if (a.some((pick) => pick.includes(', '))) {
      throw malformed(`a pick must not contain ", ", which joins a multi-select's picks; send the answer as one string`)
    }
    return a.join(', ')
  })
  if (answers.some((a) => a.length > ANSWER_MAX)) {
    throw malformed(`each answer must be at most ${ANSWER_MAX} characters, a multi-select's picks joined with ", "`)
  }
  if (answers.some((a) => !a.trim())) throw malformed('an answer must not be only spaces')
  return { answers }
}
