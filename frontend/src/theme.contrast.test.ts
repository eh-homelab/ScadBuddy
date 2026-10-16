/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

// Read from disk: vitest's `css: false` empties any `?raw` import of a stylesheet.
const css = readFileSync(`${import.meta.dirname}/index.css`, 'utf8')

/** The `--sb-*` colour tokens of one theme block of index.css. */
function tokens(block: string): Record<string, string> {
  return Object.fromEntries([...block.matchAll(/--sb-([\w-]+):\s*(#[0-9a-f]{6})/gi)].map((m) => [m[1]!, m[2]!]))
}

const [darkBlock = '', rest = ''] = css.split('@media (prefers-color-scheme: light)')
const themes = { dark: tokens(darkBlock), light: tokens(rest.split('@theme')[0] ?? '') }

/** WCAG 2.x relative luminance and contrast ratio. */
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  }) as [number, number, number]
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number]
  return (hi + 0.05) / (lo + 0.05)
}

/** WCAG AA for normal-size text: every token here is set at 11–14px. */
const AA = 4.5
/** The surfaces text sits on. */
const GROUNDS = ['bg', 'surface', 'surface-2']

describe('design token contrast (#970, WCAG AA)', () => {
  for (const [theme, t] of Object.entries(themes)) {
    describe(theme, () => {
      it('has every token it is checked for', () => {
        for (const name of ['text', 'muted', 'faint', 'accent', 'accent-ink', 'ok', 'warn', ...GROUNDS]) {
          expect(t[name], name).toMatch(/^#[0-9a-f]{6}$/i)
        }
      })

      for (const text of ['text', 'muted', 'faint', 'accent', 'ok', 'warn']) {
        it(`${text} text reads at AA on every surface`, () => {
          for (const ground of GROUNDS) {
            expect(contrast(t[text]!, t[ground]!), `${text} on ${ground}`).toBeGreaterThanOrEqual(AA)
          }
        })
      }

      it('primary buttons: accent-ink on accent reads at AA', () => {
        expect(contrast(t['accent-ink']!, t.accent!)).toBeGreaterThanOrEqual(AA)
      })

      it('keeps faint quieter than muted, so the two still read as two levels', () => {
        expect(contrast(t.muted!, t.surface!)).toBeGreaterThan(contrast(t.faint!, t.surface!) + 1)
      })
    })
  }
})
