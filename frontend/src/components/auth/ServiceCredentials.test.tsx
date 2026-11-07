import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { ServiceCredentials } from './ServiceCredentials'

// #4975 phase 4 — Settings → System → Service credentials, against src/mocks/features/serviceCredentials.ts.

describe('ServiceCredentials', () => {
  it('lists each service’s credentials, never a secret', async () => {
    renderPage(<ServiceCredentials />)
    const agent = await screen.findByRole('list', { name: 'Agent credentials' })
    expect(within(agent).getByText(/last used/)).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Print worker credentials' })).getByText(/never used/)).toBeInTheDocument()
    // #5298: the projects worker exchanges its credential; it has no sealed one to rotate.
    expect(screen.queryByRole('region', { name: 'Projects worker' })).not.toBeInTheDocument()
  })

  it('rotates and revokes', async () => {
    const user = userEvent.setup()
    renderPage(<ServiceCredentials />)
    const section = await screen.findByRole('region', { name: 'Print worker' })
    await user.click(within(section).getByRole('button', { name: 'Rotate now' }))
    const list = screen.getByRole('list', { name: 'Print worker credentials' })
    expect(await within(list).findAllByRole('button', { name: 'Revoke' })).toHaveLength(2)
    await user.click(within(list).getAllByRole('button', { name: 'Revoke' })[0]!)
    expect(await within(list).findByText('revoked')).toBeInTheDocument()
  })
})
