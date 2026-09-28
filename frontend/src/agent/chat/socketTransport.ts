import { socketUrl } from '../../lib/lsp'
import type { ClientMessage } from './protocol'
import type { ChatTransport, SendResult, TransportHandlers } from './transport'

/**
 * The assistant's real transport: the agent's WebSocket `GET /api/v1/ai/chat` (agent
 * `src/routes/chat.ts`), on the UI's own origin. The ingress routes `/api/v1/ai/*` to
 * the agent sidecar (AI spec §4.2), and so do `pnpm dev` and `pnpm preview`
 * (`vite.config.ts`). Frames are the panel protocol, one JSON object each. They are
 * handed on as text, and `useAgentChat` parses them.
 *
 * The socket reconnects with back-off when it drops. Each new connection starts with
 * a fresh `sessions.snapshot`, and `onOpen` lets the hook re-attach the open session.
 * Messages sent while disconnected wait for the next connection, up to `MAX_QUEUED`
 * each for control frames (approval decisions, interrupts), which go first, and for
 * everything else. Past that `send` answers `refused` and the caller says so. The agent pings at the WebSocket level (control frames), so nothing
 * extra arrives for the panel to parse.
 */

export const CHAT_SOCKET_PATH = '/api/v1/ai/chat'
/** Per queue: chat messages and attaches, and control frames, each hold this many. */
export const MAX_QUEUED = 50

/** Frames that decide or stop something: queued apart from, and sent before, the rest. */
const CONTROL: ReadonlySet<ClientMessage['type']> = new Set(['approval.decision', 'session.interrupt'])

export interface SocketTransportOptions {
  url?: string
  WebSocketImpl?: typeof WebSocket
  /** Reconnect back-off: `baseMs · 2^attempt`, capped at `maxMs`. */
  baseMs?: number
  maxMs?: number
}

export function createSocketTransport({
  url = socketUrl(CHAT_SOCKET_PATH),
  WebSocketImpl = WebSocket,
  baseMs = 500,
  maxMs = 15_000,
}: SocketTransportOptions = {}): ChatTransport {
  let handlers: TransportHandlers | null = null
  let socket: WebSocket | undefined
  let closed = false
  let attempt = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  const queue: string[] = []
  const control: string[] = []

  const open = () => {
    if (closed) return
    const ws = new WebSocketImpl(url)
    socket = ws
    ws.onopen = () => {
      if (socket !== ws) return
      attempt = 0
      while (control.length && ws.readyState === ws.OPEN) ws.send(control.shift()!)
      while (queue.length && ws.readyState === ws.OPEN) ws.send(queue.shift()!)
      handlers?.onOpen?.()
    }
    ws.onmessage = (event: MessageEvent) => {
      if (socket === ws && typeof event.data === 'string') handlers?.onFrame(event.data)
    }
    ws.onclose = (event: CloseEvent) => {
      if (socket !== ws || closed) return
      socket = undefined
      handlers?.onClose?.(
        event.code === 1001
          ? 'The assistant service is restarting; reconnecting…'
          : 'Lost the connection to the assistant; reconnecting…',
      )
      const delay = Math.min(maxMs, baseMs * 2 ** attempt)
      attempt += 1
      timer = setTimeout(open, delay)
    }
  }

  return {
    connect(h) {
      handlers = h
      open()
    },
    send(message: ClientMessage): SendResult {
      const frame = JSON.stringify(message)
      if (socket && socket.readyState === socket.OPEN) {
        socket.send(frame)
        return 'sent'
      }
      // Decisions and interrupts have their own queue, sent first: a backlog of
      // chat text can never crowd out, or delay, the answer an outward call waits on.
      const target = CONTROL.has(message.type) ? control : queue
      if (target.length >= MAX_QUEUED) return 'refused'
      target.push(frame)
      return 'queued'
    },
    close() {
      closed = true
      handlers = null
      clearTimeout(timer)
      const ws = socket
      socket = undefined
      ws?.close()
    },
  }
}
