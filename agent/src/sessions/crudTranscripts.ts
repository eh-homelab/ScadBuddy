import type { SessionKey, SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { CrudReadError, crudRead, wellFormed } from '../api/crudRead.js'
import { SubjectForgotten } from '../temporal/payloadCodec.js'
import { sessionPath } from './rows.js'
import type { AppendFence, TranscriptHead, TranscriptRows } from './store.js'

// A session's transcript over the API's CRUD routes (#5299 step 9, part b; backend
// api/crud_agent_transcripts.py, area `transcripts`): `PgTranscriptRows` method for method, each
// one route and one transaction. The API seals and opens every entry with the session's data key
// (#5299 D5): an entry travels as its JSON text, so what comes back is deep-equal to what went in
// (the SDK's one invariant, store.ts). A session id goes into a path only as `sessionPath` makes
// it; a subpath and a project key travel in the query or the body.

/** One append's entries and body at most (backend `ENTRIES_MAX`, `BODY_BYTES_MAX`, less room for the rest of the body). */
export const APPEND_ENTRIES_MAX = 1000
export const APPEND_BYTES_MAX = 30 * 1024 * 1024
/** The uuids `moved` sends at most (backend `OWN_MAX`): past it the tail counts as moved, and the process is spawned again. */
export const OWN_MAX = 20_000
/** A transcript is megabytes: a load and an append get longer than a read. */
const TRANSCRIPT_TIMEOUT_MS = 60_000

const forgotten = (error: unknown): boolean => error instanceof CrudReadError && error.fromRoute && error.status === 410

/** The JSON texts in batches of at most APPEND_ENTRIES_MAX entries and about APPEND_BYTES_MAX bytes, in order. */
export function appendBatches(texts: readonly string[]): string[][] {
  const batches: string[][] = []
  let batch: string[] = []
  let bytes = 0
  for (const text of texts) {
    // As the request's JSON carries it: quoted and escaped, at most six bytes a UTF-16 unit.
    const size = Buffer.byteLength(JSON.stringify(text))
    if (batch.length > 0 && (batch.length >= APPEND_ENTRIES_MAX || bytes + size > APPEND_BYTES_MAX)) {
      batches.push(batch)
      batch = []
      bytes = 0
    }
    batch.push(text)
    bytes += size
  }
  if (batch.length > 0) batches.push(batch)
  return batches
}

export class CrudTranscriptRows implements TranscriptRows {
  readonly #base: string
  readonly #fetch: typeof fetch

  constructor(baseUrl: string, options: { fetch?: typeof fetch } = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
  }

  async #call<T>(step: string, path: string, init: { body?: unknown; timeoutMs?: number } = {}): Promise<T> {
    try {
      return await crudRead<T>(this.#fetch, this.#base, step, path, {
        // Well-formed: the API refuses a lone surrogate, which postgres.js wrote as U+FFFD (crudRead.ts).
        ...(init.body === undefined ? {} : { method: 'POST' as const, body: wellFormed(init.body) }),
        ...(init.timeoutMs === undefined ? {} : { timeoutMs: init.timeoutMs }),
      })
    } catch (error) {
      // As the store's key lookup answers a forgotten session over SQL.
      if (forgotten(error)) throw new SubjectForgotten(`session transcript: ${(error as CrudReadError).detail || 'the session was forgotten'}`)
      throw error
    }
  }

  /** The session's path, or a throw: a write names a session by its UUID. */
  #path(sessionId: string, tail: string): string {
    const path = sessionPath(sessionId)
    if (path === undefined) throw new Error(`session ${JSON.stringify(sessionId)} is not a session id`)
    return `${path}${tail}`
  }

  /**
   * One request per batch (`appendBatches`), each its own transaction under the fence, so a batch
   * past the body's size is written as a prefix of the entries when the fence turns between two:
   * as a placement cut off between two SDK messages would leave it. Stops at the first refused.
   */
  async append(key: SessionKey, entries: SessionStoreEntry[], fence?: AppendFence): Promise<boolean> {
    const path = this.#path(key.sessionId, '/entries')
    const texts = entries.map((entry) => JSON.stringify(entry))
    for (const batch of appendBatches(texts)) {
      const { written } = await this.#call<{ written: boolean }>(`the transcript of session ${key.sessionId}`, path, {
        body: {
          project_key: key.projectKey,
          subpath: key.subpath ?? '',
          entries: batch,
          ...(fence ? { fence: { attempt: fence.attempt, turn_id: fence.turnId } } : {}),
        },
        timeoutMs: TRANSCRIPT_TIMEOUT_MS,
      })
      if (!written) return false
    }
    return true
  }

  async transcript(sessionId: string): Promise<SessionStoreEntry[]> {
    const path = sessionPath(sessionId)
    if (path === undefined) return []
    const { entries } = await this.#call<{ entries: string[] | null }>(`the transcript of session ${sessionId}`, `${path}/transcript`, {
      timeoutMs: TRANSCRIPT_TIMEOUT_MS,
    })
    return (entries ?? []).map((text) => JSON.parse(text) as SessionStoreEntry)
  }

  async subpath(sessionId: string, subpath: string): Promise<SessionStoreEntry[]> {
    const path = sessionPath(sessionId)
    if (path === undefined || subpath === '') return []
    const query = new URLSearchParams({ subpath })
    const { entries } = await this.#call<{ entries: string[] | null }>(
      `the transcript ${subpath} of session ${sessionId}`,
      `${path}/entries?${query.toString()}`,
      { timeoutMs: TRANSCRIPT_TIMEOUT_MS },
    )
    return (entries ?? []).map((text) => JSON.parse(text) as SessionStoreEntry)
  }

  async lastOfType(sessionId: string, subpath: string, type: string): Promise<SessionStoreEntry | null> {
    const path = sessionPath(sessionId)
    if (path === undefined) return null
    const query = new URLSearchParams({ type, subpath })
    const { entry } = await this.#call<{ entry: string | null }>(`the last ${type} of session ${sessionId}`, `${path}/entries/last?${query.toString()}`)
    return entry === null ? null : (JSON.parse(entry) as SessionStoreEntry)
  }

  async lastCwd(sessionId: string): Promise<string | undefined> {
    const path = sessionPath(sessionId)
    if (path === undefined) return undefined
    const { cwd } = await this.#call<{ cwd: string | null }>(`the working directory of session ${sessionId}`, `${path}/cwd`, {
      timeoutMs: TRANSCRIPT_TIMEOUT_MS,
    })
    return cwd ?? undefined
  }

  async listSessions(projectKey: string): Promise<{ sessionId: string; mtime: number }[]> {
    const query = new URLSearchParams({ project_key: projectKey })
    const { sessions } = await this.#call<{ sessions: { session_id: string; mtime: number }[] }>('the transcripts of a project', `/transcripts?${query.toString()}`)
    return sessions.map((s) => ({ sessionId: s.session_id, mtime: s.mtime }))
  }

  async delete(sessionId: string, subpath?: string): Promise<void> {
    await this.#call(`the transcript of session ${sessionId}`, this.#path(sessionId, '/entries/delete'), { body: { subpath: subpath ?? null } })
  }

  async listSubkeys(sessionId: string): Promise<string[]> {
    const path = sessionPath(sessionId)
    if (path === undefined) return []
    return (await this.#call<{ subkeys: string[] }>(`the subagent transcripts of session ${sessionId}`, `${path}/subkeys`)).subkeys
  }

  async head(sessionId: string): Promise<TranscriptHead> {
    const path = sessionPath(sessionId)
    if (path === undefined) return { exists: false, since: null }
    return this.#call<TranscriptHead>(`the transcript head of session ${sessionId}`, `${path}/transcript/head`)
  }

  async moved(sessionId: string, since: string | null, own: readonly string[]): Promise<boolean> {
    // More than the route takes: the process is spawned again rather than its tail read in part.
    if (own.length > OWN_MAX) return true
    const path = this.#path(sessionId, '/transcript/moved')
    return (await this.#call<{ moved: boolean }>(`the transcript tail of session ${sessionId}`, path, { body: { since, own } })).moved
  }

  async userLanded(sessionId: string, mark: number): Promise<boolean> {
    const path = sessionPath(sessionId)
    if (path === undefined) return false
    const query = new URLSearchParams({ after: String(mark) })
    return (await this.#call<{ landed: boolean }>(`the transcript of session ${sessionId}`, `${path}/entries/landed?${query.toString()}`)).landed
  }
}
