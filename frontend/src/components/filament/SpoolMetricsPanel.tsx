import { useState } from 'react'
import type { SpoolHistory } from '../../api/filament'
import { when } from './formClasses'
import { useMoney } from './purchasing/useMoney'

const grams = (value: number) => `${Number(value.toFixed(1))} g`
const signed = (value: number) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${grams(Math.abs(value))}`
const days = (value: number) => {
  const whole = Math.floor(value)
  return whole === 0 ? 'Today' : `${whole} ${whole === 1 ? 'day' : 'days'} ago`
}
/** A calendar day (`YYYY-MM-DD`) as a person reads it, on that day wherever they are. */
const day = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
const DAY_MS = 86_400_000
const pairs = (count: number) => `${count} ${count === 1 ? 'pair' : 'pairs'}`

interface Tile {
  label: string
  /** `null` shows "—", and `why` says why. */
  value: string | null
  why?: string
  note?: string
  /** Shown whether the value is set or not. */
  also?: string | null
}

/**
 * #6269 — one spool's metrics from its history: use, burn rate and forecast, waste, cost,
 * age and the AMS reading error. A metric with no value shows "—" and a line that says
 * why; the burn window is the server's (`window_days`), never a fixed number.
 */
export function SpoolMetricsPanel({ history, gramsLeft, createdAt }: { history: SpoolHistory; gramsLeft: number | null; createdAt: string }) {
  const money = useMoney()
  const { metrics, window_days: window, type_ams_error: typeError } = history
  const noPrints = `No prints in the last ${window} days`
  const burn = metrics.burn_g_per_day
  // `metrics.burn` divides by the spool's age from `created_at` when that is shorter than the window (at least a day).
  const [now] = useState(() => Date.now())
  const age = (now - Date.parse(createdAt)) / DAY_MS
  const aged = Math.max(1, Math.round(age))
  const burnOver = age < window ? `Since it was added ${aged} ${aged === 1 ? 'day' : 'days'} ago` : `Over the last ${window} days`
  const tiles: Tile[] = [
    {
      label: 'Used',
      value: grams(metrics.used_g),
      note: metrics.prints === 0 ? 'No prints yet' : `${metrics.prints} ${metrics.prints === 1 ? 'print' : 'prints'}${metrics.grams_per_print != null ? `, ${grams(metrics.grams_per_print)} each` : ''}`,
    },
    { label: 'Burn rate', value: burn == null ? null : `${grams(burn)} a day`, why: noPrints, note: burnOver },
    {
      label: 'Empty around',
      value: metrics.empty_on ? day(metrics.empty_on) : null,
      why: burn == null ? noPrints : burn <= 0 ? `No filament used in the last ${window} days` : gramsLeft == null ? 'The grams left are not known' : 'More than ten years away at this rate',
    },
    {
      label: 'Waste',
      value: grams(metrics.wasted_g),
      note: metrics.used_g > 0 ? `${Math.round((metrics.wasted_g / metrics.used_g) * 100)}% of what it used, in failed and cancelled prints` : 'From failed and cancelled prints',
    },
    { label: 'Cost used', value: metrics.cost_used == null ? null : money.format(metrics.cost_used, history.currency), why: 'No cost per kg is set' },
    {
      label: 'Value left',
      value: metrics.value_left == null ? null : money.format(metrics.value_left, history.currency),
      why: gramsLeft == null ? 'The grams left are not known' : 'No cost per kg is set',
    },
    {
      label: 'Opened',
      value: metrics.opened_at ? when(metrics.opened_at) : null,
      why: 'Never loaded or printed',
      note: metrics.days_since_last_use != null ? `Last used: ${days(metrics.days_since_last_use).toLowerCase()}` : 'Never used',
    },
    {
      label: 'AMS error',
      value: metrics.ams_error ? `±${grams(metrics.ams_error.mean_abs_g)}` : null,
      why: 'No AMS reading is paired with a scale reading yet',
      note: metrics.ams_error ? `${signed(metrics.ams_error.bias_g)} bias over ${pairs(metrics.ams_error.pairs)}` : undefined,
      also: typeError ? `Its type: ±${grams(typeError.mean_abs_g)} over ${pairs(typeError.pairs)}` : null,
    },
  ]
  return (
    <dl aria-label="Spool metrics" className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
      {tiles.map((tile) => (
        <div key={tile.label} className="flex min-w-0 flex-col gap-0.5 border-l-2 border-border pl-3">
          <dt className="text-[12px] text-muted-foreground">{tile.label}</dt>
          <dd className="text-[15px] font-semibold tracking-tight tabular-nums">{tile.value ?? '—'}</dd>
          {tile.value == null && tile.why ? (
            <dd className="text-[12px] text-muted-foreground">{tile.why}</dd>
          ) : (
            tile.note && <dd className="text-[12px] text-muted-foreground">{tile.note}</dd>
          )}
          {tile.also && <dd className="text-[12px] text-muted-foreground">{tile.also}</dd>}
        </div>
      ))}
    </dl>
  )
}
