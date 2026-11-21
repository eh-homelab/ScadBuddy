import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SpoolDetail } from '../../api/filament'
import { Label } from './Label'

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
