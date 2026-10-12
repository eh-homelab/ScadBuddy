import { HttpResponse, http } from 'msw'
import type { SentImage } from '../../agent/chat/protocol'
import { storeSentImage } from './assistantBlobs'

/**
 * #1941 — the agent's `POST /api/v1/ai/attachments` and `DELETE …/:id`
 * (agent `src/routes/attachments.ts`): the composer uploads each image it is given, and
 * a message names it by id. #2479 — the upload is multipart, two typed file parts
 * (`image`, `preview`), and the answer names them; no image is base64 in JSON. The
 * scripted agent (`../agent.ts`) names an attachment's image and preview in the
 * `user.turn`, as the real one does, by the names they are served under
 * (`./assistantBlobs.ts`), and forgets the upload once sent.
 */

const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp']
const PREVIEW_TYPES = ['image/png', 'image/jpeg', 'image/webp']

const uploads = new Map<string, SentImage>()
let next = 0

export function reset(): void {
  uploads.clear()
  next = 0
}

/** The names an upload's image and preview are served under, as the turn gives them; undefined when unknown. */
export function takeAttachment(id: string): SentImage | undefined {
  const upload = uploads.get(id)
  uploads.delete(id)
  return upload
}

// A file part, not a text field; by shape, since under vitest the parser's `File` is Node's, not jsdom's.
const isFile = (value: FormDataEntryValue | null, types: string[]): value is File =>
  typeof value === 'object' && value !== null && types.includes(value.type) && value.size > 0

export const handlers = [
  http.post('/api/v1/ai/attachments', async ({ request }) => {
    if (!request.headers.get('content-type')?.startsWith('multipart/form-data')) {
      return HttpResponse.json({ detail: 'request body must be multipart/form-data' }, { status: 415 })
    }
    const body = await request.formData()
    const image = body.get('image')
    const preview = body.get('preview')
    if (!isFile(image, IMAGE_TYPES) || !isFile(preview, PREVIEW_TYPES)) {
      return HttpResponse.json({ detail: 'image: a PNG, JPEG, GIF or WebP file part with a preview' }, { status: 400 })
    }
    const id = `00000000-0000-4000-8000-${String(++next).padStart(12, '0')}`
    const sent = {
      ...storeSentImage(image),
      preview: storeSentImage(preview),
    } as SentImage
    uploads.set(id, sent)
    return HttpResponse.json({ id, ...sent }, { status: 201 })
  }),
  http.delete('/api/v1/ai/attachments/:id', ({ params }) =>
    uploads.delete(String(params.id))
      ? new HttpResponse(null, { status: 204 })
      : HttpResponse.json({ detail: 'no such attachment' }, { status: 404 }),
  ),
]
