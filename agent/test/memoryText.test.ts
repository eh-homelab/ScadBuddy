import { describe, expect, it } from 'vitest'
import { MEMORY_TEXT_MAX, memoryText } from '../src/sessions/manager.js'

// A memory event's text is redacted before it is capped (#842 review).
describe('memoryText', () => {
  const secret = 'sk-live-0123456789abcdef'

  it('leaks no fragment of a secret the cap would split', () => {
    const text = `${'x'.repeat(MEMORY_TEXT_MAX - 8)}${secret} and more`
    const out = memoryText(text, [secret])
    expect(out).not.toContain(secret.slice(0, 8))
  })

  it('caps long text and says how long it was', () => {
    const out = memoryText('y'.repeat(MEMORY_TEXT_MAX + 10), [])
    expect(out).toBe(`${'y'.repeat(MEMORY_TEXT_MAX)}… (${MEMORY_TEXT_MAX + 10} characters)`)
  })

  it('leaves short text as it was, apart from redaction', () => {
    expect(memoryText(`key ${secret}`, [secret])).toBe('key [redacted]')
  })
})
