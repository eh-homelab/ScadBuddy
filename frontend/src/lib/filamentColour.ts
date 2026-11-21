import type { FilamentInventorySpool } from '../api/types'
import { normalizeHex } from './format'

/**
 * #1026 — the colour a spool gives a `// color` parameter. A ScadBuddy spool's colour
 * is `#RRGGBBAA` (#6262; a bare `RRGGBBAA` or `RRGGBB` reads the same). A spool whose
 * alpha is 0 is a clear filament recorded with no tint (Insignia "Clear" is `#00000000`),
 * so it has no colour to take: taking its RGB would turn the part black.
 */
export interface SpoolColour {
  hex: string
  /** Alpha below FF: a translucent filament (`spoolValue` keeps its alpha for the preview). */
  translucent: boolean
}

/** Anything that carries a recorded colour. */
type Coloured = { rgba?: string | null }

/**
 * What a spool's recorded colour says, from the one parse (#3358): a colour; clear,
 * for a spool recorded with a zero alpha; or unknown, when none is recorded or
 * it cannot be read. A chip draws each differently, and never black.
 */
export type SpoolChip = ({ kind: 'colour' } & SpoolColour) | { kind: 'clear' } | { kind: 'unknown' }

export function spoolChip(spool: Coloured): SpoolChip {
  const raw = (spool.rgba ?? '').trim().replace(/^#/, '')
  if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(raw)) return { kind: 'unknown' }
  const alpha = raw.length === 8 ? Number.parseInt(raw.slice(6), 16) : 255
  if (alpha === 0) return { kind: 'clear' }
  return { kind: 'colour', hex: normalizeHex(raw.slice(0, 6)), translucent: alpha < 255 }
}

export function spoolColour(spool: Coloured): SpoolColour | null {
  const chip = spoolChip(spool)
  return chip.kind === 'colour' ? { hex: chip.hex, translucent: chip.translucent } : null
}

/**
 * The value a colour parameter takes from a spool: its `#RRGGBB`, plus its alpha
 * (`#RRGGBBAA`) when it is translucent, so the preview draws the part see-through
 * and the covers too. OpenSCAD gets the `#RRGGBB`; the render keeps the alpha for the part
 * (`render/jobs.py` `part_alphas`). Null for a spool with no colour to give.
 */
export function spoolValue(spool: Coloured): string | null {
  const colour = spoolColour(spool)
  if (!colour) return null
  if (!colour.translucent) return colour.hex
  return `${colour.hex}${(spool.rgba ?? '').trim().slice(-2).toUpperCase()}`
}

/**
 * "Bambu Lab PLA Basic — Hot Pink", as the print dialog names a spool: its effective
 * brand, material, subtype and colour name, or "Spool #N" when it has none.
 */
export function spoolName(row: Pick<FilamentInventorySpool, 'spool'>): string {
  const { spool } = row
  const names = spool.names ?? {}
  const name = [names.brand, names.material, names.subtype].filter(Boolean).join(' ')
  const colour = spool.effective?.colour_name ?? spool.colour_name
  if (!colour) return name || `Spool #${spool.number}`
  return name ? `${name} — ${colour}` : colour
}
