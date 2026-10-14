import type { SessionKey, SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Sql } from 'postgres'
import { openBytes, sealBytes } from '../secrets.js'
import type { PayloadKeys } from '../temporal/payloadCodec.js'

// The Agent SDK `SessionStore` adapter on Postgres (#300, spec §6 "Storage"):
// the SDK mirrors every transcript line here, so any replica can resume a
// session and transcripts survive restarts. Table `ai_session_entries`
// (db/migrations/20260928T0107Z_sessions.sql).
//
// The interface, as declared in the pinned SDK
// (node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts, 0.3.283, and the same
// in 0.3.287, `export declare type SessionStore`, marked @alpha):
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

// Entries are sealed under the session's data key (`ai_payload_keys`, subject
// `session-<id>`, unified agent runtime D7): `sealed` holds the JSON, `entry` is
// NULL, and the AAD binds the bytes to their row, so a row cannot be swapped for
// another's: `ai_session_entries:<session_id>:<subpath>:<uuid ?? id>`. A row
// without a uuid is bound by its own id, which append reserves first with
// nextval (an id is never written twice). Rows written before sealing still hold
// plaintext `entry` and are read as they are, until the backfill.

type EntryRow = { id: string; uuid: string | null; entry: string | null; sealed: Buffer | null }

const entryContext = (sessionId: string, subpath: string, bound: string): string =>
  `ai_session_entries:${sessionId}:${subpath}:${bound}`

/** The SDK's key as stored: the main transcript is subpath ''. */
function subpathOf(key: { subpath?: string }): string {
  return key.subpath ?? ''
}

export class PostgresSessionStore implements SessionStore {
  private readonly sql: Sql
  private readonly keys: PayloadKeys | undefined
  /** `keys` is the data keys of sessions; without them the store reads legacy plaintext but never appends. */
  constructor(sql: Sql, options: { keys?: PayloadKeys } = {}) {
    this.sql = sql
    this.keys = options.keys
  }

  private async keyOf(sessionId: string, create: boolean): Promise<Buffer> {
    if (!this.keys) {
      throw new Error('the session transcript is sealed and needs SCADBUDDY_SECRET_KEY_FILE; it is not set, so nothing is written')
    }
    return this.keys.keyFor(`session-${sessionId}`, create)
  }

  /** The tail of each session's appends: the contract keeps append-call order, and a call's key lookup and id reservation are not one statement. */
  private readonly appending = new Map<string, Promise<unknown>>()

  async append(key: SessionKey, entries: SessionStoreEntry[]): Promise<void> {
    if (entries.length === 0) return
    const turn = (this.appending.get(key.sessionId) ?? Promise.resolve()).then(() => this.appendNow(key, entries))
    const tail = turn.catch(() => {})
    this.appending.set(key.sessionId, tail)
    try {
      await turn
    } finally {
      if (this.appending.get(key.sessionId) === tail) this.appending.delete(key.sessionId)
    }
  }

  private async appendNow(key: SessionKey, entries: SessionStoreEntry[]): Promise<void> {
    const subpath = subpathOf(key)
    const dek = await this.keyOf(key.sessionId, true)
    try {
      // Every row's id is reserved first, in order, so a batch keeps its order and a row
      // without a uuid can be bound by its own id.
      const ids = await this.sql<{ id: string }[]>`
        SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id'))::text AS id FROM generate_series(1, ${entries.length})`
      const rows = entries.map((entry, i) => {
        const uuid = typeof entry.uuid === 'string' ? entry.uuid : null
        const id = ids[i]!.id
        const json = Buffer.from(JSON.stringify(entry), 'utf8')
        return {
          id,
          project_key: key.projectKey,
          session_id: key.sessionId,
          subpath,
          uuid,
          entry_type: typeof entry.type === 'string' ? entry.type : null,
          sealed: sealBytes(dek, json, entryContext(key.sessionId, subpath, uuid ?? id)),
        }
      })
      await this.sql`
        INSERT INTO ai_session_entries ${this.sql(rows, 'id', 'project_key', 'session_id', 'subpath', 'uuid', 'entry_type', 'sealed')}
        ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`
    } finally {
      dek.fill(0)
    }
  }

  async load(key: SessionKey): Promise<SessionStoreEntry[] | null> {
    const subpath = subpathOf(key)
    const rows = await this.sql<EntryRow[]>`
      SELECT id::text AS id, uuid, entry, sealed FROM ai_session_entries
      WHERE session_id = ${key.sessionId} AND subpath = ${subpath}
      ORDER BY ai_session_entries.id`
    if (rows.length === 0) return null
    return this.opened(key.sessionId, subpath, rows)
  }

  /**
   * The newest row without a uuid of this `type` in the key's transcript, or null. Found by the
   * plaintext `entry_type`; a row from before sealing has none, so its text is matched instead.
   */
  async lastOfType(key: SessionKey, type: string): Promise<SessionStoreEntry | null> {
    const subpath = subpathOf(key)
    const rows = await this.sql<EntryRow[]>`
      SELECT id::text AS id, uuid, entry, sealed FROM ai_session_entries
      WHERE session_id = ${key.sessionId} AND subpath = ${subpath} AND uuid IS NULL
        AND (entry_type = ${type} OR (entry_type IS NULL AND entry LIKE ${`%"${type}"%`}))
      ORDER BY ai_session_entries.id DESC LIMIT 1`
    return rows.length === 0 ? null : (await this.opened(key.sessionId, subpath, rows))[0]!
  }

  private async opened(sessionId: string, subpath: string, rows: EntryRow[]): Promise<SessionStoreEntry[]> {
    // SubjectForgotten when the session's key is gone; no key is made to read.
    const dek = rows.some((row) => row.sealed) ? await this.keyOf(sessionId, false) : undefined
    try {
      return rows.map((row) => {
        if (!row.sealed) return JSON.parse(row.entry!) as SessionStoreEntry
        const json = openBytes(dek!, row.sealed, entryContext(sessionId, subpath, row.uuid ?? row.id))
        try {
          return JSON.parse(json.toString('utf8')) as SessionStoreEntry
        } finally {
          json.fill(0)
        }
      })
    } finally {
      dek?.fill(0)
    }
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
