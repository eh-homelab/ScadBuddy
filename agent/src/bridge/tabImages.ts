import type { Sql } from 'postgres'
import type { ImageMediaType } from '../sessions/images.js'
import type { SessionImage } from '../sessions/blobs.js'

// The images a tab answers with (`browser_screenshot`, #254), between the replica that
// holds the tab's socket and the one whose call asked for it (bridge/relay.ts, #4780).
// The tab sends the bytes as binary frames (bridge/protocol.ts `image`) to the replica
// it is connected to, which stores them here before it hands the call's answer back;
// the answer names the image only, `{name, mediaType}` (#2479), so it crosses Temporal
// small, and the tool reads the bytes by that name wherever it runs.
//
// A name is the bytes' sha256 and the type's extension (sessions/blobs.ts BLOB_NAME),
// checked against the bytes before they are stored, so one name is one image and a
// row cannot be made to answer for another. An image is kept for TAB_IMAGE_TTL_MS only:
// the call that asked for it reads it at once; each put drops the rows past that.

/** How long a tab's image is held for the call that asked for it. */
export const TAB_IMAGE_TTL_MS = 10 * 60_000
/** Images held at once without a database (one process). */
export const MEMORY_TAB_IMAGES_MAX = 16

export type StoredTabImage = { mediaType: ImageMediaType; bytes: Buffer }

export interface TabImageStore {
  put(image: SessionImage): Promise<void>
  /** `signal` stops the read with the call it is for. */
  get(name: string, signal?: AbortSignal): Promise<StoredTabImage | undefined>
}

/** Without a database: only this process's tabs are reached (hub.ts), so this process holds their images. */
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

  async get(name: string): Promise<StoredTabImage | undefined> {
    this.#drop()
    const held = this.#images.get(name)
    return held ? { mediaType: held.image.mediaType, bytes: held.image.bytes } : undefined
  }

  #drop(): void {
    const oldest = this.#now() - TAB_IMAGE_TTL_MS
    for (const [name, held] of this.#images) if (held.at < oldest) this.#images.delete(name)
  }
}

/** `ai_tab_images` (db/migrations/20261101T1813Z_tab_images.sql), shared by every replica. */
export class PostgresTabImageStore implements TabImageStore {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async put(image: SessionImage): Promise<void> {
    const ttl = `${TAB_IMAGE_TTL_MS} milliseconds`
    await this.#sql`DELETE FROM ai_tab_images WHERE stored_at < now() - ${ttl}::interval`
    await this.#sql`
      INSERT INTO ai_tab_images (name, media_type, bytes) VALUES (${image.name}, ${image.mediaType}, ${image.bytes})
      ON CONFLICT (name) DO UPDATE SET stored_at = now()`
  }

  async get(name: string, signal?: AbortSignal): Promise<StoredTabImage | undefined> {
    const ttl = `${TAB_IMAGE_TTL_MS} milliseconds`
    const query = this.#sql<{ media_type: ImageMediaType; bytes: Buffer }[]>`
      SELECT media_type, bytes FROM ai_tab_images WHERE name = ${name} AND stored_at >= now() - ${ttl}::interval`
    const onAbort = () => query.cancel()
    signal?.addEventListener('abort', onAbort, { once: true })
    try {
      const [row] = await query
      return row ? { mediaType: row.media_type, bytes: row.bytes } : undefined
    } finally {
      signal?.removeEventListener('abort', onAbort)
    }
  }
}
