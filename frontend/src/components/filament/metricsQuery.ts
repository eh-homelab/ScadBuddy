/**
 * #6269 — the Metrics tab's view (plan Decision 19): a range, a bucket and a dimension,
 * kept in the address (`?range=&bucket=&dimension=`, each left out at its default). With
 * "Remember my view" on, a visit with no query opens the last view; the browser keeps it
 * under `scadbuddy.filament.metrics.view`. Storage that throws (private mode, a sandboxed
 * frame) leaves the tab working without it.
 */
import type { MetricsQueryParams } from '../../api/filament'

export type MetricsRange = '90d' | '180d' | '365d'
export type MetricsBucket = 'week' | 'month'
/** The backend's `metrics_models.Dimension`. */
export type Dimension = 'material' | 'brand' | 'type' | 'supplier' | 'location'

export interface MetricsQuery {
  range: MetricsRange
  bucket: MetricsBucket
  dimension: Dimension
}

export const RANGES: readonly MetricsRange[] = ['90d', '180d', '365d']
export const BUCKETS: readonly MetricsBucket[] = ['week', 'month']
export const DIMENSIONS: readonly Dimension[] = ['material', 'brand', 'type', 'supplier', 'location']

export const DEFAULT_METRICS_QUERY: MetricsQuery = { range: '180d', bucket: 'month', dimension: 'material' }

export const METRICS_VIEW_KEY = 'scadbuddy.filament.metrics.view'

const pick = <T extends string>(value: unknown, allowed: readonly T[], fallback: T): T =>
  typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : fallback

/** The view in the address; an unknown value falls back to its default. */
export function parseMetricsQuery(params: URLSearchParams): MetricsQuery {
  return {
    range: pick(params.get('range'), RANGES, DEFAULT_METRICS_QUERY.range),
    bucket: pick(params.get('bucket'), BUCKETS, DEFAULT_METRICS_QUERY.bucket),
    dimension: pick(params.get('dimension'), DIMENSIONS, DEFAULT_METRICS_QUERY.dimension),
  }
}

/** The view as the address's parameters, leaving out each default. */
export function metricsParams(q: MetricsQuery): URLSearchParams {
  const params = new URLSearchParams()
  for (const key of ['range', 'bucket', 'dimension'] as const) if (q[key] !== DEFAULT_METRICS_QUERY[key]) params.set(key, q[key])
  return params
}

/** The days a range covers. */
export const rangeDays = (range: MetricsRange) => Number(range.slice(0, -1))

/** The routes' `since` and `until` (UTC days, both included) for the view's range, ending on `today`. */
export function metricsRange(q: MetricsQuery, now: Date = new Date()): Required<Pick<MetricsQueryParams, 'since' | 'until'>> {
  const until = now.toISOString().slice(0, 10)
  const since = new Date(Date.parse(`${until}T00:00:00Z`) - rangeDays(q.range) * 86_400_000).toISOString().slice(0, 10)
  return { since, until }
}

interface Memory {
  on: boolean
  query: MetricsQuery
}

function readMemory(): Memory | null {
  try {
    const raw = window.localStorage.getItem(METRICS_VIEW_KEY)
    if (!raw) return null
    const stored = JSON.parse(raw) as { on?: unknown; query?: Record<string, unknown> }
    const params = new URLSearchParams()
    for (const key of ['range', 'bucket', 'dimension'] as const) {
      const value = stored.query?.[key]
      if (typeof value === 'string') params.set(key, value)
    }
    return { on: stored.on === true, query: parseMetricsQuery(params) }
  } catch {
    return null
  }
}

function writeMemory(memory: Memory) {
  try {
    window.localStorage.setItem(METRICS_VIEW_KEY, JSON.stringify(memory))
  } catch {
    // A browser that refuses storage still shows the view for this visit.
  }
}

/** Whether "Remember my view" is on. */
export const rememberingMetrics = (): boolean => readMemory()?.on === true

/** The view a visit with no query opens, or `null` while "Remember my view" is off. */
export function rememberedMetricsQuery(): MetricsQuery | null {
  const memory = readMemory()
  return memory?.on ? memory.query : null
}

/** Keeps `q` as the remembered view, when remembering is on. */
export function rememberMetrics(q: MetricsQuery) {
  if (rememberingMetrics()) writeMemory({ on: true, query: q })
}

/** The switch: on keeps `q` from now on, off forgets it. */
export function setRememberingMetrics(on: boolean, q: MetricsQuery) {
  writeMemory({ on, query: on ? q : DEFAULT_METRICS_QUERY })
}

/** Each bucket's first day from `since` to `until`, as the route keys them (a week from Monday). */
export function bucketsBetween(since: string, until: string, bucket: MetricsBucket): string[] {
  const start = new Date(`${since}T00:00:00Z`)
  if (bucket === 'month') start.setUTCDate(1)
  else start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7))
  const out: string[] = []
  while (start.toISOString().slice(0, 10) <= until) {
    out.push(start.toISOString().slice(0, 10))
    if (bucket === 'month') start.setUTCMonth(start.getUTCMonth() + 1)
    else start.setTime(start.getTime() + 7 * 86_400_000)
  }
  return out
}
