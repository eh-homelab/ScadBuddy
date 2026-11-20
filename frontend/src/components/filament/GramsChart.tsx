import type { GramsPoint } from '../../api/filament'
import { LineChart, type LinePoint } from './charts/LineChart'

const grams = (value: number) => `${Number(value.toFixed(1))} g`

/**
 * #6269 — a spool's grams left over time, as SB believed them then (plan Decision 13):
 * a step line, the readings the grams anchored on as filled circles in the accent, and
 * each plate (`eventText` `plateTimes`) as a short tick on the x axis in ink, the colours the timeline
 * gives readings and prints. Oldest first, as the history route sends the series.
 */
export function GramsChart({ points, plates: plateAt, label }: { points: readonly GramsPoint[]; plates: readonly string[]; label: string }) {
  if (points.length === 0) return <p className="text-[13px] text-muted-foreground">No weight history yet.</p>
  const line: LinePoint[] = points.map((one) => ({ at: Date.parse(one.at), value: one.grams }))
  const anchors = line.filter((_, i) => points[i]!.anchor)
  const plates = plateAt.map((at) => Date.parse(at))
  const first = points[0]!.grams
  const last = points[points.length - 1]!.grams
  const name = `${label}: ${grams(first)} to ${grams(last)}, ${anchors.length} anchor ${anchors.length === 1 ? 'reading' : 'readings'}`
  return (
    <figure className="flex flex-col gap-1.5">
      <LineChart points={line} label={name} formatValue={grams} marks={{ name: 'anchor', points: anchors }} ticks={{ name: 'plate', at: plates }} step width={320} height={120} />
      <figcaption className="flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          <svg aria-hidden="true" viewBox="0 0 8 8" className="size-2 text-primary">
            <circle cx={4} cy={4} r={3.5} className="fill-current" />
          </svg>
          A reading the grams start from
        </span>
        {plates.length > 0 && (
          <span className="inline-flex items-center gap-1.5">
            <svg aria-hidden="true" viewBox="0 0 8 8" className="size-2 text-foreground">
              <line x1={4} x2={4} y1={1} y2={7} stroke="currentColor" strokeWidth={2} strokeLinecap="round" />
            </svg>
            A print
          </span>
        )}
      </figcaption>
    </figure>
  )
}
