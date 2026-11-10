import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { setMockMailKek, setMockMailServerError } from '../../../mocks/features/storefrontMail'
import { mockSettings, resetMockState, setMockSettings } from '../../../mocks/handlers'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as StorefrontMail } from './storefront-mail'

const route = '/settings/system'

/** The host and user the password is bound to (ruling R2). */
function connected() {
  setMockSettings({ ...mockSettings(), storefront_mail_host: 'smtp.example.com', storefront_mail_user: 'shop' })
}

describe('Settings → Storefront mail (#5283)', () => {
  beforeEach(() => resetMockState())

  it('saves the SMTP host, port, user and sender', async () => {
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
    connected()
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    expect(await screen.findByText('No password set')).toBeInTheDocument()
    const field = screen.getByLabelText('SMTP password')
    expect(field).toHaveAttribute('type', 'password')
    // Never the browser's saved login (review M5).
    expect(field).toHaveAttribute('autocomplete', 'new-password')
    await enter(user, field, 'hunter2')
    await user.click(screen.getByRole('button', { name: 'Save password' }))
    expect(await screen.findByText('Password set for shop@smtp.example.com:587 (starttls)')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('hunter2')).not.toBeInTheDocument()
    expect(JSON.stringify(await api.storefrontMailPassword())).not.toContain('hunter2')
    await user.click(screen.getByRole('button', { name: 'Forget password' }))
    expect(await screen.findByText('No password set')).toBeInTheDocument()
  })

  it('says the key file is missing on kek_missing', async () => {
    connected()
    setMockMailKek(false)
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    await enter(user, await screen.findByLabelText('SMTP password'), 'pw')
    await user.click(screen.getByRole('button', { name: 'Save password' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/key file/)
    expect(screen.getByText('No password set')).toBeInTheDocument()
  })

  it('asks for the host and user before the password', async () => {
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    await enter(user, await screen.findByLabelText('SMTP password'), 'pw')
    await user.click(screen.getByRole('button', { name: 'Save password' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/host and user/)
  })

  it('shows the last failure of the mail server', async () => {
    setMockMailServerError('SMTPAuthenticationError: (535, bad login)')
    renderSettingsSection(<StorefrontMail />, { route })
    expect(await screen.findByRole('status')).toHaveTextContent(/SMTPAuthenticationError/)
  })

  it('offers one TLS setting: STARTTLS, implicit TLS or none', async () => {
    const { user } = renderSettingsSection(<StorefrontMail />, { route })
    await user.click(await screen.findByRole('combobox', { name: 'TLS' }))
    expect((await screen.findAllByRole('option')).map((option) => option.textContent)).toEqual([
      'STARTTLS on the port (usually 587)',
      'Implicit TLS (usually 465)',
      'None, a relay with no login',
    ])
    expect(screen.queryByLabelText('Use STARTTLS')).not.toBeInTheDocument()
  })

  it('refuses a user on a plain connection', async () => {
    setMockSettings({ ...mockSettings(), storefront_mail_tls: 'none' })
    await expect(api.putSettings({ storefront_mail_user: 'shop' })).rejects.toThrow(/relay with no login/)
    expect(mockSettings().storefront_mail_user).toBeNull()
  })
})
