import { CircleAlertIcon } from 'lucide-react'
import type { SlicePreview } from '../../api/types'
import { formatDuration } from '../../lib/format'
import { cn } from '@/lib/utils'
import { Spinner } from '../ui/spinner'

function grams(value: number): string {
  return `${value < 10 ? value.toFixed(1) : value.toFixed(0)} g`
}

/**
 * #6885 — the footer's left side, beside Print: the slice's estimate (the time and the
 * grams the slice result gives) and the first check that holds Print. The preview pane
 * keeps the full estimate; this keeps the two facts in sight on a long page.
 */
export function FooterStatus({
  estimate,
  stale,
  slicing,
  plateLabel,
  blocking,
}: {
  estimate: SlicePreview | null
  /** The estimate is of choices since changed. */
  stale: boolean
  /** A slice of the choices on screen is on its way. */
  slicing: boolean
  /** Which plate the estimate is of, when the file has more than one. */
  plateLabel: string | null
  /** The first check that holds Print, in its own words. */
  blocking: string | null
}) {
  const time = estimate?.print_time_seconds
  const weight = estimate?.filament_used_g
  const known = time != null || weight != null
  if (!known && !slicing && !blocking) return null
  return (
    <div className="mr-auto flex min-w-0 flex-[1_1_14rem] flex-col gap-0.5 text-xs" data-testid="print-footer-status">
      {known ? (
        <p className={cn('flex flex-wrap items-baseline gap-x-3', stale && 'text-muted-foreground')} data-testid="footer-estimate">
          {plateLabel && <span className="text-muted-foreground">{plateLabel}</span>}
          {time != null && (
            <span>
              <span className="text-muted-foreground">Time </span>
              <span className="sb-num font-medium" data-testid="footer-time">{formatDuration(time)}</span>
            </span>
          )}
          {weight != null && (
            <span>
              <span className="text-muted-foreground">Filament </span>
              <span className="sb-num font-medium" data-testid="footer-grams">{grams(weight)}</span>
            </span>
          )}
          {(stale || slicing) && (
            <span className="text-muted-foreground">{slicing ? 'Slicing the new choices' : 'For earlier choices'}</span>
          )}
        </p>
      ) : (
        slicing && (
          <p className="flex items-center gap-1.5 text-muted-foreground" data-testid="footer-slicing">
            <Spinner />
            Slicing for an estimate
          </p>
        )
      )}
      {blocking && (
        <p className="flex min-w-0 items-start gap-1.5 text-destructive" data-testid="footer-blocking">
          <CircleAlertIcon aria-hidden="true" className="mt-px size-3.5 shrink-0" />
          <span className="line-clamp-2">{blocking}</span>
        </p>
      )}
    </div>
  )
}
