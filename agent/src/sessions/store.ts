import type { SessionKey, SessionStore, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'

// The Agent SDK `SessionStore` adapter (#300, spec §6 "Storage"): the SDK mirrors every
// transcript line here, so any replica can resume a session and transcripts survive restarts.
// The rows are the API's (`TranscriptRows`, crudTranscripts.ts; table `ai_session_entries`,
// backend/scadbuddy/migrations_agent/20260928T0107Z_sessions.sql).
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
// copies each child's range into the child first (on the API, forget.ts): those
// rows are `inherited`, read before the child's own, and the link is cleared. Its events
// and the images they name are copied in then too, since the child reads those through the
// same link. The
// parent's entries are passed on as they are, with the parent's `sessionId`: Claude
// Code restores a session-scoped entry (a `cost-state`) only of the session it
// resumes (warm.ts `restoredCostUsd`, measured in test/warmResumeCost.sdk.test.ts), so
// the child does not take the parent's spend as its own.

/**
 * The condition a turn placement's appends are written under (#5299 step 9, part b): the
 * session's row still names this placement's `attempt` and either its turn or no turn at all
 * (turnHost.ts `holdsOrLast`), checked `FOR SHARE` in the append's own transaction on the API.
 */
export type AppendFence = { attempt: string; turnId: string }

// `entry_type` is plaintext, so only a short token-like type is stored; anything else is NULL, never content.

/** The SDK's key as stored: the main transcript is subpath ''. */
function subpathOf(key: { subpath?: string }): string {
  return key.subpath ?? ''
}

/** Whether a main transcript is there to resume, and its own newest row with a uuid (warm.ts). */
export type TranscriptHead = { exists: boolean; since: string | null }

/**
 * Where a session's transcript rows are kept (#5299 step 9, part b): `CrudTranscriptRows`
 * (crudTranscripts.ts), the API's routes (area `transcripts`, backend
 * tools/go/core/internal/transcripts), which seal and open each entry there. Each method is one
 * transaction.
 */
export interface TranscriptRows {
  /** The entries in one transaction, only while `fence` holds; whether they were written. SubjectForgotten once forgotten. */
  append(key: SessionKey, entries: SessionStoreEntry[], fence?: AppendFence): Promise<boolean>
  /** The main transcript as it loads (a fork's through its chain, one snapshot); empty for none. */
  transcript(sessionId: string): Promise<SessionStoreEntry[]>
  /** A subagent transcript's entries; empty for none. */
  subpath(sessionId: string, subpath: string): Promise<SessionStoreEntry[]>
  /** The newest row without a uuid of this `type` in the key's transcript, or null. */
  lastOfType(sessionId: string, subpath: string, type: string): Promise<SessionStoreEntry | null>
  /** The working directory the main transcript's newest entry ran in. */
  lastCwd(sessionId: string): Promise<string | undefined>
  listSessions(projectKey: string): Promise<{ sessionId: string; mtime: number }[]>
  /** Without `subpath`, the main transcript and its subagent transcripts. */
  delete(sessionId: string, subpath?: string): Promise<void>
  listSubkeys(sessionId: string): Promise<string[]>
  head(sessionId: string): Promise<TranscriptHead>
  /** Whether a row with a uuid not in `own` landed in the main transcript after row `since` (warm.ts `tailMoved`). */
  moved(sessionId: string, since: string | null, own: readonly string[]): Promise<boolean>
  /** Whether a user entry landed in the main transcript after row `mark` (turnHost.ts). */
  userLanded(sessionId: string, mark: number): Promise<boolean>
}

export class TranscriptStore implements SessionStore {
  private readonly rows: TranscriptRows
  /** `rows` is where the rows are kept. */
  constructor(rows: TranscriptRows) {
    this.rows = rows
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
    const turn = (this.appending.get(key.sessionId) ?? Promise.resolve()).then(() => this.rows.append(key, entries, fence))
    const tail = turn.catch(() => {})
    this.appending.set(key.sessionId, tail)
    try {
      return await turn
    } finally {
      if (this.appending.get(key.sessionId) === tail) this.appending.delete(key.sessionId)
    }
  }

  async load(key: SessionKey): Promise<SessionStoreEntry[] | null> {
    const subpath = subpathOf(key)
    const entries = subpath === '' ? await this.rows.transcript(key.sessionId) : await this.rows.subpath(key.sessionId, subpath)
    return entries.length === 0 ? null : entries
  }

  /**
   * The newest row without a uuid of this `type` in the key's transcript, or null. Found by the
   * plaintext `entry_type`; a row from before sealing has none, so its text is matched instead.
   */
  lastOfType(key: SessionKey, type: string): Promise<SessionStoreEntry | null> {
    return this.rows.lastOfType(key.sessionId, subpathOf(key), type)
  }

  /**
   * The working directory the main transcript's newest entry ran in (Claude Code writes its
   * `cwd` on each user and assistant entry), or undefined when no entry names one (#5867).
   * The session's own newest rows first; a session with none of its own yet (a fork before its
   * first turn) reads its transcript as it loads, through its parent's cut.
   */
  lastCwd(sessionId: string): Promise<string | undefined> {
    return this.rows.lastCwd(sessionId)
  }

  listSessions(projectKey: string): Promise<{ sessionId: string; mtime: number }[]> {
    return this.rows.listSessions(projectKey)
  }

  async delete(key: SessionKey): Promise<void> {
    // The main transcript takes its subagent transcripts with it.
    await this.rows.delete(key.sessionId, key.subpath)
  }

  listSubkeys(key: { projectKey: string; sessionId: string }): Promise<string[]> {
    return this.rows.listSubkeys(key.sessionId)
  }

  /** Whether this session has a transcript to resume (decides `sessionId` vs `resume`): rows of its own, or a fork's parent. */
  async exists(sessionId: string): Promise<boolean> {
    return (await this.rows.head(sessionId)).exists
  }

  /** Whether a transcript is there to resume, and its own newest row with a uuid (warm.ts `spawn`). */
  head(sessionId: string): Promise<TranscriptHead> {
    return this.rows.head(sessionId)
  }

  /** Whether another writer's row landed in the main transcript after `since` (warm.ts `tailMoved`). */
  moved(sessionId: string, since: string | null, own: readonly string[]): Promise<boolean> {
    return this.rows.moved(sessionId, since, own)
  }

  /** Whether a user entry landed in the main transcript after row `mark` (turnHost.ts). */
  userLanded(sessionId: string, mark: number): Promise<boolean> {
    return this.rows.userLanded(sessionId, mark)
  }
}

/** A session with forks that read through it, forgotten without the data keys that copy its transcript into them. */
export class ForksNeedKeys extends Error {
  /**
   * `begun`: what the forget had already done when the API refused it: its background tasks
   * cancelled and, with a Temporal client (`terminated`), their workflows and its open tool
   * calls terminated.
   */
  constructor(parentId: string, begun?: { terminated: boolean }) {
    const done = begun?.terminated
      ? 'its background tasks were cancelled and their workflows and its open tool calls terminated'
      : 'its background tasks were cancelled (no Temporal client, so no workflow or tool call was terminated)'
    super(
      begun
        ? `session ${parentId} has forks that read its transcript, and the API has no SCADBUDDY_SECRET_KEY_FILE to copy it into them; ${done}, but its key, transcript and rows remain. Run the forget again once the API has the key`
        : `session ${parentId} has forks that read its transcript; forgetting it needs SCADBUDDY_SECRET_KEY_FILE to copy it into them first`,
    )
    this.name = 'ForksNeedKeys'
  }
}

