import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
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
    expect(within(list).getByText('Fake')).toBeInTheDocument()
    expect(within(list).getByRole('button', { name: 'Copy link' })).toBeEnabled()
    expect(within(list).getByRole('link', { name: 'Email' })).toHaveAttribute('href', expect.stringMatching(/^mailto:/))
    expect(within(list).getByRole('button', { name: 'Cancel' })).toBeEnabled()
  })

  it('shows a paid request with the provider after its webhook, and no link', async () => {
    await setup((id) => mockPaymentEvent(ORDER_ACCEPTED, id, 'paid'))
    expect(await screen.findByText('Paid')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Copy link' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
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
