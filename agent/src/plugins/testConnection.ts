import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport, StreamableHTTPError } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { FetchLike } from '@modelcontextprotocol/sdk/shared/transport.js'
import { z } from 'zod'
import { type ListedTool, listedTool, type Omitted, type StoredTool, toStoredTools } from '../access/listings.js'
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
//
// The page is read through a loose schema (`ListedPage`), not `client.listTools()`: the
// SDK's `ToolSchema` is a plain `z.object` (@modelcontextprotocol/sdk 1.32,
// dist/esm/types.js), which strips every field it does not know, an unknown annotation
// key included, and the stored listing keeps the server's entry as sent (#2171, #778).
// An entry without a name is `toStoredTools`'s to drop, not a reason to fail the test.
const ListedPage = z.looseObject({
  tools: z.array(z.unknown()),
  nextCursor: z.string().optional(),
})

/** The largest response the test reads (forwarder `maxResponseBytes`): the caps bound what is stored, this what is read. */
export const MAX_LIST_RESPONSE_BYTES = 16 * 1024 * 1024

export type PluginToolView = {
  /** As the server names it. */
  name: string
  /** As the model sees it: `mcp__<plugin>__<tool>`, after Claude Code's renaming (registry.ts `harnessToolName`). */
  harness_name: string
  title: string | null
  description: string | null
  annotations: Record<string, unknown> | null
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  /** The server's entry as listed, minus what a cap removed (access/listings.ts). */
  raw: Record<string, unknown>
  /** What a cap removed from `raw`. */
  omitted: Omitted[]
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
  /** Not every tool the server has is here: a `nextCursor`, or entries dropped (nameless, past the cap). */
  truncated: boolean
  /** The listing as stored in ai_tool_listings (empty when the test failed); the routes never send it. */
  stored: StoredTool[]
}

export type PluginTestOptions = {
  timeoutMs?: number
  /** For tests: the fetch the MCP client uses to reach the forwarder. */
  fetch?: FetchLike
}

export const DEFAULT_PLUGIN_TEST_TIMEOUT_MS = 10_000

export function describeTools(
  plugin: Pick<RemotePlugin, 'name' | 'toolTiers' | 'disabledTools'>,
  tools: readonly ListedTool[],
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
    const annotations = tool.annotations
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
      title: tool.title,
      description: tool.description,
      annotations,
      input_schema: tool.input_schema,
      output_schema: tool.output_schema,
      raw: tool.raw,
      omitted: tool.omitted,
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
  const done = (
    ok: boolean,
    detail: string,
    tools: PluginToolView[] = [],
    truncated = false,
    stored: StoredTool[] = [],
  ): PluginTest => ({
    ok,
    detail: redact(detail, secrets),
    duration_ms: performance.now() - started,
    server,
    tools,
    truncated,
    stored,
  })

  const registration = forwarder.register(plugin, address, { filterTools: false, maxResponseBytes: MAX_LIST_RESPONSE_BYTES })
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
    const listed = await Promise.race([client.request({ method: 'tools/list' }, ListedPage, requestOptions), timedOut])
    const stored = toStoredTools(listed.tools)
    const tools = describeTools(plugin, stored.map(listedTool))
    const truncated = listed.nextCursor !== undefined || stored.length < listed.tools.length
    return done(true, `connected; ${tools.length} tool(s)`, tools, truncated, stored)
  } catch (err) {
    if (controller.signal.aborted) return done(false, `timed out after ${timeoutMs} ms waiting for the MCP endpoint`)
    if (err instanceof StreamableHTTPError && err.code !== undefined) {
      return done(false, `the MCP endpoint answered HTTP ${err.code}: ${err.message}`)
    }
    return done(false, `the MCP endpoint could not be used: ${(err as Error).message}`)
  } finally {
    clearTimeout(timer)
    controller.abort()
    // A test opens one MCP session; this ends it (the route is not recorded, so nothing else would).
    await transport.terminateSession().catch(() => {})
    await client.close().catch(() => {})
    registration.release()
  }
}
