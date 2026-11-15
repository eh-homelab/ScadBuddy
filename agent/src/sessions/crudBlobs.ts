import { CrudReadError, crudRead, crudResponse } from '../api/crudRead.js'
import type { SavedBodies, SavedMeta } from '../harness/httpRequest.js'
import { BLOB_NAME, type SessionImage, type SessionImages, type StoredBlob, type TurnFence } from './blobs.js'
import { IMAGE_MEDIA_TYPES, type ImageMediaType } from './images.js'

// A session's images and saved HTTP bodies over the API's CRUD routes (#5299 step 7; backend
// api/crud_agent_blobs.py, areas `session-blobs` and `http-bodies`): `SessionBlobs` and
// `PgHttpBodies` method for method, each one route and one transaction. The bytes are the
// request's multipart parts and the answer's body, never base64 (#5299 D8, #2479). A session
// id and a name travel in the query or the body, never the route's path, and a name is
// checked against BLOB_NAME before it is sent.

/** The most images one write sends (backend `IMAGES_PER_WRITE`). */
export const IMAGES_PER_WRITE = 8
/** A write of images or a body carries megabytes: it gets longer than a read. */
const WRITE_TIMEOUT_MS = 60_000

export type CrudBlobsOptions = { fetch?: typeof fetch }

function imageForm(meta: object, images: readonly SessionImage[]): FormData {
  const form = new FormData()
  form.append('meta', JSON.stringify(meta))
  images.forEach((image, index) => {
    form.append(`image${index}`, new Blob([new Uint8Array(image.bytes)], { type: 'application/octet-stream' }), `image${index}`)
  })
  return form
}

function query(params: Record<string, string>): string {
  return `?${new URLSearchParams(params).toString()}`
}

export class CrudSessionBlobs implements SessionImages {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: CrudBlobsOptions = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async #write(sessionId: string, images: readonly SessionImage[], extra: object): Promise<boolean> {
    const meta = { session_id: sessionId, images: images.map((i) => ({ name: i.name, media_type: i.mediaType })), ...extra }
    const response = await crudResponse(this.#fetch, this.#base, `the images of session ${sessionId}`, '/session-blobs', {
      method: 'POST',
      body: imageForm(meta, images),
      timeoutMs: WRITE_TIMEOUT_MS,
    })
    return ((await response.json()) as { stored: boolean }).stored
  }

  async put(sessionId: string, images: readonly SessionImage[], toolUseId?: string): Promise<void> {
    if (toolUseId !== undefined) {
      // One tool call's images are recorded whole, in one transaction: never split.
      if (images.length > IMAGES_PER_WRITE) throw new Error(`a tool call keeps at most ${IMAGES_PER_WRITE} images`)
      await this.#write(sessionId, images, { tool_use_id: toolUseId })
      return
    }
    for (let i = 0; i < images.length; i += IMAGES_PER_WRITE) {
      await this.#write(sessionId, images.slice(i, i + IMAGES_PER_WRITE), {})
    }
  }

  /**
   * One SDK message may carry several tool results' images, more than one write takes: each batch
   * is checked against the fence in its own transaction, and the first one refused stops the rest
   * and answers false (a batch stored before it stays: named by its bytes, and only the session
   * reads it).
   */
  async putIf(sessionId: string, images: readonly SessionImage[], fence: TurnFence): Promise<boolean> {
    const extra = { fence: { attempt: fence.attempt, turn_id: fence.turnId } }
    if (images.length === 0) return this.#write(sessionId, images, extra)
    for (let i = 0; i < images.length; i += IMAGES_PER_WRITE) {
      if (!(await this.#write(sessionId, images.slice(i, i + IMAGES_PER_WRITE), extra))) return false
    }
    return true
  }

  async get(sessionId: string, name: string): Promise<StoredBlob | undefined> {
    if (!BLOB_NAME.test(name)) return undefined
    let response: Response
    try {
      response = await crudResponse(this.#fetch, this.#base, `image ${name}`, `/session-blobs${query({ session_id: sessionId, name })}`)
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute && error.status === 404) return undefined
      throw error
    }
    const mediaType = (response.headers.get('content-type') ?? '').split(';')[0]?.trim() ?? ''
    if (!(IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType)) {
      await response.body?.cancel()
      throw new CrudReadError(`image ${name}: the backend answered a ${mediaType || 'missing'} type`)
    }
    return { mediaType: mediaType as ImageMediaType, bytes: Buffer.from(await response.arrayBuffer()) }
  }
}

/** A saved body's id: a UUID, as http_request makes it. */
const BODY_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export class CrudHttpBodies implements SavedBodies {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #sessionId: string

  constructor(baseUrl: string, sessionId: string, options: CrudBlobsOptions = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#sessionId = sessionId
  }

  #query(id: string, extra: Record<string, string> = {}): string {
    return query({ session_id: this.#sessionId, id, ...extra })
  }

  async save(id: string, bytes: Buffer, meta: SavedMeta): Promise<void> {
    if (!BODY_ID.test(id)) throw new Error('a saved body id is a UUID')
    const form = new FormData()
    form.append(
      'meta',
      JSON.stringify({ session_id: this.#sessionId, id, url: meta.url, content_type: meta.content_type, size_bytes: meta.size_bytes, complete: meta.complete }),
    )
    form.append('body', new Blob([new Uint8Array(bytes)], { type: 'application/octet-stream' }), 'body')
    await crudResponse(this.#fetch, this.#base, `saved body ${id}`, '/http-bodies', { method: 'POST', body: form, timeoutMs: WRITE_TIMEOUT_MS })
  }

  async meta(id: string): Promise<SavedMeta | undefined> {
    if (!BODY_ID.test(id)) return undefined
    try {
      const row = await crudRead<SavedMeta>(this.#fetch, this.#base, `saved body ${id}`, `/http-bodies/meta${this.#query(id)}`)
      return { url: row.url, content_type: row.content_type, size_bytes: row.size_bytes, complete: row.complete }
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute && error.status === 404) return undefined
      throw error
    }
  }

  async #bytes(id: string, range: Record<string, string>): Promise<Buffer | undefined> {
    if (!BODY_ID.test(id)) return undefined
    try {
      const response = await crudResponse(this.#fetch, this.#base, `saved body ${id}`, `/http-bodies/bytes${this.#query(id, range)}`, {
        timeoutMs: WRITE_TIMEOUT_MS,
      })
      return Buffer.from(await response.arrayBuffer())
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute && error.status === 404) return undefined
      throw error
    }
  }

  async read(id: string, offset: number, length: number): Promise<Buffer> {
    return (await this.#bytes(id, { offset: String(offset), length: String(length) })) ?? Buffer.alloc(0)
  }

  async whole(id: string): Promise<{ meta: SavedMeta; bytes: Buffer } | undefined> {
    const meta = await this.meta(id)
    if (!meta) return undefined
    const bytes = await this.#bytes(id, {})
    return bytes && { meta, bytes }
  }
}
