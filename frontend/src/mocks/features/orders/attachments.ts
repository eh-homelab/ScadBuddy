import { HttpResponse, http } from 'msw'
import type { OrderAttachment, OrderAttachmentPatch, OrderAttachmentRole } from '../../../api/types'
import { SHOWN_IMAGES, THUMBNAILED } from '../../../components/orders/attachmentMedia'
import { event, itemOf, lock, refused, transition } from './orders'
import { BROWSER, fieldProblem, newId, now, problem, Refused, state } from './state'

/**
 * An order's files (#4251, `backend/scadbuddy/api/order_attachments.py`): any file on
 * an order or an item, typed from its bytes as `orders/attachments.py` `sniff` types
 * it, served back as it was sent, and an item's image marked as what was delivered.
 */

const ROLES = new Set<OrderAttachmentRole>(['reference', 'issue', 'finished', 'packed', 'timelapse', 'other'])

/** `orders/attachments.py` `sniff`, over the first bytes. */
function sniff(head: Uint8Array, name: string): [OrderAttachment['kind'], string] {
  const at = (offset: number, text: string) => [...text].every((c, i) => head[offset + i] === c.charCodeAt(0))
  if (head[0] === 0x89 && at(1, 'PNG')) return ['image', 'image/png']
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return ['image', 'image/jpeg']
  if (at(0, 'RIFF') && at(8, 'WEBP')) return ['image', 'image/webp']
  if (at(0, 'GIF8')) return ['image', 'image/gif']
  if (at(4, 'ftyp')) return at(8, 'qt  ') ? ['video', 'video/quicktime'] : at(8, 'heic') ? ['image', 'image/heic'] : ['video', 'video/mp4']
  if (head[0] === 0x1a && head[1] === 0x45 && head[2] === 0xdf && head[3] === 0xa3) return ['video', 'video/webm']
  if (at(0, '%PDF-')) return ['document', 'application/pdf']
  const extension = name.split('.').pop()?.toLowerCase()
  const byName: Record<string, string> = { stl: 'model/stl', '3mf': 'model/3mf', svg: 'image/svg+xml', txt: 'text/plain', html: 'text/html', zip: 'application/zip' }
  return ['other', (extension && byName[extension]) || 'application/octet-stream']
}

async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer())
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
}

type Part = { name: string; filename: string | null; bytes: Uint8Array<ArrayBuffer> }

/**
 * The multipart body's parts, read by hand rather than with `formData()`: undici's
 * parser refuses the body jsdom's XHR sends (as `printAttachments.ts` notes). Each byte
 * becomes one character (never `TextDecoder('latin1')`, which is windows-1252 and moves
 * 0x80-0x9F), so a file's bytes come through as they were sent.
 */
async function parts(request: Request): Promise<Part[]> {
  const boundary = /boundary=(?:"([^"]+)"|([^;]+))/.exec(request.headers.get('content-type') ?? '')
  if (!boundary) return []
  const bytes = new Uint8Array(await request.arrayBuffer())
  let raw = ''
  for (let at = 0; at < bytes.length; at += 0x8000) raw += String.fromCharCode(...bytes.subarray(at, at + 0x8000))
  const found: Part[] = []
  for (const chunk of raw.split(`--${boundary[1] ?? boundary[2]}`)) {
    const split = chunk.indexOf('\r\n\r\n')
    if (split < 0) continue
    const headers = chunk.slice(0, split)
    const name = /name="([^"]*)"/.exec(headers)?.[1]
    if (name === undefined) continue
    const body = chunk.slice(split + 4).replace(/\r\n$/, '')
    found.push({
      name,
      filename: /filename="([^"]*)"/.exec(headers)?.[1] ?? null,
      bytes: Uint8Array.from(body, (c) => c.charCodeAt(0)),
    })
  }
  return found
}

async function upload(request: Request, orderId: string, itemId: string | null) {
  let subject: string
  try {
    const order = lock(orderId)
    if (itemId) itemOf(order, itemId)
    subject = itemId ? `order_item:${itemId}` : `order:${orderId}`
  } catch (error) {
    return refused(request, error)
  }
  const sent = await parts(request)
  const text = (name: string) => new TextDecoder().decode(sent.find((p) => p.name === name)?.bytes ?? new Uint8Array())
  const part = sent.find((p) => p.name === 'file')
  if (!part || part.bytes.length === 0) return problem(request, 422, 'the upload has no `file` part, or it is empty')
  const file = new Blob([part.bytes])
  const role = text('role').trim() || 'other'
  if (!ROLES.has(role as OrderAttachmentRole)) return fieldProblem(request, 'role', `role is one of ${[...ROLES].join(', ')}`)
  const filename = part.filename?.split(/[\\/]/).pop() || 'file'
  const [kind, mediaType] = sniff(part.bytes.slice(0, 16), filename)
  const attachment: OrderAttachment = {
    id: newId('att'),
    order_id: orderId,
    subject,
    item_id: itemId,
    kind,
    role: role as OrderAttachmentRole,
    filename,
    media_type: mediaType,
    size_bytes: file.size,
    sha256: await sha256(file),
    caption: text('caption').trim(),
    created_at: now(),
    created_by: BROWSER,
  }
  state.attachments.push(attachment)
  state.files[attachment.id] = file
  event(lock(orderId), 'attachment.added', { attachment_id: attachment.id, item_id: itemId, role: attachment.role })
  return HttpResponse.json(attachment)
}

function found(orderId: string, id: string): OrderAttachment {
  const order = lock(orderId)
  const attachment = state.attachments.find((a) => a.id === id && a.order_id === orderId)
  if (!attachment) throw new Refused('attachment_not_found', `no attachment ${id} of order ${order.number}`, [], true)
  return attachment
}

export const handlers = [
  http.post('/api/v1/orders/:id/attachments', ({ request, params }) => upload(request, String(params.id), null)),
  http.post('/api/v1/orders/:id/items/:item/attachments', ({ request, params }) => upload(request, String(params.id), String(params.item))),
  http.get('/api/v1/orders/:id/attachments', ({ request, params }) =>
    transition(request, () => {
      lock(String(params.id))
      return { items: state.attachments.filter((a) => a.order_id === params.id) }
    }),
  ),
  http.get('/api/v1/orders/:id/attachments/:a/content', ({ request, params }) => {
    try {
      const attachment = found(String(params.id), String(params.a))
      const download = new URL(request.url).searchParams.get('download') === 'true'
      return new HttpResponse(state.files[attachment.id], {
        headers: { 'Content-Type': download ? 'application/octet-stream' : attachment.media_type },
      })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.get('/api/v1/orders/:id/attachments/:a/thumbnail', ({ request, params }) => {
    try {
      const attachment = found(String(params.id), String(params.a))
      if (!THUMBNAILED.has(attachment.media_type)) {
        return problem(request, 404, 'only a PNG, JPEG or WebP photo has a thumbnail')
      }
      return new HttpResponse(state.files[attachment.id], { headers: { 'Content-Type': attachment.media_type } })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.patch('/api/v1/orders/:id/attachments/:a', async ({ request, params }) => {
    const body = (await request.json()) as OrderAttachmentPatch
    if (body.role != null && !ROLES.has(body.role)) return fieldProblem(request, 'role', 'not a role')
    return transition(request, () => {
      const attachment = found(String(params.id), String(params.a))
      if (body.role != null && body.role !== attachment.role) {
        event(lock(String(params.id)), 'attachment.role', { attachment_id: attachment.id, from_role: attachment.role, to: body.role })
        attachment.role = body.role
      }
      if (body.caption != null && body.caption !== attachment.caption) {
        event(lock(String(params.id)), 'attachment.caption', { attachment_id: attachment.id, from_caption: attachment.caption, to: body.caption })
        attachment.caption = body.caption
      }
      return attachment
    })
  }),
  http.delete('/api/v1/orders/:id/attachments/:a', ({ request, params }) => {
    try {
      const attachment = found(String(params.id), String(params.a))
      const item = state.items.find((i) => i.id === attachment.item_id)
      const delivered = item?.delivered_attachment_id === attachment.id
      if (item && delivered) item.delivered_attachment_id = null
      state.attachments = state.attachments.filter((a) => a.id !== attachment.id)
      delete state.files[attachment.id]
      event(lock(String(params.id)), 'attachment.removed', { attachment_id: attachment.id, item_id: attachment.item_id, filename: attachment.filename, was_delivered: delivered })
      return new HttpResponse(null, { status: 204 })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.put('/api/v1/orders/:id/items/:item/delivered', async ({ request, params }) => {
    const { attachment_id: id } = (await request.json()) as { attachment_id: string | null }
    try {
      const order = lock(String(params.id))
      const item = itemOf(order, String(params.item))
      if (id !== null) {
        const attachment = state.attachments.find((a) => a.id === id)
        if (!attachment || attachment.subject !== `order_item:${item.id}`) return fieldProblem(request, 'attachment_id', `attachment ${id} is not attached to this item`)
        if (attachment.kind !== 'image' || !SHOWN_IMAGES.has(attachment.media_type)) {
          return fieldProblem(request, 'attachment_id', `attachment ${id} is not an image a browser shows (PNG, JPEG, WebP, GIF or AVIF), so it cannot stand as the item's picture`)
        }
      }
      if ((item.delivered_attachment_id ?? null) !== id) {
        item.delivered_attachment_id = id
        event(order, 'item.delivered', { item_id: item.id, attachment_id: id })
      }
      return HttpResponse.json(item)
    } catch (error) {
      return refused(request, error)
    }
  }),
]
