import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { RgbaSwatch } from './RgbaSwatch'

describe('RgbaSwatch', () => {
  it('draws the colour, alpha and all, over a checkerboard', () => {
    render(<RgbaSwatch rgba="#ffffff80" />)
    const chip = screen.getByTestId('rgba-swatch')
    expect(chip).toHaveAttribute('data-colour', '#FFFFFF80')
    const checker = chip.querySelector('[data-layer="checker"]')
    const colour = chip.querySelector<HTMLElement>('[data-layer="colour"]')
    expect(checker).not.toBeNull()
    expect(checker?.className).toContain('conic-gradient')
    expect(colour?.style.backgroundColor).toBe('rgba(255, 255, 255, 0.502)')
    // The checker is under the colour.
    expect(checker!.compareDocumentPosition(colour!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('reads #RRGGBB as opaque', () => {
    render(<RgbaSwatch rgba="#1b6ca8" />)
    expect(screen.getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#1B6CA8FF')
  })

  it('shows the grams left as an arc only when both weights are known', () => {
    const { rerender } = render(<RgbaSwatch rgba="#1B6CA8FF" gramsLeft={250} labelWeight={1000} />)
    const arc = screen.getByTestId('rgba-swatch').querySelector('[data-layer="grams"]')
    expect(arc).toHaveAttribute('data-fraction', '0.25')

    rerender(<RgbaSwatch rgba="#1B6CA8FF" gramsLeft={250} labelWeight={null} />)
    expect(screen.getByTestId('rgba-swatch').querySelector('[data-layer="grams"]')).toBeNull()
    rerender(<RgbaSwatch rgba="#1B6CA8FF" gramsLeft={null} labelWeight={1000} />)
    expect(screen.getByTestId('rgba-swatch').querySelector('[data-layer="grams"]')).toBeNull()
  })

  it('keeps the arc within an empty and a full spool', () => {
    const { rerender } = render(<RgbaSwatch rgba="#1B6CA8FF" gramsLeft={1300} labelWeight={1000} />)
    expect(screen.getByTestId('rgba-swatch').querySelector('[data-layer="grams"]')).toHaveAttribute('data-fraction', '1')
    rerender(<RgbaSwatch rgba="#1B6CA8FF" gramsLeft={-5} labelWeight={1000} />)
    expect(screen.getByTestId('rgba-swatch').querySelector('[data-layer="grams"]')).toHaveAttribute('data-fraction', '0')
  })
})
