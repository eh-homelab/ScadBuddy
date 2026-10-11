import { createHash, randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { z } from 'zod'
import { type Principal, TIERS } from '../auth/principal.js'

// /mcp sessions across agent replicas (#2086). A session is its transport, its
// McpServer, its resource subscriptions and its replay log: live objects in
// the process that opened it (mcp/http.ts), which no other replica can rebuild.
// So a request that reaches another replica is relayed to the owner, the way
// browser calls are (bridge/relay.ts, #1916):
//
//   any replica (got the request)            owner (holds the session)
//   look the session up in ai_mcp_sessions
//   INSERT request row + NOTIFY req   ──▶    DELETE … RETURNING the row
//                                     ◀──    NOTIFY ack, then a beat every BEAT_MS
//                                            runs it on the session's transport
//                                     ◀──    head (status, headers)
//                                     ◀──    chunk, chunk, … (the body as it is written)
//                                     ◀──    end
//   client went away: NOTIFY cancel   ──▶    stops reading the body
//
// The answer is streamed, not buffered, because it is often SSE: a tools/call
// that reports progress, and the standing GET stream that carries resource
// notifications. Wherever that GET lands, the owner's transport writes the
// notifications and they reach the client through the replica it reached.
//
// Messages are addressed to one replica (`to`), so nothing is claimed by a
// race: the directory names the owner. A chunk small enough rides in the
// NOTIFY itself; a larger one, and every request, is a row in
// ai_mcp_relay_messages, inserted in the transaction that NOTIFYs its id.
// Postgres delivers notifications in commit order and the owner sends one
// request's messages one at a time, so the receiver handles them in order.
//
// The owner went away: nobody acks within the ack timeout. The directory row
// is removed and the request answers 404, so the client starts a new session,
// as the transport spec requires ("When a client receives HTTP 404 in response
// to a request containing an MCP-Session-Id, it MUST start a new session",
// Session Management). Once acked, an owner that stops beating for
// SILENT_AFTER_BEATS beats is gone too: a request still waiting for its head
// answers 503, and a body already streaming is ended with an error, so the
// client reconnects and meets the 404.
//
// The channel is `scadbuddy_mcp`, LISTENed on the event listener's own
// connection (events/pgListener.ts `listenAlso`), as the bridge's is.
//
// Session ids are credentials (mcp/http.ts): the directory and every relay
// message carry the id's SHA-256, never the id.

export const MCP_CHANNEL = 'scadbuddy_mcp'
/** How long a relayed request waits for its owner to say it has it. */
export const ACK_TIMEOUT_MS = 3_000
/** How often an owner says it is still working on a relayed request. */
export const BEAT_MS = 10_000
/** Beats missed before the owner is taken for gone. */
const SILENT_AFTER_BEATS = 3
/**
 * NOTIFY payloads are capped at 8000 bytes; a chunk whose whole payload, as
 * serialised, is past this goes in a row. Measured on the payload, not the
 * text: JSON escapes the text again, and an SSE chunk of JSON is mostly quotes.
 */
const INLINE_MAX_BYTES = 7_500
/** Relay rows older than this are nobody's any more. */
const STALE_MESSAGES = '10 minutes'
const SWEEP_EVERY_MS = 60_000
/** Headers of an answer that describe its own hop's framing, not the answer. */
const FRAMING_HEADERS: ReadonlySet<string> = new Set(['content-length', 'transfer-encoding', 'connection', 'keep-alive'])
/** How often a session's `last_seen` is written, at most. */
const TOUCH_EVERY_MS = 30_000

/** The directory's key for a session id. */
export function sessionHash(sessionId: string): string {
  return createHash('sha256').update(sessionId, 'utf8').digest('hex')
}

/** A request as it is relayed: what the owner's transport reads of it. */
export type RelayedRequest = { method: string; url: string; headers: [string, string][]; body: string | null }

/** The owner's side: running a relayed request on a session it holds. */
export type SessionOwner = {
  run(hash: string, principal: Principal, request: Request, signal: AbortSignal): Promise<Response>
}

/** What mcp/http.ts relays through (McpEndpointDeps `relay`). */
export interface McpSessionRelay {
  /** Becomes the owner relayed requests are run by; once, by mountMcp. */
  serve(owner: SessionOwner): void
  /** Records that this replica holds the session. */
  register(hash: string, callerKey: string): Promise<void>
  /** Marks the session as used (written at most every TOUCH_EVERY_MS). */
  touch(hash: string): Promise<void>
  remove(hash: string): Promise<void>
  /** Open sessions across every replica, and the caller's, not idle for `idleMs`. */
  counts(callerKey: string, idleMs: number): Promise<{ total: number; caller: number }>
  /**
   * Runs `request` on the replica holding the session. Undefined when no
   * replica holds it, or its owner did not answer within the ack timeout (its
   * directory row is then removed). The caller answers both with 404.
   */
  forward(hash: string, principal: Principal, request: RelayedRequest, signal: AbortSignal): Promise<Response | undefined>
  close(): void
}

// The caller as authenticated, before it is tied to a session: an anonymous
// caller's per-session id (`anonymous:<session id>`) is made by the owner, so
// the id never travels. Credentials (Authorization, Cookie, Mcp-Session-Id) are
// stripped from the relayed request for the same reason (mcp/http.ts).
const PrincipalSchema = z.object({
  id: z.string(),
  kind: z.enum(['browser', 'bearer', 'oidc', 'anonymous', 'flow']),
  tiers: z.array(z.enum(TIERS)),
  clientIp: z.string().optional(),
  subject: z.string().optional(),
  clientId: z.string().optional(),
})
const RequestBodySchema = z.object({
  from: z.string(),
  hash: z.string(),
  principal: PrincipalSchema,
  request: z.object({
    method: z.string(),
    url: z.string(),
    headers: z.array(z.tuple([z.string(), z.string()])),
    body: z.string().nullable(),
  }),
})
type RequestBody = z.infer<typeof RequestBodySchema>
const HeadSchema = z.object({ status: z.number(), headers: z.array(z.tuple([z.string(), z.string()])) })
const ChunkRowSchema = z.object({ text: z.string() })
const HeaderSchema = z.discriminatedUnion('t', [
  z.object({ t: z.literal('req'), req: z.string(), to: z.string(), row: z.string() }),
  z.object({ t: z.literal('ack'), req: z.string(), to: z.string() }),
  z.object({ t: z.literal('beat'), req: z.string(), to: z.string() }),
  z.object({ t: z.literal('head'), req: z.string(), to: z.string(), status: z.number(), headers: z.array(z.tuple([z.string(), z.string()])) }),
  z.object({ t: z.literal('chunk'), req: z.string(), to: z.string(), text: z.string().optional(), row: z.string().optional() }),
  z.object({ t: z.literal('end'), req: z.string(), to: z.string(), error: z.boolean().optional() }),
  z.object({ t: z.literal('cancel'), req: z.string(), to: z.string() }),
])
type Header = z.infer<typeof HeaderSchema>

/** A request this replica forwarded and is waiting on. */
type Waiting = {
  /** The owner's messages for it, handled one at a time, in order. */
  chain: Promise<void>
  acked: () => void
  head: (status: number, headers: [string, string][]) => void
  chunk: (text: string) => void
  end: (error: boolean) => void
  heard: () => void
  /** This replica is closing: the request fails now. */
  shutdown: () => void
}

export type PgMcpSessionRelayOptions = {
  /** LISTEN on MCP_CHANNEL (events/pgListener.ts `listenAlso`). */
  listen: (channel: string, onNotify: (payload: string) => void) => void
  ackTimeoutMs?: number
  beatMs?: number
  log?: (message: string) => void
}

export class PgMcpSessionRelay implements McpSessionRelay {
  readonly #sql: Sql
  /** This replica's id: the directory names it, and answers are addressed to it. */
  readonly replica = randomUUID()
  readonly #ackTimeoutMs: number
  readonly #beatMs: number
  readonly #log: (message: string) => void
  readonly #waiting = new Map<string, Waiting>()
  /** Relayed requests this replica is running as owner, to cancel. */
  readonly #running = new Map<string, { controller: AbortController; heard: number }>()
  readonly #touched = new Map<string, number>()
  #owner: SessionOwner | undefined
  #lastSweep = 0
  #closed = false

  constructor(sql: Sql, options: PgMcpSessionRelayOptions) {
    this.#sql = sql
    this.#ackTimeoutMs = options.ackTimeoutMs ?? ACK_TIMEOUT_MS
    this.#beatMs = options.beatMs ?? BEAT_MS
    this.#log = options.log ?? ((m) => console.error(m))
    options.listen(MCP_CHANNEL, (payload) => void this.#heard(payload))
  }

  serve(owner: SessionOwner): void {
    this.#owner = owner
  }

  async register(hash: string, callerKey: string): Promise<void> {
    await this.#sql`
      INSERT INTO ai_mcp_sessions (id_hash, replica, caller_key)
      VALUES (${hash}, ${this.replica}, ${callerKey})
      ON CONFLICT (id_hash) DO UPDATE SET replica = EXCLUDED.replica, caller_key = EXCLUDED.caller_key, last_seen = now()`
    this.#touched.set(hash, Date.now())
  }

  async touch(hash: string): Promise<void> {
    const last = this.#touched.get(hash) ?? 0
    if (Date.now() - last < TOUCH_EVERY_MS) return
    this.#touched.set(hash, Date.now())
    await this.#sql`UPDATE ai_mcp_sessions SET last_seen = now() WHERE id_hash = ${hash}`.catch((err: unknown) =>
      this.#failed(err),
    )
  }

  async remove(hash: string): Promise<void> {
    this.#touched.delete(hash)
    await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash}`.catch((err: unknown) => this.#failed(err))
  }

  async counts(callerKey: string, idleMs: number): Promise<{ total: number; caller: number }> {
    await this.#sweep(idleMs)
    const [row] = await this.#sql<{ total: number; caller: number }[]>`
      SELECT count(*)::int AS total, (count(*) FILTER (WHERE caller_key = ${callerKey}))::int AS caller
      FROM ai_mcp_sessions WHERE last_seen > now() - make_interval(secs => ${idleMs / 1000})`
    return row ?? { total: 0, caller: 0 }
  }

  async forward(
    hash: string,
    principal: Principal,
    request: RelayedRequest,
    signal: AbortSignal,
  ): Promise<Response | undefined> {
    const [found] = await this.#sql<{ replica: string }[]>`SELECT replica FROM ai_mcp_sessions WHERE id_hash = ${hash}`
    if (!found) return undefined
    const owner = found.replica
    if (owner === this.replica) {
      // Ours by the directory but not in memory: left by a session this
      // process lost (it cannot be another process's: the id is per process).
      await this.remove(hash)
      return undefined
    }
    const req = randomUUID()
    const row = randomUUID()
    const encoder = new TextEncoder()
    return new Promise<Response | undefined>((resolve, reject) => {
      let settled = false
      let acked = false
      let stream: ReadableStreamDefaultController<Uint8Array> | undefined
      let lastHeard = Date.now()
      let ackTimer: NodeJS.Timeout | undefined
      let liveness: NodeJS.Timeout | undefined
      // Liveness both ways: the owner stops a request whose relaying replica
      // went silent (crashed, OOM-killed, lost its node), which would never send
      // a cancel, so a relayed GET stream is not held on a dead replica's behalf.
      const beating = setInterval(
        () => void this.#send({ t: 'beat', req, to: owner }).catch((err: unknown) => this.#failed(err)),
        this.#beatMs,
      )
      const done = () => {
        clearTimeout(ackTimer)
        clearInterval(liveness)
        clearInterval(beating)
        signal.removeEventListener('abort', onAbort)
        this.#waiting.delete(req)
      }
      const settle = (value: Response | undefined) => {
        if (settled) return
        settled = true
        resolve(value)
      }
      const cancelOwner = () => void this.#send({ t: 'cancel', req, to: owner }).catch((err: unknown) => this.#failed(err))
      const onAbort = () => {
        done()
        cancelOwner()
        if (stream) stream.error(abortError())
        if (!settled) {
          settled = true
          reject(abortError())
        }
      }
      const ownerGone = async () => {
        done()
        // Forgotten first, so the client's reconnect meets the 404.
        await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} AND replica = ${owner}`.catch((err: unknown) =>
          this.#failed(err),
        )
        if (!settled) {
          settle(
            Response.json(
              { jsonrpc: '2.0', error: { code: -32000, message: 'The agent replica holding this MCP session stopped answering' }, id: null },
              { status: 503 },
            ),
          )
        } else stream?.error(new Error('the agent replica holding this MCP session stopped answering'))
      }
      const unclaimed = async () => {
        try {
          // Withdrawn only if still there: an owner that took it is running it,
          // and its ack was lost or is late. Then keep waiting, on its beats.
          const withdrawn = await this.#sql`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING id`
          if (withdrawn.length === 0) return this.#waiting.get(req)?.acked()
          // Nobody took it: nobody holds the session.
          done()
          await this.#sql`DELETE FROM ai_mcp_sessions WHERE id_hash = ${hash} AND replica = ${owner}`
        } catch (err) {
          this.#failed(err)
          done()
        }
        settle(undefined)
      }
      signal.addEventListener('abort', onAbort, { once: true })
      this.#waiting.set(req, {
        chain: Promise.resolve(),
        heard: () => {
          lastHeard = Date.now()
        },
        shutdown: () => {
          done()
          if (!settled) {
            settle(
              Response.json(
                { jsonrpc: '2.0', error: { code: -32000, message: 'This agent replica is shutting down; try again' }, id: null },
                { status: 503 },
              ),
            )
          } else stream?.error(new Error('this agent replica is shutting down'))
        },
        acked: () => {
          clearTimeout(ackTimer)
          if (acked) return
          acked = true
          lastHeard = Date.now()
          liveness = setInterval(() => {
            if (Date.now() - lastHeard > this.#beatMs * SILENT_AFTER_BEATS) void ownerGone()
          }, this.#beatMs)
        },
        head: (status, headers) => {
          const body = new ReadableStream<Uint8Array>({
            start: (controller) => {
              stream = controller
            },
            cancel: () => {
              done()
              cancelOwner()
            },
          })
          settle(new Response(nullBodyStatus(status) ? null : body, { status, headers }))
        },
        chunk: (text) => stream?.enqueue(encoder.encode(text)),
        end: (error) => {
          done()
          if (error) stream?.error(new Error('the MCP session owner failed while answering'))
          else stream?.close()
          settle(undefined)
        },
      })
      const value: RequestBody = { from: this.replica, hash, principal: PrincipalSchema.parse(principal), request }
      this.#send({ t: 'req', req, to: owner, row }, { row, value }).then(
        // From the commit, so a slow pool does not eat the owner's time to ack;
        // and not at all once the ack was heard, which can come first.
        () => {
          if (this.#waiting.has(req) && !acked) {
            ackTimer = setTimeout(() => void unclaimed(), this.#ackTimeoutMs)
          }
        },
        (err: unknown) => {
          done()
          if (!settled) {
            settled = true
            reject(err)
          }
        },
      )
    })
  }

  close(): void {
    this.#closed = true
    for (const { controller } of this.#running.values()) controller.abort()
    this.#running.clear()
    // Requests this replica relayed: their clients learn now, not after the owner's silence.
    for (const waiting of [...this.#waiting.values()]) waiting.shutdown()
  }

  /** Inserts `body` (when given) as row `row` and NOTIFYs `header`, in one transaction. */
  async #send(header: Header, body?: { row: string; value: unknown }): Promise<void> {
    await this.#sql.begin(async (tx) => {
      if (body) await tx`INSERT INTO ai_mcp_relay_messages (id, body) VALUES (${body.row}, ${tx.json(body.value as never)})`
      await tx`SELECT pg_notify(${MCP_CHANNEL}, ${JSON.stringify(header)})`
    })
  }

  async #sweep(idleMs: number): Promise<void> {
    if (Date.now() - this.#lastSweep < SWEEP_EVERY_MS) return
    this.#lastSweep = Date.now()
    await Promise.all([
      this.#sql`DELETE FROM ai_mcp_relay_messages WHERE created_at < now() - ${STALE_MESSAGES}::interval`,
      // A dead replica's sessions: idle by now, since nothing could use them.
      this.#sql`DELETE FROM ai_mcp_sessions WHERE last_seen < now() - make_interval(secs => ${idleMs / 1000})`,
    ]).catch((err: unknown) => this.#failed(err))
  }

  async #take(row: string): Promise<unknown> {
    const [taken] = await this.#sql<{ body: unknown }[]>`DELETE FROM ai_mcp_relay_messages WHERE id = ${row} RETURNING body`
    return taken?.body
  }

  async #heard(payload: string): Promise<void> {
    let header: Header
    try {
      header = HeaderSchema.parse(JSON.parse(payload))
    } catch {
      return this.#log(`mcp relay: ignored a malformed ${MCP_CHANNEL} payload (${payload.length} bytes)`)
    }
    if (header.to !== this.replica) return
    if (header.t === 'req') return void this.#serveRequest(header)
    if (header.t === 'cancel') return void this.#running.get(header.req)?.controller.abort()
    const running = this.#running.get(header.req)
    if (running) {
      // As the owner: the relaying replica is still there, and still wants the answer.
      if (header.t === 'beat') running.heard = Date.now()
      return
    }
    const waiting = this.#waiting.get(header.req)
    if (!waiting) {
      // Nobody waits any more: a row addressed here is still ours to remove.
      if (header.t === 'chunk' && header.row) await this.#take(header.row).catch((err: unknown) => this.#failed(err))
      return
    }
    waiting.heard()
    waiting.chain = waiting.chain.then(async () => {
      try {
        switch (header.t) {
          case 'ack':
            return waiting.acked()
          case 'beat':
            return
          case 'head':
            return waiting.head(header.status, header.headers)
          case 'chunk': {
            const text = header.row ? ChunkRowSchema.parse(await this.#take(header.row)).text : (header.text ?? '')
            return waiting.chunk(text)
          }
          case 'end':
            return waiting.end(header.error === true)
        }
      } catch (err) {
        this.#failed(err)
        waiting.end(true)
      }
    })
  }

  /** As the owner: runs a request another replica relayed, and streams its answer back. */
  async #serveRequest(header: Extract<Header, { t: 'req' }>): Promise<void> {
    const owner = this.#owner
    if (!owner || this.#closed) return
    let body: RequestBody
    try {
      const taken = await this.#take(header.row)
      if (taken === undefined) return // the caller gave up before we got here
      const parsed = RequestBodySchema.safeParse(taken)
      if (!parsed.success) return this.#log('mcp relay: ignored a malformed request row')
      body = parsed.data
    } catch (err) {
      return this.#failed(err)
    }
    const to = body.from
    const req = header.req
    const controller = new AbortController()
    const running = { controller, heard: Date.now() }
    this.#running.set(req, running)
    // One message at a time, so they commit, and so are heard, in order.
    let queue = Promise.resolve()
    /** A message could not be sent: the answer is incomplete, and ends as an error. */
    let lost = false
    const send = (h: Header, row?: { row: string; value: unknown }) => {
      // Closed: this replica is going away, and says nothing more.
      if (this.#closed) return queue
      queue = queue.then(() => this.#send(h, row)).catch((err: unknown) => {
        this.#failed(err)
        lost = true
        controller.abort()
      })
      return queue
    }
    const beat = setInterval(() => {
      // The relaying replica has gone silent: nobody will read the answer, and a
      // GET stream held for it would refuse the client's reconnect (409).
      if (Date.now() - running.heard > this.#beatMs * SILENT_AFTER_BEATS) {
        controller.abort()
        return
      }
      void send({ t: 'beat', req, to })
      // A long relayed GET stream is use too: keep the directory row from the sweep.
      void this.touch(body.hash)
    }, this.#beatMs)
    let headSent = false
    try {
      await send({ t: 'ack', req, to })
      const r = body.request
      const request = new Request(r.url, {
        method: r.method,
        headers: r.headers,
        ...(r.body !== null ? { body: r.body } : {}),
        signal: controller.signal,
      })
      const response = await owner.run(body.hash, body.principal as Principal, request, controller.signal)
      // Framing is the receiving replica's: it re-streams the body, so a length
      // or encoding set for this hop would not describe that one.
      const headers = [...response.headers.entries()].filter(([name]) => !FRAMING_HEADERS.has(name.toLowerCase()))
      const head = HeadSchema.parse({ status: response.status, headers })
      await send({ t: 'head', req, to, ...head })
      headSent = true
      if (response.body) {
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        const cancel = () => void reader.cancel().catch(() => {})
        controller.signal.addEventListener('abort', cancel, { once: true })
        try {
          for (;;) {
            const { done, value } = await reader.read()
            const text = done ? decoder.decode() : decoder.decode(value, { stream: true })
            if (text) {
              const inline: Header = { t: 'chunk', req, to, text }
              if (Buffer.byteLength(JSON.stringify(inline), 'utf8') <= INLINE_MAX_BYTES) await send(inline)
              else {
                const row = randomUUID()
                await send({ t: 'chunk', req, to, row }, { row, value: { text } })
              }
            }
            if (done || controller.signal.aborted) break
          }
        } finally {
          controller.signal.removeEventListener('abort', cancel)
        }
      }
      await send(lost ? { t: 'end', req, to, error: true } : { t: 'end', req, to })
    } catch (err) {
      if (!controller.signal.aborted) this.#failed(err)
      if (!headSent) {
        await send({
          t: 'head',
          req,
          to,
          status: 500,
          headers: [['content-type', 'application/json']],
        })
        await send({ t: 'chunk', req, to, text: JSON.stringify({ jsonrpc: '2.0', error: { code: -32603, message: 'Internal error' }, id: null }) })
        await send({ t: 'end', req, to })
      } else await send({ t: 'end', req, to, error: true })
    } finally {
      clearInterval(beat)
      this.#running.delete(req)
    }
  }

  #failed(err: unknown): void {
    this.#log(`mcp relay: ${err instanceof Error ? err.message : String(err)}`)
  }
}

/** Statuses whose Response may not carry a body. */
function nullBodyStatus(status: number): boolean {
  return status === 101 || status === 204 || status === 205 || status === 304
}

function abortError(): Error {
  const err = new Error('the request was cancelled')
  err.name = 'AbortError'
  return err
}
