import { describe, expect, it } from 'vitest'
import { displayHex, swatchStyle } from './filamentSwatch'

describe('filament swatch (#6209)', () => {
  it('shows the 8-digit hex only below FF', () => {
    expect(displayHex('#000000', 0)).toBe('#00000000')
    expect(displayHex('#e3ecef', 128)).toBe('#E3ECEF80')
    expect(displayHex('#ffffff', 255)).toBe('#FFFFFF')
    expect(displayHex('#ffffff')).toBe('#FFFFFF')
  })

  it('draws a clear filament over a checkerboard, not as black', () => {
    const style = swatchStyle({ colour: '#000000', alpha: 0 })
    expect(style.backgroundImage).toBe('linear-gradient(#00000000, #00000000), repeating-conic-gradient(#979797 0% 25%, #f5f5f5 0% 50%)')
    expect(style.backgroundSize).toBe('cover, 8px 8px')
  })

  it('draws multicolor stops as a conic gradient, other stops as a linear one', () => {
    expect(swatchStyle({ colour: '#7F3696', extra_colours: ['#7F3696', '#3A8FD2'], effect: 'multicolor' }).backgroundImage).toMatch(
      /^conic-gradient\(from 0deg, #7F3696 0\.000deg 180\.000deg, #3A8FD2 180\.000deg 360\.000deg\)/,
    )
    expect(swatchStyle({ colour: '#7F3696', extra_colours: ['#7F3696', '#3A8FD2'], subtype: 'Multicolor' }).backgroundImage).toMatch(/^conic-gradient/)
    expect(swatchStyle({ colour: '#7F3696', extra_colours: ['#7F3696', '#3A8FD2'], effect: 'gradient' }).backgroundImage).toMatch(
      /^linear-gradient\(135deg, #7F3696, #3A8FD2\)/,
    )
  })

  it('lays an effect overlay on top, and none for an effect without one', () => {
    const silk = swatchStyle({ colour: '#C49449', effect: 'silk' })
    expect(String(silk.backgroundImage).startsWith('linear-gradient(110deg')).toBe(true)
    expect(silk.backgroundSize).toBe('cover, cover, 8px 8px')
    expect(swatchStyle({ colour: '#C49449', effect: 'translucent' }).backgroundSize).toBe('cover, 8px 8px')
  })
})
