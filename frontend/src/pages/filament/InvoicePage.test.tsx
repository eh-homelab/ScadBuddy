import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { createInvoice, getInvoice } from '../../api/filamentPurchasing'
import { cmpDecimals } from '../../lib/decimal'
import { spoolId, T } from '../../mocks/features/filament/seed'
import { INVOICE, SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { InvoicePage } from './InvoicePage'

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/filament/invoices/:id" element={<InvoicePage />} />
      <Route path="/filament/invoices" element={<p>The invoices tab</p>} />
      <Route path="*" element={<p>Elsewhere</p>} />
    </Routes>,
    { route: `/filament/invoices/${id}` },
  )
}

const heading = () => screen.findByRole('heading', { level: 1, name: /Invoice/ })

function requests() {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => {
    seen.push(`${request.method} ${new URL(request.url).pathname}`)
  })
  return seen
}

/** A Bambu draft whose second line names no type. */
const withUntypedLine = () =>
  createInvoice({
    supplier_id: SUPPLIER.BAMBU,
    number: 'R-2',
    invoice_date: '2026-11-16',
    currency: 'USD',
    subtotal: '25.99',
    total: '25.99',
    lines: [
      { description: 'PLA Basic Jade White', sku: 'A00-W1', quantity: 1, unit_price: '15.99', type_id: T.JADE },
      { description: 'Mystery spool', quantity: 1, unit_price: '10.00' },
    ],
  })

beforeEach(() => {
  window.localStorage.clear()
})

describe('the invoice page', () => {
  it('names the invoice and shows its file beside the lines', async () => {
    render(INVOICE.DRAFT_A)
    expect(await heading()).toHaveTextContent('US-452318')
    expect(await screen.findByText('Bambu Lab')).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Invoice lines' })).toBeInTheDocument()
    const file = await screen.findByTitle('Invoice file')
    expect(file).toHaveAttribute('data', `/api/v1/filament/invoices/${INVOICE.DRAFT_A}/file`)
    expect(screen.getByRole('link', { name: 'Open' })).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument()
  })

  it('disables Confirm with the reason until every line has a type', async () => {
    const draft = await withUntypedLine()
    const { user } = render(draft.id)
    await heading()
    const confirm = await screen.findByRole('button', { name: 'Confirm' })
    expect(confirm).toBeDisabled()
    expect(screen.getByText('Line 2 has no type')).toBeInTheDocument()
    await choose(user, await screen.findByRole('combobox', { name: 'Type, line 2' }), 'BL-PLA-BASIC-BLACK')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled())
    expect(screen.queryByText('Line 2 has no type')).not.toBeInTheDocument()
  })

  it('saves a line after an edit, once, and shows the server cost and tax basis', async () => {
    const draft = await createInvoice({
      supplier_id: SUPPLIER.BAMBU,
      invoice_date: '2026-11-16',
      currency: 'USD',
      subtotal: '100',
      tax: '7',
      total: '107',
      lines: [{ description: 'PLA Basic Jade White', quantity: 1, unit_price: '100', type_id: T.JADE }],
    })
    const seen = requests()
    const { user } = render(draft.id)
    await heading()
    const lines = await screen.findByRole('table', { name: 'Invoice lines' })
    expect(await within(lines).findByText(/receipt-wide 7\.0%/)).toBeInTheDocument()
    expect(screen.getByText(/\(receipt-wide 7\.0%\)/)).toBeInTheDocument()
    const price = await screen.findByLabelText('Unit price, line 1')
    await user.clear(price)
    await user.type(price, '50')
    await user.tab()
    await waitFor(() => expect(seen.filter((one) => one === `PUT /api/v1/filament/invoices/${draft.id}/lines`)).toHaveLength(1))
    await waitFor(async () => expect(cmpDecimals((await getInvoice(draft.id)).lines[0]!.unit_price, '50')).toBe(0))
  })

  it('shows a bad number inline and sends nothing', async () => {
    const draft = await withUntypedLine()
    const seen = requests()
    const { user } = render(draft.id)
    await heading()
    const price = await screen.findByLabelText('Unit price, line 1')
    await user.clear(price)
    await user.type(price, '1x')
    await user.tab()
    expect(await screen.findByText('Enter a price such as 12.50.')).toBeInTheDocument()
    expect(seen.some((one) => one.startsWith('PUT'))).toBe(false)
  })

  it('confirms with one POST, then shows the invoice read-only with its spools linked', async () => {
    const seen = requests()
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    await user.click(await screen.findByRole('button', { name: 'Confirm' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('5 spools will be made, 0 filled, 2 price points')
    await user.click(within(dialog).getByRole('button', { name: 'Make the spools' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument())
    expect(seen.filter((one) => one === `POST /api/v1/filament/invoices/${INVOICE.DRAFT_A}/confirm`)).toHaveLength(1)
    expect(screen.getByText('Confirmed')).toBeInTheDocument()
    const lines = screen.getByRole('table', { name: 'Invoice lines' })
    expect(within(lines).getAllByRole('link', { name: /^Spool #/ })).toHaveLength(5)
    expect(screen.queryByLabelText('Unit price, line 1')).not.toBeInTheDocument()
  })

  it('keeps a confirmed invoice frozen except the note and the file', async () => {
    const { user } = render(INVOICE.CONFIRMED_A)
    await heading()
    expect(await screen.findByText('Confirmed')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete draft' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Total')).toBeDisabled()
    const note = screen.getByLabelText('Note')
    expect(note).toBeEnabled()
    await user.type(note, 'Paid by card')
    await user.tab()
    await waitFor(async () => expect((await getInvoice(INVOICE.CONFIRMED_A)).note).toBe('Paid by card'))
    expect(screen.getByRole('button', { name: 'Replace file' })).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'Invoice lines' })).getByRole('link', { name: `Spool #1` })).toHaveAttribute('href', `/filament/spools/${spoolId(1)}`)
  })

  it('says a number is used, and links to the invoice that has it', async () => {
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    const number = await screen.findByLabelText('Number')
    await user.clear(number)
    await user.type(number, 'US-441907')
    await user.tab()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/supplier has an invoice with that number/)
    expect(within(alert).getByRole('link', { name: /Open that invoice/ })).toHaveAttribute('href', `/filament/invoices/${INVOICE.CONFIRMED_A}`)
  })

  it('asks before it deletes a draft', async () => {
    const { user } = render(INVOICE.DRAFT_B)
    await heading()
    await user.click(await screen.findByRole('button', { name: 'Delete draft' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    expect(await screen.findByText('The invoices tab')).toBeInTheDocument()
  })

  it('shows the totals with the difference', async () => {
    render(INVOICE.DRAFT_A)
    await heading()
    expect(await screen.findByRole('status')).toHaveTextContent(/reconciles/i)
  })
})
