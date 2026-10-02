import type { RackAlgorithm, RackPickView } from '../../api/types'
import { ALGORITHM_LABELS, flowLabel } from './rackLabels'

/**
 * #836 — Simple mode's one line: the rack position ScadBuddy would pick for the rack
 * side, and why. A preview: the run picks per sliced group, and its result says what was
 * sent. Warnings (an unsafe material) come through the check's verdict and never hold Print.
 */
export function RackNozzleLine({ rack, algorithm }: { rack: RackPickView | null | undefined; algorithm: RackAlgorithm }) {
  if (!rack) return null
  const picked = rack.options?.find((option) => option.position === rack.position)
  let text: string | null = null
  if (picked) {
    text = `Rack nozzle: position ${picked.position} (${picked.nozzle_diameter} ${flowLabel(picked.flow)})`
    if (rack.reason) text += ` — ${rack.reason}`
  } else if (algorithm === 'bambuddy') text = 'Rack nozzle: Bambuddy picks at dispatch'
  if (text === null) return null
  return (
    <div className="text-[12.5px] text-ink">
      <p data-testid="rack-nozzle-line">{text}</p>
      {rack.glow_unchecked && (
        <p className="text-[12px] text-muted">A spool is not in the inventory, so Glow could not be checked.</p>
      )}
    </div>
  )
}

interface StepProps {
  rack: RackPickView | null | undefined
  algorithm: RackAlgorithm
  position: number | null
  onAlgorithm: (next: RackAlgorithm) => void
  onPosition: (next: number | null) => void
}

/** #836 — Advanced mode: the algorithm (remembered per printer) and a hand-picked position. */
export function RackNozzleStep({ rack, algorithm, position, onAlgorithm, onPosition }: StepProps) {
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Rack nozzle</legend>
      <label className="mt-1.5 flex flex-col gap-1 text-[12px] text-muted">
        Algorithm
        <select
          aria-label="Rack algorithm"
          value={algorithm}
          onChange={(event) => onAlgorithm(event.target.value as RackAlgorithm)}
          className="sb-field"
        >
          {(Object.keys(ALGORITHM_LABELS) as RackAlgorithm[]).map((value) => (
            <option key={value} value={value}>
              {ALGORITHM_LABELS[value]}
            </option>
          ))}
        </select>
      </label>
      <label className="mt-1.5 flex flex-col gap-1 text-[12px] text-muted">
        Nozzle
        <select
          aria-label="Rack nozzle position"
          value={position === null ? '' : String(position)}
          onChange={(event) => onPosition(event.target.value === '' ? null : Number(event.target.value))}
          className="sb-field"
        >
          <option value="">Automatic</option>
          {(rack?.options ?? []).map((option) => (
            <option key={option.position} value={option.position}>
              {`Position ${option.position} · ${option.nozzle_diameter} ${flowLabel(option.flow)} · ${option.material ?? 'material unknown'} · ${option.prints} ${option.prints === 1 ? 'print' : 'prints'}${option.color ? ` · ${option.color}` : ''}`}
            </option>
          ))}
        </select>
      </label>
    </fieldset>
  )
}
