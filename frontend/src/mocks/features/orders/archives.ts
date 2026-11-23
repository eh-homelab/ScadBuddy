import { HttpResponse, http } from 'msw'
import type { ArchiveCopy, ArchiveOffer, AttemptArchives, Fulfilment, OrderAttachment } from '../../../api/types'
import { mockSettings } from '../../handlers'
import { event, itemOf, lock, refused } from './orders'
import { BROWSER, newId, now, problem, Refused, state } from './state'

/**
 * #4253 (spec 2026-10-18 §8.2, `backend/scadbuddy/orders/archives.py`): a printed
 * attempt's Bambuddy archives and the copy of one's finish photo or timelapse onto the
 * attempt. Every settled attempt a print started has one archive, numbered from its
 * attempt; with no Bambuddy URL there are none.
 *
 * #7236 (spec 2026-11-05 §4.3, §4.4), the states the order page shows:
 * - an archive completed under an hour ago (a hand attempt's archive completes when it is
 *   recorded) has no timelapse yet: "waiting for Bambuddy to process the timelapse";
 * - a failed attempt's archive has none after that hour: "no timelapse in Bambuddy";
 * - otherwise it has both media. A read starts their import (`AttemptMedia`), and the next
 *   read finds them attached, as the workflow's first step would.
 */

const PHOTO = Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, ...new Array<number>(64).fill(0x6a)])
const VIDEO = Uint8Array.from([0, 0, 0, 0x18, ...[...'ftypisom'].map((c) => c.charCodeAt(0)), ...new Array<number>(64).fill(0x76)])
const HOUR_MS = 60 * 60_000

/** Each attempt's imports a read started, which the next read finds done. */
const importing = new WeakMap<Fulfilment, Set<string>>()

function attemptOf(params: Record<string, string | readonly string[] | undefined>): Fulfilment {
  const order = lock(String(params.id))
  itemOf(order, String(params.item))
  const attempt = state.fulfilments.find((f) => f.id === String(params.f) && f.order_item_id === String(params.item))
  if (!attempt) throw new Refused('not_found', `no attempt ${String(params.f)} of that item`, [], true)
  return attempt
}

function offersOf(attempt: Fulfilment): ArchiveOffer[] {
  if (!mockSettings().bambuddy_url) return []
  // #4251: an attempt by hand names the archive it is.
  const ids = attempt.print_run_id ? [8000 + attempt.attempt] : (attempt.archive_ids ?? [])
  const completed = attempt.finished_at ?? attempt.created_at
  const processed = Date.now() - Date.parse(completed) >= HOUR_MS
  return ids.map((id) => ({
    archive_id: id,
    status: 'completed',
    finish_photos: [`finish_${id}_ab12.jpg`],
    timelapse: processed && attempt.outcome !== 'failed',
    completed_at: completed,
    actual_seconds: 3600,
    grams: 9.5,
  }))
}

/** What the attempt, or its item, holds of the archive's `what` (`archive_media_attachments`). */
function attachedOf(attempt: Fulfilment, archiveId: number, what: ArchiveCopy['what']): OrderAttachment | undefined {
  return state.attachments.find(
    (a) => (a.fulfilment_id === attempt.id || (a.item_id === attempt.order_item_id && !a.fulfilment_id)) && a.archive_id === archiveId && a.archive_media === what,
  )
}

function archivesOf(attempt: Fulfilment): ArchiveOffer[] {
  return offersOf(attempt).map((offer) => ({
    ...offer,
    finished_attachment_id: attachedOf(attempt, offer.archive_id, 'finished_photo')?.id ?? null,
    timelapse_attachment_id: attachedOf(attempt, offer.archive_id, 'timelapse')?.id ?? null,
  }))
}

/** Copies `what` of the archive onto the attempt, once; a chosen photo `name` is another copy (`copy_in`). */
function copyIn(orderId: string, attempt: Fulfilment, archive: ArchiveOffer, what: ArchiveCopy['what'], name?: string | null): OrderAttachment {
  const same = attachedOf(attempt, archive.archive_id, what)
  if (same && !name) return same
  const photo = what === 'finished_photo'
  const role = photo ? 'finished' : 'timelapse'
  const bytes = photo ? PHOTO : VIDEO
  const attachment: OrderAttachment = {
    id: newId('att'),
    order_id: orderId,
    subject: `order_item_fulfilment:${attempt.id}`,
    item_id: attempt.order_item_id,
    fulfilment_id: attempt.id,
    kind: photo ? 'image' : 'video',
    role,
    filename: photo ? (name ?? archive.finish_photos![0]!) : `timelapse-${archive.archive_id}.mp4`,
    media_type: photo ? 'image/jpeg' : 'video/mp4',
    size_bytes: bytes.length,
    sha256: `${role}-${archive.archive_id}`.padEnd(64, '0'),
    caption: `Attempt ${attempt.attempt}, Bambuddy archive ${archive.archive_id}`,
    created_at: now(),
    // Spec 2026-11-05 §4.5: an automatic write names the attempt's creator.
    created_by: attempt.created_by ?? BROWSER,
    archive_id: archive.archive_id,
    archive_media: what,
  }
  state.attachments.push(attachment)
  state.files[attachment.id] = new Blob([bytes])
  event(lock(orderId), 'attachment.added', { attachment_id: attachment.id, item_id: attempt.order_item_id, role })
  return attachment
}

/** The import a read starts: what the last read started is done now; the rest starts. */
function importStep(orderId: string, attempt: Fulfilment) {
  const started = importing.get(attempt) ?? new Set<string>()
  importing.set(attempt, started)
  for (const offer of offersOf(attempt)) {
    const wanted: ArchiveCopy['what'][] = []
    if ((offer.finish_photos ?? []).length > 0) wanted.push('finished_photo')
    if (offer.timelapse) wanted.push('timelapse')
    for (const what of wanted) {
      const key = `${offer.archive_id}:${what}`
      if (started.has(key)) copyIn(orderId, attempt, offer, what)
      else if (!attachedOf(attempt, offer.archive_id, what)) started.add(key)
    }
  }
}

export const handlers = [
  http.get('/api/v1/orders/:id/items/:item/fulfilments/:f/archives', ({ request, params }) => {
    try {
      const attempt = attemptOf(params)
      // The imports an earlier read started are done; the ones this read starts show on
      // the next read. The answer is what is attached now.
      importStep(String(params.id), attempt)
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
      if (body.what === 'timelapse' && !archive.timelapse) throw new Refused('no_timelapse', `archive ${archive.archive_id} has no timelapse`)
      return HttpResponse.json(copyIn(String(params.id), attempt, archive, body.what, body.name))
    } catch (error) {
      return refused(request, error)
    }
  }),
]
