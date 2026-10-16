import { useId } from 'react'
import { normalizeHex } from '../lib/format'
import { Swatch } from './print/Swatch'
import { FilamentColourDisclosure } from './widgets/FilamentColourPicker'

/** #3637 — the colours a copy prints in: each of `colours` swapped as `recolour` says. */
function printedColours(colours: string[], recolour: Record<string, string>): string[] {
  return [...new Set(colours.map((c) => normalizeHex(recolour[normalizeHex(c)] ?? c)))]
}

/**
 * #3637 — one Arrange row's colours, as swatches, and on Change colours an editor that
 * swaps any of them: picked from a spool in Bambuddy, or any colour. A swapped colour is
 * a filament slot of its own, so the same model can print twice in two colourings on one
 * plate. `recolour` maps an original colour to the one it prints in instead.
 */
export function CopyColours({
  label,
  colours,
  recolour,
  open,
  onToggle,
  onChange,
}: {
  label: string
  colours: string[]
  recolour: Record<string, string>
  open: boolean
  onToggle: () => void
  onChange: (next: Record<string, string>) => void
}) {
  const editorId = useId()
  const own = [...new Set(colours.map(normalizeHex))]
  const shown = printedColours(own, recolour)

  function swap(colour: string, next: string) {
    const rest = Object.fromEntries(Object.entries(recolour).filter(([from]) => from !== colour))
    const to = normalizeHex(next)
    onChange(to === colour ? rest : { ...rest, [colour]: to })
  }

  if (own.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-1.5">
        <span role="img" aria-label={`${label} prints in ${shown.join(', ')}`} className="flex gap-0.5">
          {shown.map((colour) => (
            <Swatch key={colour} colour={colour} size="sm" />
          ))}
        </span>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={editorId}
          onClick={onToggle}
          className="text-[12px] text-primary hover:underline"
        >
          {open ? 'Done' : 'Change colours'}
        </button>
      </div>
      {open && (
        <ul id={editorId} aria-label={`Colours of ${label}`} className="flex flex-col gap-2 rounded-[6px] border border-line bg-surface p-2">
          {own.map((colour) => {
            const now = normalizeHex(recolour[colour] ?? colour)
            return (
              <li key={colour} className="text-[12px]">
                <div className="flex items-center gap-2">
                  <Swatch colour={colour} size="sm" />
                  <span className="sb-num text-subtle">{colour}</span>
                  <span aria-hidden="true" className="text-faint">
                    →
                  </span>
                  <input
                    type="color"
                    value={now.toLowerCase()}
                    aria-label={`${colour} of ${label} prints in`}
                    onChange={(event) => swap(colour, event.target.value)}
                    className="size-7 shrink-0 cursor-pointer rounded-[4px] border border-line bg-surface-2 p-0.5"
                  />
                  <span className="sb-num">{now}</span>
                  {now !== colour && (
                    <button type="button" onClick={() => swap(colour, colour)} className="text-primary hover:underline">
                      Keep {colour}
                    </button>
                  )}
                </div>
                <FilamentColourDisclosure label={`${colour} of ${label}`} value={now} onPick={(hex) => swap(colour, hex)} />
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
