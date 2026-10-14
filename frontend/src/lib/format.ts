import type { BoundingBox, ParamValue } from '../api/types'
import { length, type DisplayUnit } from './units'

/** An id as the UI shows one with no name: its first 8 characters. */
export function shortId(id: string): string {
  return id.slice(0, 8)
}

/**
 * #2288 — an output's 3MF is saved under its name, which the user typed: what no file
 * system takes (a path separator, `:`, `*`, `?`, quotes, `<>|`, control characters) is a
 * `-`, so every browser saves it under the same name. Nothing left of the name, the id.
 */
export function outputFileName(output: { slug: string; name?: string | null; id: string }): string {
  // eslint-disable-next-line no-control-regex
  const safe = (output.name ?? '').replace(/[\\/:*?"<>|\u0000-\u001f]+/g, '-').trim()
  return `${output.slug}-${/[^-.\s]/.test(safe) ? safe : output.id}.3mf`
}

export function formatBbox(bbox: BoundingBox, unit: DisplayUnit = 'mm'): string {
  const [x, y, z] = bbox.size
  return `${length(x, unit)} × ${length(y, unit)} × ${length(z, unit)} ${unit}`
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
  // `#RGB` and `#RGBA` are shorthand; alpha is dropped, as OpenSCAD's materials do.
  const short = clean.length === 3 || clean.length === 4
  const full = short ? [...clean].map((c) => c + c).join('') : clean
  return `#${full.padEnd(6, '0').slice(0, 6).toUpperCase()}`
}

/** Picks black or white ink so a filament swatch stays readable. */
export function inkOn(hex: string): string {
  const int = Number.parseInt(normalizeHex(hex).slice(1), 16)
  const [r, g, b] = [(int >> 16) & 0xff, (int >> 8) & 0xff, int & 0xff] as const
  const luminance = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255
  return luminance > 0.55 ? '#12161d' : '#f2f5fa'
}

/** A print's length: `1h 47m`, `43m`, or `42s` under a minute. */
export function formatDuration(seconds: number): string {
  const total = Math.round(seconds)
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

/** #1638 — where a waiting render stands: `next`, or how many are ahead of it. */
export function queuePlace(position: number): string {
  return position <= 1 ? 'next in line' : `${position - 1} ahead`
}

/** `0:07`, `4:12`: a render's wait, to the second. */
export function formatElapsed(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds))
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
}

/** Decimal units, as the server's caps are written (1 GB = 1 000 000 000 bytes). */
export function formatBytes(bytes: number): string {
  if (bytes < 1000) return `${bytes} B`
  const units = ['kB', 'MB', 'GB', 'TB']
  let value = bytes / 1000
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}
