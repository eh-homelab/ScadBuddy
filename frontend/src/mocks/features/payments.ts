import { HttpResponse, http } from 'msw'
import type { CostEntry, Order, Payment, PaymentRequestView, ProviderConnect, ProviderStatus } from '../../api/types'
import { newId, now, problem, state } from './orders/state'
import { closeBalance, lock, settleRequest } from './orders/orders'

/**
 * #4957 — the payment providers (`api/payments.py`) with one provider, `fake`, and the
 * events a provider sends (`Lifecycle.record_provider_event`). A secret goes in and never
 * comes out: the mock keeps no secret at all, only that the shop connected the provider.
 */

const PROBLEMS = 'https://scadbuddy.dev/problems/payments/'

const FAKE = {
  id: 'fake',
  label: 'Fake',
  // The backend's fake captures an approval, as PayPal does (`Payments._reapprovable`).
  reapprovable: true,
  fields: [
    { name: 'account', label: 'Account', secret: false },
    { name: 'signing_secret', label: 'Signing secret', secret: true },
  ],
}

/** Whether `SCADBUDDY_SECRET_KEY_FILE` is set (`KekMissingError`). */
let kek = true
export const setMockKek = (present: boolean) => {
  kek = present
}
export function reset(): void {
  kek = true
}

function statusOf(id: string): ProviderStatus {
  const connected = state.providers.find((p) => p.id === id)
  return {
    ...FAKE,
    connected: connected !== undefined,
    account_label: connected?.account_label ?? null,
    is_default: connected?.is_default ?? false,
    connected_at: connected?.connected_at ?? null,
    last_webhook_at: connected?.last_webhook_at ?? null,
    last_webhook_error: connected?.last_webhook_error ?? null,
    last_webhook_error_at: connected?.last_webhook_error_at ?? null,
    currency: connected?.currency ?? null,
  }
}

const unknown = (request: Request, id: string) => problem(request, 404, `no connected payment provider '${id}'`, PROBLEMS + 'provider_not_found')

export const handlers = [
  http.get('/api/v1/payments/providers', () => HttpResponse.json([statusOf(FAKE.id)])),
  http.put('/api/v1/payments/providers/:id', async ({ request, params }) => {
    const id = String(params.id)
    if (id !== FAKE.id) return unknown(request, id)
    const body = (await request.json()) as ProviderConnect
    for (const field of FAKE.fields) {
      const given = (field.secret ? body.secrets : body.settings)?.[field.name]
      if (!given) {
        return problem(request, 422, `${field.name}: Field required`, 'about:blank', { errors: [{ loc: ['body', field.secret ? 'secrets' : 'settings', field.name], msg: 'Field required' }] })
      }
    }
    if (!kek) return problem(request, 409, 'payment credentials need SCADBUDDY_SECRET_KEY_FILE', PROBLEMS + 'kek_missing')
    // The first provider connected is the default (`is_default`, at most one row).
    const was = state.providers.find((p) => p.id === id)
    const row = { id, account_label: `Fake account ${body.settings.account}`, is_default: was?.is_default ?? state.providers.length === 0, connected_at: now(), last_webhook_at: was?.last_webhook_at ?? null, last_webhook_error: null, last_webhook_error_at: null, currency: null }
    state.providers = [...state.providers.filter((p) => p.id !== id), row]
    return HttpResponse.json(statusOf(id))
  }),
  http.delete('/api/v1/payments/providers/:id', ({ request, params }) => {
    const id = String(params.id)
    if (!state.providers.some((p) => p.id === id)) return unknown(request, id)
    // A pending request (#4958) still needs the provider's webhook secret, and a failed one's page can
    // still be approved (#4959), as `has_open_requests` says.
    if (state.paymentRequests.some((r) => r.provider === id && ['open', 'pending', 'failed'].includes(r.status))) {
      return problem(request, 409, `payment provider ${id} has open payment requests`, PROBLEMS + 'provider_has_open_requests')
    }
    state.providers = state.providers.filter((p) => p.id !== id)
    return new HttpResponse(null, { status: 204 })
  }),
  http.post('/api/v1/payments/providers/:id/default', ({ request, params }) => {
    const id = String(params.id)
    if (!state.providers.some((p) => p.id === id)) return unknown(request, id)
    for (const p of state.providers) p.is_default = p.id === id
    return new HttpResponse(null, { status: 204 })
  }),
]

/** Connect the fake provider as the settings form would, for a test's setup. */
export function mockConnectProvider(account = 'a', currency: string | null = null) {
  state.providers = [{ id: FAKE.id, account_label: `Fake account ${account}`, is_default: true, connected_at: now(), last_webhook_at: null, last_webhook_error: null, last_webhook_error_at: null, currency }]
}

/** #5684 — the fake provider's last webhook failed with `error` (connect it first). */
export function setMockWebhookError(error: NonNullable<ProviderStatus['last_webhook_error']>) {
  const provider = state.providers.find((p) => p.id === FAKE.id)
  if (!provider) throw new Error('connect the fake provider first')
  Object.assign(provider, { last_webhook_error: error, last_webhook_error_at: now() })
}

/** The fee the fake provider charges on a payment, in minor units. */
export const FAKE_FEE_MINOR = 30

/**
 * A provider's event on a request, as `Lifecycle._apply_event` records it: `paid` writes a
 * payment and the request's `paid`, closes the open and failed balance requests once the
 * balance is clear and writes the provider's fee entry; `refunded` writes a negative payment and
 * closes nothing; `expired` ends an open request. `pending` makes an open request pending
 * and `failed` ends an open or pending one (#4958); neither writes money. It is recorded on
 * a cancelled order too.
 */
export function mockPaymentEvent(orderId: string, requestId: string, kind: 'paid' | 'pending' | 'failed' | 'refunded' | 'expired'): void {
  const order: Order = lock(orderId)
  const found: PaymentRequestView | undefined = state.paymentRequests.find((r) => r.id === requestId && r.order_id === orderId)
  if (!found?.provider) throw new Error(`no provider request ${requestId}`)
  const provider = state.providers.find((p) => p.id === found.provider)
  // A webhook that is recorded clears the last failure (`Payments.handle_webhook`, #5684).
  if (provider) Object.assign(provider, { last_webhook_at: now(), last_webhook_error: null, last_webhook_error_at: null })
  const by = { kind: 'provider' as const, principal: `payment:${found.provider}`, session: null }
  if (kind === 'pending' || kind === 'failed') {
    if (found.status === 'open' || (kind === 'failed' && found.status === 'pending')) {
      settleRequest(found, kind)
      state.events.push({ id: state.events.length + 1, order_id: order.id, at: now(), by, kind: `payment_request.${kind}`, detail: { request_id: found.id } })
    }
    return
  }
  if (kind === 'expired') {
    if (found.status === 'open') {
      settleRequest(found, 'expired')
      state.events.push({ id: state.events.length + 1, order_id: order.id, at: now(), by, kind: 'payment_request.expired', detail: { request_id: found.id } })
    }
    return
  }
  const paidBy = found.status === 'paid' && state.payments.some((p) => p.id === found.payment_id && p.provider === found.provider && p.reference === found.provider_ref)
  if (kind === 'paid' && paidBy) return
  const payment: Payment = {
    id: newId('p'),
    order_id: order.id,
    amount_minor: kind === 'paid' ? found.amount_minor : -found.amount_minor,
    method: found.provider,
    reference: found.provider_ref,
    received_at: null,
    notes: '',
    created_at: now(),
    payment_request_id: found.id,
    provider: found.provider,
  }
  state.payments.push(payment)
  state.events.push({ id: state.events.length + 1, order_id: order.id, at: now(), by, kind: 'payment.added', detail: { payment_id: payment.id, payment_request_id: found.id, provider: found.provider } })
  if (kind === 'refunded') return
  // A request already paid keeps the payment that paid it first.
  if (found.status !== 'paid') settleRequest(found, 'paid', payment.id)
  closeBalance(order, payment)
  const fee: CostEntry = {
    id: newId('ce'),
    order_id: order.id,
    order_item_id: null,
    kind: 'payment_fee',
    amount_minor: FAKE_FEE_MINOR,
    note: `${found.provider} fee`,
    source: `payment:${payment.id}`,
    created_by: by,
    created_at: now(),
  }
  state.costEntries.push(fee)
  state.events.push({ id: state.events.length + 1, order_id: order.id, at: now(), by, kind: 'cost.added', detail: { entry_id: fee.id, item_id: null, entry_kind: fee.kind, amount_minor: fee.amount_minor } })
}
