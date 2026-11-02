import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool } from '@anthropic-ai/claude-agent-sdk'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { CallToolRequestSchema, type CallToolResult, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { BuiltinResult } from './result.js'
import { type WebContext, webFetch, webSearch } from './web.js'
import { type BackgroundContext, cronCreate, cronDelete, cronList, monitor, scheduleWakeup, taskStop, workflow } from './background.js'
import { MAX_ACTIVE } from '../background/store.js'
import { CALL_ID_META } from './callId.js'

// The built-ins the session provides in place of Claude Code's (#2393): WebFetch,
// WebSearch and the background ones, as one MCP server named `builtin`. Each takes
// Claude Code's own input and answers in its words. Since Step 9.6 Claude Code's file
// tools and task list run in its own process (access/nativeBuiltins.ts), so a package
// that named `builtin__Read` and the like gets the native tool instead.

export const BUILTIN_SERVER = 'builtin'

export type BuiltinContext = {
  web: WebContext
  /** The session's background tasks (CronCreate and the rest); absent where there are none (a flow). */
  background?: BackgroundContext | undefined
  /** The call's tool_use id, when its turn tool sends one (CALL_ID_META): a retried call is the same call. */
  callId?: string | undefined
}

/** The background built-ins: kept by the session past its turn (background.ts). */
export const BACKGROUND_BUILTINS: readonly string[] = ['CronCreate', 'CronDelete', 'CronList', 'ScheduleWakeup', 'Monitor', 'Workflow', 'TaskStop']

const noTasks = (): BuiltinResult => ({ text: 'This session cannot keep background tasks.', isError: true })
const withTasks =
  <I>(run: (ctx: BackgroundContext, input: I) => Promise<BuiltinResult>) =>
  (ctx: BuiltinContext, input: I) =>
    ctx.background ? run(ctx.background, input) : noTasks()

type Builtin = { description: string; input: z.ZodType; run: (ctx: BuiltinContext, input: never) => Promise<BuiltinResult> | BuiltinResult }

/**
 * The built-ins the session provides in place of Claude Code's, by its name: WebFetch and
 * WebSearch (egress-checked, decision Q2) and the background ones. Claude Code's own file
 * tools and task list run in its process since Step 9.6 (access/nativeBuiltins.ts).
 */
export const BUILTINS: Record<string, Builtin> = {
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
  Monitor: {
    description:
      "Claude Code's Monitor: starts a background watch that streams events. With `command`, a shell script (bash, in the same " +
      'directory as this session\'s Bash) whose stdout lines are the events, batched within 200 ms, and whose exit ends the watch; ' +
      'filter it to the lines you would act on (grep --line-buffered), covering failures as well as success. With `ws`, a WebSocket ' +
      'whose text frames are the events. It expires after `timeout_ms` (default 5 minutes, at most 30); a monitor making more than ' +
      '200 events is stopped. Events reach you as a new message once the session is idle. Use TaskStop to cancel it early. For one ' +
      'notification ("tell me when X is ready"), prefer a command that exits when the condition holds.',
    input: z.object({
      description: z.string(),
      timeout_ms: z.number().optional(),
      command: z.string().optional(),
      ws: z.object({ url: z.string(), protocols: z.array(z.string()).optional() }).optional(),
    }),
    run: withTasks(monitor),
  },
  Workflow: {
    description:
      "Claude Code's Workflow: runs a workflow script that orchestrates subagents in the background and returns at once with a " +
      'task ID; its result arrives as a new message once the session is idle. The script is plain JavaScript beginning with ' +
      '`export const meta = { name, description, phases? }` (a pure literal), then a body using agent(prompt, opts?), ' +
      'parallel(thunks), pipeline(items, ...stages), phase(title), log(message), args and workflow({scriptPath}). Each agent() ' +
      'runs as a chat of its own under this session (at most 4 at once, 50 in all), spending from its budget, with ' +
      "opts.schema returning a checked object. Date.now(), new Date() and Math.random() throw. Run it only when the user asked " +
      'for a workflow or multi-agent orchestration in their own words.',
    input: z.object({
      script: z.string().max(524_288).optional(),
      scriptPath: z.string().optional(),
      name: z.string().optional(),
      args: z.unknown().optional(),
      resumeFromRunId: z.string().optional(),
      description: z.string().optional(),
      title: z.string().optional(),
    }),
    run: withTasks(workflow),
  },
  TaskStop: {
    description: "Claude Code's TaskStop: stops one of this session's background tasks (a cron job, wakeup, monitor or workflow) by its ID.",
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

/** `context` whose web work also stops when the call's caller gives up on it (its request timed out or was cancelled, #3199). */
function stoppable(context: BuiltinContext, signal: AbortSignal): BuiltinContext {
  // A context with no web work (a test's of only the background built-ins) has nothing to stop.
  if (!(context.web as WebContext | undefined)) return context
  return { ...context, web: { ...context.web, signal: AbortSignal.any([context.web.signal, signal]) } }
}

/** An MCP server offering `names` (those of BUILTINS), each run with `context()`. */
export function builtinServer(names: readonly string[], context: () => BuiltinContext): Server {
  const offered = names.filter((n) => PROVIDED.has(n))
  const server = new Server({ name: BUILTIN_SERVER, version: '1' }, { capabilities: { tools: {} } })
  server.setRequestHandler(ListToolsRequestSchema, () => ({
    tools: offered.map((name) => ({
      name,
      description: BUILTINS[name]!.description,
      inputSchema: z.toJSONSchema(BUILTINS[name]!.input) as { type: 'object' },
    })),
  }))
  server.setRequestHandler(CallToolRequestSchema, async (request, extra) => {
    const name = request.params.name
    const builtin = offered.includes(name) ? BUILTINS[name] : undefined
    if (!builtin) return toResult({ text: `No such tool: ${name}`, isError: true })
    const parsed = builtin.input.safeParse(request.params.arguments ?? {})
    if (!parsed.success) {
      return toResult({ text: `<tool_use_error>InputValidationError: ${z.prettifyError(parsed.error)}</tool_use_error>`, isError: true })
    }
    const callId = request.params._meta?.[CALL_ID_META]
    return toResult(await builtin.run(stoppable({ ...context(), ...(typeof callId === 'string' ? { callId } : {}) }, extra.signal), parsed.data as never))
  })
  return server
}

/**
 * The built-ins a classic turn gets from the session instead of Claude Code, served
 * in-process. The background ones (CronCreate and the rest) live in Claude Code's own
 * process, which the turn ends ("Session-only (not written to disk, dies when Claude
 * exits)"), while these are kept by the session (src/background/). WebFetch's fetch
 * connects only to the addresses its egress check passed (#2684; Claude Code's resolves
 * the name again as it connects). Each call is outward, as every package built-in is.
 */
export const CLASSIC_BUILTINS: readonly string[] = [...BACKGROUND_BUILTINS, 'WebFetch']

/** A classic turn's in-process `builtin` server, serving the CLASSIC_BUILTINS of `names`. */
export function classicBuiltinServer(
  names: readonly string[],
  context: { background?: BackgroundContext | undefined; web?: WebContext | undefined },
): McpSdkServerConfigWithInstance {
  return createSdkMcpServer({
    name: BUILTIN_SERVER,
    tools: names
      .filter((n) => CLASSIC_BUILTINS.includes(n))
      .map((name) => {
        const builtin = BUILTINS[name]!
        return tool(name, builtin.description, (builtin.input as z.ZodObject).shape, async (args) =>
          toResult(await builtin.run(context as BuiltinContext, args as never)),
        )
      }),
  })
}

/** What a classic turn's model is told about the built-ins it gets that way. */
export function classicBuiltinNote(names: readonly string[]): string {
  const tools = names.map((n) => `${n} (mcp__${BUILTIN_SERVER}__${n})`).join(', ')
  const kept = names.some((n) => BACKGROUND_BUILTINS.includes(n))
    ? ' The session keeps the jobs, wakeups, watches and workflows they start past this turn: each sends its prompt, events or result to the ' +
      'session as a new message when it is due and the session is idle.'
    : ''
  return (
    `Claude Code's built-in tools ${tools} are provided in this session as tools of the "${BUILTIN_SERVER}" server, because a ` +
    'plugin package you have names them. Use them where its skills or agents say to use the built-in of that name: they take ' +
    `the same input. Every call of one asks the user first.${kept}`
  )
}
