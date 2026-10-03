import type { PermissionResult } from '@anthropic-ai/claude-agent-sdk'
import { z } from 'zod'

// Structured questions for the user (#940): multiple choice, and a draft to
// approve (an option's `preview`, Markdown). The agent asks them with Claude
// Code's own built-in AskUserQuestion tool rather than a registry tool: the
// SDK hands every call of it to `canUseTool`, and the host answers by allowing
// the call with `answers` added to its input, which Claude Code turns into the
// tool result the model reads ("Your questions have been answered: …").
// Measured on SDK 0.3.283 (test/questions.sdk.test.ts). The tool is not on
// /mcp, where there is no user to ask. Whether a subagent's question reaches
// the gate is not measured yet (#940's remaining work).
//
// A run gets the tool only with a QuestionGate (a session turn: the panel is
// where the user answers, questions/service.ts); a bare run has no one to ask
// and is not offered it. The gate parks the call until the user answers or the
// turn ends; anything but an answer reaches the model as the tool's error,
// so a question nobody answered is never taken for a choice.

export const ASK_USER_QUESTION = 'AskUserQuestion'

/** Claude Code's own limits (sdk-tools.d.ts `AskUserQuestionInput`), and caps on what the panel shows. */
export const QUESTIONS_MAX = 4
export const OPTIONS_MIN = 2
export const OPTIONS_MAX = 4
export const QUESTION_TEXT_MAX = 2_000
export const PREVIEW_MAX = 20_000
/** The longest answer the user may type ("Other", or an edited draft). */
export const ANSWER_MAX = 20_000

const OptionSchema = z.object({
  label: z.string().min(1).max(200),
  description: z.string().max(QUESTION_TEXT_MAX),
  preview: z.string().max(PREVIEW_MAX).optional(),
})

export const QuestionSchema = z
  .object({
    question: z.string().min(1).max(QUESTION_TEXT_MAX),
    header: z.string().max(200),
    multiSelect: z.boolean(),
    options: z
      .array(OptionSchema)
      .min(OPTIONS_MIN)
      .max(OPTIONS_MAX)
      // The panel tells options apart by label, and the answer names one.
      .refine((options) => new Set(options.map((o) => o.label)).size === options.length, 'each option needs its own label'),
  })
  // A multi-select answer is its labels joined by ", " (plus "Other: <the user's words>"),
  // so a label with a comma could not be told apart.
  .refine((q) => !q.multiSelect || q.options.every((o) => !o.label.includes(',')), {
    message: 'an option label of a multiSelect question must not contain a comma',
  })

export type UserQuestion = z.infer<typeof QuestionSchema>

const InputSchema = z.object({
  questions: z
    .array(QuestionSchema)
    .min(1)
    .max(QUESTIONS_MAX)
    .refine((qs) => new Set(qs.map((q) => q.question)).size === qs.length, 'each question must be different'),
})

/** One AskUserQuestion call waiting for the user. */
export type QuestionRequest = {
  questions: UserQuestion[]
  /** The tool_use block's id: the panel's `tool.call` id. */
  toolUseId: string
  /** Aborted when the SDK drops the request (the query stops). */
  signal: AbortSignal
}

export type QuestionVerdict =
  /** Keyed by question text; a multi-select answer is its labels joined by ", ". */
  | { answered: true; answers: Record<string, string> }
  /** Not answered; `message` is what the model reads as the tool's error. */
  | { answered: false; message: string }

/** Parks a question until it is answered or the turn ends; a rejection counts as unanswered. */
export type QuestionGate = (request: QuestionRequest) => Promise<QuestionVerdict>

/** The questions in an AskUserQuestion input, or why they are refused. */
export function parseQuestions(input: unknown): { ok: true; questions: UserQuestion[] } | { ok: false; error: string } {
  const parsed = InputSchema.safeParse(input)
  return parsed.success ? { ok: true, questions: parsed.data.questions } : { ok: false, error: z.prettifyError(parsed.error) }
}

/** canUseTool's answer to one AskUserQuestion call. */
export async function askThroughGate(
  gate: QuestionGate,
  input: Record<string, unknown>,
  toolUseId: string,
  signal: AbortSignal,
): Promise<PermissionResult> {
  const parsed = parseQuestions(input)
  if (!parsed.ok) return { behavior: 'deny', message: `The question was not asked: ${parsed.error}` }
  try {
    const verdict = await gate({ questions: parsed.questions, toolUseId, signal })
    if (!verdict.answered) return { behavior: 'deny', message: verdict.message }
    return { behavior: 'allow', updatedInput: { questions: parsed.questions, answers: verdict.answers } }
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err)
    return { behavior: 'deny', message: `The user did not answer: the question could not complete (${why}).` }
  }
}
