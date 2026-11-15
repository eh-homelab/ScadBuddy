import type { Sql } from 'postgres'
import { CrudReadError, crudResponse } from '../api/crudRead.js'
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
//
// The rows are the API's (#5299 step 6, area `forget`): once it lists the area, `CrudFlowBlobs`
// keeps and reads them through `…/crud/agent/flow-blobs/{subject}` (backend
// api/crud_agent_forget.py), sealed here as before and sent as bytes (multipart, never base64,
// #5299 D8); the tombstone check, the cap and the insert are one transaction there, under the
// subject's lock. `PgFlowBlobs` is the SQL it ran itself until then (D9).

/** What a flow holds for an image its call returned: a pointer, never the bytes. */
export type FlowImage = { name: string; mediaType: ImageMediaType; bytes: number }

/** The context an image is sealed in under its run's payload key (backend flows/images.py `blob_context`). */
export const flowBlobContext = (subject: string, name: string): string => `flow-blob:${subject}:${name}`

/**
 * The most images one run keeps (#2578): `ai_flow_blobs` has no retention but forgetting
 * the run, so a flow that loops over an image-returning tool must not grow it without
 * bound. Each call keeps at most RESULT_IMAGES_MAX (sessions/blobs.ts); past this, a
 * call's new images are not kept and its result says so.
 */
export const FLOW_IMAGES_MAX = 256

const FLOW_SUBJECT = /^flow-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

export interface FlowBlobStore {
  /**
   * Keeps a run's images; one already there (the same bytes) is left as it is. Answers
   * the names the run holds afterwards, of those given: a new one past FLOW_IMAGES_MAX
   * is not kept.
   */
  put(subject: string, images: readonly SessionImage[]): Promise<readonly string[]>
  /** One of the run's images, or undefined: a malformed name is never looked up. */
  get(subject: string, name: string): Promise<StoredBlob | undefined>
}

export function flowImageOf(image: SessionImage): FlowImage {
  return { name: image.name, mediaType: image.mediaType, bytes: image.bytes.length }
}

export class PgFlowBlobs implements FlowBlobStore {
  readonly #sql: Sql
  readonly #keys: PayloadKeys
  readonly #max: number

  constructor(sql: Sql, keys: PayloadKeys, max = FLOW_IMAGES_MAX) {
    this.#sql = sql
    this.#keys = keys
    this.#max = max
  }

  async put(subject: string, images: readonly SessionImage[]): Promise<readonly string[]> {
    if (!FLOW_SUBJECT.test(subject)) throw new Error(`${subject} is not a flow run`)
    if (images.length === 0) return []
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
      // The lock also serialises the count: two calls of one run never both take the last room.
      const kept = await this.#sql.begin(async (tx) => {
        await tx`SELECT pg_advisory_xact_lock(hashtext(${payloadKeyLock(subject)}))`
        const [forgotten] = await tx`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`
        if (forgotten) return undefined
        const held = new Set((await tx<{ name: string }[]>`SELECT name FROM ai_flow_blobs WHERE subject = ${subject}`).map((r) => r.name))
        const fresh = rows.filter((r, i) => !held.has(r.name) && rows.findIndex((o) => o.name === r.name) === i)
        const taken = fresh.slice(0, Math.max(0, this.#max - held.size))
        if (taken.length) {
          await tx`
            INSERT INTO ai_flow_blobs ${tx(taken, 'subject', 'name', 'media_type', 'size', 'data')}
            ON CONFLICT (subject, name) DO NOTHING`
        }
        for (const r of taken) held.add(r.name)
        return [...new Set(rows.map((r) => r.name))].filter((name) => held.has(name))
      })
      if (!kept) throw new SubjectForgotten(`${subject} was forgotten; its images are not kept`)
      return kept
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

/** A stored image's type, in the CRUD answer's header (backend crud_agent_forget.py `MEDIA_TYPE_HEADER`). */
const MEDIA_TYPE_HEADER = 'x-scadbuddy-media-type'
const MEDIA_TYPES = new Set<string>(['image/png', 'image/jpeg', 'image/gif', 'image/webp'])

/** A run's images through the API's CRUD routes (above): sealed and opened here, kept there. */
export class CrudFlowBlobs implements FlowBlobStore {
  readonly #base: string
  readonly #keys: PayloadKeys
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, keys: PayloadKeys, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#keys = keys
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async put(subject: string, images: readonly SessionImage[]): Promise<readonly string[]> {
    if (!FLOW_SUBJECT.test(subject)) throw new Error(`${subject} is not a flow run`)
    if (images.length === 0) return []
    // SubjectForgotten for a run already forgotten: its key is never made again.
    const key = await this.#keys.keyFor(subject, true)
    const form = new FormData()
    try {
      form.append('images', JSON.stringify(images.map((image) => ({ name: image.name, media_type: image.mediaType, size: image.bytes.length }))))
      for (const image of images) {
        const sealed = sealBytes(key, image.bytes, flowBlobContext(subject, image.name))
        form.append('blob', new Blob([new Uint8Array(sealed)], { type: 'application/octet-stream' }), image.name)
      }
    } finally {
      key.fill(0)
    }
    let response: Response
    try {
      response = await crudResponse(this.#fetch, this.#base, `the images of ${subject}`, `/flow-blobs/${subject}`, { method: 'POST', body: form })
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 410) throw new SubjectForgotten(`${subject} was forgotten; its images are not kept`)
      throw err
    }
    const { names } = (await response.json()) as { names: string[] }
    return names
  }

  async get(subject: string, name: string): Promise<StoredBlob | undefined> {
    if (!FLOW_SUBJECT.test(subject) || !BLOB_NAME.test(name)) return undefined
    let response: Response
    try {
      response = await crudResponse(this.#fetch, this.#base, `the image ${name} of ${subject}`, `/flow-blobs/${subject}/${name}`)
    } catch (err) {
      if (err instanceof CrudReadError && err.fromRoute && err.status === 404) return undefined
      throw err
    }
    const mediaType = response.headers.get(MEDIA_TYPE_HEADER) ?? ''
    const data = Buffer.from(await response.arrayBuffer())
    if (!MEDIA_TYPES.has(mediaType)) throw new Error(`the API answered the image ${name} of ${subject} with no image type`)
    const key = await this.#keys.keyFor(subject, false)
    try {
      return { mediaType: mediaType as ImageMediaType, bytes: openBytes(key, data, flowBlobContext(subject, name)) }
    } finally {
      key.fill(0)
    }
  }
}

/** `crud` once `useCrud()` holds (the API lists area `forget`), `pg` until then, chosen per call (#5299 D9). */
export function gatedFlowBlobs(pg: FlowBlobStore, crud: FlowBlobStore, useCrud: () => boolean): FlowBlobStore {
  const pick = (): FlowBlobStore => (useCrud() ? crud : pg)
  return {
    put: (subject, images) => pick().put(subject, images),
    get: (subject, name) => pick().get(subject, name),
  }
}
