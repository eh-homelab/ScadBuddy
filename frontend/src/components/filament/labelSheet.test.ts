import QRCode from 'qrcode'
import { describe, expect, it } from 'vitest'
import { labelBase, labelPayload, locationPayload, parseLabelPayload, QR_MM, qrSvg } from './labelSheet'

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

describe('the location payload', () => {
  it('builds the address of a code', () => {
    expect(locationPayload('https://sb.example.com/', 'L-12')).toBe('https://sb.example.com/x/L-12')
    expect(locationPayload('https://sb.example.com', 'L-12')).toBe('https://sb.example.com/x/L-12')
  })

  it('keeps the module at least 0.5 mm for the longest base', () => {
    const payload = locationPayload(`https://${'a'.repeat(56)}`, 'L-9999999')
    expect(payload.length - 'L-9999999'.length - 3).toBe(64)
    const { modules } = QRCode.create(payload, { errorCorrectionLevel: 'M' })
    expect(modules.size).toBeLessThanOrEqual(37)
    expect(QR_MM / modules.size).toBeGreaterThanOrEqual(0.5)
  })

  it('takes the base from the setting, else the page origin, with no trailing slash', () => {
    expect(labelBase('https://sb.example.com/')).toBe('https://sb.example.com')
    expect(labelBase(null)).toBe(window.location.origin)
  })
})
