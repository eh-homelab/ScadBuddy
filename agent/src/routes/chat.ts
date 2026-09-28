import type { Hono, MiddlewareHandler } from 'hono'
import type { UpgradeWebSocket, WSContext } from 'hono/ws'
import { ApprovalError } from '../approvals/service.js'
import type { OriginPolicy } from '../http/origins.js'
import { type ClientMessage, parseClientFrame, renderPageContext } from '../sessions/clientProtocol.js'
import { type SessionManager, SessionError } from '../sessions/manager.js'
import { event, type Owner, type ServerEvent } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'

// The assistant panel's socket (#256, #300): `GET /api/v1/ai/chat`, upgraded
// to a WebSocket that carries the panel protocol (frontend
// src/agent/chat/protocol.ts) both ways, one JSON object per text frame.
//
// It lives in the agent, under /api/v1/ai/*, because spec §4.2 routes that
// prefix to the agent at the ingress and keeps the backend's /api/v1/ws for
// the UI's domain events: "The agent's own streams (sessions, approvals, the
// browser bridge, #254) are served by the agent under /api/v1/ai/*".
//
//   on open                → sessions.snapshot (every session the browser user sees)
//   user.message           → SessionManager.start (no sessionId: a new `chat`
//                            session) or .send; the page context rides along
//                            for the model only (manager.ts SendOptions)
//   session.attach         → replay the session's event log from the start,
//                            then follow it live (SessionManager.attach)
//   approval.decision      → ApprovalService.decision (#258): the same decision
//                            as POST /api/v1/ai/approvals/:id/approve|deny
//   session.interrupt      → SessionManager.interrupt
//   session.handoff        → SessionManager.handoff to the browser user (take over)
//
// A refused operation comes back as an `error` event naming the session and
// the SessionError/ApprovalError code; the socket stays open.
//
// The upgrade passes guard.ts `uiRequestProblem` first: a browser always sends
// `Origin` on a WebSocket handshake, and it must be the UI's (spec §8.4: "An
// `Origin` check on /mcp, the agent's sockets and the backend's /api/v1/ws
// prevents DNS rebinding"). A connection that passes acts as the browser user,
// as the approval routes do (routes/approvals.ts BROWSER_USER). The browser
// never holds a Claude credential: every model call is the agent's own.

export const CHAT_PATH = '/api/v1/ai/chat'

/** Sessions one connection follows at once; the oldest is dropped past this. */
export const MAX_FOLLOWS = 16

export type ChatRouteDeps = {
  /** Undefined without a database (spec §9); the upgrade then answers 503. */
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  /** The runtime's WebSocket upgrade (`@hono/node-server`'s in main.ts). No socket route without it. */
  upgradeWebSocket: UpgradeWebSocket | undefined
  log?: (message: string) => void
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

function errorEvent(err: unknown, sessionId: string | undefined, log: (m: string) => void): ServerEvent {
  const where = sessionId ? { sessionId } : {}
  if (err instanceof SessionError || err instanceof ApprovalError) {
    return event({ type: 'error', ...where, code: err.code, message: err.message })
  }
  // Anything else is ours, not the user's: log it, and say only that it failed.
  log(`chat: ${err instanceof Error ? (err.stack ?? err.message) : String(err)}`)
  return event({ type: 'error', ...where, code: 'internal', message: 'the assistant service failed; see its log' })
}

/**
 * One panel connection, independent of the socket so it can be tested without
 * one. `out` receives every server event in order; `receive` takes each text
 * frame. Frames are handled one at a time, in arrival order.
 */
export class ChatConnection {
  private readonly sessions: SessionManager
  private readonly out: (e: ServerEvent) => void
  private readonly principal: Owner
  private readonly log: (message: string) => void
  /** Followed sessions, oldest first (Map keeps insertion order). */
  private readonly follows = new Map<string, AbortController>()
  private queue: Promise<void> = Promise.resolve()
  private closed = false

  constructor(
    sessions: SessionManager,
    out: (e: ServerEvent) => void,
    options: { principal?: Owner; log?: (message: string) => void } = {},
  ) {
    this.sessions = sessions
    this.out = out
    this.principal = options.principal ?? BROWSER_USER
    this.log = options.log ?? ((m) => console.error(m))
  }

  private emit(e: ServerEvent): void {
    if (!this.closed) this.out(e)
  }

  /** Sends the session picker's snapshot. */
  open(): Promise<void> {
    return this.enqueue(async () => {
      this.emit(await this.sessions.snapshot(this.principal))
    })
  }

  receive(raw: string): Promise<void> {
    const parsed = parseClientFrame(raw)
    if (!parsed.ok) {
      this.emit(event({ type: 'error', code: 'invalid', message: `ignored a malformed message: ${parsed.error}` }))
      return Promise.resolve()
    }
    const message = parsed.value
    return this.enqueue(() => this.handle(message))
  }

  close(): void {
    this.closed = true
    for (const controller of this.follows.values()) controller.abort()
    this.follows.clear()
  }

  /** Sessions this connection follows now (for tests). */
  following(): string[] {
    return [...this.follows.keys()]
  }

  private enqueue(task: () => Promise<void>): Promise<void> {
    const run = this.queue.then(async () => {
      if (this.closed) return
      try {
        await task()
      } catch (err) {
        this.emit(errorEvent(err, undefined, this.log))
      }
    })
    this.queue = run
    return run
  }

  private async handle(message: ClientMessage): Promise<void> {
    const sessionId = 'sessionId' in message ? message.sessionId : undefined
    try {
      switch (message.type) {
        case 'user.message': {
          const context = renderPageContext(message.context)
          if (!message.sessionId) {
            const { session } = await this.sessions.start(this.principal, {
              origin: 'chat',
              prompt: message.text,
              context,
            })
            // From the start: session.started is what the panel adopts its new chat by.
            this.follow(session.id, 0)
            return
          }
          if (!this.follows.has(message.sessionId)) {
            await this.sessions.get(message.sessionId, this.principal)
            this.follow(message.sessionId, await this.sessions.events.lastSeq(message.sessionId))
          }
          await this.sessions.send(message.sessionId, this.principal, message.text, { context })
          return
        }
        case 'session.attach':
          // The panel clears the session's feed when it attaches (state.ts
          // `select`), so this is always a full replay, even when followed already.
          await this.sessions.get(message.sessionId, this.principal)
          this.follow(message.sessionId, 0)
          return
        case 'approval.decision':
          await this.sessions.approvals.decision(this.principal, message)
          return
        case 'session.interrupt':
          await this.sessions.interrupt(message.sessionId, this.principal)
          return
        case 'session.handoff':
          await this.sessions.handoff(message.sessionId, this.principal, this.principal)
          return
      }
    } catch (err) {
      this.emit(errorEvent(err, sessionId, this.log))
    }
  }

  /** (Re)starts following a session's log after `afterSeq`. */
  private follow(id: string, afterSeq: number): void {
    // The socket may have closed while handle() awaited start/get: close()
    // has run already and will not run again, so nothing may be registered.
    if (this.closed) return
    this.follows.get(id)?.abort()
    this.follows.delete(id)
    while (this.follows.size >= MAX_FOLLOWS) {
      const [oldest, controller] = this.follows.entries().next().value as [string, AbortController]
      controller.abort()
      this.follows.delete(oldest)
    }
    const controller = new AbortController()
    this.follows.set(id, controller)
    void (async () => {
      try {
        const stream = await this.sessions.attach(id, this.principal, { afterSeq, signal: controller.signal })
        // Closed while attach() checked the session: stop before the first read,
        // so the log's follower is never registered.
        if (this.closed || controller.signal.aborted) {
          controller.abort()
          return
        }
        for await (const { event: e } of stream) {
          if (controller.signal.aborted) return
          this.emit(e)
        }
      } catch (err) {
        if (!controller.signal.aborted) this.emit(errorEvent(err, id, this.log))
      } finally {
        if (this.follows.get(id) === controller) this.follows.delete(id)
      }
    })()
  }
}

export function registerChatRoute(app: Hono, deps: ChatRouteDeps): void {
  const upgrade = deps.upgradeWebSocket
  if (!upgrade) return
  const log = deps.log ?? ((m: string) => console.error(m))

  const gate: MiddlewareHandler = async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'the assistant socket')
    if (problem) return c.json({ detail: problem }, 403)
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    if (c.req.header('upgrade')?.toLowerCase() !== 'websocket') {
      return c.json({ detail: 'this is a WebSocket endpoint' }, 426)
    }
    await next()
  }

  app.get(
    CHAT_PATH,
    gate,
    upgrade(() => {
      let connection: ChatConnection | undefined
      return {
        onOpen: (_evt: Event, ws: WSContext) => {
          const sessions = deps.sessions
          if (!sessions) return ws.close(1011, 'no database')
          connection = new ChatConnection(sessions, (e) => ws.send(JSON.stringify(e)), { log })
          void connection.open()
        },
        onMessage: (evt: MessageEvent, ws: WSContext) => {
          if (typeof evt.data !== 'string') {
            ws.send(JSON.stringify(event({ type: 'error', code: 'invalid', message: 'frames must be JSON text' })))
            return
          }
          void connection?.receive(evt.data)
        },
        onClose: () => connection?.close(),
        onError: () => connection?.close(),
      }
    }),
  )
}

/** A socket that can be pinged: `ws`'s WebSocket (the server's `clients`). */
type Pingable = { readyState: number; ping(): void; terminate(): void; on(event: 'pong', fn: () => void): unknown }

/**
 * Pings every open socket each `intervalMs` and drops one that did not answer
 * the previous ping. Ingress proxies close a WebSocket that stays silent past
 * their read timeout (60 s by default in ingress-nginx, `proxy-read-timeout`),
 * and a session can sit idle waiting for a human far longer; control-frame
 * pings keep it open without adding frames the panel would have to parse.
 * Returns a stop function.
 */
export function startHeartbeat(server: { clients: Set<Pingable> }, intervalMs = 25_000): () => void {
  const alive = new WeakMap<Pingable, boolean>()
  const timer = setInterval(() => {
    for (const socket of server.clients) {
      if (socket.readyState !== 1) continue
      if (alive.get(socket) === false) {
        socket.terminate()
        continue
      }
      if (!alive.has(socket)) socket.on('pong', () => alive.set(socket, true))
      alive.set(socket, false)
      socket.ping()
    }
  }, intervalMs)
  timer.unref()
  return () => clearInterval(timer)
}
