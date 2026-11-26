import { randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { z } from 'zod'
import {
  ATTACHMENT_TTL_MS,
  type AttachmentLimits,
  type AttachmentRef,
  AttachmentError,
  type Attachments,
  type AttachmentView,
  OWNER_BYTES_MAX,
  OWNER_ROWS_MAX,
  type ResolvedAttachment,
  startAttachmentSweeper,
  unknownAttachment,
} from '../../../src/attachments/store.js'
import { type SessionImages, sessionImage } from '../../../src/sessions/blobs.js'
import type { ImageMediaType, PreviewMediaType, UserImage } from '../../../src/sessions/images.js'
import type { Owner } from '../../../src/sessions/protocol.js'
import { SessionBlobs } from './sessionBlobs.js'

// The staged attachments over SQL on the test database: what the agent ran itself before #5299
// step 12, and what the API's `attachments` routes (tools/go/core/internal/blobs) run. Test-only.

type Row = {
  id: string
  name: string
  media_type: ImageMediaType
  data: Buffer
  preview_media_type: PreviewMediaType
  preview: Buffer
}

const lockKey = (owner: Pick<Owner, 'kind' | 'id'>) => `scadbuddy:ai_attachments:${owner.kind}:${owner.id}`

export class AttachmentStore implements Attachments {
  private readonly sql: Sql
  private readonly blobs: SessionImages
  private readonly limits: AttachmentLimits

  constructor(sql: Sql, options: { blobs?: SessionImages; limits?: Partial<AttachmentLimits> } = {}) {
    this.sql = sql
    this.blobs = options.blobs ?? new SessionBlobs(sql)
    this.limits = { ttlMs: ATTACHMENT_TTL_MS, rowsMax: OWNER_ROWS_MAX, bytesMax: OWNER_BYTES_MAX, ...options.limits }
  }

  /** Stores a checked image for `owner`; refused `quota` past the owner's caps. */
  async put(owner: Owner, image: UserImage): Promise<AttachmentView> {
    const { bytes } = image
    const { name } = sessionImage(image.mediaType, bytes)
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
        INSERT INTO ai_attachments (id, owner_kind, owner_id, name, media_type, data, preview_media_type, preview, expires_at)
        VALUES (${id}, ${owner.kind}, ${owner.id}, ${name}, ${image.mediaType}, ${bytes},
                ${image.preview.mediaType}, ${image.preview.bytes}, now() + (${ttlMs} * interval '1 millisecond'))`
    })
    const preview = sessionImage(image.preview.mediaType, image.preview.bytes)
    return { id, name, mediaType: image.mediaType, preview: { name: preview.name, mediaType: preview.mediaType as PreviewMediaType } }
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
      SELECT id, name, media_type, data, preview_media_type, preview FROM ai_attachments
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
          bytes: row.data,
          preview: { mediaType: row.preview_media_type, bytes: row.preview },
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
    return startAttachmentSweeper(() => this.sweep(), everyMs, options)
  }
}
