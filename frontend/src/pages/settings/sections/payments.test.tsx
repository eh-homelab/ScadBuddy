import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { HttpResponse, http } from 'msw'
import { api } from '../../../api/client'
import { mockSettings, resetMockState } from '../../../mocks/handlers'
import { server } from '../../../mocks/server'
import { choose, optionDisabled, optionsOf } from '../../../test/select'
import { FAKE_PROVIDER, mockConnectProvider, mockPaymentEvent, setMockKek, setMockWebhookError } from '../../../mocks/features/payments'
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
    expect(await fake.findByText('Connected in CAD; orders are in USD. Payment links for orders in USD will fail; orders in CAD still work.')).toBeInTheDocument()
  })

  it('says nothing when the account takes the orders currency, or any', async () => {
    mockConnectProvider('a', 'USD')
    const first = renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByText(/Payment links for orders in/)).not.toBeInTheDocument()
    first.unmount()
    mockConnectProvider('a')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByText(/Payment links for orders in/)).not.toBeInTheDocument()
  })

  it('asks to reconnect an account in one currency whose currency was never checked (#5666)', async () => {
    mockConnectProvider('a', null, true)
    renderSettingsSection(<Payments />, { route })
    const fake = within(await screen.findByRole('region', { name: 'Fake' }))
    expect(await fake.findByText('Currency not checked; reconnect to check it.')).toBeInTheDocument()
  })

  it('never asks a provider whose account takes any currency', async () => {
    mockConnectProvider('a')
    renderSettingsSection(<Payments />, { route })
    expect(await screen.findByText('Connected as Fake account a')).toBeInTheDocument()
    expect(screen.queryByText(/Currency not checked/)).not.toBeInTheDocument()
  })

  it('a connect takes reports_currency from the provider, not a constant (#5752 review)', async () => {
    // Not the mock's usual false, so a hard-coded false cannot pass. resetMockState
    // (beforeEach) puts it back.
    FAKE_PROVIDER.reports_currency = true
    const body = { settings: { account: 'a' }, secrets: { signing_secret: 's' } }
    expect((await api.connectPaymentProvider('fake', body)).reports_currency).toBe(true)
    const [listed] = await api.paymentProviders()
    expect(listed?.reports_currency).toBe(true)
  })

  it('puts reports_currency back at resetMockState', () => {
    FAKE_PROVIDER.reports_currency = true
    resetMockState()
    expect(FAKE_PROVIDER.reports_currency).toBe(false)
  })

  it('offers a field with choices as a list and sends the chosen one (#4960)', async () => {
    const withChoices = {
      id: 'picky',
      label: 'Picky',
      fields: [{ name: 'mode', label: 'Mode', secret: false, choices: ['sandbox', 'live'] }],
      connected: false,
      account_label: null,
      is_default: false,
      connected_at: null,
      last_webhook_at: null,
    }
    let sent: unknown = null
    server.use(
      http.get('/api/v1/payments/providers', () => HttpResponse.json([withChoices])),
      http.put('/api/v1/payments/providers/picky', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ...withChoices, connected: true, account_label: 'Picky (live)' })
      }),
    )
    const { user } = renderSettingsSection(<Payments />, { route })
    const mode = await screen.findByRole('combobox', { name: 'Mode' })
    expect(await optionsOf(user, mode)).toEqual(['sandbox', 'live'])
    await choose(user, mode, 'live')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sent).toEqual({ settings: { mode: 'live' }, secrets: {} }))
  })

  it('preselects a field\'s default and shows an unavailable choice disabled, with why (#4960)', async () => {
    const withDefault = {
      id: 'picky',
      label: 'Picky',
      fields: [{ name: 'venmo', label: 'Venmo (USD)', secret: false, choices: ['off', 'on'], default: 'off', unavailable: { on: 'Needs the pay page' } }],
      connected: false,
      account_label: null,
      is_default: false,
      connected_at: null,
      last_webhook_at: null,
    }
    let sent: unknown = null
    server.use(
      http.get('/api/v1/payments/providers', () => HttpResponse.json([withDefault])),
      http.put('/api/v1/payments/providers/picky', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ...withDefault, connected: true, account_label: 'Picky' })
      }),
    )
    const { user } = renderSettingsSection(<Payments />, { route })
    const venmo = await screen.findByRole('combobox', { name: 'Venmo (USD)' })
    expect(venmo).toHaveTextContent('off')
    expect(await optionDisabled(user, venmo, /^on/)).toBe(true)
    expect(screen.getByText('on: Needs the pay page')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(sent).toEqual({ settings: { venmo: 'off' }, secrets: {} }))
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
    // The box is disabled until the settings load: keys typed before that are lost.
    await waitFor(() => expect(box).toBeEnabled())
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

