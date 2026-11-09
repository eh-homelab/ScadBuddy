import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { setMockMailKek } from '../../../mocks/features/storefrontMail'
import { resetMockState } from '../../../mocks/handlers'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as StorefrontMail } from './storefront-mail'

const route = '/settings/system'

describe('Settings → Storefront mail (#5283)', () => {
  beforeEach(() => resetMockState())

  it('saves the SMTP host, port, user, sender and STARTTLS', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    await user.type(await screen.findByLabelText('SMTP host'), 'smtp.example.com')
    await user.type(screen.getByLabelText('Sender'), 'shop@example.com')
    await user.click(within(region('Storefront mail')).getByRole('button', { name: 'Save Storefront mail' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({
      storefront_mail_host: 'smtp.example.com',
      storefront_mail_sender: 'shop@example.com',
    })
    put.mockRestore()
  })

  it('sets the password and never shows it again', async () => {
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    expect(await screen.findByText('No password set')).toBeInTheDocument()
    const field = screen.getByLabelText('SMTP password')
    expect(field).toHaveAttribute('type', 'password')
    await enter(user, field, 'hunter2')
    await user.click(screen.getByRole('button', { name: 'Save password' }))
    expect(await screen.findByText('Password set')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('hunter2')).not.toBeInTheDocument()
    expect(JSON.stringify(await api.storefrontMailPassword())).not.toContain('hunter2')
    await user.click(screen.getByRole('button', { name: 'Forget password' }))
    expect(await screen.findByText('No password set')).toBeInTheDocument()
  })

  it('says the key file is missing on kek_missing', async () => {
    setMockMailKek(false)
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    await enter(user, await screen.findByLabelText('SMTP password'), 'pw')
    await user.click(screen.getByRole('button', { name: 'Save password' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/key file/)
    expect(screen.getByText('No password set')).toBeInTheDocument()
  })
})
