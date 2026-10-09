import { createHash } from 'node:crypto'
import type { Sql } from 'postgres'
import { IMAGE_DATA_MAX, type ImageMediaType, IMAGE_MEDIA_TYPES, sniff } from './images.js'
import type { ImageRef } from './protocol.js'

// The images a session's tool results carried (#782, design
// docs/superpowers/specs/2026-10-08-friendly-tool-calls-design.md §3.1), in
// `ai_session_blobs`. Claude Code hands every MCP image to the SDK stream as an
// `image` block with its base64 (and writes a copy under its own config
// directory, which on a resumed turn is a temporary one the SDK removes: never
// read that). sdkEvents.ts takes the block; the manager stores the bytes here
// before it logs the `tool.result` that names them; the session route serves
// them. The event carries only `{name, mediaType}`, so the event log every
// watcher replays stays small.
//
// A blob is found by (session, name) and nothing else: no path is ever built
// from a request. The name is the bytes' sha256 and the type's extension, so
// one image twice in a session is one row, and a name that is not that shape
// is refused before the database is asked.

/** The most images one tool result keeps; any past it stay `[image]` in the summary. */
export const RESULT_IMAGES_MAX = 8

const EXTENSION: Record<ImageMediaType, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

/** What a blob's name must be: lowercase sha256 hex, a dot, one of the four extensions. */
export const BLOB_NAME = /^[0-9a-f]{64}\.(png|jpg|gif|webp)$/

// As images.ts: a plain class, never a group repeated per quartet.
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/

/** An image with its bytes, on its way to the store. */
export type SessionImage = ImageRef & { bytes: Buffer }

/**
 * The image an SDK `image` content block holds, if it is one the panel may
 * show: base64, one of the four Messages API types, its bytes starting with
 * that type's signature, and no larger than an image the Messages API takes.
 * Anything else is undefined, and stays a placeholder.
 */
export function imageOfBlock(block: unknown): SessionImage | undefined {
  if (typeof block !== 'object' || block === null) return undefined
  const source = (block as { type?: unknown; source?: unknown }).source
  if ((block as { type?: unknown }).type !== 'image' || typeof source !== 'object' || source === null) return undefined
  const { type, media_type: mediaType, data } = source as { type?: unknown; media_type?: unknown; data?: unknown }
  if (type !== 'base64' || typeof data !== 'string' || typeof mediaType !== 'string') return undefined
  if (!(IMAGE_MEDIA_TYPES as readonly string[]).includes(mediaType)) return undefined
  if (data.length === 0 || data.length > IMAGE_DATA_MAX || data.length % 4 !== 0 || !BASE64.test(data)) return undefined
  if (sniff(data) !== mediaType) return undefined
  const bytes = Buffer.from(data, 'base64')
  const media = mediaType as ImageMediaType
  return { name: `${createHash('sha256').update(bytes).digest('hex')}.${EXTENSION[media]}`, mediaType: media, bytes }
}

export type StoredBlob = { mediaType: ImageMediaType; bytes: Buffer }

export class SessionBlobs {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  /** Stores a session's images; one already there (the same bytes) is left as it is. */
  async put(sessionId: string, images: readonly SessionImage[]): Promise<void> {
    for (const image of images) {
      await this.sql`
        INSERT INTO ai_session_blobs (session_id, name, media_type, data)
        VALUES (${sessionId}, ${image.name}, ${image.mediaType}, ${image.bytes})
        ON CONFLICT (session_id, name) DO NOTHING`
    }
  }

  /** One of the session's images, or undefined: a malformed name is never looked up. */
  async get(sessionId: string, name: string): Promise<StoredBlob | undefined> {
    if (!BLOB_NAME.test(name)) return undefined
    const [row] = await this.sql<{ media_type: ImageMediaType; data: Buffer }[]>`
      SELECT media_type, data FROM ai_session_blobs WHERE session_id = ${sessionId} AND name = ${name}`
    return row ? { mediaType: row.media_type, bytes: row.data } : undefined
  }

  /** A fork's copy of its parent's images, which the events it copies name. */
  async copy(from: string, to: string): Promise<void> {
    await this.sql`
      INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)
      SELECT ${to}, name, media_type, data, created_at FROM ai_session_blobs WHERE session_id = ${from}
      ON CONFLICT (session_id, name) DO NOTHING`
  }
}
