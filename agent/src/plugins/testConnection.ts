import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport, StreamableHTTPError } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { FetchLike } from '@modelcontextprotocol/sdk/shared/transport.js'
import type { RiskTier } from '../harness/permissions.js'
import { redact } from '../secrets.js'
import { pluginTierResolver, type RemotePlugin, toolPrefix } from './registry.js'

// Settings' "test connection" for a plugin (#297): connect to the endpoint
// over Streamable HTTP (spec D5) with the official MCP client, run the MCP
// handshake (`initialize`, which the protocol requires before any request) and
// ONE `tools/list`, then close. No tool is called. The result lists each tool
// with the tier the harness would give it, so the admin reviews the tools
// before enabling the plugin (issue #297, "Review before enable").
//
// `suggested_tier` pre-fills the review from `readOnlyHint` and nothing else;
// it is never applied (the MCP spec: "clients MUST consider tool annotations
// to be untrusted unless they come from trusted servers",
// https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
//
// Redirects are refused (`redirect: 'error'` on every fetch the transport
// makes), so the egress check on the URL (registry.ts `assertEndpointAllowed`)
// cannot be stepped around by a 30x to a metadata address. One `tools/list`
// page only: a `nextCursor` is reported as `truncated`.

export type PluginToolView = {
  name: string
  /** As the model sees it: `mcp__<plugin>__<tool>`. */
  harness_name: string
  description: string | null
  annotations: Record<string, unknown> | null
  /** The tier the harness applies. */
  tier: RiskTier
  /** `explicit`: set by an admin in tool_tiers; `default`: outward (spec §8.1). */
  tier_source: 'explicit' | 'default'
  /** `read` when the server claims readOnlyHint; a pre-fill for the review only. */
  suggested_tier: RiskTier | null
  /** In disabled_tools, so the model never sees it. */
  disabled: boolean
}

export type PluginTest = {
  ok: boolean
  /** A human-readable reason; the secret is redacted from it. */
  detail: string
  duration_ms: number
  server: { name: string; version: string } | null
  tools: PluginToolView[]
  /** The server has more tools than one tools/list page returned. */
  truncated: boolean
}

export type PluginTestOptions = {
  timeoutMs?: number
  /** For tests; the global fetch otherwise. Always called with `redirect: 'error'`. */
  fetch?: FetchLike
}

export const DEFAULT_PLUGIN_TEST_TIMEOUT_MS = 10_000

export function describeTools(
  plugin: Pick<RemotePlugin, 'name' | 'toolTiers' | 'disabledTools'>,
  tools: readonly { name: string; description?: string | undefined; annotations?: object | undefined }[],
): PluginToolView[] {
  const tierOf = pluginTierResolver([plugin])
  const disabled = new Set(plugin.disabledTools)
  return tools.map((tool) => {
    const harnessName = `${toolPrefix(plugin.name)}${tool.name}`
    const annotations = (tool.annotations ?? null) as Record<string, unknown> | null
    return {
      name: tool.name,
      harness_name: harnessName,
      description: tool.description ?? null,
      annotations,
      tier: tierOf(harnessName) ?? 'outward',
      tier_source: Object.hasOwn(plugin.toolTiers, tool.name) ? 'explicit' : 'default',
      suggested_tier: annotations?.readOnlyHint === true ? 'read' : null,
      disabled: disabled.has(tool.name),
    }
  })
}

export async function testPlugin(plugin: RemotePlugin, options: PluginTestOptions = {}): Promise<PluginTest> {
  const started = Date.now()
  const timeoutMs = options.timeoutMs ?? DEFAULT_PLUGIN_TEST_TIMEOUT_MS
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('timed out')), timeoutMs)
  const secrets = plugin.header ? [plugin.header.value] : []
  let server: PluginTest['server'] = null
  const done = (ok: boolean, detail: string, tools: PluginToolView[] = [], truncated = false): PluginTest => ({
    ok,
    detail: redact(detail, secrets),
    duration_ms: Date.now() - started,
    server,
    tools,
    truncated,
  })

  const baseFetch: FetchLike = options.fetch ?? ((url, init) => fetch(url, init))
  const noRedirects: FetchLike = (url, init) => baseFetch(url, { ...init, redirect: 'error' })
  const transport = new StreamableHTTPClientTransport(new URL(plugin.url), {
    fetch: noRedirects,
    requestInit: plugin.header ? { headers: { [plugin.header.name]: plugin.header.value } } : {},
    // One attempt: a test answers now rather than retrying in the background.
    reconnectionOptions: { maxReconnectionDelay: 0, initialReconnectionDelay: 0, reconnectionDelayGrowFactor: 1, maxRetries: 0 },
  })
  const client = new Client({ name: 'scadbuddy-agent', version: '0.1.0' })
  const timedOut = new Promise<never>((_, reject) => {
    controller.signal.addEventListener('abort', () => reject(new Error('timed out')), { once: true })
  })
  timedOut.catch(() => {})
  try {
    const requestOptions = { signal: controller.signal, timeout: timeoutMs }
    await Promise.race([client.connect(transport, requestOptions), timedOut])
    const info = client.getServerVersion()
    server = info ? { name: info.name, version: info.version } : null
    const listed = await Promise.race([client.listTools(undefined, requestOptions), timedOut])
    const tools = describeTools(plugin, listed.tools)
    return done(true, `connected; ${tools.length} tool(s)`, tools, listed.nextCursor !== undefined)
  } catch (err) {
    if (controller.signal.aborted) return done(false, `timed out after ${timeoutMs} ms waiting for the MCP endpoint`)
    if (err instanceof StreamableHTTPError && err.code !== undefined) {
      return done(false, `the MCP endpoint answered HTTP ${err.code}: ${err.message}`)
    }
    return done(false, `the MCP endpoint could not be used: ${(err as Error).message}`)
  } finally {
    clearTimeout(timer)
    controller.abort()
    await client.close().catch(() => {})
  }
}
