import type { SessionKey, SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'

// The Agent SDK `SessionStore` adapter on Postgres (#300, spec §6 "Storage"):
// the SDK mirrors every transcript line here, so any replica can resume a
// session and transcripts survive restarts. Table `ai_session_entries`
// (db/migrations.ts, entry 2).
//
// The interface, as declared in the pinned SDK
// (node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts, 0.3.283,
// `export declare type SessionStore`, marked @alpha):
//
//   append(key, entries): Promise<void>   required
//   load(key): Promise<SessionStoreEntry[] | null>   required
//   listSessions?(projectKey): Promise<{ sessionId, mtime }[]>
//   listSessionSummaries?(projectKey): Promise<SessionSummaryEntry[]>
//   delete?(key): Promise<void>
//   listSubkeys?({ projectKey, sessionId }): Promise<string[]>
//
// with `SessionKey = { projectKey, sessionId, subpath? }`, where projectKey is
// "Default: sanitized cwd" and subpath is "Undefined = main transcript ... Empty
// string is invalid", and `SessionStoreEntry = { type: string; uuid?: string;
// timestamp?: string; [k: string]: unknown }`, which "Adapters should treat ...
// as pass-through blobs; round-tripping JSON.stringify / JSON.parse is the only
// required invariant". The contract points this adapter follows, quoted from
// the same declarations:
//
//   - append: "Within a single process, persist entries in append-call order"
//     → one INSERT per batch, rows ordered by a bigserial id.
//   - append: "Adapters SHOULD treat `uuid` as an idempotency key (upsert /
//     ignore-duplicate) ... Entries without a `uuid` ... should be appended
//     without dedup" → a partial unique index on (session_id, subpath, uuid)
//     and ON CONFLICT DO NOTHING.
//   - load: "Return `null` for a key that was never written"; "Returned entries
//     must be deep-equal to what was appended" → entries are stored as JSON
//     TEXT, not jsonb, which would reject a "\u0000" in a tool's output.
//   - listSessions: "`mtime` is integer Unix epoch milliseconds".
//   - listSessionSummaries is optional: "when undefined, it falls back to
//     `listSessions()` + per-session `load()`". Not implemented.
//
// ONE DELIBERATE DEPARTURE: lookups (load, listSubkeys, delete) are keyed by
// sessionId and subpath and IGNORE projectKey. The SDK derives projectKey from
// the query's cwd (spec §3.1), and ScadBuddy gives every session its own cwd
// (harness/stateDirs.ts `sessionWorkDir`), so a fork or a resume on another
// replica with another state-dir path would otherwise look in the wrong
// project. Session ids are UUIDs (the SDK's own, or ours via its `sessionId`
// option), so they are unique without the project. Measured in
// test/sessions.e2e.test.ts: a session resumed with a different cwd on a
// fresh CLAUDE_CONFIG_DIR sees its history. projectKey is still recorded per
// row and filters listSessions(projectKey).

type EntryRow = { entry: string }

/** The SDK's key as stored: the main transcript is subpath ''. */
function subpathOf(key: { subpath?: string }): string {
  return key.subpath ?? ''
}

export class PostgresSessionStore implements SessionStore {
  private readonly sql: Sql
  constructor(sql: Sql) {
    this.sql = sql
  }

  async append(key: SessionKey, entries: SessionStoreEntry[]): Promise<void> {
    if (entries.length === 0) return
    const subpath = subpathOf(key)
    const rows = entries.map((entry) => ({
      project_key: key.projectKey,
      session_id: key.sessionId,
      subpath,
      uuid: typeof entry.uuid === 'string' ? entry.uuid : null,
      entry: JSON.stringify(entry),
    }))
    // One statement: rows get ids in array order, so a batch keeps its order.
    await this.sql`
      INSERT INTO ai_session_entries ${this.sql(rows, 'project_key', 'session_id', 'subpath', 'uuid', 'entry')}
      ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`
  }

  async load(key: SessionKey): Promise<SessionStoreEntry[] | null> {
    const rows = await this.sql<EntryRow[]>`
      SELECT entry FROM ai_session_entries
      WHERE session_id = ${key.sessionId} AND subpath = ${subpathOf(key)}
      ORDER BY id`
    if (rows.length === 0) return null
    return rows.map((row) => JSON.parse(row.entry) as SessionStoreEntry)
  }

  async listSessions(projectKey: string): Promise<{ sessionId: string; mtime: number }[]> {
    const rows = await this.sql<{ session_id: string; mtime: string }[]>`
      SELECT session_id, floor(extract(epoch FROM max(created_at)) * 1000)::bigint::text AS mtime
      FROM ai_session_entries
      WHERE project_key = ${projectKey} AND subpath = ''
      GROUP BY session_id`
    return rows.map((row) => ({ sessionId: row.session_id, mtime: Number(row.mtime) }))
  }

  async delete(key: SessionKey): Promise<void> {
    // The main transcript takes its subagent transcripts with it.
    if (key.subpath === undefined) {
      await this.sql`DELETE FROM ai_session_entries WHERE session_id = ${key.sessionId}`
    } else {
      await this.sql`
        DELETE FROM ai_session_entries WHERE session_id = ${key.sessionId} AND subpath = ${key.subpath}`
    }
  }

  async listSubkeys(key: { projectKey: string; sessionId: string }): Promise<string[]> {
    const rows = await this.sql<{ subpath: string }[]>`
      SELECT DISTINCT subpath FROM ai_session_entries
      WHERE session_id = ${key.sessionId} AND subpath <> ''
      ORDER BY subpath`
    return rows.map((row) => row.subpath)
  }

  /** Whether the SDK has written anything for this session (decides `sessionId` vs `resume`). */
  async exists(sessionId: string): Promise<boolean> {
    const rows = await this.sql`
      SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '' LIMIT 1`
    return rows.length > 0
  }
}
