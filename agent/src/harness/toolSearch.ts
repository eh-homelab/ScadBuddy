// Tool search (#5221): Claude Code withholds the definitions of the turn's MCP tools,
// names them in a system-reminder, and loads the ones the model asks for with its
// `ToolSearch` built-in. https://code.claude.com/docs/en/agent-sdk/tool-search.md:
//
//   ENABLE_TOOL_SEARCH (unset) | true  "Tool search is on, with tool definitions deferred"
//   auto   "Tool search is off ... until the definitions it can defer reach 10% of the
//          model's context window. Tool search then activates"
//   false  "Tool search is off. All tool definitions are loaded into context on every turn."
//
// Measured on Claude Code 2.1.310 against test/support/fakeAnthropic.ts with the whole
// registry in-process (test/toolSearch.sdk.test.ts): with `tools: []` (harness/options.ts)
// there is no ToolSearch, so nothing is deferred and the first request was 334,793 bytes
// with 283 tool definitions. With ToolSearch offered and nothing upfront it was 42,516
// bytes: ToolSearch, a `DeferredToolPlaceholder` with `defer_loading`, the
// `advanced-tool-use-2025-11-20` beta, and every other tool by name only. With the
// `UPFRONT_TOOLS` below in full as well (what a session turn sends by default), 47,990.
// A search's result is a `tool_reference` block, and the tool found goes into `tools`
// with `defer_loading` from then on. The credential gateway
// passes the beta header and the body as they are (gateway/server.ts). A deferred tool the
// model calls by name without a search still runs: Claude Code holds its schema.

/** Settings → Assistant → Runtime `tool_search` (sessions/turnSettings.ts). */
export type ToolSearchMode = 'off' | 'auto' | 'always'

export const TOOL_SEARCH_MODES: readonly ToolSearchMode[] = ['off', 'auto', 'always']

/** Claude Code's built-in that loads deferred tools. It reads the turn's own tool list only: `read`. */
export const TOOL_SEARCH = 'ToolSearch'

/** The variable Claude Code reads for each mode. */
export function toolSearchEnv(mode: ToolSearchMode): string {
  return mode === 'off' ? 'false' : mode === 'always' ? 'true' : 'auto'
}

/**
 * ScadBuddy tools whose definitions are sent on every request by default, whatever the mode
 * (`alwaysLoad`, tools/projections.ts): their default loading is `base`, and Settings → Tools
 * & plugins sets each tool's (`ToolLoading` below, #838). What almost every turn starts with. Each eval
 * scenario (agent/evals/scenarios.ts) and the session's own flows open a model
 * (`list_models`, `get_model`, `get_schema`, `get_source`), change it (`update_source`),
 * render it (`render_model`, then `get_render_job` to follow it), or write one, which
 * starts at `get_authoring_guide`. Together about 5.6 KB of the 304 KB the registry's
 * definitions came to (measured as above). The question server's ask_user and
 * request_user_attention load upfront too (harness/questions.ts), and AskUserQuestion is
 * a core built-in, never deferred.
 */
export const UPFRONT_TOOLS: ReadonlySet<string> = new Set([
  'list_models',
  'get_model',
  'get_schema',
  'get_source',
  'update_source',
  'render_model',
  'get_render_job',
  'get_authoring_guide',
])

/**
 * A ScadBuddy tool's loading (#838), set per tool in Settings → Tools & plugins beside its
 * access: `base` sends its definition on every request (`alwaysLoad`); `searchable` leaves it
 * to tool search, found by ToolSearch and callable by name all the same. A tool that is `off`
 * is its access (`ai_access`), not a loading. With `tool_search` off every tool loads anyway.
 */
export type ToolLoading = 'base' | 'searchable'

export const TOOL_LOADINGS: readonly ToolLoading[] = ['base', 'searchable']

/**
 * The `ai_settings` key of the stored loadings, `{ <registry tool name>: ToolLoading }`: only
 * the tools whose loading differs from `defaultLoading`. A write evicts every warm process
 * (sessions/warmEvict.ts), and each turn reads it as it is assembled (sessions/turnRunner.ts).
 */
export const TOOL_LOADING_KEY = 'tool_loading'

export type ToolLoadings = Readonly<Record<string, ToolLoading>>

export function isToolLoading(v: unknown): v is ToolLoading {
  return typeof v === 'string' && (TOOL_LOADINGS as readonly string[]).includes(v)
}

/** What a registry tool loads as with nothing stored: `UPFRONT_TOOLS` are base. */
export function defaultLoading(tool: string): ToolLoading {
  return UPFRONT_TOOLS.has(tool) ? 'base' : 'searchable'
}

/** The stored loadings; an entry that does not parse (written by hand) is dropped, so its tool takes its default. */
export function parseToolLoadings(value: unknown): Record<string, ToolLoading> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return {}
  return Object.fromEntries(Object.entries(value).filter((e): e is [string, ToolLoading] => e[0] !== '' && isToolLoading(e[1])))
}

export async function readToolLoadings(settings: { get<T>(key: string): Promise<T | undefined> }): Promise<Record<string, ToolLoading>> {
  return parseToolLoadings(await settings.get<unknown>(TOOL_LOADING_KEY))
}

/** A registry tool's loading under `stored`. */
export function loadingOf(tool: string, stored: ToolLoadings): ToolLoading {
  return Object.hasOwn(stored, tool) ? (stored[tool] as ToolLoading) : defaultLoading(tool)
}

/** `stored` with `tool` set to `loading` (`null`: its default); an entry is kept only where it differs from the default. */
export function withLoading(stored: ToolLoadings, tool: string, loading: ToolLoading | null): Record<string, ToolLoading> {
  const rest = Object.fromEntries(Object.entries(stored).filter(([name]) => name !== tool))
  return loading === null || loading === defaultLoading(tool) ? rest : { ...rest, [tool]: loading }
}
