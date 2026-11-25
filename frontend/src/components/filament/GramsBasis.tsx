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
 * #7682 — an added-full spool whose starting amount (its `created` event) came from
 * something else than a reading (a Bambuddy pull's grams) started at its label weight; the
 * gap to that amount is unexplained. A later anchor (a sync, an AMS reading) follows prints,
 * so it shows no gap. Nothing under 0.05 g.
 */
function startGap(anchor: SpoolEvent | null, addedFull: boolean | null, labelWeight: number | null): string | null {
  if (addedFull !== true || labelWeight == null || !anchor || anchor.grams_after == null) return null
  if (anchor.kind !== 'created' || anchor.source === 'scale' || anchor.source === 'person') return null
  const gap = labelWeight - anchor.grams_after
  if (Math.abs(gap) < 0.05) return null
  const source = anchor.source ? (SOURCE[anchor.source] ?? anchor.source) : 'The spool'
  return `Started at the label weight ${grams(labelWeight)}. ${source.charAt(0).toUpperCase()}${source.slice(1)} reported ${grams(anchor.grams_after)}; ${grams(gap)} unexplained.`
}

/**
 * #6262 — what a spool's grams left are made of: the reading they start from, the print
 * use taken off since (`SpoolDetail.basis`, the server's own sum), and the result, so a
 * person can trust the number or weigh the spool again. `addedFull` and `labelWeight`
 * are the spool's, for the start gap (#7682).
 */
export function GramsBasis({ basis, gramsLeft, addedFull = null, labelWeight = null }: { basis: Basis; gramsLeft: number | null; addedFull?: boolean | null; labelWeight?: number | null }) {
  const gap = startGap(basis.anchor, addedFull, labelWeight)
  return (
    <ul aria-label="How the grams left add up" className="flex flex-col gap-0.5 text-[13px]">
      {gap && <li className="text-muted-foreground">{gap}</li>}
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
