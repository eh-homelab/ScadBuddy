import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { emitRealtime } from '../../mocks/realtime'
import { resetMockState } from '../../mocks/handlers'
import { mockDepositSuggestion } from '../../mocks/features/orders/bankDeposits'
import { ORDER_ACCEPTED } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { useAsync } from '../../lib/useAsync'
import { DepositSuggestions } from './DepositSuggestions'

function Host({ id }: { id: string }) {
  const view = useAsync(() => api.order(id), [id])
  if (!view.data) return null
  return (
    <>
      <p data-testid="paid">{view.data.totals.paid_minor}</p>
      <DepositSuggestions order={view.data.order} onChanged={view.refresh} />
    </>
  )
}

describe('DepositSuggestions (#4961)', () => {
  beforeEach(() => resetMockState())

  it('shows nothing when no deposit is suggested', async () => {
    renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByTestId('paid')).toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Suggested bank deposits' })).not.toBeInTheDocument()
  })

  it('records a suggested deposit as a payment only when the shop confirms it', async () => {
    const deposit = mockDepositSuggestion(ORDER_ACCEPTED, { amount_minor: 2500, description: 'ZELLE FROM JANE ORD-2026-2' })
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Suggested bank deposits' })
    expect(within(list).getByText('$25.00')).toBeInTheDocument()
    expect(within(list).getByText(/ZELLE FROM JANE ORD-2026-2/)).toBeInTheDocument()
    expect(within(list).getByText(/matches the balance/)).toBeInTheDocument()
    // Shown, not recorded.
    expect(screen.getByTestId('paid')).toHaveTextContent('0')
    await user.click(within(list).getByRole('button', { name: 'Record this deposit as payment' }))
    expect(await screen.findByTestId('paid')).toHaveTextContent('2500')
    expect(screen.queryByRole('list', { name: 'Suggested bank deposits' })).not.toBeInTheDocument()
    const [payment] = (await api.order(ORDER_ACCEPTED)).payments
    expect(payment).toMatchObject({ method: 'bank_transfer', reference: deposit.transaction_id, amount_minor: 2500 })
  })

  it('hides a suggestion the shop says is not this order', async () => {
    mockDepositSuggestion(ORDER_ACCEPTED, { reason: 'order_number' })
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Suggested bank deposits' })
    expect(within(list).getByText(/names this order/)).toBeInTheDocument()
    await user.click(within(list).getByRole('button', { name: 'Not this order' }))
    expect(await screen.findByTestId('paid')).toHaveTextContent('0')
    expect(screen.queryByRole('list', { name: 'Suggested bank deposits' })).not.toBeInTheDocument()
  })

  it("shows the refusal when the deposit was recorded on another order first", async () => {
    const deposit = mockDepositSuggestion(ORDER_ACCEPTED)
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    const list = await screen.findByRole('list', { name: 'Suggested bank deposits' })
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 1, method: 'bank_transfer', reference: deposit.transaction_id })
    await user.click(within(list).getByRole('button', { name: 'Record this deposit as payment' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('already recorded')
  })

  it('shows a deposit a sync suggests while the page is open', async () => {
    renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByTestId('paid')).toBeInTheDocument()
    mockDepositSuggestion(ORDER_ACCEPTED, { amount_minor: 700 })
    await waitFor(() => {
      emitRealtime('order.bank_deposits', [`order:${ORDER_ACCEPTED}`], { order_id: ORDER_ACCEPTED })
      expect(screen.getByRole('list', { name: 'Suggested bank deposits' })).toHaveTextContent('$7.00')
    })
  })

  it('warns, with no action, when the bank changed a recorded deposit, until the payment adds up', async () => {
    const deposit = mockDepositSuggestion(ORDER_ACCEPTED, { reason: 'changed', amount_minor: 1200 })
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 1000, method: 'bank_transfer', reference: deposit.transaction_id })
    const { unmount } = renderPage(<Host id={ORDER_ACCEPTED} />)
    const warning = await screen.findByRole('status')
    expect(warning).toHaveTextContent('it now reads $12.00. Recorded: $10.00')
    expect(warning).toHaveTextContent(`reference ${deposit.transaction_id}`)
    expect(screen.queryByRole('button', { name: 'Record this deposit as payment' })).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Suggested bank deposits' })).not.toBeInTheDocument()
    unmount()
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 200, method: 'bank_transfer', reference: deposit.transaction_id })
    renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByTestId('paid')).toHaveTextContent('1200')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('warns when the bank removed a recorded deposit', async () => {
    const deposit = mockDepositSuggestion(ORDER_ACCEPTED, { reason: 'removed', amount_minor: 1000 })
    await api.addPayment(ORDER_ACCEPTED, { amount_minor: 1000, method: 'bank_transfer', reference: deposit.transaction_id })
    renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByRole('status')).toHaveTextContent('The bank removed a deposit recorded as a payment ($10.00)')
  })
})
