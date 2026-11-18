import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { registerInvoiceImportRoutes } from '../src/routes/invoiceImport.js'
import { IMAGE_BYTES_MAX, PDF_BYTES_MAX } from '../src/sessions/images.js'
import { SessionError } from '../src/sessions/manager.js'
import { BACKEND, LOOPBACK, ORIGINS } from './helpers/mcp.js'

// #6267 Task 3: POST /api/v1/ai/filament/invoice-import starts the session that imports an
// invoice, once, and links it to the invoice (`import_run_id`).

const INVOICE = '0e5a3c1e-1111-4222-8333-944455556666'
const PDF = Buffer.from('%PDF-1.7\n1 0 obj\n<<>>\nendobj\n')
const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0])

type Invoice = {
  status: 'draft' | 'confirmed'
  number: string | null
  import_run_id: string | null
  file: { name: string; media_type: string; size: number } | null
}

/** The backend as the route reads and writes it: the invoice, its file, and the conditional PATCH. */
function fakeBackend(over: Partial<Invoice> = {}, file: Buffer = PDF) {
  const invoice: Invoice = {
    status: 'draft',
    number: 'INV-77',
    import_run_id: null,
    file: { name: 'invoice.pdf', media_type: 'application/pdf', size: file.length },
    ...over,
  }
  const patches: Record<string, unknown>[] = []
  const authors: (string | null)[] = []
  /** Set to make the next PATCH answer this status, whatever the invoice holds. */
  const patchRefusal: { status: number | null } = { status: null }
  /** Runs before the PATCH is applied: a rival claim that lands between the route's read and write. */
  const beforePatch: { run: (() => void) | null } = { run: null }
  const backend = createBackendClient(BACKEND, async (input) => {
    const request = input as Request
    const path = new URL(request.url).pathname
    if (path === `/api/v1/filament/invoices/${INVOICE}` && request.method === 'GET') {
      return Response.json({ id: INVOICE, ...invoice, lines: [] })
    }
    if (path === `/api/v1/filament/invoices/${INVOICE}/file` && request.method === 'GET') {
      return new Response(new Uint8Array(file), { headers: { 'content-type': invoice.file?.media_type ?? 'application/pdf' } })
    }
    if (path === `/api/v1/filament/invoices/${INVOICE}` && request.method === 'PATCH') {
      const body = (await request.json()) as { import_run_id?: string | null; if_import_run_id?: string | null }
      patches.push(body)
      authors.push(request.headers.get('x-scadbuddy-agent-author'))
      beforePatch.run?.()
      if (patchRefusal.status) return Response.json({ detail: 'refused' }, { status: patchRefusal.status })
      if (body.if_import_run_id !== undefined && body.if_import_run_id !== invoice.import_run_id) {
        return Response.json({ detail: 'the invoice’s import run changed since it was read' }, { status: 409 })
      }
      invoice.import_run_id = body.import_run_id ?? null
      return Response.json({ id: INVOICE, ...invoice, lines: [] })
    }
    return Response.json({ detail: `unexpected ${request.method} ${path}` }, { status: 500 })
  })
  return { backend, invoice, patches, authors, patchRefusal, beforePatch }
}

type Started = { title?: string; origin: string; prompt?: string }
type Sent = { id: string; prompt: string; images?: readonly { mediaType: string; bytes: Buffer }[] }

/** The session manager's three calls the route makes, over a map of sessions. */
function fakeSessions() {
  const rows = new Map<string, { status: string; archivedAt: string | null }>()
  const started: Started[] = []
  const sent: Sent[] = []
  const archived: string[] = []
  /** Set to make `get` fail with this error for every id. */
  const getFails: { error: Error | null } = { error: null }
  let next = 0
  const sessions = {
    async get(id: string) {
      if (getFails.error) throw getFails.error
      const row = rows.get(id)
      if (!row) throw new SessionError('not_found', `no session ${id}`)
      return { id, ...row }
    },
    async start(_owner: unknown, options: Started) {
      started.push(options)
      const id = `s-${++next}`
      rows.set(id, { status: 'idle', archivedAt: null })
      await Promise.resolve()
      return { session: { id } }
    },
    async send(id: string, _owner: unknown, prompt: string, options: { images?: Sent['images'] } = {}) {
      sent.push({ id, prompt, ...(options.images ? { images: options.images } : {}) })
      return { turnId: `t-${sent.length}` }
    },
    edits: {
      async update(id: string, _owner: unknown, edit: { archived?: boolean }) {
        if (edit.archived) archived.push(id)
        return { id }
      },
    },
  }
  return { sessions, rows, started, sent, archived, getFails }
}

function app(backend: ReturnType<typeof fakeBackend>['backend'], fake = fakeSessions(), ready = true) {
  const hono = new Hono()
  registerInvoiceImportRoutes(hono, {
    backend,
    sessions: fake.sessions as never,
    ready: async () => ready,
    origins: ORIGINS,
    remoteAddress: () => LOOPBACK,
  })
  return { hono, ...fake }
}

const post = (hono: Hono, body: unknown = { invoice_id: INVOICE }, headers: Record<string, string> = {}) =>
  hono.request('http://127.0.0.1/api/v1/ai/filament/invoice-import', {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: 'http://127.0.0.1', host: '127.0.0.1', ...headers },
    body: JSON.stringify(body),
  })

describe('starting an invoice import (#6267)', () => {
  it('starts a session whose first message names the invoice and the skill, attaches the file, and links it', async () => {
    const b = fakeBackend()
    const { hono, started, sent } = app(b.backend)
    const res = await post(hono)
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ session_id: 's-1' })
    expect(started).toHaveLength(1)
    expect(started[0]!.prompt).toBeUndefined()
    expect(started[0]!.title).toBe('Import invoice INV-77')
    expect(started[0]!.origin).toBe('chat')
    expect(sent).toHaveLength(1)
    expect(sent[0]!.id).toBe('s-1')
    const first = sent[0]!
    expect(first.prompt).toContain(INVOICE)
    expect(first.prompt).toContain('`filament` skill')
    expect(first.prompt).toContain('filament_invoice_candidates')
    expect(first.prompt).toContain('filament_bambu_colour')
    expect(first.prompt).toContain('(line subtotal + line tax + the line\'s share of shipping) ÷ quantity')
    expect(first.prompt).toMatch(/never confirm/i)
    expect(first.images).toHaveLength(1)
    expect(first.images![0]!.mediaType).toBe('application/pdf')
    expect(first.images![0]!.bytes.equals(PDF)).toBe(true)
    expect(b.patches).toEqual([{ import_run_id: 's-1', if_import_run_id: null }])
    expect(b.invoice.import_run_id).toBe('s-1')
  })

  it('titles the session by the file name when the invoice has no number, and attaches an image as an image', async () => {
    const b = fakeBackend({ number: null, file: { name: 'scan.png', media_type: 'image/png', size: PNG.length } }, PNG)
    const { hono, started, sent } = app(b.backend)
    expect((await post(hono)).status).toBe(200)
    expect(started[0]!.title).toBe('Import invoice scan.png')
    expect(sent[0]!.images![0]!.mediaType).toBe('image/png')
  })

  it('answers 409 for a confirmed invoice and starts nothing', async () => {
    const b = fakeBackend({ status: 'confirmed' })
    const { hono, started } = app(b.backend)
    const res = await post(hono)
    expect(res.status).toBe(409)
    expect(started).toEqual([])
    expect(b.patches).toEqual([])
  })

  it('answers 422 for an invoice with no file', async () => {
    const b = fakeBackend({ file: null })
    const { hono, started } = app(b.backend)
    const res = await post(hono)
    expect(res.status).toBe(422)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/no file/i)
    expect(started).toEqual([])
  })

  it('refuses a HEIC or a text file with a sentence the person understands (Review Focus 5)', async () => {
    for (const type of ['image/heic', 'text/plain']) {
      const b = fakeBackend({ file: { name: 'f', media_type: type, size: 3 } }, Buffer.from('abc'))
      const { hono, started } = app(b.backend)
      const res = await post(hono)
      expect(res.status).toBe(422)
      expect(((await res.json()) as { detail: string }).detail).toBe(
        `This file is ${type === 'image/heic' ? 'a HEIC photo' : 'a text file'}. ScadBuddy reads invoices as PDF, PNG, JPEG, GIF or WebP.`,
      )
      expect(started).toEqual([])
    }
  })

  it('refuses a photo or a PDF over its cap with a sentence that says what to do, before it reads the file', async () => {
    const cases = [
      { media_type: 'image/png', size: IMAGE_BYTES_MAX + 1, text: /photo/ },
      { media_type: 'application/pdf', size: PDF_BYTES_MAX + 1, text: /PDF/ },
    ]
    for (const { media_type, size, text } of cases) {
      const b = fakeBackend({ file: { name: 'big', media_type, size } })
      const { hono, started } = app(b.backend)
      const res = await post(hono)
      expect(res.status).toBe(422)
      const detail = ((await res.json()) as { detail: string }).detail
      expect(detail).toMatch(/too large for the assistant to read/)
      expect(detail).toMatch(/at most 3\.9 MB for a photo and 6\.2 MB for a PDF/)
      expect(detail).toMatch(/Upload a smaller photo or a PDF/)
      expect(detail).toMatch(text)
      expect(detail).not.toMatch(/bytes/)
      expect(started).toEqual([])
    }
  })

  it('names the invoice file in the PDF attachment, so the chip can show it', async () => {
    const b = fakeBackend({ file: { name: 'Bambu order 4411.pdf', media_type: 'application/pdf', size: PDF.length } })
    const { hono, sent } = app(b.backend)
    expect((await post(hono)).status).toBe(200)
    expect(sent[0]!.images).toMatchObject([{ mediaType: 'application/pdf', fileName: 'Bambu order 4411.pdf' }])
  })

  it('tells the person to wait when the session start is rate limited, and to try later when it is not ready', async () => {
    const cases: [SessionError, number, RegExp][] = [
      [new SessionError('rate_limited', 'too many'), 429, /Wait a minute/],
      [new SessionError('unavailable', 'no workflow'), 503, /not ready/],
    ]
    for (const [error, status, text] of cases) {
      const b = fakeBackend()
      const fake = fakeSessions()
      fake.sessions.start = async () => {
        throw error
      }
      const { hono, sent } = app(b.backend, fake)
      const res = await post(hono)
      expect(res.status).toBe(status)
      const body = (await res.json()) as { detail: string; type: string }
      expect(body.detail).toMatch(text)
      expect(body.type).toBe('session_refused')
      expect(sent).toEqual([])
    }
  })

  it('refuses a file whose bytes are not the type it names', async () => {
    const b = fakeBackend({}, Buffer.from('not a pdf'))
    const { hono, started } = app(b.backend)
    const res = await post(hono)
    expect(res.status).toBe(422)
    expect(await res.json()).toEqual({
      detail: 'This file is not a readable PDF or photo. Upload it again or use another file.',
      type: 'unreadable_file',
    })
    expect(started).toEqual([])
  })

  it('does not name a rival session that is gone when it wins the race: answers 502 and no session_id', async () => {
    const b = fakeBackend()
    const fake = fakeSessions()
    fake.rows.set('s-rival', { status: 'done', archivedAt: null })
    b.beforePatch.run = () => {
      b.invoice.import_run_id = 's-rival'
    }
    const { hono, sent } = app(b.backend, fake)
    const res = await post(hono)
    expect(res.status).toBe(502)
    const body = await res.json()
    expect(body).toMatchObject({ type: 'upstream_error', detail: 'The import could not start. Try again.' })
    expect(body).not.toHaveProperty('session_id')
    expect(sent).toEqual([])
  })

  it('answers 409 with the live session’s id, so the page opens it (Review Focus 2)', async () => {
    const b = fakeBackend({ import_run_id: 's-live' })
    const fake = fakeSessions()
    fake.rows.set('s-live', { status: 'idle', archivedAt: null })
    const { hono, started } = app(b.backend, fake)
    const res = await post(hono)
    expect(res.status).toBe(409)
    expect(await res.json()).toMatchObject({ session_id: 's-live' })
    expect(started).toEqual([])
    expect(b.patches).toEqual([])
  })

  it('replaces a session that is done, failed, archived or gone, naming the id it read', async () => {
    const dead: [string, { status: string; archivedAt: string | null } | undefined][] = [
      ['s-done', { status: 'done', archivedAt: null }],
      ['s-failed', { status: 'failed', archivedAt: null }],
      ['s-archived', { status: 'idle', archivedAt: '2026-11-18T00:00:00Z' }],
      ['s-gone', undefined],
    ]
    for (const [id, row] of dead) {
      const b = fakeBackend({ import_run_id: id })
      const fake = fakeSessions()
      if (row) fake.rows.set(id, row)
      const { hono, started } = app(b.backend, fake)
      const res = await post(hono)
      expect(res.status, id).toBe(200)
      expect(started, id).toHaveLength(1)
      expect(b.patches, id).toEqual([{ import_run_id: 's-1', if_import_run_id: id }])
    }
  })

  it('starts one session for two near-simultaneous calls', async () => {
    const b = fakeBackend()
    const { hono, started } = app(b.backend)
    const [one, two] = await Promise.all([post(hono), post(hono)])
    expect(started).toHaveLength(1)
    const statuses = [one.status, two.status].sort()
    expect(statuses).toEqual([200, 409])
    const loser = one.status === 409 ? one : two
    expect(await loser.json()).toMatchObject({ session_id: 's-1' })
    expect(b.invoice.import_run_id).toBe('s-1')
  })

  it('loses a race with another replica cleanly: the conditional write is refused, no turn runs, its session is archived, and the winner’s id is answered', async () => {
    const b = fakeBackend()
    const fake = fakeSessions()
    fake.rows.set('s-rival', { status: 'running', archivedAt: null })
    // The rival claims the invoice after this call read it as unclaimed and before its write.
    b.beforePatch.run = () => {
      b.invoice.import_run_id = 's-rival'
    }
    const { hono, started, sent, archived } = app(b.backend, fake)
    const res = await post(hono)
    expect(res.status).toBe(409)
    expect(await res.json()).toMatchObject({ session_id: 's-rival' })
    expect(started).toHaveLength(1)
    expect(sent).toEqual([])
    expect(archived).toEqual(['s-1'])
    expect(b.invoice.import_run_id).toBe('s-rival')
  })

  it('answers 502 naming the session when the link cannot be written, runs no turn and archives it', async () => {
    const b = fakeBackend()
    b.patchRefusal.status = 500
    const { hono, sent, archived } = app(b.backend)
    const res = await post(hono)
    expect(res.status).toBe(502)
    expect(sent).toEqual([])
    expect(archived).toEqual(['s-1'])
    const body = (await res.json()) as { detail: string; session_id: string }
    expect(body.session_id).toBe('s-1')
    expect(body.detail).toContain('s-1')
  })

  it('answers 502, and starts no duplicate, when reading the linked session fails for any reason but not-found', async () => {
    const b = fakeBackend({ import_run_id: 's-live' })
    const fake = fakeSessions()
    fake.getFails.error = new Error('connection reset')
    const { hono, started } = app(b.backend, fake)
    const res = await post(hono)
    expect(res.status).toBe(502)
    expect(started).toEqual([])
    expect(b.patches).toEqual([])
  })

  it('archives the session when the first message cannot be sent, so a retry starts a clean one', async () => {
    const b = fakeBackend()
    const fake = fakeSessions()
    fake.sessions.send = async () => {
      throw new Error('no worker')
    }
    const { hono, archived } = app(b.backend, fake)
    expect((await post(hono)).status).toBe(502)
    expect(archived).toEqual(['s-1'])
  })

  it('answers the backend’s 404 for no such invoice', async () => {
    const hono = new Hono()
    const backend = createBackendClient(BACKEND, async () => Response.json({ detail: 'no invoice' }, { status: 404 }))
    registerInvoiceImportRoutes(hono, {
      backend,
      sessions: fakeSessions().sessions as never,
      ready: async () => true,
      origins: ORIGINS,
      remoteAddress: () => LOOPBACK,
    })
    expect((await post(hono)).status).toBe(404)
  })

  it('writes as the person, from the UI’s origin only, with a JSON body naming an invoice', async () => {
    const b = fakeBackend()
    const { hono, started } = app(b.backend)
    expect((await post(hono, { invoice_id: INVOICE }, { origin: 'https://evil.example' })).status).toBe(403)
    expect((await post(hono, { invoice_id: 'nope' })).status).toBe(400)
    expect((await post(hono, {})).status).toBe(400)
    expect(started).toEqual([])
    expect((await post(hono)).status).toBe(200)
    expect(b.authors[0]).not.toBeNull()
  })

  it('answers 503 while the AI database is not ready', async () => {
    const b = fakeBackend()
    const { hono, started } = app(b.backend, fakeSessions(), false)
    expect((await post(hono)).status).toBe(503)
    expect(started).toEqual([])
  })
})
