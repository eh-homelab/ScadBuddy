import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { BrowserRouter, Link, MemoryRouter, useLocation, useNavigate } from 'react-router'
import userEvent from '@testing-library/user-event'
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

  describe('with within', () => {
    function Settings() {
      const guard = useLeaveGuard(true, { within: '/settings' })
      const location = useLocation()
      const navigate = useNavigate()
      return (
        <>
          <Link to="/settings/library">Library</Link>
          <Link to="/settings#fonts">Fonts</Link>
          <Link to="/">Home</Link>
          <button onClick={() => void navigate('/settings#fonts')}>Go fonts</button>
          <button onClick={() => void navigate('/')}>Go home</button>
          <p data-testid="where">{location.pathname + location.hash}</p>
          <p data-testid="pending">{guard.pending === null ? 'none' : String(guard.pending)}</p>
        </>
      )
    }
    const mount = () => {
      window.history.replaceState(null, '', '/settings/general')
      render(
        <BrowserRouter>
          <Settings />
        </BrowserRouter>,
      )
    }

    it('lets a link to another path under it through while dirty', async () => {
      mount()
      await userEvent.setup().click(screen.getByRole('link', { name: 'Library' }))
      expect(screen.getByTestId('where')).toHaveTextContent('/settings/library')
      expect(screen.getByTestId('pending')).toHaveTextContent('none')
    })

    it('lets a programmatic navigation under it through', () => {
      mount()
      press('Go fonts')
      expect(screen.getByTestId('where')).toHaveTextContent('/settings#fonts')
      expect(screen.getByTestId('pending')).toHaveTextContent('none')
    })

    it('still holds a link out of it', async () => {
      mount()
      await userEvent.setup().click(screen.getByRole('link', { name: 'Home' }))
      expect(screen.getByTestId('where')).toHaveTextContent('/settings/general')
      expect(screen.getByTestId('pending')).toHaveTextContent('/')
    })

    it('still holds a programmatic navigation out of it', () => {
      mount()
      press('Go home')
      expect(screen.getByTestId('where')).toHaveTextContent('/settings/general')
      expect(screen.getByTestId('pending')).toHaveTextContent('/')
    })

    it('Back from a second Settings path returns to the first without asking', async () => {
      mount()
      await userEvent.setup().click(screen.getByRole('link', { name: 'Library' }))
      const back = popped()
      window.history.back()
      await act(() => back)
      await waitFor(() => expect(window.location.pathname).toBe('/settings/general'))
      expect(screen.getByTestId('pending')).toHaveTextContent('none')
    })
  })

  it('without options, a link to another path is still held', async () => {
    function Plain() {
      const guard = useLeaveGuard(true)
      return (
        <>
          <Link to="/settings/library">Library</Link>
          <p data-testid="pending">{guard.pending === null ? 'none' : String(guard.pending)}</p>
        </>
      )
    }
    window.history.replaceState(null, '', '/settings/general')
    render(
      <BrowserRouter>
        <Plain />
      </BrowserRouter>,
    )
    await userEvent.setup().click(screen.getByRole('link', { name: 'Library' }))
    expect(screen.getByTestId('pending')).toHaveTextContent('/settings/library')
  })
})
