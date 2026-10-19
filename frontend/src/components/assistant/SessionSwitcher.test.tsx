import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SessionState } from '../../agent/chat/state'
import { SessionSwitcher } from './SessionSwitcher'

const NOW = Date.parse('2026-10-11T12:00:00.000Z')

function offered(untilMs: number): SessionState {
  return {
    id: 's1',
    title: 'Box',
    origin: 'chat',
    owner: { kind: 'browser', id: 'browser', label: 'You' },
    status: 'idle',
    items: [],
    offer: { to: { kind: 'bearer', id: 'token:t1', label: 'MCP token:t1' }, until: new Date(untilMs).toISOString() },
  }
}

function renderSwitcher(session: SessionState, onDone: (id: string) => Promise<void> = () => Promise.resolve()) {
  const noop = () => Promise.resolve()
  render(
    <SessionSwitcher
      sessions={[session]}
      activeId={null}
      onOpen={() => {}}
      onRename={noop}
      onDone={onDone}
      onArchive={noop}
      onCancelOffer={noop}
    />,
  )
}

describe('a handoff offer lapsing (#1284)', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    vi.setSystemTime(NOW)
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('drops the offer at its until', () => {
    renderSwitcher(offered(NOW + 400))
    expect(screen.getByText('Offered to MCP token t1')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(399))
    expect(screen.getByText('Offered to MCP token t1')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(screen.queryByText('Offered to MCP token t1')).toBeNull()
  })

  it('still drops it when its timer fires before the clock reaches until', () => {
    // The wall clock stepped back after the timer was set (NTP, a VM's clock), or the
    // timer fired early: the clock reads short of until when it runs.
    renderSwitcher(offered(NOW + 400))
    vi.setSystemTime(NOW - 5)
    act(() => vi.advanceTimersByTime(400))
    expect(screen.getByText('Offered to MCP token t1')).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(5))
    expect(screen.queryByText('Offered to MCP token t1')).toBeNull()
  })
})

// #3983 — a Done the agent refuses from the row's menu says so on that row, as the
// header's Done says so in the header.
it('shows a refused Done from the menu on its row', async () => {
  const user = userEvent.setup()
  const session = { ...offered(Date.now() + 60_000), offer: undefined }
  renderSwitcher(session, () => Promise.reject(new Error('This chat is still running.')))
  await user.click(screen.getByRole('button', { name: 'Actions for Box' }))
  await user.click(await screen.findByRole('menuitem', { name: 'Mark Box done' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('This chat is still running.')
})
