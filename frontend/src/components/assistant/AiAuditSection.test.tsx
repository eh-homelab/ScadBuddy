import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuditPage } from '../../agent/audit'
import type * as embed from '../../lib/embed'
import { downloadBlob } from '../../lib/embed'
import { AUDIT_FIXTURES } from '../../mocks/features/audit'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { AiAuditSection } from './AiAuditSection'

const availability = vi.hoisted(() => ({ available: true }))
vi.mock('../../agent/chat/availability', () => ({
  useAiAvailability: () => availability,
}))
// Loads the file the way the real one does; saving it is lib/embed's own test.
vi.mock('../../lib/embed', async (original) => ({
  ...(await original<typeof embed>()),
  downloadBlob: vi.fn(async (load: () => Promise<Blob>) => {
    await load()
  }),
}))

beforeEach(() => {
  availability.available = true
  vi.mocked(downloadBlob).mockClear()
  resetMockState()
})

const rows = () => within(screen.getByRole('list', { name: 'AI activity entries' })).getAllByTestId('audit-entry')

describe('AiAuditSection', () => {
  it('renders nothing when AI is off', () => {
    availability.available = false
    const { container } = renderPage(<AiAuditSection />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows memory rows in words, not their JSON summary', async () => {
    const memory = (id: string, fields: Partial<AuditPage['entries'][number]>) => ({
      ...AUDIT_FIXTURES[3]!,
      id,
      kind: 'memory' as const,
      tier: null,
      duration_ms: 120,
      ...fields,
    })
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json<AuditPage>({
          entries: [
            memory('3', { action: 'retain', outcome: 'ok', input_summary: '{"bank":"scadbuddy","document_id":"conversation:abc"}' }),
            memory('2', { action: 'recall', outcome: 'ok', input_summary: '{"bank":"scadbuddy","results":3}' }),
            memory('1', {
              action: 'recall',
              outcome: 'error',
              input_summary: '{"bank":"scadbuddy"}',
              detail: 'timed out after 3000 ms',
            }),
          ],
          next: null,
          retention_days: 90,
        }),
      ),
    )
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(3))
    const [retain, recall, failed] = rows()
    expect(retain).toHaveTextContent('Memory save')
    expect(retain).toHaveTextContent('bank scadbuddy · conversation:abc')
    expect(recall).toHaveTextContent('Memory recall')
    expect(recall).toHaveTextContent('bank scadbuddy · 3 memories')
    expect(recall).not.toHaveTextContent('{')
    expect(failed).toHaveTextContent('Error')
    expect(failed).toHaveTextContent('bank scadbuddy · timed out after 3000 ms')
    expect(screen.getByRole('option', { name: 'Memory' })).toBeInTheDocument()
  })

  it('shows http rows as method, host, status and size (#827)', async () => {
    const row = (id: string, fields: Partial<AuditPage['entries'][number]>) => ({
      ...AUDIT_FIXTURES[3]!,
      id,
      kind: 'http' as const,
      duration_ms: 40,
      ...fields,
    })
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json<AuditPage>({
          entries: [
            row('2', {
              action: 'GET',
              tier: 'read',
              outcome: 'ok',
              input_summary: '{"method":"GET","scheme":"http","host":"printer.lan:8080","status":200,"size_bytes":512}',
            }),
            row('1', {
              action: 'POST',
              tier: 'outward',
              outcome: 'refused',
              input_summary: '{"method":"POST","host":"example.com"}',
              detail: "the Authorization header contains the agent's own credential",
            }),
          ],
          next: null,
          retention_days: 90,
        }),
      ),
    )
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(2))
    const [get, refused] = rows()
    expect(get).toHaveTextContent('HTTP GET')
    expect(get).toHaveTextContent('http://printer.lan:8080 · HTTP 200 · 512 bytes')
    expect(get).not.toHaveTextContent('{')
    expect(refused).toHaveTextContent('Refused')
    expect(refused).toHaveTextContent("example.com · the Authorization header contains the agent's own credential")
    expect(screen.getByRole('option', { name: 'HTTP requests' })).toBeInTheDocument()
  })

  it('links an http row to the body its request saved, and only that row (#1292)', async () => {
    const SESSION = '5a1c3e2b-7d4f-4e6a-9b8c-0d1e2f3a4b5c'
    const SAVED = '0b7d4c3e-5f6a-4b8c-9d0e-1f2a3b4c5d6e'
    const row = (id: string, fields: Partial<AuditPage['entries'][number]>) => ({
      ...AUDIT_FIXTURES[3]!,
      id,
      kind: 'http' as const,
      action: 'GET',
      tier: 'read' as const,
      outcome: 'ok' as const,
      session_id: SESSION,
      ...fields,
    })
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json<AuditPage>({
          entries: [
            row('3', {
              input_summary: `{"method":"GET","scheme":"https","host":"example.com","status":200,"size_bytes":2000000,"saved":"${SAVED}"}`,
            }),
            // Returned inline: nothing was saved.
            row('2', { input_summary: '{"method":"GET","scheme":"https","host":"example.com","status":200,"size_bytes":12}' }),
            // Not an id the agent makes: never turned into a path.
            row('1', { input_summary: '{"method":"GET","host":"example.com","saved":"../../etc/passwd"}' }),
          ],
          next: null,
          retention_days: 90,
        }),
      ),
    )
    const fetched: string[] = []
    server.use(
      http.get(`/api/v1/ai/sessions/${SESSION}/http/${SAVED}`, ({ request }) => {
        fetched.push(new URL(request.url).pathname)
        return new HttpResponse('{"big":true}', { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
      }),
    )
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(3))
    const [saved, inline, odd] = rows()
    // A button through downloadBlob, never a plain link: Bambuddy's frame drops those.
    expect(within(saved!).queryByRole('link')).not.toBeInTheDocument()
    await userEvent.click(within(saved!).getByRole('button', { name: 'Download body' }))
    await waitFor(() => expect(downloadBlob).toHaveBeenCalledWith(expect.any(Function), 'http-response-0b7d4c3e'))
    expect(fetched).toEqual([`/api/v1/ai/sessions/${SESSION}/http/${SAVED}`])
    // The summary line stays words, without the id.
    expect(saved).toHaveTextContent('https://example.com · HTTP 200 · 2000000 bytes')
    expect(within(inline!).queryByRole('button', { name: 'Download body' })).not.toBeInTheDocument()
    expect(within(odd!).queryByRole('button', { name: 'Download body' })).not.toBeInTheDocument()
  })

  it('says so when a saved body is no longer kept (#1292)', async () => {
    const SESSION = '5a1c3e2b-7d4f-4e6a-9b8c-0d1e2f3a4b5c'
    const SAVED = '0b7d4c3e-5f6a-4b8c-9d0e-1f2a3b4c5d6e'
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json<AuditPage>({
          entries: [
            {
              ...AUDIT_FIXTURES[3]!,
              id: '9',
              kind: 'http',
              action: 'GET',
              tier: 'read',
              outcome: 'ok',
              session_id: SESSION,
              input_summary: `{"method":"GET","host":"example.com","status":200,"saved":"${SAVED}"}`,
            },
          ],
          next: null,
          retention_days: 90,
        }),
      ),
      http.get(`/api/v1/ai/sessions/${SESSION}/http/${SAVED}`, () => new HttpResponse(null, { status: 404 })),
    )
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(1))
    await userEvent.click(screen.getByRole('button', { name: 'Download body' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('no longer kept')
  })

  it("shows each turn's cost, and flags one Claude Code never priced (#1922)", async () => {
    const turn = (id: string, fields: Partial<AuditPage['entries'][number]>) => ({
      ...AUDIT_FIXTURES[3]!,
      id,
      kind: 'turn' as const,
      tier: null,
      input_summary: null,
      duration_ms: null,
      ...fields,
    })
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json<AuditPage>({
          entries: [
            turn('4', { action: 'failed', outcome: 'error', cost_usd: 0, cost_priced: false, cost_estimated_usd: 0, detail: 'exited with code 1' }),
            turn('3', { action: 'interrupted', outcome: 'refused', cost_usd: 0.06006, cost_priced: false, cost_estimated_usd: 0.06006 }),
            turn('2', { action: 'interrupted', outcome: 'refused', cost_usd: 0.1, cost_priced: true, cost_estimated_usd: 0.06 }),
            turn('1', { action: 'success', outcome: 'ok', cost_usd: 0.0123, cost_priced: true, cost_estimated_usd: 0 }),
          ],
          next: null,
          retention_days: 90,
        }),
      ),
    )
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(4))
    const [failed, cut, mixed, priced] = rows()
    expect(priced).toHaveTextContent('Turn success')
    expect(priced).toHaveTextContent('$0.0123')
    expect(priced).not.toHaveTextContent('not priced')
    expect(mixed).toHaveTextContent('$0.1000 (incl. ~$0.0600 estimated)')
    // Never a priced zero: the estimate, flagged.
    expect(cut).toHaveTextContent('not priced by Claude Code · ~$0.0601 estimated')
    expect(failed).toHaveTextContent('not priced by Claude Code')
    expect(failed).not.toHaveTextContent('$0.0000')
    expect(failed).toHaveTextContent('exited with code 1')
    expect(screen.getByRole('option', { name: 'Turns' })).toBeInTheDocument()
  })

  it('lists the log newest first: who, what, tier, outcome and the scrubbed input', async () => {
    renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(AUDIT_FIXTURES.length))
    const [newest, resource, denied] = rows()
    // An outward call that ran: who ran it and who approved it, on one row.
    expect(newest).toHaveTextContent('set_print_options')
    expect(newest).toHaveTextContent('OK')
    expect(newest).toHaveTextContent('approved by You')
    expect(resource).toHaveTextContent('Resource read')
    expect(resource).toHaveTextContent('scadbuddy://models/name-keychain/source')
    expect(denied).toHaveTextContent('delete_model')
    expect(denied).toHaveTextContent('outward')
    expect(denied).toHaveTextContent('Denied')
    expect(denied).toHaveTextContent('You via assistant')
    expect(denied).toHaveTextContent('61250 ms')
    expect(denied).toHaveTextContent('The user denied')
    expect(denied).not.toHaveTextContent('approved by')
    expect(rows().at(-1)).toHaveTextContent('Settings: model')
    expect(screen.getByLabelText('Keep entries for (days)')).toHaveValue(90)
  })

  it('filters by outcome and kind on the server', async () => {
    const seen: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.url.includes('/api/v1/ai/audit')) seen.push(new URL(request.url).search)
    })
    const { user } = renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(AUDIT_FIXTURES.length))

    await user.selectOptions(screen.getByLabelText('Outcome'), 'denied')
    await waitFor(() => expect(rows()).toHaveLength(2))
    await user.selectOptions(screen.getByLabelText('Kind'), 'approval')
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(rows()[0]).toHaveTextContent('Approval denied')
    expect(seen.at(-1)).toBe('?kind=approval&outcome=denied&limit=25')

    await user.selectOptions(screen.getByLabelText('Outcome'), 'error')
    expect(await screen.findByText('Nothing recorded for these filters.')).toBeInTheDocument()
    server.events.removeAllListeners()
  })

  it('loads older pages with the cursor', async () => {
    const [a, b] = AUDIT_FIXTURES
    server.use(
      http.get('/api/v1/ai/audit', ({ request }) => {
        const before = new URL(request.url).searchParams.get('before')
        const body: AuditPage = before
          ? { entries: [a!], next: null, retention_days: 90 }
          : { entries: [b!], next: b!.id, retention_days: 90 }
        return HttpResponse.json(body)
      }),
    )
    const { user } = renderPage(<AiAuditSection />)
    await waitFor(() => expect(rows()).toHaveLength(1))
    await user.click(screen.getByRole('button', { name: 'Load older' }))
    await waitFor(() => expect(rows()).toHaveLength(2))
    expect(rows()[1]).toHaveTextContent('Settings: model')
    expect(screen.queryByRole('button', { name: 'Load older' })).not.toBeInTheDocument()
  })

  it('saves the retention, and refuses an out-of-range one without asking the server', async () => {
    const puts: unknown[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method === 'PUT') puts.push(request.url)
    })
    const { user } = renderPage(<AiAuditSection />)
    const field = await screen.findByLabelText('Keep entries for (days)')
    await waitFor(() => expect(field).toHaveValue(90))

    await user.clear(field)
    await user.type(field, '0')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('1 to 3650 days')
    expect(puts).toEqual([])

    await user.clear(field)
    await user.type(field, '30')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Entries are kept for 30 days.')
    expect(puts).toHaveLength(1)
    server.events.removeAllListeners()
  })

  it('says why when the log cannot be read', async () => {
    server.use(
      http.get('/api/v1/ai/audit', () =>
        HttpResponse.json({ detail: 'the AI database is unreachable' }, { status: 503 }),
      ),
    )
    renderPage(<AiAuditSection />)
    expect(await screen.findByRole('alert')).toHaveTextContent('the AI database is unreachable')
  })
})
