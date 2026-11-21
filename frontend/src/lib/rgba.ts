/**
 * #6262 — filament colours as `#RRGGBBAA`, upper case, as the backend's `filament_*`
 * tables store them (`backend/scadbuddy/filament/colour.py`). Alpha `FF` is opaque.
 */

const HEX = /^#?([0-9A-Fa-f]{6})([0-9A-Fa-f]{2})?$/

/** spec §5.1's "clear" preset: inside the clear band, alpha `00`-`20`. */
export const CLEAR_ALPHA = 0x10
/** spec §5.1's "translucent" preset: Bambu writes translucent filaments with alpha `80`. */
export const TRANSLUCENT_ALPHA = 0x80
/** The top of spec §5.1's clear band. */
export const CLEAR_MAX_ALPHA = 0x20

/**
 * `RRGGBB` or `RRGGBBAA`, with or without `#`, any case, as `#RRGGBBAA`; six digits are
 * opaque (`FF`). `null` for anything else. Mirrors `normalise_rgba`.
 */
export function parseRgba(value: string): string | null {
  const match = HEX.exec(value)
  if (!match) return null
  return `#${match[1]}${match[2] ?? 'FF'}`.toUpperCase()
}

/** A colour the caller already holds, as `#RRGGBBAA`; an unreadable one reads as opaque black. */
function rgbaOf(rgba: string): string {
  return parseRgba(rgba) ?? '#000000FF'
}

/** The alpha byte, 0-255. */
export function alphaOf(rgba: string): number {
  return Number.parseInt(rgbaOf(rgba).slice(7, 9), 16)
}

/** The colour with this alpha byte, rounded and kept within 0-255. */
export function withAlpha(rgba: string, alpha: number): string {
  const byte = Math.min(0xff, Math.max(0, Math.round(alpha)))
  return `${rgbaOf(rgba).slice(0, 7)}${byte.toString(16).padStart(2, '0').toUpperCase()}`
}

/** True when the alpha byte is below `FF`. Mirrors `is_translucent`. */
export function isTranslucent(rgba: string): boolean {
  return alphaOf(rgba) < 0xff
}

/**
 * Whether two colours are both opaque or both translucent (spec §5.1, plan Decision 12):
 * a translucent spool never matches an opaque slot by colour alone. `#RRGGBB` and `null`
 * read as opaque, and so does a colour that is not hex. Mirrors `same_opacity` in
 * `backend/scadbuddy/filament/colour.py`.
 */
export function sameOpacity(left: string | null | undefined, right: string | null | undefined): boolean {
  return translucent(left) === translucent(right)
}

function translucent(colour: string | null | undefined): boolean {
  const rgba = colour == null ? null : parseRgba(colour)
  return rgba !== null && isTranslucent(rgba)
}

/**
 * #6349 — the least opacity the 3D preview draws a part at. A clear filament (alpha
 * `00`-`20`) at its own alpha would vanish; at this floor it still shows as glass.
 */
export const MIN_PREVIEW_OPACITY = 0.35

/** The preview's opacity for an alpha byte, 0-255: 1 at `FF`, else `alpha / 255`, never below the floor. */
export function opacityOfAlpha(alpha: number): number {
  return alpha >= 0xff ? 1 : Math.max(MIN_PREVIEW_OPACITY, alpha / 0xff)
}

/** The preview's opacity for a colour: opaque when it has no alpha, is `FF`, or is not hex. */
export function previewOpacity(colour: string | null | undefined): number {
  const rgba = colour == null ? null : parseRgba(colour)
  return rgba === null ? 1 : opacityOfAlpha(alphaOf(rgba))
}

/** `rgba(r, g, b, a)` for CSS, alpha to three places. */
export function cssRgba(rgba: string): string {
  const full = rgbaOf(rgba)
  const [r, g, b, a] = [1, 3, 5, 7].map((at) => Number.parseInt(full.slice(at, at + 2), 16)) as [number, number, number, number]
  return `rgba(${r}, ${g}, ${b}, ${Number((a / 255).toFixed(3))})`
}
