import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { parseFontValue } from './fonts'
import { useDebounced } from './useDebounced'

/** How long the text and font must sit still before the server is asked. */
export const GLYPH_CHECK_DEBOUNCE_MS = 400

/** The answers kept: one per font and text, the least recently asked forgotten first. */
export const GLYPH_ANSWERS_KEPT = 200

/**
 * Asks in flight at once: a picker opens with a row per installed family, and each
 * ask is an `fc-match` on the server, so the rest wait their turn.
 */
export const GLYPH_ASKS_IN_FLIGHT = 4

/** One answer per font and text: the picker asks for the same pairs on every open. */
const answers = new Map<string, Promise<string[]>>()

let inFlight = 0
const waiting: (() => void)[] = []

/** Forgets every answer: a font was installed, or a test starts clean. */
export function forgetGlyphAnswers(): void {
  answers.clear()
}

async function inTurn<T>(run: () => Promise<T>): Promise<T> {
  if (inFlight < GLYPH_ASKS_IN_FLIGHT) inFlight += 1
  else await new Promise<void>((resolve) => waiting.push(resolve))
  try {
    return await run()
  } finally {
    // The slot passes straight to the next ask, so none slips in between.
    const next = waiting.shift()
    if (next) next()
    else inFlight -= 1
  }
}

/** The characters `font` lacks of `text`, asked once and kept (exported for tests). */
export function askMissingGlyphs(font: string, text: string): Promise<string[]> {
  const key = `${font}\n${text}`
  const known = answers.get(key)
  if (known) {
    // Asked again: the most recent now.
    answers.delete(key)
    answers.set(key, known)
    return known
  }
  const answer = inTurn(() => api.getGlyphs(font, text)).then(
    (coverage) => coverage.missing ?? [],
    () => {
      // Not an answer: asked again next time.
      answers.delete(key)
      return []
    },
  )
  answers.set(key, answer)
  if (answers.size > GLYPH_ANSWERS_KEPT) {
    const oldest = answers.keys().next().value
    if (oldest !== undefined) answers.delete(oldest)
  }
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
    void askMissingGlyphs(askedFont, askedText).then((missing) => {
      if (current) setAnswer({ asked, missing })
    })
    return () => {
      current = false
    }
  }, [ask, asked])
  return ask && answer?.asked === `${font}\n${text}` ? answer.missing : []
}
