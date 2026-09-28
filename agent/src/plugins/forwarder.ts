import { randomBytes } from 'node:crypto'
import {
  createServer,
  type IncomingHttpHeaders,
  type IncomingMessage,
  request as httpRequest,
  type OutgoingHttpHeaders,
  type Server,
  type ServerResponse,
} from 'node:http'
import { request as httpsRequest } from 'node:https'
import { type AddressInfo, isIP, type LookupFunction } from 'node:net'
import { plainAddress } from '../http/origins.js'
import { markUntrustedContent, wrapUntrustedText } from '../safety/untrusted.js'
import { harnessToolName, headerSecretVariants, type LoadedPlugins, type RemotePlugin } from './registry.js'

// The loopback forwarder between Claude Code and plugin endpoints (#297).
//
// WHY. Claude Code's own MCP client, given a plugin URL and header, follows
// 30x redirects and the `resource_metadata` URL of a `WWW-Authenticate`
// challenge, and sends the configured header to wherever they point
// (measured on CLI 2.1.283 by the PR #464 review: a 307 to another origin got
// every request with the header; resource_metadata=http://169.254.169.254/…
// got a GET with it). That steps around the egress check (http/egress.ts),
// the URL binding of the sealed secret, and spec D5's https-only rule. So
// Claude Code is never given the plugin's URL or its secret. Each run
// registers its plugins here and hands Claude Code
// `http://127.0.0.1:<port>/p/<random token>` instead (loopback, spec §8.4's
// one exception). The forwarder:
//
//   - connects to the ADDRESS the egress check passed (registry.ts
//     `assertEndpointAllowed`), never resolving the name again, with TLS
//     still verified against the hostname (SNI and certificate);
//   - follows no redirect: a 30x becomes a 502;
//   - turns a 401/407 into a 502 without its `WWW-Authenticate`, so no OAuth
//     discovery starts;
//   - adds the plugin's auth header itself, and passes on only the MCP
//     transport headers (content-type, accept, mcp-session-id,
//     mcp-protocol-version, last-event-id) in, and content-type,
//     mcp-session-id and cache-control out;
//   - enforces tool names (registry.ts "TOOL NAMES"): a `tools/list` result
//     loses its disabled tools and every tool whose harness name collides
//     with another's, and a `tools/call` is refused for a disabled or hidden
//     tool, or for one whose raw name differs from the `tool_tiers` entry its
//     harness name matches.
//   - marks every tools/call result as untrusted data (#258,
//     `rewriteMessages`): Claude Code puts a plugin's result straight into the
//     model's context, so it gets the same envelope as ScadBuddy's own tools.
//     The ids of the calls a route has forwarded are kept on the ROUTE, not
//     the request, and every 200 JSON or SSE body the route answers with is
//     rewritten against them: the MCP client matches a response to its
//     request by id alone, whichever stream carries it (its own POST's, another
//     POST's, or the standalone GET stream), so a plugin cannot get a result
//     past the rewrite by answering elsewhere. An SSE stream is split the way
//     the spec reads it (a line ends at CRLF, CR or LF), and while a route has
//     calls in flight an event whose data is not JSON is withheld: the plugin
//     cannot smuggle a result in a block the forwarder cannot parse but the
//     client can.
//
// The token is a per-registration capability (144 random bits), released
// when the run ends. It is on Claude Code's command line (the SDK passes MCP
// config there, spec §3.1), which is why it is not the secret.

export type ForwardOptions = {
  /** Filter `tools/list` results (disabled and colliding tools). Off for the connection test, which shows them all. */
  filterTools?: boolean
}

export type Registration = { url: string; release(): void }

type Route = {
  plugin: RemotePlugin
  address: string
  filterTools: boolean
  /** Harness names hidden because two or more tools share them (learned from tools/list). */
  collided: Set<string>
  /** The requests this route has forwarded whose responses are rewritten, on whichever stream they arrive. */
  rewrites: Rewrites
}

/** Request ids remembered per route; the oldest is forgotten past this (a client's ids only grow). */
export const MAX_TRACKED_IDS = 4096

export const MAX_BODY_BYTES = 4 * 1024 * 1024
const REQUEST_HEADERS = ['content-type', 'accept', 'mcp-session-id', 'mcp-protocol-version', 'last-event-id']
const RESPONSE_HEADERS = ['content-type', 'mcp-session-id', 'cache-control']

type JsonRpc = { jsonrpc?: string; id?: string | number | null; method?: string; params?: { name?: unknown } }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Why a `tools/call` of `raw` is refused, or undefined when it may go through. */
export function callRefusal(route: Pick<Route, 'plugin' | 'collided'>, raw: string): string | undefined {
  const name = harnessToolName(raw)
  const { plugin } = route
  if (plugin.disabledTools.includes(raw) || plugin.disabledTools.some((d) => harnessToolName(d) === name)) {
    return `tool "${raw}" is disabled for plugin ${plugin.name}`
  }
  if (route.collided.has(name)) {
    return `tool "${raw}" shares the name mcp__${plugin.name}__${name} with another tool, so it is hidden`
  }
  const tier = Object.hasOwn(plugin.toolTiers, name) ? plugin.toolTiers[name] : undefined
  if (tier !== undefined && tier !== 'outward' && raw !== name) {
    return `tool "${raw}" is not the tool "${name}" whose tier was set; it was not run`
  }
  return undefined
}

/** Drops disabled and colliding tools from a tools/list result; records the collisions. */
export function filterToolList(route: Pick<Route, 'plugin' | 'collided'>, tools: unknown[]): unknown[] {
  const counts = new Map<string, number>()
  for (const tool of tools) {
    if (isRecord(tool) && typeof tool.name === 'string') {
      const name = harnessToolName(tool.name)
      counts.set(name, (counts.get(name) ?? 0) + 1)
    }
  }
  for (const [name, count] of counts) if (count > 1) route.collided.add(name)
  return tools.filter((tool) => isRecord(tool) && typeof tool.name === 'string' && !isHidden(route, tool.name))
}

/** Hidden from the list: disabled or colliding (a tier mismatch is refused at call time, not hidden). */
function isHidden(route: Pick<Route, 'plugin' | 'collided'>, raw: string): boolean {
  const name = harnessToolName(raw)
  return (
    route.collided.has(name) ||
    route.plugin.disabledTools.includes(raw) ||
    route.plugin.disabledTools.some((d) => harnessToolName(d) === name)
  )
}

function readBody(req: IncomingMessage): Promise<Buffer | 'too large'> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    req.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        resolve('too large')
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

function fail(res: ServerResponse, status: number, message: string): void {
  if (res.headersSent) {
    res.destroy()
    return
  }
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(JSON.stringify({ error: message }))
}

function isRewritable(type: string): boolean {
  return type.startsWith('application/json') || type.startsWith('text/event-stream')
}

function pick(headers: IncomingHttpHeaders, names: readonly string[]): OutgoingHttpHeaders {
  const out: OutgoingHttpHeaders = {}
  for (const name of names) {
    const value = headers[name]
    if (value !== undefined) out[name] = value
  }
  return out
}

/** The requests whose responses are rewritten: tools/list ids, and tools/call ids with their tool. */
export type Rewrites = { lists: Set<unknown>; calls: Map<unknown, string> }

function remember<K, V>(map: Map<K, V>, key: K, value: V): void {
  if (map.size >= MAX_TRACKED_IDS) map.delete(map.keys().next().value as K)
  map.set(key, value)
}

/**
 * Splits an SSE stream into event blocks the way the spec (and the MCP client's
 * `eventsource-parser`) reads it: a line ends at CRLF, CR or LF, and an empty
 * line ends an event. Line endings come out normalised to LF. A trailing CR is
 * held back until the next chunk says whether it was half a CRLF.
 */
export class SseBlocks {
  private pending = ''
  private carry = false

  /** The complete event blocks in the stream so far, without their terminating blank line. */
  feed(chunk: string): string[] {
    let text = (this.carry ? '\r' : '') + chunk
    this.carry = text.endsWith('\r')
    if (this.carry) text = text.slice(0, -1)
    this.pending += text.replace(/\r\n|\r/g, '\n')
    const blocks: string[] = []
    let at: number
    while ((at = this.pending.indexOf('\n\n')) !== -1) {
      blocks.push(this.pending.slice(0, at))
      this.pending = this.pending.slice(at + 2)
    }
    return blocks
  }
}

/** Where a plugin tool's content comes from, for the untrusted-data envelope. */
export function pluginSource(plugin: Pick<RemotePlugin, 'name'>): string {
  return `the remote plugin "${plugin.name}", a third-party MCP server; its results can carry anything its operator or its data sources wrote`
}

/**
 * Rewrites each JSON-RPC response to a request in `rewrites`:
 *   - tools/list: disabled and colliding tools dropped (`filterToolList`);
 *   - tools/call (#258): the result's content marked as untrusted data, as
 *     ScadBuddy's own tool results are (safety/untrusted.ts
 *     `markUntrustedContent`: text in the envelope, a preamble before each
 *     image, audio or blob), under the name the model knows the tool by
 *     (`mcp__<plugin>__<tool>`); a JSON-RPC error's message is wrapped too,
 *     since Claude Code hands it to the model as the tool's error.
 */
export function rewriteMessages(payload: unknown, rewrites: Rewrites, route: Pick<Route, 'plugin' | 'collided'>): unknown {
  const one = (m: unknown): unknown => {
    if (!isRecord(m)) return m
    if (rewrites.lists.has(m.id) && isRecord(m.result) && Array.isArray(m.result.tools)) {
      return { ...m, result: { ...m.result, tools: filterToolList(route, m.result.tools) } }
    }
    const raw = rewrites.calls.get(m.id)
    if (raw === undefined) return m
    const tool = `mcp__${route.plugin.name}__${harnessToolName(raw)}`
    const source = pluginSource(route.plugin)
    if (isRecord(m.result) && Array.isArray(m.result.content)) {
      return { ...m, result: { ...m.result, content: markUntrustedContent(m.result.content, tool, source) } }
    }
    if (isRecord(m.error) && typeof m.error.message === 'string') {
      return { ...m, error: { ...m.error, message: wrapUntrustedText(tool, source, m.error.message) } }
    }
    return m
  }
  return Array.isArray(payload) ? payload.map(one) : one(payload)
}

/**
 * Rewrites one SSE event block (without its terminating blank line). Undefined
 * means the block is withheld: its data is not JSON while the route has tool
 * calls in flight, so it could be a reply the rewrite cannot see but the
 * client would parse (the same rule as a JSON body that is not JSON).
 */
export function rewriteSseEvent(block: string, ids: Rewrites, route: Pick<Route, 'plugin' | 'collided'>): string | undefined {
  const lines = block.split(/\r\n|\r|\n/)
  const data = lines.filter((l) => l.startsWith('data:')).map((l) => l.slice(5).replace(/^ /, ''))
  if (data.length === 0) return block
  let parsed: unknown
  try {
    parsed = JSON.parse(data.join('\n'))
  } catch {
    return ids.calls.size > 0 ? undefined : block
  }
  // Guarded like the JSON-body path: a shape the rewrite cannot handle is
  // withheld while a call is in flight, never thrown into the stream's handler.
  let data_: string
  try {
    const rewritten = rewriteMessages(parsed, ids, route)
    if (rewritten === parsed) return block
    data_ = JSON.stringify(rewritten)
  } catch {
    return ids.calls.size > 0 ? undefined : block
  }
  return [...lines.filter((l) => !l.startsWith('data:')), `data: ${data_}`].join('\n')
}

/** The JSON-RPC error a call gets when its reply was withheld. */
function withheldError(id: unknown, why: string): string {
  return JSON.stringify({ jsonrpc: '2.0', id, error: { code: -32603, message: `ScadBuddy: ${why}` } })
}

export class PluginForwarder {
  private readonly server: Server
  private readonly routes = new Map<string, Route>()
  private port = 0

  private constructor() {
    this.server = createServer((req, res) => {
      this.handle(req, res).catch((err: unknown) => fail(res, 502, `forwarder error: ${(err as Error).message}`))
    })
  }

  /** Listens on 127.0.0.1, on a port the OS picks. */
  static async start(): Promise<PluginForwarder> {
    const forwarder = new PluginForwarder()
    await new Promise<void>((resolve, reject) => {
      forwarder.server.once('error', reject)
      forwarder.server.listen(0, '127.0.0.1', () => resolve())
    })
    forwarder.port = (forwarder.server.address() as AddressInfo).port
    return forwarder
  }

  /** Registers a plugin whose endpoint passed the egress check at `address`. */
  register(plugin: RemotePlugin, address: string, options: ForwardOptions = {}): Registration {
    const token = randomBytes(18).toString('base64url')
    this.routes.set(token, {
      plugin,
      address,
      filterTools: options.filterTools ?? true,
      collided: new Set(),
      rewrites: { lists: new Set(), calls: new Map() },
    })
    return {
      url: `http://127.0.0.1:${this.port}/p/${token}`,
      release: () => {
        this.routes.delete(token)
      },
    }
  }

  get size(): number {
    return this.routes.size
  }

  close(): Promise<void> {
    this.routes.clear()
    return new Promise((resolve) => {
      this.server.closeAllConnections()
      this.server.close(() => resolve())
    })
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const match = /^\/p\/([A-Za-z0-9_-]{24})$/.exec((req.url ?? '').split('?')[0] ?? '')
    const route = match?.[1] === undefined ? undefined : this.routes.get(match[1])
    if (!route) return fail(res, 404, 'no such plugin route')
    const method = req.method ?? 'GET'
    if (!['POST', 'GET', 'DELETE'].includes(method)) return fail(res, 405, 'method not allowed')

    let body: Buffer | undefined
    const { rewrites } = route
    /** This request's own tools/call ids: the replies it is expected to carry. */
    const own = new Set<unknown>()
    if (method === 'POST') {
      const read = await readBody(req)
      if (read === 'too large') return fail(res, 413, 'request body too large')
      body = read
      let parsed: unknown
      try {
        parsed = JSON.parse(body.toString('utf8'))
      } catch {
        return fail(res, 400, 'request body is not JSON')
      }
      const messages = (Array.isArray(parsed) ? parsed : [parsed]) as JsonRpc[]
      for (const m of messages) {
        if (!isRecord(m)) continue
        if (m.method === 'tools/list' && route.filterTools && m.id !== undefined && m.id !== null) {
          if (rewrites.lists.size >= MAX_TRACKED_IDS) rewrites.lists.delete(rewrites.lists.values().next().value)
          rewrites.lists.add(m.id)
        }
        if (m.method === 'tools/call' && isRecord(m.params) && typeof m.params.name === 'string') {
          const refusal = callRefusal(route, m.params.name)
          if (refusal !== undefined) {
            // A JSON-RPC error, so the client reports it as the tool's failure.
            res.writeHead(200, { 'content-type': 'application/json' })
            res.end(
              JSON.stringify({ jsonrpc: '2.0', id: m.id ?? null, error: { code: -32602, message: `ScadBuddy: ${refusal}` } }),
            )
            return
          }
          if (m.id !== undefined && m.id !== null) {
            remember(rewrites.calls, m.id, m.params.name)
            own.add(m.id)
          }
        }
      }
    }

    const target = new URL(route.plugin.url)
    const hostname = target.hostname.startsWith('[') ? target.hostname.slice(1, -1) : target.hostname
    const address = plainAddress(route.address)
    const family = isIP(address) === 6 ? 6 : 4
    // Pinned: the checked address, whatever the name resolves to now.
    const lookup = ((_host: string, opts: { all?: boolean }, cb: (...args: unknown[]) => void) => {
      if (opts.all) cb(null, [{ address, family }])
      else cb(null, address, family)
    }) as unknown as LookupFunction
    const headers: OutgoingHttpHeaders = { ...pick(req.headers, REQUEST_HEADERS), host: target.host }
    if (route.plugin.header) headers[route.plugin.header.name] = route.plugin.header.value
    if (body) headers['content-length'] = body.length

    const send = target.protocol === 'https:' ? httpsRequest : httpRequest
    const upstream = send({
      protocol: target.protocol,
      hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      path: `${target.pathname}${target.search}`,
      method,
      headers,
      lookup,
      agent: false,
      ...(target.protocol === 'https:' && isIP(hostname) === 0 ? { servername: hostname } : {}),
    })
    res.on('close', () => {
      if (!res.writableFinished) upstream.destroy()
    })
    upstream.on('error', (err) => {
      const secrets = route.plugin.header ? headerSecretVariants(route.plugin.header.value) : []
      let message = err.message
      for (const s of secrets) message = message.split(s).join('[redacted]')
      fail(res, 502, `plugin endpoint unreachable: ${message}`)
    })
    upstream.on('response', (up) => {
      const status = up.statusCode ?? 502
      if (status >= 300 && status < 400) {
        up.resume()
        return fail(res, 502, `plugin endpoint answered HTTP ${status} (a redirect); redirects are not followed`)
      }
      if (status === 401 || status === 407) {
        up.resume()
        return fail(res, 502, `plugin endpoint refused the credential (HTTP ${status}); OAuth discovery is not supported`)
      }
      const out = pick(up.headers, RESPONSE_HEADERS)
      const type = String(up.headers['content-type'] ?? '').toLowerCase()
      // A tool call's reply the rewrite cannot mark (#258): an error status (the
      // MCP client folds the body into the error the model reads) or a body
      // that is neither JSON nor SSE. Withheld; the status and session header
      // stay, so a client still sees a 404's expired session.
      if (own.size > 0 && (status !== 200 || !isRewritable(type))) {
        up.resume()
        const kept = pick(up.headers, ['mcp-session-id'])
        res.writeHead(status === 200 ? 502 : status, { ...kept, 'content-type': 'application/json' })
        res.end(JSON.stringify({ error: `plugin answered a tool call with HTTP ${status} (${type || 'no content type'}); ScadBuddy does not pass that reply on` }))
        return
      }
      if (status !== 200) {
        res.writeHead(status, out)
        up.pipe(res)
        return
      }
      // Every 200 JSON or SSE body goes through the rewrite, GET streams
      // included: a response is matched to its request by id, on any stream.
      if (type.startsWith('application/json')) {
        const chunks: Buffer[] = []
        up.on('data', (c: Buffer) => chunks.push(c))
        up.on('end', () => {
          let text = Buffer.concat(chunks).toString('utf8')
          try {
            text = JSON.stringify(rewriteMessages(JSON.parse(text), rewrites, route))
          } catch {
            // Not JSON: passed through for a listing, withheld from a tool call (#258).
            if (own.size > 0) {
              return fail(res, 502, 'plugin answered a tool call with a body that is not JSON; ScadBuddy does not pass it on')
            }
          }
          res.writeHead(status, out)
          res.end(text)
        })
        return
      }
      if (type.startsWith('text/event-stream')) {
        res.writeHead(status, out)
        const blocks = new SseBlocks()
        up.setEncoding('utf8')
        up.on('data', (chunk: string) => {
          for (const block of blocks.feed(chunk)) {
            const rewritten = rewriteSseEvent(block, rewrites, route)
            if (rewritten !== undefined) {
              res.write(`${rewritten}\n\n`)
              continue
            }
            // Withheld (#258). On a stream that carries no call's reply (the
            // standalone GET stream) the block is dropped; on a POST's stream the
            // replies its own calls wait for may have been in it, so they get an
            // error instead of a wait that ends in the client's timeout, and the
            // stream ends here.
            if (own.size === 0) continue
            for (const id of own) {
              res.write(`data: ${withheldError(id, 'the plugin answered with an event that is not JSON; ScadBuddy does not pass it on')}\n\n`)
            }
            own.clear()
            up.removeAllListeners('data')
            up.destroy()
            res.end()
            return
          }
        })
        // What is left is an unterminated event, which no client dispatches.
        up.on('end', () => res.end())
        return
      }
      res.writeHead(status, out)
      up.pipe(res)
    })
    upstream.end(body)
  }
}

/** A plugin as one harness run sees it: its forwarder URL, not its endpoint. */
export type HarnessPlugin = {
  name: string
  url: string
  toolTiers: RemotePlugin['toolTiers']
  disabledTools: string[]
}

export type PluginsForRun = {
  plugins: HarnessPlugin[]
  problems: string[]
  /** Header values and their bare tokens, for redaction. */
  secrets: string[]
  release(): void
}

/** Registers the loaded plugins for one run; `release()` when the run ends. */
export function forwardForRun(loaded: LoadedPlugins, forwarder: PluginForwarder): PluginsForRun {
  const registrations = loaded.plugins.map(({ plugin, address }) => ({
    plugin,
    registration: forwarder.register(plugin, address),
  }))
  return {
    plugins: registrations.map(({ plugin, registration }) => ({
      name: plugin.name,
      url: registration.url,
      toolTiers: plugin.toolTiers,
      disabledTools: plugin.disabledTools,
    })),
    problems: loaded.problems,
    secrets: loaded.plugins.flatMap(({ plugin }) => (plugin.header ? headerSecretVariants(plugin.header.value) : [])),
    release: () => {
      for (const { registration } of registrations) registration.release()
    },
  }
}
