import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PRINTERS_NOW, setPrintersNow } from '../../mocks/features/printersNow'
import { PlateClearNotice } from './PlateClearNotice'

describe('PlateClearNotice (#3632)', () => {
  it("repeats the printer's plate warning where the next print starts", async () => {
    setPrintersNow(PRINTERS_NOW.map((printer) => (printer.printer_id === 2 ? { ...printer, awaiting_plate_clear: true } : printer)))
    const { rerender } = render(<PlateClearNotice printerId={2} />)

    const warning = await screen.findByTestId('print-plate-warning')
    expect(warning).toHaveTextContent('Clear the plate of 3DP-77A-114 before the next print.')
    expect(warning).toHaveClass('text-warn')

    // Another printer's plate is its own.
    rerender(<PlateClearNotice printerId={1} />)
    expect(screen.queryByTestId('print-plate-warning')).toBeNull()
  })
})
