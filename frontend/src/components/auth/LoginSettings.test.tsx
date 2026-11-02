import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockSignedIn } from '../../mocks/features/auth'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LoginSettings } from './LoginSettings'
import { SignedInSessions } from './SignedInSessions'

// #4975 — Settings → System → Sign-in and Signed-in sessions, against src/mocks/features/auth.ts.

describe('LoginSettings', () => {
  it('starts from the MCP section’s issuer and client id (D1)', async () => {
    renderPage(<LoginSettings />)
    expect(await screen.findByLabelText('Issuer URL')).toHaveValue('https://idp.example.com/')
    expect(screen.getByText(/http:\/\/localhost\/api\/v1\/auth\/callback/)).toBeInTheDocument()
  })

  it('refuses to switch on without a rule, and saves with one', async () => {
    const user = userEvent.setup()
    renderPage(<LoginSettings />)
    await user.click(await screen.findByRole('switch', { name: /Let people sign in/ }))
    await user.type(screen.getByLabelText('Client ID'), 'scadbuddy')
    await user.type(screen.getByLabelText('Client secret'), 'shh')
    await user.click(screen.getByRole('button', { name: 'Save sign-in settings' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('sign-in rule')
    await user.type(screen.getByLabelText('Subjects'), 'alice')
    await user.click(screen.getByRole('button', { name: 'Save sign-in settings' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. The header now offers Sign in.')
    expect(screen.getByLabelText('Client secret')).toHaveAttribute('placeholder', 'Saved; leave empty to keep it')
  })

  it('tests discovery', async () => {
    const user = userEvent.setup()
    renderPage(<LoginSettings />)
    await user.click(await screen.findByRole('button', { name: 'Test discovery' }))
    expect(await screen.findByRole('status')).toHaveTextContent('2 signing keys')
  })

  it('shows a load failure', async () => {
    server.use(http.get('/api/v1/auth/config', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    renderPage(<LoginSettings />)
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })
})

describe('SignedInSessions', () => {
  it('says the sign-in is off', async () => {
    renderPage(<SignedInSessions />)
    expect(await screen.findByText('Sign-in is off.')).toBeInTheDocument()
  })

  it('lists, revokes and signs out everywhere', async () => {
    mockSignedIn()
    const user = userEvent.setup()
    renderPage(<SignedInSessions />)
    expect(await screen.findByText('Safari on iPhone')).toBeInTheDocument()
    expect(screen.getByText('This browser')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(screen.queryByText('Safari on iPhone')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Sign out everywhere' }))
    expect(await screen.findByText('Sign in to see your sessions.')).toBeInTheDocument()
  })
})
