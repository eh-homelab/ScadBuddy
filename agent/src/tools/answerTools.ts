import { z } from 'zod'
import { DEFAULT_TIMEOUT_S, MESSAGE_MAX, TIMEOUT_MAX_S, TIMEOUT_MIN_S } from '../harness/attention.js'
import { QUESTIONS_MAX, QuestionSchema } from '../harness/questions.js'
import { defineTool, type Tool, ToolError } from './registry.js'

// The `answer` tools of a durable session (spec 2026-10-01 §6.6, plan 5b ruling 4):
// `ask_user` (a classic session's built-in AskUserQuestion, with the same input and
// answer) and `wait_for_user` (#815's attention request). Each exists to ask; running
// it has no effect of its own. DurableSession parks the call at its gate
// (`needs_approval`, hitl `answer`), the browser user answers through `respond`, and
// the call's activity on `agent-tools` returns the recorded answer as its result
// (gate/answers.ts), never this handler's.
//
// Durable-only: they are in the tool manifest and served as activities, never listed
// over /mcp (only the browser user answers, and an MCP client must not ask for an
// answer it cannot be given) nor given to a classic turn, which asks through its own
// question server (harness/questions.ts).

export const ASK_USER = 'ask_user'
export const WAIT_FOR_USER = 'wait_for_user'

/** wait_for_user's reasons: a `done` summary is posted, never waited on, and is not a durable call (ruling 12). */
const WAIT_REASONS = ['tab_disconnected', 'question', 'blocked'] as const

const notInWorkflow = (name: string) =>
  new ToolError(`${name} asks the user from a durable session only; its answer comes from the session's workflow`)

export const DURABLE_ONLY_TOOLS: Tool[] = [
  defineTool({
    name: ASK_USER,
    description:
      'Ask the user one to four multiple-choice questions and wait for their answers. The user may also type ' +
      'their own answer. Use it when you need a decision only the user can make; the answers come back keyed by question.',
    input: z.strictObject({ questions: z.array(QuestionSchema).min(1).max(QUESTIONS_MAX) }),
    risk: 'read',
    routes: [],
    handler: async () => {
      throw notInWorkflow(ASK_USER)
    },
  }),
  defineTool({
    name: WAIT_FOR_USER,
    description:
      "Get the user's attention and wait for their reply, shown in the ScadBuddy panel and counted on the " +
      'Assistant badge. Use it when you cannot go on without them (`blocked`, a `question` with no fixed choices, ' +
      'or `tab_disconnected` after a browser_* call found no tab). `options` are up to four quick replies; the user ' +
      'may also type their own. After `timeout_s` (default 300) the call returns timed_out and you carry on with ' +
      'work that needs no approval only. A timeout never approves anything.',
    input: z.strictObject({
      reason: z.enum(WAIT_REASONS),
      message: z.string().trim().min(1).max(MESSAGE_MAX),
      options: z
        .array(z.string().trim().min(1).max(200))
        .min(2)
        .max(4)
        .refine((o) => new Set(o).size === o.length, 'each option must be different')
        .optional(),
      timeout_s: z.number().int().min(TIMEOUT_MIN_S).max(TIMEOUT_MAX_S).default(DEFAULT_TIMEOUT_S),
      // Only `proceed` until cancelling a durable turn is verified (spec §6.6 Timeouts):
      // `stop` and `wait` fail closed by not being offered.
      on_timeout: z.enum(['proceed']).default('proceed'),
    }),
    risk: 'read',
    routes: [],
    handler: async () => {
      throw notInWorkflow(WAIT_FOR_USER)
    },
  }),
]

export const DURABLE_ONLY_NAMES: ReadonlySet<string> = new Set(DURABLE_ONLY_TOOLS.map((t) => t.name))
