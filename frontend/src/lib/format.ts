import type { BoundingBox, ParamValue } from '../api/types'

export function mm(value: number): string {
  return value.toFixed(1)
}

export function formatBbox(bbox: BoundingBox): string {
  const [x, y, z] = bbox.size
  return `${mm(x)} × ${mm(y)} × ${mm(z)} mm`
}

export function formatValue(value: ParamValue): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (typeof value === 'number') return String(value)
  return value
}

const RELATIVE = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 365 * 24 * 3600],
  ['month', 30 * 24 * 3600],
  ['day', 24 * 3600],
  ['hour', 3600],
  ['minute', 60],
]

export function timeAgo(iso: string, now = Date.now()): string {
  const seconds = (new Date(iso).getTime() - now) / 1000
  const magnitude = Math.abs(seconds)
  for (const [unit, size] of UNITS) {
    if (magnitude >= size) return RELATIVE.format(Math.round(seconds / size), unit)
  }
  return RELATIVE.format(Math.round(seconds), 'second')
}

export function normalizeHex(value: string): string {
  const clean = value.trim().replace(/^#/, '')
  const full = clean.length === 3 ? [...clean].map((c) => c + c).join('') : clean
  return `#${full.padEnd(6, '0').slice(0, 6).toUpperCase()}`
}

/** Picks black or white ink so a filament swatch stays readable. */
export function inkOn(hex: string): string {
  const int = Number.parseInt(normalizeHex(hex).slice(1), 16)
  const [r, g, b] = [(int >> 16) & 0xff, (int >> 8) & 0xff, int & 0xff] as const
  const luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
  return luminance > 0.55 ? '#12161d' : '#f2f5fa'
}
