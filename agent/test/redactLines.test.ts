import { describe, expect, it } from 'vitest'
import { lineRedactor } from '../src/harness/redactLines.js'

const SECRET = 'sk-ant-api03-split-secret-abcdef123456'

function collect(maxBuffer?: number) {
  const out: string[] = []
  const r = lineRedactor([SECRET, undefined], (l) => out.push(l), maxBuffer)
  return { out, r }
}

describe('lineRedactor', () => {
  it('redacts a secret split across chunks at every position', () => {
    for (let cut = 1; cut < SECRET.length; cut++) {
      const { out, r } = collect()
      r.write(`auth ${SECRET.slice(0, cut)}`)
      r.write(`${SECRET.slice(cut)} failed\n`)
      expect(out).toEqual(['auth [redacted] failed\n'])
    }
  })

  it('handles one chunk holding several lines, and a secret split over three chunks', () => {
    const { out, r } = collect()
    r.write(`a\nb ${SECRET.slice(0, 5)}`)
    r.write(SECRET.slice(5, 20))
    r.write(`${SECRET.slice(20)}\nc`)
    expect(out).toEqual(['a\n', 'b [redacted]\n'])
    r.flush()
    expect(out).toEqual(['a\n', 'b [redacted]\n', 'c'])
  })

  it('flushes a last line without a newline, redacted, once', () => {
    const { out, r } = collect()
    r.write(`exit: ${SECRET}`)
    expect(out).toEqual([])
    r.flush()
    r.flush()
    expect(out).toEqual(['exit: [redacted]'])
  })

  it('bounds a runaway line without splitting a secret at the cut', () => {
    const { out, r } = collect(64)
    const filler = 'x'.repeat(50)
    r.write(filler)
    r.write(`${SECRET.slice(0, 20)}`) // buffer is now over the limit mid-secret
    r.write(`${SECRET.slice(20)}${filler}`)
    r.flush()
    const all = out.join('')
    expect(all).not.toContain(SECRET.slice(0, 20))
    expect(all).toBe(`${filler}[redacted]${filler}`)
    expect(out.length).toBeGreaterThan(1)
  })
})
