import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { MOCK_CHECKING, MOCK_LINK_TOKEN, mockBankLink, setMockKeysRefused, setMockKeysUnreadable } from '../../mocks/features/bank'
import type { PlaidGlobal, PlaidLinkError } from '../../lib/plaidLink'
import { enter, renderSettingsSection } from '../../pages/settings/testing'
import { BankAccount } from './BankAccount'

const route = '/settings/system'

/** Plaid Link as `Plaid.create` gives it, answering with `accounts` or closing. */
function stubLink(answer: { accounts: { id: string }[] } | 'exit' | { error: PlaidLinkError }) {
  const create = vi.fn<PlaidGlobal['create']>((config) => ({
    open: () =>
      answer === 'exit'
        ? config.onExit(null)
        : 'error' in answer
          ? config.onExit(answer.error)
          : config.onSuccess('public-sandbox-1', answer),
    destroy: () => {},
  }))
  window.Plaid = { create }
  return create
}

describe('Settings → Payments → Bank account (#4961)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => {
    delete window.Plaid
  })

  it('saves the Plaid keys and never shows the secret again', async () => {
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await enter(user, await screen.findByLabelText('Client ID'), 'client-1')
    await enter(user, screen.getByLabelText('Secret'), 'plaid-s3cret')
    expect(screen.getByLabelText('Secret')).toHaveAttribute('type', 'password')
    await user.click(screen.getByRole('radio', { name: 'Sandbox' }))
    await user.click(screen.getByRole('button', { name: 'Save keys' }))
    expect(await screen.findByRole('button', { name: 'Link a bank account' })).toBeInTheDocument()
    expect(screen.getByText(/Plaid keys saved \(sandbox\)/)).toBeInTheDocument()
    expect(screen.queryByDisplayValue('plaid-s3cret')).not.toBeInTheDocument()
  })

  it("shows Plaid's refusal of the keys", async () => {
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await enter(user, await screen.findByLabelText('Client ID'), 'client-1')
    await enter(user, screen.getByLabelText('Secret'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Save keys' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid client_id or secret')
  })

  it('links the account chosen in Plaid Link and shows its name, type and mask only', async () => {
    mockBankLink({ linked: false })
    const create = stubLink({ accounts: [{ id: MOCK_CHECKING }] })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByText('Plaid Checking')).toBeInTheDocument()
    expect(screen.getByText(/checking · ending 0000/)).toBeInTheDocument()
    expect(create.mock.calls[0]![0].token).toBe(MOCK_LINK_TOKEN)
    expect(screen.getByText('Stripe payouts go to the bank set in your Stripe dashboard.')).toBeInTheDocument()
  })

  it('links nothing when the shop closes Plaid Link', async () => {
    mockBankLink({ linked: false })
    stubLink('exit')
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('button', { name: 'Link a bank account' })).toBeEnabled()
    expect(screen.queryByText('Plaid Checking')).not.toBeInTheDocument()
  })

  it('says when the deposits were last read, and why the last read failed', async () => {
    mockBankLink({ linked: true, synced: true })
    const first = renderSettingsSection(<BankAccount />, { route })
    expect(await screen.findByText(/Deposits last read/)).toBeInTheDocument()
    first.unmount()
    mockBankLink({ linked: true, syncError: 'ITEM_LOGIN_REQUIRED' })
    renderSettingsSection(<BankAccount />, { route })
    expect(await screen.findByRole('status')).toHaveTextContent('Plaid could not be read (ITEM_LOGIN_REQUIRED)')
    expect(screen.getByText(/Deposits not read yet/)).toHaveTextContent('the housekeeping sweeps must be on')
  })

  it('shows Reconnect while Plaid needs a sign-in, and clears it after update mode', async () => {
    mockBankLink({ linked: true, loginRequired: true, syncError: 'ITEM_LOGIN_REQUIRED' })
    const create = stubLink({ accounts: [{ id: MOCK_CHECKING }] })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    expect(await screen.findByRole('status')).toHaveTextContent('Plaid needs you to sign in to the bank again.')
    await user.click(screen.getByRole('button', { name: 'Reconnect' }))
    expect(create).toHaveBeenCalledOnce()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Reconnect' })).not.toBeInTheDocument())
    expect(screen.getByText('Plaid Checking')).toBeInTheDocument()
  })

  it('keeps Reconnect when the shop closes update mode', async () => {
    mockBankLink({ linked: true, loginRequired: true })
    stubLink('exit')
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Reconnect' }))
    expect(await screen.findByRole('button', { name: 'Reconnect' })).toBeEnabled()
  })

  it('unlinks the account and keeps the keys', async () => {
    mockBankLink({ linked: true })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('button', { name: 'Link a bank account' })).toBeInTheDocument()
    expect(screen.queryByText('Plaid Checking')).not.toBeInTheDocument()
  })

  it('says to remove the Item in the Plaid Dashboard when Plaid refused the keys', async () => {
    mockBankLink({ linked: true })
    setMockKeysRefused(true)
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Remove it in the Plaid Dashboard.')
    expect(await screen.findByRole('button', { name: 'Link a bank account' })).toBeInTheDocument()
  })

  it('clears the unlink note when the next action starts (link)', async () => {
    mockBankLink({ linked: true })
    setMockKeysRefused(true)
    stubLink({ accounts: [{ id: MOCK_CHECKING }] })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Remove it in the Plaid Dashboard.')
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByText('Plaid Checking')).toBeInTheDocument()
    expect(screen.queryByText(/Remove it in the Plaid Dashboard/)).not.toBeInTheDocument()
  })

  it('says the keys would not open, and to save them again, when they are unreadable', async () => {
    mockBankLink({ linked: true })
    setMockKeysUnreadable(true)
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('status')).toHaveTextContent('could not be opened with this key file')
    expect(await screen.findByLabelText('Client ID')).toBeInTheDocument()
  })

  it('preselects the saved environment when replacing the keys', async () => {
    mockBankLink({ linked: false })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Replace keys' }))
    expect(screen.getByRole('radio', { name: 'Sandbox' })).toBeChecked()
  })

  it('closes the key form once another action succeeds', async () => {
    mockBankLink({ linked: false })
    stubLink({ accounts: [{ id: MOCK_CHECKING }] })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Replace keys' }))
    await user.click(screen.getByRole('button', { name: 'Link a bank account' }))
    await user.click(await screen.findByRole('button', { name: 'Unlink' }))
    expect(await screen.findByRole('button', { name: 'Replace keys' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Client ID')).not.toBeInTheDocument()
  })

  it('sends the token even when Link gives several accounts, so the Item is removed', async () => {
    mockBankLink({ linked: false })
    stubLink({ accounts: [{ id: MOCK_CHECKING }, { id: 'mock-savings' }] })
    let sent: unknown = null
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname === '/api/v1/payments/bank/link') sent = await request.clone().json()
    })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose one checking or savings account in Plaid.')
    expect(sent).toEqual({ public_token: 'public-sandbox-1', account_ids: [MOCK_CHECKING, 'mock-savings'] })
    server.events.removeAllListeners()
  })

  it("shows Link's own error when Link fails", async () => {
    mockBankLink({ linked: false })
    stubLink({ error: { display_message: 'The bank is down.', error_message: 'institution down' } })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The bank is down.')
  })

  it("falls back to Link's error_message when there is no display_message", async () => {
    mockBankLink({ linked: false })
    stubLink({ error: { display_message: null, error_message: 'link token expired' } })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('link token expired')
  })

  it('says Plaid is unavailable when it did not answer', async () => {
    mockBankLink({ linked: false })
    server.use(
      http.post('/api/v1/payments/bank/link-token', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/payments/plaid_unavailable', title: 'Service Unavailable', status: 503, detail: 'Plaid did not answer' },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Plaid is unavailable right now')
  })

  it('forgets the keys', async () => {
    mockBankLink({ linked: false })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Forget keys' }))
    expect(await screen.findByLabelText('Client ID')).toBeInTheDocument()
  })
})
