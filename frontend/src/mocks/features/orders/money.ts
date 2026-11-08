import { HttpResponse, http } from 'msw'
import type { DepositRule, Order, OrderCharge, PaymentRequestView, ShippingOption } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { checkSubmitted, event, lock, notEnded, OPEN_FOR_EDITS, rederive, refused, TAKES_ITEMS, totalsOf, transition } from './orders'
import { BROWSER, fieldProblem, newId, now, Refused, state } from './state'

/**
 * Deposits, the balance, auto tax and shipping options (#5281, storefront spec §8-§9;
 * `backend/scadbuddy/orders/money.py`, `lifecycle.py` `_retax`, `_set_deposit`,
 * `_open_balance`, `_regate_balance`, `set_shipping_option`; routes in
 * `api/order_payments.py`). Every amount is in minor units; a percent is a decimal string,
 * rounded half up once, away from zero, as the backend's `ROUND_HALF_UP`.
 */

const DERIVED = new Set<Order['status']>(['accepted', 'in_production', 'ready', 'shipped'])

/** `money._percent_of`: `base * percent / 100`, rounded half up once. */
function percentOf(base: number, percent: string): number {
  const [whole, fraction = ''] = percent.trim().split('.')
  const scaled = BigInt(`${whole}${fraction}`)
  const den = 100n * 10n ** BigInt(fraction.length)
  const num = BigInt(Math.abs(base)) * scaled
  const rounded = Number((num * 2n + den) / (2n * den))
  return base < 0 ? -rounded : rounded
}

/** `lifecycle._plain`: a percent as a label shows it, with no trailing zeros. */
function plain(percent: string): string {
  const text = percent.trim()
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text
}

const chargesOf = (orderId: string) => state.charges.filter((c) => c.order_id === orderId)

/** `money.deposit_for`: the largest positive amount among the rules that apply, else null. */
export function depositFor(
  policy: readonly DepositRule[],
  { total, hasRequest, firstOrder, currency, shopCurrency }: { total: number; hasRequest: boolean; firstOrder: boolean; currency: string; shopCurrency: string },
): number | null {
  const same = currency === shopCurrency
  const amounts: number[] = []
  for (const rule of policy) {
    const applies = rule.criterion === 'has_request' ? hasRequest : rule.criterion === 'first_order' ? firstOrder : same && total > (rule.above_minor ?? 0)
    if (!applies) continue
    if (rule.percent != null) amounts.push(percentOf(total, rule.percent))
    else if (same && rule.amount_minor != null) amounts.push(rule.amount_minor)
  }
  const best = Math.max(0, ...amounts)
  return best > 0 ? best : null
}

/** `payments.insert_request` with its audit row, on the default provider. */
function openRequest(order: Order, kind: PaymentRequestView['kind'], amount: number): PaymentRequestView {
  const id = newId('pr')
  const provider = state.providers.find((p) => p.is_default)?.id ?? null
  const opened: PaymentRequestView = {
    id,
    order_id: order.id,
    kind,
    provider,
    amount_minor: amount,
    currency: order.currency,
    hosted_url: provider ? `https://pay.${provider}.example/${id}` : null,
    status: 'open',
    created_at: now(),
    paid_at: null,
    provider_ref: provider ? `${provider}_${id}` : null,
    link_error: null,
    payment_id: null,
    created_by: BROWSER,
  }
  state.paymentRequests.push(opened)
  event(order, 'payment_request.opened', { request_id: id, kind, amount_minor: amount })
  return opened
}

function cancelRequest(order: Order, request: PaymentRequestView) {
  request.status = 'cancelled'
  event(order, 'payment_request.cancelled', { request_id: request.id })
}

const requestsOf = (orderId: string) => state.paymentRequests.filter((r) => r.order_id === orderId)

/** `Lifecycle._regate_balance`: an accepted order whose deposit paid it whole, once its balance is above zero again. */
function regateBalance(order: Order) {
  if (!TAKES_ITEMS.has(order.status)) return
  const found = requestsOf(order.id)
  if (found.some((r) => r.kind === 'balance') || found.some((r) => ['open', 'pending', 'failed'].includes(r.status))) return
  if (order.deposit_minor == null && found.every((r) => r.status === 'cancelled')) return
  const due = totalsOf(order).balance_minor
  if (due > 0) openRequest(order, 'balance', due)
}

/**
 * `Lifecycle._retax`: the order's one auto tax row, `orders_tax_percent` of the items (plus
 * the shipping charges with `orders_tax_shipping`), while `draft` through `ready`. None at
 * 0 percent, at an amount of 0, or while a typed `tax` charge stands.
 */
export function retax(orderId: string): void {
  const order = state.orders.find((o) => o.id === orderId)
  if (!order || !(OPEN_FOR_EDITS.has(order.status) || TAKES_ITEMS.has(order.status))) return
  const settings = mockSettings()
  const percent = settings.orders_tax_percent ?? '0'
  const charges = chargesOf(order.id)
  const typed = charges.filter((c) => !c.auto)
  if (Number(percent) > 0 || typed.length < charges.length) {
    let amount = 0
    if (Number(percent) > 0 && !typed.some((c) => c.kind === 'tax')) {
      const shipping = typed.filter((c) => c.kind === 'shipping').reduce((sum, c) => sum + c.amount_minor, 0)
      amount = percentOf(totalsOf(order).subtotal_minor + (settings.orders_tax_shipping ? shipping : 0), percent)
    }
    const auto = charges.find((c) => c.auto)
    if (amount === 0) state.charges = state.charges.filter((c) => !(c.order_id === order.id && c.auto))
    else if (auto) Object.assign(auto, { label: `Tax ${plain(percent)}%`, amount_minor: amount })
    else state.charges.push({ id: newId('h'), order_id: order.id, kind: 'tax', label: `Tax ${plain(percent)}%`, amount_minor: amount, position: 0, auto: true })
  }
  regateBalance(order)
}

/**
 * `Lifecycle._typed`: the charges a replace writes, less the auto row sent back as read,
 * marked by `auto` or its `id`. Any other `tax` charge, whatever its label, is typed.
 */
export function typedCharges<T extends { id?: string | null; auto?: boolean | null }>(orderId: string, charges: T[]): T[] {
  const auto = chargesOf(orderId).find((c) => c.auto)
  return charges.filter((c) => !(c.auto || (auto && c.id === auto.id)))
}

/** `money.skipped_for_currency`: the rules `depositFor` skips only for the order's currency. */
function skippedForCurrency(policy: readonly DepositRule[], { hasRequest, firstOrder, currency, shopCurrency }: { hasRequest: boolean; firstOrder: boolean; currency: string; shopCurrency: string }): number[] {
  if (currency === shopCurrency) return []
  const applies = (rule: DepositRule) => (rule.criterion === 'has_request' ? hasRequest : firstOrder)
  return policy.flatMap((rule, index) => (rule.criterion === 'total_above' || (rule.amount_minor != null && applies(rule)) ? [index] : []))
}

/**
 * `Lifecycle._no_option_ships`: clearing the shipping address is refused while a shipping
 * option that ships stays on the order (the edit's `charges`, else the order's own).
 */
export function noOptionShips(order: Order, charges: { shipping_option_id?: string | null }[] | null | undefined) {
  const kept = charges ?? chargesOf(order.id)
  const named = new Set(kept.map((c) => c.shipping_option_id).filter((id): id is string => id != null))
  if (named.size === 0) return
  const ships = (mockSettings().storefront_shipping_options ?? []).filter((o) => named.has(o.id) && o.ships)
  if (ships.length > 0) {
    throw new Refused('shipping_address_required', `order ${order.number} ships by ${ships[0]!.label}: it keeps its shipping address; choose an option that does not ship, or remove the option, first`)
  }
}

/** `replace_charges`: the typed charges replaced whole; the auto row stays. */
export function replaceCharges(orderId: string, charges: { kind: OrderCharge['kind']; label: string; amount_minor: number; shipping_option_id?: string | null }[]) {
  state.charges = state.charges.filter((c) => c.order_id !== orderId || c.auto)
  charges.forEach((c, position) =>
    state.charges.push({ id: newId('h'), order_id: orderId, kind: c.kind, label: c.label, amount_minor: c.amount_minor, position, auto: false, shipping_option_id: c.shipping_option_id ?? null }),
  )
}

/** `Lifecycle._deposit_paid`: refused while a deposit is set, not waived and not paid. */
export function depositUnpaid(orderId: string): Refused | null {
  const order = lock(orderId)
  if (order.deposit_minor == null || order.deposit_waived_reason != null) return null
  const paid = totalsOf(order).paid_minor
  if (paid >= order.deposit_minor) return null
  return new Refused('deposit_unpaid', `order ${order.number}'s deposit is not paid: ${paid} of ${order.deposit_minor} received; record the payment or proceed without it`)
}

/** `totals.balance_due` over `has_standing_balance`. */
function balanceDue(order: Order): number {
  if (!requestsOf(order.id).some((r) => r.kind === 'balance' && r.status !== 'cancelled')) return 0
  return Math.max(totalsOf(order).balance_minor, 0)
}

/** `Lifecycle._balance_paid`: refused while a balance request stands, no one waived it, and the balance is above zero. */
export function balanceUnpaid(orderId: string): Refused | null {
  const order = lock(orderId)
  if (order.balance_waived_reason != null) return null
  const due = balanceDue(order)
  if (due <= 0) return null
  return new Refused('balance_unpaid', `order ${order.number}'s balance is not paid: ${due} due; record the payment or ship without full payment`)
}

/**
 * `Lifecycle._open_balance`: at acceptance, one open `balance` request for the rest of an
 * order that asked for money. `reopened`: a cancelled order back past quoted, whose cancel
 * ended every request, so any request at all says it asked.
 */
export function openBalance(order: Order, reopened = false) {
  const found = requestsOf(order.id)
  if (!reopened && found.some((r) => r.status === 'pending')) throw new Refused('payment_in_flight', `order ${order.number}'s payment is in flight; wait until it settles`)
  const asked = reopened ? found.length > 0 : found.some((r) => r.status !== 'cancelled')
  if (order.deposit_minor == null && !asked) return
  const due = totalsOf(order).balance_minor
  const live = found.filter((r) => r.status === 'open' || r.status === 'failed')
  const [only] = live
  if (live.length === 1 && only && only.status === 'open' && only.kind === 'balance' && only.amount_minor === due) return
  for (const request of live) cancelRequest(order, request)
  if (due > 0) openRequest(order, 'balance', due)
}

/** `Lifecycle._close_deposit`: once the payments reach the deposit, the open deposit requests are paid. */
export function closeDeposit(order: Order, paymentId: string) {
  if (order.deposit_minor == null || totalsOf(order).paid_minor < order.deposit_minor) return
  for (const r of requestsOf(order.id)) {
    if (r.kind === 'deposit' && r.status === 'open') {
      r.status = 'paid'
      r.paid_at = now()
      r.payment_id = paymentId
    }
  }
}

function depositEditable(order: Order) {
  checkSubmitted(order)
  notEnded(order)
  if (!OPEN_FOR_EDITS.has(order.status)) throw new Refused('order_not_editable', `order ${order.number} is ${order.status}: its deposit is set while draft or quoted`)
}

/** `Lifecycle._set_deposit`. */
function setDeposit(order: Order, amount: number | null, policy = false, skipped: number[] = []): Order {
  const deposits = requestsOf(order.id).filter((r) => r.kind === 'deposit')
  if (deposits.some((r) => r.status === 'pending')) throw new Refused('payment_in_flight', `order ${order.number}'s deposit payment is in flight; wait until it settles`)
  const due = amount === null ? 0 : amount - totalsOf(order).paid_minor
  const asking = deposits.filter((r) => r.status === 'open')
  const failed = deposits.filter((r) => r.status === 'failed')
  const unchanged =
    amount === (order.deposit_minor ?? null) &&
    order.deposit_waived_reason == null &&
    failed.length === 0 &&
    JSON.stringify(asking.map((r) => r.amount_minor)) === JSON.stringify(due > 0 ? [due] : [])
  if (unchanged) return order
  for (const r of [...asking, ...failed]) cancelRequest(order, r)
  order.deposit_minor = amount
  order.deposit_waived_reason = null
  order.updated_at = now()
  event(order, 'deposit.set', { amount_minor: amount, ...(policy ? { policy: true } : {}), ...(skipped.length > 0 ? { skipped } : {}) })
  if (due > 0) openRequest(order, 'deposit', due)
  return order
}

/** A waiver's reason, trimmed, 1 to 500 characters; else the 422 to answer. */
async function reasonOf(request: Request): Promise<string | Response> {
  const body = (await request.json().catch(() => ({}))) as { reason?: unknown }
  const reason = typeof body.reason === 'string' ? body.reason.trim() : ''
  if (reason.length < 1 || reason.length > 500) return fieldProblem(request, 'reason', 'a reason is 1 to 500 characters')
  return reason
}

export const handlers = [
  http.put('/api/v1/orders/:id/deposit', async ({ request, params }) => {
    const body = (await request.json()) as { amount_minor?: unknown }
    const amount = body.amount_minor
    if (amount !== null && !(typeof amount === 'number' && Number.isInteger(amount) && amount >= 1)) {
      return fieldProblem(request, 'amount_minor', 'Input should be greater than or equal to 1')
    }
    return transition(request, () => {
      const order = lock(String(params.id))
      depositEditable(order)
      return setDeposit(order, amount)
    })
  }),
  http.post('/api/v1/orders/:id/deposit/policy', ({ request, params }) =>
    transition(request, () => {
      const order = lock(String(params.id))
      depositEditable(order)
      const settings = mockSettings()
      const items = state.items.filter((i) => i.order_id === order.id)
      const policy = settings.orders_deposit_policy ?? []
      const context = {
        hasRequest: items.some((i) => i.kind === 'request' && i.status !== 'cancelled'),
        firstOrder: order.customer_id == null || !state.orders.some((o) => o.customer_id === order.customer_id && o.id !== order.id && o.accepted_at != null),
        currency: order.currency,
        shopCurrency: settings.orders_currency ?? 'USD',
      }
      const amount = depositFor(policy, { total: totalsOf(order).total_minor, ...context })
      const skipped = skippedForCurrency(policy, context)
      if (amount === null && skipped.length > 0) {
        throw new Refused(
          'deposit_policy_currency',
          `order ${order.number} is in ${order.currency}: the deposit policy's rules [${skipped.join(', ')}] that would apply are in ${context.shopCurrency}; set the deposit by hand`,
        )
      }
      return setDeposit(order, amount, true, skipped)
    }),
  ),
  http.post('/api/v1/orders/:id/deposit/waive', async ({ request, params }) => {
    const reason = await reasonOf(request)
    if (reason instanceof Response) return reason
    return transition(request, () => {
      const order = lock(String(params.id))
      checkSubmitted(order)
      notEnded(order)
      if (order.deposit_minor == null || order.deposit_waived_reason === reason) return order
      order.deposit_waived_reason = reason
      order.updated_at = now()
      event(order, 'deposit.waived', { reason })
      return order
    })
  }),
  http.post('/api/v1/orders/:id/balance/waive', async ({ request, params }) => {
    const reason = await reasonOf(request)
    if (reason instanceof Response) return reason
    return transition(request, () => {
      const order = lock(String(params.id))
      checkSubmitted(order)
      notEnded(order)
      if (!DERIVED.has(order.status)) throw new Refused('order_not_accepted', `order ${order.number} is ${order.status}, not accepted`)
      if (order.balance_waived_reason === reason || !balanceDue(order)) return order
      order.balance_waived_reason = reason
      order.updated_at = now()
      event(order, 'balance.waived', { reason })
      return order
    })
  }),
  http.put('/api/v1/orders/:id/shipping-option', async ({ request, params }) => {
    const body = (await request.json()) as { option_id?: string | null }
    try {
      const order = lock(String(params.id))
      // `check_visitors_cart`: the back office never sets a visitor's option.
      checkSubmitted(order)
      notEnded(order)
      if (!OPEN_FOR_EDITS.has(order.status)) {
        const route = TAKES_ITEMS.has(order.status) ? `; change the charges with PUT /orders/${order.id}/charges/after-acceptance` : ''
        throw new Refused('order_not_editable', `order ${order.number} is ${order.status}: its shipping option is set while draft or quoted${route}`)
      }
      const kept = chargesOf(order.id)
        .filter((c) => !c.auto && c.kind !== 'shipping')
        .sort((a, b) => a.position - b.position)
      const next: Parameters<typeof replaceCharges>[1] = kept.map((c) => ({ kind: c.kind, label: c.label, amount_minor: c.amount_minor }))
      const optionId = body.option_id ?? null
      if (optionId !== null) {
        const option = (mockSettings().storefront_shipping_options ?? []).find((o: ShippingOption) => o.id === optionId)
        if (!option) return fieldProblem(request, 'option_id', `no shipping option ${optionId} (shipping_option_not_found)`)
        if (option.ships && order.shipping_address_id == null) {
          throw new Refused('shipping_address_required', `order ${order.number} has no shipping address: ${option.label} ships it; set the address first, or choose an option that does not ship`)
        }
        next.push({ kind: 'shipping', label: option.label, amount_minor: option.amount_minor, shipping_option_id: option.id })
      }
      replaceCharges(order.id, next)
      event(order, 'shipping.option_set', { option_id: optionId })
      retax(order.id)
      rederive(order)
      order.updated_at = now()
      return HttpResponse.json(order)
    } catch (error) {
      return refused(request, error)
    }
  }),
]
