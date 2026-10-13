import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { parseFontValue } from './fonts'
import { useDebounced } from './useDebounced'

/** How long the text and font must sit still before the server is asked. */
export const GLYPH_CHECK_DEBOUNCE_MS = 400

/** One answer per font and text: the picker asks for the same pairs on every open. */
const answers = new Map<string, Promise<string[]>>()

/** Forgets every answer: a font was installed, or a test starts clean. */
export function forgetGlyphAnswers(): void {
  answers.clear()
}

function missingFor(font: string, text: string): Promise<string[]> {
  const key = `${font}\n${text}`
  const known = answers.get(key)
  if (known) return known
  const answer = api.getGlyphs(font, text).then(
    (coverage) => coverage.missing ?? [],
    () => {
      // Not an answer: asked again next time.
      answers.delete(key)
      return []
    },
  )
  answers.set(key, answer)
  return answer
}

/**
 * #2358 — the characters of `text` the installed font `font` has no glyph for, which
 * OpenSCAD's `text()` leaves out without a word. Empty until the server answers, for the
 * default font, and when it cannot say. The browser cannot tell: it draws a missing
 * glyph in a fallback font, so every font looks as if it had it.
 */
export function useMissingGlyphs(font: string, text: string, enabled = true): string[] {
  const asked = useDebounced(`${font}\n${text}`, GLYPH_CHECK_DEBOUNCE_MS)
  const [answer, setAnswer] = useState<{ asked: string; missing: string[] }>()
  const ask = enabled && parseFontValue(font).family !== '' && text.trim() !== ''
  useEffect(() => {
    if (!ask) return
    // A font value holds no newline; the text may.
    const cut = asked.indexOf('\n')
    const askedFont = asked.slice(0, cut)
    const askedText = asked.slice(cut + 1)
    let current = true
    void missingFor(askedFont, askedText).then((missing) => {
      if (current) setAnswer({ asked, missing })
    })
    return () => {
      current = false
    }
  }, [ask, asked])
  return ask && answer?.asked === `${font}\n${text}` ? answer.missing : []
}
