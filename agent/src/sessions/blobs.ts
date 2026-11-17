import { createHash } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import { IMAGE_DATA_MAX, type ImageMediaType, IMAGE_MEDIA_TYPES, sniff, type UserImage } from './images.js'
import { forkChain } from './eventLog.js'
import type { ImageRef, SentImage, ServerEvent } from './protocol.js'

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
// them. The one exception is a queued message's images (#6413): a write made by
// `putMade` answers the names it made and when, and once the message leaves the queue
// unrun, or the queue refuses it, `delete` takes those names back, unless a write
// stored them again since. Every write sets `created_at` (an image already there
// keeps its bytes), and a write of a name waits for the transaction that holds its
// row, so a later write's time is always past `at`: an image another message or turn
// stores, before its event or queue entry names it, is never deleted. A fork copies none (#4212): it reads its ancestors' images that the events it
// reads through its chain name (`get`), never those past its cut (#2150), and
// forgetting a parent copies those into it first (`copyForkBlobs`).
//
// Once the API lists area `session-blobs` (#5299 step 7), `CrudSessionBlobs` (crudBlobs.ts)
// runs each method on the API's CRUD routes instead (backend api/crud_agent_blobs.py), the
// same statements in one transaction. A forget through the API copies a fork's images there
// (backend events.py `copy_fork_blobs`, #5299 step 9, part c); `copyForkBlobs` is the agent's own
// forget transaction's, while the API has no area `forget` or no KEK.

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

/**
 * An AgentSession placement's hold on its turn (turnHost.ts): the session's row still names
 * this attempt and this turn, checked `FOR SHARE` in the write's own transaction.
 */
export type TurnFence = { attempt: string; turnId: string }

/**
 * What one write of images made (#6413): the names not there before, and a time no later write
 * of one of them comes before (each write sets `created_at`), which `delete` takes as `before`.
 */
export type MadeImages = { names: string[]; at: string }

/** A session's images: `SessionBlobs` over SQL, `CrudSessionBlobs` over the API's routes. */
export interface SessionImages {
  put(sessionId: string, images: readonly SessionImage[], toolUseId?: string): Promise<void>
  /**
   * `put`, answering what it made, one entry per write (transaction) that made a name: a queued
   * message's images, which `delete` takes back once it leaves the queue unrun.
   */
  putMade(sessionId: string, images: readonly SessionImage[]): Promise<MadeImages[]>
  /**
   * Deletes the images one write made, unless a write stored one again since or a tool call names
   * it; the names deleted. With `fence`, nothing unless the fence holds.
   */
  delete(sessionId: string, made: MadeImages, fence?: TurnFence): Promise<string[]>
  /** False when the fence refuses; over the API's routes a batch stored before the refusal stays (crudBlobs.ts). */
  putIf(sessionId: string, images: readonly SessionImage[], fence: TurnFence): Promise<boolean>
  get(sessionId: string, name: string): Promise<StoredBlob | undefined>
}

export class SessionBlobs implements SessionImages {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  /**
   * Stores a session's images; one already there (the same bytes) keeps its bytes, and its
   * `created_at` is set to this write's time (#6413).
   * With `toolUseId`, a durable tool call's images (#2478): they are recorded as what
   * that call produced, in order, replacing what an earlier attempt of it recorded, so
   * follow_session can name them on its `tool.result`.
   */
  async put(sessionId: string, images: readonly SessionImage[], toolUseId?: string): Promise<void> {
    if (toolUseId === undefined) {
      await this.putMade(sessionId, images)
      return
    }
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
  async putIf(sessionId: string, images: readonly SessionImage[], fence: TurnFence): Promise<boolean> {
    return this.sql.begin(async (tx) => {
      if (!(await held(tx, sessionId, fence))) return false
      await this.insert(tx, sessionId, images)
      return true
    })
  }

  /** One transaction, so a later write of a name waits for it and sets a time past `at`. */
  async putMade(sessionId: string, images: readonly SessionImage[]): Promise<MadeImages[]> {
    const made = await this.sql.begin((tx) => this.insert(tx, sessionId, images))
    return made.names.length && made.at !== undefined ? [{ names: made.names, at: made.at }] : []
  }

  async delete(sessionId: string, made: MadeImages, fence?: TurnFence): Promise<string[]> {
    const names = made.names.filter((name) => BLOB_NAME.test(name))
    if (names.length === 0) return []
    return this.sql.begin(async (tx) => {
      if (fence && !(await held(tx, sessionId, fence))) return []
      // Sent as text: postgres.js sends a timestamptz parameter through a Date, which drops the microseconds.
      const rows = await tx<{ name: string }[]>`
        DELETE FROM ai_session_blobs b
         WHERE b.session_id = ${sessionId} AND b.name = ANY(${names}) AND b.created_at <= ${made.at}::text::timestamptz
           AND NOT EXISTS (SELECT 1 FROM ai_session_tool_images t WHERE t.session_id = b.session_id AND t.name = b.name)
        RETURNING b.name`
      return rows.map((r) => r.name).sort()
    })
  }

  /**
   * Stores each image, or sets the time of one already there (its bytes stay): the names made,
   * and the latest time set (backend api/crud_agent_blobs.py `_insert_blobs`).
   */
  private async insert(tx: TransactionSql, sessionId: string, images: readonly SessionImage[]): Promise<{ names: string[]; at?: string }> {
    const names: string[] = []
    let at: string | undefined
    for (const image of images) {
      const [row] = await tx<{ at: string; made: boolean }[]>`
        INSERT INTO ai_session_blobs (session_id, name, media_type, data, created_at)
        VALUES (${sessionId}, ${image.name}, ${image.mediaType}, ${image.bytes}, clock_timestamp())
        ON CONFLICT (session_id, name) DO UPDATE SET created_at = clock_timestamp()
        RETURNING to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') AS at, (xmax = 0) AS made`
      if (row!.made && !names.includes(image.name)) names.push(image.name)
      // ISO 8601 in UTC to the microsecond, so the strings compare as the times do.
      if (at === undefined || row!.at > at) at = row!.at
    }
    return at === undefined ? { names } : { names, at }
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

/** Whether the session's row still names the fence's turn attempt, locked `FOR SHARE` in `tx`. */
async function held(tx: TransactionSql, sessionId: string, fence: TurnFence): Promise<boolean> {
  const rows = await tx`
    SELECT 1 FROM ai_sessions WHERE id = ${sessionId} AND turn_attempt = ${fence.attempt}
       AND durable_turn_id = ${fence.turnId} FOR SHARE`
  return rows.length > 0
}

/** The events a fork takes that can name an image (forkPoint.ts `blobNames`), all among eventLog.ts `FORK_EVENT_TYPES`. */
const IMAGE_EVENT_TYPES: readonly ServerEvent['type'][] = ['tool.result', 'user.turn']

async function getThroughChain(sql: Sql | TransactionSql, sessionId: string, name: string): Promise<StoredBlob | undefined> {
  const [row] = await sql<{ media_type: ImageMediaType; data: Buffer }[]>`
    ${forkChain(sql, sessionId)}
    SELECT b.media_type, b.data FROM chain c JOIN ai_session_blobs b ON b.session_id = c.id AND b.name = ${name}
     WHERE c.depth = 0 OR EXISTS (
       SELECT 1 FROM chain r JOIN ai_session_events e ON e.session_id = r.id
        WHERE r.depth > 0 AND e.seq <= r.bound AND strpos(e.event, ${name}) > 0
          AND e.event::jsonb->>'type' = ANY(${sql.array(IMAGE_EVENT_TYPES as string[])}))
     ORDER BY c.depth LIMIT 1`
  return row ? { mediaType: row.media_type, bytes: row.data } : undefined
}

/**
 * Before the parent of `sessionId` goes (store.ts `copyForkRanges`, in forgetSubject's
 * transaction, before the link is cleared): the images `names` lists (forkPoint.ts `blobNames`
 * of the events `copyForkEvents` copied), found through its chain, become rows of its own. One
 * it holds already (a queued message's write made it) gets this copy's time, as every write
 * does (#6413): the copied events name it now, so that message's `delete` must not take it.
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
    ON CONFLICT (session_id, name) DO UPDATE SET created_at = clock_timestamp()`
}
