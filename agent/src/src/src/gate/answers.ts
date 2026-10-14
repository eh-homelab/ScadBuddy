import type { Sql } from 'postgres'
import { answeredText, DEFAULT_TIMEOUT_S, timedOutText } from '../harness/attention.js'
import { answersText } from '../harness/questions.js'
import { ASK_USER } from '../tools/answerTools.js'

// How an `answer` tool's call ends in a durable session (spec 2026-10-01 §6.6, "How
// each kind resolves"): DurableSession's resolve_input records the outcome in
// ai_input_responses, `{"answers": [...]}` for an answer (one per question, in order,
// or the one reply), and then lets the call through. The call's activity on
// `agent-tools` reads that row by its request id and returns it as the tool's result.
// The plugin's gate carries only a yes or no, so this is how an answer becomes the
// result. A timer never answers: `timed_out` is an attention request's timeout, and
// `cancelled`, or a row that is missing, is an error result, never an answer.

export type AnswerResult = { ok: boolean; text: string }

export interface AnswerReader {
  /** The result of the answer tool `tool` called with `input`, from its request's recorded outcome. */
  result(requestId: string, tool: string, input: unknown): Promise<AnswerResult>
}

type Row = { outcome: string; response: { answers?: unknown } | null; reason: string | null }

export function answerResult(row: Row | undefined, tool: string, input: unknown): AnswerResult {
  if (!row) return { ok: false, text: 'The user did not answer: the request is no longer recorded.' }
  const answers = Array.isArray(row.response?.answers) ? row.response.answers.filter((a): a is string => typeof a === 'string') : []
  if (row.outcome === 'answered' && answers.length > 0) {
    if (tool !== ASK_USER) return { ok: true, text: answeredText(answers[0]!) }
    const questions = (input as { questions?: { question?: unknown }[] } | undefined)?.questions ?? []
    const keyed: Record<string, string> = {}
    questions.forEach((q, i) => {
      if (typeof q.question === 'string') keyed[q.question] = answers[i] ?? ''
    })
    return { ok: true, text: answersText(keyed) }
  }
  if (row.outcome === 'timed_out' && tool !== ASK_USER) {
    const seconds = (input as { timeout_s?: unknown } | undefined)?.timeout_s
    return { ok: true, text: timedOutText(typeof seconds === 'number' ? seconds : DEFAULT_TIMEOUT_S) }
  }
  return { ok: false, text: `The user did not answer: ${row.reason ?? 'the request was cancelled'}.` }
}

export class PgAnswers implements AnswerReader {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async result(requestId: string, tool: string, input: unknown): Promise<AnswerResult> {
    const [row] = await this.#sql<Row[]>`
      SELECT outcome, response, reason FROM ai_input_responses WHERE request_id = ${requestId} AND kind = 'answer'`
    return answerResult(row, tool, input)
  }
}
