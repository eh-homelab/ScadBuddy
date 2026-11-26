import type { Sql, TransactionSql } from 'postgres'
import {
  BLOB_NAME,
  type MadeImages,
  type SessionBlob,
  type SessionImages,
  type StoredSessionBlob,
  type TurnFence,
} from '../../../src/sessions/blobs.js'
import type { BlobMediaType } from '../../../src/sessions/images.js'
import type { ServerEvent } from '../../../src/sessions/protocol.js'
import { forkChain } from './eventRows.js'

// A session's images over SQL on the test database: what the agent ran itself before #5299 step
// 12, and what the API's `session-blobs` routes (tools/go/core/internal/blobs) run. Test-only.

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
  async put(sessionId: string, images: readonly SessionBlob[], toolUseId?: string): Promise<void> {
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
  async putIf(sessionId: string, images: readonly SessionBlob[], fence: TurnFence): Promise<boolean> {
    return this.sql.begin(async (tx) => {
      if (!(await held(tx, sessionId, fence))) return false
      await this.insert(tx, sessionId, images)
      return true
    })
  }

  /** One transaction, so a later write of a name waits for it and sets a time past `at`. */
  async putMade(sessionId: string, images: readonly SessionBlob[]): Promise<MadeImages[]> {
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
   * and the latest time set (tools/go/core/internal/blobs `_insert_blobs`).
   */
  private async insert(tx: TransactionSql, sessionId: string, images: readonly SessionBlob[]): Promise<{ names: string[]; at?: string }> {
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
  async get(sessionId: string, name: string): Promise<StoredSessionBlob | undefined> {
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

async function getThroughChain(sql: Sql | TransactionSql, sessionId: string, name: string): Promise<StoredSessionBlob | undefined> {
  const [row] = await sql<{ media_type: BlobMediaType; data: Buffer }[]>`
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
