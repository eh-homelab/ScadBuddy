import { http, HttpResponse } from 'msw'
import type { UnitCostHistory } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { fieldProblem, minorExponent } from './state'

/**
 * #5328, job cost spec §6.3 — `GET /orders/history/unit-cost` (`backend/scadbuddy/orders/history.py`).
 * The mock keeps no jobs of its own: a test seeds a model's history with `seedUnitCost`
 * (its real cost per unit, jobs and main reason), and the suggestion and the warning are
 * worked out from the settings as the backend does. A model with none has no history.
 */

type Seed = Pick<UnitCostHistory, 'jobs' | 'units' | 'unit_cost_minor' | 'estimated' | 'main_reason' | 'main_reason_minor'>

let seeded = new Map<string, Seed>()

export function seedUnitCost(slug: string, seed: Seed): void {
  seeded.set(slug, seed)
}

export function reset(): void {
  seeded = new Map()
}

function suggested(cost: number, markup: string, thin: string): number {
  if (Number(markup) > 0) return Math.round((cost * (100 + Number(markup))) / 100)
  const t = Number(thin)
  return t <= 0 || t >= 100 ? cost : Math.ceil((cost * 100) / (100 - t))
}

export const handlers = [
  http.get('/api/v1/orders/history/unit-cost', ({ request }) => {
    const query = new URL(request.url).searchParams
    const slug = query.get('slug')
    if (!slug) return fieldProblem(request, 'slug', 'Field required')
    const settings = mockSettings()
    const currency = query.get('currency') ?? settings.orders_currency
    const price = query.get('unit_price_minor')
    const markup = settings.pricing_markup_percent
    const thin = settings.orders_thin_margin_percent
    const seed = seeded.get(slug)
    const answer: UnitCostHistory = {
      slug,
      currency,
      minor_exponent: minorExponent(currency),
      markup_percent: markup,
      thin_margin_percent: thin,
      unit_price_minor: price === null ? null : Number(price),
      jobs: 0,
      units: 0,
      unit_cost_minor: null,
      estimated: false,
      main_reason: null,
      main_reason_minor: null,
      suggested_unit_price_minor: null,
      margin_percent: null,
      warning: false,
      below_cost: false,
    }
    if (!seed || seed.unit_cost_minor == null) return HttpResponse.json(answer)
    const cost = seed.unit_cost_minor
    Object.assign(answer, seed, { suggested_unit_price_minor: suggested(cost, markup, thin) })
    if (price !== null) {
      const unit = Number(price)
      const percent = unit > 0 ? ((unit - cost) * 100) / unit : null
      answer.margin_percent = percent === null ? null : Math.round(percent * 10) / 10
      answer.warning = percent === null ? cost > 0 : percent < Number(thin)
      answer.below_cost = unit < cost
    }
    return HttpResponse.json(answer)
  }),
]
