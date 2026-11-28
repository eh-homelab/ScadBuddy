import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { SpoolDetail } from '../../api/filament'
import { Label, CodeLabel } from './Label'
import { resetMeasuring } from './labelSheet'

const spool = (colour: string) =>
  ({ id: 's1', number: 42, colour_name: colour, effective: { colour_name: colour }, names: { material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab' } }) as unknown as SpoolDetail

describe('a spool label', () => {
  it('shows the number large, the material, the colour and the brand', () => {
    render(<Label spool={spool('Jade White')} svg="<svg></svg>" />)
    expect(screen.getByText('#42')).toBeInTheDocument()
    expect(screen.getByText('PLA Basic')).toBeInTheDocument()
    expect(screen.getByText('Jade White')).toBeInTheDocument()
    expect(screen.getByText('Bambu Lab')).toBeInTheDocument()
  })

  it('is 50 mm wide and cuts a long colour name with an ellipsis', () => {
    const name = 'Very long colour '.repeat(8).slice(0, 120)
    render(<Label spool={spool(name)} svg="<svg></svg>" />)
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 50mm; height: 25mm/)
    expect(screen.getByText(name)).toHaveClass('truncate')
  })

  it('shrinks the number so #10000 fits the text column', () => {
    const big = { ...spool('Red'), number: 10000 } as SpoolDetail
    render(<Label spool={big} svg="<svg></svg>" />)
    const size = parseFloat(screen.getByText('#10000').style.fontSize)
    expect(size).toBeLessThan(9)
    expect(size * 0.62 * '#10000'.length).toBeLessThanOrEqual(22.01)
  })
})

describe('a spool label with chosen fields (#7216)', () => {
  const full = { ...spool('Jade White'), effective: { colour_name: 'Jade White', rgba: '#00ae42ff' } } as unknown as SpoolDetail

  it('shows only the chosen fields', () => {
    render(<Label spool={full} svg="<svg></svg>" fields={['number', 'hex']} />)
    expect(screen.getByText('#42')).toBeInTheDocument()
    expect(screen.getByText('#00AE42')).toBeInTheDocument()
    expect(screen.queryByText('Bambu Lab')).toBeNull()
    expect(screen.queryByText('PLA Basic')).toBeNull()
  })

  it('draws no QR box when the QR is not chosen', () => {
    render(<Label spool={full} svg="<svg></svg>" fields={['number', 'hex']} />)
    expect(screen.getByTestId('label').querySelector('svg')).toBeNull()
  })

  it('draws the QR box by default', () => {
    render(<Label spool={full} svg="<svg></svg>" />)
    expect(screen.getByTestId('label').querySelector('svg')).not.toBeNull()
  })

  it('leaves off the lines that do not fit, from the bottom', () => {
    const all = { ...full, effective: { ...full.effective, label_weight_g: 1000 }, names: { ...full.names, location: 'Shelf A' } } as unknown as SpoolDetail
    render(<Label spool={all} svg="<svg></svg>" fields={['number', 'material', 'colour', 'hex', 'brand', 'location', 'weight']} />)
    expect(screen.getByText('#00AE42')).toBeInTheDocument()
    expect(screen.queryByText('Bambu Lab')).toBeNull()
    expect(screen.queryByText('1000 g')).toBeNull()
  })

  it('gives the number the QR column too when the QR is not chosen', () => {
    resetMeasuring()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      font: '',
      measureText: (text: string) => ({ width: 60 * text.length }),
    } as unknown as CanvasRenderingContext2D)
    render(<Label spool={{ ...full, number: 10000 } as SpoolDetail} svg="<svg></svg>" fields={['number']} />)
    const size = parseFloat(screen.getByText('#10000').style.fontSize)
    // 50 mm less the quiet zone and the padding leaves 45.9 mm: the cap holds.
    expect(size).toBe(9)
  })

  afterEach(() => {
    vi.restoreAllMocks()
    resetMeasuring()
  })
})

describe('a label at another size (#7157)', () => {
  it('draws a spool label at the size it is given', () => {
    render(<Label spool={spool('Red')} svg="<svg></svg>" size={{ widthMm: 62, heightMm: 29, sheet: null }} />)
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 62mm; height: 29mm/)
  })

  it('shrinks the number to the narrower text column of a 40 mm label', () => {
    render(<Label spool={{ ...spool('Red'), number: 999 } as SpoolDetail} svg="<svg></svg>" size={{ widthMm: 40, heightMm: 30, sheet: null }} />)
    const size = parseFloat(screen.getByText('#999').style.fontSize)
    // 40 mm less the QR box, its quiet zones and the padding leaves 13.5 mm, 12 mm for the number.
    expect(size * 0.62 * '#999'.length).toBeLessThanOrEqual(12.01)
  })

  it('draws a code label at the size it is given', () => {
    render(<CodeLabel code="L-3" name="Shelf" svg="<svg></svg>" size={{ widthMm: 75, heightMm: 55, sheet: null }} />)
    expect(screen.getByTestId('label').getAttribute('style')).toMatch(/width: 75mm; height: 55mm/)
  })
})

describe('a label measured in the browser', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    resetMeasuring()
  })

  // jsdom has no canvas: the stub measures each character as 60 px wide at 100 px.
  const stubCanvas = () => {
    resetMeasuring()
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      font: '',
      measureText: (text: string) => ({ width: 60 * text.length }),
    } as unknown as CanvasRenderingContext2D)
  }

  it('sizes the number so its measured width fills the 23.5 mm column', () => {
    stubCanvas()
    const big = { ...spool('Red'), number: 10000 } as SpoolDetail
    render(<Label spool={big} svg="<svg></svg>" />)
    const text = screen.getByText('#10000')
    const size = parseFloat(text.style.fontSize)
    expect(size * 0.6 * '#10000'.length).toBeCloseTo(23.5, 1)
    expect(text.style.fontFamily).toMatch(/Helvetica/)
  })

  it('measures every digit as wide as the widest, as tabular-nums renders it', () => {
    resetMeasuring()
    // A 1 is 40 px wide, every other digit 60: tabular-nums gives the 1 the 60 px as well.
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
      font: '',
      measureText: (text: string) => ({ width: [...text].reduce((sum, c) => sum + (c === '1' ? 40 : 60), 0) }),
    } as unknown as CanvasRenderingContext2D)
    const big = { ...spool('Red'), number: 11111 } as SpoolDetail
    render(<Label spool={big} svg="<svg></svg>" />)
    const size = parseFloat(screen.getByText('#11111').style.fontSize)
    expect(size * 0.6 * '#11111'.length).toBeCloseTo(23.5, 1)
  })

  it('keeps the cap when the measured text fits', () => {
    stubCanvas()
    render(<Label spool={spool('Red')} svg="<svg></svg>" />)
    expect(parseFloat(screen.getByText('#42').style.fontSize)).toBe(9)
  })
})
