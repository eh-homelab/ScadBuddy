import { describe, expect, it } from 'vitest'
import { labelPayload, parseLabelPayload, qrSvg } from './labelSheet'

describe('the label payload', () => {
  it('is the spool id behind a fixed prefix, with no host', () => {
    expect(labelPayload('0b9e1c')).toBe('scadbuddy:spool:0b9e1c')
  })

  it('reads the id back, and nothing from other text', () => {
    expect(parseLabelPayload('scadbuddy:spool:abc')).toBe('abc')
    expect(parseLabelPayload('https://x/y')).toBeNull()
    expect(parseLabelPayload('scadbuddy:spool:')).toBeNull()
  })

  it('draws an SVG that differs by payload', async () => {
    const a = await qrSvg(labelPayload('a'))
    const b = await qrSvg(labelPayload('b'))
    expect(a.startsWith('<svg')).toBe(true)
    expect(a).not.toBe(b)
  })
})
