import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '../../components/ui/collapsible'
import { ApiError } from '../../api/client'
import {
  getForecast,
  getMaterialNumberStats,
  getMetrics,
  getPurchasingMetrics,
  type Forecast,
  type InventoryMetrics,
  type MaterialNumberStats,
  type PurchasingMetrics,
} from '../../api/filament'
import { MaterialNumberTable } from '../../components/filament/MaterialNumberTable'
import { BarList } from '../../components/filament/charts/BarList'
import { ColumnChart, type Column, type ColumnSeries } from '../../components/filament/charts/ColumnChart'
import { LineChart } from '../../components/filament/charts/LineChart'
import { tickDecimal } from '../../components/filament/charts/scale'
import { TD, TH } from '../../components/filament/formClasses'
import {
  BUCKETS,
  bucketsBetween,
  DIMENSIONS,
  RANGES,
  metricsParams,
  metricsRange,
  parseMetricsQuery,
  rangeDays,
  rememberedMetricsQuery,
  rememberingMetrics,
  rememberMetrics,
  setRememberingMetrics,
  type Dimension,
  type MetricsBucket,
  type MetricsQuery,
} from '../../components/filament/metricsQuery'
import { useMoney } from '../../components/filament/purchasing/useMoney'
import { dayText } from '../../components/filament/forecastText'
import { TypeForecastTable } from '../../components/filament/SpoolForecast'
import { grams } from '../../components/filament/spoolsQuery'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { addDecimals } from '../../lib/decimal'
import { cn } from '@/lib/utils'
import { filamentLinks } from '../../lib/filamentLinks'
import { EntityLink } from '../../components/filament/EntityLink'

const DIMENSION_TITLES: Record<Dimension, string> = { material: 'Material', brand: 'Brand', type: 'Type', supplier: 'Supplier', location: 'Location' }

/** A dimension value's page (#6869): its catalog, type, supplier, or a place's page; none for "none" (no key). */
function dimensionHref(dimension: Dimension, key: string | null | undefined): string | null {
  if (!key) return null
  switch (dimension) {
    case 'material':
      return filamentLinks.catalog('materials')
    case 'brand':
      return filamentLinks.catalog('brands')
    case 'type':
      return filamentLinks.type(key)
    case 'supplier':
      return filamentLinks.supplier(key)
    case 'location':
      return filamentLinks.location(key)
  }
}
const BUCKET_TITLES: Record<MetricsBucket, string> = { week: 'Weeks', month: 'Months' }
const WASTE_TITLES = { material: 'By material', type: 'By type', printer: 'By printer' } as const
const NON_PRINT_WASTE_TITLES = { material: 'By material', type: 'By type' } as const
/**
 * The categorical series in their fixed order (`index.css` `--sb-series-*`): the palette's
 * first three slots, its all-pairs cap, because zero segments are skipped and any two can
 * touch (R-T8-4). Every value past them folds into "Other".
 */
const SERIES_COLOURS = ['text-series-1', 'text-series-2', 'text-series-3'] as const
const OTHER = '__other__'
/** The range the colours are ranked over: the widest, so every range's values are in it (R-T8-1). */
const RANK_RANGE = '365d'

const g = (value: number) => `${grams(value)} g`
const percent = (rate: number) => `${Math.round(rate * 100)}%`
const message = (cause: unknown, what: string) => (cause instanceof ApiError ? cause.detail : `${what} could not be loaded. Try again.`)

/** A price day's epoch ms (UTC midnight) as its calendar day, wherever the viewer is (R-T8-3). */
const priceDayText = (at: number) => new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' })
const priceDayTitle = (at: number) => dayText(new Date(at).toISOString().slice(0, 10))

/** The dimension's values by their grams used, most first: the order that gives each its colour. */
function ranked(metrics: InventoryMetrics): string[] {
  const totals = new Map<string, { name: string; used: number }>()
  for (const row of metrics.consumption) {
    const key = row.key ?? ''
    const found = totals.get(key) ?? { name: row.label, used: 0 }
    found.used += row.used_g
    totals.set(key, found)
  }
  return [...totals.entries()].sort((a, b) => b[1].used - a[1].used || a[1].name.localeCompare(b[1].name)).map(([key]) => key)
}

const bucketLabel = (iso: string, bucket: MetricsBucket) =>
  new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, bucket === 'month' ? { month: 'short', timeZone: 'UTC' } : { month: 'short', day: 'numeric', timeZone: 'UTC' })
const bucketTitle = (iso: string, bucket: MetricsBucket) =>
  bucket === 'month'
    ? new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { month: 'long', year: 'numeric', timeZone: 'UTC' })
    : `Week of ${dayText(iso)}`

function Section({ id, title, note, children, className }: { id: string; title: string; note?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section aria-labelledby={id} className={cn('flex min-w-0 flex-col gap-3', className)}>
      <div>
        <h2 id={id} className="text-sm font-semibold tracking-tight">
          {title}
        </h2>
        {note && <p className="mt-0.5 text-[12px] text-muted-foreground">{note}</p>}
      </div>
      {children}
    </section>
  )
}

function Segmented<T extends string>({ label, value, options, title, onChange }: { label: string; value: T; options: readonly T[]; title: (one: T) => string; onChange: (next: T) => void }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-md border p-0.5">
      {options.map((one) => (
        <button
          key={one}
          type="button"
          aria-pressed={value === one}
          onClick={() => onChange(one)}
          className={cn('rounded-sm px-2 py-0.5 text-[13px] text-muted-foreground hover:text-foreground', value === one && 'bg-muted text-foreground')}
        >
          {title(one)}
        </button>
      ))}
    </div>
  )
}

/**
 * The consumption as stacked columns. The three values that used most over the last 365 days
 * (`order`) keep their colour in every range, even where another used more in this one; the
 * rest fold into "Other" (R-T8-1). Until the 365-day request returns, the caller passes the
 * range's own rank, so the colours may change once when it arrives (R-T8-12).
 */
function Consumption({ metrics, order }: { metrics: InventoryMetrics; order: readonly string[] }) {
  const { bucket, dimension } = metrics
  const slots = order.slice(0, SERIES_COLOURS.length)
  const totals = new Map<string, { name: string; used: number }>()
  for (const row of metrics.consumption) {
    const key = row.key ?? ''
    const found = totals.get(key) ?? { name: row.label, used: 0 }
    found.used += row.used_g
    totals.set(key, found)
  }
  const series: (ColumnSeries & { used: number })[] = slots.flatMap((key, i) => {
    const one = totals.get(key)
    return one ? [{ id: key, name: one.name, colour: SERIES_COLOURS[i]!, used: one.used }] : []
  })
  const folded = [...totals.entries()].filter(([key]) => !slots.includes(key))
  if (folded.length > 0) series.push({ id: OTHER, name: `Other (${folded.length})`, colour: 'text-series-other', used: folded.reduce((sum, [, one]) => sum + one.used, 0) })
  const seriesOf = (key: string | null) => (slots.includes(key ?? '') ? (key ?? '') : OTHER)

  const columns: Column[] = bucketsBetween(metrics.since, metrics.until, bucket).map((first) => {
    const values: Record<string, number> = {}
    for (const row of metrics.consumption.filter((one) => one.bucket === first)) values[seriesOf(row.key)] = (values[seriesOf(row.key)] ?? 0) + row.used_g
    return { key: first, label: bucketLabel(first, bucket), title: bucketTitle(first, bucket), values }
  })
  const used = metrics.consumption.reduce((sum, row) => sum + row.used_g, 0)
  const wasted = metrics.consumption.reduce((sum, row) => sum + row.wasted_g, 0)
  const span = `from ${dayText(metrics.since)} to ${dayText(metrics.until)}`
  if (used === 0) return <p className="text-[13px] text-muted-foreground">No filament was used {span}.</p>
  const label = `Grams used per ${bucket} ${span}, by ${dimension}: ${series.map((one) => `${one.name} ${g(one.used)}`).join(', ')}. ${g(used)} in all, ${g(wasted)} of it wasted.`
  return (
    <figure className="flex flex-col gap-2">
      <ColumnChart columns={columns} series={series} label={label} formatValue={g} />
      <figcaption className="flex flex-col gap-2">
        <ul aria-label={`Consumption by ${dimension}`} className="flex flex-wrap gap-x-4 gap-y-1 text-[12px]">
          {series.map((one) => (
            <li key={one.id} className="inline-flex items-center gap-1.5">
              <span aria-hidden="true" className={`size-2.5 rounded-[3px] bg-current ${one.colour}`} />
              <span>{one.name}</span>
              <span className="text-muted-foreground tabular-nums">{g(one.used)}</span>
            </li>
          ))}
        </ul>
        <p className="text-[12px] text-muted-foreground">
          {g(used)} used {span}; {g(wasted)} of it in failed and cancelled prints.
        </p>
        <Collapsible className="text-[12px]">
          <CollapsibleTrigger className="cursor-pointer text-muted-foreground hover:text-foreground">Show the numbers</CollapsibleTrigger>
          <CollapsibleContent className="mt-2 max-h-72 overflow-auto rounded-md border">
            <table aria-label="Consumption" className="w-full text-[12px]">
              <thead className="bg-muted/40 text-left text-muted-foreground">
                <tr>
                  <th className={TH}>{bucket === 'month' ? 'Month' : 'Week'}</th>
                  <th className={TH}>{DIMENSION_TITLES[dimension]}</th>
                  <th className={`${TH} text-right`}>Used</th>
                  <th className={`${TH} text-right`}>Wasted</th>
                </tr>
              </thead>
              <tbody>
                {metrics.consumption.map((row) => (
                  <tr key={`${row.bucket}|${row.key}`} className="border-t">
                    <td className={TD}>{bucketTitle(row.bucket, bucket)}</td>
                    <td className={TD}>
                      {(() => {
                        const href = dimensionHref(dimension, row.key)
                        return href ? <EntityLink to={href}>{row.label}</EntityLink> : row.label
                      })()}
                    </td>
                    <td className={`${TD} text-right tabular-nums`}>{g(row.used_g)}</td>
                    <td className={`${TD} text-right tabular-nums`}>{g(row.wasted_g)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CollapsibleContent>
        </Collapsible>
      </figcaption>
    </figure>
  )
}

/**
 * #6269 — the Filament Metrics tab (spec §5.7, plan Decision 19): what to reorder (each
 * type's days of stock), the stock now, the consumption per week or month, the waste, the
 * price per kg over time and the spend per supplier. The range, bucket and dimension are in
 * the address (`metricsQuery.ts`); "Remember my view" makes a visit with no query open the
 * last one. Money is the routes' decimal strings, shown with `useMoney`, each in its own
 * currency: invoice spend is never converted.
 */
export function MetricsTab() {
  const [params, setParams] = useSearchParams()
  const [restore, setRestore] = useState(() => (params.size === 0 ? rememberedMetricsQuery() : null))
  const [keeping, setKeeping] = useState(rememberingMetrics)
  const money = useMoney()
  const [metrics, setMetrics] = useState<InventoryMetrics | null>(null)
  const [rank, setRank] = useState<{ dimension: Dimension; order: string[] } | null>(null)
  const [purchasing, setPurchasing] = useState<PurchasingMetrics | null>(null)
  const [forecast, setForecast] = useState<Forecast | null>(null)
  const [numbers, setNumbers] = useState<MaterialNumberStats | null>(null)
  const [errors, setErrors] = useState<Partial<Record<'metrics' | 'rank' | 'purchasing' | 'forecast' | 'numbers', string>>>({})

  const q: MetricsQuery = params.size === 0 && restore ? restore : parseMetricsQuery(params)

  useEffect(() => {
    if (restore === null) return
    setRestore(null)
    if (params.size === 0) setParams(metricsParams(restore), { replace: true })
  }, [restore, params, setParams])

  function change(next: Partial<MetricsQuery>) {
    const merged = { ...q, ...next }
    setParams(metricsParams(merged), { replace: true })
    rememberMetrics(merged)
  }

  useEffect(() => {
    let live = true
    getForecast().then(
      (found) => {
        if (!live) return
        setForecast(found)
        setErrors(({ forecast: _gone, ...rest }) => rest)
      },
      (cause) => live && setErrors((now) => ({ ...now, forecast: message(cause, 'The forecast') })),
    )
    return () => {
      live = false
    }
  }, [])

  const { range, bucket, dimension } = q
  // The colours' order: the dimension's values ranked over the widest range, which holds every range (R-T8-1). Until it returns, the chart uses the range's own rank (R-T8-12).
  useEffect(() => {
    let live = true
    getMetrics({ ...metricsRange({ range: RANK_RANGE, bucket: 'month', dimension }), bucket: 'month', dimension }).then(
      (found) => {
        if (!live) return
        setRank({ dimension, order: ranked(found) })
        setErrors(({ rank: _gone, ...rest }) => rest)
      },
      (cause) => live && setErrors((now) => ({ ...now, rank: message(cause, 'The consumption') })),
    )
    return () => {
      live = false
    }
  }, [dimension])

  useEffect(() => {
    let live = true
    const span = metricsRange({ range, bucket, dimension })
    getMetrics({ ...span, bucket, dimension }).then(
      (found) => {
        if (!live) return
        setMetrics(found)
        setErrors(({ metrics: _gone, ...rest }) => rest)
      },
      (cause) => live && setErrors((now) => ({ ...now, metrics: message(cause, 'The metrics') })),
    )
    return () => {
      live = false
    }
  }, [range, bucket, dimension])

  useEffect(() => {
    let live = true
    getMaterialNumberStats(metricsRange({ range, bucket: 'month', dimension: 'material' })).then(
      (found) => {
        if (!live) return
        setNumbers(found)
        setErrors(({ numbers: _gone, ...rest }) => rest)
      },
      (cause) => live && setErrors((now) => ({ ...now, numbers: message(cause, 'The material number stats') })),
    )
    return () => {
      live = false
    }
  }, [range])

  useEffect(() => {
    let live = true
    getPurchasingMetrics(metricsRange({ range, bucket: 'month', dimension: 'material' })).then(
      (found) => {
        if (!live) return
        setPurchasing(found)
        setErrors(({ purchasing: _gone, ...rest }) => rest)
      },
      (cause) => live && setErrors((now) => ({ ...now, purchasing: message(cause, 'The prices and spend') })),
    )
    return () => {
      live = false
    }
  }, [range])

  const spend = useMemo(() => [...(purchasing?.spend ?? [])].sort((a, b) => b.month.localeCompare(a.month) || a.supplier_name.localeCompare(b.supplier_name) || a.currency.localeCompare(b.currency)), [purchasing])
  const spendTotals = useMemo(() => {
    const totals = new Map<string, string>()
    for (const row of spend) totals.set(row.currency, totals.has(row.currency) ? addDecimals(totals.get(row.currency)!, row.total) : row.total)
    return [...totals.entries()].sort((a, b) => a[0].localeCompare(b[0]))
  }, [spend])

  const priceUntil = Date.parse(`${metricsRange({ range, bucket: 'month', dimension: 'material' }).until}T00:00:00Z`)
  const stockValue = metrics?.stock.reduce<string | null>((sum, row) => (row.value == null ? sum : sum == null ? row.value : addDecimals(sum, row.value)), null) ?? null
  const span = metrics ? `${dayText(metrics.since)} to ${dayText(metrics.until)}` : `the last ${rangeDays(range)} days`
  const loading = <p className="text-[13px] text-muted-foreground">Loading…</p>
  const problem = (text: string | undefined) =>
    text && (
      <p role="alert" className="text-[13px] text-destructive">
        {text}
      </p>
    )

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px]">
        <Segmented label="Range" value={range} options={RANGES} title={(one) => `${rangeDays(one)} days`} onChange={(next) => change({ range: next })} />
        <Segmented label="Columns per" value={bucket} options={BUCKETS} title={(one) => BUCKET_TITLES[one]} onChange={(next) => change({ bucket: next })} />
        <Select value={dimension} onValueChange={(next) => change({ dimension: next as Dimension })}>
          <SelectTrigger size="sm" aria-label="Group by" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {DIMENSIONS.map((one) => (
              <SelectItem key={one} value={one}>
                By {DIMENSION_TITLES[one].toLowerCase()}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <label className="ml-auto flex items-center gap-2 text-[12px] text-muted-foreground">
          <Switch
            size="sm"
            checked={keeping}
            onCheckedChange={(on) => {
              setKeeping(on)
              setRememberingMetrics(on, q)
            }}
          />
          Remember my view
        </label>
      </div>

      <Section
        id="metrics-days"
        title="Days of stock"
        note="How long each type lasts at the rate it is used, and when to order more so it arrives in time."
      >
        {problem(errors.forecast)}
        {forecast ? <TypeForecastTable types={forecast.types} windowDays={forecast.window_days} /> : !errors.forecast && loading}
      </Section>

      <div className="grid gap-x-10 gap-y-8 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <Section
          id="metrics-stock"
          title="Stock"
          note={
            metrics
              ? `${g(metrics.stock.reduce((sum, row) => sum + row.grams_left, 0))} left${stockValue != null ? `, worth ${money.format(stockValue, metrics.currency)} at cost` : ''}, by ${dimension}.`
              : `Grams left now, by ${dimension}.`
          }
        >
          {problem(errors.metrics)}
          {metrics ? (
            <BarList
              label={`Stock by ${metrics.dimension}`}
              empty="No spools in stock."
              rows={metrics.stock.map((row) => ({
                key: row.key ?? '',
                label: row.label,
                value: row.grams_left,
                text: g(row.grams_left),
                note: `${row.spools} ${row.spools === 1 ? 'spool' : 'spools'}${row.value != null ? `, ${money.format(row.value, metrics.currency)}` : ''}`,
              }))}
            />
          ) : (
            !errors.metrics && loading
          )}
        </Section>

        <Section id="metrics-consumption" title="Consumption" note={`Grams the plates used, ${span}, per ${bucket}.`}>
          {problem(errors.rank)}
          {metrics ? <Consumption metrics={metrics} order={rank?.dimension === metrics.dimension ? rank.order : ranked(metrics)} /> : !errors.metrics && loading}
        </Section>
      </div>

      <Section
        id="metrics-material-numbers"
        title="By material number"
        note={`Each type code (Bambuddy's material number), with its archived types: spools in stock, grams on hand and their value at cost, and the grams its prints used, ${numbers ? `${dayText(numbers.since)} to ${dayText(numbers.until)}` : span}.`}
      >
        {problem(errors.numbers)}
        {numbers ? <MaterialNumberTable stats={numbers} money={money} /> : !errors.numbers && loading}
      </Section>

      <Section id="metrics-waste" title="Waste" note={`Grams of failed and cancelled prints, ${span}, and their share of what was used.`}>
        {metrics ? (
          <div className="grid gap-6 sm:grid-cols-3">
            {(['material', 'type', 'printer'] as const).map((name) => (
              <div key={name} className="flex min-w-0 flex-col gap-2">
                <h3 className="text-[12px] text-muted-foreground">{WASTE_TITLES[name]}</h3>
                <BarList
                  label={`Waste ${WASTE_TITLES[name].toLowerCase()}`}
                  empty="No prints in this range."
                  rows={metrics.waste
                    .filter((row) => row.dimension === name)
                    .map((row) => ({
                      key: row.key ?? '',
                      label: row.label,
                      value: row.wasted_g,
                      text: g(row.wasted_g),
                      note: row.rate == null ? 'Nothing used' : `${percent(row.rate)} of ${g(row.used_g)}`,
                    }))}
                />
              </div>
            ))}
          </div>
        ) : (
          !errors.metrics && loading
        )}
        {metrics && (metrics.non_print_waste_g > 0 || metrics.non_print_waste.length > 0) && (
          <div role="group" aria-label="Non-print waste" className="mt-6 flex flex-col gap-3 border-t border-border pt-4">
            <div className="flex flex-col gap-0.5">
              <h3 className="text-[13px] font-medium">Non-print waste</h3>
              <p className="text-[12px] text-muted-foreground">
                {g(metrics.non_print_waste_g)} thrown away off the printer, {span}: what was left on archived spools and replaced boxes. It is not in the print waste above.
              </p>
            </div>
            <div className="grid gap-6 sm:grid-cols-3">
              {(['material', 'type'] as const).map((name) => (
                <div key={name} className="flex min-w-0 flex-col gap-2">
                  <h4 className="text-[12px] text-muted-foreground">{NON_PRINT_WASTE_TITLES[name]}</h4>
                  <BarList
                    label={`Non-print waste ${NON_PRINT_WASTE_TITLES[name].toLowerCase()}`}
                    empty="No non-print waste in this range."
                    rows={metrics.non_print_waste
                      .filter((row) => row.dimension === name)
                      .map((row) => ({ key: row.key ?? '', label: row.label, value: row.wasted_g, text: g(row.wasted_g) }))}
                  />
                </div>
              ))}
            </div>
          </div>
        )}
      </Section>

      <Section id="metrics-prices" title="Prices" note={`Price per kg of each type at each supplier, ${span}, in the currency it was paid in.`}>
        {problem(errors.purchasing)}
        {purchasing ? (
          purchasing.prices.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">No prices were recorded in this range.</p>
          ) : (
            <ul aria-label="Price per kg" className="grid gap-x-8 gap-y-5 sm:grid-cols-2 lg:grid-cols-3">
              {purchasing.prices.map((series) => {
                const last = series.points[series.points.length - 1]!
                const first = series.points[0]!
                const name = `${series.type_label} at ${series.supplier_name}`
                // Text comes from the decimal strings, never a float (R-T8-2): an axis tick is a round number `tickDecimal` writes exactly.
                const tick = (value: number) => money.format(tickDecimal(value), series.currency)
                return (
                  <li key={`${series.type_id}|${series.supplier_id}|${series.currency}`} className="flex min-w-0 flex-col gap-1">
                    <div className="flex items-baseline justify-between gap-3 text-[13px]">
                      <span className="min-w-0 truncate" title={name}>
                        <EntityLink to={filamentLinks.type(series.type_id)}>{series.type_label}</EntityLink>
                        <span className="block truncate text-[12px] text-muted-foreground">
                          <EntityLink to={filamentLinks.supplier(series.supplier_id)}>{series.supplier_name}</EntityLink>
                        </span>
                      </span>
                      <span className="shrink-0 font-medium tabular-nums">{money.format(last.price_per_kg, series.currency)}/kg</span>
                    </div>
                    <LineChart
                      points={series.points.map((point) => ({ at: Date.parse(`${point.on_date}T00:00:00Z`), value: Number(point.price_per_kg), text: `${money.format(point.price_per_kg, series.currency)}/kg` }))}
                      label={`${name}, price per kg: ${money.format(first.price_per_kg, series.currency)} on ${dayText(first.on_date)}${series.points.length > 1 ? ` to ${money.format(last.price_per_kg, series.currency)} on ${dayText(last.on_date)}` : ''}`}
                      formatValue={tick}
                      formatTime={priceDayText}
                      formatPointTime={priceDayTitle}
                      step
                      until={priceUntil}
                      width={320}
                      height={90}
                    />
                  </li>
                )
              })}
            </ul>
          )
        ) : (
          !errors.purchasing && loading
        )}
      </Section>

      <Section id="metrics-spend" title="Spend" note={`Confirmed invoices, ${span}, per supplier and month. Each currency stays apart; nothing is converted.`}>
        {purchasing ? (
          spend.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">No confirmed invoices in this range.</p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <table aria-label="Spend by supplier and month" className="w-full min-w-[28rem] text-[13px]">
                <thead className="bg-muted/40 text-left text-[12px] text-muted-foreground">
                  <tr>
                    <th className={TH}>Month</th>
                    <th className={TH}>Supplier</th>
                    <th className={`${TH} text-right`}>Invoices</th>
                    <th className={`${TH} text-right`}>Total</th>
                  </tr>
                </thead>
                <tbody>
                  {spend.map((row) => (
                    <tr key={`${row.month}|${row.supplier_id}|${row.currency}`} data-currency={row.currency} className="border-t">
                      <td className={TD}>{bucketTitle(row.month, 'month')}</td>
                      <td className={TD}>
                        <EntityLink to={filamentLinks.supplier(row.supplier_id)}>{row.supplier_name}</EntityLink>
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>
                        <EntityLink to={filamentLinks.supplierInvoices(row.supplier_id)}>{row.invoices}</EntityLink>
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>{money.format(row.total, row.currency)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  {spendTotals.map(([currency, total]) => (
                    <tr key={currency} className="border-t font-medium">
                      <td className={TD} colSpan={3}>
                        Total in {currency}
                      </td>
                      <td className={`${TD} text-right tabular-nums`}>{money.format(total, currency)}</td>
                    </tr>
                  ))}
                </tfoot>
              </table>
            </div>
          )
        ) : (
          !errors.purchasing && loading
        )}
      </Section>
    </div>
  )
}
