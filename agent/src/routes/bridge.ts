import type { Hono, MiddlewareHandler } from 'hono'
import type { UpgradeWebSocket, WSContext } from 'hono/ws'
import type { TabConnection, TabHub } from '../bridge/hub.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiRequestProblem } from './guard.js'

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

export const BRIDGE_PATH = '/api/v1/ai/bridge'

export type BridgeRouteDeps = {
  tabs: TabHub
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  upgradeWebSocket: UpgradeWebSocket | undefined
  log?: (message: string) => void
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
    upgrade(() => {
      let connection: TabConnection | undefined
      return {
        onOpen: (_evt: Event, ws: WSContext) => {
          connection = deps.tabs.open(
            (frame) => ws.send(JSON.stringify(frame)),
            // 4000: this tab connected again on another socket; the old one goes.
            () => ws.close(4000, 'the tab connected again'),
          )
        },
        onMessage: (evt: MessageEvent) => void connection?.receive(evt.data),
        onClose: () => connection?.close(),
        onError: (evt: Event) => {
          log(`bridge: socket error: ${evt.type}`)
          connection?.close()
        },
      }
    }),
  )
}
