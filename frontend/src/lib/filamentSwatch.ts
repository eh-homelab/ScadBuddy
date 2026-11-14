import type { CSSProperties } from 'react'

/**
 * A filament's swatch as Bambuddy draws it (#6209; `filamentSwatchHelpers.ts` at
 * https://github.com/maziggy/bambuddy/blob/1aa7bce166682fde929fec0d535bdaaa22685aad/frontend/src/components/filamentSwatchHelpers.ts),
 * kept small: the colour (or its stops) over a checkerboard, so an alpha below FF shows
 * as translucent and not as black; a conic gradient for multicolor stops, else a linear
 * one; and a light overlay for the effects that have one there.
 */
export type SwatchFilament = {
  colour: string
  alpha?: number
  extra_colours?: string[]
  effect?: string | null
  subtype?: string | null
}

const CHECKERBOARD = 'repeating-conic-gradient(#979797 0% 25%, #f5f5f5 0% 50%)'
const CHECKERBOARD_TILE = '8px 8px'

/** Bambuddy's `EFFECT_OVERLAYS`, without the seeded sparkle flecks (one fixed fleck set instead). */
const OVERLAYS: Record<string, string> = {
  sparkle:
    'radial-gradient(circle at 25% 30%, rgba(255,248,220,0.9) 0 1px, transparent 2px), radial-gradient(circle at 70% 65%, rgba(255,248,220,0.9) 0 1px, transparent 2px)',
  wood: 'repeating-linear-gradient(90deg, rgba(0,0,0,0.18) 0 1px, transparent 1px 6px, rgba(0,0,0,0.08) 6px 7px, transparent 7px 12px)',
  marble: 'repeating-linear-gradient(135deg, rgba(255,255,255,0.18) 0 2px, transparent 2px 8px)',
  glow: 'radial-gradient(circle at 50% 50%, rgba(255,255,255,0.35) 0%, rgba(255,255,255,0) 70%)',
  matte: 'linear-gradient(180deg, rgba(0,0,0,0.10) 0%, rgba(0,0,0,0) 50%, rgba(0,0,0,0.10) 100%)',
  silk: 'linear-gradient(110deg, rgba(255,255,255,0) 30%, rgba(255,255,255,0.30) 50%, rgba(255,255,255,0) 70%)',
  galaxy: 'linear-gradient(110deg, rgba(255,255,255,0) 25%, rgba(255,255,255,0.40) 50%, rgba(255,255,255,0) 75%)',
  metal: 'linear-gradient(180deg, rgba(255,255,255,0.18) 0%, rgba(0,0,0,0.18) 100%)',
}

function alphaHex(alpha: number): string {
  return Math.max(0, Math.min(255, Math.round(alpha))).toString(16).padStart(2, '0').toUpperCase()
}

/** `#RRGGBB`, or `#RRGGBBAA` only when the alpha is below FF, as Bambuddy's `displayHex`. */
export function displayHex(colour: string, alpha = 255): string {
  const rgb = colour.toUpperCase()
  return alpha < 255 ? `${rgb}${alphaHex(alpha)}` : rgb
}

function colourLayer(f: SwatchFilament): string {
  const stops = f.extra_colours ?? []
  if (stops.length === 0) {
    const solid = displayHex(f.colour, f.alpha)
    return `linear-gradient(${solid}, ${solid})`
  }
  const all = stops.length === 1 ? [stops[0], stops[0]] : stops
  if ((f.subtype ?? '').toLowerCase() === 'multicolor' || f.effect === 'multicolor') {
    const segments = all.map((c, i) => `${c} ${((i / all.length) * 360).toFixed(3)}deg ${(((i + 1) / all.length) * 360).toFixed(3)}deg`)
    return `conic-gradient(from 0deg, ${segments.join(', ')})`
  }
  return `linear-gradient(135deg, ${all.join(', ')})`
}

/** The swatch's `background-image` and `background-size`: overlay, colour, checkerboard. */
export function swatchStyle(f: SwatchFilament): CSSProperties {
  const overlay = f.effect ? OVERLAYS[f.effect] : undefined
  const layers = [...(overlay ? [overlay] : []), colourLayer(f), CHECKERBOARD]
  const sizes = [...(overlay ? ['cover'] : []), 'cover', CHECKERBOARD_TILE]
  return { backgroundImage: layers.join(', '), backgroundSize: sizes.join(', ') }
}
