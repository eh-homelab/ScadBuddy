import { randomUUID } from 'node:crypto'
import type { IncomingHttpHeaders, IncomingMessage, OutgoingHttpHeaders } from 'node:http'
import { mkdir, open, readdir, readFile, rm, stat, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool } from '@anthropic-ai/claude-agent-sdk'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import { type AuditActor, type AuditSink, safeDetail } from '../audit/log.js'
import { assertHostAllowed, assertHttpUrl, type Resolver, systemResolver } from '../http/egress.js'
import { pinnedRequestOptions, requestFor } from '../http/pinned.js'
import { markUntrusted } from '../safety/untrusted.js'
import { AGENT_ACTOR_HEADER } from './headlessBrowser.js'
import type { RiskTier } from './permissions.js'

// The assistant's `http_request` tool (#827): "curl" for a harness that runs
// with `tools: []` (options.ts: no Bash, no WebFetch) and refuses plugins that
// start a process (plugins.ts). An in-process SDK MCP server, like the
// headless browser's grant tool (headlessGrants.ts), added to a session's
// turn when the `http_request_enabled` setting is not `false`
// (routes/httpRequest.ts; on by default).
//
// REACH IS OPEN, decided by the owner on 2026-09-30 (#827): the internet, the
// LAN, private ranges, `*.internal` names and plain `http:` to any host. There
// is no private-range block, on purpose; security for this tool will come
// from running it in a sandbox later. What egress.ts still refuses for every
// URL the agent itself fetches stays refused here: link-local addresses
// (169.254/16, fe80::/10) and the cloud metadata hosts, where a node's own
// cloud credentials live, and URLs carrying user:password.
//
// TIERS, by method (permissions.ts reads the call's input for this one tool):
//   GET, HEAD                  read     runs at once
//   POST, PUT, PATCH, DELETE   outward  parks for a human approval (#258),
//                                       bound to the exact input
// Anything the resolver does not recognise is `outward`.
//
// GUARDS
//   - Headers are ONLY what the model passed. Nothing from the agent's
//     environment is added (no proxy variables, no user agent, no cookies):
//     the request is built from the tool input alone, and Claude Code's
//     environment, which holds the Claude credential (run.ts credentialEnv),
//     never reaches this process's request code.
//   - Never the agent-actor header (AGENT_ACTOR_HEADER): a request that names
//     it is refused, as are the connection-level headers Node sets itself.
//   - No request carries the turn's secrets (the Claude credential and the
//     plugins' header tokens, the same list the event log is redacted of): a
//     header value, the URL or the body that contains one is refused, so the
//     model cannot be talked into sending its own credential anywhere.
//   - Every hop is checked (assertHttpUrl, assertHostAllowed) and connects to
//     exactly the address that was checked (pinned.ts), so a name re-pointed
//     between check and connect (DNS rebinding) reaches nothing new.
//   - Limits: at most MAX_REDIRECTS redirects; one deadline for the whole
//     request (timeout_ms, default 30 s, at most 120 s); the body is read up
//     to SAVE_MAX_BYTES (20 MiB) and returned inline up to INLINE_MAX_BYTES
//     (1 MiB). A longer body is saved in the session's directory and read in
//     pages with `http_response_read`.
//   - Redirects follow fetch's rules (303, and 301/302 after a POST, become a
//     GET with no body); credentials (Authorization, Cookie) are dropped when
//     a redirect leaves the origin, and a 307/308 that would re-send an
//     outward method to another origin is returned instead of followed: the
//     human approved one URL.
//   - Responses are untrusted data (safety/untrusted.ts): every result is
//     wrapped in the `untrusted_data` envelope naming the URL's host.
//   - Every request (every hop) is an `http` audit row: method, scheme, host,
//     status, size and timing. Never a path, a header or a body.

export const HTTP_SERVER = 'scadbuddy_http'
export const HTTP_TOOL = 'http_request'
export const HTTP_READ_TOOL = 'http_response_read'
export const HTTP_TOOL_NAME = `mcp__${HTTP_SERVER}__${HTTP_TOOL}`
export const HTTP_READ_TOOL_NAME = `mcp__${HTTP_SERVER}__${HTTP_READ_TOOL}`

/** `ai_settings` key; anything but a stored `false` is on (#827: on by default). */
export const SETTING_HTTP_REQUEST = 'http_request_enabled'

export function httpRequestEnabled(stored: unknown): boolean {
  return stored !== false
}

export const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'] as const
export type HttpMethod = (typeof METHODS)[number]
const READ_METHODS: ReadonlySet<string> = new Set(['GET', 'HEAD'])

export const INLINE_MAX_BYTES = 1024 * 1024
export const SAVE_MAX_BYTES = 20 * 1024 * 1024
export const DEFAULT_TIMEOUT_MS = 30_000
export const MAX_TIMEOUT_MS = 120_000
export const MAX_REDIRECTS = 5
/** Saved bodies kept per session; the oldest go first. */
export const MAX_SAVED = 10
/** Largest page `http_response_read` returns. */
export const PAGE_MAX_BYTES = INLINE_MAX_BYTES
const PAGE_DEFAULT_BYTES = 256 * 1024

/**
 * The tier of an http tool call by its input, or undefined for any other tool.
 * GET and HEAD (and no method, which the schema defaults to GET) are `read`;
 * anything else, including a method the schema would refuse, is `outward`.
 */
export function httpTierOf(toolName: string, input?: unknown): RiskTier | undefined {
  if (toolName === HTTP_READ_TOOL_NAME) return 'read'
  if (toolName !== HTTP_TOOL_NAME) return undefined
  const method = typeof input === 'object' && input !== null ? (input as { method?: unknown }).method : undefined
  if (method === undefined) return 'read'
  return typeof method === 'string' && READ_METHODS.has(method) ? 'read' : 'outward'
}

/** Header names the model may not set: Node sets them from the request, or they are ScadBuddy's own. */
const FORBIDDEN_HEADERS: ReadonlySet<string> = new Set([
  AGENT_ACTOR_HEADER.toLowerCase(),
  'host',
  'content-length',
  'transfer-encoding',
  'connection',
  'keep-alive',
  'proxy-connection',
  'upgrade',
  'te',
  'trailer',
])

/** Dropped when a redirect leaves the origin, as fetch does. */
const CREDENTIAL_HEADERS: ReadonlySet<string> = new Set(['authorization', 'cookie', 'proxy-authorization'])

export const HttpRequestInput = {
  method: z.enum(METHODS).optional().describe('HTTP method (default GET); GET and HEAD run at once, the others wait for the user to approve'),
  url: z.string().max(8192).describe('Absolute http:// or https:// URL; LAN hosts and plain http are allowed'),
  // `catchall`, not `z.record`: see `params` in tools/common.ts (the SDK's
  // bundled MCP server fails tools/list on a z.record field).
  headers: z
    .object({})
    .catchall(z.string().max(8192))
    .optional()
    .describe('Request headers, exactly as sent; nothing is added'),
  body: z.string().max(SAVE_MAX_BYTES).optional().describe('Request body as text (not for GET or HEAD)'),
  timeout_ms: z
    .number()
    .int()
    .min(1)
    .max(MAX_TIMEOUT_MS)
    .optional()
    .describe(`Deadline for the whole request, redirects included (default ${DEFAULT_TIMEOUT_MS}, at most ${MAX_TIMEOUT_MS})`),
}
// Defaults are applied here, not with zod's `.default()`: the MCP server
// bundled in the SDK validated tool input without applying them (measured on
// SDK 0.3.283: "expected nonoptional, received undefined"; 0.3.287 applies
// them, #1540).
export type HttpRequestInputArgs = z.infer<z.ZodObject<typeof HttpRequestInput>>
export type HttpRequestArgs = Omit<HttpRequestInputArgs, 'method' | 'timeout_ms'> & { method: HttpMethod; timeout_ms: number }

/** The input with its defaults. */
export function withDefaults(input: HttpRequestInputArgs): HttpRequestArgs {
  return { ...input, method: input.method ?? 'GET', timeout_ms: input.timeout_ms ?? DEFAULT_TIMEOUT_MS }
}

const SAVED_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export const HttpReadInput = {
  id: z
    .string()
    .regex(SAVED_ID, 'a saved response id')
    .describe('The `saved.id` an http_request result gave'),
  offset: z.number().int().min(0).optional().describe('Byte offset to start at (the previous page\'s next_offset)'),
  length: z
    .number()
    .int()
    .min(1)
    .max(PAGE_MAX_BYTES)
    .optional()
    .describe(`Bytes to read (default ${PAGE_DEFAULT_BYTES}, at most ${PAGE_MAX_BYTES})`),
}
export type HttpReadArgs = z.infer<z.ZodObject<typeof HttpReadInput>>

export type HttpLimits = {
  inlineMaxBytes: number
  saveMaxBytes: number
  maxRedirects: number
}

export const DEFAULT_LIMITS: HttpLimits = {
  inlineMaxBytes: INLINE_MAX_BYTES,
  saveMaxBytes: SAVE_MAX_BYTES,
  maxRedirects: MAX_REDIRECTS,
}

export type HttpRequestContext = {
  /** Where bodies too long to return inline are saved: the session's own directory. */
  saveDir: string
  /** The turn's secrets (credential, plugin tokens); no request may carry one. */
  secrets: () => readonly string[]
  /** The audit log; one `http` row per request. */
  audit?: AuditSink | undefined
  actor: AuditActor
  sessionId?: string | undefined
  turnId?: string | undefined
  /** Aborts every request in flight (the turn stopped). */
  signal?: AbortSignal | undefined
  resolve?: Resolver
  limits?: Partial<HttpLimits>
}

function contains(value: string, secrets: readonly string[]): boolean {
  // The same floor as secrets.ts `redact`: shorter strings are not secrets.
  return secrets.some((secret) => secret.length >= 4 && value.includes(secret))
}

/** Why this request must not be sent, or undefined. */
export function requestProblem(args: HttpRequestArgs, secrets: readonly string[]): string | undefined {
  for (const [name, value] of Object.entries(args.headers ?? {})) {
    const lower = name.toLowerCase()
    if (lower === AGENT_ACTOR_HEADER.toLowerCase()) return `the ${AGENT_ACTOR_HEADER} header is ScadBuddy's own and is never sent`
    if (FORBIDDEN_HEADERS.has(lower)) return `the ${name} header is set by the connection and cannot be given`
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name)) return `${JSON.stringify(name)} is not a valid header name`
    if (/[\r\n\0]/.test(value)) return `the ${name} header value contains a line break or NUL`
    if (contains(value, secrets)) return `the ${name} header contains the agent's own credential or a plugin token; it is never sent`
  }
  if (contains(args.url, secrets)) return "the URL contains the agent's own credential or a plugin token; it is never sent"
  if (args.body !== undefined && contains(args.body, secrets)) {
    return "the body contains the agent's own credential or a plugin token; it is never sent"
  }
  if (args.body !== undefined && READ_METHODS.has(args.method)) return `a ${args.method} request cannot have a body`
  return undefined
}

type Hop = {
  status: number
  statusText: string
  headers: IncomingHttpHeaders
  /** At most `saveMaxBytes`. */
  bytes: Buffer
  /** Whether the body ended before the cap. */
  complete: boolean
}

function textual(contentType: string | undefined, bytes: Buffer): boolean {
  if (contentType) {
    const type = contentType.split(';')[0]!.trim().toLowerCase()
    return (
      type.startsWith('text/') ||
      /[/+](json|xml|javascript|ecmascript|yaml|csv|x-www-form-urlencoded|graphql)$/.test(type) ||
      type === 'image/svg+xml' ||
      type === 'application/x-ndjson'
    )
  }
  return !bytes.subarray(0, 8192).includes(0)
}

/** `end` moved back so it does not split a UTF-8 sequence. */
function utf8Boundary(bytes: Buffer, end: number): number {
  if (end >= bytes.length) return bytes.length
  let cut = end
  // Back over continuation bytes (10xxxxxx) to the start of the sequence.
  while (cut > 0 && cut > end - 4 && (bytes[cut]! & 0xc0) === 0x80) cut--
  return cut
}

function combinedSignal(signals: (AbortSignal | undefined)[]): AbortSignal {
  return AbortSignal.any(signals.filter((s): s is AbortSignal => s !== undefined))
}

/** One request to the pinned address; the body read up to `maxBytes`. */
function sendOnce(
  target: URL,
  address: string,
  method: string,
  headers: OutgoingHttpHeaders,
  body: Buffer | undefined,
  maxBytes: number,
  signal: AbortSignal,
): Promise<Hop> {
  return new Promise((resolve, reject) => {
    const sent: OutgoingHttpHeaders = { ...headers, host: target.host }
    if (body) sent['content-length'] = body.length
    const req = requestFor(target)({
      ...pinnedRequestOptions(target, address),
      method,
      headers: sent,
      signal,
    })
    req.on('error', reject)
    req.on('response', (res: IncomingMessage) => {
      const chunks: Buffer[] = []
      let size = 0
      let done = false
      const finish = (complete: boolean) => {
        if (done) return
        done = true
        resolve({
          status: res.statusCode ?? 0,
          statusText: res.statusMessage ?? '',
          headers: res.headers,
          bytes: Buffer.concat(chunks),
          complete,
        })
      }
      res.on('data', (chunk: Buffer) => {
        if (done) return
        const room = maxBytes - size
        if (chunk.length > room) {
          if (room > 0) chunks.push(chunk.subarray(0, room))
          size = maxBytes
          finish(false)
          res.destroy()
          return
        }
        size += chunk.length
        chunks.push(chunk)
      })
      res.on('end', () => finish(true))
      res.on('error', (err) => (done ? undefined : reject(err)))
      res.on('aborted', () => (done ? undefined : reject(new Error('the response was cut off'))))
    })
    req.end(body)
  })
}

function isRedirect(status: number): boolean {
  return status === 301 || status === 302 || status === 303 || status === 307 || status === 308
}

type Outcome =
  | {
      ok: true
      url: URL
      redirects: string[]
      hop: Hop
      /** Set when a redirect was returned instead of followed. */
      note?: string
    }
  | { ok: false; reason: string }

/** Sends the request, following redirects within the limits; audits every hop. */
async function exchange(args: HttpRequestArgs, context: HttpRequestContext, callSignal?: AbortSignal): Promise<Outcome> {
  const limits = { ...DEFAULT_LIMITS, ...context.limits }
  const resolve = context.resolve ?? systemResolver
  const deadline = AbortSignal.timeout(args.timeout_ms)
  const signal = combinedSignal([deadline, context.signal, callSignal])
  let method: string = args.method
  let body = args.body === undefined ? undefined : Buffer.from(args.body, 'utf8')
  let headers: Record<string, string> = { ...args.headers }
  const redirects: string[] = []
  let url: URL
  try {
    url = assertHttpUrl(args.url, 'url')
  } catch (err) {
    return { ok: false, reason: (err as Error).message }
  }
  for (let hop = 0; ; hop++) {
    const startedAt = new Date()
    const audited = (outcome: 'ok' | 'error' | 'refused', extra: { status?: number; size?: number; detail?: string }) =>
      context.audit?.record({
        kind: 'http',
        action: method,
        surface: 'harness',
        actor: context.actor,
        sessionId: context.sessionId,
        turnId: context.turnId,
        tier: READ_METHODS.has(method) ? 'read' : 'outward',
        inputSummary: JSON.stringify({
          method,
          scheme: url.protocol.replace(/:$/, ''),
          host: url.host,
          ...(hop > 0 ? { redirect: hop } : {}),
          ...(extra.status !== undefined ? { status: extra.status } : {}),
          ...(extra.size !== undefined ? { size_bytes: extra.size } : {}),
        }),
        outcome,
        ...(extra.detail !== undefined ? { detail: safeDetail(extra.detail, context.secrets()) } : {}),
        startedAt,
        finishedAt: new Date(),
      })
    let addresses: string[]
    try {
      addresses = await assertHostAllowed(url.href, resolve, 'url', 'an allowed host')
    } catch (err) {
      const reason = (err as Error).message
      await audited('refused', { detail: reason })
      return { ok: false, reason }
    }
    let result: Hop
    try {
      result = await sendOnce(url, addresses[0]!, method, headers, body, limits.saveMaxBytes, signal)
    } catch (err) {
      const reason = deadline.aborted
        ? `no complete answer within ${args.timeout_ms} ms`
        : signal.aborted
          ? 'the request was stopped'
          : `the request failed: ${(err as Error).message}`
      await audited('error', { detail: reason })
      return { ok: false, reason }
    }
    await audited('ok', { status: result.status, size: result.bytes.length })
    const location = result.headers.location
    if (!isRedirect(result.status) || typeof location !== 'string') {
      return { ok: true, url, redirects, hop: result }
    }
    if (redirects.length >= limits.maxRedirects) {
      return { ok: false, reason: `more than ${limits.maxRedirects} redirects (last: ${url.href} → ${location})` }
    }
    let next: URL
    try {
      next = assertHttpUrl(new URL(location, url).href, 'redirect')
    } catch (err) {
      return { ok: false, reason: (err as Error).message }
    }
    const crossOrigin = next.origin !== url.origin
    const toGet = result.status === 303 ? method !== 'HEAD' : (result.status === 301 || result.status === 302) && method === 'POST'
    if (!toGet && crossOrigin && !READ_METHODS.has(method)) {
      return {
        ok: true,
        url,
        redirects,
        hop: result,
        note: `not followed: a ${result.status} would re-send the approved ${method} to another origin (${next.origin})`,
      }
    }
    if (toGet) {
      method = 'GET'
      body = undefined
      headers = Object.fromEntries(
        Object.entries(headers).filter(([k]) => !['content-type', 'content-encoding', 'content-language', 'content-location'].includes(k.toLowerCase())),
      )
    }
    if (crossOrigin) headers = Object.fromEntries(Object.entries(headers).filter(([k]) => !CREDENTIAL_HEADERS.has(k.toLowerCase())))
    redirects.push(next.href)
    url = next
  }
}

/** The ids of saved bodies are UUIDs; a file's name is `<id>.body`, its metadata `<id>.json`. */
type SavedMeta = { content_type: string | null; size_bytes: number; complete: boolean; url: string }

async function save(dir: string, bytes: Buffer, meta: SavedMeta): Promise<string> {
  await mkdir(dir, { recursive: true })
  const id = randomUUID()
  await writeFile(path.join(dir, `${id}.body`), bytes, { mode: 0o600 })
  await writeFile(path.join(dir, `${id}.json`), JSON.stringify(meta), { mode: 0o600 })
  await prune(dir)
  return id
}

/** Keeps the MAX_SAVED newest bodies. */
async function prune(dir: string): Promise<void> {
  const names = (await readdir(dir)).filter((n) => n.endsWith('.body'))
  if (names.length <= MAX_SAVED) return
  const dated = await Promise.all(names.map(async (n) => ({ n, t: (await stat(path.join(dir, n))).mtimeMs })))
  dated.sort((a, b) => a.t - b.t)
  for (const { n } of dated.slice(0, dated.length - MAX_SAVED)) {
    const id = n.slice(0, -'.body'.length)
    await rm(path.join(dir, `${id}.body`), { force: true })
    await rm(path.join(dir, `${id}.json`), { force: true })
  }
}

function text(value: unknown, isError = false): CallToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(value, null, 2) }], ...(isError ? { isError: true } : {}) }
}

function source(url: string): string {
  let host = url
  try {
    host = new URL(url).host
  } catch {
    // keep the raw text
  }
  return `an HTTP response from ${host}, written by whoever runs that server`
}

/** The `http_request` handler, on already-parsed input. The result is NOT yet marked untrusted. */
export async function runHttpRequest(
  args: HttpRequestArgs,
  context: HttpRequestContext,
  callSignal?: AbortSignal,
): Promise<CallToolResult> {
  const problem = requestProblem(args, context.secrets())
  if (problem) {
    let host = ''
    try {
      host = new URL(args.url).host
    } catch {
      // not a URL; the summary says so below
    }
    await context.audit?.record({
      kind: 'http',
      action: args.method,
      surface: 'harness',
      actor: context.actor,
      sessionId: context.sessionId,
      turnId: context.turnId,
      tier: READ_METHODS.has(args.method) ? 'read' : 'outward',
      inputSummary: JSON.stringify({ method: args.method, host }),
      outcome: 'refused',
      detail: safeDetail(problem, context.secrets()),
    })
    return text({ error: `Not sent: ${problem}.` }, true)
  }
  const limits = { ...DEFAULT_LIMITS, ...context.limits }
  const outcome = await exchange(args, context, callSignal)
  if (!outcome.ok) return text({ error: `Not completed: ${outcome.reason}.` }, true)
  const { hop } = outcome
  const contentType = typeof hop.headers['content-type'] === 'string' ? hop.headers['content-type'] : undefined
  const isText = textual(contentType, hop.bytes)
  const fits = hop.bytes.length <= limits.inlineMaxBytes && hop.complete
  // Saved when it does not fit inline, or is binary (not inlined at all), so it can be paged.
  const saved =
    hop.bytes.length === 0 || (fits && isText)
    ? undefined
    : await save(context.saveDir, hop.bytes, {
        content_type: contentType ?? null,
        size_bytes: hop.bytes.length,
        complete: hop.complete,
        url: outcome.url.href,
      })
  let inline: string | null = null
  if (isText) {
    const end = utf8Boundary(hop.bytes, Math.min(hop.bytes.length, limits.inlineMaxBytes))
    inline = hop.bytes.subarray(0, end).toString('utf8')
  }
  const result = {
    status: hop.status,
    status_text: hop.statusText,
    url: outcome.url.href,
    ...(outcome.redirects.length ? { redirects: outcome.redirects } : {}),
    ...(outcome.note ? { note: outcome.note } : {}),
    headers: hop.headers,
    size_bytes: hop.bytes.length,
    // False when the body was longer than the save cap and was cut there.
    body_complete: hop.complete,
    body: args.method === 'HEAD' ? null : inline,
    ...(inline === null && hop.bytes.length > 0 ? { body_omitted: 'binary content; read it from `saved` as base64' } : {}),
    inline_truncated: inline !== null && !fits,
    saved: saved
      ? {
          id: saved,
          size_bytes: hop.bytes.length,
          read_with: `${HTTP_READ_TOOL_NAME} (id, offset, length)`,
          ...(hop.complete ? {} : { note: `only the first ${limits.saveMaxBytes} bytes were kept` }),
        }
      : null,
  }
  return text(result)
}

/** The `http_response_read` handler: one page of a saved body. NOT yet marked untrusted. */
export async function readSavedResponse(input: HttpReadArgs, context: Pick<HttpRequestContext, 'saveDir'>): Promise<CallToolResult> {
  const args = { id: input.id, offset: input.offset ?? 0, length: input.length ?? PAGE_DEFAULT_BYTES }
  // The schema already refuses anything else; checked again so no name can leave the directory.
  if (!SAVED_ID.test(args.id)) return text({ error: 'not a saved response id' }, true)
  let meta: SavedMeta
  try {
    meta = JSON.parse(await readFile(path.join(context.saveDir, `${args.id}.json`), 'utf8')) as SavedMeta
  } catch {
    return text({ error: `no saved response ${args.id} in this session (only the ${MAX_SAVED} newest are kept)` }, true)
  }
  const file = await open(path.join(context.saveDir, `${args.id}.body`), 'r')
  try {
    const want = Math.min(args.length, PAGE_MAX_BYTES)
    // Read a few bytes more so a page can end on a whole UTF-8 sequence.
    const buffer = Buffer.alloc(want + 4)
    const { bytesRead } = await file.read(buffer, 0, want + 4, args.offset)
    const chunk = buffer.subarray(0, bytesRead)
    const isText = textual(meta.content_type ?? undefined, chunk)
    let start = 0
    let end = Math.min(bytesRead, want)
    if (isText) {
      // Skip a split sequence at the start (the previous page ended before it).
      while (start < end && args.offset + start > 0 && (chunk[start]! & 0xc0) === 0x80) start++
      end = utf8Boundary(chunk, end)
      if (end <= start) end = Math.min(bytesRead, want)
    }
    const page = chunk.subarray(start, end)
    const next = args.offset + end
    return text({
      id: args.id,
      url: meta.url,
      size_bytes: meta.size_bytes,
      offset: args.offset + start,
      next_offset: next < meta.size_bytes ? next : null,
      encoding: isText ? 'utf8' : 'base64',
      content: isText ? page.toString('utf8') : page.toString('base64'),
    })
  } finally {
    await file.close()
  }
}

function signalOf(extra: unknown): AbortSignal | undefined {
  const signal = typeof extra === 'object' && extra !== null ? (extra as { signal?: unknown }).signal : undefined
  return signal instanceof AbortSignal ? signal : undefined
}

/** The in-process MCP server a turn gets while the setting is on. */
export function httpRequestServer(context: HttpRequestContext): McpSdkServerConfigWithInstance {
  const request = tool(
    HTTP_TOOL,
    'Make one HTTP request, like curl, to any http or https URL: the internet or the local network. ' +
      'GET and HEAD run at once; POST, PUT, PATCH and DELETE wait for the user to approve that exact request. ' +
      'Only the headers you give are sent. Returns the status, the response headers and the body ' +
      `(up to ${INLINE_MAX_BYTES} bytes inline); a longer or binary body is saved and read with ${HTTP_READ_TOOL}. ` +
      `Follows up to ${MAX_REDIRECTS} redirects.`,
    HttpRequestInput,
    async (args, extra) => {
      const result = await runHttpRequest(withDefaults(args), context, signalOf(extra))
      return markUntrusted(result, HTTP_TOOL, source(args.url))
    },
    { annotations: { openWorldHint: true } },
  )
  const read = tool(
    HTTP_READ_TOOL,
    `Read one page of a response body that ${HTTP_TOOL} saved (its \`saved.id\`), from a byte offset. ` +
      'Text comes back as text, anything else as base64.',
    HttpReadInput,
    async (args) => markUntrusted(await readSavedResponse(args, context), HTTP_READ_TOOL, 'a saved HTTP response body'),
    { annotations: { readOnlyHint: true } },
  )
  return createSdkMcpServer({ name: HTTP_SERVER, tools: [request, read] })
}

