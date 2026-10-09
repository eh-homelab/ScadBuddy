import { HttpResponse, http } from 'msw'
import type { ImagePreview } from '../../agent/chat/protocol'

/**
 * #1941 — the agent's `POST /api/v1/ai/attachments` and `DELETE …/:id`
 * (agent `src/routes/attachments.ts`): the composer uploads each image it is given, and
 * a message names it by id. The scripted agent (`../agent.ts`) shows an attachment's
 * preview in the `user.turn`, as the real one does, and forgets it once sent.
 */

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const PREVIEW_TYPES = ['image/png', 'image/jpeg', 'image/webp']

const uploads = new Map<string, ImagePreview>()
let next = 0

export function reset(): void {
  uploads.clear()
  next = 0
}

/** The preview an upload carried, taken as the real agent moves it into the session; undefined when unknown. */
export function takeAttachment(id: string): ImagePreview | undefined {
  const preview = uploads.get(id)
  uploads.delete(id)
  return preview
}

export const handlers = [
  http.post('/api/v1/ai/attachments', async ({ request }) => {
    const body = (await request.json()) as {
      mediaType?: unknown
      data?: unknown
      preview?: { mediaType?: unknown; data?: unknown }
    }
    const { preview } = body
    if (
      typeof body.mediaType !== 'string' ||
      !IMAGE_TYPES.includes(body.mediaType) ||
      typeof body.data !== 'string' ||
      body.data === '' ||
      typeof preview?.mediaType !== 'string' ||
      !PREVIEW_TYPES.includes(preview.mediaType) ||
      typeof preview.data !== 'string'
    ) {
      return HttpResponse.json({ detail: 'mediaType: a PNG, JPEG, GIF or WebP image with a preview' }, { status: 400 })
    }
    const id = `00000000-0000-4000-8000-${String(++next).padStart(12, '0')}`
    const kept = { mediaType: preview.mediaType as ImagePreview['mediaType'], data: preview.data }
    uploads.set(id, kept)
    return HttpResponse.json({ id, preview: kept }, { status: 201 })
  }),
  http.delete('/api/v1/ai/attachments/:id', ({ params }) =>
    uploads.delete(String(params.id))
      ? new HttpResponse(null, { status: 204 })
      : HttpResponse.json({ detail: 'no such attachment' }, { status: 404 }),
  ),
]
