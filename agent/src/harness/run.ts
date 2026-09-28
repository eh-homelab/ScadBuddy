import path from 'node:path'
import {
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
  browserInputProblem,
  browserTierOf,
  disallowedBrowserTools,
  type HeadlessBrowserOptions,
  materializeHeadlessBrowser,
} from './headlessBrowser.js'
import {
  type DecisionListener,
  type InputGuard,
  makeCanUseTool,
  makePreToolUseHook,
  type TierResolver,
} from './permissions.js'
import { assertPluginAllowed } from './plugins.js'
import { type LineRedactor, lineRedactor } from './redactLines.js'

// The harness loop (issue #255): one `query()` of the Claude Agent SDK per turn,
// built on buildQueryOptions() so every query keeps `tools: []`,
// `settingSources: []`, `strictMcpConfig` and the service-owned
// CLAUDE_CONFIG_DIR (spec §4.4). This module adds, per query:
//
//   - the credential, through the SDK's `env` option only. `env` "REPLACES the
//     subprocess environment entirely" (sdk.d.ts, 0.3.283), so the key reaches
//     that one Claude Code process and never the container environment
//     (spec §4.4, "Credentials are passed per query through the SDK's `env`
//     option"). Variable names, from
//     https://code.claude.com/docs/en/llm-gateway-connect ("Each variable sends
//     the credential in a different HTTP header: `ANTHROPIC_AUTH_TOKEN` in
//     `Authorization: Bearer`, `ANTHROPIC_API_KEY` in `x-api-key`"):
//       anthropic_api_key → ANTHROPIC_API_KEY
//       gateway           → ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN
//   - CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1: without it Claude Code "also
//     sends nonessential background traffic outside the gateway path, to
//     Anthropic and to third-party services such as GitHub: version checks,
//     telemetry, release notes" (same page, "Turn off traffic outside the
//     gateway path"). The service has no use for any of it.
//   - limits: `maxTurns`, `maxBudgetUsd` ("The query will stop if this budget is
//     exceeded, returning an `error_max_budget_usd` result", sdk.d.ts) and an
//     abort signal for the panel's stop button;
//   - the permission seam (permissions.ts) as both `canUseTool` and a
//     `PreToolUse` hook;
//   - in-process MCP servers (#251's registry plugs in here) and local plugin
//     paths (#297, #299), each vetted by plugins.ts: a plugin that would start
//     a process of its own (command hook, stdio MCP server, LSP server,
//     monitor) is refused, since that process would inherit the credential env;
//   - the headless browser (#349, headlessBrowser.ts) when the session has it
//     enabled: a per-session copy of the vendored `playwright` plugin, its
//     tier map, its disallowed tools and its origin/file-name guard;
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
   * The headless browser for this query (#349, spec §5.3). Only when the
   * `headless_browser_enabled` setting is on; the session manager decides.
   */
  headlessBrowser?: HeadlessBrowserOptions
  /** Maps each tool to its risk tier; tools it does not know are `outward`. */
  tierOf?: TierResolver
  onDecision?: DecisionListener
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
  /** Claude Code's stderr, whole lines, already redacted of the credential. */
  stderr?: (line: string) => void
}

/** The credential's environment variables, and nothing else. */
export function credentialEnv(credential: Credential): Record<string, string> {
  switch (credential.kind) {
    case 'anthropic_api_key':
      return { ANTHROPIC_API_KEY: credential.secret }
    case 'gateway':
      return { ANTHROPIC_BASE_URL: credential.baseUrl, ANTHROPIC_AUTH_TOKEN: credential.secret }
  }
}

function linkedController(signal: AbortSignal | undefined): AbortController {
  const controller = new AbortController()
  if (signal) {
    if (signal.aborted) controller.abort(signal.reason)
    else signal.addEventListener('abort', () => controller.abort(signal.reason), { once: true })
  }
  return controller
}

/** The full SDK options for one run. Pure apart from the AbortController; tests read it. */
export function buildHarnessOptions(run: HarnessRun): Options {
  return buildHarness(run).options
}

function buildHarness(run: HarnessRun): { options: Options; stderr: LineRedactor | undefined } {
  const base = buildQueryOptions(run.paths)
  const ownTiers = run.tierOf ?? (() => undefined)
  let tierOf: TierResolver = ownTiers
  let guard: InputGuard | undefined
  let browserPlugin: string | undefined
  if (run.headlessBrowser) {
    const browser = materializeHeadlessBrowser(run.headlessBrowser)
    assertHeadlessPlugin(browser.pluginDir)
    browserPlugin = browser.pluginDir
    tierOf = (name) => browserTierOf(name) ?? ownTiers(name)
    guard = (name, input) => browserInputProblem(name, input, browser.allowedOrigin)
  }
  const options: Options = {
    ...base,
    env: {
      ...base.env,
      ...credentialEnv(run.credential),
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    },
    mcpServers: { ...(run.mcpServers ?? {}) },
    maxTurns: run.maxTurns ?? DEFAULT_MAX_TURNS,
    maxBudgetUsd: run.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD,
    abortController: linkedController(run.signal),
    canUseTool: makeCanUseTool(tierOf, run.onDecision, guard),
    hooks: { PreToolUse: [makePreToolUseHook(tierOf, run.onDecision, guard)] },
    permissionMode: 'default',
  }
  if (run.model !== undefined) options.model = run.model
  if (run.resume !== undefined) options.resume = run.resume
  if (run.sessionId !== undefined) options.sessionId = run.sessionId
  if (run.sessionStore !== undefined) options.sessionStore = run.sessionStore
  if (run.cwd !== undefined) options.cwd = run.cwd
  if (run.includePartialMessages) options.includePartialMessages = true
  const plugins = (run.pluginPaths ?? []).map((p) => {
    assertPluginAllowed(p)
    return { type: 'local' as const, path: path.resolve(p) }
  })
  // Checked by assertHeadlessPlugin above instead: it is a stdio server, which
  // assertPluginAllowed refuses, but one this module wrote and starts under `env -i`.
  if (browserPlugin !== undefined) plugins.push({ type: 'local', path: browserPlugin })
  if (plugins.length) options.plugins = plugins
  if (browserPlugin !== undefined) {
    // "The `Bash` tool definition is removed from the request. Claude does not
    // see the tool and cannot attempt it." (spec §3.1, permissions). Measured
    // for a plugin server's tools too (test/headlessBrowser.e2e.test.ts).
    options.disallowedTools = disallowedBrowserTools()
    // Measured on Claude Code 2.1.283: with `strictMcpConfig` a plugin's MCP
    // servers are not started at all (the init message lists the plugin but no
    // server). The option exists to ignore MCP configs from settings files,
    // and `settingSources: []` already loads none: the e2e test plants a
    // project `.mcp.json` in the session's cwd and asserts it is not started.
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
  return { options, stderr }
}

/**
 * Starts one query. Iterate the returned Query for the SDK message stream.
 * When the stream ends (done, thrown, or returned early) the last partial
 * stderr line is flushed, redacted.
 */
export function runHarness(run: HarnessRun): Query {
  const { options, stderr } = buildHarness(run)
  const q = query({ prompt: run.prompt, options })
  if (!stderr) return q
  const next = q.next.bind(q)
  const ret = q.return.bind(q)
  const thr = q.throw.bind(q)
  q.next = async (...args) => {
    try {
      const result = await next(...args)
      if (result.done) stderr.flush()
      return result
    } catch (err) {
      stderr.flush()
      throw err
    }
  }
  q.return = async (value) => {
    try {
      return await ret(value)
    } finally {
      stderr.flush()
    }
  }
  q.throw = async (err) => {
    try {
      return await thr(err)
    } finally {
      stderr.flush()
    }
  }
  return q
}
