import type { ClientMessage } from './protocol'

export type SendResult = 'sent' | 'queued' | 'refused'

/**
 * How the panel talks to the agent service. The panel only ever sees this interface:
 * the agent's chat WebSocket (`./socketTransport.ts`) is one implementation, the
 * scripted mock agent in `src/mocks/agent.ts` is another.
 *
 * Frames arrive as `unknown` on purpose — the panel validates each one with
 * `parseServerEvent` before it touches state, whatever the transport.
 */
export interface ChatTransport {
  /** Starts delivering server frames. Called once per transport instance. */
  connect(handlers: TransportHandlers): void
  /**
   * `sent`: on the wire now. `queued`: held until the connection is back, then sent
   * before anything else. `refused`: not taken (the queue is full) and it will never
   * arrive, so the caller must say so and let the user try again.
   */
  send(message: ClientMessage): SendResult
  /** Stops delivery; no handler runs after this returns. */
  close(): void
}

export interface TransportHandlers {
  onFrame: (frame: unknown) => void
  /** The connection dropped. `reason` is for display. */
  onClose?: (reason?: string) => void
  /**
   * A connection is open: the first one, and each reconnect after `onClose`. A
   * transport that is never disconnected (the mock) need not call it.
   */
  onOpen?: () => void
}

/** A fresh transport per mount (StrictMode mounts twice, and each mount closes its own). */
export type ChatTransportFactory = () => ChatTransport

/**
 * The transport this build uses: the agent's chat socket, or in the mocked build
 * (`VITE_MOCK_API=1`, which the Playwright suite and `pnpm dev` against msw use) the
 * scripted mock. The dynamic imports keep the mock out of the production bundle, and
 * the socket out of the mocked one. Only called once the agent reported itself
 * available (`useAiAvailability`).
 */
export async function loadChatTransportFactory(): Promise<ChatTransportFactory | null> {
  if (import.meta.env.VITE_MOCK_API === '1') {
    const { createMockAgentTransport } = await import('../../mocks/agent')
    return () => createMockAgentTransport()
  }
  const { createSocketTransport } = await import('./socketTransport')
  return () => createSocketTransport()
}
