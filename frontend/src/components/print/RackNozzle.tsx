import type { RackAlgorithm, RackOption, RackPickView } from '../../api/types'
import { ALGORITHM_LABELS, flowLabel } from './rackLabels'
import { Swatch } from './Swatch'

/**
 * #836 — Simple mode's one line: the rack position ScadBuddy would pick for the right
 * nozzle, the side the rack swaps hotends onto, and why. A preview: the run picks per
 * sliced group, and its result says what was sent. Warnings (an unsafe material) come
 * through the check's verdict and never hold Print.
 */
export function RackNozzleLine({ rack, algorithm }: { rack: RackPickView | null | undefined; algorithm: RackAlgorithm }) {
  if (!rack) return null
  const picked = rack.options?.find((option) => option.position === rack.position)
  let text: string | null = null
  if (picked) {
    text = `Right nozzle from the rack: position ${picked.position} (${picked.nozzle_diameter} ${flowLabel(picked.flow)})`
    if (rack.reason) text += ` — ${rack.reason}`
  } else if (algorithm === 'bambuddy') text = 'Right nozzle from the rack: Bambuddy picks at dispatch'
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

/** What a hotend last ran, as a person reads it: "last ran black PLA", or nothing known. */
function lastRan(option: RackOption): string | null {
  const ran = [option.color_word, (option.filament_type ?? '').trim()].filter(Boolean).join(' ')
  return ran ? `last ran ${ran}` : null
}

interface StepProps {
  rack: RackPickView | null | undefined
  algorithm: RackAlgorithm
  position: number | null
  onAlgorithm: (next: RackAlgorithm) => void
  /** The algorithm's save for this printer failed; this print still uses it. */
  algorithmUnsaved?: boolean
  onPosition: (next: number | null) => void
}

/**
 * #836, #2166 — Advanced mode: which hotend the right nozzle takes from the rack, as one
 * list: Automatic, or a position picked by hand, each named by its size, flow and the
 * filament it last ran, with that filament's colour (never a hex). How Automatic ranks
 * them is remembered per printer. The left nozzle has no rack: it prints with the hotend
 * mounted on it.
 */
export function RackNozzleStep({ rack, algorithm, position, onAlgorithm, onPosition, algorithmUnsaved }: StepProps) {
  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2" data-testid="rack-step">
      <legend className="px-1 text-[13px] text-ink">Right nozzle, from the rack</legend>
      <div role="radiogroup" aria-label="Rack nozzle position" className="mt-1.5 flex flex-col gap-1">
        <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink">
          <input
            type="radio"
            name="rack-position"
            checked={position === null}
            onChange={() => onPosition(null)}
            className="accent-[var(--sb-accent)]"
            data-testid="rack-position-auto"
          />
          Automatic
        </label>
        {(rack?.options ?? []).map((option) => {
          const ran = lastRan(option)
          return (
            <label
              key={option.position}
              className="flex cursor-pointer flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px] text-ink"
            >
              <input
                type="radio"
                name="rack-position"
                checked={position === option.position}
                onChange={() => onPosition(option.position)}
                className="accent-[var(--sb-accent)]"
                data-testid={`rack-position-${option.position}`}
              />
              {option.color ? <Swatch colour={option.color} size="sm" /> : null}
              <span>
                Position {option.position} · {option.nozzle_diameter} {flowLabel(option.flow)}
                {ran ? ` · ${ran}` : ''}
              </span>
              <span className="text-faint">
                {option.prints} {option.prints === 1 ? 'print' : 'prints'}
                {option.pending ? ` · ${option.pending} queued` : ''}
              </span>
            </label>
          )
        })}
      </div>
      <div className="mt-2 flex flex-col gap-1">
        <label htmlFor="rack-algorithm" className="text-[12px] text-muted">
          Automatic picks by
        </label>
        <select
          id="rack-algorithm"
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
        {algorithmUnsaved && (
          <p role="alert" data-testid="rack-algorithm-unsaved" className="text-[12px] text-warn">
            Not remembered for this printer; this print still uses it.
          </p>
        )}
      </div>
      <p className="mt-1.5 text-[12px] text-muted">The left nozzle prints with the hotend mounted on it.</p>
    </fieldset>
  )
}
