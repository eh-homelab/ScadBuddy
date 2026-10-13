import type { InstalledNozzle, NozzleChoice } from '../../api/types'

type Props = {
  sizes: string[]
  installed: InstalledNozzle[]
  value: NozzleChoice[]
  onChange: (next: NozzleChoice[]) => void
}

const SIDES = ['Left', 'Right'] as const

/**
 * An Advanced step (#768): Simple mode does not show it, and sends the size the dialog
 * opened on (this model's last, else 0.4 mm) with Standard flow on both sides. The dialog
 * opens in Advanced instead when the flow it opens on is High Flow, remembered or the
 * printer's default (#1895).
 *
 * Spec 2026-09-27 §4.1 — Bambuddy rejects mixed nozzle sizes on the left and right
 * extruder (422 "different sizes"), so size is ONE radiogroup setting both sides. There
 * is no per-side size selector at all. Flow (Standard / High Flow) is per-side, and the
 * print states it for each side in the sliced file, as Bambu Studio does (#484).
 *
 * Only installed sizes are offered (#2166); they and the not-installed note read the whole hotend rack, not what is
 * mounted: the printer swaps the sliced size on itself, and nothing here is checked
 * against the mounted pair (#768).
 */
export function NozzleStep({ sizes, installed, value, onChange }: Props) {
  const has = (size: string) => installed.some((n) => n.size === size)
  const setBothSizes = (size: string) =>
    onChange(value.map((n) => ({ ...n, size: size as NozzleChoice['size'] })))
  const setFlow = (side: number, flow: NozzleChoice['flow']) =>
    onChange(value.map((n, i) => (i === side ? { ...n, flow } : n)))
  const missing = [...new Set(value.map((n) => n.size))].filter(
    (size) => installed.length > 0 && !has(size),
  )
  // #2166 — only the sizes the printer has (mounted or in the rack), and the one chosen.
  const shown = installed.length > 0 ? sizes.filter((size) => has(size) || value[0]?.size === size) : sizes

  return (
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Nozzles</legend>

      <div role="radiogroup" aria-label="Nozzle size" className="mt-1.5 flex flex-wrap gap-3">
        {shown.map((size) => (
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
            {size} mm
          </label>
        ))}
      </div>

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

      {missing.map((size) => (
        <p key={size} role="status" className="mt-1.5 text-[12px] text-warn">
          No {size} mm nozzle is installed. Install one before this prints.
        </p>
      ))}
    </fieldset>
  )
}
