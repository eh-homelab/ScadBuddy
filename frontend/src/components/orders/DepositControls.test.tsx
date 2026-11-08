import { screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { depositUnpaid } from '../../mocks/features/orders/money'
import { ORDER_ACCEPTED, ORDER_DRAFT, item, state } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { useAsync } from '../../lib/useAsync'
import { DepositControls } from './DepositControls'

function Host({ id }: { id: string }) {
  const view = useAsync(() => api.order(id), [id])
  if (!view.data) return null
  return <DepositControls order={view.data.order} totals={view.data.totals} requests={view.data.payment_requests ?? []} onChanged={view.refresh} />
}

describe('DepositControls (#5281)', () => {
  beforeEach(() => resetMockState())

  it('sets a deposit, shows it due, and blocks a send until it is paid', async () => {
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    await user.type(await screen.findByLabelText('Deposit (USD)'), '20')
    await user.click(screen.getByRole('button', { name: 'Set deposit' }))
    expect(await screen.findByText('Deposit due $20.00')).toBeInTheDocument()
    // The deposit request asks for it, and a send waits for it (`deposit_unpaid`).
    expect(state.paymentRequests.filter((r) => r.order_id === ORDER_DRAFT && r.kind === 'deposit' && r.status === 'open').map((r) => r.amount_minor)).toEqual([2000])
    expect(depositUnpaid(ORDER_DRAFT)?.slug).toBe('deposit_unpaid')

    await api.addPayment(ORDER_DRAFT, { amount_minor: 2000 })
    expect(depositUnpaid(ORDER_DRAFT)).toBeNull()
  })

  it('clears the deposit', async () => {
    await api.setDeposit(ORDER_DRAFT, 1500)
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    expect(await screen.findByText('Deposit due $15.00')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear deposit' }))
    expect(await screen.findByText('No deposit.')).toBeInTheDocument()
    expect(state.paymentRequests.filter((r) => r.order_id === ORDER_DRAFT).map((r) => r.status)).toEqual(['cancelled'])
  })

  it('asks for a reason before proceeding without the deposit', async () => {
    await api.setDeposit(ORDER_DRAFT, 2000)
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    await user.click(await screen.findByRole('button', { name: 'Proceed without deposit' }))
    expect(screen.getByRole('button', { name: 'Proceed' })).toBeDisabled()
    await user.type(screen.getByLabelText('Reason'), 'regular customer')
    await user.click(screen.getByRole('button', { name: 'Proceed' }))
    expect(await screen.findByText(/Waived: regular customer/)).toBeInTheDocument()
    expect(depositUnpaid(ORDER_DRAFT)).toBeNull()
  })

  it('applies the policy', async () => {
    setMockSettings({ ...mockSettings(), orders_deposit_policy: [{ criterion: 'has_request', amount_minor: 2000 }] })
    state.items.push(item('7b1c2d3e-4f50-4617-8829-aabbccddee09', ORDER_DRAFT, 'draft', { kind: 'request', reference: null, current_revision_id: null, output_id: null, position: 1 }))
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    await user.click(await screen.findByRole('button', { name: 'Use the policy' }))
    expect(await screen.findByText('Deposit due $20.00')).toBeInTheDocument()
  })

  it('ships without full payment once accepted, for a reason', async () => {
    // ORDER_ACCEPTED: 35.00 due, and a balance request for it holds the shipment.
    await api.requestPayment(ORDER_ACCEPTED, { kind: 'balance' })
    const { user } = renderPage(<Host id={ORDER_ACCEPTED} />)
    expect(await screen.findByText('The unpaid balance holds the shipment.')).toBeInTheDocument()
    // No deposit is set once accepted.
    expect(screen.queryByLabelText('Deposit (USD)')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Ship without full payment' }))
    const confirm = screen.getByRole('button', { name: 'Ship unpaid' })
    expect(confirm).toBeDisabled()
    await user.type(screen.getByLabelText('Reason'), 'pays on delivery')
    await user.click(confirm)
    expect(await screen.findByText(/Ships without full payment: pays on delivery/)).toBeInTheDocument()
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_ACCEPTED)?.balance_waived_reason).toBe('pays on delivery'))
  })

  // Review fix round 1, minor 2: a shipped order has no shipment left to hold.
  it('offers no Ship without full payment once shipped', async () => {
    await api.requestPayment(ORDER_ACCEPTED, { kind: 'balance' })
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'shipped'
    renderPage(<Host id={ORDER_ACCEPTED} />)
    await screen.findByTestId('deposit-state')
    expect(screen.queryByRole('button', { name: 'Ship without full payment' })).toBeNull()
  })

  // Review fix round 1, minor 3: a cancelled reason is not carried into the next waiver.
  it('forgets a reason typed before Cancel', async () => {
    await api.setDeposit(ORDER_DRAFT, 2000)
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    await user.click(await screen.findByRole('button', { name: 'Proceed without deposit' }))
    await user.type(screen.getByLabelText('Reason'), 'not this one')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByLabelText('Reason')).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Proceed without deposit' }))
    expect(screen.getByLabelText('Reason')).toHaveValue('')
  })
})

