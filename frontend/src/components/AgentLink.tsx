import { useEffect, useState, useSyncExternalStore } from 'react'
import { createTabLink, type TabLink, type TabLinkState } from '../agent/link'
import { useAgentBridge } from '../agent/useAgentHandlers'
import { PairingPrompt } from './PairingPrompt'

/**
 * The browser bridge's link to the agent service (#254, `agent/link.ts`), held for as
 * long as the assistant is available, and the pairing prompt it feeds. Loaded lazily by
 * the app shell, so neither the link nor its zod schemas are in the entry chunk.
 */

export type TabLinkFactory = (bridge: Parameters<typeof createTabLink>[0]['bridge']) => TabLink

declare global {
  interface Window {
    /** The mocked e2e build's opt-in to the real tab socket (`e2e/agent-link.spec.ts`). */
    __scadbuddyTabLink?: boolean
  }
}

/**
 * The link this build makes, or null for none. The msw-mocked build has no agent to
 * connect to, so it links only when a Playwright test has asked for it (and then routes
 * the socket itself); unit tests pass their own factory.
 */
function defaultFactory(): TabLinkFactory | null {
  if (import.meta.env.MODE === 'test') return null
  if (import.meta.env.VITE_MOCK_API === '1' && window.__scadbuddyTabLink !== true) return null
  return (bridge) => createTabLink({ bridge })
}

const IDLE: TabLinkState = { connected: false, pending: [], paired: [], results: {} }
const noSubscribe = () => () => {}

export function AgentLink({ factory }: { factory?: TabLinkFactory | null }) {
  const bridge = useAgentBridge()
  const [link, setLink] = useState<TabLink | null>(null)

  useEffect(() => {
    const make = factory === undefined ? defaultFactory() : factory
    if (!make) return
    const created = make(bridge)
    created.connect()
    setLink(created)
    return () => {
      created.close()
      setLink(null)
    }
  }, [bridge, factory])

  const state = useSyncExternalStore(link?.subscribe ?? noSubscribe, link?.getState ?? (() => IDLE))
  if (!link) return null
  return (
    <PairingPrompt
      state={state}
      onAccept={(id, code) => link.accept(id, code)}
      onDeny={(id) => link.deny(id)}
      onEnd={(id) => link.end(id)}
    />
  )
}
