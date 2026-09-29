import { socketUrl } from '../lib/lsp'
import { parseAgentFrame, tabFrame, type PairingEntry, type TabFrame } from './linkProtocol'
import { TAB_ID } from './tabId'

/**
 * This tab's socket to the agent service (#254): the transport that lets a server-side
 * agent drive the tab through the bridge (`bridge.ts`), the piece PR #339 left for later.
 * It connects to `GET /api/v1/ai/bridge` (agent `src/routes/bridge.ts`) on the UI's own
 * origin, which the ingress routes to the agent (AI spec §4.2), and:
 *
 * - says which tab this is, where it is and which tools are live (`hello`), then again
 *   whenever the route or the live tools change (`state`), as the issue asks: "The tab
 *   reports its live handlers on each route change";
 * - runs each `call` through `bridge.call()`, the same path as WebMCP and the e2e hook,
 *   so the arguments are checked against the tool's schema, the newest mounted handler
 *   runs, the touched element is highlighted, and the answer is a typed result, never a
 *   throw. Confirmations that send, print or delete stay user-only (`dom.ts USER_ONLY`);
 * - carries the pairing prompt of AI spec §8.5: the agent pushes the requests waiting
 *   and this tab's pairings (`pairings`), and the user answers them here
 *   (`PairingPrompt.tsx`). Only the user can: the prompt is user-only, so an agent's own
 *   `click` and `fill` refuse it.
 *
 * It reconnects with back-off when the socket drops, with the same tab id, so a paired
 * agent reaches the tab again once it is back. Calls in flight when it drops are lost; the
 * agent answers them as "disconnected".
 */

export const BRIDGE_SOCKET_PATH = '/api/v1/ai/bridge'
/**
 * The largest `result` frame sent, in UTF-8 bytes (not string length: non-ASCII text takes
 * up to 3 bytes per UTF-16 unit). The agent's sockets take frames up to 256 KiB (agent
 * `src/main.ts`), and a larger one would close this socket; a result over this is answered
 * as a `failed` error instead, so the agent can ask for less.
 */
export const MAX_RESULT_BYTES = 200_000

/** What the link needs of the bridge (`AgentBridge` has it all). */
export interface LinkBridge {
  call(name: string, args?: unknown): Promise<unknown>
  liveNames(): readonly string[]
  currentRoute(): string
  subscribe(listener: () => void): () => void
}

export interface TabLinkState {
  connected: boolean
  /** Requests from agents that want to drive a tab, waiting for the user. */
  pending: PairingEntry[]
  /** Agents the user paired with this tab. */
  paired: PairingEntry[]
  /** The agent's answer to the last accept of each request, by request id. */
  results: Record<string, { ok: boolean; message: string }>
}

export interface TabLink {
  connect(): void
  close(): void
  getState(): TabLinkState
  subscribe(listener: () => void): () => void
  /** The user typed `code` for request `id`. False when there is no connection to send it on. */
  accept(id: string, code: string): boolean
  deny(id: string): boolean
  /** The user disconnected a paired agent. */
  end(id: string): boolean
}

export interface TabLinkOptions {
  bridge: LinkBridge
  tabId?: string
  url?: string
  WebSocketImpl?: typeof WebSocket
  /** Reconnect back-off: `baseMs · 2^attempt`, capped at `maxMs`. */
  baseMs?: number
  maxMs?: number
}

export function createTabLink({
  bridge,
  tabId = TAB_ID,
  url = socketUrl(BRIDGE_SOCKET_PATH),
  WebSocketImpl = WebSocket,
  baseMs = 500,
  maxMs = 15_000,
}: TabLinkOptions): TabLink {
  let socket: WebSocket | undefined
  let closed = false
  let attempt = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let state: TabLinkState = { connected: false, pending: [], paired: [], results: {} }
  const listeners = new Set<() => void>()
  /** The route and tools last reported, so an unchanged state is not sent again. */
  let reported = ''
  let unsubscribe: (() => void) | undefined

  const setState = (next: Partial<TabLinkState>) => {
    state = { ...state, ...next }
    for (const listener of listeners) listener()
  }

  const send = (frame: TabFrame): boolean => {
    if (!socket || socket.readyState !== socket.OPEN) return false
    socket.send(JSON.stringify(frame))
    return true
  }

  const where = () => ({ route: bridge.currentRoute(), live: [...bridge.liveNames()] })

  const report = () => {
    const now = where()
    const key = JSON.stringify(now)
    if (key === reported) return
    if (send(tabFrame({ type: 'state', ...now }))) reported = key
  }

  const run = async (id: string, tool: string, args: Record<string, unknown>) => {
    const outcome = await bridge.call(tool, args)
    let frame = JSON.stringify(tabFrame({ type: 'result', id, outcome }))
    const bytes = new TextEncoder().encode(frame).byteLength
    if (bytes > MAX_RESULT_BYTES) {
      frame = JSON.stringify(
        tabFrame({
          type: 'result',
          id,
          outcome: {
            ok: false,
            error: {
              code: 'failed',
              message:
                `The result of "${tool}" is ${bytes} bytes, over the ${MAX_RESULT_BYTES} this tab ` +
                'sends; ask for less (a snapshot of one dialog, a range of the source).',
            },
          },
        }),
      )
    }
    if (socket && socket.readyState === socket.OPEN) socket.send(frame)
  }

  const onFrame = (raw: unknown) => {
    const parsed = parseAgentFrame(raw)
    if (!parsed.ok) return
    const frame = parsed.value
    switch (frame.type) {
      case 'call':
        void run(frame.id, frame.tool, frame.args)
        return
      case 'pairings': {
        // Answers for requests that are gone are dropped with them.
        const live = new Set([...frame.pending, ...frame.paired].map((entry) => entry.id))
        const results = Object.fromEntries(Object.entries(state.results).filter(([id]) => live.has(id)))
        setState({ pending: frame.pending, paired: frame.paired, results })
        return
      }
      case 'pairing.result':
        setState({ results: { ...state.results, [frame.id]: { ok: frame.ok, message: frame.message } } })
        return
      case 'error':
        return
    }
  }

  const open = () => {
    if (closed) return
    const ws = new WebSocketImpl(url)
    socket = ws
    ws.onopen = () => {
      if (socket !== ws) return
      attempt = 0
      const now = where()
      reported = JSON.stringify(now)
      ws.send(JSON.stringify(tabFrame({ type: 'hello', tabId, ...now })))
      setState({ connected: true })
    }
    ws.onmessage = (event: MessageEvent) => {
      if (socket === ws && typeof event.data === 'string') onFrame(event.data)
    }
    ws.onclose = () => {
      if (socket !== ws || closed) return
      socket = undefined
      // What was pending may have been answered elsewhere; the next connection says again.
      setState({ connected: false, pending: [], paired: [] })
      const delay = Math.min(maxMs, baseMs * 2 ** attempt)
      attempt += 1
      timer = setTimeout(open, delay)
    }
  }

  return {
    connect() {
      unsubscribe = bridge.subscribe(() => queueMicrotask(report))
      open()
    },
    close() {
      closed = true
      clearTimeout(timer)
      unsubscribe?.()
      const ws = socket
      socket = undefined
      ws?.close()
      setState({ connected: false, pending: [], paired: [] })
    },
    getState: () => state,
    subscribe(listener) {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    accept: (id, code) => send(tabFrame({ type: 'pairing.accept', id, code })),
    deny: (id) => send(tabFrame({ type: 'pairing.deny', id })),
    end: (id) => send(tabFrame({ type: 'pairing.end', id })),
  }
}
