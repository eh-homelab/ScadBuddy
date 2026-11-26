import type { Closing } from '../../api/filament'

const REASON_TEXT: Record<NonNullable<Closing['reason']>, string> = { finished: 'Finished', respooled: 'Respooled', damaged: 'Damaged', other: 'Other' }

const grams = (value: number | null | undefined) => (value == null ? 'not known' : `${Number(value.toFixed(1))} g`)

/** The closing's one sentence (spec 2026-11-25 §5.4): "{Reason} on {date}: started with …". */
export function closingText(closing: Closing, at: string): string {
  const reason = closing.reason ? REASON_TEXT[closing.reason] : 'Archived'
  const date = new Date(at).toLocaleDateString(undefined, { dateStyle: 'medium' })
  const from = closing.started_from === 'label' ? 'from the label' : closing.started_from === 'reading' ? 'first weighing' : null
  const started = `started with ${grams(closing.started_g)}${from ? ` (${from})` : ''}`
  if (closing.scale_consumed_g == null || closing.drift_g == null) {
    return `${reason} on ${date}: ${started}, the ledger used ${grams(closing.ledger_consumed_g)}; not weighed at the end.`
  }
  const drift = Math.abs(closing.drift_g)
  return `${reason} on ${date}: ${started}, the ledger used ${grams(closing.ledger_consumed_g)}, the scale says ${grams(closing.scale_consumed_g)} were used; ${grams(drift)} ${closing.drift_g < 0 ? 'less' : 'more'} than the ledger expected.`
}

/**
 * #7684 — an archived spool's lifetime reconciliation, in the Weight section: what it started
 * with and where that came from, what the ledger used, what the scale says was used and the
 * drift between them (`SpoolDetail.closing`, the newest `archived` event's data).
 */
export function ClosingSummary({ closing, at }: { closing: Closing; at: string }) {
  return (
    <p data-testid="closing-summary" className="text-[13px]">
      {closingText(closing, at)}
    </p>
  )
}
