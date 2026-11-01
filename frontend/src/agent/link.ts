import { socketUrl } from '../lib/lsp'
import { parseAgentFrame, tabFrame, type PairingEntry, type TabFrame } from './linkProtocol'
import { TAB_ID } from './tabId'
import type { CallResult } from './types'

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
 * up to 3 bytes per UTF-16 unit). The agent takes a tab's frames up to 256 KiB (agent
 * `src/routes/bridge.ts` `BRIDGE_FRAME_MAX`), and a larger one would close this socket; a
 * result over this is answered as a `failed` error instead, so the agent can ask for less.
 */
export const MAX_RESULT_BYTES = 200_000
/**
 * The bytes of one binary frame of an image (agent `src/bridge/protocol.ts`
 * `IMAGE_CHUNK_MAX`). An answer holding an image (`screenshot`'s `{ image: Blob }`) is
 * sent as an `image` frame with its type and size, its bytes in binary frames of at most
 * this, and then the `result`, whose `image` is `{ mediaType }`: an image never goes as
 * base64 in JSON (#2479). The agent names it by the bytes' sha256, so the tab needs no
 * `crypto.subtle`, which a plain-http origin does not have (as `tabId.ts`).
 */
export const IMAGE_CHUNK_BYTES = 128 * 1024

/** The image types the agent takes (agent `src/sessions/images.ts` `IMAGE_MEDIA_TYPES`). */
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

/** An answer with an image in it, which goes as bytes rather than in the JSON. */
function withImage(outcome: unknown): outcome is { ok: true; result: { image: Blob } & Record<string, unknown> } {
  if (typeof outcome !== 'object' || outcome === null || (outcome as { ok?: unknown }).ok !== true) return false
  const result = (outcome as { result?: unknown }).result
  return typeof result === 'object' && result !== null && (result as { image?: unknown }).image instanceof Blob
}

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
  /**
   * Reconnect back-off: `baseMs · 2^attempt`, capped at `maxMs`, then scaled by a random
   * half to all of it, so tabs an agent restart dropped together do not return in lockstep (#747).
   */
  baseMs?: number
  maxMs?: number
  /** The jitter's source, in [0, 1); tests fix it. */
  random?: () => number
}

export function createTabLink({
  bridge,
  tabId = TAB_ID,
  url = socketUrl(BRIDGE_SOCKET_PATH),
  WebSocketImpl = WebSocket,
  baseMs = 500,
  maxMs = 15_000,
  random = Math.random,
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

  /**
   * Sends an answer's image on `ws` (an `image` frame, then its bytes) and gives the answer
   * as it then goes in the `result`; a failure when it cannot.
   */
  const sendImage = async (ws: WebSocket, id: string, tool: string, image: Blob): Promise<{ mediaType: string } | CallResult> => {
    if (!IMAGE_TYPES.has(image.type)) {
      return { ok: false, error: { code: 'failed', message: `"${tool}" made an image of type "${image.type}"` } }
    }
    const bytes = new Uint8Array(await image.arrayBuffer())
    if (socket !== ws || ws.readyState !== ws.OPEN) return { mediaType: image.type }
    ws.send(JSON.stringify(tabFrame({ type: 'image', id, mediaType: image.type, size: bytes.byteLength })))
    for (let at = 0; at < bytes.byteLength; at += IMAGE_CHUNK_BYTES) ws.send(bytes.subarray(at, at + IMAGE_CHUNK_BYTES))
    return { mediaType: image.type }
  }

  const run = async (id: string, tool: string, args: Record<string, unknown>) => {
    const ws = socket
    // #747 — a call that throws (a stale catalogue chunk) still gets its answer, or the
    // agent waits out its timeout for it.
    let outcome: unknown = await bridge.call(tool, args).catch(
      (error: unknown): CallResult => ({
        ok: false,
        error: { code: 'failed', message: `"${tool}" failed in the tab: ${String(error)}` },
      }),
    )
    if (withImage(outcome) && ws) {
      const { image, ...rest } = outcome.result
      const sent = await sendImage(ws, id, tool, image).catch(
        (error: unknown): CallResult => ({
          ok: false,
          error: { code: 'failed', message: `"${tool}" could not send its image: ${String(error)}` },
        }),
      )
      outcome = 'mediaType' in sent ? { ok: true, result: { ...rest, image: sent } } : sent
    }
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
    // The image's frames went on `ws`; its answer goes there too, or nowhere.
    if (socket && socket === (ws ?? socket) && socket.readyState === socket.OPEN) socket.send(frame)
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
      const delay = Math.min(maxMs, baseMs * 2 ** attempt) * (0.5 + random() / 2)
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
