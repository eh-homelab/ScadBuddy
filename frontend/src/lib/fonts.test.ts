import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CatalogueFont } from '../api/types'
import {
  cssFontFamily,
  formatFontValue,
  googleFontsCssUrl,
  installedAsCatalogue,
  orderFonts,
  parseFontValue,
  preferredStyle,
  readRecentFonts,
  rememberFont,
} from './fonts'

function font(family: string, installed = false): CatalogueFont {
  return { family, category: 'sans-serif', variants: [], popularity: null, installed }
}

describe('the OpenSCAD font string', () => {
  it('splits into a family and a style', () => {
    expect(parseFontValue('Lobster Two:style=Bold')).toEqual({
      family: 'Lobster Two',
      style: 'Bold',
    })
  })

  it('treats a bare family as having no style', () => {
    expect(parseFontValue('Pacifico')).toEqual({ family: 'Pacifico', style: '' })
  })

  it('round-trips', () => {
    expect(formatFontValue('Lobster Two', 'Bold')).toBe('Lobster Two:style=Bold')
    expect(formatFontValue('Pacifico', '')).toBe('Pacifico')
    expect(formatFontValue('', 'Bold')).toBe('')
  })

  it('keeps a style containing a space, which is how fontconfig names them', () => {
    const parsed = parseFontValue('Roboto:style=Light Italic')
    expect(parsed.style).toBe('Light Italic')
    expect(formatFontValue(parsed.family, parsed.style)).toBe('Roboto:style=Light Italic')
  })
})

describe('preview CSS', () => {
  it('quotes the family so a name with spaces resolves', () => {
    expect(cssFontFamily('Lobster Two')).toBe('"Lobster Two", sans-serif')
    expect(cssFontFamily('')).toBeUndefined()
  })

  it('asks for every family in one request', () => {
    const url = googleFontsCssUrl(['Pacifico', 'Lobster Two'])
    expect(url).toContain('family=Pacifico')
    expect(url).toContain('family=Lobster+Two')
    expect(url).toContain('display=swap')
  })

  it('subsets to the characters actually on screen', () => {
    const url = googleFontsCssUrl(['AB'], 'BC')
    const text = new URL(url).searchParams.get('text')
    expect(text).toBe('ABC')
  })

  it('is empty when there is nothing to preview', () => {
    expect(googleFontsCssUrl([])).toBe('')
  })
})

describe('recently used', () => {
  beforeEach(() => window.localStorage.clear())

  it('starts empty and remembers most-recent first', () => {
    expect(readRecentFonts()).toEqual([])
    rememberFont('Pacifico')
    expect(rememberFont('Lobster Two')).toEqual(['Lobster Two', 'Pacifico'])
  })

  it('moves a repeat pick to the front rather than duplicating it', () => {
    rememberFont('Pacifico')
    rememberFont('Lobster Two')
    expect(rememberFont('Pacifico')).toEqual(['Pacifico', 'Lobster Two'])
  })

  it('survives a storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(readRecentFonts()).toEqual([])
    vi.restoreAllMocks()
  })

  it('ignores a corrupt entry', () => {
    window.localStorage.setItem('scadbuddy.recent-fonts', 'not json')
    expect(readRecentFonts()).toEqual([])
  })
})

describe('browse order', () => {
  it('puts recently used first, then installed, then the server order', () => {
    const ordered = orderFonts(
      [font('Roboto'), font('Noto Sans', true), font('Pacifico'), font('Inter')],
      { recent: ['Pacifico'], installed: new Set<string>() },
    )
    expect(ordered.map((f) => f.family)).toEqual(['Pacifico', 'Noto Sans', 'Roboto', 'Inter'])
  })

  it('leaves the relative order of equals alone', () => {
    const ordered = orderFonts([font('Roboto'), font('Inter')], {
      recent: [],
      installed: new Set<string>(),
    })
    expect(ordered.map((f) => f.family)).toEqual(['Roboto', 'Inter'])
  })
})

describe('offline rows', () => {
  it('presents installed families as already-installed catalogue rows', () => {
    expect(installedAsCatalogue([{ family: 'DejaVu Sans', styles: ['Book'] }])).toEqual([
      { family: 'DejaVu Sans', category: '', variants: [], popularity: null, installed: true },
    ])
  })
})

describe('style choice', () => {
  it('prefers Regular, else the first face', () => {
    expect(preferredStyle(['Bold', 'Regular', 'Italic'])).toBe('Regular')
    expect(preferredStyle(['Book', 'Bold'])).toBe('Book')
    expect(preferredStyle([])).toBe('')
  })
})
