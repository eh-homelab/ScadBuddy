import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

beforeEach(() => {
  for (const id of ['sess-1', 'sess-2', 'sess-3', 'sess-other']) setSessionResources(id, [], { status: 'running', running: true })
})

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

  it('says the assistant waits for the person when the session is idle, with Open and no pulse', async () => {
    setSessionResources('sess-idle', [], { status: 'idle' })
    const { user, opener, container } = render({ ...draft, import_run_id: 'sess-idle' })
    expect(await screen.findByText('The assistant is waiting for you')).toBeInTheDocument()
    expect(screen.queryByText('Import running')).not.toBeInTheDocument()
    expect(container.querySelector('.animate-pulse')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Open' }))
    expect(opener!.openSession).toHaveBeenCalledWith('sess-idle')
  })

  it('reads the session again on window focus, so a session that ends brings the button back', async () => {
    setSessionResources('sess-end', [], { status: 'running', running: true })
    render({ ...draft, import_run_id: 'sess-end' })
    expect(await screen.findByText('Import running')).toBeInTheDocument()
    setSessionResources('sess-end', [], { status: 'done' })
    window.dispatchEvent(new Event('focus'))
    expect(await button()).toBeInTheDocument()
    expect(screen.queryByText('Import running')).not.toBeInTheDocument()
  })

  it('reads the session again on an interval, and stops when unmounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      let reads = 0
      server.use(
        http.get('/api/v1/ai/sessions/sess-poll', () => {
          reads += 1
          return HttpResponse.json({ id: 'sess-poll', title: 't', status: reads > 1 ? 'idle' : 'running', running: reads === 1, archived: false })
        }),
      )
      const { unmount } = render({ ...draft, import_run_id: 'sess-poll' })
      expect(await screen.findByText('Import running')).toBeInTheDocument()
      await vi.advanceTimersByTimeAsync(15_000)
      expect(await screen.findByText('The assistant is waiting for you')).toBeInTheDocument()
      unmount()
      const seen = reads
      await vi.advanceTimersByTimeAsync(60_000)
      expect(reads).toBe(seen)
    } finally {
      vi.useRealTimers()
    }
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

  it('says the plain sentence for an unreadable file', async () => {
    const detail = 'This file is not a readable PDF or photo. Upload it again or use another file.'
    answers(422, { detail, type: 'unreadable_file' })
    const { user } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
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
    answers(422, { detail: 'This file is a HEIC photo. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.' })
    const { user, opener } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent('This file is a HEIC photo. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.')
    expect(opener!.openSession).not.toHaveBeenCalled()
  })

  it('says a too-large file is too large, with what to do', async () => {
    const detail = 'This file is too large for the assistant to read. It can read at most 3.9 MB for a photo and 6.2 MB for a PDF. Upload a smaller photo or a PDF.'
    answers(422, { detail })
    const { user } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
  })

  it('says the agent\'s own reason when the session start is rate limited, with Retry', async () => {
    const detail = 'You have started many sessions in a short time. Wait a minute, then import again.'
    answers(429, { detail, type: 'session_refused' })
    const { user } = render(draft)
    await user.click(await button())
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('does not read the session while the assistant is off', async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/ai/sessions/sess-off', () => {
        reads += 1
        return HttpResponse.json({ id: 'sess-off', title: 't', status: 'running', running: true, archived: false })
      }),
    )
    const { container } = render({ ...draft, import_run_id: 'sess-off' }, null)
    await new Promise((resolve) => setTimeout(resolve, 100))
    expect(container).toBeEmptyDOMElement()
    expect(reads).toBe(0)
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

  it('says the invoice is gone on a 404, with no Retry', async () => {
    answers(404, { detail: 'the backend refused to read the invoice', type: 'backend_refused' })
    const { user } = render(draft)
    await user.click(await button())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('This invoice no longer exists.')
    expect(alert).not.toHaveTextContent('backend')
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  it('does not show a forwarded backend 422 detail', async () => {
    answers(422, { detail: 'the backend refused to read the invoice file', type: 'backend_refused' })
    const { user } = render(draft)
    await user.click(await button())
    const alert = await screen.findByRole('alert')
    expect(alert).not.toHaveTextContent('backend')
    expect(alert).toHaveTextContent('cannot be imported')
  })

  it('says the import could not start on a 502 that names a session, and offers Retry, not the archived session', async () => {
    answers(502, {
      detail: 'The import session 3f2b6c1e-0000-4000-8000-000000000000 started, but ScadBuddy could not link it to the invoice. Try again.',
      type: 'upstream_error',
      session_id: 'sess-named',
    })
    const { user, opener } = render(draft)
    await user.click(await button())
    const alert = await screen.findByRole('alert')
    expect(alert).not.toHaveTextContent(/[0-9a-f]{8}-/)
    expect(alert).toHaveTextContent('The import could not start. Try again.')
    expect(screen.queryByRole('button', { name: 'Open the session' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(opener!.openSession).not.toHaveBeenCalled()
  })

  it('keeps the import shown when a status read fails, and reads again on the next focus', async () => {
    let reads = 0
    server.use(
      http.get('/api/v1/ai/sessions/sess-flaky', () => {
        reads += 1
        if (reads === 2) return HttpResponse.json({ detail: 'boom' }, { status: 500 })
        if (reads === 3) return HttpResponse.error()
        return HttpResponse.json({ id: 'sess-flaky', title: 't', status: reads > 3 ? 'idle' : 'running', running: reads === 1, archived: false })
      }),
    )
    render({ ...draft, import_run_id: 'sess-flaky' })
    expect(await screen.findByText('Import running')).toBeInTheDocument()
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(reads).toBe(2))
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(reads).toBe(3))
    expect(screen.getByText('Import running')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Import with the assistant' })).not.toBeInTheDocument()
    window.dispatchEvent(new Event('focus'))
    expect(await screen.findByText('The assistant is waiting for you')).toBeInTheDocument()
  })

  it('drops an older answer that arrives after a newer one', async () => {
    let reads = 0
    let release = () => {}
    server.use(
      http.get('/api/v1/ai/sessions/sess-race', async () => {
        reads += 1
        const mine = reads
        if (mine === 2) await new Promise<void>((resolve) => (release = resolve))
        const idle = mine === 3
        return HttpResponse.json({ id: 'sess-race', title: 't', status: idle ? 'idle' : 'running', running: !idle, archived: false })
      }),
    )
    render({ ...draft, import_run_id: 'sess-race' })
    expect(await screen.findByText('Import running')).toBeInTheDocument()
    window.dispatchEvent(new Event('focus'))
    await waitFor(() => expect(reads).toBe(2))
    window.dispatchEvent(new Event('focus'))
    expect(await screen.findByText('The assistant is waiting for you')).toBeInTheDocument()
    release()
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.getByText('The assistant is waiting for you')).toBeInTheDocument()
    expect(screen.queryByText('Import running')).not.toBeInTheDocument()
  })

  it.each([
    [503, 'The assistant is unavailable'],
    [403, 'You cannot import invoices'],
  ])('says something specific on a %i', async (status, text) => {
    answers(status, { detail: 'raw refusal' })
    const { user } = render(draft)
    await user.click(await button())
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(text)
    expect(alert).not.toHaveTextContent('raw refusal')
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
