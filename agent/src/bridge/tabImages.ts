import { type CrudTarget, crudCall, segment } from '../api/crudStores.js'
import { type ImageMediaType, IMAGE_MEDIA_TYPES } from '../sessions/images.js'
import { BLOB_NAME, type SessionImage } from '../sessions/blobs.js'

// The images a tab answers with (`browser_screenshot`, #254), between the replica that
// holds the tab's socket and the one whose call asked for it (bridge/relay.ts, #4780).
// The tab sends the bytes as binary frames (bridge/protocol.ts `image`) to the replica
// it is connected to, which stores them here before it hands the call's answer back;
// the answer names the image only, `{name, mediaType}` (#2479), so it crosses Temporal
// small, and the tool reads the bytes by that name wherever it runs.
//
// A name is the bytes' sha256 and the type's extension (sessions/blobs.ts BLOB_NAME),
// checked against the bytes before they are stored, so one name is one image and a
// row cannot be made to answer for another. The call that asked for an image reads it at
// once, and the read takes it (it is deleted); one never read (its call gave up) is kept
// for TAB_IMAGE_TTL_MS at most, dropped by the next put or read.

/** How long a tab's image is held for the call that asked for it. */
export const TAB_IMAGE_TTL_MS = 10 * 60_000
/** Images held at once without the shared store (one process). */
export const MEMORY_TAB_IMAGES_MAX = 16

export type StoredTabImage = { mediaType: ImageMediaType; bytes: Buffer }

export interface TabImageStore {
  put(image: SessionImage): Promise<void>
  /** The image, deleted as it is read. `signal` stops the read with the call it is for. */
  take(name: string, signal?: AbortSignal): Promise<StoredTabImage | undefined>
}

/** Without the shared store: only this process's tabs are reached (hub.ts), so this process holds their images. */
export class MemoryTabImageStore implements TabImageStore {
  readonly #images = new Map<string, { image: SessionImage; at: number }>()
  readonly #now: () => number

  constructor(now: () => number = Date.now) {
    this.#now = now
  }

  async put(image: SessionImage): Promise<void> {
    this.#drop()
    this.#images.delete(image.name)
    this.#images.set(image.name, { image, at: this.#now() })
    while (this.#images.size > MEMORY_TAB_IMAGES_MAX) this.#images.delete(this.#images.keys().next().value!)
  }

  async take(name: string): Promise<StoredTabImage | undefined> {
    this.#drop()
    const held = this.#images.get(name)
    this.#images.delete(name)
    return held ? { mediaType: held.image.mediaType, bytes: held.image.bytes } : undefined
  }

  #drop(): void {
    const oldest = this.#now() - TAB_IMAGE_TTL_MS
    for (const [name, held] of this.#images) if (held.at < oldest) this.#images.delete(name)
  }
}

/**
 * `ai_tab_images` through the API's CRUD routes, area `bridge-tabs` (#5299 D8): the
 * bytes are the request's and the answer's body, never JSON; the API checks the name against
 * them again.
 */
export class CrudTabImageStore implements TabImageStore {
  readonly #fetch: typeof fetch
  readonly #base: string

  constructor(target: CrudTarget) {
    this.#fetch = target.fetch ?? globalThis.fetch
    this.#base = target.baseUrl
  }

  async put(image: SessionImage): Promise<void> {
    const response = await crudCall(this.#fetch, this.#base, "a tab's image stored", `/tab-images/${segment(image.name, BLOB_NAME, "a tab's image name")}`, {
      method: 'PUT',
      bytes: image.bytes,
      contentType: image.mediaType,
    })
    await response.body?.cancel()
  }

  async take(name: string, signal?: AbortSignal): Promise<StoredTabImage | undefined> {
    // A name that is no blob's names no image.
    if (!BLOB_NAME.test(name)) return undefined
    const response = await crudCall(this.#fetch, this.#base, "a tab's image", `/tab-images/${segment(name, BLOB_NAME, "a tab's image name")}/take`, {
      method: 'POST',
      signal,
    })
    if (response.status === 204) return undefined
    const mediaType = (response.headers.get('content-type') ?? '').split(';')[0]!.trim()
    if (!(IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType)) {
      await response.body?.cancel()
      throw new Error(`a tab's image came back as ${JSON.stringify(mediaType)}, not an image type`)
    }
    return { mediaType: mediaType as ImageMediaType, bytes: Buffer.from(await response.arrayBuffer()) }
  }
}
