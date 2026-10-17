import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { useLeaveGuard } from './useLeaveGuard'

const onSentinel = () => {
  const state: unknown = window.history.state
  return typeof state === 'object' && state !== null && 'scadbuddyLeaveGuard' in state
}

function Page() {
  const [dirty, setDirty] = useState(true)
  const guard = useLeaveGuard(dirty)
  return (
    <>
      <button onClick={() => setDirty(false)}>Save</button>
      <button onClick={() => setDirty(true)}>Edit</button>
      <p data-testid="guard">{guard.pending === null ? 'quiet' : 'asking'}</p>
    </>
  )
}

const press = (name: string) => fireEvent.click(screen.getByRole('button', { name }))

/** The popstate of a `history.back()`: jsdom sends it a task later. */
const popped = () =>
  new Promise<void>((resolve) => window.addEventListener('popstate', () => resolve(), { once: true }))

describe('useLeaveGuard (#322)', () => {
  it('does not take its own Back for the user’s when the page is dirty again before it lands (#3671)', async () => {
    render(
      <MemoryRouter>
        <Page />
      </MemoryRouter>,
    )
    expect(onSentinel()).toBe(true)

    // Saved, then typed into again at once: the sentinel's own Back is still on its way.
    const pop = popped()
    press('Save')
    press('Edit')
    await act(() => pop)

    expect(screen.getByTestId('guard')).toHaveTextContent('quiet')
    // Re-armed: the page sits on a sentinel again, so the user's Back is still held.
    expect(onSentinel()).toBe(true)
    const back = popped()
    window.history.back()
    await act(() => back)
    await waitFor(() => expect(screen.getByTestId('guard')).toHaveTextContent('asking'))
  })

  it('takes the sentinel off when the page is saved, and then lets Back go', async () => {
    render(
      <MemoryRouter>
        <Page />
      </MemoryRouter>,
    )
    expect(onSentinel()).toBe(true)
    const pop = popped()
    press('Save')
    await act(() => pop)
    expect(onSentinel()).toBe(false)
    expect(screen.getByTestId('guard')).toHaveTextContent('quiet')
  })
})
