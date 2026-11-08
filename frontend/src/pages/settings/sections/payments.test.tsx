import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { mockSettings, resetMockState } from '../../../mocks/handlers'
import { mockConnectProvider, mockPaymentEvent, setMockKek, setMockWebhookError } from '../../../mocks/features/payments'
import { ORDER_ACCEPTED } from '../../../mocks/features/orders/state'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Payments } from './payments'

const route = '/settings/system'

async function fill(user: ReturnType<typeof renderSettingsSection>['user']) {
  await enter(user, await screen.findByLabelText('Account'), 'a')
  await enter(user, screen.getByLabelText('Signing secret'), 's3cret')
}

describe('Settings → Payments (#4957)', () => {
  beforeEach(() => resetMockState())

  it('connects a provider and never shows its secret again', async () => {
    const { user } = renderSettingsSection(<Payments />, { route })
    await fill(user)
    expect(screen.getByLabelText('Signing secret')).toHaveAttribute('type', 'password')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('s3cret')).not.toBeInTheDocument()
    expect(screen.getByText('No webhook received yet')).toBeInTheDocument()
    expect(JSON.stringify(await api.paymentProviders())).not.toContain('s3cret')
  })

  it('says the key file is missing on kek_missing', async () => {
    setMockKek(false)
    const { user } = renderSettingsSection(<Payments />, { route })
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/key file/)
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument()
  })

  it('shows when the last webhook came', async () => {
    mockConnectProvider()
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 100 })
    const opened = await api.requestPayment(ORDER_ACCEPTED, {})
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'expired')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText(/Last webhook/)).toBeInTheDocument()
  })

  it('shows why the last webhook failed until one succeeds (#5684)', async () => {
    mockConnectProvider()
    setMockWebhookError('signature')
    const first = renderSettingsSection(<Payments />, { route })
    expect(await screen.findByRole('status')).toHaveTextContent(/bad signature arrived/)
    first.unmount()
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 100 })
    const opened = await api.requestPayment(ORDER_ACCEPTED, {})
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'expired')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText(/Last webhook/)).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('refuses to disconnect while a request is open, with the backend’s reason', async () => {
    mockConnectProvider()
    await api.requestPayment(ORDER_ACCEPTED, {})
    const { user } = renderSettingsSection(<Payments />, { route })
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('open payment requests')
    expect(screen.getByText('Connected as Fake account a')).toBeInTheDocument()
  })

  it('disconnects a provider with no open request', async () => {
    mockConnectProvider()
    const { user } = renderSettingsSection(<Payments />, { route })
    await user.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument())
    expect(within(region('Payments')).getByLabelText('Account')).toBeInTheDocument()
  })

  it('warns when the account takes another currency than the orders (#5666)', async () => {
    mockConnectProvider('a', 'CAD')
    renderSettingsSection(<Payments />, { route })
    const fake = within(await screen.findByRole('region', { name: 'Fake' }))
    expect(await fake.findByText('Connected in CAD; orders are in USD. New payment links will fail.')).toBeInTheDocument()
  })

  it('says nothing when the account takes the orders currency, or any', async () => {
    mockConnectProvider('a', 'USD')
    const first = renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByText(/New payment links will fail/)).not.toBeInTheDocument()
    first.unmount()
    mockConnectProvider('a')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByText(/New payment links will fail/)).not.toBeInTheDocument()
  })

  it('puts the key file back at resetMockState, so one test cannot leak it into the next', async () => {
    setMockKek(false)
    resetMockState()
    const body = { settings: { account: 'a' }, secrets: { signing_secret: 's' } }
    expect((await api.connectPaymentProvider('fake', body)).connected).toBe(true)
  })
})

describe('Settings → Payments, paying by hand (#5281)', () => {
  beforeEach(() => resetMockState())

  it('saves the payment instructions as plain text', async () => {
    const { user } = renderSettingsSection(<Payments />, { route })
    const box = await screen.findByLabelText('Payment instructions')
    await user.type(box, 'Bank transfer to <b>12-34</b>, with the order number.')
    await user.click(screen.getByRole('button', { name: 'Save instructions' }))
    await waitFor(() => expect(mockSettings().storefront_payment_instructions).toBe('Bank transfer to <b>12-34</b>, with the order number.'))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    // Kept as typed: plain text, never markup.
    expect(box).toHaveValue('Bank transfer to <b>12-34</b>, with the order number.')
    // Cleared, it is null again.
    await user.clear(box)
    await user.click(screen.getByRole('button', { name: 'Save instructions' }))
    await waitFor(() => expect(mockSettings().storefront_payment_instructions).toBeNull())
  })
})

