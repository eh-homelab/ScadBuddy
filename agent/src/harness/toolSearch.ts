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
 * ScadBuddy tools whose definitions are sent on every request whatever the mode
 * (`alwaysLoad`, tools/projections.ts): what almost every turn starts with. Each eval
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
