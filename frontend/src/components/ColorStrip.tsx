import { inkOn, normalizeHex } from '../lib/format'

/**
 * Filament colours in extruder order — the one saturated element in the UI.
 * Colour order is extruder order (spec §7), so the index is meaningful, not decorative.
 */
export function ColorStrip({
  colors,
  size = 'md',
  showNumbers = true,
}: {
  colors: string[]
  size?: 'sm' | 'md'
  showNumbers?: boolean
}) {
  if (colors.length === 0) return null
  const box = size === 'sm' ? 'size-4 text-[9px]' : 'size-6 text-[10px]'

  return (
    <ul className="flex items-center gap-1" aria-label="Filament colours by extruder">
      {colors.map((color, index) => {
        const hex = normalizeHex(color)
        return (
          <li key={`${hex}-${index}`}>
            <span
              title={`Extruder ${index + 1} — ${hex}`}
              style={{ background: hex, color: inkOn(hex) }}
              className={`sb-num flex items-center justify-center rounded-[3px] font-semibold ring-1 ring-black/25 ring-inset ${box}`}
            >
              {showNumbers ? index + 1 : ''}
            </span>
          </li>
        )
      })}
    </ul>
  )
}
