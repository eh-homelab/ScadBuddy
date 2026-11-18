import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { AssistantOpenerContext } from '../../../agent/chat/opener'
import { setSessionResources } from '../../../mocks/features/assistantSessions'
import { server } from '../../../mocks/server'
import { renderPage } from '../../../test/utils'
import { InvoiceImport } from './InvoiceImport'

const ROUTE = '/api/v1/ai/filament/invoice-import'
const FILE = { name: 'acme.pdf', media_type: 'application/pdf', size: 10 }
const draft = { id: '11111111-1111-4111-8111-111111111111', status: 'draft' as const, file: FILE, import_run_id: null as string | null }

function render(invoice: Parameters<typeof InvoiceImport>[0]['invoice'], opener: { openSession: (id: string) => void } | null = { openSession: vi.fn() }) {
  const onChanged = vi.fn()
  const view = renderPage(
    <AssistantOpenerContext.Provider value={opener}>
      <InvoiceImport invoice={invoice} onChanged={onChanged} />
    </AssistantOpenerContext.Provider>,
  )
  return { ...view, onChanged, opener }
}

function answers(status: number, body: Record<string, unknown>) {
  const bodies: unknown[] = []
  server.use(
    http.post(ROUTE, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json(body, { status })
    }),
  )
  return bodies
}

const button = () => screen.findByRole('button', { name: 'Import with the assistant' })

describe('InvoiceImport', () => {
  it('starts the import once and opens its session in the assistant', async () => {
    const bodies = answers(200, { session_id: 'sess-1' })
    const { user, opener, onChanged } = render(draft)
    const press = await button()
    await user.dblClick(press)
    await waitFor(() => expect(opener!.openSession).toHaveBeenCalledWith('sess-1'))
    expect(bodies).toEqual([{ invoice_id: draft.id }])
    expect(opener!.openSession).toHaveBeenCalledTimes(1)
    expect(onChanged).toHaveBeenCalled()
    expect(await screen.findByText('Import running')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
  })

  it('disables the button while the request runs', async () => {
    let release = () => {}
    server.use(
      http.post(ROUTE, async () => {
        await new Promise<void>((resolve) => (release = resolve))
        return HttpResponse.json({ session_id: 'sess-2' })
      }),
    )
    const { user } = render(draft)
    await user.click(await button())
    expect(screen.getByRole('button', { name: /Starting the import/ })).toBeDisabled()
    release()
    expect(await screen.findByText('Import running')).toBeInTheDocument()
  })

  it('shows "Import running" with Open while the invoice names a live session', async () => {
    setSessionResources('sess-live', [], { status: 'running', running: true })
    const { user, opener } = render({ ...draft, import_run_id: 'sess-live' })
    expect(await screen.findByText('Import running')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Open' }))
    expect(opener!.openSession).toHaveBeenCalledWith('sess-live')
  })

  it('offers the import again when the linked session is done', async () => {
    setSessionResources('sess-done', [], { status: 'done' })
    render({ ...draft, import_run_id: 'sess-done' })
    expect(await button()).toBeInTheDocument()
    expect(screen.queryByText('Import running')).not.toBeInTheDocument()
  })

  it('offers the import again when the linked session is not found', async () => {
    render({ ...draft, import_run_id: 'sess-never' })
    expect(await button()).toBeInTheDocument()
  })

  it('opens the session a 409 import_running names', async () => {
    answers(409, { detail: 'This invoice is already being imported. Open its session.', type: 'import_running', session_id: 'sess-other' })
    const { user, opener } = render(draft)
    await user.click(await button())
    await waitFor(() => expect(opener!.openSession).toHaveBeenCalledWith('sess-other'))
    expect(await screen.findByText('Import running')).toBeInTheDocument()
  })

  it('hides the button on a 409 confirmed', async () => {
    answers(409, { detail: 'This invoice is confirmed. There is nothing left to import.', type: 'confirmed' })
    const { user, onChanged } = render(draft)
    await user.click(await button())
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('says the sentence a 422 carries', async () => {
    answers(422, { detail: 'This file is a image/heic. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.' })
    const { user, opener } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent('This file is a image/heic. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.')
    expect(opener!.openSession).not.toHaveBeenCalled()
  })

  it('shows a plain error with Retry on a 502, and Retry tries again', async () => {
    answers(502, { detail: 'The import could not start. Try again.', type: 'upstream_error' })
    const { user, opener } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent('The import could not start. Try again.')
    answers(200, { session_id: 'sess-3' })
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(opener!.openSession).toHaveBeenCalledWith('sess-3'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows nothing for a confirmed invoice, an invoice with no file, or without an assistant', () => {
    const confirmed = render({ ...draft, status: 'confirmed' })
    expect(confirmed.container).toBeEmptyDOMElement()
    confirmed.unmount()
    const none = render({ ...draft, file: null })
    expect(none.container).toBeEmptyDOMElement()
    none.unmount()
    const bare = render(draft, null)
    expect(bare.container).toBeEmptyDOMElement()
  })
})
