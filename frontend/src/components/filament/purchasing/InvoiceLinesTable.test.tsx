import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { Spool } from '../../../api/filament'
import type { InvoiceLine } from '../../../api/filamentPurchasing'
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

  it('links a confirmed line to its spools, type and product, marks a deleted spool and the line a link names (#6869)', () => {
    const line = {
      id: 'l1',
      invoice_id: 'i1',
      position: 1,
      description: 'PLA white',
      sku: 'A00-W1',
      colour_code: null,
      quantity: 2,
      unit_price: '20.0000',
      type_id: 't1',
      supplier_product_id: 'p1',
      allocated_cost: '40.0000',
      line_tax: '0.0000',
      tax_basis: 'none',
      spool_ids: ['a', 'gone'],
    } as unknown as InvoiceLine
    render(
      <MemoryRouter initialEntries={['/filament/invoices/i1#line-l1']}>
        <InvoiceLinesTable
          editable={false}
          lines={[line]}
          supplierId="s1"
          types={[{ id: 't1', code: 'BL-PLA-WHITE', rgba: '#FFFFFFFF' } as never]}
          spools={[spool('a', 12, null)]}
          money={(amount) => formatCost(amount, 'USD', 2)}
        />
      </MemoryRouter>,
    )
    const row = screen.getByRole('row', { name: /PLA white/ })
    expect(row).toHaveAttribute('id', 'line-l1')
    expect(row).toHaveAttribute('aria-current', 'location')
    expect(within(row).getByRole('link', { name: 'Spool #12' })).toHaveAttribute('href', '/filament/spools/a')
    expect(within(row).getByRole('link', { name: /BL-PLA-WHITE/ })).toHaveAttribute('href', '/filament/types/t1')
    expect(within(row).getByRole('link', { name: 'A00-W1' })).toHaveAttribute('href', '/filament/suppliers/s1#product-p1')
    const items = within(row).getAllByRole('listitem').map((item) => item.textContent)
    expect(items).toEqual(['Spool #12', 'Spool (deleted)'])
    expect(within(row).getAllByRole('link').map((one) => one.getAttribute('href'))).not.toContain('/filament/spools/gone')
  })
})
