import { HttpResponse, http } from 'msw'
import type { ArchiveCopy, ArchiveOffer, AttemptArchives, Fulfilment, OrderAttachment } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { event, itemOf, lock, refused } from './orders'
import { BROWSER, newId, now, problem, Refused, state } from './state'

/**
 * #4253 (spec 2026-10-18 §8.2, `backend/scadbuddy/orders/archives.py`): a printed
 * attempt's Bambuddy archives and the copy of one's finish photo or timelapse into the
 * item's files. Every settled attempt a print started has one archive, numbered from its
 * attempt, with a finish photo and a timelapse; with no Bambuddy URL there are none.
 */

const PHOTO = Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, ...new Array<number>(64).fill(0x6a)])
const VIDEO = Uint8Array.from([0, 0, 0, 0x18, ...[...'ftypisom'].map((c) => c.charCodeAt(0)), ...new Array<number>(64).fill(0x76)])

function attemptOf(params: Record<string, string | readonly string[] | undefined>): Fulfilment {
  const order = lock(String(params.id))
  itemOf(order, String(params.item))
  const attempt = state.fulfilments.find((f) => f.id === String(params.f) && f.order_item_id === String(params.item))
  if (!attempt) throw new Refused('not_found', `no attempt ${String(params.f)} of that item`, [], true)
  return attempt
}

function archivesOf(attempt: Fulfilment): ArchiveOffer[] {
  if (!mockSettings().bambuddy_url) return []
  // #4251: an attempt by hand names the archive it is.
  if (!attempt.print_run_id) return (attempt.archive_ids ?? []).map((id) => ({ archive_id: id, status: 'completed', finish_photos: [`finish_${id}_ab12.jpg`], timelapse: true, actual_seconds: 3600, grams: 9.5 }))
  const id = 8000 + attempt.attempt
  return [{ archive_id: id, status: 'completed', finish_photos: [`finish_${id}_ab12.jpg`], timelapse: true, actual_seconds: 3600, grams: 9.5 }]
}

export const handlers = [
  http.get('/api/v1/orders/:id/items/:item/fulfilments/:f/archives', ({ request, params }) => {
    try {
      const attempt = attemptOf(params)
      const archives = archivesOf(attempt)
      if (attempt.finished_at && archives.length > 0) {
        attempt.archive_ids = archives.map((a) => a.archive_id)
        attempt.actual_seconds = 3600
        attempt.actual_grams = 9.5
      }
      return HttpResponse.json<AttemptArchives>({ linked: Boolean(mockSettings().bambuddy_url), attempt, archives })
    } catch (error) {
      return refused(request, error)
    }
  }),
  http.post('/api/v1/orders/:id/items/:item/fulfilments/:f/archives/:archive/copy', async ({ request, params }) => {
    if (!request.headers.get('Idempotency-Key')) {
      return problem(request, 428, 'This request needs an Idempotency-Key header', 'https://scadbuddy.dev/problems/idempotency-key-required')
    }
    const body = (await request.json()) as ArchiveCopy
    try {
      const attempt = attemptOf(params)
      const archive = archivesOf(attempt).find((a) => a.archive_id === Number(params.archive))
      if (!archive) throw new Refused('archive_not_attempts', `archive ${String(params.archive)} is not one of attempt ${attempt.attempt}'s prints`)
      const photo = body.what === 'finished_photo'
      const role = photo ? 'finished' : 'timelapse'
      const itemId = String(params.item)
      const same = state.attachments.find((a) => a.item_id === itemId && a.role === role && a.caption.endsWith(`archive ${archive.archive_id}`))
      if (same) return HttpResponse.json(same)
      const bytes = photo ? PHOTO : VIDEO
      const attachment: OrderAttachment = {
        id: newId('att'),
        order_id: String(params.id),
        subject: `order_item_fulfilment:${attempt.id}`,
        item_id: itemId,
        fulfilment_id: attempt.id,
        kind: photo ? 'image' : 'video',
        role,
        filename: photo ? (body.name ?? archive.finish_photos![0]!) : `timelapse-${archive.archive_id}.mp4`,
        media_type: photo ? 'image/jpeg' : 'video/mp4',
        size_bytes: bytes.length,
        sha256: `${role}-${archive.archive_id}`.padEnd(64, '0'),
        caption: `Attempt ${attempt.attempt}, Bambuddy archive ${archive.archive_id}`,
        created_at: now(),
        created_by: BROWSER,
      }
      state.attachments.push(attachment)
      state.files[attachment.id] = new Blob([bytes])
      event(lock(String(params.id)), 'attachment.added', { attachment_id: attachment.id, item_id: itemId, role })
      return HttpResponse.json(attachment)
    } catch (error) {
      return refused(request, error)
    }
  }),
]
