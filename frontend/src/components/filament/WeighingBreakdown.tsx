import type { SpoolEvent } from '../../api/filament'

const grams = (value: number) => `${Number(value.toFixed(1))} g`
const when = (at: string) => new Date(at).toLocaleDateString(undefined, { dateStyle: 'medium' })

/** The word for where the empty weight came from (`empty_basis`, resolved by the backend). */
const BASIS: Record<string, string> = { catalog: 'from the catalog', placeholder: 'placeholder', derived: 'from new spools', weighed_empty: 'weighed empty', person: 'by hand' }

type Breakdown = { reading_g: number; empty_g: number; empty_source: string; empty_basis: string | null; empty_name: string | null; expected_g: number | null; drift_g: number | null }

/**
 * #7682 — what a weighing's reading was made of (the filament and the empty spool taken
 * off it), and how the filament compares with what the ledger expected just before it.
 * For a `weighed` event whose `data` has the breakdown (`ledger.breakdown`).
 */
export function WeighingBreakdown({ event }: { event: SpoolEvent }) {
  const data = event.data as Breakdown
  const filament = event.grams_after ?? data.reading_g - data.empty_g
  const spool =
    data.empty_source === 'person'
      ? `spool ${grams(data.empty_g)} (taken off by hand)`
      : `spool ${grams(data.empty_g)} (${[data.empty_name, data.empty_basis ? (BASIS[data.empty_basis] ?? data.empty_basis) : null].filter(Boolean).join(', ')})`
  const drift = data.drift_g ?? 0
  const compared =
    data.expected_g == null
      ? ''
      : ` The ledger expected ${grams(data.expected_g)}. ${
          Math.abs(drift) < 1 ? 'The scale matches the prints.' : `The scale found ${grams(Math.abs(drift))} ${drift < 0 ? 'less' : 'more'} than the prints accounted for.`
        }`
  return (
    <p className="text-[13px] text-muted-foreground">
      {`Weighed ${grams(data.reading_g)} on ${when(event.at)}: filament ${grams(filament)} + ${spool}.${compared}`}
    </p>
  )
}
