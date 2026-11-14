import { createHash } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { IMAGE_DATA_MAX, type ImageMediaType, IMAGE_MEDIA_TYPES, sniff, type UserImage } from './images.js'
import { FORK_EVENT_TYPES, forkChain } from './eventLog.js'
import type { ImageRef, SentImage } from './protocol.js'

// The images a session's tool results carried (#782, design
// docs/superpowers/specs/2026-10-08-friendly-tool-calls-design.md §3.1), in
// `ai_session_blobs`. Claude Code hands every MCP image to the SDK stream as an
// `image` block with its base64 (and writes a copy under its own config
// directory, which on a resumed turn is a temporary one the SDK removes: never
// read that). sdkEvents.ts takes the block; the manager stores the bytes here
// before it logs the `tool.result` that names them; the session route serves
// them. The event carries only `{name, mediaType}`, so the event log every
// watcher replays stays small. The images a user sends with a turn, and their
// previews, are stored here too (`sentImageBlobs`), before the `user.turn`
// event that names them, so any tab loads them by name (#2479). A durable tool
// call's images are stored by its agent-tools activity, which records them under
// the call's tool_use_id (`ai_session_tool_images`); agent-durable's follow_session
// names them on that call's `tool.result` (#2478).
//
// A blob is found by (session, name) and nothing else: no path is ever built
// from a request. The name is the bytes' sha256 and the type's extension, so
// one image twice in a session is one row, and a name that is not that shape
// is refused before the database is asked.
//
// Rows go with their session (ON DELETE CASCADE) and are not pruned before that:
// a session keeps every image its transcript names, so the panel can show any of
// them. A fork copies none (#4212): it reads its ancestors' images that the events it
// reads through its chain name (`get`), never those past its cut (#2150), and
// forgetting a parent copies those into it first (`copyForkBlobs`).

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
  const bytes = Buffer.from(data, 'base64')
  if (sniff(bytes) !== mediaType) return undefined
  return sessionImage(mediaType as ImageMediaType, bytes)
}

/** `bytes` as a session image, named by their sha256 and the type's extension. */
export function sessionImage(mediaType: ImageMediaType, bytes: Buffer): SessionImage {
  return { name: `${createHash('sha256').update(bytes).digest('hex')}.${EXTENSION[mediaType]}`, mediaType, bytes }
}

/**
 * A turn's images as the blobs to store (each full image, then its preview) and
 * what `user.turn` names them by (#2479).
 */
export function sentImageBlobs(images: readonly UserImage[]): { blobs: SessionImage[]; sent: SentImage[] } {
  const blobs: SessionImage[] = []
  const sent = images.map((image) => {
    const full = sessionImage(image.mediaType, image.bytes)
    const small = sessionImage(image.preview.mediaType, image.preview.bytes)
    blobs.push(full, small)
    return {
      name: full.name,
      mediaType: image.mediaType,
      preview: { name: small.name, mediaType: image.preview.mediaType },
    }
  })
  return { blobs, sent }
}

export type StoredBlob = { mediaType: ImageMediaType; bytes: Buffer }

export class SessionBlobs {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  /**
   * Stores a session's images; one already there (the same bytes) is left as it is.
   * With `toolUseId`, a durable tool call's images (#2478): they are recorded as what
   * that call produced, in order, replacing what an earlier attempt of it recorded, so
   * follow_session can name them on its `tool.result`.
   */
  async put(sessionId: string, images: readonly SessionImage[], toolUseId?: string): Promise<void> {
    if (toolUseId === undefined) return this.insert(this.sql, sessionId, images)
    await this.sql.begin(async (tx) => {
      await this.insert(tx, sessionId, images)
      await tx`DELETE FROM ai_session_tool_images WHERE session_id = ${sessionId} AND tool_use_id = ${toolUseId}`
      for (const [ord, image] of images.entries()) {
        await tx`
          INSERT INTO ai_session_tool_images (session_id, tool_use_id, ord, name)
          VALUES (${sessionId}, ${toolUseId}, ${ord}, ${image.name})`
      }
    })
  }

  /**
   * `put` under a fence checked in the same transaction (an AgentSession placement's hold on its
   * turn, turnHost.ts): false, and nothing stored, when the fence refuses.
   */
  async putIf(sessionId: string, images: readonly SessionImage[], fence: (tx: TransactionSql) => Promise<boolean>): Promise<boolean> {
    return this.sql.begin(async (tx) => {
      if (!(await fence(tx))) return false
      await this.insert(tx, sessionId, images)
      return true
    })
  }

  private async insert(sql: Sql | TransactionSql, sessionId: string, images: readonly SessionImage[]): Promise<void> {
    for (const image of images) {
      await sql`
        INSERT INTO ai_session_blobs (session_id, name, media_type, data)
        VALUES (${sessionId}, ${image.name}, ${image.mediaType}, ${image.bytes})
        ON CONFLICT (session_id, name) DO NOTHING`
    }
  }

  /**
   * One of the session's images, or undefined: a malformed name is never looked up. A fork's
   * (#4212) is its own, else an ancestor's that an event it reads through its chain names
   * (eventLog.ts), so a fork reads no image of its parent's turns past the cut.
   */
  async get(sessionId: string, name: string): Promise<StoredBlob | undefined> {
    if (!BLOB_NAME.test(name)) return undefined
    return getThroughChain(this.sql, sessionId, name)
  }
}

async function getThroughChain(sql: Sql | TransactionSql, sessionId: string, name: string): Promise<StoredBlob | undefined> {
  const [row] = await sql<{ media_type: ImageMediaType; data: Buffer }[]>`
    ${forkChain(sql, sessionId)}
    SELECT b.media_type, b.data FROM chain c JOIN ai_session_blobs b ON b.session_id = c.id AND b.name = ${name}
     WHERE c.depth = 0 OR EXISTS (
       SELECT 1 FROM chain r JOIN ai_session_events e ON e.session_id = r.id
        WHERE r.depth > 0 AND e.seq <= r.bound AND strpos(e.event, ${name}) > 0
          AND e.event::jsonb->>'type' = ANY(${sql.array(FORK_EVENT_TYPES as string[])}))
     ORDER BY c.depth LIMIT 1`
  return row ? { mediaType: row.media_type, bytes: row.data } : undefined
}

/**
 * Before the parent of `sessionId` goes (store.ts `copyForkRanges`, in forgetSubject's
 * transaction, before the link is cleared): the images `names` lists (forkPoint.ts `blobNames`
 * of the events `copyForkEvents` copied), found through its chain, become rows of its own.
 */
export async function copyForkBlobs(tx: TransactionSql, sessionId: string, names: readonly string[]): Promise<void> {
  if (names.length === 0) return
  await tx`
    ${forkChain(tx, sessionId)}
    INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)
    SELECT DISTINCT ON (b.name) ${sessionId}::uuid, b.name, b.media_type, b.data, b.created_at
      FROM chain c JOIN ai_session_blobs b ON b.session_id = c.id
     WHERE c.depth > 0 AND b.name = ANY(${names as string[]})
     ORDER BY b.name, c.depth
    ON CONFLICT (session_id, name) DO NOTHING`
}
