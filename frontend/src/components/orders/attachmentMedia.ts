import { API_BASE, api } from '../../api/client'
import type { OrderAttachment, OrderAttachmentRole, OrderItem } from '../../api/types'

/** #4251 — what the order page knows about an attached file and an item's picture. */

/** Why a file is kept (`AttachmentRole`), as a person reads it. */
export const ROLE_LABELS: Record<OrderAttachmentRole, string> = {
  reference: 'Customer reference',
  issue: 'Print issue',
  finished: 'Finished print',
  packed: 'Packed',
  timelapse: 'Timelapse',
  other: 'Other',
}
export const ROLES = Object.keys(ROLE_LABELS) as OrderAttachmentRole[]
/** What the server serves inline (`orders/attachments.py` `INLINE`): opened in a tab. */
export const OPENS = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/avif', 'image/heic', 'video/mp4', 'video/quicktime', 'video/webm', 'application/pdf'])

/** The image types a browser draws (`orders/attachments.py` `SHOWN_IMAGES`). */
export const SHOWN_IMAGES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/avif'])
/** The ones the server shrinks to a thumbnail; a GIF or AVIF is shown as it is. */
const THUMBNAILED = new Set(['image/png', 'image/jpeg', 'image/webp'])

/** An attached photo's smallest URL the browser can draw, or null when it cannot draw it. */
export function attachmentImageUrl(orderId: string, attachment: OrderAttachment): string | null {
  if (attachment.kind !== 'image' || !SHOWN_IMAGES.has(attachment.media_type)) return null
  return THUMBNAILED.has(attachment.media_type)
    ? api.orderAttachmentThumbnailUrl(orderId, attachment.id)
    : api.orderAttachmentUrl(orderId, attachment.id)
}

type Source = { url: string; label: string; delivered: boolean }

/**
 * Where an item's picture comes from, best first (#4251): the photo marked as what was
 * delivered, then the output it was ordered from, then its template's own thumbnail.
 * A source that fails to load (an output deleted since) gives way to the next.
 */
export function itemPictureSources(orderId: string, item: OrderItem, attachments: OrderAttachment[]): Source[] {
  const sources: Source[] = []
  const delivered = attachments.find((a) => a.id === item.delivered_attachment_id)
  const deliveredUrl = delivered ? attachmentImageUrl(orderId, delivered) : null
  if (deliveredUrl) sources.push({ url: deliveredUrl, label: `What was delivered: ${item.title}`, delivered: true })
  if (item.output_id) sources.push({ url: api.outputThumbnailUrl(item.output_id), label: `The render of ${item.title}`, delivered: false })
  sources.push({
    url: `${API_BASE}/models/${encodeURIComponent(item.reference.slug)}/thumbnail`,
    label: `${item.reference.template_name}'s picture`,
    delivered: false,
  })
  return sources
}

