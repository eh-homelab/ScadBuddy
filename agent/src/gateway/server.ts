import {
  createServer,
  type IncomingHttpHeaders,
  type IncomingMessage,
  type OutgoingHttpHeaders,
  type Server,
  type ServerResponse,
} from 'node:http'
import type { AddressInfo } from 'node:net'
import type { Credential } from '../credentials.js'
import { assertHostAllowed, type Resolver, systemResolver } from '../http/egress.js'
import { pinnedRequestOptions, requestFor } from '../http/pinned.js'
import { SseBlocks } from '../plugins/forwarder.js'
import { costOf, type Usage } from '../sessions/unpricedSpend.js'
import { GatewayTokens, sameToken } from './tokens.js'

// The credential gateway (plan 2026-10-13 Step 9.4, decision Q6; spec
// 2026-10-13 §14.7 R4): a loopback proxy in the agent's own process, between
// every Claude Code process and the model endpoint.
//
// WHY. Claude Code's environment is inherited by every process it starts:
// native `Bash` and `PowerShell` (an allowed package's today, every session's
// once Step 9.6 lands), a hook, an MCP server. With the credential in that
// environment, any of them could read it and use it anywhere, outside the
// session's budget, for as long as it is valid. So Claude Code is never given
// the credential. Each run leases a random token here (`lease`), and Claude
// Code's environment carries only
//
//   ANTHROPIC_BASE_URL = http://127.0.0.1:<port>/t/<token>
//
// plus the token in the variable its credential kind is read from (run.ts
// `credentialEnv`), so Claude Code presents it in the header it would have
// presented the credential in, with whatever else it sends for that kind (an
// OAuth token's `oauth-2025-04-20` beta, which Claude Code 2.1.310 still sends
// to a custom base URL: measured, test/run.test.ts). Anthropic's gateway docs say both variables are
// how Claude Code is pointed at a gateway: "Set ANTHROPIC_BASE_URL to the
// gateway URL"; "`ANTHROPIC_AUTH_TOKEN` in `Authorization: Bearer`,
// `ANTHROPIC_API_KEY` in `x-api-key`"
// (https://code.claude.com/docs/en/llm-gateway-connect).
//
// For each request the gateway:
//
//   - refuses it (401) unless its path names a live token and its header
//     presents the same token;
//   - refuses it (403) unless it is one of `ENDPOINTS`, matched on the raw path,
//     so no other endpoint is reached with the credential and no spelling of a
//     priced one goes unmetered;
//   - drops the presented token and adds the credential's real header
//     (`x-api-key` for an API key, `Authorization: Bearer` for an OAuth token
//     or a gateway's token), sending it to https://api.anthropic.com or, for
//     kind `gateway`, to the credential's own base URL, whose host is checked
//     first as at save time (http/egress.ts) and then connected to at the
//     address the check passed (http/pinned.ts), as the plugin forwarder does;
//   - follows no redirect: a 30x is a 502, so the credential goes nowhere else;
//   - passes the response through as it streams, status and rate-limit headers
//     included (fallback.ts reads them through Claude Code's errors);
//   - prices each `POST /v1/messages` from the usage the response reports
//     (message_start and message_delta in a stream, `usage` in a JSON body; the
//     table of sessions/unpricedSpend.ts) and hands it to the lease's `onSpend`.
//     A request made by a `Bash` command with the token is counted like Claude
//     Code's own (sessions/turnHost.ts adds what Claude Code did not price).
//
// The token dies with the run (`release`), and the port is loopback only.

const ANTHROPIC_API = 'https://api.anthropic.com'

/**
 * The only requests the gateway sends on, matched against the raw path exactly, so a dot
 * segment, an empty segment or a percent-encoded dot or slash (which an endpoint might
 * normalise into another endpoint, unmetered) matches none of them. They are what an
 * Anthropic Messages gateway serves for Claude Code
 * (https://code.claude.com/docs/en/llm-gateway-protocol): inference, token counting and
 * model discovery. Only inference is priced: "Token counting is free to use"
 * (https://docs.claude.com/en/docs/build-with-claude/token-counting). Claude Code 2.1.310
 * called only `POST /v1/messages` in test/run.test.ts.
 */
const ENDPOINTS: readonly { method: string; path: string; priced: boolean }[] = [
  { method: 'POST', path: '/v1/messages', priced: true },
  { method: 'POST', path: '/v1/messages/count_tokens', priced: false },
  { method: 'GET', path: '/v1/models', priced: false },
]

/** A request body past this is refused (413): the Messages API's own request limit is 32 MB. */
export const MAX_REQUEST_BYTES = 64 * 1024 * 1024
/** A JSON response past this is passed through unpriced. */
const MAX_PRICED_JSON_BYTES = 16 * 1024 * 1024

/** Request headers never passed upstream: hop-by-hop, the presented token, and the framing set here. */
const DROPPED_REQUEST = new Set([
  'host',
  'connection',
  'keep-alive',
  'proxy-connection',
  'proxy-authorization',
  'transfer-encoding',
  'te',
  'trailer',
  'upgrade',
  'content-length',
  'authorization',
  'x-api-key',
  'accept-encoding',
])
/** Response headers never passed back: hop-by-hop, and the length, since the reply is passed on chunked. */
const DROPPED_RESPONSE = new Set(['connection', 'keep-alive', 'transfer-encoding', 'trailer', 'upgrade', 'content-length'])

export type GatewayOptions = {
  /** Where an API key's and an OAuth token's requests go; https://api.anthropic.com unless a test points it at its fake. */
  anthropicUrl?: string
  /** The resolver a gateway credential's host is checked with; its answer is the address connected to. */
  resolve?: Resolver
  /** A request body past this is refused (413); MAX_REQUEST_BYTES unless a test sets it lower. */
  maxRequestBytes?: number
}

export type LeaseOptions = {
  /** What each priced request cost, in USD, as its response ends. */
  onSpend?: (usd: number) => void
}

export type GatewayLease = {
  /** The base URL Claude Code is given: the gateway's, with the token as its path. */
  baseUrl: string
  token: string
  release(): void
}

type Lease = { credential: Credential; onSpend: ((usd: number) => void) | undefined }

function refuse(res: ServerResponse, status: number, type: string, message: string, headers: Record<string, string> = {}): void {
  if (res.headersSent) {
    res.destroy()
    return
  }
  res.writeHead(status, { 'content-type': 'application/json', ...headers })
  res.end(JSON.stringify({ type: 'error', error: { type, message } }))
}

/** The body, or `too large` once it passes `max`: reading stops there, and the rest is never buffered. */
function readBody(req: IncomingMessage, max: number): Promise<Buffer | 'too large'> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    const onData = (chunk: Buffer) => {
      size += chunk.length
      if (size > max) {
        req.off('data', onData)
        req.pause()
        resolve('too large')
        return
      }
      chunks.push(chunk)
    }
    req.on('data', onData)
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

/**
 * One upstream request. To `address` when given (a gateway credential's host, as the egress
 * check resolved it: never resolved again, TLS still checked against the name), else by name.
 */
function sendUpstream(
  target: URL,
  address: string | undefined,
  method: string,
  headers: OutgoingHttpHeaders,
  body: Buffer | undefined,
  signal: AbortSignal,
): Promise<IncomingMessage> {
  return new Promise((resolve, reject) => {
    const send = requestFor(target)
    const req =
      address === undefined
        ? send(target, { method, headers, signal }, resolve)
        : send({ ...pinnedRequestOptions(target, address), method, headers, signal }, resolve)
    req.on('error', reject)
    req.end(body)
  })
}

/** The token Claude Code presented: `x-api-key`, else a bearer `Authorization`. */
function presented(headers: IncomingHttpHeaders): string | undefined {
  const key = headers['x-api-key']
  if (typeof key === 'string' && key !== '') return key
  const auth = headers.authorization
  const match = typeof auth === 'string' ? /^Bearer\s+(.+)$/i.exec(auth) : null
  return match?.[1]?.trim()
}

/** The credential's own header, as the endpoint expects it. */
function credentialHeader(credential: Credential): Record<string, string> {
  return credential.kind === 'anthropic_api_key'
    ? { 'x-api-key': credential.secret }
    : { authorization: `Bearer ${credential.secret}` }
}

/** What one Messages response's usage says it cost, as the stream (or the JSON body) goes by. */
class Meter {
  #model: string | undefined
  readonly #usage: Usage = {}
  #seen = false
  /** The endpoint ended the stream with an `error` event: charged nowhere, as Claude Code charges it (unpricedSpend.ts). */
  #failed = false
  readonly #blocks = new SseBlocks()
  readonly #decoder = new TextDecoder()

  sse(chunk: Uint8Array): void {
    for (const block of this.#blocks.feed(this.#decoder.decode(chunk, { stream: true }))) {
      const data = block
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).replace(/^ /, ''))
      if (data.length === 0) continue
      let event: { type?: string; message?: { model?: string; usage?: Usage }; usage?: Usage }
      try {
        event = JSON.parse(data.join('\n')) as typeof event
      } catch {
        continue
      }
      if (event.type === 'message_start') {
        this.#model = event.message?.model
        this.#take(event.message?.usage)
      } else if (event.type === 'message_delta') this.#take(event.usage)
      else if (event.type === 'error') this.#failed = true
    }
  }

  json(body: string): void {
    try {
      const message = JSON.parse(body) as { model?: string; usage?: Usage }
      this.#model = message.model
      this.#take(message.usage)
    } catch {
      // Not a message: nothing to price.
    }
  }

  /** Counts are cumulative for the request (message_delta repeats or raises message_start's). */
  #take(usage: Usage | undefined): void {
    if (!usage) return
    for (const [k, v] of Object.entries(usage) as [keyof Usage, unknown][]) {
      if (typeof v === 'number' || (k === 'cache_creation' && typeof v === 'object' && v !== null)) {
        ;(this.#usage as Record<string, unknown>)[k] = v
      }
    }
    this.#seen = true
  }

  usd(): number {
    return this.#seen && !this.#failed ? costOf(this.#model, this.#usage) : 0
  }
}

export class CredentialGateway {
  readonly #server: Server
  readonly #port: number
  readonly #tokens = new GatewayTokens<Lease>()
  readonly #anthropicUrl: string
  readonly #resolve: Resolver
  readonly #maxRequestBytes: number

  private constructor(server: Server, options: GatewayOptions) {
    this.#server = server
    this.#port = (server.address() as AddressInfo).port
    this.#anthropicUrl = (options.anthropicUrl ?? ANTHROPIC_API).replace(/\/+$/, '')
    this.#resolve = options.resolve ?? systemResolver
    this.#maxRequestBytes = options.maxRequestBytes ?? MAX_REQUEST_BYTES
  }

  /** Listens on 127.0.0.1, on a port of the system's choosing. */
  static async start(options: GatewayOptions = {}): Promise<CredentialGateway> {
    const started: { gateway?: CredentialGateway } = {}
    const server = createServer((req, res) => {
      const gateway = started.gateway
      if (!gateway) return refuse(res, 503, 'api_error', 'the credential gateway is starting')
      gateway.#handle(req, res).catch(() => refuse(res, 502, 'api_error', 'the credential gateway failed'))
    })
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject)
      server.listen(0, '127.0.0.1', () => {
        server.off('error', reject)
        resolve()
      })
    })
    started.gateway = new CredentialGateway(server, options)
    return started.gateway
  }

  /** A token for one Claude Code process, mapped to `credential` until released. */
  lease(credential: Credential, options: LeaseOptions = {}): GatewayLease {
    const token = this.#tokens.issue({ credential, onSpend: options.onSpend })
    return {
      baseUrl: `http://127.0.0.1:${this.#port}/t/${token}`,
      token,
      release: () => this.#tokens.release(token),
    }
  }

  /** How many tokens are live: one per running process. */
  get leases(): number {
    return this.#tokens.size
  }

  async close(): Promise<void> {
    this.#server.closeAllConnections()
    await new Promise<void>((resolve) => this.#server.close(() => resolve()))
  }

  async #handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const match = /^\/t\/([A-Za-z0-9_-]+)(\/.*)?$/.exec(req.url ?? '')
    const token = match?.[1]
    const lease = token === undefined ? undefined : this.#tokens.get(token)
    const shown = presented(req.headers)
    if (!lease || token === undefined || shown === undefined || !sameToken(shown, token)) {
      req.resume()
      return refuse(res, 401, 'authentication_error', 'the credential gateway does not know this token')
    }
    const rest = match?.[2] ?? '/'
    const at = rest.indexOf('?')
    const path = at === -1 ? rest : rest.slice(0, at)
    const endpoint = ENDPOINTS.find((e) => e.method === req.method && e.path === path)
    const { credential } = lease
    const base = credential.kind === 'gateway' ? credential.baseUrl.replace(/\/+$/, '') : this.#anthropicUrl
    // The request goes to exactly that endpoint under the pinned base, and nowhere else.
    const pinned = new URL(base)
    const target = endpoint ? new URL(`${base}${rest}`) : undefined
    if (!target || target.origin !== pinned.origin || target.pathname !== `${pinned.pathname.replace(/\/+$/, '')}${path}`) {
      req.resume()
      return refuse(res, 403, 'permission_error', 'the credential gateway passes only the Messages, token counting and model list endpoints')
    }
    const body = req.method === 'GET' || req.method === 'HEAD' ? undefined : await readBody(req, this.#maxRequestBytes)
    if (body === 'too large') {
      // Answered before the connection goes, so the client reads the 413 rather than a reset.
      res.on('finish', () => req.destroy())
      return refuse(res, 413, 'request_too_large', 'the request is larger than the credential gateway passes', { connection: 'close' })
    }
    let address: string | undefined
    if (credential.kind === 'gateway') {
      try {
        // The connection goes to the address checked here, so a name re-pointed since (DNS
        // rebinding) cannot take the credential to a refused address.
        address = (await assertHostAllowed(base, this.#resolve, 'base_url', 'a model gateway'))[0]
      } catch (err) {
        return refuse(res, 502, 'api_error', err instanceof Error ? err.message : 'the gateway host is refused')
      }
    }
    const headers: OutgoingHttpHeaders = {}
    for (const [name, value] of Object.entries(req.headers)) {
      if (value === undefined || DROPPED_REQUEST.has(name)) continue
      headers[name] = Array.isArray(value) ? value.join(', ') : value
    }
    Object.assign(headers, credentialHeader(credential))
    // An unencoded reply, which the meter can read and is passed on as it is.
    headers['accept-encoding'] = 'identity'
    if (body !== undefined) headers['content-length'] = String(body.length)
    const abort = new AbortController()
    res.on('close', () => {
      if (!res.writableFinished) abort.abort()
    })
    let upstream: IncomingMessage
    try {
      upstream = await sendUpstream(target, address, req.method ?? 'GET', headers, body, abort.signal)
    } catch {
      return refuse(res, 502, 'api_error', 'the model endpoint could not be reached')
    }
    const status = upstream.statusCode ?? 502
    if (status >= 300 && status < 400) {
      upstream.destroy()
      return refuse(res, 502, 'api_error', `the model endpoint answered a redirect (HTTP ${status}), which the credential gateway does not follow`)
    }
    const out: OutgoingHttpHeaders = {}
    for (const [name, value] of Object.entries(upstream.headers)) {
      if (value !== undefined && !DROPPED_RESPONSE.has(name)) out[name] = value
    }
    res.writeHead(status, out)
    const priced = endpoint?.priced === true && status >= 200 && status < 300
    const type = String(upstream.headers['content-type'] ?? '')
    const meter = priced ? new Meter() : undefined
    const stream = meter !== undefined && type.startsWith('text/event-stream')
    const json: Buffer[] = []
    let jsonBytes = 0
    try {
      for await (const chunk of upstream as AsyncIterable<Buffer>) {
        if (stream) meter.sse(chunk)
        else if (meter && jsonBytes <= MAX_PRICED_JSON_BYTES) {
          json.push(chunk)
          jsonBytes += chunk.length
        }
        if (!res.write(chunk)) await new Promise<void>((resolve) => res.once('drain', resolve))
        if (res.destroyed) break
      }
      res.end()
    } catch {
      res.destroy()
    } finally {
      // Priced as far as it went: a stream Claude Code stopped still used its input.
      if (meter && !stream && jsonBytes <= MAX_PRICED_JSON_BYTES) meter.json(Buffer.concat(json).toString('utf8'))
      const usd = meter?.usd() ?? 0
      if (usd > 0) lease.onSpend?.(usd)
    }
  }
}

let shared: CredentialGateway | undefined
let starting: Promise<CredentialGateway> | undefined

/** Starts the process's gateway once (main.ts at start; test/support/gateway.ts per test file). */
export function startCredentialGateway(options: GatewayOptions = {}): Promise<CredentialGateway> {
  starting ??= CredentialGateway.start(options).then((gateway) => {
    shared = gateway
    return gateway
  })
  return starting
}

/** The process's gateway; every Claude Code process gets its credential through it (run.ts). */
export function credentialGateway(): CredentialGateway {
  if (!shared) throw new Error('the credential gateway is not started (gateway/server.ts startCredentialGateway)')
  return shared
}

export async function stopCredentialGateway(): Promise<void> {
  const gateway = shared
  shared = undefined
  starting = undefined
  await gateway?.close()
}
