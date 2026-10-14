import type { Sql } from 'postgres'
import { MAX_SAVED, type SavedBodies, type SavedMeta } from '../harness/httpRequest.js'

// A session's saved http_request bodies (#2374), in `ai_session_http_bodies`: any
// replica serves them, and a deploy keeps them, for as long as the session and its
// audit rows name them. The newest MAX_SAVED are kept, as on disk before.

type Row = { url: string; content_type: string | null; size_bytes: number; complete: boolean }

export class PgHttpBodies implements SavedBodies {
  private readonly sql: Sql
  private readonly sessionId: string

  constructor(sql: Sql, sessionId: string) {
    this.sql = sql
    this.sessionId = sessionId
  }

  async save(id: string, bytes: Buffer, meta: SavedMeta): Promise<void> {
    await this.sql.begin(async (tx) => {
      await tx`
        INSERT INTO ai_session_http_bodies (session_id, id, url, content_type, size_bytes, complete, data)
        VALUES (${this.sessionId}, ${id}, ${meta.url}, ${meta.content_type}, ${meta.size_bytes}, ${meta.complete}, ${bytes})`
      await tx`
        DELETE FROM ai_session_http_bodies
         WHERE session_id = ${this.sessionId}
           AND id NOT IN (SELECT id FROM ai_session_http_bodies WHERE session_id = ${this.sessionId}
                           ORDER BY created_at DESC LIMIT ${MAX_SAVED})`
    })
  }

  async meta(id: string): Promise<SavedMeta | undefined> {
    const [row] = await this.sql<Row[]>`
      SELECT url, content_type, size_bytes, complete FROM ai_session_http_bodies
       WHERE session_id = ${this.sessionId} AND id = ${id}`
    return row && { url: row.url, content_type: row.content_type, size_bytes: row.size_bytes, complete: row.complete }
  }

  async read(id: string, offset: number, length: number): Promise<Buffer> {
    // substring() is 1-based; past the end it is empty.
    const [row] = await this.sql<{ part: Buffer }[]>`
      SELECT substring(data FROM ${offset + 1} FOR ${length}) AS part FROM ai_session_http_bodies
       WHERE session_id = ${this.sessionId} AND id = ${id}`
    return row ? Buffer.from(row.part) : Buffer.alloc(0)
  }

  async whole(id: string): Promise<{ meta: SavedMeta; bytes: Buffer } | undefined> {
    const [row] = await this.sql<(Row & { data: Buffer })[]>`
      SELECT url, content_type, size_bytes, complete, data FROM ai_session_http_bodies
       WHERE session_id = ${this.sessionId} AND id = ${id}`
    if (!row) return undefined
    return {
      meta: { url: row.url, content_type: row.content_type, size_bytes: row.size_bytes, complete: row.complete },
      bytes: Buffer.from(row.data),
    }
  }
}
