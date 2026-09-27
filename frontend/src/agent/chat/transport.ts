import type { ClientMessage } from './protocol'

/**
 * How the panel talks to the agent service. The panel only ever sees this interface:
 * the realtime WebSocket (#266) is one implementation, the scripted mock agent in
 * `src/mocks/agent.ts` is another.
 *
 * Frames arrive as `unknown` on purpose — the panel validates each one with
 * `parseServerEvent` before it touches state, whatever the transport.
 */
export interface ChatTransport {
  /** Starts delivering server frames. Called once per transport instance. */
  connect(handlers: TransportHandlers): void
  send(message: ClientMessage): void
  /** Stops delivery; no handler runs after this returns. */
  close(): void
}

export interface TransportHandlers {
  onFrame: (frame: unknown) => void
  /** The connection dropped. `reason` is for display. */
  onClose?: (reason?: string) => void
}

/** A fresh transport per mount (StrictMode mounts twice, and each mount closes its own). */
export type ChatTransportFactory = () => ChatTransport

/**
 * The transport this build uses, or null when there is none.
 *
 * TODO(#266): return the WebSocket transport once the realtime gateway exists. Until
 * then only the mocked build (`VITE_MOCK_API=1`, which the Playwright suite and
 * `pnpm dev` against msw use) has an agent, and it is the scripted mock. The dynamic
 * import keeps the mock out of the production bundle.
 */
export async function loadChatTransportFactory(): Promise<ChatTransportFactory | null> {
  if (import.meta.env.VITE_MOCK_API === '1') {
    const { createMockAgentTransport } = await import('../../mocks/agent')
    return () => createMockAgentTransport()
  }
  return null
}
