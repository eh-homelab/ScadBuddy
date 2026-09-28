import type { InstalledNozzle, NozzleChoice } from '../../api/types'

type Props = {
  sizes: string[]
  installed: InstalledNozzle[]
  advanced: boolean
  value: NozzleChoice[]
  onChange: (next: NozzleChoice[]) => void
}

const SIDES = ['Left', 'Right'] as const

/**
 * R9 (task-8 addendum) — Bambuddy rejects mixed nozzle sizes on the left and right
 * extruder (422 "different sizes"), so size is ONE radiogroup setting both sides, in
 * both Simple and Advanced mode. There is no per-side size selector at all. Flow
 * (Standard / High Flow) is per-side, but only exposed in Advanced mode — Bambuddy has
 * no High Flow presets yet, so a High Flow choice slices as Standard and this step says
 * so rather than pretending the choice does something.
 *
 * The High Flow note is shown regardless of mode: a value carrying `high_flow` over
 * from an earlier Advanced visit is still true in Simple mode, and hiding the note
 * there would make the choice look like it silently reverted.
 */
export function NozzleStep({ sizes, installed, advanced, value, onChange }: Props) {
  const has = (size: string) => installed.some((n) => n.size === size)
  const setBothSizes = (size: string) =>
    onChange(value.map((n) => ({ ...n, size: size as NozzleChoice['size'] })))
  const setFlow = (side: number, flow: NozzleChoice['flow']) =>
    onChange(value.map((n, i) => (i === side ? { ...n, flow } : n)))
  const missing = [...new Set(value.map((n) => n.size))].filter(
    (size) => installed.length > 0 && !has(size),
  )
  const hasHighFlow = value.some((n) => n.flow === 'high_flow')

  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Nozzles</legend>

      <div role="radiogroup" aria-label="Nozzle size" className="mt-1.5 flex flex-wrap gap-3">
        {sizes.map((size) => (
          <label
            key={size}
            className="flex cursor-pointer items-center gap-1.5 text-[12px] text-ink"
          >
            <input
              type="radio"
              name="nozzle-size"
              checked={value[0]?.size === size}
              onChange={() => setBothSizes(size)}
              className="accent-[var(--sb-accent)]"
            />
            {size} mm{has(size) ? ' (installed)' : ''}
          </label>
        ))}
      </div>

      {advanced && (
        <div className="mt-2 flex flex-wrap gap-4">
          {SIDES.map((side, index) => (
            <div
              key={side}
              role="radiogroup"
              aria-label={`${side} nozzle flow`}
              className="flex items-center gap-2"
            >
              <span className="text-[12px] text-muted">{side}</span>
              {(['standard', 'high_flow'] as const).map((flow) => (
                <label
                  key={flow}
                  className="flex cursor-pointer items-center gap-1.5 text-[12px] text-ink"
                >
                  <input
                    type="radio"
                    name={`flow-${side}`}
                    checked={value[index]?.flow === flow}
                    aria-label={`${side} ${flow === 'standard' ? 'Standard' : 'High Flow'}`}
                    onChange={() => setFlow(index, flow)}
                    className="accent-[var(--sb-accent)]"
                  />
                  {flow === 'standard' ? 'Standard' : 'High Flow'}
                </label>
              ))}
            </div>
          ))}
        </div>
      )}

      {hasHighFlow && (
        <p role="status" className="mt-1.5 text-[12px] text-muted">
          Bambuddy slices this as Standard flow; High Flow presets aren&apos;t supported by
          Bambuddy yet.
        </p>
      )}

      {missing.map((size) => (
        <p key={size} role="status" className="mt-1.5 text-[12px] text-warn">
          No {size} mm nozzle is installed. Install one before this prints.
        </p>
      ))}
    </fieldset>
  )
}
