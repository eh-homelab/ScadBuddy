import type { Hono, MiddlewareHandler } from 'hono'
import { WebSocket } from 'ws'
import type { UpgradeWebSocket, WSContext } from 'hono/ws'
import { ApprovalError } from '../approvals/service.js'
import { QuestionError } from '../questions/service.js'
import type { TabHub } from '../bridge/hub.js'
import type { OriginPolicy } from '../http/origins.js'
import { type ClientMessage, parseClientFrame, renderPageContext } from '../sessions/clientProtocol.js'
import { type SessionManager, SessionError } from '../sessions/manager.js'
import { event, type Owner, type ServerEvent } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The assistant panel's socket (#256, #300): `GET /api/v1/ai/chat`, upgraded
// to a WebSocket that carries the panel protocol (frontend
// src/agent/chat/protocol.ts) both ways, one JSON object per text frame.
//
// It lives in the agent, under /api/v1/ai/*, because spec §4.2 routes that
// prefix to the agent at the ingress and keeps the backend's /api/v1/ws for
// the UI's domain events: "The agent's own streams (sessions, approvals, the
// browser bridge, #254) are served by the agent under /api/v1/ai/*".
//
//   on open                → sessions.snapshot (every session the browser user sees),
//                            then again whenever that list changes (read every
//                            SNAPSHOT_MS), so a session started elsewhere, over
//                            /mcp or on another tab or replica, shows up live
//   user.message           → SessionManager.start (no sessionId: a new `chat`
//                            session) or .send; the page context rides along
//                            for the model only (manager.ts SendOptions)
//   session.attach         → replay the session's event log from the start,
//                            then follow it live (SessionManager.attach)
//   question.answer        → QuestionService.answer (#940): the user's answer to
//                            the agent's AskUserQuestion
//   approval.decision      → ApprovalService.decision (#258): the same decision
//                            as POST /api/v1/ai/approvals/:id/approve|deny
//                          (The panel now answers both through POST
//                          /api/v1/ai/pending-input/{id}, #815; these two stay
//                          for a panel loaded before that.)
//   session.interrupt      → SessionManager.interrupt
//   session.handoff        → SessionManager.handoff to the browser user (take over)
//   tab.bind               → the tab this panel is in (#254): from then on each
//                            session it starts or sends to is paired with that
//                            tab, and one it attaches to when it has no
//                            connected tab yet, so the session's browser_*
//                            tools drive it (bridge/hub.ts `pairSession`,
//                            spec §8.5: "The browser user's own chat sessions
//                            pair with their tab automatically")
//
// A refused operation comes back as an `error` event naming the session and
// the SessionError/ApprovalError code; the socket stays open. A send refused
// because the budget is spent is preceded by a `session.budget` event (#790).
// Raising a budget and forking are HTTP routes (routes/sessions.ts), not
// socket messages.
//
// The upgrade passes guard.ts `uiRequestProblem` first: a browser always sends
// `Origin` on a WebSocket handshake, and it must be the UI's (spec §8.4: "An
// `Origin` check on /mcp, the agent's sockets and the backend's /api/v1/ws
// prevents DNS rebinding"). A connection that passes acts as the browser user,
// as the approval routes do (routes/approvals.ts BROWSER_USER). The browser
// never holds a Claude credential: every model call is the agent's own.

export const CHAT_PATH = '/api/v1/ai/chat'

/** How often an open connection re-reads the session list, sending it only when it changed. */
export const SNAPSHOT_MS = 5_000

/** Sessions one connection follows at once; the oldest is dropped past this. */
export const MAX_FOLLOWS = 16

/**
 * Backpressure on the way out. A follow sends its next event only while the
 * socket holds less than `SEND_HIGH_WATER` unsent bytes, so a slow reader
 * pauses the log reads instead of piling the replay into memory. Frames sent
 * outside a follow (the snapshot, errors) are not held back, so the buffer can
 * still grow: past `SEND_BUFFER_MAX`, or when a follow has waited `DRAIN_STALL_MS`
 * for the buffer to drain, the client is too slow and its connection is closed.
 */
export const SEND_HIGH_WATER = 1024 * 1024
export const SEND_BUFFER_MAX = 8 * 1024 * 1024
export const DRAIN_STALL_MS = 60_000
const DRAIN_POLL_MS = 20

/** Frames one connection may have waiting to be handled; past this a frame is refused with `busy`. */
export const MAX_QUEUED_FRAMES = 32

export type ChatLimits = {
  highWater: number
  bufferMax: number
  drainStallMs: number
  maxQueued: number
}

export type ChatConnectionOptions = {
  principal?: Owner
  log?: (message: string) => void
  /** Bytes sent but not yet written to the client (the socket's `bufferedAmount`). */
  buffered?: () => number
  /** Called once when the client cannot keep up; the route closes the socket. */
  overflow?: () => void
  /** Overrides for tests. */
  limits?: Partial<ChatLimits>
  /** SNAPSHOT_MS when omitted. */
  snapshotMs?: number
  /** The browser bridge's tabs (#254); without them `tab.bind` pairs nothing. */
  tabs?: Pick<TabHub, 'pairSession' | 'sessionHasTab'> | undefined
  /** The client's address, for the audit rows of what it does here (a question's answer, #1075). */
  clientIp?: string | undefined
}

export type ChatRouteDeps = {
  /** Undefined without a database (spec §9); the upgrade then answers 503. */
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  /** The runtime's WebSocket upgrade (`@hono/node-server`'s in main.ts). No socket route without it. */
  upgradeWebSocket: UpgradeWebSocket | undefined
  log?: (message: string) => void
  /** SNAPSHOT_MS when omitted. */
  snapshotMs?: number
  /** The browser bridge's tabs (#254, bridge/hub.ts). */
  tabs?: Pick<TabHub, 'pairSession' | 'sessionHasTab'> | undefined
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'

/**
 * The session and question a malformed `question.answer` frame names, when it
 * names both readably (#940): the panel re-opens that card only, and no other
 * malformed frame touches a sent answer.
 */
function refusedAnswer(raw: string): { sessionId: string; questionId: string } | Record<string, never> {
  try {
    const frame: unknown = JSON.parse(raw)
    if (typeof frame !== 'object' || frame === null) return {}
    const { type, sessionId, id } = frame as Record<string, unknown>
    const named = (v: unknown): v is string => typeof v === 'string' && v.length > 0 && v.length <= 100
    return type === 'question.answer' && named(sessionId) && named(id) ? { sessionId, questionId: id } : {}
  } catch {
    return {}
  }
}

function errorEvent(
  err: unknown,
  sessionId: string | undefined,
  log: (m: string) => void,
  questionId?: string,
): ServerEvent {
  // A failed answer names its question (#940), so the panel re-opens that card only.
  const where = { ...(sessionId ? { sessionId } : {}), ...(questionId ? { questionId } : {}) }
  if (err instanceof SessionError || err instanceof ApprovalError || err instanceof QuestionError) {
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
  private queued = 0
  private closed = false
  private readonly snapshotMs: number
  private snapshotTimer: NodeJS.Timeout | undefined
  /** A timer re-read of the session list is queued or running. */
  private snapshotPending = false
  /** The last session list sent, as JSON, so an unchanged list is not sent again. */
  private lastSnapshot = ''
  private readonly buffered: () => number
  private readonly overflow: () => void
  private readonly limits: ChatLimits
  private readonly tabs: Pick<TabHub, 'pairSession' | 'sessionHasTab'> | undefined
  /** The tab this panel is in, once it said (`tab.bind`). */
  private tabId: string | undefined
  private readonly clientIp: string | undefined

  constructor(sessions: SessionManager, out: (e: ServerEvent) => void, options: ChatConnectionOptions = {}) {
    this.sessions = sessions
    this.out = out
    this.principal = options.principal ?? BROWSER_USER
    this.log = options.log ?? ((m) => console.error(m))
    this.snapshotMs = options.snapshotMs ?? SNAPSHOT_MS
    this.buffered = options.buffered ?? (() => 0)
    this.overflow = options.overflow ?? (() => {})
    this.tabs = options.tabs
    this.clientIp = options.clientIp
    this.limits = {
      highWater: SEND_HIGH_WATER,
      bufferMax: SEND_BUFFER_MAX,
      drainStallMs: DRAIN_STALL_MS,
      maxQueued: MAX_QUEUED_FRAMES,
      ...options.limits,
    }
  }

  private emit(e: ServerEvent): void {
    if (this.closed) return
    this.out(e)
    if (this.buffered() > this.limits.bufferMax) this.giveUp('its send buffer passed the cap')
  }

  /** Closes a client that does not read what it is sent. */
  private giveUp(why: string): void {
    if (this.closed) return
    this.log(`chat: closing a connection that cannot keep up: ${why}`)
    this.close()
    this.overflow()
  }

  /**
   * Resolves once the socket's unsent bytes are under the high-water mark
   * (true), or false when the follow should stop: aborted, closed, or stalled
   * past `drainStallMs` (which closes the connection).
   */
  private async drained(signal: AbortSignal): Promise<boolean> {
    const since = Date.now()
    while (this.buffered() > this.limits.highWater) {
      if (this.closed || signal.aborted) return false
      if (Date.now() - since > this.limits.drainStallMs) {
        this.giveUp(`nothing drained for ${this.limits.drainStallMs} ms`)
        return false
      }
      await new Promise((r) => setTimeout(r, DRAIN_POLL_MS))
    }
    return !this.closed && !signal.aborted
  }

  /** Sends the session picker's snapshot, and keeps it current while open. */
  open(): Promise<void> {
    const first = this.enqueue(() => this.sendSnapshot(true))
    this.snapshotTimer = setInterval(() => {
      // Queued like a frame, so a list never overtakes the events before it, but
      // not counted against the frame cap, and never more than one at a time: a
      // slow database must not fill the queue with stale re-reads and turn the
      // panel's frames away as `busy`. A failed re-read is skipped quietly; the
      // next tick tries again.
      if (this.snapshotPending) return
      this.snapshotPending = true
      void this.enqueue(
        () =>
          this.sendSnapshot(false)
            .catch(() => {})
            .finally(() => {
              this.snapshotPending = false
            }),
        { counted: false },
      )
    }, this.snapshotMs)
    this.snapshotTimer.unref()
    return first
  }

  private async sendSnapshot(always: boolean): Promise<void> {
    const snapshot = await this.sessions.snapshot(this.principal)
    const key = JSON.stringify(snapshot)
    if (!always && key === this.lastSnapshot) return
    this.lastSnapshot = key
    this.emit(snapshot)
  }

  /**
   * Takes one inbound frame: the text of a text frame, or whatever else the
   * socket delivered (a binary frame's bytes), which is refused as `invalid`.
   */
  receive(raw: unknown): Promise<void> {
    // The cap is checked before the frame is parsed, so a flood of frames is
    // refused with `busy` whether or not they parse: a malformed frame costs the
    // process a parse and a reply, and that is the work the cap bounds.
    if (this.queued >= this.limits.maxQueued) {
      this.emit(
        event({
          type: 'error',
          code: 'busy',
          message: 'too many messages waiting on this connection; send it again later',
        }),
      )
      return Promise.resolve()
    }
    if (typeof raw !== 'string') {
      // A binary frame is answered through the queue too, so it counts against the cap.
      return this.enqueue(async () => {
        this.emit(event({ type: 'error', code: 'invalid', message: 'frames must be JSON text' }))
      })
    }
    const parsed = parseClientFrame(raw)
    if (!parsed.ok) {
      // Answered through the queue, so it counts against the cap like any other frame.
      const error = parsed.error
      const where = refusedAnswer(raw)
      return this.enqueue(async () => {
        this.emit(event({ type: 'error', ...where, code: 'invalid', message: `ignored a malformed message: ${error}` }))
      })
    }
    const message = parsed.value
    // New sessions are rate-limited per owner by the manager (sessions/manager.ts
    // MAX_NEW_SESSIONS), not per connection, so reconnecting does not reset it;
    // a refusal comes back as an `error` frame with code `rate_limited`.
    return this.enqueue(() => this.handle(message))
  }

  close(): void {
    this.closed = true
    clearInterval(this.snapshotTimer)
    for (const controller of this.follows.values()) controller.abort()
    this.follows.clear()
  }

  /** Sessions this connection follows now (for tests). */
  following(): string[] {
    return [...this.follows.keys()]
  }

  /** Runs `task` after everything queued before it. `counted` tasks (frames) take a `maxQueued` slot. */
  private enqueue(task: () => Promise<void>, { counted = true } = {}): Promise<void> {
    if (counted) this.queued += 1
    const run = this.queue.then(async () => {
      try {
        if (this.closed) return
        await task()
      } catch (err) {
        this.emit(errorEvent(err, undefined, this.log))
      } finally {
        if (counted) this.queued -= 1
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
            this.pairTab(session.id)
            // From the start: session.started is what the panel adopts its new chat by.
            this.follow(session.id, 0)
            return
          }
          // Ownership first, every time (get() refuses another owner's session):
          // pairing this tab must never outrun the check that send() repeats.
          await this.sessions.get(message.sessionId, this.principal)
          if (!this.follows.has(message.sessionId)) {
            this.follow(message.sessionId, await this.sessions.events.lastSeq(message.sessionId))
          }
          // Before the turn starts, so its first browser_* call already finds this tab.
          this.pairTab(message.sessionId)
          await this.sessions.send(message.sessionId, this.principal, message.text, { context })
          return
        }
        case 'session.attach':
          // The panel clears the session's feed when it attaches (state.ts
          // `select`), so this is always a full replay, even when followed already.
          await this.sessions.get(message.sessionId, this.principal)
          if (!this.tabs?.sessionHasTab(message.sessionId)) this.pairTab(message.sessionId)
          this.follow(message.sessionId, 0)
          return
        case 'approval.decision':
          await this.sessions.approvals.decision(this.principal, message)
          return
        case 'question.answer':
          await this.sessions.questions.answer(this.principal, message, { clientIp: this.clientIp })
          return
        case 'session.interrupt':
          await this.sessions.interrupt(message.sessionId, this.principal)
          return
        case 'session.handoff':
          await this.sessions.handoff(message.sessionId, this.principal, this.principal)
          return
        case 'tab.bind':
          this.tabId = message.tabId
          return
      }
    } catch (err) {
      // A spent budget comes with the numbers, so the panel's meter and its
      // "used its budget" state are right even for a session whose log has none.
      if (sessionId && err instanceof SessionError && err.budget) {
        this.emit(event({ type: 'session.budget', sessionId, ...err.budget }))
      }
      this.emit(errorEvent(err, sessionId, this.log, message.type === 'question.answer' ? message.id : undefined))
    }
  }

  /** Pairs `sessionId` with this panel's tab, once the panel has named it. */
  private pairTab(sessionId: string): void {
    if (this.tabId !== undefined) this.tabs?.pairSession(sessionId, this.tabId)
  }

  /**
   * (Re)starts following a session's log after `afterSeq`. A follow already
   * running for the session is aborted first, so a repeated attach replaces
   * the replay instead of adding one.
   */
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
          // Wait for the client to read what it has before sending more.
          if (!(await this.drained(controller.signal))) return
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
    upgrade((c) => {
      let connection: ChatConnection | undefined
      const clientIp = deps.remoteAddress(c)
      return {
        onOpen: (_evt: Event, ws: WSContext) => {
          const sessions = deps.sessions
          if (!sessions) return ws.close(1011, 'no database')
          const raw = ws.raw as { bufferedAmount?: number } | undefined
          connection = new ChatConnection(sessions, (e) => ws.send(JSON.stringify(e)), {
            log,
            clientIp,
            ...(deps.snapshotMs === undefined ? {} : { snapshotMs: deps.snapshotMs }),
            ...(deps.tabs ? { tabs: deps.tabs } : {}),
            buffered: () => raw?.bufferedAmount ?? 0,
            // 1013 Try Again Later: the client is not reading what it is sent.
            overflow: () => ws.close(1013, 'client too slow'),
          })
          void connection.open()
        },
        // Every frame, binary ones included, goes through the connection, so
        // the queue cap and its `busy` answer apply to all of them.
        onMessage: (evt: MessageEvent) => void connection?.receive(evt.data),
        onClose: () => connection?.close(),
        onError: (evt: Event) => {
          // A transport error is ours to know about, like every other failure here.
          log(`chat: socket error: ${socketError(evt)}`)
          connection?.close()
        },
      }
    }),
  )
}

/** What a socket's error event says: the `ErrorEvent`'s error or message, or just its type. */
function socketError(evt: Event): string {
  const { error, message } = evt as { error?: unknown; message?: unknown }
  if (error instanceof Error) return error.stack ?? error.message
  if (typeof message === 'string' && message !== '') return message
  return evt.type
}

/** `WebSocket.OPEN` (the WHATWG readyState, which `ws` uses too); `Pingable` stays structural for tests. */
const OPEN = WebSocket.OPEN

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
/** The chat socket's ping interval; must stay under the ingress read timeout (docs/ai/operating.md §1.1). */
export const HEARTBEAT_MS = 25_000

export function startHeartbeat(server: { clients: Set<Pingable> }, intervalMs = HEARTBEAT_MS): () => void {
  const alive = new WeakMap<Pingable, boolean>()
  const timer = setInterval(() => {
    for (const socket of server.clients) {
      if (socket.readyState !== OPEN) continue
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

declare module '../app.js' {
  interface AppDeps {
    /** How often the chat socket re-reads the session list (SNAPSHOT_MS when omitted). */
    chatSnapshotMs?: number
  }
}

/** The assistant's chat socket (#256, #300), when `upgradeWebSocket` is given. */
export const route: RouteModule = {
  register(app, deps) {
    registerChatRoute(app, {
      sessions: deps.sessions,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      upgradeWebSocket: deps.upgradeWebSocket,
      ...(deps.chatSnapshotMs === undefined ? {} : { snapshotMs: deps.chatSnapshotMs }),
      ...(deps.tabs ? { tabs: deps.tabs } : {}),
    })
  },
}
