import { HttpResponse, http } from 'msw'
import type { PrintAttachment } from '../../api/types'
import * as fixtures from '../fixtures'
import { mockModels, problem } from '../handlers'

/**
 * #309 — the photos and videos added to a print (`backend/scadbuddy/api/print_attachments.py`):
 * add, caption, remove, copy into a template's gallery. `GET /prints/:id` (in `handlers.ts`)
 * lists them through `attachmentsOf`.
 */

const base = '/api/v1'

/** The photos and videos added to each print, by archive id. */
const attachments = new Map<number, PrintAttachment[]>()
let next = 1

export function reset(): void {
  attachments.clear()
  next = 1
}

/** The attachments added to a print, oldest first: live, so a test can change one. */
export function attachmentsOf(archiveId: number): PrintAttachment[] {
  return attachments.get(archiveId) ?? []
}

const noPrint = (archiveId: unknown) =>
  problem(404, 'Not Found', `archive ${String(archiveId)} is not a print of any ScadBuddy output`)
const noAttachment = (archiveId: unknown, id: unknown) =>
  problem(404, 'Not Found', `print ${String(archiveId)} has no attachment ${String(id)}`)

export const handlers = [
  // Typed by its name here (the server reads the bytes).
  http.post(`${base}/prints/:archiveId/attachments`, async ({ params, request }) => {
    const print = fixtures.prints.find((p) => String(p.archive_id) === params['archiveId'])
    if (!print) return noPrint(params['archiveId'])
    // Read as text, not `formData()`: undici's parser refuses the body jsdom's XHR
    // sends. The part's type decides; a part sent with no name is named by it.
    const raw = await request.text()
    const type = /name="file";[^\r]*\r\nContent-Type: ([^\r]+)/.exec(raw)?.[1]
    if (type === undefined) return problem(422, 'Unprocessable Content', 'the upload has no `file` part')
    const extension = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'video/mp4': 'mp4', 'video/webm': 'webm' }[type]
    if (extension === undefined) {
      return problem(415, 'Unsupported Media Type', 'the upload is not a PNG, JPEG or WebP photo, or an MP4 or WebM video')
    }
    const video = type.startsWith('video/')
    const sent = /filename="([^"]*)"/.exec(raw)?.[1]
    const id = next++
    const caption = /name="caption"\r\n\r\n([^\r]*)\r\n/.exec(raw)?.[1]
    const added: PrintAttachment = {
      id,
      kind: video ? 'video' : 'photo',
      caption: caption || null,
      url: `/api/v1/prints/${print.archive_id}/attachments/${id}/file`,
      filename: sent && sent !== 'blob' ? sent : `${video ? 'video' : 'photo'}-${id}.${extension}`,
      size_bytes: raw.length,
      promoted_to: [],
      created_at: '2026-10-13T12:00:00Z',
    }
    attachments.set(print.archive_id, [...attachmentsOf(print.archive_id), added])
    return HttpResponse.json(added, { status: 201 })
  }),

  http.patch(`${base}/prints/:archiveId/attachments/:id`, async ({ params, request }) => {
    const found = attachmentsOf(Number(params['archiveId'])).find((each) => String(each.id) === params['id'])
    if (!found) return noAttachment(params['archiveId'], params['id'])
    const { caption } = (await request.json()) as { caption: string | null }
    found.caption = caption?.trim() || null
    return HttpResponse.json(found)
  }),

  http.delete(`${base}/prints/:archiveId/attachments/:id`, ({ params }) => {
    const archiveId = Number(params['archiveId'])
    const list = attachmentsOf(archiveId)
    if (!list.some((each) => String(each.id) === params['id'])) return noAttachment(archiveId, params['id'])
    attachments.set(
      archiveId,
      list.filter((each) => String(each.id) !== params['id']),
    )
    return new HttpResponse(null, { status: 204 })
  }),

  http.post(`${base}/prints/:archiveId/attachments/:id/promote`, async ({ params, request }) => {
    const found = attachmentsOf(Number(params['archiveId'])).find((each) => String(each.id) === params['id'])
    if (!found) return noAttachment(params['archiveId'], params['id'])
    const { slug } = (await request.json()) as { slug: string }
    const model = mockModels().find((each) => each.slug === slug)
    if (!model) return problem(404, 'Not Found', `there is no template ${slug}`)
    if (found.promoted_to.includes(slug)) {
      return problem(409, 'Conflict', `this attachment is in ${slug}'s gallery already`)
    }
    found.promoted_to = [...found.promoted_to, slug]
    return HttpResponse.json(model)
  }),

  http.get(`${base}/prints/:archiveId/attachments/:id/file`, () =>
    HttpResponse.arrayBuffer(
      Uint8Array.from(atob(fixtures.MEDIA_PNG_BASE64), (c) => c.charCodeAt(0)).buffer,
      { headers: { 'Content-Type': 'image/png' } },
    ),
  ),
]
