import type { SpoolDetail, SpoolEvent } from '../../api/filament'

type Basis = SpoolDetail['basis']

const when = (at: string) => new Date(at).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })

const SOURCE: Record<string, string> = { scale: 'scale', person: 'by hand', ams_remain: 'AMS reading', bb: 'Bambuddy', print_actual: 'print' }

const grams = (value: number) => `${Number(value.toFixed(1))} g`

function anchorText(event: SpoolEvent): string {
  if (event.grams_after == null) return 'Starting amount not known'
  const via = event.source ? `, ${SOURCE[event.source] ?? event.source}` : ''
  return `${event.kind === 'created' ? 'Starting amount' : 'Reading'} ${grams(event.grams_after)}${via}`
}

/**
 * #6262 — what a spool's grams left are made of: the reading they start from, the print
 * use taken off since (`SpoolDetail.basis`, the server's own sum), and the result, so a
 * person can trust the number or weigh the spool again.
 */
export function GramsBasis({ basis, gramsLeft }: { basis: Basis; gramsLeft: number | null }) {
  return (
    <ul aria-label="How the grams left add up" className="flex flex-col gap-0.5 text-[13px]">
      {basis.anchor ? (
        <li className="flex justify-between gap-3">
          <span>{anchorText(basis.anchor)}</span>
          <span className="text-muted-foreground tabular-nums">{when(basis.anchor.at)}</span>
        </li>
      ) : (
        <li className="text-muted-foreground">No reading yet. Weigh the spool to start one.</li>
      )}
      {basis.deltas.map((event) => (
        <li key={event.id} className="flex justify-between gap-3">
          <span>
            {event.delta_g != null && event.delta_g < 0 ? 'Used' : 'Added'} <span className="tabular-nums">{grams(Math.abs(event.delta_g ?? 0))}</span>
            {event.ref_kind ? `, ${event.ref_kind} ${event.ref_id ?? ''}`.trimEnd() : ''}
          </span>
          <span className="text-muted-foreground tabular-nums">{when(event.at)}</span>
        </li>
      ))}
      <li className="mt-1 flex justify-between gap-3 border-t pt-1 font-medium">
        <span>Left</span>
        <span className="tabular-nums">{gramsLeft == null ? 'Not known' : grams(gramsLeft)}</span>
      </li>
    </ul>
  )
}
