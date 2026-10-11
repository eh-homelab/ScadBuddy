import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SessionState } from '../../agent/chat/state'
import { SessionSwitcher } from './SessionSwitcher'

const MAX_DELAY = 2 ** 31 - 1

function session(until: number): SessionState {
  return {
    id: 's1',
    title: 'Own chat',
    origin: 'chat',
    owner: { kind: 'browser', id: 'browser:me', label: 'you' },
    status: 'idle',
    items: [],
    offer: { to: { kind: 'bearer', label: 'MCP token t1' }, until: new Date(until).toISOString() },
  }
}

function renderAt(until: number) {
  const done = () => Promise.resolve()
  render(
    <SessionSwitcher
      sessions={[session(until)]}
      activeId="s1"
      onOpen={() => {}}
      onRename={done}
      onDone={done}
      onArchive={done}
      onCancelOffer={done}
    />,
  )
}

const offered = () => screen.queryByText('Offered to MCP token t1')

describe('a handoff offer lapsing with no new snapshot (#1284)', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('drops the offer at its until', () => {
    renderAt(Date.now() + 1_000)
    expect(offered()).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(999))
    expect(offered()).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(offered()).toBeNull()
  })

  // The timer used to fire once: a capped wait (or one that fired a little early)
  // found the offer not yet lapsed and was never armed again, so it showed forever.
  it('re-arms past the 32-bit timer cap instead of keeping the offer', () => {
    renderAt(Date.now() + MAX_DELAY + 5_000)
    act(() => vi.advanceTimersByTime(MAX_DELAY))
    expect(offered()).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(5_000))
    expect(offered()).toBeNull()
  })
})
