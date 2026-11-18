import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { INVOICE, SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { InvoicesTab } from './InvoicesTab'
import { setRemembering } from '../../components/filament/purchasing/invoicesQuery'

function render(route = '/filament/invoices') {
  return renderPage(
    <Routes>
      <Route path="/filament/invoices" element={<InvoicesTab />} />
      <Route path="/filament/invoices/:id" element={<p>An invoice</p>} />
    </Routes>,
    { route },
  )
}

const rows = () => screen.getAllByRole('row').filter((row) => row.dataset.invoice)

beforeEach(() => {
  window.localStorage.clear()
})

describe('the invoices tab', () => {
  afterEach(() => vi.restoreAllMocks())

  it('lists every invoice with its status, supplier, number and total', async () => {
    render()
    await waitFor(() => expect(rows()).toHaveLength(4))
    const draft = rows().find((row) => row.dataset.invoice === INVOICE.DRAFT_A)!
    expect(within(draft).getByText('Draft')).toBeInTheDocument()
    expect(within(draft).getByRole('link', { name: 'US-452318' })).toHaveAttribute('href', `/filament/invoices/${INVOICE.DRAFT_A}`)
    expect(within(draft).getByText('Bambu Lab')).toBeInTheDocument()
    expect(within(draft).getByText(/95\.20/)).toBeInTheDocument()
    expect(within(draft).getByLabelText('Has a file')).toBeInTheDocument()
    const confirmed = rows().find((row) => row.dataset.invoice === INVOICE.CONFIRMED_B)!
    expect(within(confirmed).getByText('Confirmed')).toBeInTheDocument()
    expect(within(confirmed).getByLabelText('Spools made or filled')).toHaveTextContent('1')
    expect(within(draft).getByLabelText('Spools made or filled')).toHaveTextContent('0')
    expect(screen.getByRole('columnheader', { name: 'Spools' })).toBeInTheDocument()
    expect(within(confirmed).queryByLabelText('Has a file')).not.toBeInTheDocument()
  })

  it('filters by status and by supplier', async () => {
    const { user } = render()
    await waitFor(() => expect(rows()).toHaveLength(4))
    await choose(user, screen.getByRole('combobox', { name: 'Status' }), 'Drafts')
    await waitFor(() => expect(rows()).toHaveLength(2))
    await choose(user, screen.getByRole('combobox', { name: 'Filter by supplier' }), 'Micro Center')
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]!.dataset.invoice).toBe(INVOICE.DRAFT_B)
  })

  it('opens an address with a query as it says', async () => {
    render(`/filament/invoices?status=confirmed&supplier=${SUPPLIER.AMAZON}`)
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]!.dataset.invoice).toBe(INVOICE.CONFIRMED_B)
  })

  it('reopens the remembered view for a visit with no query', async () => {
    setRemembering(true, { status: 'confirmed' })
    render()
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(rows().every((row) => within(row).queryByText('Draft') === null)).toBe(true)
  })

  it('remembers the view once "Remember my view" is on, and still renders when storage throws', async () => {
    const { user } = render()
    await waitFor(() => expect(rows()).toHaveLength(4))
    await user.click(screen.getByRole('switch', { name: 'Remember my view' }))
    await choose(user, screen.getByRole('combobox', { name: 'Status' }), 'Drafts')
    await waitFor(() => expect(JSON.parse(window.localStorage.getItem('scadbuddy.filament.invoices.view')!)).toMatchObject({ on: true, query: { status: 'draft' } }))
  })

  it('renders without storage', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    render()
    await waitFor(() => expect(rows()).toHaveLength(4))
  })
})
