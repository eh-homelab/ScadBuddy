import path from 'node:path'
import {
  type CanUseTool,
  type HookCallbackMatcher,
  type HookEvent,
  type McpHttpServerConfig,
  type McpSdkServerConfigWithInstance,
  type Options,
  type Query,
  query,
  type SDKUserMessage,
  type SessionStore,
} from '@anthropic-ai/claude-agent-sdk'
import type { Credential } from '../credentials.js'
import { buildQueryOptions, type HarnessPaths } from './options.js'
import {
  assertHeadlessPlugin,
  browserInputGuard,
  browserTierOf,
  disallowedBrowserTools,
  type HeadlessBrowserOptions,
  materializeHeadlessBrowser,
  originToApprove,
} from './headlessBrowser.js'
import {
  type ApprovalGate,
  type ApprovalRequest,
  type ApprovalVerdict,
  decide,
  type DecisionListener,
  type InputGuard,
  makeCanUseTool,
  makePreToolUseHook,
  type TierResolver,
} from './permissions.js'
import { OWN_PLUGIN_TOOLS, ownPluginTierOf } from './ownPlugin.js'
import { ASK_USER_QUESTION, askThroughGate, isQuestionTool, QUESTION_SERVER, type QuestionGate, questionServer } from './questions.js'
import { assertPluginAllowed } from './plugins.js'
import { type LineRedactor, lineRedactor } from './redactLines.js'
import type { HarnessPlugin } from '../plugins/forwarder.js'
import { harnessToolName, pluginTierResolver, toolPrefix } from '../plugins/registry.js'

// The harness loop (issue #255): one `query()` of the Claude Agent SDK per turn,
// built on buildQueryOptions() so every query keeps `tools: []` (or only
// Skill and Agent, with ScadBuddy's own plugin: ownPlugin.ts, #896),
// `settingSources: []`, `strictMcpConfig` and the service-owned
// CLAUDE_CONFIG_DIR (spec §4.4). This module adds, per query:
//
//   - the credential, through the SDK's `env` option only. `env` "REPLACES the
//     subprocess environment entirely" (sdk.d.ts, 0.3.287), so the key reaches
//     that one Claude Code process and never the container environment
//     (spec §4.4, "Credentials are passed per query through the SDK's `env`
//     option"). Variable names, from
//     https://code.claude.com/docs/en/llm-gateway-connect ("Each variable sends
//     the credential in a different HTTP header: `ANTHROPIC_AUTH_TOKEN` in
//     `Authorization: Bearer`, `ANTHROPIC_API_KEY` in `x-api-key`"):
//       anthropic_api_key  → ANTHROPIC_API_KEY
//       claude_oauth_token → CLAUDE_CODE_OAUTH_TOKEN, the token `claude setup-token`
//                            prints (an sk-ant-oat01- token in x-api-key is a 401)
//       gateway            → ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
//   - CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1: without it Claude Code "also
//     sends nonessential background traffic outside the gateway path, to
//     Anthropic and to third-party services such as GitHub: version checks,
//     telemetry, release notes" (same page, "Turn off traffic outside the
//     gateway path"). The service has no use for any of it.
//   - CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 (#946): an `Agent` call asked to
//     `run_in_background` then runs inside the turn, as a foreground one does
//     (Claude Code 2.1.283 and 2.1.287, test/harnessWiring.test.ts).
//     Backgrounded, it outlived its parent's turn. SDK 0.3.283 closed Claude
//     Code's input at a string prompt's first result, and after that Claude
//     Code refused every permission request itself, with "The user doesn't
//     want to take this action right now", asking neither canUseTool nor the
//     user: its calls, and those of the turn Claude Code starts when it
//     reports back, read tools included (measured on 2.1.283). 0.3.287 keeps
//     the input open until the session is idle (at most 10 minutes, sdk.mjs),
//     so those calls do reach canUseTool, but after the turn's result: the
//     report-back turn comes as a second init and result in the same query
//     (measured 2026-10-06). In the turn, every call goes through the
//     permission seam below, and an outward one parks at the gate.
//   - CLAUDE_CODE_MAX_RETRIES, only with `maxRetries`: fallback.ts bounds
//     Claude Code's retries on one credential when there is another to fall
//     back to (#1093);
//   - limits: `maxTurns`, `maxBudgetUsd` ("The query will stop if this budget is
//     exceeded, returning an `error_max_budget_usd` result", sdk.d.ts) and an
//     abort signal for the panel's stop button;
//   - the permission seam (permissions.ts) as both `canUseTool` and a
//     `PreToolUse` hook, with the session's approval gate when it has one
//     (#258: outward calls park until a human decides);
//   - in-process MCP servers (#251's registry plugs in here) and local plugin
//     paths (#297, #299), each vetted by plugins.ts: a plugin that would start
//     a process of its own (command hook, stdio MCP server, LSP server,
//     monitor) is refused, since that process would inherit the credential env;
//   - registered remote MCP plugins (#297, `remotePlugins`, via the loopback
//     forwarder in src/plugins/forwarder.ts), as Streamable
//     HTTP servers with their own tier maps (remotePluginOptions below);
//   - the headless browser (#349, headlessBrowser.ts) when the session has it
//     enabled: a per-session copy of the vendored `playwright` plugin, its
//     tier map, its disallowed tools and its origin/file-name guard;
//   - in-process memory hooks (memory/hindsight.ts): `UserPromptSubmit`,
//     `Stop` and optionally `PostToolUse` callbacks that recall from and
//     retain to the enabled `hindsight` plugin's bank. SDK callbacks, run in
//     this process, so they are not the command hooks plugins.ts refuses;
//     they sit beside the permission seam's `PreToolUse` hook;  
//     a human's approval of an off-origin navigation also approves
//     that origin for the session (browserOrigins.ts).
//   - the AskUserQuestion built-in (#940, questions.ts) when the run has a
//     question gate: the call is answered in `canUseTool`, where it parks
//     until the user answers in the panel; and, for subagents, which Claude
//     Code refuses AskUserQuestion, the `scadbuddy_questions` server's ask_user tool on
//     the same gate;
//   - Claude Code's stderr, buffered to whole lines and redacted of the
//     credential (redactLines.ts), so a secret split across chunks is caught.

/** Placeholders until Settings stores per-session caps in `ai_settings` (#256). */
export const DEFAULT_MAX_TURNS = 25
export const DEFAULT_MAX_BUDGET_USD = 1

export type HarnessRun = {
  paths: HarnessPaths
  credential: Credential
  prompt: string | AsyncIterable<SDKUserMessage>
  /** Claude model id or alias; the SDK's default when omitted. */
  model?: string
  maxTurns?: number
  maxBudgetUsd?: number
  /**
   * How many times Claude Code retries a failed model request on this
   * credential (CLAUDE_CODE_MAX_RETRIES); its own default when omitted. Set
   * when there is another credential to fall back to (fallback.ts, #1093).
   */
  maxRetries?: number
  /** Aborting stops the query and its Claude Code process. */
  signal?: AbortSignal
  /**
   * In-process SDK MCP servers only (`createSdkMcpServer`), keyed by server
   * name; their tools are `mcp__{name}__{tool}`. Remote or stdio servers are
   * not accepted here (spec D5, D9).
   */
  mcpServers?: Record<string, McpSdkServerConfigWithInstance>
  /**
   * Local plugin directories (the SDK accepts `type: "local"` only). Each is
   * vetted first (plugins.ts): one that would start a process of its own, which
   * would inherit the credential env, throws PluginRefusedError.
   */
  pluginPaths?: string[]
  /**
   * ScadBuddy's own plugin (ownPlugin.ts `OWN_PLUGIN_DIR`, #896), vetted like
   * `pluginPaths`. Loading it gives the run the Skill and Agent tools, at
   * `read`, so its skills and subagents can be used; a run without it has no
   * built-in tool at all.
   */
  ownPlugin?: string
  /**
   * Registered remote MCP plugins (#297), each already registered with the
   * loopback forwarder (src/plugins/forwarder.ts `forwardForRun`), so `url` is
   * the forwarder's and carries no secret. Each is loaded as an SDK
   * `{ type: 'http' }` server named after the plugin. Their tiers come from the
   * plugin's own `tool_tiers` (unlisted: outward), ahead of `tierOf`; their
   * disabled tools go in `disallowedTools`.
   */
  remotePlugins?: HarnessPlugin[]
  /**
   * The headless browser for this query (#349, spec §5.3). Only when the
   * `headless_browser_enabled` setting is on; the session manager decides.
   */
  headlessBrowser?: HeadlessBrowserOptions
  /** Maps each tool to its risk tier; tools it does not know are `outward`. */
  tierOf?: TierResolver
  onDecision?: DecisionListener
  /**
   * Parks outward calls until a human decides (#258, src/approvals/). Without
   * one, outward calls are denied as needing approval.
   */
  approvalGate?: ApprovalGate
  /**
   * Parks AskUserQuestion calls until the user answers (#940, questions.ts).
   * Only with one is the run given the tool.
   */
  questionGate?: QuestionGate
  /** Appended to the SDK's default system prompt: route, model, diagnostics (#256). */
  systemPromptAppend?: string
  /** Session id to resume (#300). */
  resume?: string
  /**
   * The id a NEW session gets (#300). sdk.d.ts: "Use a specific session ID for
   * the conversation instead of an auto-generated one. Must be a valid UUID.
   * Cannot be used with `continue` or `resume` unless `forkSession` is also set".
   */
  sessionId?: string
  /**
   * Transcript mirror (#300, sessions/store.ts). sdk.d.ts: "the subprocess
   * still writes to CLAUDE_CONFIG_DIR ... AND emits entries to this adapter";
   * resume `load()`s from it "before subprocess spawn".
   */
  sessionStore?: SessionStore
  /** The query's working directory; the service-wide scratch dir when omitted. */
  cwd?: string
  /** Yield `stream_event` messages (text deltas) as well as complete messages. */
  includePartialMessages?: boolean
  /**
   * SDK callback hooks for automatic memory (memory/hindsight.ts
   * `createMemoryHooks`), added beside the permission seam's `PreToolUse`.
   */
  memoryHooks?: Partial<Record<HookEvent, HookCallbackMatcher[]>>
  /**
   * SDK callback hooks for the turn's trace (telemetry/turn.ts
   * `TurnTrace.hooks`): every tool's start and end, after the permission
   * seam's `PreToolUse` and the memory hooks. In-process callbacks, like the
   * memory hooks, so not the command hooks plugins.ts refuses.
   */
  traceHooks?: Partial<Record<HookEvent, HookCallbackMatcher[]>>
  /** Claude Code's stderr, whole lines, already redacted of the credential. */
  stderr?: (line: string) => void
}

/** The permission seam's PreToolUse hook first, then the memory hooks, then the trace hooks, by event. */
function mergeHooks(
  base: Partial<Record<HookEvent, HookCallbackMatcher[]>>,
  extra: Partial<Record<HookEvent, HookCallbackMatcher[]>> | undefined,
): Partial<Record<HookEvent, HookCallbackMatcher[]>> {
  const out = { ...base }
  for (const [event, matchers] of Object.entries(extra ?? {}) as [HookEvent, HookCallbackMatcher[]][]) {
    out[event] = [...(out[event] ?? []), ...matchers]
  }
  return out
}

/** The credential's environment variables, and nothing else. */
export function credentialEnv(credential: Credential): Record<string, string> {
  switch (credential.kind) {
    case 'anthropic_api_key':
      return { ANTHROPIC_API_KEY: credential.secret }
    case 'claude_oauth_token':
      return { CLAUDE_CODE_OAUTH_TOKEN: credential.secret }
    case 'gateway':
      return { ANTHROPIC_BASE_URL: credential.baseUrl, ANTHROPIC_AUTH_TOKEN: credential.secret }
  }
}

export class PluginConfigError extends Error {
  override name = 'PluginConfigError'
}

/**
 * The SDK options for the remote plugins: `mcpServers` entries and
 * `disallowedTools`.
 *
 * - `{ type: 'http', url }` is the SDK's `McpHttpServerConfig` (sdk.d.ts
 *   0.3.287), the Streamable HTTP transport (spec D5); `'sse'` is the legacy
 *   transport D5 rejects and is never produced. The URL is the loopback
 *   forwarder's, and no header is configured: the forwarder adds the plugin's
 *   own. The SDK passes this config on Claude Code's argv (`--mcp-config`,
 *   sdk.mjs 0.3.287), where the forwarder token is all there is to see.
 * - `alwaysLoad: true`: "all tools from this server are always included in
 *   the prompt and never deferred behind tool search, except a tool the server
 *   itself lists with _meta anthropic/alwaysLoad set to false ... true also
 *   blocks startup until the server is connected (capped at the standard 5s
 *   connect timeout)" (sdk.d.ts 0.3.287). Without it MCP startup is
 *   non-blocking and the first turn may not see the plugin's tools.
 * - disabled tools: `disallowedTools` "will be removed from the model's
 *   context and cannot be used" (sdk.d.ts), by the name Claude Code gives the
 *   tool (`harnessToolName`); the forwarder also hides them from tools/list.
 */
export function remotePluginOptions(
  plugins: readonly HarnessPlugin[],
  taken: ReadonlySet<string>,
): { mcpServers: Record<string, McpHttpServerConfig>; disallowedTools: string[] } {
  const mcpServers: Record<string, McpHttpServerConfig> = {}
  const disallowedTools: string[] = []
  for (const plugin of plugins) {
    if (taken.has(plugin.name) || Object.hasOwn(mcpServers, plugin.name)) {
      throw new PluginConfigError(`MCP server name "${plugin.name}" is used twice`)
    }
    mcpServers[plugin.name] = { type: 'http', url: plugin.url, alwaysLoad: true }
    for (const tool of plugin.disabledTools) {
      const name = `${toolPrefix(plugin.name)}${harnessToolName(tool)}`
      if (!disallowedTools.includes(name)) disallowedTools.push(name)
    }
  }
  return { mcpServers, disallowedTools }
}

/**
 * The tier resolver a run uses: the own plugin's Skill and Agent when it is
 * loaded, then its plugins' tiers, then `run.tierOf`.
 */
export function harnessTierOf(run: Pick<HarnessRun, 'remotePlugins' | 'tierOf' | 'ownPlugin'>): TierResolver {
  const given = run.tierOf ?? (() => undefined)
  const remote = run.remotePlugins?.length ? pluginTierResolver(run.remotePlugins) : undefined
  const base: TierResolver = remote ? (toolName, input) => remote(toolName) ?? given(toolName, input) : given
  if (run.ownPlugin === undefined) return base
  return (toolName, input) => ownPluginTierOf(toolName) ?? base(toolName, input)
}

/** `promise`'s value, or undefined once `signal` aborts first. */
function untilAborted<T>(promise: Promise<T>, signal: AbortSignal): Promise<T | undefined> {
  if (signal.aborted) return Promise.resolve(undefined)
  return new Promise((resolve) => {
    const onAbort = () => resolve(undefined)
    signal.addEventListener('abort', onAbort, { once: true })
    void promise.then((value) => {
      signal.removeEventListener('abort', onAbort)
      resolve(value)
    })
  })
}

/** canUseTool with AskUserQuestion answered through the question gate (#940). */
function answeringQuestions(questions: QuestionGate, inner: CanUseTool): CanUseTool {
  return (toolName, input, options) =>
    toolName === ASK_USER_QUESTION ? askThroughGate(questions, input, options.toolUseID, options.signal) : inner(toolName, input, options)
}

/**
 * Forces the permission prompt for AskUserQuestion, so the call always
 * reaches canUseTool (where it is answered) and no allow rule can skip it.
 */
const QUESTION_PROMPT_HOOK: HookCallbackMatcher = {
  // Anchored: the SDK tests the matcher as a regex (memory/hindsight.ts).
  matcher: `^${ASK_USER_QUESTION}$`,
  hooks: [
    (input) =>
      Promise.resolve(
        input.hook_event_name === 'PreToolUse' && input.tool_name === ASK_USER_QUESTION
          ? {
              hookSpecificOutput: {
                hookEventName: 'PreToolUse' as const,
                permissionDecision: 'ask' as const,
                permissionDecisionReason: 'A question for the user, answered in the ScadBuddy panel.',
              },
            }
          : {},
      ),
  ],
}

/**
 * The full SDK options for one run. Pure apart from the AbortController,
 * which `run.signal` aborts directly; tests read it. runHarness interrupts
 * first instead (`stopFirst`).
 */
export function buildHarnessOptions(run: HarnessRun): Options {
  const { options, abort } = buildHarness(run)
  if (run.signal) {
    if (run.signal.aborted) abort.abort(run.signal.reason)
    else run.signal.addEventListener('abort', () => abort.abort(run.signal?.reason), { once: true })
  }
  return options
}

/** How long an interrupted query may take to end on its own before it is aborted. */
export const INTERRUPT_GRACE_MS = 5_000

function buildHarness(run: HarnessRun): { options: Options; stderr: LineRedactor | undefined; abort: AbortController } {
  const base = buildQueryOptions(run.paths)
  const harnessTiers = harnessTierOf(run)
  const questions = run.questionGate
  // AskUserQuestion and ask_user only ask the user; the first is answered in
  // canUseTool below, the second by its own handler.
  const ownTiers: TierResolver = questions
    ? (name, input) => (isQuestionTool(name) ? 'read' : harnessTiers(name, input))
    : harnessTiers
  const local = { ...(run.mcpServers ?? {}), ...(questions ? { [QUESTION_SERVER]: questionServer(questions) } : {}) }
  if (questions && Object.hasOwn(run.mcpServers ?? {}, QUESTION_SERVER)) {
    throw new PluginConfigError(`MCP server name "${QUESTION_SERVER}" is used twice`)
  }
  const remote = remotePluginOptions(run.remotePlugins ?? [], new Set(Object.keys(local)))
  let tierOf: TierResolver = ownTiers
  let guard: InputGuard | undefined
  let gate = run.approvalGate
  let browserPlugin: string | undefined
  if (run.headlessBrowser) {
    const browser = materializeHeadlessBrowser(run.headlessBrowser)
    const remember = run.headlessBrowser.rememberOrigin
    assertHeadlessPlugin(browser.pluginDir)
    browserPlugin = browser.pluginDir
    tierOf = (name, input) => browserTierOf(name) ?? ownTiers(name, input)
    guard = (name, input) => browserInputGuard(name, input, browser.origins, browser.approved)
    const inner = gate
    if (inner) {
      // The approval of the first navigation to an origin approves the origin
      // for the session: recorded durably first (a failure denies the call),
      // then in the set the guard and the request guard read, before the
      // navigation runs.
      // Calls in flight for an origin already waiting on a human wait on
      // that one decision instead of each asking: approved, they are decided
      // again (and now allowed); refused, they are refused with it.
      const asking = new Map<string, Promise<ApprovalVerdict>>()
      const approveOrigin = async (request: ApprovalRequest): Promise<ApprovalVerdict> => {
        const verdict = await inner(request)
        if (!verdict.approved) return verdict
        const origin = originToApprove(request.toolName, verdict.input, browser.origins, browser.approved)
        if (origin !== undefined) {
          await remember?.(origin, verdict.approvalId)
          browser.approve(origin)
        }
        return verdict
      }
      gate = async (request) => {
        const origin = originToApprove(request.toolName, request.input, browser.origins, browser.approved)
        if (origin === undefined) return approveOrigin(request)
        const pending = asking.get(origin)
        if (pending === undefined) {
          const own = approveOrigin(request)
          asking.set(origin, own)
          try {
            return await own
          } finally {
            asking.delete(origin)
          }
        }
        const first = await untilAborted(
          pending.catch((err: unknown): ApprovalVerdict => ({
            approved: false,
            message: err instanceof Error ? err.message : String(err),
          })),
          request.signal,
        )
        if (first === undefined) return { approved: false, message: `The call stopped while ${origin} awaited approval.` }
        if (browser.approved.has(origin)) {
          const decision = decide(request.toolName, tierOf, request.input, guard)
          if (decision.decision === 'allow') return { approved: true, input: decision.input ?? request.input }
          return inner(request)
        }
        const why = first.approved ? '' : ` ${first.message}`
        return { approved: false, message: `Opening ${origin} was not approved for this session.${why}` }
      }
    }
  }
  const permission = makeCanUseTool(tierOf, run.onDecision, gate, guard)
  const abort = new AbortController()
  const options: Options = {
    ...base,
    env: {
      ...base.env,
      ...credentialEnv(run.credential),
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
      CLAUDE_CODE_DISABLE_BACKGROUND_TASKS: '1',
      ...(run.maxRetries === undefined ? {} : { CLAUDE_CODE_MAX_RETRIES: String(run.maxRetries) }),
    },
    mcpServers: { ...local, ...remote.mcpServers },
    maxTurns: run.maxTurns ?? DEFAULT_MAX_TURNS,
    maxBudgetUsd: run.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD,
    abortController: abort,
    canUseTool: questions ? answeringQuestions(questions, permission) : permission,
    hooks: mergeHooks(
      mergeHooks(
        {
          PreToolUse: [
            makePreToolUseHook(tierOf, run.onDecision, gate, guard),
            ...(questions ? [QUESTION_PROMPT_HOOK] : []),
          ],
        },
        run.memoryHooks,
      ),
      run.traceHooks,
    ),
    permissionMode: 'default',
  }
  if (remote.disallowedTools.length) options.disallowedTools = remote.disallowedTools
  if (run.model !== undefined) options.model = run.model
  if (run.resume !== undefined) options.resume = run.resume
  if (run.sessionId !== undefined) options.sessionId = run.sessionId
  if (run.sessionStore !== undefined) {
    options.sessionStore = run.sessionStore
    // 'eager': every transcript frame is appended as it is written, not at the
    // turn's end ('batched', the default: "flush at end-of-turn or when pending
    // thresholds are exceeded", sdk.d.ts 0.3.287 SessionStoreFlush). An aborted
    // query still flushes its batch as it ends, if the process lives that long
    // (SessionManager.stopTurns waits for it; test/sessions.e2e.test.ts). One
    // that dies first (a SIGKILL, an OOM, the grace period running out) would
    // otherwise leave nothing of its turn in Postgres, not even the user's
    // message, and the next turn would resume without it, as on 2026-09-30.
    options.sessionStoreFlush = 'eager'
  }
  if (run.cwd !== undefined) options.cwd = run.cwd
  if (run.includePartialMessages) options.includePartialMessages = true
  const plugins = [...(run.ownPlugin !== undefined ? [run.ownPlugin] : []), ...(run.pluginPaths ?? [])].map((p) => {
    assertPluginAllowed(p)
    return { type: 'local' as const, path: path.resolve(p) }
  })
  const builtins = [...(run.ownPlugin !== undefined ? OWN_PLUGIN_TOOLS : []), ...(questions ? [ASK_USER_QUESTION] : [])]
  if (builtins.length) options.tools = builtins
  // Checked by assertHeadlessPlugin above instead: it is a stdio server, which
  // assertPluginAllowed refuses, but one this module wrote and starts under `env -i`.
  if (browserPlugin !== undefined) plugins.push({ type: 'local', path: browserPlugin })
  if (plugins.length) options.plugins = plugins
  if (browserPlugin !== undefined) {
    // "The `Bash` tool definition is removed from the request. Claude does not
    // see the tool and cannot attempt it." (spec §3.1, permissions). Measured
    // for a plugin server's tools too (test/headlessBrowser.e2e.test.ts).
    options.disallowedTools = [...remote.disallowedTools, ...disallowedBrowserTools()]
    // Measured on Claude Code 2.1.283 and 2.1.287: with `strictMcpConfig` a
    // plugin's MCP servers are not started at all (the init message lists the
    // plugin but no server). The option exists to ignore MCP configs from
    // settings files, and `settingSources: []` already loads none: the e2e test
    // plants a project `.mcp.json` in the session's cwd and asserts it is not
    // started.
    options.strictMcpConfig = false
  }
  if (run.systemPromptAppend !== undefined) {
    options.systemPrompt = { type: 'preset', preset: 'claude_code', append: run.systemPromptAppend }
  }
  let stderr: LineRedactor | undefined
  if (run.stderr) {
    // Line-buffered: a secret split across two chunks is still one line here.
    const redactor = lineRedactor([run.credential.secret], run.stderr)
    stderr = redactor
    options.stderr = (data) => redactor.write(data)
  }
  return { options, stderr, abort }
}

/**
 * Starts one query. Iterate the returned Query for the SDK message stream.
 * When the stream ends (done, thrown, or returned early) the last partial
 * stderr line is flushed, redacted.
 */
export function runHarness(run: HarnessRun): Query {
  const { options, stderr, abort } = buildHarness(run)
  const q = query({ prompt: run.prompt, options })
  const ended = stopFirst(q, run.signal, abort)
  const next = q.next.bind(q)
  const ret = q.return.bind(q)
  const thr = q.throw.bind(q)
  const end = () => {
    ended()
    stderr?.flush()
  }
  q.next = async (...args) => {
    try {
      const result = await next(...args)
      if (result.done) end()
      return result
    } catch (err) {
      end()
      throw err
    }
  }
  q.return = async (value) => {
    try {
      return await ret(value)
    } finally {
      end()
    }
  }
  q.throw = async (err) => {
    try {
      return await thr(err)
    } finally {
      end()
    }
  }
  // The SDK's Query hands for-await its inner message stream, which bypasses
  // the wrappers above (#1009). Iterate the Query itself.
  q[Symbol.asyncIterator] = () => q
  return q
}

/**
 * Stops the query when `signal` aborts (#1168): by interrupting it first, as
 * Claude Code's own Esc does, and aborting it only if it has not ended within
 * INTERRUPT_GRACE_MS. An abort alone closes the control stream, and a call
 * parked in canUseTool (an approval, a question) then fails with "Tool
 * permission request failed: AbortError: Tool permission stream closed
 * before response received", which Claude Code hands the model as the tool's
 * error and calls it again: a reply nobody asked for, and spend. Interrupted,
 * it refuses the pending call and ends the turn with an
 * `error_during_execution` result, and the model is not called. Measured on
 * SDK 0.3.283 and 0.3.287 (test/questions.e2e.test.ts, test/approvals.e2e.test.ts).
 * Returns what the caller calls once the stream has ended.
 */
function stopFirst(q: Query, signal: AbortSignal | undefined, abort: AbortController): () => void {
  let done = false
  let grace: ReturnType<typeof setTimeout> | undefined
  const stop = () => {
    if (done) {
      abort.abort(signal?.reason)
      return
    }
    grace = setTimeout(() => abort.abort(signal?.reason), INTERRUPT_GRACE_MS)
    grace.unref()
    q.interrupt().catch(() => abort.abort(signal?.reason))
  }
  if (signal?.aborted) abort.abort(signal.reason)
  else signal?.addEventListener('abort', stop, { once: true })
  return () => {
    done = true
    clearTimeout(grace)
    signal?.removeEventListener('abort', stop)
    // Whatever stopped it, the process goes with the stream.
    if (signal?.aborted) abort.abort(signal.reason)
  }
}
