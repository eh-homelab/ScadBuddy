import { HttpResponse, http } from 'msw'
import type { DepositSuggestion, Payment } from '../../../api/types'
import { closeBalance, event, lock, refused } from './orders'
import { newId, now, Refused, state } from './state'

/**
 * #4961 — the deposits of the Plaid-linked account suggested for an order
 * (`api/order_bank_deposits.py`). Recording one adds a `bank_transfer` payment whose reference is
 * the transaction id, once; dismissing one hides it.
 */

type Suggested = DepositSuggestion & { order_id: string; dismissed: boolean }
let suggested: Suggested[] = []

export function reset(): void {
  suggested = []
}

/** A deposit the sync suggested for `orderId`, for a test's setup. */
export function mockDepositSuggestion(orderId: string, deposit: Partial<DepositSuggestion> = {}): DepositSuggestion {
  const made: DepositSuggestion = {
    transaction_id: newId('tx'),
    amount_minor: 1000,
    currency: 'USD',
    posted_on: '2026-11-06',
    description: 'ACH CREDIT ACCT •••',
    reason: 'amount',
    ...deposit,
  }
  suggested.push({ ...made, order_id: orderId, dismissed: false })
  return made
}

const recorded = (transactionId: string) => state.payments.some((p) => p.method === 'bank_transfer' && p.reference === transactionId)

function suggestionOf(orderId: string, transactionId: string): Suggested {
  const found = suggested.find((s) => s.order_id === orderId && s.transaction_id === transactionId && !s.dismissed)
  if (!found) throw new Refused('deposit_not_suggested', `deposit ${transactionId} is not suggested for order ${orderId}`, [], true)
  return found
}

export const handlers = [
  http.get('/api/v1/orders/:id/bank-deposits', ({ request, params }) => {
    try {
      lock(String(params.id))
    } catch (error) {
      return refused(request, error)
    }
    const shown = suggested.filter((s) => s.order_id === params.id && !s.dismissed && !recorded(s.transaction_id))
    return HttpResponse.json(shown.map(({ order_id: _order, dismissed: _dismissed, ...deposit }) => deposit))
  }),
  http.post('/api/v1/orders/:id/bank-deposits/:tx/record', ({ request, params }) => {
    try {
      const order = lock(String(params.id))
      const found = suggestionOf(order.id, String(params.tx))
      if (recorded(found.transaction_id)) throw new Refused('deposit_recorded', `deposit ${found.transaction_id} is already recorded`)
      if (order.status === 'cancelled') throw new Refused('order_cancelled', `order ${order.number} is cancelled`)
      const payment: Payment = {
        id: newId('p'),
        order_id: order.id,
        amount_minor: found.amount_minor,
        method: 'bank_transfer',
        reference: found.transaction_id,
        received_at: `${found.posted_on}T12:00:00Z`,
        notes: found.description,
        created_at: now(),
        payment_request_id: null,
        provider: null,
      }
      state.payments.push(payment)
      closeBalance(order, payment)
      event(order, 'payment.added', { payment_id: payment.id, payment_request_id: null })
      return HttpResponse.json(payment, { status: 201 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.post('/api/v1/orders/:id/bank-deposits/:tx/dismiss', ({ request, params }) => {
    try {
      suggestionOf(String(params.id), String(params.tx)).dismissed = true
      return new HttpResponse(null, { status: 204 })
    } catch (error) {
      return refused(request, error)
    }
  }),
]
