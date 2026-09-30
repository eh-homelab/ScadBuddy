import { act, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { bridge } from '../agent/bridge'
import type { LinkBridge, TabLink, TabLinkState } from '../agent/link'
import { renderPage } from '../test/utils'
import { AppShell } from './AppShell'

/** What `useAiAvailability` answers; `set` re-renders whoever reads it, as the real one does. */
const availability = vi.hoisted(() => {
  type Value = { available: boolean; state?: string }
  let value: Value = { available: false }
  const listeners = new Set<() => void>()
  return {
    get: () => value,
    set: (next: Value) => {
      value = next
      for (const listener of listeners) listener()
    },
    subscribe: (listener: () => void) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
  }
})
vi.mock('../agent/chat/availability', async () => {
  const { useSyncExternalStore } = await import('react')
  return { useAiAvailability: () => useSyncExternalStore(availability.subscribe, availability.get) }
})

/** A link the test drives: it records what the shell did with it. */
function fakeLink() {
  let state: TabLinkState = { connected: false, pending: [], paired: [], results: {} }
  const listeners = new Set<() => void>()
  const made: { bridge: LinkBridge }[] = []
  const link: TabLink & { push(next: Partial<TabLinkState>): void } = {
    connect: vi.fn(),
    close: vi.fn(),
    getState: () => state,
    subscribe: (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    accept: vi.fn(() => true),
    deny: vi.fn(() => true),
    end: vi.fn(() => true),
    push(next) {
      state = { ...state, ...next }
      for (const listener of listeners) listener()
    },
  }
  const factory = (b: LinkBridge) => {
    made.push({ bridge: b })
    return link
  }
  return { link, factory, made }
}

describe('the shell holds the browser bridge’s link while the assistant is available (#254)', () => {
  it('links the app’s bridge once available, shows what it pushes, and closes it when AI goes', async () => {
    const { link, factory, made } = fakeLink()
    availability.set({ available: false })
    renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={factory} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
    )
    expect(made).toHaveLength(0)

    act(() => availability.set({ available: true, state: 'configured' }))
    await waitFor(() => expect(link.connect).toHaveBeenCalledTimes(1))
    expect(made[0]!.bridge).toBe(bridge)

    act(() => link.push({ pending: [{ id: 'p1', label: 'MCP token “laptop”', expiresAt: '2026-09-29T12:00:00Z' }] }))
    expect(await screen.findByRole('region', { name: 'Agent pairing' })).toHaveTextContent('MCP token “laptop” asks to use this tab')

    act(() => availability.set({ available: false, state: 'unavailable' }))
    await waitFor(() => expect(link.close).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('region', { name: 'Agent pairing' })).not.toBeInTheDocument()
  })
})
