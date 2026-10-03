import { createSdkMcpServer, type McpSdkServerConfigWithInstance, type PermissionResult, tool } from '@anthropic-ai/claude-agent-sdk'
import { z } from 'zod'

// Structured questions for the user (#940): multiple choice, and a draft to
// approve (an option's `preview`, Markdown). The agent asks them with Claude
// Code's own built-in AskUserQuestion tool rather than a registry tool: the
// SDK hands every call of it to `canUseTool`, and the host answers by allowing
// the call with `answers` added to its input, which Claude Code turns into the
// tool result the model reads ("Your questions have been answered: …").
// Measured on SDK 0.3.283 (test/questions.sdk.test.ts). The tool is not on
// /mcp, where there is no user to ask.
//
// A SUBAGENT cannot use AskUserQuestion: Claude Code refuses the call itself
// ("AskUserQuestion is not available inside subagents ... return findings to
// the orchestrator") and never hands it to canUseTool, and an agent whose
// `tools` lists it gets "not available to subagents" (measured on Claude Code
// 2.1.283, test/harnessWiring.test.ts). So a run with a gate also gets
// ASK_USER_TOOL, an in-process MCP tool with the same input that parks on the
// same gate: a subagent's MCP calls reach the host like the parent's. Our
// subagents list its server in their `tools`.
//
// A run gets the tool only with a QuestionGate (a session turn: the panel is
// where the user answers, questions/service.ts); a bare run has no one to ask
// and is not offered it. The gate parks the call until the user answers or the
// turn ends; anything but an answer reaches the model as the tool's error,
// so a question nobody answered is never taken for a choice.

export const ASK_USER_QUESTION = 'AskUserQuestion'

/** The in-process server and tool a subagent asks with (see above). */
export const QUESTION_SERVER = 'scadbuddy_questions'
export const ASK_USER_TOOL = `mcp__${QUESTION_SERVER}__ask_user`

/** Either way of asking the user: both only ask, so both are tiered `read`. */
export function isQuestionTool(name: string): boolean {
  return name === ASK_USER_QUESTION || name === ASK_USER_TOOL
}

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

const QuestionsSchema = z
    .array(QuestionSchema)
    .min(1)
    .max(QUESTIONS_MAX)
    .refine((qs) => new Set(qs.map((q) => q.question)).size === qs.length, 'each question must be different')

const InputSchema = z.object({ questions: QuestionsSchema })

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

/** Asks through the gate; anything but an answer is the message the model reads as the tool's error. */
async function ask(
  gate: QuestionGate,
  input: unknown,
  toolUseId: string,
  signal: AbortSignal,
): Promise<{ answered: true; questions: UserQuestion[]; answers: Record<string, string> } | { answered: false; message: string }> {
  const parsed = parseQuestions(input)
  if (!parsed.ok) return { answered: false, message: `The question was not asked: ${parsed.error}` }
  try {
    const verdict = await gate({ questions: parsed.questions, toolUseId, signal })
    return verdict.answered ? { ...verdict, questions: parsed.questions } : verdict
  } catch (err) {
    const why = err instanceof Error ? err.message : String(err)
    return { answered: false, message: `The user did not answer: the question could not complete (${why}).` }
  }
}

/** canUseTool's answer to one AskUserQuestion call. */
export async function askThroughGate(
  gate: QuestionGate,
  input: Record<string, unknown>,
  toolUseId: string,
  signal: AbortSignal,
): Promise<PermissionResult> {
  const result = await ask(gate, input, toolUseId, signal)
  if (!result.answered) return { behavior: 'deny', message: result.message }
  return { behavior: 'allow', updatedInput: { questions: result.questions, answers: result.answers } }
}

/**
 * The tool result Claude Code gives AskUserQuestion's answers (measured on
 * 2.1.283), so the model reads ask_user's the same way.
 */
export function answersText(answers: Record<string, string>): string {
  const pairs = Object.entries(answers).map(([q, a]) => `"${q}"="${a}"`)
  return `User has answered your questions: ${pairs.join(', ')}. You can now continue with the user's answers in mind.`
}

/** What Claude Code puts in an MCP call's `_meta` (measured on 2.1.283). */
const TOOL_USE_ID_META = 'claudecode/toolUseId'

/** The parts of the MCP SDK's RequestHandlerExtra the handler reads. */
const ExtraSchema = z.object({
  signal: z.instanceof(AbortSignal),
  _meta: z.object({ [TOOL_USE_ID_META]: z.string().min(1) }).optional(),
})

/** ASK_USER_TOOL, on the run's question gate. */
export function questionServer(gate: QuestionGate): McpSdkServerConfigWithInstance {
  const askUser = tool(
    'ask_user',
    'Ask the user in the ScadBuddy panel one to four multiple-choice questions, and wait for the answers. ' +
      'Use it from a subagent, where AskUserQuestion is not available; it takes the same input. The user may ' +
      'always type an answer of their own. For a draft to approve, give it as an option\'s Markdown `preview`.',
    { questions: QuestionsSchema },
    async (args, extra) => {
      const parsed = ExtraSchema.safeParse(extra)
      const toolUseId = parsed.success ? parsed.data._meta?.[TOOL_USE_ID_META] : undefined
      if (!parsed.success || toolUseId === undefined) {
        return { content: [{ type: 'text' as const, text: 'The question was not asked: the call has no tool_use id.' }], isError: true }
      }
      const result = await ask(gate, args, toolUseId, parsed.data.signal)
      return result.answered
        ? { content: [{ type: 'text' as const, text: answersText(result.answers) }] }
        : { content: [{ type: 'text' as const, text: result.message }], isError: true }
    },
  )
  return createSdkMcpServer({ name: QUESTION_SERVER, tools: [askUser] })
}
