import type { SessionKey, SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import type { Sql, TransactionSql } from 'postgres'
import { isUuid } from '../harness/uuid.js'
import { openBytes, sealBytes } from '../secrets.js'
import { type PayloadKeys, payloadKeyLock, SubjectForgotten } from '../temporal/payloadCodec.js'

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
// another's: `ai_session_entries:<session_id>:<subpath>:<uuid ?? #id>`. A row
// without a uuid is bound by its own id behind a '#', which append reserves first with
// nextval (an id is never written twice); the '#' keeps a CLI uuid of "5" from
// colliding with row id 5. Rows written before sealing still hold
// plaintext `entry` and are read as they are, until the backfill.

// A fork is copy-on-write (plan 2026-10-13 Step 6.5, decision Q8, #4212): its
// `ai_sessions` row names the session it was forked from (`fork_parent_id`) and how
// many entries of that session's main transcript it takes (`fork_cut`). `load` of
// the child's main transcript is the parent's as the parent loads it (so through the
// parent's own cut, for a fork of a fork), cut there and opened under each owner's
// key, then the child's own rows. Nothing is copied at the fork. Forgetting a parent
// copies each child's range into the child first (`copyForkRanges`, forget.ts): those
// rows are `inherited`, read before the child's own, and the link is cleared. The
// parent's entries are passed on as they are, with the parent's `sessionId`: Claude
// Code restores a session-scoped entry (a `cost-state`) only of the session it
// resumes (warm.ts `restoredCostUsd`, measured in test/warmResumeCost.sdk.test.ts), so
// the child does not take the parent's spend as its own.

/** A condition an append is written under, checked in its transaction (`appendIf`). */
export type AppendFence = (tx: TransactionSql) => Promise<boolean>

// `entry_type` is plaintext, so only a short token-like type is stored; anything else is NULL, never content.
const ENTRY_TYPE = /^[a-z][a-z0-9_-]{0,63}$/

type EntryRow = { id: string; uuid: string | null; entry: string | null; sealed: Buffer | null }

/** A run of one session's rows in a transcript as it loads, and whose they are (their key and AAD). */
type Segment = { owner: string; rows: EntryRow[] }

/** At most this many rows in one INSERT of `copyForkRanges` (8 parameters each, under the protocol's 65535). */
const COPY_BATCH = 1000

export const entryTypeOf = (entry: { type?: unknown }): string | null =>
  typeof entry.type === 'string' && ENTRY_TYPE.test(entry.type) ? entry.type : null

export const entryContext = (sessionId: string, subpath: string, bound: string): string =>
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
    await this.appendIf(key, entries)
  }

  /**
   * `append`, only while `fence` holds, checked in the transaction that inserts the rows (an
   * AgentSession placement that still holds its turn, sessions/turnHost.ts). Whether it was written.
   */
  async appendIf(key: SessionKey, entries: SessionStoreEntry[], fence?: AppendFence): Promise<boolean> {
    if (entries.length === 0) return true
    const turn = (this.appending.get(key.sessionId) ?? Promise.resolve()).then(() => this.appendNow(key, entries, fence))
    const tail = turn.catch(() => {})
    this.appending.set(key.sessionId, tail)
    try {
      return await turn
    } finally {
      if (this.appending.get(key.sessionId) === tail) this.appending.delete(key.sessionId)
    }
  }

  private async appendNow(key: SessionKey, entries: SessionStoreEntry[], fence?: AppendFence): Promise<boolean> {
    const subpath = subpathOf(key)
    const dek = await this.keyOf(key.sessionId, true)
    try {
      // Every row's id is reserved first, in order, so a batch keeps its order and a row
      // without a uuid can be bound by its own id. Outside the transaction below on purpose:
      // a refused append only skips sequence values, which nothing relies on being dense.
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
          entry_type: entryTypeOf(entry),
          sealed: sealBytes(dek, json, entryContext(key.sessionId, subpath, uuid ?? `#${id}`)),
        }
      })
      // The key may come from a cache that outlives a forget on another replica (#3316).
      // forgetSubject tombstones under the subject's lock and deletes the entries later,
      // so a write under that lock, shared, lands either before the tombstone (and goes
      // with the entries) or after it, and is refused as a fresh key lookup would be.
      // The tombstone is its own SELECT, not a NOT EXISTS on the insert: ON CONFLICT
      // also inserts no row for a repeated uuid, so a zero count could not tell the two apart.
      // The fence, when given, is checked in the same transaction, after the lock.
      const subject = `session-${key.sessionId}`
      return await this.sql.begin(async (tx) => {
        await tx`SELECT pg_advisory_xact_lock_shared(hashtext(${payloadKeyLock(subject)}))`
        const [forgotten] = await tx`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`
        if (forgotten) throw new SubjectForgotten(`${subject} was forgotten: its transcript is not written`)
        if (fence && !(await fence(tx))) return false
        await tx`
          INSERT INTO ai_session_entries ${tx(rows, 'id', 'project_key', 'session_id', 'subpath', 'uuid', 'entry_type', 'sealed')}
          ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`
        return true
      })
    } finally {
      dek.fill(0)
    }
  }

  async load(key: SessionKey): Promise<SessionStoreEntry[] | null> {
    const subpath = subpathOf(key)
    if (subpath === '') {
      const entries = await transcriptOf(this.sql, (sessionId, create) => this.keyOf(sessionId, create), key.sessionId)
      return entries.length === 0 ? null : entries
    }
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
      WHERE session_id = ${key.sessionId} AND subpath = ${subpath} AND uuid IS NULL AND NOT inherited
        AND (entry_type = ${type} OR (entry_type IS NULL AND entry LIKE ${`%"${type}"%`}))
      ORDER BY ai_session_entries.id DESC LIMIT 1`
    return rows.length === 0 ? null : (await this.opened(key.sessionId, subpath, rows))[0]!
  }

  private async opened(sessionId: string, subpath: string, rows: EntryRow[]): Promise<SessionStoreEntry[]> {
    return openRows((id, create) => this.keyOf(id, create), sessionId, subpath, rows)
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

  /** Whether this session has a transcript to resume (decides `sessionId` vs `resume`): rows of its own, or a fork's parent. */
  async exists(sessionId: string): Promise<boolean> {
    return transcriptExists(this.sql, sessionId)
  }
}

type KeyOf = (sessionId: string, create: boolean) => Promise<Buffer>

/** Whether `sessionId` has a main transcript to resume: rows of its own, or the parent a fork reads through. */
export async function transcriptExists(sql: Sql, sessionId: string): Promise<boolean> {
  const [row] = isUuid(sessionId)
    ? await sql<{ found: boolean }[]>`
        SELECT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '')
          OR EXISTS (SELECT 1 FROM ai_sessions WHERE id = ${sessionId}::uuid AND fork_parent_id IS NOT NULL) AS found`
    : await sql<{ found: boolean }[]>`
        SELECT EXISTS (SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '') AS found`
  return row?.found === true
}

async function openRows(keyOf: KeyOf, sessionId: string, subpath: string, rows: EntryRow[]): Promise<SessionStoreEntry[]> {
  // SubjectForgotten when the session's key is gone; no key is made to read.
  const dek = rows.some((row) => row.sealed) ? await keyOf(sessionId, false) : undefined
  try {
    return rows.map((row) => {
      if (!row.sealed) return JSON.parse(row.entry!) as SessionStoreEntry
      const json = openBytes(dek!, row.sealed, entryContext(sessionId, subpath, row.uuid ?? `#${row.id}`))
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

/** The first `n` rows of a transcript's segments. */
function firstRows(segments: Segment[], n: number): Segment[] {
  const out: Segment[] = []
  let left = n
  for (const segment of segments) {
    if (left <= 0) break
    const rows = segment.rows.slice(0, left)
    left -= rows.length
    out.push({ owner: segment.owner, rows })
  }
  return out
}

/**
 * The rows of `sessionId`'s main transcript, in the order it loads: its inherited rows (a
 * forgotten parent's range, copied in), then its parent's transcript through the cut, then
 * its own. `sql` is one snapshot, so a copy committed meanwhile is read either as the link or
 * as the rows, never both.
 */
async function segmentsOf(sql: TransactionSql, sessionId: string): Promise<Segment[]> {
  const [link] = isUuid(sessionId)
    ? await sql<{ parent: string | null; cut: number | null }[]>`
        SELECT fork_parent_id::text AS parent, fork_cut AS cut FROM ai_sessions WHERE id = ${sessionId}::uuid`
    : []
  const rows = await sql<(EntryRow & { inherited: boolean })[]>`
    SELECT id::text AS id, uuid, entry, sealed, inherited FROM ai_session_entries
    WHERE session_id = ${sessionId} AND subpath = ''
    ORDER BY inherited DESC, ai_session_entries.id`
  const inherited = rows.filter((row) => row.inherited)
  const own = rows.filter((row) => !row.inherited)
  const through = link?.parent ? firstRows(await segmentsOf(sql, link.parent), link.cut ?? 0) : []
  return [{ owner: sessionId, rows: inherited }, ...through, { owner: sessionId, rows: own }].filter((s) => s.rows.length > 0)
}

async function openSegments(keyOf: KeyOf, segments: Segment[]): Promise<SessionStoreEntry[]> {
  const entries: SessionStoreEntry[] = []
  for (const segment of segments) entries.push(...(await openRows(keyOf, segment.owner, '', segment.rows)))
  return entries
}

/** How often a load is read again when an ancestor was forgotten while it read (its range is then the child's own). */
const TRANSCRIPT_TRIES = 3

/** `sessionId`'s main transcript as it loads (`segmentsOf`), each run opened under its owner's key. */
async function transcriptOf(sql: Sql, keyOf: KeyOf, sessionId: string): Promise<SessionStoreEntry[]> {
  for (let attempt = 1; ; attempt++) {
    const segments = await sql.begin('isolation level repeatable read read only', (tx) => segmentsOf(tx, sessionId))
    try {
      return await openSegments(keyOf, segments)
    } catch (err) {
      // An ancestor whose key went after the snapshot: forgetSubject copied the range into the
      // child in that same transaction, so the next read has it as the child's own.
      const ancestorGone = err instanceof SubjectForgotten && segments.some((s) => s.owner !== sessionId)
      if (!ancestorGone || attempt >= TRANSCRIPT_TRIES) throw err
    }
  }
}

/** A session with forks that read through it, forgotten without the data keys that copy its transcript into them. */
export class ForksNeedKeys extends Error {
  constructor(parentId: string) {
    super(`session ${parentId} has forks that read its transcript; forgetting it needs SCADBUDDY_SECRET_KEY_FILE to copy it into them first`)
    this.name = 'ForksNeedKeys'
  }
}

/** Whether a fork reads its transcript through `parentId`. */
export async function hasForks(sql: Sql, parentId: string): Promise<boolean> {
  const rows = await sql`SELECT 1 FROM ai_sessions WHERE fork_parent_id = ${parentId}::uuid LIMIT 1`
  return rows.length > 0
}

/**
 * Before `parentId`'s key goes (forgetSubject, in its transaction, under the parent's lock): each
 * fork that reads through it takes the range it references as rows of its own, `inherited` and
 * sealed under its own key, and its link is cleared, so it needs neither the parent's rows nor
 * its key again. A fork being forgotten itself gets no rows, only its link cleared. `keys` opens
 * the parent's transcript (its own ancestors' too) and seals the copies; without them a parent
 * with forks is not forgotten. How many forks there were.
 */
export async function copyForkRanges(tx: TransactionSql, keys: PayloadKeys | undefined, parentId: string): Promise<number> {
  const children = await tx<{ id: string; cut: number }[]>`
    SELECT id::text AS id, fork_cut AS cut FROM ai_sessions WHERE fork_parent_id = ${parentId}::uuid ORDER BY id FOR UPDATE`
  if (children.length === 0) return 0
  if (!keys) throw new ForksNeedKeys(parentId)
  const keyOf: KeyOf = (sessionId, create) => keys.keyFor(`session-${sessionId}`, create)
  const transcript = await openSegments(keyOf, await segmentsOf(tx, parentId))
  for (const child of children) {
    const range = transcript.slice(0, child.cut)
    const subject = `session-${child.id}`
    let dek: Buffer | undefined
    try {
      // Before its lock: making the key takes that lock itself, on another connection.
      if (range.length > 0) dek = await keyOf(child.id, true)
    } catch (err) {
      if (!(err instanceof SubjectForgotten)) throw err
    }
    try {
      // As an append does (appendNow): a forget of the child either went first, and its tombstone
      // is seen here, or waits for this to commit and then deletes these rows with the rest.
      await tx`SELECT pg_advisory_xact_lock_shared(hashtext(${payloadKeyLock(subject)}))`
      const [forgotten] = await tx`SELECT 1 FROM ai_forgotten_subjects WHERE subject = ${subject}`
      if (dek && !forgotten) {
        for (let at = 0; at < range.length; at += COPY_BATCH) {
          const batch = range.slice(at, at + COPY_BATCH)
          const ids = await tx<{ id: string }[]>`
            SELECT nextval(pg_get_serial_sequence('ai_session_entries', 'id'))::text AS id FROM generate_series(1, ${batch.length})`
          const rows = batch.map((entry, i) => {
            const uuid = typeof entry.uuid === 'string' ? entry.uuid : null
            const id = ids[i]!.id
            const json = Buffer.from(JSON.stringify(entry), 'utf8')
            return {
              id,
              project_key: '',
              session_id: child.id,
              subpath: '',
              uuid,
              entry_type: entryTypeOf(entry),
              sealed: sealBytes(dek, json, entryContext(child.id, '', uuid ?? `#${id}`)),
              inherited: true,
            }
          })
          await tx`
            INSERT INTO ai_session_entries ${tx(rows, 'id', 'project_key', 'session_id', 'subpath', 'uuid', 'entry_type', 'sealed', 'inherited')}
            ON CONFLICT (session_id, subpath, uuid) WHERE uuid IS NOT NULL DO NOTHING`
        }
      }
      await tx`UPDATE ai_sessions SET fork_parent_id = NULL, fork_cut = NULL WHERE id = ${child.id}::uuid`
    } finally {
      dek?.fill(0)
    }
  }
  return children.length
}
