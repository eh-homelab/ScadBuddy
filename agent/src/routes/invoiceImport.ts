import type { Context, Hono } from 'hono'
import type { ContentfulStatusCode } from 'hono/utils/http-status'
import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import { browserUser } from '../auth/browserSession.js'
import { invoiceImportPrompt } from '../filament/invoiceImportPrompt.js'
import type { OriginPolicy } from '../http/origins.js'
import { checkedAttachments, DOCUMENT_MEDIA_TYPES, IMAGE_BYTES_MAX, IMAGE_MEDIA_TYPES, PDF_BYTES_MAX, unsupported } from '../sessions/images.js'
import { SessionError, type SessionManager } from '../sessions/manager.js'
import { authored, authorHeaders } from '../tools/authorship.js'
import { jsonBodyLimit, type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// Start the import of a filament invoice (#6267, plan 2026-11-18-filament-invoice-import.md
// Decision 4): the invoice page's "Import" button.
//
//   POST /api/v1/ai/filament/invoice-import   {invoice_id} → {session_id}
//
// It starts one assistant session, links it to the invoice, and only then sends it the import prompt
// (filament/invoiceImportPrompt.ts) with the invoice's file attached. It links the session to the
// invoice as its `import_run_id`. It starts the session through the manager, as POST
// /api/v1/ai/sessions does (routes/sessions.ts), as the browser user; it reaches the invoice
// through the backend's /api/v1/filament routes with the agent's service credential (#5295).
//
//   404  no such invoice (the backend's)
//   409  `confirmed`: the invoice is confirmed, nothing to import
//   409  `import_running` + `session_id`: a live session already imports it (Review Focus 2);
//        the page opens that session. A session that is done, failed, archived or gone is dead,
//        and a new import replaces it
//   422  `unreadable_file`: the bytes are not the type the invoice names
//   422  the invoice has no file, or a file type ScadBuddy cannot read (the person's own sentence
//        as `detail`, Review Focus 5)
//   502  `upstream_error`, no `session_id`: a rival linked its session during the race and has since
//        closed; it is not handed to the page, and a retry starts a clean import
//   502  the session started but could not be linked (`session_id` names it): it is archived,
//        and it ran no turn; a retry then starts a clean one
//
// The order is: start the session with no prompt (no turn runs), claim the link, then send the
// import prompt and the file as its first message. A lost claim or a failed link therefore runs
// no agent and its session is archived, so nothing is left in the person's list.
//
// Two calls at once: one in-process queue per invoice makes the second wait for the first and
// then find its live session (409). A call on another replica is caught by the backend: the link
// is written only while `import_run_id` is still the value this call read (`if_import_run_id`,
// one UPDATE under the invoice's row lock), so the loser's write is refused and its (turnless) session is archived.

declare module '../app.js' {
  interface AppDeps {
    /** What the invoice import needs (main.ts); without it the route answers 503. */
    invoiceImport?: { backend: BackendClient } | undefined
  }
}

const TOOL = 'filament_invoice_import'
const Body = z.strictObject({ invoice_id: z.string().uuid() })
const READABLE: readonly string[] = [...IMAGE_MEDIA_TYPES, ...DOCUMENT_MEDIA_TYPES]
const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
/** A session that no longer takes messages: ended, or put away. */
const ENDED = new Set(['done', 'failed'])
/** The backend's own refusals the route forwards as they are; any other is the agent's problem. */
const FORWARDED = new Set([404, 422])

const megabytes = (bytes: number) => `${(Math.floor(bytes / 100_000) / 10).toFixed(1)} MB`
const UNREADABLE = 'This file is not a readable PDF or photo. Upload it again or use another file.'
const TOO_LARGE =
  `This file is too large for the assistant to read. It can read at most ${megabytes(IMAGE_BYTES_MAX)} for a photo ` +
  `and ${megabytes(PDF_BYTES_MAX)} for a PDF. Upload a smaller photo or a PDF.`
/** What a refused session start tells the person (Minor 4): the reason, and when to try again. */
const REFUSED: Partial<Record<string, string>> = {
  rate_limited: 'You have started many sessions in a short time. Wait a minute, then import again.',
  unavailable: 'The assistant is not ready yet. Wait a moment, then import again.',
}

export type InvoiceImportDeps = {
  backend: BackendClient | undefined
  sessions: SessionManager | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

type Answer = { status: ContentfulStatusCode; body: Record<string, unknown> }
const answer = (status: ContentfulStatusCode, body: Record<string, unknown>): Answer => ({ status, body })

export function registerInvoiceImportRoutes(app: Hono, deps: InvoiceImportDeps): void {
  const limit = jsonBodyLimit()
  /** The tail of each invoice's queue of starts on this replica. */
  const queues = new Map<string, Promise<unknown>>()

  /** Runs `job` after every start already queued for `key`. */
  async function serially<T>(key: string, job: () => Promise<T>): Promise<T> {
    const run = (queues.get(key) ?? Promise.resolve()).then(job, job)
    const tail = run.catch(() => undefined)
    queues.set(key, tail)
    try {
      return await run
    } finally {
      if (queues.get(key) === tail) queues.delete(key)
    }
  }

  /**
   * Whether session `id` still takes messages. Every browser principal is one person (#4975 D8),
   * so a live session is always the caller's.
   */
  async function live(sessions: SessionManager, id: string, owner: ReturnType<typeof browserUser>): Promise<boolean> {
    try {
      const session = await sessions.get(id, owner)
      return session.archivedAt === null && !ENDED.has(session.status)
    } catch (err) {
      // Only "no such session" means gone; any other error is not an answer, so it reaches the route (502).
      if (err instanceof SessionError && err.code === 'not_found') return false
      throw err
    }
  }

  /** Puts away a session this call made and will not use, so no "Import invoice" session is left in the list. */
  async function putAway(sessions: SessionManager, id: string, owner: ReturnType<typeof browserUser>): Promise<void> {
    await sessions.edits.update(id, owner, { archived: true }).catch((err: unknown) => {
      console.error(`invoice import: could not archive the unused session ${id}: ${String(err)}`)
    })
  }

  async function start(
    sessions: SessionManager,
    backend: BackendClient,
    invoiceId: string,
    owner: ReturnType<typeof browserUser>,
  ): Promise<Answer> {
    const read = await backend.GET('/api/v1/filament/invoices/{invoice_id}', { params: { path: { invoice_id: invoiceId } } })
    if (read.error !== undefined || !read.data) return upstream(read.response.status, 'read the invoice')
    const invoice = read.data
    if (invoice.status === 'confirmed') {
      return answer(409, { detail: 'This invoice is confirmed. There is nothing left to import.', type: 'confirmed' })
    }
    if (!invoice.file) {
      return answer(422, { detail: 'This invoice has no file. Add its PDF or photo first, then import it.' })
    }
    const mediaType = invoice.file.media_type.toLowerCase()
    if (!READABLE.includes(mediaType)) return answer(422, { detail: unsupported(mediaType) })

    const cap = mediaType === 'application/pdf' ? PDF_BYTES_MAX : IMAGE_BYTES_MAX
    if (invoice.file.size > cap) return answer(422, { detail: TOO_LARGE })

    const running = invoice.import_run_id
    if (running !== null && (await live(sessions, running, owner))) {
      return answer(409, { detail: 'This invoice is already being imported. Open its session.', type: 'import_running', session_id: running })
    }

    const file = await backend.GET('/api/v1/filament/invoices/{invoice_id}/file', {
      params: { path: { invoice_id: invoiceId } },
      parseAs: 'arrayBuffer',
    })
    if (file.error !== undefined || !file.data) return upstream(file.response.status, 'read the invoice file')
    const attachments = checkedAttachments([{ mediaType, bytes: Buffer.from(file.data as ArrayBuffer), fileName: invoice.file.name }])
    if (!attachments.ok) {
      console.error(`invoice import: the file of invoice ${invoiceId} was refused: ${attachments.error}`)
      return answer(422, { detail: UNREADABLE, type: 'unreadable_file' })
    }

    // No prompt: no turn runs until the link is claimed, so a lost claim never runs an agent.
    const { session } = await sessions.start(owner, {
      origin: 'chat',
      title: `Import invoice ${invoice.number ?? invoice.file.name}`,
    })

    const linked = await backend.PATCH('/api/v1/filament/invoices/{invoice_id}', {
      params: { path: { invoice_id: invoiceId } },
      body: { import_run_id: session.id, if_import_run_id: running },
    })
    if (linked.error === undefined) {
      try {
        await sessions.send(session.id, owner, invoiceImportPrompt(invoiceId), { images: attachments.attachments })
      } catch (err) {
        await putAway(sessions, session.id, owner)
        throw err
      }
      return answer(200, { session_id: session.id })
    }

    await putAway(sessions, session.id, owner)
    if (linked.response.status === 409) {
      // A rival linked its session first (or the invoice was confirmed meanwhile): theirs stands.
      const now = await backend.GET('/api/v1/filament/invoices/{invoice_id}', { params: { path: { invoice_id: invoiceId } } })
      const winner = now.data?.import_run_id
      if (now.data?.status === 'confirmed') return answer(409, { detail: 'This invoice is confirmed. There is nothing left to import.', type: 'confirmed' })
      if (winner) {
        // A closed rival is not handed to the page: it would open a dead session.
        if (!(await live(sessions, winner, owner))) return answer(502, { detail: 'The import could not start. Try again.', type: 'upstream_error' })
        return answer(409, { detail: 'This invoice is already being imported. Open its session.', type: 'import_running', session_id: winner })
      }
    }
    console.error(`invoice import: the backend answered HTTP ${linked.response.status} linking session ${session.id} to invoice ${invoiceId}`)
    return answer(502, {
      detail: `The import session ${session.id} started, but ScadBuddy could not link it to the invoice, so it was put away. Try again.`,
      type: 'upstream_error',
      session_id: session.id,
    })
  }

  /** A backend refusal as the route's answer: its own 404 and 422 as they are, anything else a 502. */
  function upstream(status: number, what: string): Answer {
    if (FORWARDED.has(status)) return answer(status as ContentfulStatusCode, { detail: `the backend refused to ${what}`, type: 'backend_refused' })
    console.error(`invoice import: the backend answered HTTP ${status} when asked to ${what}`)
    return answer(502, { detail: `the backend did not ${what}`, type: 'upstream_error' })
  }

  app.post('/api/v1/ai/filament/invoice-import', limit, async (c: Context) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'an invoice import')
    if (problem) return c.json({ detail: problem }, 403)
    if (c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase() !== 'application/json') {
      return c.json({ detail: 'request body must be application/json' }, 400)
    }
    const body = Body.safeParse(await c.req.json().catch(() => undefined))
    if (!body.success) {
      return c.json({ detail: body.error.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ') }, 400)
    }
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    if (!deps.backend) return c.json({ detail: 'this agent cannot reach the backend' }, 503)
    if (!(await deps.ready())) return c.json({ detail: NOT_READY }, 503)
    const owner = browserUser(c)
    const backend = authored(deps.backend, authorHeaders({ id: owner.id, kind: 'browser', tiers: [] }, undefined, TOOL))
    const { invoice_id: id } = body.data
    const done = await serially(id, () => start(deps.sessions as SessionManager, backend, id, owner)).catch((err: unknown) => {
      console.error(`invoice import: invoice ${id} failed: ${String(err)}`)
      const why = err instanceof SessionError ? REFUSED[err.code] : undefined
      if (why !== undefined) return answer((err as SessionError).status, { detail: why, type: 'session_refused' })
      return answer(502, { detail: 'The import could not start. Try again.', type: 'upstream_error' })
    })
    return c.json(done.body, done.status)
  })
}

export const route: RouteModule = {
  register(app, deps) {
    registerInvoiceImportRoutes(app, {
      backend: deps.invoiceImport?.backend,
      sessions: deps.sessions,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
