import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { CallToolResultSchema, ErrorCode, McpError } from '@modelcontextprotocol/sdk/types.js'
import type { components } from '../api/schema.js'
import type { HarnessPlugin } from '../plugins/forwarder.js'

// The messaging client of #5660 (spec 2026-11-08-order-notify-customer-design.md §4, §6.1):
// one call of the tool the shop bound to "customer messages" (the stored setting
// `orders_customer_messaging`). The plugin is reached only as a remote MCP server through
// the loopback forwarder, as a turn reaches it (temporal/turnServers.ts): an `ai_plugins`
// row (`remote:<name>`) or a plugin package's http server (`package:<pkg>/<server>`). A
// stdio or local server is never offered here, since its code would run in this pod. The
// model never sees the plugin's URL or secret, and the plugin's own text never leaves
// this module: an outcome carries a fixed reason only.

export type MessagingBinding = components['schemas']['MessagingBinding']

/** How one call of the plugin's tool ended (§7.2). */
export type SendOutcome =
  | { status: 'sent'; providerMessageId?: string }
  | { status: 'failed'; reason: 'plugin_error' }
  | { status: 'unknown'; reason: 'no_answer' }

/** Why the bound plugin cannot be reached: nothing was sent and nothing was claimed. */
export class MessagingUnavailableError extends Error {
  override readonly name = 'MessagingUnavailableError'
}

/** One open client on the bound tool's server. */
export type MessagingConnection = {
  /** Lists the server's tools (the binding check, §4.3). */
  tools(): Promise<{ name: string; inputSchema: unknown }[]>
  /** Calls the bound tool once with these arguments. Never throws for the plugin's answer. */
  send(args: Record<string, unknown>): Promise<SendOutcome>
  close(): Promise<void>
}

export type CustomerMessaging = {
  /** Opens the bound source; `MessagingUnavailableError` when it cannot be reached now. */
  open(binding: Pick<MessagingBinding, 'source' | 'tool'>): Promise<MessagingConnection>
}

/** What the plugin loaders give: the plugins forwarded for this call, and how to free their routes. */
export type ForwardedPlugins = {
  plugins: HarnessPlugin[]
  /** A package's servers: each of `plugins`, in order, by package and server. */
  servers?: { pkg: string; server: string }[]
  release(): void
}

export type MessagingDeps = {
  /** The enabled remote plugins through the forwarder (main.ts `durableRemotePlugins`). */
  remote?: (() => Promise<ForwardedPlugins>) | undefined
  /** The enabled packages' http MCP servers through the forwarder. */
  packages?: (() => Promise<ForwardedPlugins>) | undefined
  /** A client on a forwarder URL; a test passes its own. */
  connect?: (url: string) => Promise<Client>
}

const REMOTE = /^remote:([A-Za-z0-9_.-]{1,100})$/
const PACKAGE = /^package:([A-Za-z0-9_.-]{1,100})\/([A-Za-z0-9_.-]{1,100})$/

async function connectHttp(url: string): Promise<Client> {
  const client = new Client({ name: 'scadbuddy-order-messages', version: '1' })
  await client.connect(new StreamableHTTPClientTransport(new URL(url)))
  return client
}

/**
 * What a thrown call means. The MCP SDK raises its own `McpError` for a JSON-RPC error the
 * server answered (it refused the call: nothing was sent), and also for a request it gave
 * up on itself (`RequestTimeout`) or a transport that closed (`ConnectionClosed`), where
 * the plugin may have sent it. Anything else (a network error mid-request) is unknown too.
 */
export function outcomeOfError(err: unknown): SendOutcome {
  if (err instanceof McpError && err.code !== ErrorCode.RequestTimeout && err.code !== ErrorCode.ConnectionClosed) {
    return { status: 'failed', reason: 'plugin_error' }
  }
  return { status: 'unknown', reason: 'no_answer' }
}

/** The provider's id from a result's `structuredContent` (`id` or `message_id`), else none. */
function providerId(result: unknown): string | undefined {
  const content = (result as { structuredContent?: unknown }).structuredContent
  if (typeof content !== 'object' || content === null) return undefined
  const record = content as Record<string, unknown>
  const id = record['message_id'] ?? record['id']
  return typeof id === 'string' && id.length > 0 && id.length <= 200 ? id : undefined
}

export function pluginMessaging(deps: MessagingDeps): CustomerMessaging {
  const connect = deps.connect ?? connectHttp
  return {
    async open({ source, tool }) {
      const remote = REMOTE.exec(source)
      const pkg = PACKAGE.exec(source)
      const loader = remote ? deps.remote : pkg ? deps.packages : undefined
      if (!loader) throw new MessagingUnavailableError(`the messaging plugin ${source} is not a remote MCP server this agent offers`)
      const forwarded = await loader()
      let plugin: HarnessPlugin | undefined
      if (remote) plugin = forwarded.plugins.find((p) => p.name === remote[1])
      else if (pkg) {
        const at = (forwarded.servers ?? []).findIndex((s) => s.pkg === pkg[1] && s.server === pkg[2])
        plugin = at < 0 ? undefined : forwarded.plugins[at]
      }
      if (!plugin) {
        forwarded.release()
        throw new MessagingUnavailableError(`the messaging plugin ${source} is not installed and enabled`)
      }
      if (plugin.disabledTools.includes(tool)) {
        forwarded.release()
        throw new MessagingUnavailableError(`${source}'s tool ${tool} is switched off in Settings → Tools & plugins`)
      }
      let client: Client
      try {
        client = await connect(plugin.url)
      } catch {
        forwarded.release()
        throw new MessagingUnavailableError(`the messaging plugin ${source} did not answer`)
      }
      return {
        async tools() {
          const listed = await client.listTools()
          return listed.tools.map((t) => ({ name: t.name, inputSchema: t.inputSchema }))
        },
        async send(args) {
          try {
            // Not `callTool`: it checks the result against the tool's `outputSchema` on this side
            // and throws an McpError for a message the plugin already sent. Only a JSON-RPC error
            // from the server, or `isError`, is a refusal (#5858 review).
            const result = await client.request({ method: 'tools/call', params: { name: tool, arguments: args } }, CallToolResultSchema)
            if (result.isError === true) return { status: 'failed', reason: 'plugin_error' }
            const id = providerId(result)
            return id === undefined ? { status: 'sent' } : { status: 'sent', providerMessageId: id }
          } catch (err) {
            return outcomeOfError(err)
          }
        },
        async close() {
          try {
            await client.close()
          } finally {
            forwarded.release()
          }
        },
      }
    },
  }
}

/** The bound tool's arguments for one message (§4.2): the mapped fields, then `fixed_args`. */
export function messageArguments(
  binding: MessagingBinding,
  message: { to: readonly { value: string }[]; subject: string | null; text: string; key: string },
): Record<string, unknown> {
  const { args } = binding
  const to = message.to.map((r) => r.value)
  return {
    ...(binding.fixed_args ?? {}),
    [args.to]: args.to_shape === 'string' ? to.join(', ') : to,
    ...(args.subject ? { [args.subject]: message.subject ?? '' } : {}),
    [args.text]: message.text,
    ...(args.idempotency_key ? { [args.idempotency_key]: message.key } : {}),
  }
}

/**
 * Why the tool's `inputSchema` cannot take the binding (§4.3 rule 4), or undefined: each
 * mapped argument a string (`to` an array of strings for `to_shape: list`), and every
 * argument the tool requires mapped or in `fixed_args`.
 */
export function bindingMismatch(binding: MessagingBinding, inputSchema: unknown): string | undefined {
  const schema = (inputSchema ?? {}) as { properties?: Record<string, { type?: unknown; items?: { type?: unknown } }>; required?: unknown }
  const props = schema.properties ?? {}
  const { args } = binding
  const want: [string, 'string' | 'list'][] = [
    [args.to, args.to_shape === 'string' ? 'string' : 'list'],
    [args.text, 'string'],
    ...(args.subject ? [[args.subject, 'string'] as [string, 'string']] : []),
    ...(args.idempotency_key ? [[args.idempotency_key, 'string'] as [string, 'string']] : []),
  ]
  for (const [name, kind] of want) {
    const prop = props[name]
    if (!prop) return `the tool ${binding.tool} takes no argument ${name}`
    const ok = kind === 'string' ? prop.type === 'string' : prop.type === 'array' && prop.items?.type === 'string'
    if (!ok) return `the tool's argument ${name} is not ${kind === 'string' ? 'a string' : 'an array of strings'}`
  }
  const covered = new Set([...want.map(([name]) => name), ...Object.keys(binding.fixed_args ?? {})])
  const required = Array.isArray(schema.required) ? schema.required.filter((r): r is string => typeof r === 'string') : []
  const missing = required.filter((r) => !covered.has(r))
  if (missing.length > 0) return `the tool requires ${missing.join(', ')}, which the binding does not fill`
  return undefined
}

/**
 * Opens the binding's source and checks its tool against the binding (§4.3 rules 1 to 4): the
 * open connection, or why it cannot take the binding (the connection is then closed). The
 * settings check and every send run it, so a binding saved without the check, or a tool
 * changed since, is refused before anything is claimed or sent.
 */
export async function openChecked(messaging: CustomerMessaging, binding: MessagingBinding): Promise<MessagingConnection | string> {
  let connection: MessagingConnection
  try {
    connection = await messaging.open(binding)
  } catch (err) {
    if (err instanceof MessagingUnavailableError) return err.message
    throw err
  }
  try {
    const tool = (await connection.tools()).find((t) => t.name === binding.tool)
    const problem = tool ? bindingMismatch(binding, tool.inputSchema) : `${binding.source} lists no tool ${binding.tool}`
    if (problem === undefined) return connection
    await connection.close()
    return problem
  } catch {
    await connection.close().catch(() => {})
    return `${binding.source} did not list its tools`
  }
}
