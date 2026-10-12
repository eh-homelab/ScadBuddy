import type { Hono } from 'hono'
import { z } from 'zod'
import { AttachmentError, type AttachmentStore } from '../attachments/store.js'
import type { OriginPolicy } from '../http/origins.js'
import { IMAGE_BYTES_MAX, PREVIEW_BYTES_MAX, type UserImage, UserImageSchema } from '../sessions/images.js'
import { BROWSER_USER } from './approvals.js'
import { jsonBodyLimit, type RemoteAddress, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The assistant panel's image uploads (#1941, attachments/store.ts). The panel
// uploads an image as soon as it is attached, and its `user.message` then names
// it, `{ kind: 'attachment', id }`, so no image travels in the chat socket.
//
//   POST   /api/v1/ai/attachments       multipart/form-data: two file parts, `image` and
//                                       `preview`, each typed by its Content-Type
//                                       (sessions/images.ts UserImageSchema)
//                                       → 201 { id, name, mediaType, preview: { name, mediaType } }
//   DELETE /api/v1/ai/attachments/:id   → 204; 404 when it is not the browser user's
//
// The image goes up as bytes and comes back as pointers, the `<sha256>.<ext>`
// names it and its preview take in the session (#2479): no image is base64 in
// JSON, either way. Both routes pass guard.ts `uiRequestProblem` (the one origin
// allowlist, http/origins.ts) and act as the browser user, as the chat socket
// does. The body is capped at one image and its preview before it is read. A
// refusal names fields and caps, never the bytes, and nothing logs them.

export const ATTACHMENTS_PATH = '/api/v1/ai/attachments'

/** One image, its preview, and the multipart framing around them. */
export const ATTACHMENT_BODY_MAX = IMAGE_BYTES_MAX + PREVIEW_BYTES_MAX + 4096

/** The upload's two parts as an image, or why they are not one. */
async function uploaded(body: Record<string, unknown>): Promise<UserImage | string> {
  const part = async (field: string): Promise<{ mediaType: string; bytes: Buffer } | string> => {
    const value = body[field]
    if (!(value instanceof Blob)) return `${field}: a file part is required`
    return { mediaType: value.type.split(';')[0]!.trim().toLowerCase(), bytes: Buffer.from(await value.arrayBuffer()) }
  }
  const image = await part('image')
  if (typeof image === 'string') return image
  const preview = await part('preview')
  if (typeof preview === 'string') return preview
  const parsed = UserImageSchema.safeParse({ ...image, preview })
  // images.ts messages name fields and caps only: they never quote the bytes.
  if (!parsed.success) return parsed.error.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
  return parsed.data as UserImage
}

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
    if (type !== 'multipart/form-data') {
      return c.json({ detail: 'request body must be multipart/form-data with `image` and `preview` file parts' }, 415)
    }
    let body: Record<string, unknown>
    try {
      body = await c.req.parseBody()
    } catch {
      return c.json({ detail: 'body is not valid multipart/form-data' }, 400)
    }
    const image = await uploaded(body)
    if (typeof image === 'string') return c.json({ detail: image }, 400)
    try {
      return c.json(await attachments.put(BROWSER_USER, image), 201)
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
