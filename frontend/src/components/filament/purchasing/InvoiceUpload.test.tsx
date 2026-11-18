import { createEvent, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SUPPLIER } from '../../../mocks/features/filamentPurchasingSeed'
import { server } from '../../../mocks/server'
import { InvoiceUpload } from './InvoiceUpload'
import { lastSupplier } from './invoicesQuery'

const photo = () => new File([new Uint8Array([137, 80, 78, 71])], 'receipt.png', { type: 'image/png' })

/** Every write the page makes, in order: `fetch` and msw both report here. */
function watch() {
  const seen: string[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method !== 'GET') seen.push(`${request.method} ${new URL(request.url).pathname}`)
  })
  return seen
}

/**
 * jsdom's `FormData` does not go through Node's fetch (`mocks/features/filamentPurchasing.test.ts`),
 * so the file part of an upload is answered here and every other call goes on to msw.
 */
function answerFileUpload(seen: string[], answer: () => Response) {
  const real = globalThis.fetch
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input)
    if (init?.method === 'POST' && init.body instanceof FormData && /\/file$/.test(url)) {
      seen.push(`POST ${new URL(url, window.location.origin).pathname}`)
      return Promise.resolve(answer())
    }
    return real(input, init)
  })
}

const stored = () => new Response(JSON.stringify({ name: 'receipt.png', media_type: 'image/png', size: 4 }), { status: 200 })
const refused = () =>
  new Response(JSON.stringify({ type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'the file is not a PDF or a photo' }), {
    status: 422,
    headers: { 'Content-Type': 'application/problem+json' },
  })

function show(onCreated = vi.fn()) {
  const user = userEvent.setup()
  render(
    <MemoryRouter>
      <InvoiceUpload onCreated={onCreated} />
    </MemoryRouter>,
  )
  return { user, onCreated }
}

function paste(files: File[]) {
  const zone = screen.getByLabelText('Add an invoice file')
  const event = createEvent.paste(zone)
  Object.defineProperty(event, 'clipboardData', { value: { files } })
  fireEvent(zone, event)
}

/** The supplier is the remembered one and the shop's currency is read. */
const ready = async () => {
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'Supplier' })).toHaveTextContent('Bambu Lab'))
  await waitFor(() => expect(screen.getByLabelText('Add an invoice file')).not.toHaveAttribute('aria-disabled', 'true'))
}

beforeEach(() => {
  window.localStorage.clear()
  window.localStorage.setItem('scadbuddy.filament.invoices.lastSupplier', SUPPLIER.BAMBU)
})
afterEach(() => vi.unstubAllGlobals())

describe('the invoice upload', () => {
  it('creates a draft and uploads the pasted photo once, in that order', async () => {
    const seen = watch()
    answerFileUpload(seen, stored)
    const { onCreated } = show()
    await ready()
    paste([photo()])
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
    expect(seen).toEqual(['POST /api/v1/filament/invoices', expect.stringMatching(/^POST \/api\/v1\/filament\/invoices\/[^/]+\/file$/)])
    expect(onCreated).toHaveBeenCalledWith(expect.any(String))
    expect(lastSupplier()).toBe(SUPPLIER.BAMBU)
  })

  it('ignores a second paste while the first is being sent', async () => {
    const seen = watch()
    answerFileUpload(seen, stored)
    const { onCreated } = show()
    await ready()
    paste([photo()])
    paste([photo()])
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
    expect(seen.filter((one) => one === 'POST /api/v1/filament/invoices')).toHaveLength(1)
  })

  it('leaves the draft and says so when the file is refused', async () => {
    const seen = watch()
    answerFileUpload(seen, refused)
    const { onCreated } = show()
    await ready()
    paste([photo()])
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The draft was saved; the file was not')
    expect(alert).toHaveTextContent('the file is not a PDF or a photo')
    expect(screen.getByRole('link', { name: 'Open the draft' })).toHaveAttribute('href', expect.stringMatching(/^\/filament\/invoices\/.+/))
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('refuses a file that is neither a PDF nor an image, before it sends anything', async () => {
    const seen = watch()
    show()
    await ready()
    paste([new File(['x'], 'notes.txt', { type: 'text/plain' })])
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a PDF or a photo.')
    expect(seen).toEqual([])
  })

  it('takes a file from the file button too', async () => {
    const seen = watch()
    answerFileUpload(seen, stored)
    const { user, onCreated } = show()
    await ready()
    await user.upload(screen.getByLabelText('Choose a file'), new File(['%PDF-1.4'], 'bill.pdf', { type: 'application/pdf' }))
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
  })

  it('asks for a supplier when none is remembered', async () => {
    window.localStorage.clear()
    const seen = watch()
    show()
    await waitFor(() => expect(screen.getByLabelText('Add an invoice file')).toBeInTheDocument())
    paste([photo()])
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose the supplier first.')
    expect(seen).toEqual([])
  })
})
