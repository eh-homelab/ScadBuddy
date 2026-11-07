import { http, HttpResponse } from 'msw'
import type { ItemCost, OrderCosts, OrderItem, ProfitReport, ProfitRow } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { DEFAULT_DAYS } from '../../../components/orders/profit'
import { costsOf } from './costs'
import { fieldProblem, minorExponent, problem, state, UNKNOWN_PARAMETER } from './state'

/**
 * #5329, job cost spec §6.4 — `GET /orders/reports/profit` (`backend/scadbuddy/orders/report.py`):
 * the orders accepted in the period, per currency and product, each item's figures from
 * `costsOf` (the mock's per-order read). `MAX_REPORT_ORDERS` mirrors the backend's.
 */

export const MAX_REPORT_ORDERS = 2000
const DAY = 24 * 60 * 60 * 1000
const PARAMS = new Set(['from', 'to', 'group'])

type Part = 'prints' | 'labour'

/** `report.py` `product_of`: a slug first, then a SKU, else `custom` (an empty SKU is none). */
export function productOf(item: Pick<OrderItem, 'reference' | 'sku'>): string {
  if (item.reference) return `model:${item.reference.slug}`
  if (item.sku) return `sku:${item.sku}`
  return 'custom'
}

class Sum {
  orders = new Set<string>()
  units = 0
  revenue = 0
  charged = false
  uncharged = 0
  cost = 0
  waste = 0
  estimated = false
  unknown: Part[] = []

  add(orderId: string, f: { cost: number; revenue: number | null; units?: number; uncharged?: number; waste?: number; estimated?: boolean; unknown?: Part[] }) {
    this.orders.add(orderId)
    this.units += f.units ?? 0
    this.cost += f.cost
    this.waste += f.waste ?? 0
    this.uncharged += f.uncharged ?? 0
    if (f.revenue != null) {
      this.revenue += f.revenue
      this.charged = true
    }
    this.estimated ||= f.estimated ?? false
    for (const part of f.unknown ?? []) if (!this.unknown.includes(part)) this.unknown.push(part)
  }

  row(group: ProfitRow['group'], currency: string, thin: number, product: string | null): ProfitRow {
    const revenue = this.charged ? this.revenue : null
    let margin: Pick<ProfitRow, 'margin_minor' | 'margin_percent' | 'margin_state'> = { margin_minor: null, margin_percent: null, margin_state: null }
    if (revenue != null) {
      const minor = revenue - this.cost
      const exact = revenue > 0 ? (minor * 100) / revenue : null
      margin = {
        margin_minor: minor,
        margin_percent: exact === null ? null : Math.round(exact * 10) / 10,
        margin_state: minor < 0 ? 'loss' : this.unknown.length ? 'unknown' : exact === null ? null : exact < thin ? 'thin' : 'ok',
      }
    }
    return {
      group,
      key: product && product !== 'custom' ? product.slice(product.indexOf(':') + 1) : null,
      product,
      currency,
      minor_exponent: minorExponent(currency),
      orders: this.orders.size,
      units: this.units,
      revenue_minor: this.revenue,
      uncharged_items: this.uncharged,
      cost_minor: this.cost,
      ...margin,
      waste_minor: this.waste,
      waste_percent: this.cost > 0 ? Math.round((this.waste * 1000) / this.cost) / 10 : null,
      estimated: this.estimated,
      unknown: this.unknown,
    }
  }
}

const waste = (line: ItemCost) => line.attempts.filter((a) => a.purpose === 'test' || a.outcome === 'failed').reduce((s, a) => s + (a.cost_minor ?? 0), 0)

class Currency {
  products = new Map<string, Sum>()
  order = new Sum()
  total = new Sum()

  add(costs: OrderCosts) {
    const id = costs.order_id
    for (const line of costs.items) {
      const item = state.items.find((i) => i.id === line.item_id)
      if (!item) continue
      const product = productOf(item)
      const sums = this.products.get(product) ?? new Sum()
      this.products.set(product, sums)
      const open = line.status !== 'cancelled'
      sums.add(id, {
        cost: line.cost_minor ?? 0,
        revenue: line.revenue_minor ?? null,
        units: open ? line.quantity : 0,
        uncharged: open && line.revenue_minor == null ? 1 : 0,
        waste: waste(line),
        estimated: (line.estimated ?? []).length > 0,
        unknown: line.unknown ?? [],
      })
    }
    const itemsRevenue = costs.items.reduce((s, l) => s + (l.revenue_minor ?? 0), 0)
    const itemsCost = costs.items.reduce((s, l) => s + (l.cost_minor ?? 0), 0)
    const own = (costs.unknown ?? []).filter((p) => !costs.items.some((l) => (l.unknown ?? []).includes(p)))
    const ownCost = (costs.cost_minor ?? 0) - itemsCost
    const ownRevenue = costs.revenue_minor == null ? null : costs.revenue_minor - itemsRevenue
    // Only an order with figures of its own is behind the `order` row.
    if (ownCost !== 0 || ownRevenue || own.length) this.order.add(id, { cost: ownCost, revenue: ownRevenue, unknown: own })
    this.total.add(id, {
      cost: costs.cost_minor ?? 0,
      revenue: costs.revenue_minor ?? null,
      units: costs.items.filter((l) => l.status !== 'cancelled').reduce((s, l) => s + l.quantity, 0),
      uncharged: costs.items.filter((l) => l.status !== 'cancelled' && l.revenue_minor == null).length,
      waste: costs.items.reduce((s, l) => s + waste(l), 0),
      estimated: (costs.estimated ?? []).length > 0,
      unknown: costs.unknown ?? [],
    })
  }

  rows(currency: string, thin: number): ProfitRow[] {
    const products = [...this.products].map(([product, sums]) => sums.row(product.split(':')[0] as ProfitRow['group'], currency, thin, product))
    products.sort((a, b) => b.revenue_minor - a.revenue_minor || a.group.localeCompare(b.group) || (a.key ?? '').localeCompare(b.key ?? ''))
    const shown = this.order.orders.size > 0
    return [...products, ...(shown ? [this.order.row('order', currency, thin, null)] : []), this.total.row('total', currency, thin, null)]
  }
}

export const handlers = [
  http.get('/api/v1/orders/reports/profit', ({ request }) => {
    const sp = new URL(request.url).searchParams
    for (const name of sp.keys()) if (!PARAMS.has(name)) return problem(request, 400, `unknown query parameter '${name}'`, UNKNOWN_PARAMETER)
    const group = sp.get('group') ?? 'model'
    if (group !== 'model') return fieldProblem(request, 'group', "Input should be 'model'")
    const to = sp.get('to') ?? new Date().toISOString()
    const from = sp.get('from') ?? new Date(Date.parse(to) - DEFAULT_DAYS * DAY).toISOString()
    if (Number.isNaN(Date.parse(from)) || Number.isNaN(Date.parse(to))) return problem(request, 422, 'from and to are date-times')
    if (Date.parse(from) >= Date.parse(to)) return problem(request, 422, 'from must be before to')
    const orders = state.orders
      .filter((o) => o.accepted_at != null && Date.parse(o.accepted_at) >= Date.parse(from) && Date.parse(o.accepted_at) < Date.parse(to))
      .sort((a, b) => (a.accepted_at === b.accepted_at ? (a.id < b.id ? 1 : -1) : (a.accepted_at ?? '') < (b.accepted_at ?? '') ? 1 : -1))
    const read = orders.slice(0, MAX_REPORT_ORDERS)
    const thinText = mockSettings().orders_thin_margin_percent ?? '15'
    const currencies = new Map<string, Currency>()
    for (const order of read) {
      const costs = costsOf(order.id)
      if (!costs) continue
      const sums = currencies.get(order.currency) ?? new Currency()
      currencies.set(order.currency, sums)
      sums.add(costs)
    }
    const complete = orders.length <= MAX_REPORT_ORDERS
    const answer: ProfitReport = {
      group: 'model',
      period_from: from,
      period_to: to,
      thin_margin_percent: thinText,
      rows: [...currencies.keys()].sort().flatMap((c) => currencies.get(c)?.rows(c, Number(thinText)) ?? []),
      orders_read: read.length,
      complete,
      read_from: complete ? null : (read.at(-1)?.accepted_at ?? null),
      max_orders: MAX_REPORT_ORDERS,
      default_days: DEFAULT_DAYS,
    }
    return HttpResponse.json(answer)
  }),
]
