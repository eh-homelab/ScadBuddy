import type { Hono, MiddlewareHandler } from 'hono'
import type { UpgradeWebSocket, WSContext } from 'hono/ws'
import { type BrowserSessions, requestSession } from '../auth/browserSession.js'
import type { TabConnection, TabHub } from '../bridge/hub.js'
import type { AuthMode } from '../config.js'
import type { OriginPolicy } from '../http/origins.js'
import { SIGNED_OUT } from './chat.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'
import type { RouteModule } from './module.js'

// The tab socket of the browser bridge (#254): `GET /api/v1/ai/bridge`,
// upgraded to a WebSocket that carries bridge/protocol.ts both ways. Every
// ScadBuddy tab opens one while the assistant is available
// (frontend/src/agent/link.ts), whether or not its chat panel is open, so an
// agent can reach it: a chat session through the tab it chats from, an MCP
// client through the tab the user paired it with (spec §8.5,
// bridge/pairings.ts).
//
// Under /api/v1/ai/*, beside the chat socket, because spec §4.2 routes that
// prefix to the agent: "The agent's own streams (sessions, approvals, the
// browser bridge, #254) are served by the agent under /api/v1/ai/*". The
// upgrade passes the same gate as the chat socket (routes/chat.ts): HTTPS
// through the trusted ingress, and an `Origin` on the UI's allowlist (spec
// §8.4). A socket that passes is the browser user's tab; what it can do is
// answer calls and accept or refuse pairings, which only the user can do in
// that tab.
//
// Opened under a signed-in session (#4975 phase 5), the socket lives only as long
// as the session (#5217): `auth.session_revoked` on the bus (every replica hears
// it) closes it with 4401 at once, and it re-reads the session every
// `SESSION_CHECK_MS` (plus browserSession.ts CACHE_MS), which catches a missed
// event or an expiry. Never with SCADBUDDY_AUTH_MODE=off.

export const BRIDGE_PATH = '/api/v1/ai/bridge'

/**
 * The largest frame a tab may send. The sockets share one server (main.ts), whose
 * `maxPayload` is the chat socket's cap (routes/chat.ts CHAT_FRAME_MAX, the same
 * 256 KiB since images are uploaded, #1941), checked here on its own so the two
 * caps can differ again; a tab's frames stay under this one (frontend link.ts
 * `MAX_RESULT_BYTES`), and a larger one closes the socket, 1009 Message Too Big.
 */
export const BRIDGE_FRAME_MAX = 256 * 1024

/** How often an open tab socket re-reads the session it was opened under (#5217). */
export const SESSION_CHECK_MS = 5_000

export type BridgeRouteDeps = {
  tabs: TabHub
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  upgradeWebSocket: UpgradeWebSocket | undefined
  log?: (message: string) => void
  /** The signed-in sessions (#4975 phase 5): a socket opened under one closes when it ends (#5217). */
  browserSessions?: BrowserSessions | undefined
  /** With `off`, a socket never re-checks its session, as before #5217. */
  authMode?: AuthMode | undefined
  /** SESSION_CHECK_MS when omitted. */
  sessionCheckMs?: number
}

export function registerBridgeRoute(app: Hono, deps: BridgeRouteDeps): void {
  const upgrade = deps.upgradeWebSocket
  if (!upgrade) return
  const log = deps.log ?? ((m: string) => console.error(m))

  const gate: MiddlewareHandler = async (c, next) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'the browser bridge socket')
    if (problem) return c.json({ detail: problem }, 403)
    if (c.req.header('upgrade')?.toLowerCase() !== 'websocket') {
      return c.json({ detail: 'this is a WebSocket endpoint' }, 426)
    }
    await next()
  }

  app.get(
    BRIDGE_PATH,
    gate,
    upgrade((c) => {
      let connection: TabConnection | undefined
      const sessions = deps.authMode !== undefined && deps.authMode !== 'off' ? deps.browserSessions : undefined
      const person = sessions ? requestSession(c) : undefined
      let unwatch: (() => void) | undefined
      let timer: NodeJS.Timeout | undefined
      let checking = false
      const stop = () => {
        unwatch?.()
        clearInterval(timer)
        connection?.close()
      }
      return {
        onOpen: (_evt: Event, ws: WSContext) => {
          connection = deps.tabs.open(
            (frame) => ws.send(JSON.stringify(frame)),
            // 4000: this tab connected again on another socket; the old one goes.
            () => ws.close(4000, 'the tab connected again'),
          )
          if (!sessions || !person) return
          const signOut = () => {
            if (connection?.closed) return
            stop()
            ws.close(SIGNED_OUT, 'signed out')
          }
          // A read that fails keeps the socket; the next tick reads again.
          const recheck = () => {
            if (checking) return
            checking = true
            void sessions
              .live(person.id, person.principal)
              .then((live) => {
                if (live === false) signOut()
              })
              .finally(() => {
                checking = false
              })
          }
          unwatch = sessions.watch(person.id, person.principal, { revoked: signOut, recheck })
          timer = setInterval(recheck, deps.sessionCheckMs ?? SESSION_CHECK_MS)
          timer.unref()
        },
        onMessage: (evt: MessageEvent, ws: WSContext) => {
          const data: unknown = evt.data
          const size =
            typeof data === 'string' ? Buffer.byteLength(data) : ((data as { byteLength?: number } | null)?.byteLength ?? 0)
          if (size > BRIDGE_FRAME_MAX) return ws.close(1009, 'frame too large')
          void connection?.receive(evt.data)
        },
        onClose: stop,
        onError: (evt: Event) => {
          log(`bridge: socket error: ${evt.type}`)
          stop()
        },
      }
    }),
  )
}

/** The browser bridge's tab socket (#254), when the app has the tabs. */
export const route: RouteModule = {
  register(app, deps) {
    if (!deps.tabs) return
    registerBridgeRoute(app, {
      tabs: deps.tabs,
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      upgradeWebSocket: deps.upgradeWebSocket,
      ...(deps.browserSessions ? { browserSessions: deps.browserSessions } : {}),
      ...(deps.authMode ? { authMode: deps.authMode } : {}),
    })
  },
}
