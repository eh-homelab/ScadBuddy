import { createHash } from 'node:crypto'
import type { SettingsReader } from '../approvals/service.js'
import { CrudReadError, crudRead, crudResponse } from '../api/crudRead.js'
import type { ServerEvent } from '../sessions/protocol.js'
import {
  type DeleteOptions,
  guessMediaType,
  limitsOf,
  RECONCILE_GRACE_MS,
  type Reconciled,
  sha256,
  type SessionFiles,
  startWorkspaceSweeper,
  STREAM_CHUNK_BYTES,
  type SweeperOptions,
  WORKSPACE_MAX_FILES,
  WORKSPACE_TTL_MS,
  type WorkspaceBytesStatus,
  WorkspaceError,
  type WorkspaceErrorCode,
  type WorkspaceFile,
  type WorkspaceFileInfo,
  type WorkspaceFiles,
  type WorkspaceLimits,
  workspacePath,
  type WorkspaceRef,
  type WorkspaceSource,
  type WriteOptions,
} from './store.js'

// The workspace files through the API's CRUD routes (#5299 step 7, area `workspace`; backend
// api/crud_agent_workspace.py), with the interface `SessionFiles` has over SQL (store.ts). The
// API seals and opens every file with its own KEK and the session's payload key (D5): this
// process sends and gets plaintext, and holds no file key, KEK or blob URL. Bytes travel as
// bytes (multipart for a write, a stream for a read), never base64 (D8). A workspace and a path
// go in the query or the body, never in the route's path (a principal's id may hold a `/`), and
// a path is checked here (`workspacePath`) before it is sent and again by the API.
//
// A refusal the API gives with a `reason` that is a WorkspaceErrorCode is that WorkspaceError,
// its detail as the message. A read hashes what streamed: a file that changed while it streamed
// ends the API's stream short, which is a `conflict`, as a change between two SQL slices was.

/** How long a read or a write of a file's bytes may take. */
export const FILE_TIMEOUT_MS = 120_000
/** The most sessions one `session-files` read names (backend `SESSIONS_MAX`). */
export const SESSIONS_PAGE = 500
/** How often `read` asks again when the file changes between its stat and its bytes. */
const READ_TRIES = 3

const CODES = new Set<string>(['invalid_path', 'not_found', 'conflict', 'too_large', 'quota', 'unavailable'])
const SHA256_HEADER = 'x-scadbuddy-sha256'

type FileJson = {
  path: string
  media_type: string
  size: number
  sha256: string
  source: WorkspaceSource | null
  created_at: string
  updated_at: string
}

function infoOf(row: FileJson): WorkspaceFileInfo {
  return {
    path: row.path,
    mediaType: row.media_type,
    size: row.size,
    sha256: row.sha256,
    source: row.source,
    createdAt: new Date(row.created_at),
    updatedAt: new Date(row.updated_at),
  }
}

function keyOf(ws: WorkspaceRef): string {
  return ws.kind === 'session' ? `session:${ws.sessionId}` : `principal:${ws.principalKind}:${ws.principalId}`
}

function query(params: Record<string, string | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== undefined) q.set(k, v)
  return `?${q}`
}

/** The API's refusal as the WorkspaceError it names, else the error as it was. */
function refusalOf(err: unknown): unknown {
  if (err instanceof CrudReadError && err.fromRoute && err.reason && CODES.has(err.reason)) {
    return new WorkspaceError(err.reason as WorkspaceErrorCode, err.detail || err.message)
  }
  return err
}

export type CrudSessionFilesOptions = {
  fetch?: typeof fetch
  /** The caps' settings (the agent's settings reader); the API checks them again. */
  settings?: SettingsReader | undefined
  limits?: Partial<WorkspaceLimits>
}

/** The workspace files, kept and sealed by the API (above). */
export class CrudSessionFiles implements WorkspaceFiles {
  readonly #base: string
  readonly #fetch: typeof fetch
  readonly #settings: SettingsReader | undefined
  readonly #fixed: Partial<WorkspaceLimits>
  readonly maxFiles = WORKSPACE_MAX_FILES

  constructor(baseUrl: string, options: CrudSessionFilesOptions = {}) {
    this.#base = baseUrl
    this.#fetch = options.fetch ?? globalThis.fetch
    this.#settings = options.settings
    this.#fixed = options.limits ?? {}
  }

  async #json<T>(step: string, path: string, body?: unknown, timeoutMs?: number): Promise<T> {
    try {
      return await crudRead<T>(this.#fetch, this.#base, step, path, {
        ...(body === undefined ? {} : { method: 'POST' as const, body }),
        ...(timeoutMs === undefined ? {} : { timeoutMs }),
      })
    } catch (err) {
      throw refusalOf(err)
    }
  }

  async #response(step: string, path: string, init: Parameters<typeof crudResponse>[4] = {}): Promise<Response> {
    try {
      return await crudResponse(this.#fetch, this.#base, step, path, { timeoutMs: FILE_TIMEOUT_MS, ...init })
    } catch (err) {
      throw refusalOf(err)
    }
  }

  limits(): Promise<WorkspaceLimits> {
    return limitsOf(this.#fixed, this.#settings)
  }

  async fileCount(ws: WorkspaceRef): Promise<number> {
    return (await this.usage(ws)).files
  }

  async #status(): Promise<{ store: WorkspaceBytesStatus['store']; backend?: string | null; reason?: string | null; unreadable_files?: number | null; sealed_unreadable?: string | null }> {
    return this.#json('the workspace status', '/workspace/status')
  }

  async bytesStatus(): Promise<WorkspaceBytesStatus> {
    try {
      const status = await this.#status()
      if (status.store === 'blobs') return { store: 'blobs', backend: status.backend ?? '' }
      return {
        store: status.store,
        reason: status.reason ?? '',
        ...(status.unreadable_files ? { unreadable_files: status.unreadable_files } : {}),
      }
    } catch (err) {
      return { store: 'refused', reason: `the API's workspace status could not be read: ${(err as Error).message}` }
    }
  }

  async sealedUnreadable(): Promise<string | undefined> {
    try {
      return (await this.#status()).sealed_unreadable ?? undefined
    } catch {
      return 'its bytes are in the blob store, which does not answer now'
    }
  }

  async list(ws: WorkspaceRef, prefix?: string): Promise<WorkspaceFileInfo[]> {
    const under = prefix === undefined ? undefined : workspacePath(prefix.replace(/\/+$/, ''))
    const { files } = await this.#json<{ files: FileJson[] }>('the workspace files', `/workspace/files${query({ workspace: keyOf(ws), prefix: under })}`)
    return files.map(infoOf)
  }

  async usage(ws: WorkspaceRef): Promise<{ files: number; bytes: number }> {
    return this.#json('the workspace usage', `/workspace/usage${query({ workspace: keyOf(ws) })}`)
  }

  async listSessions(sessionIds: readonly string[]): Promise<Map<string, (WorkspaceFileInfo & { sealed: boolean })[]>> {
    const out = new Map<string, (WorkspaceFileInfo & { sealed: boolean })[]>()
    for (let i = 0; i < sessionIds.length; i += SESSIONS_PAGE) {
      const page = sessionIds.slice(i, i + SESSIONS_PAGE)
      const { sessions } = await this.#json<{ sessions: Record<string, (FileJson & { sealed: boolean })[]> }>(
        'the sessions\' workspace files',
        '/workspace/session-files',
        { session_ids: page },
      )
      for (const [id, rows] of Object.entries(sessions)) out.set(id, rows.map((row) => ({ ...infoOf(row), sealed: row.sealed })))
    }
    return out
  }

  async stat(ws: WorkspaceRef, path: string): Promise<WorkspaceFileInfo | undefined> {
    const name = workspacePath(path)
    const { file } = await this.#json<{ file: FileJson | null }>(`the workspace file ${name}`, `/workspace/file${query({ workspace: keyOf(ws), path: name })}`)
    return file === null ? undefined : infoOf(file)
  }

  async read(ws: WorkspaceRef, path: string): Promise<WorkspaceFile | undefined> {
    for (let attempt = 1; ; attempt += 1) {
      const info = await this.stat(ws, path)
      if (!info) return undefined
      try {
        const parts: Buffer[] = []
        for await (const part of this.chunks(ws, info.path, info.sha256)) parts.push(part)
        return { ...info, bytes: Buffer.concat(parts) }
      } catch (err) {
        // Replaced or deleted between the stat and the bytes: read it as it is now.
        if (!(err instanceof WorkspaceError) || (err.code !== 'conflict' && err.code !== 'not_found') || attempt >= READ_TRIES) throw err
      }
    }
  }

  /**
   * The bytes of `path` as it is at `expectedSha256`, as they stream from the API (`chunkBytes`
   * is the SQL store's slice; the API streams a sealed segment at a time). `not_found` when there
   * is no such file, `conflict` when it is not at that sha256, then or while it streams.
   */
  async *chunks(ws: WorkspaceRef, path: string, expectedSha256: string, _chunkBytes = STREAM_CHUNK_BYTES): AsyncGenerator<Buffer> {
    const name = workspacePath(path)
    const response = await this.#response(`the bytes of ${name}`, `/workspace/file-bytes${query({ workspace: keyOf(ws), path: name, expected_sha256: expectedSha256 })}`)
    const hash = createHash('sha256')
    const changed = () => new WorkspaceError('conflict', `${name} changed while it was being sent`)
    if (response.headers.get(SHA256_HEADER) !== expectedSha256 || !response.body) {
      await response.body?.cancel()
      throw changed()
    }
    const reader = response.body.getReader()
    try {
      for (;;) {
        let next: Awaited<ReturnType<typeof reader.read>>
        try {
          next = await reader.read()
        } catch {
          // The API ended the stream short: the file changed under it, or the read broke off.
          throw changed()
        }
        if (next.done) break
        const part = Buffer.from(next.value)
        hash.update(part)
        if (part.length > 0) yield part
      }
    } finally {
      reader.releaseLock()
    }
    if (hash.digest('hex') !== expectedSha256) throw changed()
  }

  async write(ws: WorkspaceRef, path: string, bytes: Uint8Array, options: WriteOptions = {}): Promise<WorkspaceFileInfo> {
    const name = workspacePath(path)
    // Checked here too, so a file past the cap is never sent; the API checks it again.
    const { fileMaxBytes } = await this.limits()
    if (bytes.byteLength > fileMaxBytes) {
      throw new WorkspaceError('too_large', `${name} would be ${bytes.byteLength} bytes; a workspace file holds at most ${fileMaxBytes}`)
    }
    const data = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const digest = sha256(data)
    const mediaType = options.mediaType?.trim() || guessMediaType(name, data)
    const events: readonly ServerEvent[] =
      ws.kind === 'session' && options.log ? options.log.events({ path: name, size: data.byteLength, mediaType, sha256: digest }) : []
    const expected = options.expectedSha256
    const meta = {
      workspace: keyOf(ws),
      path: name,
      media_type: mediaType,
      sha256: digest,
      size: data.byteLength,
      source: options.source ?? null,
      expect: expected === undefined ? 'any' : expected === null ? 'absent' : 'sha256',
      expected_sha256: typeof expected === 'string' ? expected : null,
      events: events.map((e) => JSON.stringify(e)),
    }
    const form = new FormData()
    form.append('meta', JSON.stringify(meta))
    form.append('file', new Blob([new Uint8Array(data)], { type: 'application/octet-stream' }), 'file')
    const response = await this.#response(`the write of ${name}`, '/workspace/file-writes', { method: 'POST', body: form })
    const { file, seqs } = (await response.json()) as { file: FileJson; seqs: number[] }
    if (options.log && events.length > 0) options.log.committed(events, seqs)
    return infoOf(file)
  }

  async delete(ws: WorkspaceRef, path: string, options: DeleteOptions = {}): Promise<boolean> {
    const name = workspacePath(path)
    const events = ws.kind === 'session' && options.log ? options.log.events : []
    const { deleted, seqs } = await this.#json<{ deleted: boolean; seqs: number[] }>(`the delete of ${name}`, '/workspace/file-deletes', {
      workspace: keyOf(ws),
      path: name,
      ...(options.expectedSha256 === undefined ? {} : { expected_sha256: options.expectedSha256 }),
      events: events.map((e) => JSON.stringify(e)),
    })
    if (options.log && deleted && events.length > 0) options.log.committed(events, seqs)
    return deleted
  }

  async copy(fromSession: string, toSession: string): Promise<void> {
    // Every file key is opened and wrapped again on the API: a large workspace takes a while.
    await this.#json('a fork\'s workspace files', '/workspace/copies', { from_session: fromSession, to_session: toSession }, FILE_TIMEOUT_MS)
  }

  async moveToBlobs(batch = 50): Promise<{ moved: number; local: boolean }> {
    let moved = 0
    for (;;) {
      // A batch of files is sealed and stored on the API before it answers.
      const page = await this.#json<{ moved: number; local: boolean; more: boolean }>('the move to blobs', '/workspace/blob-moves', { batch }, FILE_TIMEOUT_MS)
      moved += page.moved
      if (page.local) return { moved, local: true }
      // A batch that moved nothing moves nothing more: rows written meanwhile stay for the next start.
      if (!page.more || page.moved === 0) return { moved, local: false }
    }
  }

  async sweep(ttlMs = WORKSPACE_TTL_MS): Promise<number> {
    return (await this.#json<{ swept: number }>('the workspace sweep', '/workspace/sweeps', { ttl_ms: ttlMs })).swept
  }

  async reconcile(graceMs = RECONCILE_GRACE_MS): Promise<Reconciled> {
    const done = await this.#json<{ released: number; held: number; skipped: 'held' | null }>('the holds\' reconcile', '/workspace/reconciles', {
      grace_seconds: Math.floor(graceMs / 1000),
    })
    return { released: done.released, held: done.held, ...(done.skipped ? { skipped: done.skipped } : {}) }
  }

  startSweeper(everyMs: number, options: SweeperOptions = {}): () => void {
    return startWorkspaceSweeper(this, everyMs, { ...options, reconciles: true })
  }
}

/** `crud` once `useCrud()` holds (the API lists area `workspace`), `pg` until then, chosen per call (#5299 D9). */
export function gatedWorkspaceFiles(pg: SessionFiles, crud: WorkspaceFiles, useCrud: () => boolean): WorkspaceFiles {
  const pick = (): WorkspaceFiles => (useCrud() ? crud : pg)
  const gated: WorkspaceFiles = {
    get maxFiles() {
      return pick().maxFiles
    },
    limits: () => pick().limits(),
    fileCount: (ws) => pick().fileCount(ws),
    bytesStatus: () => pick().bytesStatus(),
    list: (ws, prefix) => pick().list(ws, prefix),
    usage: (ws) => pick().usage(ws),
    listSessions: (ids) => pick().listSessions(ids),
    sealedUnreadable: () => pick().sealedUnreadable(),
    stat: (ws, path) => pick().stat(ws, path),
    read: (ws, path) => pick().read(ws, path),
    chunks: (ws, path, expected, chunkBytes) => pick().chunks(ws, path, expected, chunkBytes),
    write: (ws, path, bytes, options) => pick().write(ws, path, bytes, options),
    delete: (ws, path, options) => pick().delete(ws, path, options),
    copy: (from, to) => pick().copy(from, to),
    moveToBlobs: (batch) => pick().moveToBlobs(batch),
    sweep: (ttlMs) => pick().sweep(ttlMs),
    reconcile: (graceMs) => pick().reconcile(graceMs),
    // Each tick picks again, so the sweeper moves to the routes once the area is listed.
    startSweeper: (everyMs, options = {}) => startWorkspaceSweeper(gated, everyMs, { ...options, reconciles: true }),
  }
  return gated
}
