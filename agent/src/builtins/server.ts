import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool } from '@anthropic-ai/claude-agent-sdk'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, type CallToolResult, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import { edit, type BuiltinResult, type FileToolContext, glob, grep, notebookEdit, read, WORKSPACE_ROOT, write } from './files.js'
import {
  enterWorktree,
  exitWorktree,
  reportFindings,
  sendMessage,
  taskCreate,
  taskGet,
  taskList,
  taskUpdate,
} from './session.js'
import SCHEMAS from './schemas.json' with { type: 'json' }
import { type WebContext, webFetch, webSearch } from './web.js'
import { type BackgroundContext, cronCreate, cronDelete, cronList, scheduleWakeup, taskStop } from './background.js'
import { MAX_ACTIVE } from '../background/store.js'

// A durable turn's Claude Code built-ins (#2393): the ones an allowed plugin package
// names, as one MCP server the agent offers as turn tools (turnServers.ts), named
// `builtin__<Name>`. The Temporal plugin runs only Bash and PowerShell of Claude Code's
// own tools as activities, so a call of any other could not wait for a human; each of
// these is outward, as every package built-in is in a classic turn, and parks first.
//
// Each tool takes Claude Code's own input (schemas.json, the bundled Claude Code's,
// checked by test/builtinSchemas.sdk.test.ts) and answers in its words.

export const BUILTIN_SERVER = 'builtin'

export type BuiltinContext = {
  files: FileToolContext
  web: WebContext
  /** The session's background tasks (CronCreate and the rest); absent where there are none (a flow). */
  background?: BackgroundContext | undefined
}

/** The background built-ins: kept by the session past its turn (background.ts). */
export const BACKGROUND_BUILTINS: readonly string[] = ['CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'TaskStop']

const noTasks = (): BuiltinResult => ({ text: 'This session cannot keep background tasks.', isError: true })
const withTasks =
  <I>(run: (ctx: BackgroundContext, input: I) => Promise<BuiltinResult>) =>
  (ctx: BuiltinContext, input: I) =>
    ctx.background ? run(ctx.background, input) : noTasks()

type Builtin = { description: string; input: z.ZodType; run: (ctx: BuiltinContext, input: never) => Promise<BuiltinResult> | BuiltinResult }

const path = `an absolute path under ${WORKSPACE_ROOT}, the session's workspace`
const status = z.enum(['pending', 'in_progress', 'completed'])

/** The built-ins a durable turn provides, by Claude Code's name. */
export const BUILTINS: Record<string, Builtin> = {
  Read: {
    description: `Claude Code's Read: reads a file of the session workspace (${path}) as numbered lines, or an image.`,
    input: z.object({ file_path: z.string(), offset: z.number().int().optional(), limit: z.number().int().optional() }).loose(),
    run: (ctx, input: Parameters<typeof read>[1]) => read(ctx.files, input),
  },
  Write: {
    description: `Claude Code's Write: writes a file of the session workspace (${path}), replacing it if it exists.`,
    input: z.object({ file_path: z.string(), content: z.string() }),
    run: (ctx, input: Parameters<typeof write>[1]) => write(ctx.files, input),
  },
  Edit: {
    description: `Claude Code's Edit: replaces exact text in a file of the session workspace (${path}).`,
    input: z.object({ file_path: z.string(), old_string: z.string(), new_string: z.string(), replace_all: z.boolean().optional() }),
    run: (ctx, input: Parameters<typeof edit>[1]) => edit(ctx.files, input),
  },
  Glob: {
    description: `Claude Code's Glob: lists the session workspace's files (under ${WORKSPACE_ROOT}) matching a glob pattern.`,
    input: z.object({ pattern: z.string(), path: z.string().optional() }),
    run: (ctx, input: Parameters<typeof glob>[1]) => glob(ctx.files, input),
  },
  Grep: {
    description: `Claude Code's Grep: searches the session workspace's files (under ${WORKSPACE_ROOT}) with a regular expression.`,
    input: z
      .object({
        pattern: z.string(),
        path: z.string().optional(),
        glob: z.string().optional(),
        output_mode: z.enum(['content', 'files_with_matches', 'count']).optional(),
        '-B': z.number().int().optional(),
        '-A': z.number().int().optional(),
        '-C': z.number().int().optional(),
        context: z.number().int().optional(),
        '-n': z.boolean().optional(),
        '-i': z.boolean().optional(),
        '-o': z.boolean().optional(),
        type: z.string().optional(),
        head_limit: z.number().int().optional(),
        offset: z.number().int().optional(),
        multiline: z.boolean().optional(),
      })
      .loose(),
    run: (ctx, input: Parameters<typeof grep>[1]) => grep(ctx.files, input),
  },
  NotebookEdit: {
    description: `Claude Code's NotebookEdit: replaces, inserts or deletes a cell of a Jupyter notebook in the session workspace (${path}).`,
    input: z.object({
      notebook_path: z.string(),
      cell_id: z.string().optional(),
      new_source: z.string(),
      cell_type: z.enum(['code', 'markdown']).optional(),
      edit_mode: z.enum(['replace', 'insert', 'delete']).optional(),
    }),
    run: (ctx, input: Parameters<typeof notebookEdit>[1]) => notebookEdit(ctx.files, input),
  },
  WebFetch: {
    description:
      "Claude Code's WebFetch: fetches a URL, converts the page to markdown, and answers `prompt` against it using a small fast model. " +
      'A cross-host redirect is returned to you; `offset` reads on through a long page. Fails on localhost and hostnames without a dot.',
    input: z.object({ url: z.string(), prompt: z.string(), offset: z.number().int().min(0).optional() }),
    run: (ctx, input: Parameters<typeof webFetch>[1]) => webFetch(ctx.web, input),
  },
  WebSearch: {
    description:
      "Claude Code's WebSearch: searches the web and returns result blocks with titles and URLs. After answering from results, end " +
      'with a "Sources:" list of the URLs you used as markdown links.',
    input: z.object({ query: z.string().min(2), allowed_domains: z.array(z.string()).optional(), blocked_domains: z.array(z.string()).optional() }),
    run: (ctx, input: Parameters<typeof webSearch>[1]) => webSearch(ctx.web, input),
  },
  TaskCreate: {
    description: "Claude Code's TaskCreate: adds a task to the session's task list.",
    input: z.object({
      subject: z.string(),
      description: z.string(),
      activeForm: z.string().optional(),
      metadata: z.record(z.string(), z.unknown()).optional(),
    }),
    run: (ctx, input: Parameters<typeof taskCreate>[1]) => taskCreate(ctx.files, input),
  },
  TaskGet: {
    description: "Claude Code's TaskGet: one task of the session's task list.",
    input: z.object({ taskId: z.string() }),
    run: (ctx, input: Parameters<typeof taskGet>[1]) => taskGet(ctx.files, input),
  },
  TaskList: {
    description: "Claude Code's TaskList: the session's task list.",
    input: z.object({}).loose(),
    run: (ctx) => taskList(ctx.files),
  },
  TaskUpdate: {
    description: "Claude Code's TaskUpdate: changes a task of the session's task list (status `deleted` removes it).",
    input: z.object({
      taskId: z.string(),
      subject: z.string().optional(),
      description: z.string().optional(),
      activeForm: z.string().optional(),
      status: z.union([status, z.literal('deleted')]).optional(),
      addBlocks: z.array(z.string()).optional(),
      addBlockedBy: z.array(z.string()).optional(),
      owner: z.string().optional(),
      metadata: z.record(z.string(), z.unknown()).optional(),
    }),
    run: (ctx, input: Parameters<typeof taskUpdate>[1]) => taskUpdate(ctx.files, input),
  },
  ReportFindings: {
    description: "Claude Code's ReportFindings: reports a review's findings.",
    input: z.object({ findings: z.array(z.unknown()) }).loose(),
    run: (_ctx, input: Parameters<typeof reportFindings>[0]) => reportFindings(input),
  },
  EnterWorktree: {
    description: "Claude Code's EnterWorktree: the session workspace is not a git repository, so there is no worktree to enter.",
    input: z.object({}).loose(),
    run: () => enterWorktree(),
  },
  ExitWorktree: {
    description: "Claude Code's ExitWorktree.",
    input: z.object({}).loose(),
    run: () => exitWorktree(),
  },
  SendMessage: {
    description: "Claude Code's SendMessage: a session runs no other agents to message.",
    input: z.object({ to: z.string().optional() }).loose(),
    run: (_ctx, input: Parameters<typeof sendMessage>[0]) => sendMessage(input),
  },
  CronCreate: {
    description:
      "Claude Code's CronCreate: schedules a prompt to be sent to this session at future times, by a standard 5-field cron " +
      'expression (M H DoM Mon DoW) in the timezone the answer names; `recurring: false` fires once. The session keeps the job ' +
      `past this turn, fires it when idle, and lists it in the panel; recurring jobs expire after 7 days; at most ${MAX_ACTIVE} ` +
      'background tasks at once. Pick a minute other than :00 and :30 unless the user names one. Returns a job ID for CronDelete.',
    input: z.object({ cron: z.string(), prompt: z.string(), recurring: z.boolean().optional(), durable: z.boolean().optional() }),
    run: withTasks(cronCreate),
  },
  CronDelete: {
    description: "Claude Code's CronDelete: cancels a job CronCreate scheduled.",
    input: z.object({ id: z.string() }),
    run: withTasks(cronDelete),
  },
  CronList: {
    description: "Claude Code's CronList: the jobs CronCreate scheduled in this session.",
    input: z.object({}).loose(),
    run: withTasks((ctx) => cronList(ctx)),
  },
  ScheduleWakeup: {
    description:
      "Claude Code's ScheduleWakeup: sends `prompt` to this session again after `delaySeconds` (clamped to [60, 3600]), " +
      'to pace a loop; `stop: true` cancels the pending wakeup. One wakeup at a time: a new one replaces it. `reason` is shown to the user.',
    input: z.object({
      delaySeconds: z.number().optional(),
      reason: z.string().optional(),
      prompt: z.string().optional(),
      stop: z.boolean().optional(),
      noop: z.boolean().optional(),
    }),
    run: withTasks(scheduleWakeup),
  },
  TaskStop: {
    description: "Claude Code's TaskStop: stops one of this session's background tasks (a cron job or wakeup) by its ID.",
    input: z.object({ task_id: z.string().optional(), shell_id: z.string().optional() }),
    run: withTasks(taskStop),
  },
}

/** The built-ins a durable turn offers this way: the rest of what a package may name. */
export const PROVIDED = new Set(Object.keys(BUILTINS))

function toResult(result: BuiltinResult): CallToolResult {
  return {
    content: [
      { type: 'text', text: result.text },
      ...(result.image ? [{ type: 'image' as const, data: result.image.data, mimeType: result.image.mimeType }] : []),
    ],
    ...(result.isError ? { isError: true } : {}),
  }
}

/** An MCP server offering `names` (those of BUILTINS), each run with `context()`. */
export function builtinServer(names: readonly string[], context: () => BuiltinContext): Server {
  const offered = names.filter((n) => PROVIDED.has(n))
  const server = new Server({ name: BUILTIN_SERVER, version: '1' }, { capabilities: { tools: {} } })
  const schemas = SCHEMAS as Record<string, Record<string, unknown>>
  server.setRequestHandler(ListToolsRequestSchema, () => ({
    tools: offered.map((name) => ({ name, description: BUILTINS[name]!.description, inputSchema: schemas[name] as { type: 'object' } })),
  }))
  server.setRequestHandler(CallToolRequestSchema, async (request) => {
    const name = request.params.name
    const builtin = offered.includes(name) ? BUILTINS[name] : undefined
    if (!builtin) return toResult({ text: `No such tool: ${name}`, isError: true })
    const parsed = builtin.input.safeParse(request.params.arguments ?? {})
    if (!parsed.success) {
      return toResult({ text: `<tool_use_error>InputValidationError: ${z.prettifyError(parsed.error)}</tool_use_error>`, isError: true })
    }
    return toResult(await builtin.run(context(), parsed.data as never))
  })
  return server
}

/**
 * A classic turn's background built-ins (CronCreate and the rest) as an in-process
 * server, in place of Claude Code's own: those live in its process, which the turn ends
 * ("Session-only (not written to disk, dies when Claude exits)"), while these are kept by
 * the session (src/background/). Each call is outward, as every package built-in is.
 */
export function backgroundSdkServer(names: readonly string[], context: BackgroundContext): McpSdkServerConfigWithInstance {
  return createSdkMcpServer({
    name: BUILTIN_SERVER,
    tools: names
      .filter((n) => BACKGROUND_BUILTINS.includes(n))
      .map((name) => {
        const builtin = BUILTINS[name]!
        return tool(name, builtin.description, (builtin.input as z.ZodObject).shape, async (args) =>
          toResult(await builtin.run({ background: context } as BuiltinContext, args as never)),
        )
      }),
  })
}

/** What a classic turn's model is told about the background built-ins it gets that way. */
export function backgroundNote(names: readonly string[]): string {
  const tools = names.map((n) => `${n} (mcp__${BUILTIN_SERVER}__${n})`).join(', ')
  return (
    `Claude Code's built-in tools ${tools} are provided in this session as tools of the "${BUILTIN_SERVER}" server, because a ` +
    'plugin package you have names them. Use them where its skills or agents say to use the built-in of that name: they take ' +
    'the same input. Every call of one asks the user first. The session keeps the jobs and wakeups they schedule past this ' +
    'turn: each sends its prompt to the session as a new message when it is due and the session is idle.'
  )
}
