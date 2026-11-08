import { screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetMockState } from '../../mocks/handlers'
import { MOCK_CHECKING, MOCK_LINK_TOKEN, mockBankLink, setMockKeysRefused } from '../../mocks/features/bank'
import type { PlaidGlobal } from '../../lib/plaidLink'
import { enter, renderSettingsSection } from '../../pages/settings/testing'
import { BankAccount } from './BankAccount'

const route = '/settings/system'

/** Plaid Link as `Plaid.create` gives it, answering with `accounts` or closing. */
function stubLink(answer: { accounts: { id: string }[] } | 'exit') {
  const create = vi.fn<PlaidGlobal['create']>((config) => ({
    open: () => (answer === 'exit' ? config.onExit(null) : config.onSuccess('public-sandbox-1', answer)),
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

  it('says to choose one account when Link gives several', async () => {
    mockBankLink({ linked: false })
    stubLink({ accounts: [{ id: MOCK_CHECKING }, { id: 'mock-savings' }] })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Link a bank account' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose one checking or savings account')
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

  it('forgets the keys', async () => {
    mockBankLink({ linked: false })
    const { user } = renderSettingsSection(<BankAccount />, { route })
    await user.click(await screen.findByRole('button', { name: 'Forget keys' }))
    expect(await screen.findByLabelText('Client ID')).toBeInTheDocument()
  })
})
