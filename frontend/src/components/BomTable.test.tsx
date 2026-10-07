import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { BomTable } from './BomTable'

describe('BomTable', () => {
  it('lists every piece with its count and plates', () => {
    render(
      <BomTable
        bom={[
          { piece: 'wall', label: 'Wall', count: 8, plates: [1, 2], part: 'k1' },
          { piece: 'floor', label: 'Floor tile', count: 2, plates: [], part: null },
        ]}
      />,
    )
    const rows = screen.getAllByRole('row')
    expect(rows).toHaveLength(3)
    expect(rows[1]).toHaveTextContent('Wall')
    expect(rows[1]).toHaveTextContent('8')
    expect(rows[1]).toHaveTextContent('1, 2')
  })

  it('renders nothing for an empty bill', () => {
    const { container } = render(<BomTable bom={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
