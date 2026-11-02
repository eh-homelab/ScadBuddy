import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { AccessTokens } from './AccessTokens'

// #4975 phase 6 — Settings → System → Access tokens, against src/mocks/features/accessTokens.ts.

describe('AccessTokens', () => {
  it('shows a new token once, then lists it without it, and revokes it', async () => {
    const user = userEvent.setup()
    renderPage(<AccessTokens />)
    const list = await screen.findByRole('list', { name: 'Access tokens' })
    expect(within(list).getByText('No tokens.')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Name'), 'laptop')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByLabelText('New token')).toHaveTextContent(/^sbpat_/)
    expect(await within(list).findByText('laptop')).toBeInTheDocument()
    expect(within(list).queryByText(/sbpat_/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByLabelText('New token')).not.toBeInTheDocument()
    await user.click(within(list).getByRole('button', { name: 'Revoke' }))
    expect(await within(list).findByText('revoked')).toBeInTheDocument()
  })
})
