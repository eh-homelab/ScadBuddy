import { afterEach, describe, expect, it, vi } from 'vitest'
import { SCADBUDDY_DARK, SCADBUDDY_LIGHT, editorTheme } from './openscadLanguage'

describe('editorTheme (#2194)', () => {
  const real = window.matchMedia
  afterEach(() => {
    window.matchMedia = real
  })

  function prefers(dark: boolean) {
    window.matchMedia = vi.fn((query: string) => ({ matches: dark && query === '(prefers-color-scheme: dark)' }) as MediaQueryList)
  }

  it('follows the colour scheme the browser prefers', () => {
    prefers(true)
    expect(editorTheme()).toBe(SCADBUDDY_DARK)
    prefers(false)
    expect(editorTheme()).toBe(SCADBUDDY_LIGHT)
  })

  it('is dark where there is no matchMedia', () => {
    // @ts-expect-error jsdom without matchMedia, as some test setups run it
    window.matchMedia = undefined
    expect(editorTheme()).toBe(SCADBUDDY_DARK)
  })
})
