import type { CatalogueFont, FontFamily } from '../api/types'

const STYLE_SEPARATOR = ':style='
const RECENT_KEY = 'scadbuddy.recent-fonts'
const RECENT_LIMIT = 8

export interface FontValue {
  family: string
  /** The fontconfig style name — "Bold", "Light Italic" — or '' for the family's default. */
  style: string
}

export const FONT_CATEGORIES: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'sans-serif', label: 'Sans' },
  { value: 'serif', label: 'Serif' },
  { value: 'display', label: 'Display' },
  { value: 'handwriting', label: 'Script' },
  { value: 'monospace', label: 'Mono' },
]

/** OpenSCAD writes a font as `Family:style=Bold`; the style half is optional. */
export function parseFontValue(value: string): FontValue {
  const at = value.indexOf(STYLE_SEPARATOR)
  if (at === -1) return { family: value.trim(), style: '' }
  return {
    family: value.slice(0, at).trim(),
    style: value.slice(at + STYLE_SEPARATOR.length).trim(),
  }
}

export function formatFontValue(family: string, style: string): string {
  const trimmed = family.trim()
  if (!trimmed) return ''
  return style ? `${trimmed}${STYLE_SEPARATOR}${style}` : trimmed
}

/** What the preview row is drawn with. Quoted, because family names contain spaces. */
export function cssFontFamily(family: string): string | undefined {
  return family ? `"${family.replace(/"/g, '')}", sans-serif` : undefined
}

/**
 * A Google Fonts CSS2 request for the preview rows.
 *
 * `text=` is not an optimisation to skip: without it a browse list of forty families
 * pulls forty whole font files, and with it Google serves one subset covering just the
 * characters on screen.
 */
export function googleFontsCssUrl(families: string[], sample = ''): string {
  const wanted = [...new Set(families.filter(Boolean))]
  if (wanted.length === 0) return ''
  const params = wanted.map(
    (family) => `family=${encodeURIComponent(family).replace(/%20/g, '+')}`,
  )
  const glyphs = [...new Set([...wanted.join(''), ...sample])].join('')
  if (glyphs) params.push(`text=${encodeURIComponent(glyphs)}`)
  params.push('display=swap')
  return `https://fonts.googleapis.com/css2?${params.join('&')}`
}

/** Browser storage is per-viewer and optional: a blocked or full store must not throw. */
export function readRecentFonts(): string[] {
  try {
    const raw = window.localStorage.getItem(RECENT_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function rememberFont(family: string): string[] {
  const next = [family, ...readRecentFonts().filter((item) => item !== family)].slice(
    0,
    RECENT_LIMIT,
  )
  try {
    window.localStorage.setItem(RECENT_KEY, JSON.stringify(next))
  } catch {
    // Nothing to do: the list is a convenience, not state anything depends on.
  }
  return next
}

/**
 * Recently used first, then what is already installed, then the server's own order
 * (which is popularity). Only for browsing — a search is ordered by relevance server
 * side and must not be reshuffled here.
 */
export function orderFonts(
  fonts: CatalogueFont[],
  { recent, installed }: { recent: string[]; installed: Set<string> },
): CatalogueFont[] {
  const rank = (font: CatalogueFont): number => {
    const at = recent.indexOf(font.family)
    if (at !== -1) return at
    return font.installed || installed.has(font.family) ? RECENT_LIMIT : RECENT_LIMIT + 1
  }
  return [...fonts]
    .map((font, index) => ({ font, index }))
    .sort((a, b) => rank(a.font) - rank(b.font) || a.index - b.index)
    .map(({ font }) => font)
}

/** Installed families as catalogue rows, so the offline list renders the same way. */
export function installedAsCatalogue(fonts: FontFamily[]): CatalogueFont[] {
  return fonts.map((font) => ({
    family: font.family,
    category: '',
    variants: [],
    popularity: null,
    installed: true,
  }))
}

/** Regular is the sensible default when a family arrives with several faces. */
export function preferredStyle(styles: string[]): string {
  return styles.find((style) => style === 'Regular') ?? styles[0] ?? ''
}
