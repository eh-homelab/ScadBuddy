import type { Hono } from 'hono'
import { z } from 'zod'
import { AttachmentError, type AttachmentStore } from '../attachments/store.js'
import type { OriginPolicy } from '../http/origins.js'
import { IMAGE_DATA_MAX, PREVIEW_DATA_MAX, UserImageSchema } from '../sessions/images.js'
import { BROWSER_USER } from './approvals.js'
import { jsonBodyLimit, type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The assistant panel's image uploads (#1941, attachments/store.ts). The panel
// uploads an image as soon as it is attached, and its `user.message` then names
// it, `{ kind: 'attachment', id }`, so no image travels in the chat socket.
//
//   POST   /api/v1/ai/attachments       body { mediaType, data, preview: { mediaType, data } }
//                                       (sessions/images.ts UserImageSchema) → 201 { id, preview }
//   DELETE /api/v1/ai/attachments/:id   → 204; 404 when it is not the browser user's
//
// Both pass guard.ts `uiRequestProblem` (the one origin allowlist,
// http/origins.ts) and act as the browser user, as the chat socket does. The
// body is capped at one image and its preview before it is read. A refusal
// names fields and caps, never the bytes, and nothing logs them.

export const ATTACHMENTS_PATH = '/api/v1/ai/attachments'

/** One image, its preview, and the JSON around them. */
export const ATTACHMENT_BODY_MAX = IMAGE_DATA_MAX + PREVIEW_DATA_MAX + 4096

export type AttachmentRouteDeps = {
  /** Undefined without a database (spec §9); the routes then answer 503. */
  attachments: AttachmentStore | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'image uploads'

export function registerAttachmentRoutes(app: Hono, deps: AttachmentRouteDeps): void {
  async function store(): Promise<AttachmentStore | string> {
    if (!deps.attachments) return NO_DATABASE
    return (await deps.ready()) ? deps.attachments : NOT_READY
  }

  app.post(ATTACHMENTS_PATH, jsonBodyLimit(ATTACHMENT_BODY_MAX), async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const attachments = await store()
    if (typeof attachments === 'string') return c.json({ detail: attachments }, 503)
    const type = c.req.header('content-type')?.split(';')[0]?.trim().toLowerCase()
    if (type !== 'application/json') return c.json({ detail: 'request body must be application/json' }, 415)
    let body: unknown
    try {
      body = await c.req.json()
    } catch {
      return c.json({ detail: 'body is not valid JSON' }, 400)
    }
    const parsed = UserImageSchema.safeParse(body)
    if (!parsed.success) {
      // images.ts messages name fields and caps only: they never quote the bytes.
      const detail = parsed.error.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
      return c.json({ detail }, 400)
    }
    try {
      return c.json(await attachments.put(BROWSER_USER, parsed.data), 201)
    } catch (err) {
      if (err instanceof AttachmentError && err.code === 'quota') return c.json({ detail: err.message }, 429)
      throw err
    }
  })

  app.delete(`${ATTACHMENTS_PATH}/:id`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const attachments = await store()
    if (typeof attachments === 'string') return c.json({ detail: attachments }, 503)
    const id = c.req.param('id')
    if (!z.uuid().safeParse(id).success || !(await attachments.remove(BROWSER_USER, id))) {
      return c.json({ detail: 'no such attachment' }, 404)
    }
    return c.body(null, 204)
  })
}

/** The panel's image uploads. `deps.attachments` is declared in app.ts (the chat socket reads it too). */
export const route: RouteModule = {
  register(app, deps) {
    registerAttachmentRoutes(app, {
      attachments: deps.attachments,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
