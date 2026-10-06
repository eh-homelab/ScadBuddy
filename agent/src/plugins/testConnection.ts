import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport, StreamableHTTPError } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { FetchLike } from '@modelcontextprotocol/sdk/shared/transport.js'
import type { RiskTier } from '../harness/permissions.js'
import { redact } from '../secrets.js'
import type { PluginForwarder } from './forwarder.js'
import { harnessToolName, headerSecretVariants, type RemotePlugin, toolPrefix } from './registry.js'

// Settings' "test connection" for a plugin (#297): connect over Streamable
// HTTP (spec D5) with the official MCP client, run the MCP handshake
// (`initialize`, which the protocol requires before any request) and ONE
// `tools/list`, then close. No tool is called. The result lists each tool with
// the name and tier the harness would give it, so the admin reviews the tools
// before enabling the plugin (issue #297, "Review before enable").
//
// The client talks to the loopback forwarder (forwarder.ts), exactly as
// Claude Code does in a run: it connects to the address the egress check
// passed, follows no redirect, starts no OAuth discovery, and adds the
// header itself. Unlike a run, the forwarder does not filter the tool list
// here, so disabled and colliding tools are shown too.
//
// `suggested_tier` pre-fills the review from `readOnlyHint` and nothing else;
// it is never applied (the MCP spec: "clients MUST consider tool annotations
// to be untrusted unless they come from trusted servers",
// https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
// One `tools/list` page only: a `nextCursor` is reported as `truncated`.

export type PluginToolView = {
  /** As the server names it. */
  name: string
  /** As the model sees it: `mcp__<plugin>__<tool>`, after Claude Code's renaming (registry.ts `harnessToolName`). */
  harness_name: string
  description: string | null
  annotations: Record<string, unknown> | null
  /** The tier the harness applies. */
  tier: RiskTier
  /**
   * `explicit`: set in tool_tiers; `default`: outward (spec §8.1);
   * `renamed`: Claude Code renames this tool, so it cannot take a tier and
   * stays outward; `collision`: another tool gets the same harness name, so
   * both are hidden from the model.
   */
  tier_source: 'explicit' | 'default' | 'renamed' | 'collision'
  /** The other tools sharing `harness_name`. */
  collides_with: string[]
  /** `read` when the server claims readOnlyHint; a pre-fill for the review only. */
  suggested_tier: RiskTier | null
  /** Never shown to the model: in disabled_tools, or colliding. */
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
  /** For tests: the fetch the MCP client uses to reach the forwarder. */
  fetch?: FetchLike
}

export const DEFAULT_PLUGIN_TEST_TIMEOUT_MS = 10_000

export function describeTools(
  plugin: Pick<RemotePlugin, 'name' | 'toolTiers' | 'disabledTools'>,
  tools: readonly { name: string; description?: string | undefined; annotations?: object | undefined }[],
): PluginToolView[] {
  const disabled = new Set(plugin.disabledTools.map(harnessToolName))
  const byName = new Map<string, string[]>()
  for (const tool of tools) {
    const name = harnessToolName(tool.name)
    byName.set(name, [...(byName.get(name) ?? []), tool.name])
  }
  return tools.map((tool) => {
    const name = harnessToolName(tool.name)
    const others = (byName.get(name) ?? []).filter((n) => n !== tool.name)
    const annotations = (tool.annotations ?? null) as Record<string, unknown> | null
    let tier: RiskTier = 'outward'
    let source: PluginToolView['tier_source'] = 'default'
    if (others.length > 0) source = 'collision'
    else if (name !== tool.name) source = 'renamed'
    else if (Object.hasOwn(plugin.toolTiers, tool.name)) {
      tier = plugin.toolTiers[tool.name] ?? 'outward'
      source = 'explicit'
    }
    return {
      name: tool.name,
      harness_name: `${toolPrefix(plugin.name)}${name}`,
      description: tool.description ?? null,
      annotations,
      tier,
      tier_source: source,
      collides_with: others,
      suggested_tier: annotations?.readOnlyHint === true ? 'read' : null,
      disabled: disabled.has(name) || others.length > 0,
    }
  })
}

/** Tests `plugin`, whose endpoint passed the egress check at `address`, through `forwarder`. */
export async function testPlugin(
  plugin: RemotePlugin,
  address: string,
  forwarder: PluginForwarder,
  options: PluginTestOptions = {},
): Promise<PluginTest> {
  const started = performance.now()
  const timeoutMs = options.timeoutMs ?? DEFAULT_PLUGIN_TEST_TIMEOUT_MS
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error('timed out')), timeoutMs)
  const secrets = plugin.header ? headerSecretVariants(plugin.header.value) : []
  let server: PluginTest['server'] = null
  const done = (ok: boolean, detail: string, tools: PluginToolView[] = [], truncated = false): PluginTest => ({
    ok,
    detail: redact(detail, secrets),
    duration_ms: Math.round(performance.now() - started),
    server,
    tools,
    truncated,
  })

  const registration = forwarder.register(plugin, address, { filterTools: false })
  const transport = new StreamableHTTPClientTransport(new URL(registration.url), {
    ...(options.fetch ? { fetch: options.fetch } : {}),
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
    registration.release()
  }
}
