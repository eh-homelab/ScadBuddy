import { act, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type * as Embed from '../../lib/embed'
import { refreshAuthSession } from '../../lib/signIn'
import { noteSignedOut } from '../../lib/signedOut'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SignInRequired } from './SignInRequired'

// #4975 phase 8 — signed out under SCADBUDDY_AUTH_MODE=enforce (spec 2026-11-01 §8.2).

const frame = vi.hoisted(() => ({ embedded: false }))
vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof Embed>()),
  isEmbedded: () => frame.embedded,
}))

function enforce(signedIn: boolean, loginEnabled = true) {
  server.use(
    http.get('/api/v1/auth/session', () =>
      HttpResponse.json({
        signed_in: signedIn,
        user: signedIn ? { principal: 'user:x#alice', name: 'Alice', email: null } : null,
        mode: 'enforce',
        login_enabled: loginEnabled,
        expires_at: null,
        kind: signedIn ? 'login' : null,
      }),
    ),
  )
}

describe('SignInRequired', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    frame.embedded = false
  })

  it('shows nothing while signed in', async () => {
    enforce(true)
    await refreshAuthSession()
    const { container } = renderPage(<SignInRequired />)
    expect(container).toBeEmptyDOMElement()
  })

  it('keeps the page under a dialog when a session ends mid-visit', async () => {
    enforce(true)
    await refreshAuthSession()
    renderPage(<SignInRequired />)
    enforce(false)
    await act(async () => {
      await refreshAuthSession()
    })
    expect(await screen.findByRole('alertdialog', { name: 'Sign in to continue' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('sends a page that never was signed in straight to the sign-in, coming back here', async () => {
    const assign = vi.fn()
    vi.spyOn(window, 'location', 'get').mockReturnValue({
      ...window.location,
      pathname: '/m/box',
      search: '?x=1',
      hash: '',
      assign,
    })
    enforce(false)
    await refreshAuthSession()
    renderPage(<SignInRequired />)
    await vi.waitFor(() =>
      expect(assign).toHaveBeenCalledWith('/api/v1/auth/login?return_to=%2Fm%2Fbox%3Fx%3D1'),
    )
  })

  it('asks for the click from inside a frame, where the sign-in is a popup', async () => {
    frame.embedded = true
    enforce(false)
    await refreshAuthSession()
    renderPage(<SignInRequired />)
    expect(await screen.findByRole('alertdialog', { name: 'Sign in to continue' })).toBeInTheDocument()
  })

  it('says sign-in is not set up when the login is off', async () => {
    enforce(true, false)
    await refreshAuthSession()
    renderPage(<SignInRequired />)
    act(() => {
      enforce(false, false)
    })
    await act(async () => {
      await refreshAuthSession()
    })
    noteSignedOut()
    expect(await screen.findByText(/sign-in is not set up/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
  })
})
