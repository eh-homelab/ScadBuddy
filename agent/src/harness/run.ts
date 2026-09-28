import path from 'node:path'
import {
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
import { type DecisionListener, makeCanUseTool, makePreToolUseHook, type TierResolver } from './permissions.js'
import { assertPluginAllowed } from './plugins.js'
import { type LineRedactor, lineRedactor } from './redactLines.js'
import { pluginTierResolver, type RemotePlugin, toolPrefix } from '../plugins/registry.js'

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
//   - registered remote MCP plugins (#297, `remotePlugins`), as Streamable
//     HTTP servers with their own tier maps (remotePluginOptions below);
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
   * Registered remote MCP plugins (#297, src/plugins/registry.ts), each loaded
   * as an SDK `{ type: 'http' }` server named after the plugin. Their tiers
   * come from the plugin's own `tool_tiers` (unlisted: outward), ahead of
   * `tierOf`; their disabled tools go in `disallowedTools`. See
   * `remotePluginOptions` for how the header value is kept off the command line.
   */
  remotePlugins?: RemotePlugin[]
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

/** Environment variable carrying the header value of the `index`-th remote plugin. */
export function pluginHeaderEnv(index: number): string {
  return `SCADBUDDY_PLUGIN_${index}_HEADER`
}

export class PluginConfigError extends Error {
  override name = 'PluginConfigError'
}

/**
 * The SDK options for the remote plugins: `mcpServers` entries, the
 * environment carrying their header values, and `disallowedTools`.
 *
 * - `{ type: 'http', url, headers }` is the SDK's `McpHttpServerConfig`
 *   (sdk.d.ts 0.3.283), the Streamable HTTP transport (spec D5); `'sse'` is the
 *   legacy transport D5 rejects and is never produced.
 * - HEADER VALUES STAY OFF THE COMMAND LINE. The SDK hands every non-SDK MCP
 *   server to Claude Code as `--mcp-config <json>` on its argv (read in
 *   sdk.mjs 0.3.283: `j.push("--mcp-config", ...({mcpServers: D}))`), where
 *   `/proc/<pid>/cmdline` shows it. So the header is written as
 *   `${SCADBUDDY_PLUGIN_<i>_HEADER}` and the value goes in the query's `env`,
 *   next to the Claude credential; Claude Code expands the reference when it
 *   connects. MEASURED in test/plugins.e2e.test.ts: the fake MCP server
 *   receives the value, and the argv the SDK builds does not contain it.
 *   Values come from the env, so a stored value is never expanded again, and
 *   registry URLs refuse `$` (registry.ts), so nothing the operator types can
 *   reference the credential variables.
 * - `alwaysLoad: true`: "all tools from this server are always included in
 *   the prompt and never deferred behind tool search ... this also blocks
 *   startup until the server is connected (capped at the standard 5s connect
 *   timeout)" (sdk.d.ts). Without it MCP startup is non-blocking and the first
 *   turn may not see the plugin's tools.
 * - disabled tools: `disallowedTools` "will be removed from the model's
 *   context and cannot be used" (sdk.d.ts), measured in the e2e test.
 */
export function remotePluginOptions(
  plugins: readonly RemotePlugin[],
  taken: ReadonlySet<string>,
): { mcpServers: Record<string, McpHttpServerConfig>; env: Record<string, string>; disallowedTools: string[] } {
  const mcpServers: Record<string, McpHttpServerConfig> = {}
  const env: Record<string, string> = {}
  const disallowedTools: string[] = []
  plugins.forEach((plugin, index) => {
    if (taken.has(plugin.name) || Object.hasOwn(mcpServers, plugin.name)) {
      throw new PluginConfigError(`MCP server name "${plugin.name}" is used twice`)
    }
    const server: McpHttpServerConfig = { type: 'http', url: plugin.url, alwaysLoad: true }
    if (plugin.header) {
      const variable = pluginHeaderEnv(index)
      env[variable] = plugin.header.value
      server.headers = { [plugin.header.name]: `\${${variable}}` }
    }
    mcpServers[plugin.name] = server
    for (const tool of plugin.disabledTools) disallowedTools.push(`${toolPrefix(plugin.name)}${tool}`)
  })
  return { mcpServers, env, disallowedTools }
}

/** The tier resolver a run uses: its plugins' tiers first, then `run.tierOf`. */
export function harnessTierOf(run: Pick<HarnessRun, 'remotePlugins' | 'tierOf'>): TierResolver {
  const base = run.tierOf ?? (() => undefined)
  if (!run.remotePlugins?.length) return base
  const plugins = pluginTierResolver(run.remotePlugins)
  return (toolName) => plugins(toolName) ?? base(toolName)
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
  const tierOf = harnessTierOf(run)
  const remote = remotePluginOptions(run.remotePlugins ?? [], new Set(Object.keys(run.mcpServers ?? {})))
  const options: Options = {
    ...base,
    env: {
      ...base.env,
      ...remote.env,
      ...credentialEnv(run.credential),
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
    },
    mcpServers: { ...(run.mcpServers ?? {}), ...remote.mcpServers },
    maxTurns: run.maxTurns ?? DEFAULT_MAX_TURNS,
    maxBudgetUsd: run.maxBudgetUsd ?? DEFAULT_MAX_BUDGET_USD,
    abortController: linkedController(run.signal),
    canUseTool: makeCanUseTool(tierOf, run.onDecision),
    hooks: { PreToolUse: [makePreToolUseHook(tierOf, run.onDecision)] },
    permissionMode: 'default',
  }
  if (remote.disallowedTools.length) options.disallowedTools = remote.disallowedTools
  if (run.model !== undefined) options.model = run.model
  if (run.resume !== undefined) options.resume = run.resume
  if (run.sessionId !== undefined) options.sessionId = run.sessionId
  if (run.sessionStore !== undefined) options.sessionStore = run.sessionStore
  if (run.cwd !== undefined) options.cwd = run.cwd
  if (run.includePartialMessages) options.includePartialMessages = true
  if (run.pluginPaths?.length) {
    options.plugins = run.pluginPaths.map((p) => {
      assertPluginAllowed(p)
      return { type: 'local' as const, path: path.resolve(p) }
    })
  }
  if (run.systemPromptAppend !== undefined) {
    options.systemPrompt = { type: 'preset', preset: 'claude_code', append: run.systemPromptAppend }
  }
  let stderr: LineRedactor | undefined
  if (run.stderr) {
    // Line-buffered: a secret split across two chunks is still one line here.
    const redactor = lineRedactor([run.credential.secret, ...Object.values(remote.env)], run.stderr)
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
