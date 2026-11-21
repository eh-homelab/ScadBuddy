import { TriangleAlertIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { ApiError } from '../../api/client'
import { getForecast, type Forecast, type Spool, type TypeForecast } from '../../api/filament'
import { niceMax } from './charts/scale'
import { TD, TH } from './formClasses'
import { burnText, byReorder, bySpoolEmpty, dayText } from './forecastText'
import { RgbaSwatch } from './RgbaSwatch'
import { SpoolLink } from './SpoolLink'
import { grams } from './spoolsQuery'

const NUMBER = 'tabular-nums whitespace-nowrap'

function ReorderNow({ className }: { className?: string }) {
  return (
    <span className={`items-center gap-1 rounded-full border border-warn/50 bg-warn/10 px-2 py-0.5 text-[12px] font-medium whitespace-nowrap text-warn ${className ?? ''}`}>
      <TriangleAlertIcon aria-hidden="true" className="size-3" />
      Reorder now
    </span>
  )
}

const daysText = (value: number | null | undefined) => (value == null ? '—' : `${Math.floor(value)} ${Math.floor(value) === 1 ? 'day' : 'days'}`)

/**
 * A type's runway (drawn only when it has days of stock): its days of stock as a bar, and its lead time plus safety margin as a
 * tick. A bar that ends left of the tick is a type to reorder. Drawn for the eye only; the
 * row's cells carry the numbers.
 */
function Runway({ row, scale }: { row: TypeForecast; scale: number }) {
  const threshold = row.lead_time_days + row.safety_margin_days
  const at = (days: number) => `${Math.min(100, (days / scale) * 100)}%`
  return (
    <div aria-hidden="true" className="relative h-2 w-full min-w-24 rounded-full bg-muted">
      <div data-runway className={`absolute inset-y-0 left-0 rounded-full ${row.reorder_now ? 'bg-warn' : 'bg-foreground/70'}`} style={{ width: at(row.days_of_stock!) }} />
      <div data-threshold className="absolute -inset-y-1 w-0.5 rounded-full bg-foreground" style={{ left: at(threshold) }} />
    </div>
  )
}

/**
 * #6269 — each type's days of stock and reorder day (spec §5.7, BB's Forecast view): the
 * type rows of `GET /filament/forecast`, "Reorder now" first. Shared by the Spools tab's
 * forecast view and the Metrics tab's "Days of stock".
 */
export function TypeForecastTable({ types, windowDays }: { types: readonly TypeForecast[]; windowDays: number }) {
  if (types.length === 0) return <p className="text-[13px] text-muted-foreground">No spool has a type yet, so there is nothing to reorder.</p>
  const rows = [...types].sort(byReorder)
  const forecastable = rows.filter((row) => row.days_of_stock != null)
  const scale = niceMax(Math.min(365, Math.max(0, ...forecastable.map((row) => Math.max(row.days_of_stock!, (row.lead_time_days + row.safety_margin_days) * 1.5)))))
  return (
    <div className="flex flex-col gap-2">
      <div className="@container overflow-x-auto rounded-md border">
        <table aria-label="Days of stock by type" className="w-full min-w-[44rem] text-[13px]">
          <thead className="bg-muted/40 text-left text-[12px] text-muted-foreground">
            <tr>
              <th className={TH}>Type</th>
              <th className={`${TH} text-right`}>Left</th>
              <th className={`${TH} text-right`}>Burn</th>
              <th className={`${TH} w-1/4`}>Days of stock</th>
              <th className={TH}>Empty around</th>
              <th className={TH}>Reorder by</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.type_id} data-type={row.type_id} className="border-t">
                <td className={TD}>
                  <Link to={`/filament/types/${encodeURIComponent(row.type_id)}`} className="font-medium hover:underline">
                    {row.label}
                  </Link>
                  <span className="block text-[12px] text-muted-foreground">
                    {row.material ?? 'No material'}, {row.spools} {row.spools === 1 ? 'spool' : 'spools'}
                    {row.low_stock && row.spools > 0 ? ', all low' : ''}
                  </span>
                  {/* Narrower than the table's 44 rem, its container scrolls sideways, so the badge sits here, in view (R-T8-6, R-T8-12). */}
                  {row.reorder_now && <ReorderNow className="mt-1 hidden @max-[44rem]:inline-flex" />}
                </td>
                <td className={`${TD} text-right ${NUMBER}`}>{grams(row.grams_left)} g</td>
                <td className={`${TD} text-right ${NUMBER}`}>{burnText(row.burn_g_per_day)}</td>
                <td className={TD}>
                  <div className="flex items-center gap-2">
                    <span className={`w-16 shrink-0 ${NUMBER}`}>{daysText(row.days_of_stock)}</span>
                    {row.days_of_stock != null && <Runway row={row} scale={scale} />}
                  </div>
                </td>
                <td className={`${TD} ${NUMBER}`}>{dayText(row.empty_on)}</td>
                <td className={TD}>
                  {row.reorder_now ? (
                    <>
                      <ReorderNow className="inline-flex @max-[44rem]:hidden" />
                      <span className={`hidden @max-[44rem]:inline ${NUMBER}`}>{dayText(row.reorder_on)}</span>
                    </>
                  ) : (
                    <span className={NUMBER}>{dayText(row.reorder_on)}</span>
                  )}
                  <span className="block text-[12px] whitespace-nowrap text-muted-foreground">
                    {row.lead_time_days} d lead, {row.safety_margin_days} d margin
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
        <span aria-hidden="true" className="inline-block h-3 w-0.5 rounded-full bg-foreground" />
        The mark is the lead time plus the safety margin: a bar that ends before it is a type to reorder. The burn is over the last {windowDays} days.
      </p>
    </div>
  )
}

/**
 * #6269 — the Spools tab's forecast view (spec §10): each shown spool's burn and empty day,
 * soonest first, then the type rows with "Reorder now" (`TypeForecastTable`). The type rows
 * show even when the filters keep no spool, and the tab says so above (R-T8-8).
 */
export function SpoolForecast({ spools }: { spools: readonly Spool[] }) {
  const [forecast, setForecast] = useState<Forecast | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getForecast().then(
      (found) => live && setForecast(found),
      (cause) => live && setError(cause instanceof ApiError ? cause.detail : 'The forecast could not be loaded. Try again.'),
    )
    return () => {
      live = false
    }
  }, [])

  if (error)
    return (
      <p role="alert" className="text-[13px] text-destructive">
        {error}
      </p>
    )
  if (!forecast) return <p className="text-[13px] text-muted-foreground">Loading the forecast…</p>

  const byId = new Map(spools.map((spool) => [spool.id, spool]))
  const rows = forecast.spools.filter((row) => byId.has(row.spool_id)).sort(bySpoolEmpty)
  return (
    <div className="flex flex-col gap-5">
      {spools.length > 0 && (
        <section aria-labelledby="forecast-spools" className="flex flex-col gap-2">
          <h2 id="forecast-spools" className="text-sm font-semibold tracking-tight">
            When each spool runs out
          </h2>
          {rows.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">No spool in this view is in stock.</p>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <table aria-label="Spool forecast" className="w-full min-w-[30rem] text-[13px]">
                <thead className="bg-muted/40 text-left text-[12px] text-muted-foreground">
                  <tr>
                    <th className={TH}>Spool</th>
                    <th className={`${TH} text-right`}>Left</th>
                    <th className={`${TH} text-right`}>Burn</th>
                    <th className={TH}>Empty around</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const spool = byId.get(row.spool_id)!
                    return (
                      <tr key={row.spool_id} data-spool={row.number} className="border-t">
                        <td className={TD}>
                          <span className="flex items-center gap-2">
                            <RgbaSwatch rgba={spool.effective?.rgba ?? '#FFFFFFFF'} size="sm" gramsLeft={spool.grams_left} labelWeight={spool.effective?.label_weight_g} />
                            <SpoolLink spool={spool} className="font-medium hover:underline">
                              #{row.number}
                            </SpoolLink>
                            <span className="truncate text-muted-foreground">{[spool.names?.material, spool.effective?.colour_name].filter(Boolean).join(' ')}</span>
                          </span>
                        </td>
                        <td className={`${TD} text-right ${NUMBER}`}>{row.grams_left == null ? '—' : `${grams(row.grams_left)} g`}</td>
                        <td className={`${TD} text-right ${NUMBER}`}>{burnText(row.burn_g_per_day)}</td>
                        <td className={`${TD} ${NUMBER}`}>{row.empty_on ? dayText(row.empty_on) : row.burn_g_per_day == null ? `No prints in ${forecast.window_days} days` : '—'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
      <section aria-labelledby="forecast-types" className="flex flex-col gap-2">
        <h2 id="forecast-types" className="text-sm font-semibold tracking-tight">
          When to reorder each type
        </h2>
        <TypeForecastTable types={forecast.types} windowDays={forecast.window_days} />
      </section>
    </div>
  )
}
