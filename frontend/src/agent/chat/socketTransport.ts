import { socketUrl } from '../../lib/lsp'
import { recheckAiAvailability } from './availability'
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
 * a fresh `sessions.snapshot`, and `onOpen` lets the hook re-attach the open session
 * before anything queued goes out (a queued message sent first would start a follow
 * that the attach's replay then repeats). Messages sent while disconnected wait for
 * the next connection, up to `MAX_QUEUED` each for control frames (approval
 * decisions, answers to the agent's questions, interrupts), which go ahead of the rest, and for everything else.
 *
 * A handshake the agent refuses (its origin and HTTPS gate, answered 403) reaches the
 * browser only as close code 1006, the same as an agent that is down or restarting
 * (the ingress answers 502/503). The transport cannot tell them apart, so a failed
 * handshake is only ever "reconnecting": after every `REFUSED_AFTER` failed
 * handshakes in a row with no connection between them, it asks the agent's status
 * (`recheckAiAvailability`), which reports the gate's verdict for this page, and
 * says the connection is refused (`REFUSED_MESSAGE`) only when that answer says so.
 * A passing outage keeps the message, the back-off and both queues, so a decision
 * queued during it still goes first when the agent is back. The agent pings at the
 * WebSocket level (control frames), so nothing extra arrives for the panel to parse.
 */

export const CHAT_SOCKET_PATH = '/api/v1/ai/chat'
/** Per queue: chat messages and attaches, and control frames, each hold this many. */
export const MAX_QUEUED = 50
/** Failed handshakes in a row before the agent's status is asked whether it refuses this page. */
export const REFUSED_AFTER = 3

export const REFUSED_MESSAGE =
  'The assistant keeps refusing the connection. It only accepts ScadBuddy opened at its public ' +
  'HTTPS address (not a LAN IP or plain http); open it there, or see Settings → Assistant. Retrying…'

/** Frames that decide or stop something: queued apart from, and sent before, the rest. */
const CONTROL: ReadonlySet<ClientMessage['type']> = new Set(['approval.decision', 'question.answer', 'session.interrupt'])

export interface SocketTransportOptions {
  url?: string
  WebSocketImpl?: typeof WebSocket
  /** Reconnect back-off: `baseMs · 2^attempt`, capped at `maxMs`. */
  baseMs?: number
  maxMs?: number
  /**
   * Asked every `REFUSED_AFTER` failed handshakes in a row whether the agent refuses
   * this page's connection; by default a re-read of the agent's status. True shows
   * `REFUSED_MESSAGE`; false (an outage, or nothing answering) keeps "reconnecting".
   */
  onRefused?: () => boolean | Promise<boolean>
}

export function createSocketTransport({
  url = socketUrl(CHAT_SOCKET_PATH),
  WebSocketImpl = WebSocket,
  baseMs = 500,
  maxMs = 15_000,
  onRefused = async () => (await recheckAiAvailability()).chat === 'refused',
}: SocketTransportOptions = {}): ChatTransport {
  let handlers: TransportHandlers | null = null
  let socket: WebSocket | undefined
  /** A connection is open now. */
  let live = false
  let closed = false
  let attempt = 0
  /** Handshakes that closed before opening, since the last connection that opened. */
  let failedHandshakes = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  const queue: string[] = []
  const control: string[] = []

  const open = () => {
    if (closed) return
    const ws = new WebSocketImpl(url)
    socket = ws
    let opened = false
    ws.onopen = () => {
      if (socket !== ws) return
      opened = true
      live = true
      attempt = 0
      failedHandshakes = 0
      // The hook re-attaches first (its sends go straight out: the socket is open).
      handlers?.onOpen?.()
      while (control.length && ws.readyState === ws.OPEN) ws.send(control.shift()!)
      while (queue.length && ws.readyState === ws.OPEN) ws.send(queue.shift()!)
    }
    ws.onmessage = (event: MessageEvent) => {
      if (socket === ws && typeof event.data === 'string') handlers?.onFrame(event.data)
    }
    ws.onclose = (event: CloseEvent) => {
      if (socket !== ws || closed) return
      socket = undefined
      live = false
      if (!opened) failedHandshakes += 1
      handlers?.onClose?.(
        event.code === 1001
          ? 'The assistant service is restarting; reconnecting…'
          : 'Lost the connection to the assistant; reconnecting…',
      )
      if (!opened && failedHandshakes % REFUSED_AFTER === 0) {
        // The answer lands later; by then a connection may be open, or the
        // transport closed, and then it says nothing.
        ;(async () => onRefused())()
          .then((refused) => {
            if (refused && !closed && !live) handlers?.onClose?.(REFUSED_MESSAGE)
          })
          .catch(() => {})
      }
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
