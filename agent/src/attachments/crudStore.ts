import { CrudReadError, crudRead, crudResponse } from '../api/crudRead.js'
import { UUID_SEGMENT, segment } from '../api/crudStores.js'
import type { SessionImages } from '../sessions/blobs.js'
import type { ImageMediaType, PreviewMediaType } from '../sessions/images.js'
import type { Owner } from '../sessions/protocol.js'
import {
  type AttachmentRef,
  AttachmentError,
  type Attachments,
  type AttachmentView,
  type ResolvedAttachment,
  startAttachmentSweeper,
  unknownAttachment,
} from './store.js'

// The staged attachments over the API's CRUD routes (#5299 step 7; backend
// api/crud_agent_blobs.py, area `attachments`): `AttachmentStore` method for method. The image
// and its preview are the upload's multipart parts and each read's body, never base64 (D8,
// #2479). An attachment id is the only id placed in a route's path, and only once it is a
// UUID; the owner travels in the query or the body. A claim stores the bytes the turn already
// holds through the session's images (`SessionImages`), then deletes the staging rows, as the
// SQL store does.

const UPLOAD_TIMEOUT_MS = 60_000

type Resolved = { id: string; name: string; media_type: ImageMediaType; preview_media_type: PreviewMediaType }

export class CrudAttachmentStore implements Attachments {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #blobs: SessionImages

  constructor(baseUrl: string, blobs: SessionImages, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#blobs = blobs
  }

  async put(owner: Owner, image: Parameters<Attachments['put']>[1]): Promise<AttachmentView> {
    const form = new FormData()
    form.append('meta', JSON.stringify({ owner: { kind: owner.kind, id: owner.id }, media_type: image.mediaType, preview_media_type: image.preview.mediaType }))
    form.append('image', new Blob([new Uint8Array(image.bytes)], { type: 'application/octet-stream' }), 'image')
    form.append('preview', new Blob([new Uint8Array(image.preview.bytes)], { type: 'application/octet-stream' }), 'preview')
    let response: Response
    try {
      response = await crudResponse(this.#fetch, this.#base, 'an attachment', '/attachments', { method: 'POST', body: form, timeoutMs: UPLOAD_TIMEOUT_MS })
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute && error.reason === 'quota') throw new AttachmentError('quota', error.detail)
      throw error
    }
    const view = (await response.json()) as { id: string; name: string; media_type: ImageMediaType; preview: { name: string; media_type: PreviewMediaType } }
    return { id: view.id, name: view.name, mediaType: view.media_type, preview: { name: view.preview.name, mediaType: view.preview.media_type } }
  }

  async #bytes(owner: Owner, id: string, part: 'image' | 'preview'): Promise<Buffer | undefined> {
    const query = new URLSearchParams({ owner_kind: owner.kind, owner_id: owner.id }).toString()
    try {
      const response = await crudResponse(this.#fetch, this.#base, `attachment ${id}`, `/attachments/${segment(id, UUID_SEGMENT, 'an attachment id')}/${part}?${query}`)
      return Buffer.from(await response.arrayBuffer())
    } catch (error) {
      if (error instanceof CrudReadError && error.fromRoute && error.status === 404) return undefined
      throw error
    }
  }

  async resolve(owner: Owner, refs: readonly AttachmentRef[]): Promise<ResolvedAttachment[]> {
    if (refs.length === 0) return []
    const ids = refs.map((r) => r.id)
    const bad = ids.findIndex((id) => !UUID_SEGMENT.test(id))
    if (bad >= 0) throw new AttachmentError('not_found', unknownAttachment(bad, ids[bad] ?? ''))
    const { attachments } = await crudRead<{ attachments: Resolved[] }>(this.#fetch, this.#base, 'the attachments', '/attachments/resolve', {
      method: 'POST',
      body: { owner: { kind: owner.kind, id: owner.id }, ids },
    })
    const byId = new Map(attachments.map((a) => [a.id, a]))
    const out: ResolvedAttachment[] = []
    for (const [index, ref] of refs.entries()) {
      const row = byId.get(ref.id)
      const image = row && (await this.#bytes(owner, ref.id, 'image'))
      const preview = image && (await this.#bytes(owner, ref.id, 'preview'))
      // One that expired between the reads reads as unknown, as one that had expired before.
      if (!row || !image || !preview) throw new AttachmentError('not_found', unknownAttachment(index, ref.id))
      out.push({
        id: row.id,
        image: { mediaType: row.media_type, bytes: image, preview: { mediaType: row.preview_media_type, bytes: preview } },
        blob: { name: row.name, mediaType: row.media_type, bytes: image },
      })
    }
    return out
  }

  async claim(sessionId: string, owner: Owner, attachments: readonly ResolvedAttachment[]): Promise<void> {
    if (attachments.length === 0) return
    await this.#blobs.put(sessionId, attachments.map((a) => a.blob))
    await this.#delete(owner, attachments.map((a) => a.id))
  }

  async #delete(owner: Owner, ids: string[]): Promise<number> {
    const { deleted } = await crudRead<{ deleted: number }>(this.#fetch, this.#base, 'the attachments\' delete', '/attachments/deletes', {
      method: 'POST',
      body: { owner: { kind: owner.kind, id: owner.id }, ids },
    })
    return deleted
  }

  async remove(owner: Owner, id: string): Promise<boolean> {
    if (!UUID_SEGMENT.test(id)) return false
    return (await this.#delete(owner, [id])) > 0
  }

  async sweep(): Promise<number> {
    const { deleted } = await crudRead<{ deleted: number }>(this.#fetch, this.#base, 'the attachments\' sweep', '/attachments/sweeps', { method: 'POST', body: {} })
    return deleted
  }

  startSweeper(everyMs: number, options: { log?: (message: string) => void } = {}): () => void {
    return startAttachmentSweeper(() => this.sweep(), everyMs, options)
  }
}
