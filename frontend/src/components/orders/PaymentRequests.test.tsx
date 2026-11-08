import { screen, waitFor, within } from '@testing-library/react'
import { http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { mockConnectProvider, mockPaymentEvent } from '../../mocks/features/payments'
import { ORDER_ACCEPTED, ORDER_CLOSED, state } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { useAsync } from '../../lib/useAsync'
import { PaymentRequests } from './PaymentRequests'

function Host({ id }: { id: string }) {
  const view = useAsync(() => api.order(id), [id])
  if (!view.data) return null
  return <PaymentRequests order={view.data.order} requests={view.data.payment_requests ?? []} balanceMinor={view.data.totals.balance_minor} onChanged={view.refresh} />
}

// ORDER_ACCEPTED: items 30.00 and a 5.00 charge, so 35.00; 10.00 paid leaves 25.00.
async function setup(seed?: (openedId: string) => void) {
  mockConnectProvider()
  await api.addPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
  if (seed) seed((await api.requestPayment(ORDER_ACCEPTED, {})).id)
  return renderPage(<Host id={ORDER_ACCEPTED} />)
}

describe('PaymentRequests (#4957)', () => {
  beforeEach(() => resetMockState())

  it('requests the balance and shows the link to copy', async () => {
    const { user } = await setup()
    await user.click(await screen.findByRole('button', { name: 'Request payment' }))
    expect(screen.getByLabelText('Amount')).toHaveValue('25.00')
    await user.click(screen.getByRole('button', { name: 'Request' }))
    const list = await screen.findByRole('list', { name: 'Payment requests' })
    expect(within(list).getByText('$25.00')).toBeInTheDocument()
    // The provider's label arrives with the providers list, which can land after the requests.
    expect(await within(list).findByText('Fake')).toBeInTheDocument()
    expect(within(list).getByRole('button', { name: 'Copy link' })).toBeEnabled()
    expect(within(list).getByRole('link', { name: 'Email' })).toHaveAttribute('href', expect.stringMatching(/^mailto:/))
    expect(within(list).getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('requests through the default provider when the form opened before the providers loaded', async () => {
    // The flake behind #5729: the form took its provider from a list not yet loaded, kept
    // "by hand", and sent `provider: null` once the list arrived.
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('/api/v1/payments/providers', async () => {
        await held
        return undefined
      }),
    )
    const { user } = await setup()
    await user.click(await screen.findByRole('button', { name: 'Request payment' }))
    release()
    expect(await screen.findByRole('combobox', { name: 'Provider' })).toHaveTextContent('Fake')
    await user.click(screen.getByRole('button', { name: 'Request' }))
    const list = await screen.findByRole('list', { name: 'Payment requests' })
    expect(await within(list).findByText('Fake')).toBeInTheDocument()
    expect(within(list).queryByText('By hand')).not.toBeInTheDocument()
  })

  it('shows a paid request with the provider after its webhook, and no link', async () => {
    await setup((id) => mockPaymentEvent(ORDER_ACCEPTED, id, 'paid'))
    expect(await screen.findByText('Paid')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('shows a bank payment as pending until it settles, with no link and no cancel (#4958)', async () => {
    await setup((id) => mockPaymentEvent(ORDER_ACCEPTED, id, 'pending'))
    expect(await screen.findByText('Pending')).toBeInTheDocument()
    expect(screen.getByText('The bank payment is on its way')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('keeps the provider connected while a request is pending, as the backend does (#4958)', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, {})
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'pending')
    await expect(api.disconnectPaymentProvider('fake')).rejects.toThrow('open payment requests')
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'failed')
    await expect(api.disconnectPaymentProvider('fake')).resolves.toBeUndefined()
  })

  it('shows a bank payment that did not settle as failed (#4958)', async () => {
    await setup((id) => {
      mockPaymentEvent(ORDER_ACCEPTED, id, 'pending')
      mockPaymentEvent(ORDER_ACCEPTED, id, 'failed')
    })
    expect(await screen.findByText('Failed')).toBeInTheDocument()
    expect(screen.queryByText('The bank payment is on its way')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
  })

  it('says nothing is due as the backend says it', async () => {
    mockConnectProvider()
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 3500 })
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    await user.click(await screen.findByRole('button', { name: 'Request payment' }))
    await user.clear(screen.getByLabelText('Amount'))
    await user.click(screen.getByRole('button', { name: 'Request' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('has nothing due')
  })

  it('offers Retry link on a link error, and links the request again', async () => {
    const { user } = await setup((id) => {
      const row = state.paymentRequests.find((r) => r.id === id)!
      row.hosted_url = null
      row.link_error = 'the provider is down'
    })
    expect(await screen.findByText('the provider is down')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry link' }))
    expect(await screen.findByRole('button', { name: 'Copy link' })).toBeEnabled()
    expect(screen.queryByText('the provider is down')).not.toBeInTheDocument()
  })

  it('tells how to record a request collected by hand', async () => {
    mockConnectProvider()
    await api.requestPayment(ORDER_ACCEPTED, { provider: null })
    renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByText('Record the payment below when it arrives')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
  })

  it('cancels an open request', async () => {
    const { user } = await setup((id) => void id)
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument())
  })

  it('says when the provider has not yet confirmed a cancelled page closed (#5700)', async () => {
    await setup((id) => {
      const found = state.paymentRequests.find((r) => r.id === id)
      if (!found) throw new Error('no request')
      found.status = 'cancelled'
      found.close_pending_at = '2026-11-08T12:00:00Z'
    })
    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    // The provider's label arrives with the providers list, which can land after the requests.
    expect(await screen.findByText('Close not yet confirmed by Fake; ScadBuddy tries again')).toBeInTheDocument()
  })

  it('says nothing of the close once the provider confirmed it', async () => {
    const { user } = await setup((id) => void id)
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByText('Cancelled')).toBeInTheDocument()
    expect(screen.queryByText(/Close not yet confirmed/)).not.toBeInTheDocument()
  })

  it('offers no new request on a closed order', async () => {
    mockConnectProvider()
    renderPage(<Host id={ORDER_CLOSED} />)
    await screen.findByText('No payment requests yet.')
    expect(screen.queryByRole('button', { name: 'Request payment' })).not.toBeInTheDocument()
  })

  it('offers a By hand choice with one provider connected, and asks with no provider', async () => {
    const { user } = await setup()
    await user.click(await screen.findByRole('button', { name: 'Request payment' }))
    await choose(user, screen.getByRole('combobox', { name: 'Provider' }), 'By hand')
    await user.click(screen.getByRole('button', { name: 'Request' }))
    expect(await screen.findByText('Record the payment below when it arrives')).toBeInTheDocument()
    expect(state.paymentRequests.at(-1)?.provider).toBeNull()
  })

  it('shows no provider select with none connected, and the request is by hand', async () => {
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    await user.click(await screen.findByRole('button', { name: 'Request payment' }))
    expect(screen.queryByRole('combobox', { name: 'Provider' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Request' }))
    expect(await screen.findByText('Record the payment below when it arrives')).toBeInTheDocument()
  })

  describe('copying the link', () => {
    afterEach(() => vi.restoreAllMocks())

    it('says so when the browser refuses the copy', async () => {
      const { user } = await setup((id) => void id)
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.reject(new Error('denied')) } })
      await user.click(await screen.findByRole('button', { name: 'Copy link' }))
      expect(await screen.findByText('Copy failed, press Ctrl+C')).toBeInTheDocument()
    })

    it('says Copied when it works', async () => {
      const { user } = await setup((id) => void id)
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.resolve() } })
      await user.click(await screen.findByRole('button', { name: 'Copy link' }))
      expect(await screen.findByText('Copied')).toBeInTheDocument()
    })
  })
})
