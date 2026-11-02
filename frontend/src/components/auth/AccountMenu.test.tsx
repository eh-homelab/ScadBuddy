import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { mockLoginEnabled, mockSignedIn } from '../../mocks/features/auth'
import { renderPage } from '../../test/utils'
import { AccountMenu } from './AccountMenu'

// #4975 — the header's sign-in, against src/mocks/features/auth.ts.

describe('AccountMenu', () => {
  it('shows nothing while the login is off', async () => {
    const { container } = renderPage(<AccountMenu />)
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(screen.queryByRole('button', { name: /sign in/i })).not.toBeInTheDocument()
  })

  it('offers Sign in once the login is on', async () => {
    mockLoginEnabled()
    renderPage(<AccountMenu />)
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('names the signed-in person and signs out', async () => {
    mockSignedIn()
    const user = userEvent.setup()
    renderPage(<AccountMenu />)
    await user.click(await screen.findByRole('button', { name: 'Account: Alice' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Sign out' }))
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('shows a break-glass session as one (spec §9.3)', async () => {
    mockSignedIn('break_glass')
    renderPage(<AccountMenu />)
    expect(await screen.findByRole('button', { name: 'Account: break-glass session' })).toHaveTextContent(
      'Break-glass session',
    )
  })
})
