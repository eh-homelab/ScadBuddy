import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Spool } from '../../../api/filament'
import { formatCost } from '../../../lib/decimal'
import { InvoiceLinesTable } from './InvoiceLinesTable'
import { blankLine } from './invoiceLines'

const spool = (id: string, number: number, cost: string | null, line: string | null = null) =>
  ({ id, number, cost_per_kg: cost, invoice_line_id: line, type_id: null, archived_at: null, rgba: null, colour_name: null }) as unknown as Spool

const show = (spools: Spool[], ticked: string[]) =>
  render(
    <MemoryRouter>
      <InvoiceLinesTable
        editable
        rows={[{ ...blankLine(), description: 'PLA white', quantity: '2', unitPrice: '20', updateSpoolIds: ticked }]}
        computed={new Map()}
        errors={{}}
        products={[]}
        types={[]}
        spools={spools}
        money={(amount) => formatCost(amount, 'USD', 2)}
        onChange={() => undefined}
        onCommit={() => undefined}
      />
    </MemoryRouter>,
  )

// #6700 R-6700-5: a filled spool that has its own cost says what the line replaces.
describe('the invoice lines table', () => {
  it('marks a filled spool that has a cost with the cost per kg the line replaces', () => {
    show([spool('a', 12, '9.0000'), spool('b', 13, null)], ['a', 'b'])
    const line = screen.getByRole('row', { name: /PLA white/ })
    expect(line).toHaveTextContent('Spool #12 replaces $9.00/kg')
    expect(line).not.toHaveTextContent('Spool #13 replaces')
  })

  // R-6700-7: the picker offers a costed spool with no invoice line, with its note, and
  // never a spool on another invoice.
  it('offers a costed spool with no invoice line and never one on another invoice', async () => {
    show([spool('b', 13, null), spool('a', 12, '9.0000'), spool('c', 14, '8.0000', 'line-x')], [])
    await userEvent.click(screen.getByRole('button', { name: 'Fills existing spools, line 1' }))
    const offered = within(screen.getByRole('dialog')).getAllByRole('listitem')
    expect(offered.map((item) => item.textContent)).toEqual(['Spool #13', 'Spool #12replaces $9.00/kg'])
  })

  it('marks nothing when no filled spool has a cost', () => {
    show([spool('b', 13, null)], ['b'])
    expect(screen.queryByText(/replaces/)).not.toBeInTheDocument()
  })
})
