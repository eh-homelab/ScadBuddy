import { createHash, randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { z } from 'zod'
import { type SessionImage, SessionBlobs } from '../sessions/blobs.js'
import { type ImageMediaType, type ImagePreview, IMAGES_MAX, type UserImage } from '../sessions/images.js'
import type { Owner } from '../sessions/protocol.js'

// Images the assistant panel uploads when the user attaches them (#1941), so a
// `user.message` names them, `{ kind: 'attachment', id }`, instead of carrying
// their base64 in the chat socket's frame.
//
// `ai_attachments` is a staging area, per owner: the panel uploads before any
// session may exist (a new chat's first message makes it). When a turn that
// names attachments has started, `claim` moves their bytes into the session's
// `ai_session_blobs` through `SessionBlobs.put` (the store #1056's durable
// sessions read images from by `<sha256>.<ext>` name) and deletes the staging
// rows. A row nobody sends expires after ATTACHMENT_TTL_MS and is swept.
// Each owner holds at most OWNER_ROWS_MAX rows and OWNER_BYTES_MAX bytes, so
// the upload route cannot fill the database.
//
// Only an attachment's owner can read, send or delete it: every query names
// the owner, and another owner's id reads as unknown. The bytes are checked by
// the upload route (sessions/images.ts UserImageSchema: type, signature, caps)
// before they are stored. Nothing here logs the bytes, and an error never
// quotes them.

/** How long an attachment waits to be sent. */
export const ATTACHMENT_TTL_MS = 60 * 60 * 1000
/** Attachments one owner may hold, unsent, at once. */
export const OWNER_ROWS_MAX = 24
/** Bytes of unsent attachments one owner may hold (about ten full-size images). */
export const OWNER_BYTES_MAX = 40 * 1024 * 1024
/** How often expired attachments are deleted. */
export const ATTACHMENT_SWEEP_MS = 5 * 60 * 1000

export const AttachmentRefSchema = z.strictObject({
  kind: z.literal('attachment'),
  id: z.uuid().describe('An id POST /api/v1/ai/attachments returned to this caller'),
})
export type AttachmentRef = z.infer<typeof AttachmentRefSchema>

export const AttachmentRefsSchema = z.array(AttachmentRefSchema).min(1).max(IMAGES_MAX)

const EXTENSION: Record<ImageMediaType, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

/** What the upload route answers: the id to send, and the preview the turn will show. */
export type AttachmentView = { id: string; preview: ImagePreview }

/** An attachment read for a turn: the image for the model, and the blob it becomes. */
export type ResolvedAttachment = { id: string; image: UserImage; blob: SessionImage }

export class AttachmentError extends Error {
  override name = 'AttachmentError'
  readonly code: 'not_found' | 'quota'
  constructor(code: 'not_found' | 'quota', message: string) {
    super(message)
    this.code = code
  }
}

/** Why `images[index]`, attachment `id`, cannot be sent: unknown, expired or another owner's read alike. */
export function unknownAttachment(index: number, id: string): string {
  return `images[${index}]: attachment ${id} is unknown or has expired; attach the image again`
}

export type AttachmentLimits = { ttlMs: number; rowsMax: number; bytesMax: number }

type Row = {
  id: string
  name: string
  media_type: ImageMediaType
  data: Buffer
  preview_media_type: ImagePreview['mediaType']
  preview_data: string
}

const lockKey = (owner: Pick<Owner, 'kind' | 'id'>) => `scadbuddy:ai_attachments:${owner.kind}:${owner.id}`

export class AttachmentStore {
  private readonly sql: Sql
  private readonly blobs: SessionBlobs
  private readonly limits: AttachmentLimits

  constructor(sql: Sql, options: { blobs?: SessionBlobs; limits?: Partial<AttachmentLimits> } = {}) {
    this.sql = sql
    this.blobs = options.blobs ?? new SessionBlobs(sql)
    this.limits = { ttlMs: ATTACHMENT_TTL_MS, rowsMax: OWNER_ROWS_MAX, bytesMax: OWNER_BYTES_MAX, ...options.limits }
  }

  /** Stores a checked image for `owner`; refused `quota` past the owner's caps. */
  async put(owner: Owner, image: UserImage): Promise<AttachmentView> {
    const bytes = Buffer.from(image.data, 'base64')
    const name = `${createHash('sha256').update(bytes).digest('hex')}.${EXTENSION[image.mediaType]}`
    const id = randomUUID()
    const { rowsMax, bytesMax, ttlMs } = this.limits
    await this.sql.begin(async (tx) => {
      // The count and the insert under one per-owner lock, so two uploads at once cannot both pass the caps.
      await tx`SELECT pg_advisory_xact_lock(hashtextextended(${lockKey(owner)}, 0))`
      await tx`DELETE FROM ai_attachments WHERE owner_kind = ${owner.kind} AND owner_id = ${owner.id} AND expires_at <= now()`
      const [held] = await tx<{ n: number; bytes: string }[]>`
        SELECT count(*)::int AS n, coalesce(sum(octet_length(data)), 0)::bigint AS bytes
        FROM ai_attachments WHERE owner_kind = ${owner.kind} AND owner_id = ${owner.id}`
      if ((held?.n ?? 0) + 1 > rowsMax || Number(held?.bytes ?? 0) + bytes.length > bytesMax) {
        throw new AttachmentError(
          'quota',
          `too many images waiting to be sent: at most ${rowsMax} images or ${bytesMax} bytes; ` +
            'send or remove some, or wait for them to expire',
        )
      }
      await tx`
        INSERT INTO ai_attachments (id, owner_kind, owner_id, name, media_type, data, preview_media_type, preview_data, expires_at)
        VALUES (${id}, ${owner.kind}, ${owner.id}, ${name}, ${image.mediaType}, ${bytes},
                ${image.preview.mediaType}, ${image.preview.data}, now() + (${ttlMs} * interval '1 millisecond'))`
    })
    return { id, preview: { mediaType: image.preview.mediaType, data: image.preview.data } }
  }

  /**
   * The attachments `refs` name, read for a turn `owner` sends, in order.
   * Refused `not_found`, naming the first by its index, when one is unknown,
   * expired, or another owner's: the three read alike.
   */
  async resolve(owner: Owner, refs: readonly AttachmentRef[]): Promise<ResolvedAttachment[]> {
    if (refs.length === 0) return []
    const ids = refs.map((r) => r.id)
    const rows = await this.sql<Row[]>`
      SELECT id, name, media_type, data, preview_media_type, preview_data FROM ai_attachments
      WHERE id = ANY(${ids}::uuid[]) AND owner_kind = ${owner.kind} AND owner_id = ${owner.id} AND expires_at > now()`
    const byId = new Map(rows.map((r) => [r.id, r]))
    return refs.map((ref, index) => {
      const row = byId.get(ref.id)
      if (!row) {
        throw new AttachmentError('not_found', unknownAttachment(index, ref.id))
      }
      return {
        id: row.id,
        image: {
          mediaType: row.media_type,
          data: row.data.toString('base64'),
          preview: { mediaType: row.preview_media_type, data: row.preview_data },
        },
        blob: { name: row.name, mediaType: row.media_type, bytes: row.data },
      }
    })
  }

  /**
   * Moves attachments a started turn sent into its session: the bytes into
   * ai_session_blobs (SessionBlobs.put, idempotent by name), then the staging
   * rows are deleted. A row already swept is no matter: the bytes are in hand.
   */
  async claim(sessionId: string, owner: Owner, attachments: readonly ResolvedAttachment[]): Promise<void> {
    if (attachments.length === 0) return
    await this.blobs.put(sessionId, attachments.map((a) => a.blob))
    const ids = attachments.map((a) => a.id)
    await this.sql`
      DELETE FROM ai_attachments WHERE id = ANY(${ids}::uuid[]) AND owner_kind = ${owner.kind} AND owner_id = ${owner.id}`
  }

  /** Deletes one of `owner`'s attachments (the panel removed it); false when there was none. */
  async remove(owner: Owner, id: string): Promise<boolean> {
    if (!z.uuid().safeParse(id).success) return false
    const rows = await this.sql`
      DELETE FROM ai_attachments WHERE id = ${id} AND owner_kind = ${owner.kind} AND owner_id = ${owner.id} RETURNING id`
    return rows.length > 0
  }

  /** Deletes every expired attachment; how many went. */
  async sweep(): Promise<number> {
    const rows = await this.sql`DELETE FROM ai_attachments WHERE expires_at <= now() RETURNING id`
    return rows.length
  }

  /** Sweeps every `everyMs`; the returned function stops it. */
  startSweeper(everyMs: number, options: { log?: (message: string) => void } = {}): () => void {
    const timer = setInterval(() => {
      this.sweep().catch((err: unknown) => {
        options.log?.(`attachments: sweep failed: ${err instanceof Error ? err.message : String(err)}`)
      })
    }, everyMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}
