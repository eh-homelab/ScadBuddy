import type { InventorySpool } from '../api/types'
import { normalizeHex } from './format'

/**
 * #1026 — the colour a spool gives a `// color` parameter. Bambuddy stores a spool's
 * colour as `RRGGBBAA` with no `#`. A spool whose alpha is 0 is a clear filament that
 * Bambuddy recorded with no tint (Insignia "Clear" is `00000000`), so it has no colour
 * to take: taking its RGB would turn the part black.
 */
export interface SpoolColour {
  hex: string
  /** Alpha below FF: a translucent filament, which the preview still draws opaque. */
  translucent: boolean
}

/**
 * What a spool's recorded colour says, from the one parse (#3358): a colour; clear,
 * for a spool Bambuddy recorded with a zero alpha; or unknown, when none is recorded or
 * it cannot be read. A chip draws each differently, and never black.
 */
export type SpoolChip = ({ kind: 'colour' } & SpoolColour) | { kind: 'clear' } | { kind: 'unknown' }

export function spoolChip(spool: Pick<InventorySpool, 'rgba'>): SpoolChip {
  const raw = (spool.rgba ?? '').trim().replace(/^#/, '')
  if (!/^[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(raw)) return { kind: 'unknown' }
  const alpha = raw.length === 8 ? Number.parseInt(raw.slice(6), 16) : 255
  if (alpha === 0) return { kind: 'clear' }
  return { kind: 'colour', hex: normalizeHex(raw.slice(0, 6)), translucent: alpha < 255 }
}

export function spoolColour(spool: Pick<InventorySpool, 'rgba'>): SpoolColour | null {
  const chip = spoolChip(spool)
  return chip.kind === 'colour' ? { hex: chip.hex, translucent: chip.translucent } : null
}

/** "Bambu Lab PLA Basic — Hot Pink", as the print dialog names a spool. */
export function spoolName(spool: Pick<InventorySpool, 'brand' | 'material' | 'subtype' | 'color_name' | 'id'>): string {
  const name = [spool.brand, spool.material, spool.subtype].filter(Boolean).join(' ')
  if (!spool.color_name) return name || `Spool ${spool.id}`
  return name ? `${name} — ${spool.color_name}` : spool.color_name
}
