import type { Sql } from 'postgres'
import { sealBytes, openBytes } from '../secrets.js'
import { BLOB_NAME, type SessionImage, type StoredBlob } from '../sessions/blobs.js'
import type { ImageMediaType } from '../sessions/images.js'
import { payloadKeyLock, type PayloadKeys, SubjectForgotten } from './payloadCodec.js'

// The images a flow run's tool calls returned (#2477), in `ai_flow_blobs`. A flow call
// has no session, so `ai_session_blobs` (sessions/blobs.ts, keyed by and deleted with a
// session) cannot hold them. These are kept by the run's subject, `flow-<run id>`, named
// by their bytes as a session's are, and sealed under the subject's own payload key
// (payloadCodec.ts) in the context `flowBlobContext`: forgetting the run deletes that
// key and these rows together (backend flows/forget.py), and a row that outlived its
// key would be unreadable anyway.
//
// The flow sees only pointers, `{name, mediaType, bytes}` (bytes: the size), never the
// image: the bytes stay here, out of every Temporal payload and the run's stored result.
// A flow hands a pointer on as a `flow_image` reference (tools/imageRefs.ts), which
// resolves only inside the same run; the run page shows them through the backend's
// `GET /workflow-runs/{id}/images/{name}`, which opens them with the same key.

/** What a flow holds for an image its call returned: a pointer, never the bytes. */
export type FlowImage = { name: string; mediaType: ImageMediaType; bytes: number }

/** The context an image is sealed in under its run's payload key (backend flows/images.py `blob_context`). */
export const flowBlobContext = (subject: string, name: string): string => `flow-blob:${subject}:${name}`

const FLOW_SUBJECT = /^flow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export interface FlowBlobStore {
  /** Keeps a run's images; one already there (the same bytes) is left as it is. */
  put(subject: string, images: readonly SessionImage[]): Promise<void>
  /** One of the run's images, or undefined: a malformed name is never looked up. */
  get(subject: string, name: string): Promise<StoredBlob | undefined>
}

export function flowImageOf(image: SessionImage): FlowImage {
  return { name: image.name, mediaType: image.mediaType, bytes: image.bytes.length }
}

export class PgFlowBlobs implements FlowBlobStore {
  readonly #sql: Sql
  readonly #keys: PayloadKeys

  constructor(sql: Sql, keys: PayloadKeys) {
    this.#sql = sql
    this.#keys = keys
  }

  async put(subject: string, images: readonly SessionImage[]): Promise<void> {
    if (!FLOW_SUBJECT.test(subject)) throw new Error(`${subject} is not a flow run`)
    if (images.length === 0) return
    // SubjectForgotten for a run already forgotten: its key is never made again.
    const key = await this.#keys.keyFor(subject, true)
    try {
      const rows = images.map((image) => ({
        subject,
        name: image.name,
        media_type: image.mediaType,
        size: image.bytes.length,
        data: sealBytes(key, image.bytes, flowBlobContext(subject, image.name)),
      }))
      // Under the lock forgetting takes, and never for a forgotten run: a key this
      // process still caches must not leave rows behind a forget that just committed.
      const written = await this.#sql.begin(async (tx) => {
        await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
        const [forgotten] = await tx`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`
        if (forgotten) return false
        await tx`
          INSERT INTO ai_flow_blobs ${tx(rows, 'subject', 'name', 'media_type', 'size', 'data')}
          ON CONFLICT (subject, name) DO NOTHING`
        return true
      })
      if (!written) throw new SubjectForgotten(`${subject} was forgotten; its images are not kept`)
    } finally {
      key.fill(0)
    }
  }

  async get(subject: string, name: string): Promise<StoredBlob | undefined> {
    if (!FLOW_SUBJECT.test(subject) || !BLOB_NAME.test(name)) return undefined
    const [row] = await this.#sql<{ media_type: ImageMediaType; data: Buffer }[]>`
      SELECT media_type, data FROM ai_flow_blobs WHERE subject = ${subject} AND name = ${name}`
    if (!row) return undefined
    const key = await this.#keys.keyFor(subject, false)
    try {
      return { mediaType: row.media_type, bytes: openBytes(key, row.data, flowBlobContext(subject, name)) }
    } finally {
      key.fill(0)
    }
  }
}
