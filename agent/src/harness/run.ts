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
import { type DecisionListener, makeCanUseTool, makePreToolUseHook, type TierResolver } from './permissions.js'

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
//     paths (#297, #299).

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
  /** Local plugin directories (the SDK accepts `type: "local"` only). */
  pluginPaths?: string[]
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
  /** Claude Code's stderr, already redacted of the credential. */
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
  const base = buildQueryOptions(run.paths)
  const tierOf = run.tierOf ?? (() => undefined)
  const secret = run.credential.secret
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
    canUseTool: makeCanUseTool(tierOf, run.onDecision),
    hooks: { PreToolUse: [makePreToolUseHook(tierOf, run.onDecision)] },
    permissionMode: 'default',
  }
  if (run.model !== undefined) options.model = run.model
  if (run.resume !== undefined) options.resume = run.resume
  if (run.sessionId !== undefined) options.sessionId = run.sessionId
  if (run.sessionStore !== undefined) options.sessionStore = run.sessionStore
  if (run.cwd !== undefined) options.cwd = run.cwd
  if (run.includePartialMessages) options.includePartialMessages = true
  if (run.pluginPaths?.length) {
    options.plugins = run.pluginPaths.map((p) => ({ type: 'local' as const, path: path.resolve(p) }))
  }
  if (run.systemPromptAppend !== undefined) {
    options.systemPrompt = { type: 'preset', preset: 'claude_code', append: run.systemPromptAppend }
  }
  if (run.stderr) {
    const sink = run.stderr
    options.stderr = (data) => sink(data.split(secret).join('[redacted]'))
  }
  return options
}

/** Starts one query. Iterate the returned Query for the SDK message stream. */
export function runHarness(run: HarnessRun): Query {
  return query({ prompt: run.prompt, options: buildHarnessOptions(run) })
}
