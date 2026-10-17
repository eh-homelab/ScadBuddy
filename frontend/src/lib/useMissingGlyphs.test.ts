import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { GlyphCoverage } from '../api/types'
import {
  GLYPH_ANSWERS_KEPT,
  GLYPH_ASKS_IN_FLIGHT,
  GLYPH_CHECK_DEBOUNCE_MS,
  askMissingGlyphs,
  forgetGlyphAnswers,
  useMissingGlyphs,
} from './useMissingGlyphs'

describe('askMissingGlyphs', () => {
  beforeEach(() => forgetGlyphAnswers())
  afterEach(() => vi.restoreAllMocks())

  it('keeps a bounded number of answers, forgetting the least recently asked', async () => {
    const asked = vi.spyOn(api, 'getGlyphs').mockImplementation(async (font) => ({ font, missing: [] }))
    await askMissingGlyphs('Lobster Two', 'kept')
    for (let n = 0; n < GLYPH_ANSWERS_KEPT; n += 1) {
      await askMissingGlyphs('Lobster Two', `text ${n}`)
      // Asked again on the way: the most recent, so never the one forgotten.
      if (n === 0) await askMissingGlyphs('Lobster Two', 'kept')
    }
    const before = asked.mock.calls.length
    await askMissingGlyphs('Lobster Two', 'kept')
    await askMissingGlyphs('Lobster Two', `text ${GLYPH_ANSWERS_KEPT - 1}`)
    expect(asked.mock.calls.length).toBe(before)
    // One past the bound pushed out the oldest: it is asked again.
    await askMissingGlyphs('Lobster Two', 'text 0')
    expect(asked.mock.calls.length).toBe(before + 1)
  })

  it('sends only a few asks at once, the rest as those answer', async () => {
    const pending: ((coverage: GlyphCoverage) => void)[] = []
    const asked = vi
      .spyOn(api, 'getGlyphs')
      .mockImplementation(() => new Promise<GlyphCoverage>((resolve) => pending.push(resolve)))
    const answers = Array.from({ length: GLYPH_ASKS_IN_FLIGHT + 3 }, (_, n) => askMissingGlyphs(`Family ${n}`, 'Reagan'))
    await vi.waitFor(() => expect(asked).toHaveBeenCalledTimes(GLYPH_ASKS_IN_FLIGHT))
    pending.shift()?.({ font: 'Family 0', missing: ['😀'] })
    await vi.waitFor(() => expect(asked).toHaveBeenCalledTimes(GLYPH_ASKS_IN_FLIGHT + 1))
    expect(await answers[0]).toEqual(['😀'])
    while (asked.mock.calls.length < answers.length || pending.length > 0) {
      await vi.waitFor(() => expect(pending.length).toBeGreaterThan(0))
      pending.shift()?.({ font: '', missing: [] })
    }
    await Promise.all(answers)
    expect(asked).toHaveBeenCalledTimes(answers.length)
  })
})

describe('useMissingGlyphs', () => {
  beforeEach(() => forgetGlyphAnswers())
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('keeps the last answer while a new text waits on the debounce and the server', async () => {
    const pending: ((coverage: GlyphCoverage) => void)[] = []
    vi.spyOn(api, 'getGlyphs').mockImplementation(
      (font, text) =>
        text === 'Hi 😀'
          ? Promise.resolve({ font, missing: ['😀'] })
          : new Promise<GlyphCoverage>((resolve) => pending.push(resolve)),
    )
    const { result, rerender } = renderHook(({ text }) => useMissingGlyphs('Lobster Two', text), {
      initialProps: { text: 'Hi 😀' },
    })
    await waitFor(() => expect(result.current).toEqual(['😀']))

    vi.useFakeTimers()
    rerender({ text: 'Hi 😀 there' })
    // A keystroke: the warning stays rather than blinking off.
    expect(result.current).toEqual(['😀'])
    await act(async () => {
      await vi.advanceTimersByTimeAsync(GLYPH_CHECK_DEBOUNCE_MS)
    })
    // Asked, not yet answered: still the last answer.
    expect(pending).toHaveLength(1)
    expect(result.current).toEqual(['😀'])
    vi.useRealTimers()
    await act(async () => {
      pending[0]?.({ font: 'Lobster Two', missing: [] })
    })
    expect(result.current).toEqual([])
  })

  // #3781: the answer belongs to its font, and a pause in asking forgets it.
  it("never shows one font's missing characters for another, nor after it stopped asking", async () => {
    vi.spyOn(api, 'getGlyphs').mockImplementation((font, text) =>
      font === 'Lobster Two' && text === 'Hi 😀'
        ? Promise.resolve({ font, missing: ['😀'] })
        : new Promise<GlyphCoverage>(() => {}),
    )
    const { result, rerender } = renderHook(
      ({ font, enabled }: { font: string; enabled: boolean }) => useMissingGlyphs(font, 'Hi 😀', enabled),
      { initialProps: { font: 'Lobster Two', enabled: true } },
    )
    await waitFor(() => expect(result.current).toEqual(['😀']))

    rerender({ font: 'Roboto', enabled: true })
    expect(result.current).toEqual([])

    rerender({ font: 'Lobster Two', enabled: false })
    expect(result.current).toEqual([])
    vi.useFakeTimers()
    rerender({ font: 'Lobster Two', enabled: true })
    // Asking again, the debounce not yet run: nothing carried over from before the pause.
    expect(result.current).toEqual([])
  })
})
