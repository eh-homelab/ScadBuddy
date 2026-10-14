import { inkOn, normalizeHex } from '../../lib/format'

/** A colour chip, as the filament picker and the print dialog's panes show one. */
export function Swatch({ colour, size = 'md' }: { colour: string | null | undefined; size?: 'sm' | 'md' }) {
  const hex = normalizeHex(colour ?? '#000000')
  return (
    <span
      aria-hidden="true"
      title={hex}
      style={{ background: hex, color: inkOn(hex) }}
      className={`inline-block shrink-0 rounded-[3px] ring-1 ring-black/25 ring-inset ${
        size === 'sm' ? 'size-4' : 'size-5'
      }`}
    />
  )
}

/** A clear spool's chip: a hatched square, never the black a missing colour reads as. */
export function ClearSwatch({ size = 'md' }: { size?: 'sm' | 'md' }) {
  return (
    <span
      aria-hidden="true"
      title="clear"
      data-testid="clear-swatch"
      className={`inline-block shrink-0 rounded-[3px] ring-1 ring-black/25 ring-inset ${
        size === 'sm' ? 'size-4' : 'size-5'
      }`}
      style={{ background: 'repeating-linear-gradient(45deg, transparent 0 3px, rgb(0 0 0 / 0.18) 3px 4px)' }}
    />
  )
}
