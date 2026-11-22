import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { AssistantOpenerContext } from '../../agent/chat/opener'
import { createInvoice, getInvoice } from '../../api/filamentPurchasing'
import { cmpDecimals, formatCost } from '../../lib/decimal'
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
    expect(await screen.findByRole('combobox', { name: 'Supplier' })).toHaveTextContent('Bambu Lab')
    expect(screen.getByRole('table', { name: 'Invoice lines' })).toBeInTheDocument()
    const file = await screen.findByTitle('Invoice PDF, bambu-US-452318.pdf')
    expect(file).toHaveAttribute('src', `/api/v1/filament/invoices/${INVOICE.DRAFT_A}/file`)
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

  describe('the spools a confirm made, before the spools are read again (R-6869-5)', () => {
    /** Answers the spool list as usual until the invoice is confirmed, then with `after`. */
    function spoolsAfterConfirm(after: () => Promise<Response | undefined> | Response | undefined) {
      let confirmed = false
      server.use(
        http.post('/api/v1/filament/invoices/:id/confirm', () => {
          confirmed = true
          return undefined
        }),
        http.get('/api/v1/filament/spools', () => (confirmed ? after() : undefined)),
      )
    }

    async function confirmDraft() {
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      await user.click(await screen.findByRole('button', { name: 'Confirm' }))
      await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Make the spools' }))
      await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm' })).not.toBeInTheDocument())
      return screen.getByRole('table', { name: 'Invoice lines' })
    }

    it('links each new spool while the read is in flight, never as deleted', async () => {
      let release: () => void = () => {}
      const held = new Promise<void>((resolve) => (release = resolve))
      spoolsAfterConfirm(async () => {
        await held
        return undefined
      })
      const lines = await confirmDraft()
      expect(within(lines).getAllByRole('link', { name: /^Spool #/ })).toHaveLength(5)
      expect(lines).not.toHaveTextContent('(deleted)')
      release()
      await waitFor(() => expect(within(lines).getAllByRole('link', { name: /^Spool #\d+$/ })).toHaveLength(5))
    })

    it('never says deleted when the read fails', async () => {
      spoolsAfterConfirm(() => HttpResponse.json({ detail: 'down' }, { status: 500 }))
      const lines = await confirmDraft()
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(within(lines).getAllByRole('link', { name: /^Spool #/ })).toHaveLength(5)
      expect(lines).not.toHaveTextContent('(deleted)')
    })
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

  it('drops a lines answer that arrives after a newer header answer', async () => {
    const old = await getInvoice(INVOICE.DRAFT_A)
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    let sentLines = false
    server.use(
      http.put('*/api/v1/filament/invoices/:id/lines', async () => {
        sentLines = true
        await gate
        return HttpResponse.json(old)
      }),
    )
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    const price = await screen.findByLabelText('Unit price, line 1')
    await user.clear(price)
    await user.type(price, '20')
    await user.tab()
    await waitFor(() => expect(sentLines).toBe(true))
    const total = screen.getByLabelText('Total')
    await user.clear(total)
    await user.type(total, '90')
    await user.tab()
    const typed = () => screen.getByText('Total typed').nextElementSibling
    await waitFor(() => expect(typed()).toHaveTextContent('90.00'))
    release()
    await waitFor(() => expect(screen.queryByText('Saving the lines…')).not.toBeInTheDocument())
    expect(typed()).toHaveTextContent('90.00')
    expect(cmpDecimals((total as HTMLInputElement).value, '90')).toBe(0)
  })

  it('keeps what is typed in one field while another field\'s save returns', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    // Holds the answer; a resolver that returns nothing falls through to the mock.
    server.use(
      http.patch('*/api/v1/filament/invoices/:id', async () => {
        await gate
      }),
    )
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    const total = await screen.findByLabelText('Total')
    await user.clear(total)
    await user.type(total, '117')
    await user.tab()
    const note = screen.getByLabelText('Note')
    await user.type(note, 'Paid by card')
    release()
    await waitFor(() => expect(total).not.toHaveValue('117'))
    expect(cmpDecimals((total as HTMLInputElement).value, '117')).toBe(0)
    expect(note).toHaveValue('Paid by card')
    await user.tab()
    await waitFor(async () => expect((await getInvoice(INVOICE.DRAFT_A)).note).toBe('Paid by card'))
  })

  it('says the lines are not saved after a failed save, and saves them on Retry', async () => {
    let calls = 0
    server.use(
      http.put('*/api/v1/filament/invoices/:id/lines', () => {
        calls += 1
        if (calls > 1) return undefined
        return HttpResponse.json({ type: 'about:blank', title: 'Server error', status: 500, detail: 'The database is busy' }, { status: 500, headers: { 'Content-Type': 'application/problem+json' } })
      }),
    )
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    const description = await screen.findByLabelText('Description, line 1')
    await user.type(description, ' again')
    await user.tab()
    expect(await screen.findByRole('alert')).toHaveTextContent('The database is busy')
    expect(screen.getByText('The lines are not saved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled())
    expect(calls).toBe(2)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect((await getInvoice(INVOICE.DRAFT_A)).lines[0]!.description).toMatch(/ again$/)
  })

  it('moves a draft to another supplier and clears its lines\' products', async () => {
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
    await waitFor(async () => expect((await getInvoice(INVOICE.DRAFT_A)).supplier_id).toBe(SUPPLIER.MICRO))
    expect((await getInvoice(INVOICE.DRAFT_A)).lines.map((line) => line.supplier_product_id)).toEqual([null, null])
    expect(screen.getByRole('combobox', { name: 'Supplier' })).toHaveTextContent('Micro Center')
    expect(screen.getByRole('combobox', { name: 'Supplier product, line 1' })).toHaveTextContent('Product by SKU')
  })

  it('refuses a move onto a supplier that has the number, with a link to that invoice', async () => {
    const taken = await createInvoice({ supplier_id: SUPPLIER.MICRO, number: 'US-452318', invoice_date: '2026-11-01', currency: 'USD', subtotal: '1', total: '1' })
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByRole('link', { name: /Open that invoice/ })).toHaveAttribute('href', `/filament/invoices/${taken.id}`)
    expect((await getInvoice(INVOICE.DRAFT_A)).supplier_id).toBe(SUPPLIER.BAMBU)
  })

  it('shows a confirmed invoice\'s supplier as a link, not a choice', async () => {
    render(INVOICE.CONFIRMED_A)
    await heading()
    expect(await screen.findByRole('link', { name: 'Bambu Lab' })).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}`)
    expect(screen.queryByRole('combobox', { name: 'Supplier' })).not.toBeInTheDocument()
  })

  describe('after a save that fails', () => {
    const problem = (status: number, detail: string) => HttpResponse.json({ type: 'about:blank', title: 'Refused', status, detail }, { status, headers: { 'Content-Type': 'application/problem+json' } })
    const held = () => {
      let release!: () => void
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      return { gate, release }
    }

    it('shows the server\'s numbers when a header change is refused while a lines save is in flight', async () => {
      const { gate, release } = held()
      let started = false
      server.use(
        // Held, then answered by the mock: the server applies it only when it is released.
        http.put('*/api/v1/filament/invoices/:id/lines', async () => {
          started = true
          await gate
        }),
        http.patch('*/api/v1/filament/invoices/:id', () => problem(422, 'the note is refused')),
      )
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      const price = await screen.findByLabelText('Unit price, line 1')
      await user.clear(price)
      await user.type(price, '30')
      await user.tab()
      await waitFor(() => expect(started).toBe(true))
      await user.type(screen.getByLabelText('Note'), 'x')
      await user.tab()
      expect(await screen.findByRole('alert')).toHaveTextContent('the note is refused')
      release()
      const status = await screen.findByText(/The total does not match the lines/)
      expect(status).toBeInTheDocument()
      const now = await getInvoice(INVOICE.DRAFT_A)
      expect(now.reconciles).toBe(false)
      expect(screen.getByText('Difference').nextElementSibling).toHaveTextContent(formatCost(now.difference!, 'USD', 2).replace('-', ''))
    })

    it('shows the server\'s numbers when a lines save fails while a header change is in flight', async () => {
      const { gate, release } = held()
      let started = false
      server.use(
        http.patch('*/api/v1/filament/invoices/:id', async () => {
          started = true
          await gate
        }),
        http.put('*/api/v1/filament/invoices/:id/lines', () => problem(500, 'The database is busy')),
      )
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      const total = await screen.findByLabelText('Total')
      await user.clear(total)
      await user.type(total, '90')
      await user.tab()
      await waitFor(() => expect(started).toBe(true))
      const description = screen.getByLabelText('Description, line 1')
      await user.type(description, ' again')
      await user.tab()
      expect(await screen.findByText('The database is busy')).toBeInTheDocument()
      release()
      expect(await screen.findByText(/The total does not match the lines/)).toBeInTheDocument()
      expect(screen.getByText('Total typed').nextElementSibling).toHaveTextContent('90.00')
    })
  })

  it('sends no product of the old supplier in a lines save right after a move', async () => {
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    let started = false
    server.use(
      http.patch('*/api/v1/filament/invoices/:id', async () => {
        started = true
        await gate
      }),
    )
    const bodies: unknown[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PUT' && /\/lines$/.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body))
    })
    const { user } = render(INVOICE.DRAFT_A)
    await heading()
    await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
    await waitFor(() => expect(started).toBe(true))
    const description = screen.getByLabelText('Description, line 1')
    await user.type(description, ' again')
    await user.tab()
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect((bodies[0] as { supplier_product_id: string | null }[]).map((line) => line.supplier_product_id)).toEqual([null, null])
    release()
  })

  it('keeps the server cost of the lines left when a middle line is removed, until the save answers', async () => {
    const draft = await createInvoice({
      supplier_id: SUPPLIER.BAMBU,
      invoice_date: '2026-11-16',
      currency: 'USD',
      subtotal: '60',
      total: '60',
      lines: [10, 20, 30].map((price) => ({ description: `Line at ${price}`, quantity: 1, unit_price: String(price), type_id: T.JADE })),
    })
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(
      http.put('*/api/v1/filament/invoices/:id/lines', async () => {
        await gate
      }),
    )
    const { user } = render(draft.id)
    await heading()
    const lines = await screen.findByRole('table', { name: 'Invoice lines' })
    const cost = (n: number) => within(lines.querySelector(`tr[data-line="${n}"]`) as HTMLElement).getByText(/\$\d/, { selector: 'td:nth-last-child(2)' })
    await waitFor(() => expect(cost(3)).toHaveTextContent('30.00'))
    expect(cost(1)).toHaveTextContent('10.00')
    await user.click(screen.getByRole('button', { name: 'Remove line 2' }))
    expect(cost(1)).toHaveTextContent('10.00')
    expect(cost(2)).toHaveTextContent('30.00')
    release()
    await waitFor(() => expect(screen.queryByText('Saving the lines…')).not.toBeInTheDocument())
    expect(cost(2)).toHaveTextContent('30.00')
  })

  describe('import with the assistant (#6267)', () => {
    function withAssistant(id: string) {
      const opened: string[] = []
      const view = renderPage(
        <AssistantOpenerContext.Provider value={{ openSession: (session) => opened.push(session) }}>
          <Routes>
            <Route path="/filament/invoices/:id" element={<InvoicePage />} />
          </Routes>
        </AssistantOpenerContext.Provider>,
        { route: `/filament/invoices/${id}` },
      )
      return { ...view, opened }
    }

    it('starts the import, opens its session, and says "Import running" after a reload of the page', async () => {
      const first = withAssistant(INVOICE.DRAFT_A)
      await heading()
      await first.user.click(await screen.findByRole('button', { name: 'Import with the assistant' }))
      await waitFor(() => expect(first.opened).toHaveLength(1))
      first.unmount()

      // A second visit: the invoice names the session, which still runs.
      const second = withAssistant(INVOICE.DRAFT_A)
      await heading()
      expect(await screen.findByText('Import running')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
      await second.user.click(screen.getByRole('button', { name: 'Open' }))
      expect(second.opened).toEqual([first.opened[0]])
    })

    it('offers no import on a confirmed invoice', async () => {
      withAssistant(INVOICE.CONFIRMED_A)
      await heading()
      expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
    })
  })

  describe('final review rulings', () => {
    const problem = (status: number, detail: string) => HttpResponse.json({ type: 'about:blank', title: 'Refused', status, detail }, { status, headers: { 'Content-Type': 'application/problem+json' } })
    const held = () => {
      let release!: () => void
      const gate = new Promise<void>((resolve) => {
        release = resolve
      })
      return { gate, release }
    }
    const pdf = () => new File(['%PDF-1.4 x'], 'again.pdf', { type: 'application/pdf' })
    // jsdom's FormData does not go through Node's fetch, so the mock's multipart parser cannot read it: answer the upload here.
    const uploadAnswered = () => server.use(http.post('*/api/v1/filament/invoices/:id/file', () => HttpResponse.json({ name: 'again.pdf', media_type: 'application/pdf', size: 10 })))

    it('keeps a product picked while a supplier move is refused, and restores the other lines', async () => {
      const { gate, release } = held()
      let started = false
      server.use(
        http.patch('*/api/v1/filament/invoices/:id', async () => {
          started = true
          await gate
          return problem(409, 'The supplier has that number')
        }),
      )
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
      await waitFor(() => expect(started).toBe(true))
      await choose(user, screen.getByRole('combobox', { name: 'Supplier product, line 1' }), 'A00-R3')
      release()
      await screen.findByRole('alert')
      await waitFor(() => expect(screen.getByRole('combobox', { name: 'Supplier product, line 2' })).toHaveTextContent('A00-W1'))
      expect(screen.getByRole('combobox', { name: 'Supplier product, line 1' })).toHaveTextContent('A00-R3')
    })

    it('reads the invoice again only after a save in flight, when a file is uploaded meanwhile', async () => {
      const { gate, release } = held()
      let putStarted = false
      server.use(
        http.put('*/api/v1/filament/invoices/:id/lines', async () => {
          putStarted = true
          await gate
        }),
      )
      uploadAnswered()
      const seen = requests()
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      await user.type(await screen.findByLabelText('Description, line 1'), ' again')
      await user.tab()
      await waitFor(() => expect(putStarted).toBe(true))
      const getsBefore = seen.filter((one) => one === `GET /api/v1/filament/invoices/${INVOICE.DRAFT_A}`).length
      await user.upload(screen.getByLabelText('Choose an invoice file'), pdf())
      await waitFor(() => expect(seen.some((one) => one === `POST /api/v1/filament/invoices/${INVOICE.DRAFT_A}/file`)).toBe(true))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(seen.filter((one) => one === `GET /api/v1/filament/invoices/${INVOICE.DRAFT_A}`)).toHaveLength(getsBefore)
      release()
      await waitFor(() => expect(seen.filter((one) => one === `GET /api/v1/filament/invoices/${INVOICE.DRAFT_A}`).length).toBe(getsBefore + 1))
    })

    it('keeps each row\'s server cost, by line, when the invoice is read again beside an unsent new row', async () => {
      uploadAnswered()
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      const lines = await screen.findByRole('table', { name: 'Invoice lines' })
      const cost = (n: number) => within(lines.querySelector(`tr[data-line="${n}"]`) as HTMLElement).getByText(/\$\d/, { selector: 'td:nth-last-child(2)' })
      await waitFor(() => expect(cost(1)).toBeInTheDocument())
      await user.click(screen.getByRole('button', { name: 'Add line' }))
      await user.upload(screen.getByLabelText('Choose an invoice file'), pdf())
      await waitFor(() => expect(screen.getByRole('button', { name: 'Replace file' })).toBeEnabled())
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(cost(1)).toBeInTheDocument()
      expect(cost(2)).toBeInTheDocument()
    })

    it('keeps Confirm disabled while a header change is in flight', async () => {
      const { gate, release } = held()
      let started = false
      server.use(
        http.patch('*/api/v1/filament/invoices/:id', async () => {
          started = true
          await gate
        }),
      )
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      const note = await screen.findByLabelText('Note')
      await user.type(note, 'Paid')
      await user.tab()
      await waitFor(() => expect(started).toBe(true))
      expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
      expect(screen.getByText('Saving the header…')).toBeInTheDocument()
      release()
      await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled())
    })

    it('puts focus on the heading after a confirm, where the Confirm button was', async () => {
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled())
      await user.click(screen.getByRole('button', { name: 'Confirm' }))
      await user.click(await screen.findByRole('button', { name: 'Make the spools' }))
      await waitFor(() => expect(screen.getByText('Confirmed')).toBeInTheDocument())
      await waitFor(() => expect(screen.getByRole('heading', { level: 1 })).toHaveFocus())
    })

    it('dims the server figures of a line while its edit is unsaved', async () => {
      const { gate, release } = held()
      server.use(
        http.put('*/api/v1/filament/invoices/:id/lines', async () => {
          await gate
        }),
      )
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      const lines = await screen.findByRole('table', { name: 'Invoice lines' })
      const costCell = () => lines.querySelector('tr[data-line="1"] td:nth-last-child(2)') as HTMLElement
      await waitFor(() => expect(costCell()).toHaveTextContent('$'))
      expect(costCell()).not.toHaveClass('opacity-50')
      const price = screen.getByLabelText('Unit price, line 1')
      await user.clear(price)
      await user.type(price, '21')
      await user.tab()
      expect(costCell()).toHaveClass('opacity-50')
      release()
      await waitFor(() => expect(costCell()).not.toHaveClass('opacity-50'))
    })

    it('puts focus on the new line\'s description after Add line', async () => {
      const { user } = render(INVOICE.DRAFT_A)
      await heading()
      await user.click(await screen.findByRole('button', { name: 'Add line' }))
      await waitFor(() => expect(screen.getByLabelText('Description, line 3')).toHaveFocus())
    })
  })
})
