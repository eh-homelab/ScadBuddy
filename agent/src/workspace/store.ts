import { createHash } from 'node:crypto'
import type { Sql, TransactionSql } from 'postgres'
import type { SettingsReader } from '../approvals/service.js'
import { WorkspaceBlobError, type WorkspaceBlobStore } from './blobs.js'
import { newFileKey, openFile, sealFile, type WorkspaceSealing } from './sealing.js'

// The assistant's workspace (#2329): files of its own, in `ai_session_files`,
// that the workspace_* tools (tools/workspace.ts) list, read, write, edit and
// delete, that workspace_save and http_request's `save_to_workspace` fill, and
// that uploads (#2329 part 2) and the panel's Files tab (part 3) read. In
// Postgres, never on the agent's disk: every replica, both session modes and
// /mcp see the same files, and no Claude Code built-in reaches them.
//
// A workspace is a session's (`session:<id>`, deleted with the session) or,
// for a call outside any session (/mcp, a flow run), its principal's own
// (`principal:<kind>:<id>`), swept WORKSPACE_TTL_MS after a file was last
// written. Paths are checked here (`workspacePath`) and again by the table's
// CHECK; no path is ever joined to a directory. Sizes are capped per file and
// per workspace by the settings WORKSPACE_FILE_MAX_SETTING and
// WORKSPACE_MAX_SETTING, checked under a per-workspace lock so two writes at
// once cannot both pass.
//
// The bytes (#4789): with `blobs` (main.ts) a file's bytes are a blob of the
// backend's content store, sealed under a file key of their own (sealing.ts) and
// held in `blob_refs` while the row stands (a trigger, migration 20261027T0114Z);
// the row keeps the metadata and the wrapped key. Only on a backend whose
// `store_backend` is `local` (it has no content store), or without `blobs` (tests),
// do the bytes stay in `data`, as before. On any other backend a write that cannot
// be sealed and stored (no KEK here, a backend that cannot mint the URLs or is too
// old or unreachable) is refused `unavailable`: never kept in plaintext. Reads take
// either. `moveToBlobs` moves the rows kept in `data` to blobs (main.ts, at start).

/** `ai_settings` key: the largest file (bytes). */
export const WORKSPACE_FILE_MAX_SETTING = 'workspace_file_max_bytes'
/** `ai_settings` key: the most one workspace holds (bytes). */
export const WORKSPACE_MAX_SETTING = 'workspace_max_bytes'
export const DEFAULT_FILE_MAX_BYTES = 25 * 1024 * 1024
export const DEFAULT_WORKSPACE_MAX_BYTES = 200 * 1024 * 1024
/** How long a principal's workspace file lives after its last write. */
export const WORKSPACE_TTL_MS = 24 * 60 * 60 * 1000
/** How often principals' expired files are swept. */
export const WORKSPACE_SWEEP_MS = 10 * 60 * 1000
/** The slice `chunks` reads at a time. */
export const STREAM_CHUNK_BYTES = 1024 * 1024

const SEGMENT = '[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}'
/** A workspace path: one to four segments, each starting with a letter, digit or `_` (the table's CHECK). */
export const WORKSPACE_PATH = new RegExp(`^${SEGMENT}(/${SEGMENT}){0,3}$`)
export const WORKSPACE_PATH_MAX = 512

/** Whose files: a session's, or a principal's own outside any session. */
export type WorkspaceRef = { kind: 'session'; sessionId: string } | { kind: 'principal'; principalKind: string; principalId: string }

/** The workspace a tool call uses: its session's, else its principal's own. */
export function workspaceOf(ctx: { session?: string | undefined; principal: { kind: string; id: string } }): WorkspaceRef {
  return ctx.session !== undefined
    ? { kind: 'session', sessionId: ctx.session }
    : { kind: 'principal', principalKind: ctx.principal.kind, principalId: ctx.principal.id }
}

/** A size as a person reads it, with the exact count: "12.1 KB (12,345 bytes)". */
export function humanSize(bytes: number): string {
  const exact = `${bytes.toLocaleString('en-US')} byte${bytes === 1 ? '' : 's'}`
  if (bytes < 1024) return exact
  const units = ['KB', 'MB', 'GB'] as const
  let n = bytes / 1024
  let unit = 0
  // Rounded before the unit is chosen, so 1,048,575 bytes is "1.0 MB", never "1024.0 KB" (#2530).
  while (Number(n.toFixed(1)) >= 1024 && unit < units.length - 1) {
    n /= 1024
    unit += 1
  }
  return `${n.toFixed(1)} ${units[unit]} (${exact})`
}

/**
 * The input without a `workspace_size` of its own. The classic gate and its audit row
 * get the SDK's unparsed input, so a size the model put there is dropped from both: the
 * only size on a card is one withWorkspaceSize read, and the audit row never records
 * a size nobody read (#2530).
 */
export function withoutWorkspaceSize(input: Record<string, unknown>): Record<string, unknown> {
  if (!('workspace_size' in input)) return input
  const own = { ...input }
  delete own.workspace_size
  return own
}

/**
 * The input an approval shows for a call that uploads a workspace file (#2396):
 * the input with `workspace_size` added, the size of the row `workspace_sha256`
 * pins in the workspace the call runs in, or saying there is no such file (the
 * upload is then refused when it runs). Keyed by the input's shape, not the
 * tool: any input with a string `workspace_path` and `workspace_sha256` (every
 * workspaceUploads.ts tool) gets it. Every gate that describes a call goes
 * through this: the classic gate and /mcp's prepare (approvals/service.ts
 * `create`, `createPrepared`) and a durable call (temporal/toolActivities.ts
 * `gate.describe_call`). Only what is SHOWN: the hash an approval binds to is
 * always of the call's own input, so the size is never something the model gave.
 * A lookup that fails says so on the card rather than failing the gate.
 */
export async function withWorkspaceSize(
  files: SessionFiles | undefined,
  ws: WorkspaceRef | undefined,
  input: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const { workspace_path: path, workspace_sha256: digest } = input
  const own = withoutWorkspaceSize(input)
  // The shape check keeps every call that uploads nothing off Postgres.
  if (files === undefined || ws === undefined || typeof path !== 'string' || typeof digest !== 'string') return own
  let info: WorkspaceFileInfo | undefined
  try {
    info = await files.stat(ws, path)
  } catch (err) {
    if (!(err instanceof WorkspaceError)) return { ...own, workspace_size: 'unknown: the workspace could not be read' }
  }
  const size =
    info?.sha256 === digest ? humanSize(info.size) : 'no such file at this sha256 in the workspace: the upload will be refused'
  return { ...own, workspace_size: size }
}

function keyOf(ws: WorkspaceRef): { key: string; sessionId: string | null } {
  return ws.kind === 'session'
    ? { key: `session:${ws.sessionId}`, sessionId: ws.sessionId }
    : { key: `principal:${ws.principalKind}:${ws.principalId}`, sessionId: null }
}

/** The workspace a stored `workspace` key names. */
function refOf(key: string): WorkspaceRef {
  if (key.startsWith('session:')) return { kind: 'session', sessionId: key.slice('session:'.length) }
  const [, principalKind = '', ...id] = key.split(':')
  return { kind: 'principal', principalKind, principalId: id.join(':') }
}

/** Where a file came from, for the audit log and the panel: a tool and its ids, or a URL. */
export type WorkspaceSource = { [key: string]: string | number | boolean | null }

/** The source key naming the builtin Edit call that last wrote a file (#3202): its retry is that call again. */
export const EDIT_CALL = 'edit_call'

/** A file's source kept by a write that is not that Edit call's: it names the call no longer. */
export function withoutEditCall(source: WorkspaceSource | null | undefined): WorkspaceSource | undefined {
  if (!source) return undefined
  const { [EDIT_CALL]: _call, ...rest } = source
  return rest
}

export type WorkspaceFileInfo = {
  path: string
  mediaType: string
  size: number
  sha256: string
  source: WorkspaceSource | null
  createdAt: Date
  updatedAt: Date
}

export type WorkspaceFile = WorkspaceFileInfo & { bytes: Buffer }

export type WorkspaceLimits = { fileMaxBytes: number; workspaceMaxBytes: number }

export type WorkspaceErrorCode = 'invalid_path' | 'not_found' | 'conflict' | 'too_large' | 'quota' | 'unavailable'

/** A refusal the caller may show as it is: it never quotes a file's bytes. */
export class WorkspaceError extends Error {
  override name = 'WorkspaceError'
  readonly code: WorkspaceErrorCode
  constructor(code: WorkspaceErrorCode, message: string) {
    super(message)
    this.code = code
  }
}

/** A write refused because the blob store cannot take it: nothing is kept in plaintext instead. */
function refused(why: string): WorkspaceError {
  return new WorkspaceError('unavailable', `${why}; nothing was written`)
}

/** `path` as stored (a leading `./` dropped), or a WorkspaceError `invalid_path` saying why not. */
export function workspacePath(path: string): string {
  const clean = path.replace(/^(\.\/)+/, '')
  if (clean.length === 0 || clean.length > WORKSPACE_PATH_MAX || !WORKSPACE_PATH.test(clean)) {
    throw new WorkspaceError(
      'invalid_path',
      `${JSON.stringify(path)} is not a workspace path: use up to 4 "/"-separated parts, each starting with a ` +
        'letter, digit or "_" and holding only letters, digits, "_", "." and "-" (e.g. "downloads/part.3mf")',
    )
  }
  return clean
}

export function sha256(bytes: Uint8Array): string {
  return createHash('sha256').update(bytes).digest('hex')
}

const BY_EXTENSION: Record<string, string> = {
  txt: 'text/plain',
  log: 'text/plain',
  md: 'text/markdown',
  scad: 'text/x-openscad',
  csv: 'text/csv',
  tsv: 'text/tab-separated-values',
  json: 'application/json',
  yaml: 'application/yaml',
  yml: 'application/yaml',
  xml: 'application/xml',
  html: 'text/html',
  htm: 'text/html',
  css: 'text/css',
  js: 'text/javascript',
  ts: 'text/plain',
  py: 'text/x-python',
  svg: 'image/svg+xml',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  gif: 'image/gif',
  webp: 'image/webp',
  '3mf': 'model/3mf',
  stl: 'model/stl',
  obj: 'model/obj',
  glb: 'model/gltf-binary',
  mp4: 'video/mp4',
  pdf: 'application/pdf',
  zip: 'application/zip',
  ttf: 'font/ttf',
  otf: 'font/otf',
  woff: 'font/woff',
  woff2: 'font/woff2',
}

/** What `bytes` at `path` most likely are: the image signature, then the extension, then text or not. */
export function guessMediaType(path: string, bytes: Uint8Array): string {
  const image = sniffImage(bytes)
  if (image) return image
  const ext = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase()
  const known = ext === undefined ? undefined : BY_EXTENSION[ext]
  if (known) return known
  return decodeText(bytes) === undefined ? 'application/octet-stream' : 'text/plain'
}

/** The type the first bytes say, for the four image types the Messages API reads. */
export function sniffImage(bytes: Uint8Array): 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp' | undefined {
  const head = Buffer.from(bytes.subarray(0, 16))
  const ascii = (from: number, text: string) => head.subarray(from, from + text.length).toString('latin1') === text
  if (head.length >= 8 && head.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'image/png'
  if (head.length >= 3 && head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return 'image/jpeg'
  if (ascii(0, 'GIF87a') || ascii(0, 'GIF89a')) return 'image/gif'
  if (ascii(0, 'RIFF') && ascii(8, 'WEBP')) return 'image/webp'
  return undefined
}

/** Types whose bytes are never text, whatever they decode as. */
const BINARY_TYPE = /^(image\/(?!svg\+xml)|video\/|audio\/|model\/|font\/|application\/(pdf|zip|gzip|x-tar|vnd\.))/

/** The bytes as text when they are UTF-8 with no NUL and the type is not a binary one, else undefined. */
export function decodeText(bytes: Uint8Array, mediaType?: string): string | undefined {
  if (mediaType !== undefined && BINARY_TYPE.test(mediaType.trim().toLowerCase())) return undefined
  if (bytes.includes(0)) return undefined
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return undefined
  }
}

type Row = {
  path: string
  media_type: string
  size: number
  sha256: string
  source: WorkspaceSource | null
  created_at: Date
  updated_at: Date
}

function infoOf(row: Row): WorkspaceFileInfo {
  return {
    path: row.path,
    mediaType: row.media_type,
    size: row.size,
    sha256: row.sha256,
    source: row.source,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  }
}

function positive(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : fallback
}

export type WriteOptions = {
  /** Stored as given; guessed from the bytes and the path when omitted. */
  mediaType?: string | undefined
  source?: WorkspaceSource | undefined
  /**
   * The sha256 the file must have now, or `null` for "must not exist yet":
   * otherwise a `conflict`. Omitted, the write replaces whatever is there.
   */
  expectedSha256?: string | null | undefined
  /** Runs in the write's transaction once the row is written, so what it writes commits with the file or not at all. */
  alongside?: ((tx: TransactionSql, file: WorkspaceFileInfo) => Promise<void>) | undefined
}

/**
 * Takes the session's row lock first when an `alongside` will write to the session (its
 * event log bumps `ai_sessions.event_seq`): a session delete takes that row before it
 * cascades to the files, so both take the locks in the same order and never deadlock.
 */
async function lockSession(tx: TransactionSql, sessionId: string | null): Promise<void> {
  if (sessionId !== null) await tx`SELECT 1 FROM ai_sessions WHERE id = ${sessionId} FOR NO KEY UPDATE`
}

/** Where a row's bytes are: `data`, or a sealed blob and its wrapped file key. */
type Stored = { data: Buffer | null; blob_key: string | null; key_sealed: Buffer | null; key_kek_id: string | null }

/** How the bytes leave Postgres: the backend's blob store, and who seals for it (none without the KEK). */
export type WorkspaceBlobs = { store: WorkspaceBlobStore; sealing?: WorkspaceSealing | undefined }

/**
 * Where a write's bytes go now (main.ts logs it at start; GET /api/v1/ai/status shows it):
 * the blob store, Postgres (`store_backend` local, or no blob store configured), or
 * nowhere, `reason` saying why every write is refused.
 */
export type WorkspaceBytesStatus =
  | { store: 'blobs'; backend: string }
  | { store: 'postgres'; reason: string; unreadable_files?: number }
  | { store: 'refused'; reason: string; unreadable_files?: number }

/** A row's bytes as `ai_session_files` takes them: inline, or a blob with its wrapped key. */
type Placed = { data: Buffer | null; blobKey: string | null; keySealed: Buffer | null; kekId: string | null }

async function collect(parts: AsyncIterable<Buffer>): Promise<Buffer> {
  const out: Buffer[] = []
  for await (const part of parts) out.push(part)
  return Buffer.concat(out)
}

export class SessionFiles {
  private readonly sql: Sql
  private readonly settings: SettingsReader | undefined
  private readonly fixed: Partial<WorkspaceLimits>
  private readonly blobs: WorkspaceBlobs | undefined

  constructor(
    sql: Sql,
    options: { settings?: SettingsReader | undefined; limits?: Partial<WorkspaceLimits>; blobs?: WorkspaceBlobs | undefined } = {},
  ) {
    this.sql = sql
    this.settings = options.settings
    this.fixed = options.limits ?? {}
    this.blobs = options.blobs
  }

  #sealing(): { store: WorkspaceBlobStore; sealing: WorkspaceSealing } {
    if (!this.blobs?.sealing) {
      throw new WorkspaceError('unavailable', 'this workspace file is sealed in the blob store, and this agent has no SCADBUDDY_SECRET_KEY_FILE to open it')
    }
    return { store: this.blobs.store, sealing: this.blobs.sealing }
  }

  /**
   * Where a write's bytes go: undefined for Postgres (no blob store configured, or
   * `store_backend` local), else the store and sealing; `unavailable` when the backend has
   * a blob store this agent cannot use.
   */
  async #target(): Promise<{ store: WorkspaceBlobStore; sealing: WorkspaceSealing } | undefined> {
    if (!this.blobs) return undefined
    let backend: string
    try {
      backend = await this.blobs.store.backend()
    } catch (err) {
      if (err instanceof WorkspaceBlobError) throw refused(err.message)
      throw err
    }
    if (backend === 'local') return undefined
    if (!this.blobs.sealing) {
      throw refused(`the backend keeps files in its blob store (store_backend ${backend}), and this agent has no SCADBUDDY_SECRET_KEY_FILE to seal them with`)
    }
    return { store: this.blobs.store, sealing: this.blobs.sealing }
  }

  /** Where a write's bytes go now, and why not when nowhere; never throws. */
  async bytesStatus(): Promise<WorkspaceBytesStatus> {
    let status: WorkspaceBytesStatus
    try {
      const target = await this.#target()
      if (target) {
        await target.store.probe()
        return { store: 'blobs', backend: await target.store.backend() }
      }
      status = { store: 'postgres', reason: this.blobs ? 'the backend has no blob store (store_backend local)' : 'no blob store is configured' }
    } catch (err) {
      const reason = err instanceof WorkspaceError || err instanceof WorkspaceBlobError ? err.message : 'the blob store could not be checked'
      status = { store: 'refused', reason: reason.replace(/; nothing was written$/, '') }
    }
    // Files already in the blob store cannot be read from here (after a switch back to
    // store_backend local the backend's URL route answers 404): say how many.
    const [row] = await this.sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ai_session_files WHERE blob_key IS NOT NULL`
    return row && row.n > 0 ? { ...status, unreadable_files: row.n } : status
  }

  /** The plaintext of a blob row, a segment at a time. */
  #open(ws: WorkspaceRef, path: string, digest: string, stored: Stored): AsyncGenerator<Buffer> {
    const blobs = this.#sealing()
    return (async function* () {
      const fileKey = await blobs.sealing.unwrap(ws, path, stored.blob_key!, stored.key_sealed!, stored.key_kek_id)
      try {
        yield* openFile(fileKey, blobs.store.get(stored.blob_key!), digest)
      } catch (err) {
        // The backend cannot give the blob (store_backend switched back to local, or it is down).
        if (err instanceof WorkspaceBlobError) throw new WorkspaceError('unavailable', `${path} is in the blob store and cannot be read now: ${err.message}`)
        throw err
      } finally {
        fileKey.fill(0)
      }
    })()
  }

  /**
   * Where `data` goes: sealed and uploaded as a blob, or inline only where #target says
   * Postgres. A store that refuses is `unavailable`.
   */
  async #place(ws: WorkspaceRef, path: string, data: Buffer, digest: string): Promise<Placed> {
    const target = await this.#target()
    if (!target) return { data, blobKey: null, keySealed: null, kekId: null }
    const fileKey = newFileKey()
    try {
      let blobKey: string
      try {
        blobKey = await target.store.put(sealFile(fileKey, data, digest))
      } catch (err) {
        if (err instanceof WorkspaceBlobError) throw refused(err.message)
        throw err
      }
      const { sealed, kekId } = await target.sealing.wrap(ws, path, blobKey, fileKey)
      return { data: null, blobKey, keySealed: sealed, kekId }
    } finally {
      fileKey.fill(0)
    }
  }

  /** The caps now: the settings, else the defaults (25 MB a file, 200 MB a workspace). */
  async limits(): Promise<WorkspaceLimits> {
    const [file, total] = await Promise.all([
      this.fixed.fileMaxBytes ?? this.settings?.get<unknown>(WORKSPACE_FILE_MAX_SETTING),
      this.fixed.workspaceMaxBytes ?? this.settings?.get<unknown>(WORKSPACE_MAX_SETTING),
    ])
    return {
      fileMaxBytes: positive(file, DEFAULT_FILE_MAX_BYTES),
      workspaceMaxBytes: positive(total, DEFAULT_WORKSPACE_MAX_BYTES),
    }
  }

  /** Every file's metadata, by path; only those under `prefix` (a directory, e.g. "downloads") when given. */
  async list(ws: WorkspaceRef, prefix?: string): Promise<WorkspaceFileInfo[]> {
    const { key } = keyOf(ws)
    const under = prefix === undefined ? undefined : `${workspacePath(prefix.replace(/\/+$/, ''))}/`
    const rows = await this.sql<Row[]>`
      SELECT path, media_type, size, sha256, source, created_at, updated_at FROM ai_session_files
      WHERE workspace = ${key} ${under === undefined ? this.sql`` : this.sql`AND starts_with(path, ${under})`}
      ORDER BY path`
    return rows.map(infoOf)
  }

  /** The bytes the workspace holds in all, and how many files. */
  async usage(ws: WorkspaceRef): Promise<{ files: number; bytes: number }> {
    const [row] = await this.sql<{ files: number; bytes: string }[]>`
      SELECT count(*)::int AS files, coalesce(sum(size), 0)::bigint AS bytes FROM ai_session_files WHERE workspace = ${keyOf(ws).key}`
    return { files: row?.files ?? 0, bytes: Number(row?.bytes ?? 0) }
  }

  /** One file's metadata, or undefined. */
  async stat(ws: WorkspaceRef, path: string): Promise<WorkspaceFileInfo | undefined> {
    const [row] = await this.sql<Row[]>`
      SELECT path, media_type, size, sha256, source, created_at, updated_at FROM ai_session_files
      WHERE workspace = ${keyOf(ws).key} AND path = ${workspacePath(path)}`
    return row ? infoOf(row) : undefined
  }

  /** One file with its bytes and sha256, or undefined. */
  async read(ws: WorkspaceRef, path: string): Promise<WorkspaceFile | undefined> {
    const [row] = await this.sql<(Row & Stored)[]>`
      SELECT path, media_type, size, sha256, source, created_at, updated_at, data, blob_key, key_sealed, key_kek_id
      FROM ai_session_files WHERE workspace = ${keyOf(ws).key} AND path = ${workspacePath(path)}`
    if (!row) return undefined
    const bytes = row.data ?? (await collect(this.#open(ws, row.path, row.sha256, row)))
    return { ...infoOf(row), bytes }
  }

  /**
   * The bytes of `path` as it is at `expectedSha256`, a slice at a time
   * (`substring` on the column, stored uncompressed for it), so an upload
   * streams a file to the backend without holding it whole. `not_found` when
   * there is no such file, `conflict` when it is not at that sha256, then or
   * between two slices.
   */
  async *chunks(ws: WorkspaceRef, path: string, expectedSha256: string, chunkBytes = STREAM_CHUNK_BYTES): AsyncGenerator<Buffer> {
    const name = workspacePath(path)
    const { key } = keyOf(ws)
    const [stored] = await this.sql<(Stored & { sha256: string })[]>`
      SELECT NULL::bytea AS data, blob_key, key_sealed, key_kek_id, sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name}`
    if (stored?.blob_key) {
      if (stored.sha256 !== expectedSha256) {
        throw new WorkspaceError('conflict', `${name} changed: its sha256 is ${stored.sha256}, not ${expectedSha256}`)
      }
      let sent = 0
      for await (const part of this.#open(ws, name, stored.sha256, stored)) {
        // Read again between segments, as from `data`: a file replaced mid-send is a conflict, not a mix.
        if (sent > 0) {
          const [now] = await this.sql<{ sha256: string }[]>`
            SELECT sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name}`
          if (!now) throw new WorkspaceError('not_found', `no file ${name} in the workspace; workspace_list lists them`)
          if (now.sha256 !== expectedSha256) {
            throw new WorkspaceError('conflict', `${name} changed while it was being sent: its sha256 is ${now.sha256}, not ${expectedSha256}`)
          }
        }
        if (part.length > 0) yield part
        sent += 1
      }
      return
    }
    for (let offset = 0; ; ) {
      const [row] = await this.sql<{ part: Buffer | null; size: number; sha256: string }[]>`
        SELECT substring(data FROM ${offset + 1}::int FOR ${chunkBytes}::int) AS part, size, sha256
        FROM ai_session_files WHERE workspace = ${key} AND path = ${name}`
      if (!row) throw new WorkspaceError('not_found', `no file ${name} in the workspace; workspace_list lists them`)
      if (row.sha256 !== expectedSha256) {
        throw new WorkspaceError(
          'conflict',
          `${name} changed${offset > 0 ? ' while it was being sent' : ''}: its sha256 is ${row.sha256}, not ${expectedSha256}`,
        )
      }
      // Moved to a blob mid-send (moveToBlobs): sent again from the start by the caller.
      if (row.part === null) throw new WorkspaceError('conflict', `${name} changed while it was being sent`)
      if (row.part.length > 0) yield row.part
      offset += row.part.length
      if (row.part.length === 0 || offset >= row.size) return
    }
  }

  /**
   * Creates or replaces a file. Refused `too_large` past the per-file cap,
   * `quota` past the workspace's, `conflict` when `expectedSha256` does not
   * hold. Answers the file as stored.
   */
  async write(ws: WorkspaceRef, path: string, bytes: Uint8Array, options: WriteOptions = {}): Promise<WorkspaceFileInfo> {
    const name = workspacePath(path)
    const { key, sessionId } = keyOf(ws)
    const { fileMaxBytes, workspaceMaxBytes } = await this.limits()
    if (bytes.byteLength > fileMaxBytes) {
      throw new WorkspaceError('too_large', `${name} would be ${bytes.byteLength} bytes; a workspace file holds at most ${fileMaxBytes}`)
    }
    const data = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength)
    const digest = sha256(data)
    const mediaType = options.mediaType?.trim() || guessMediaType(name, data)
    const source = options.source ?? null
    // Checked before any upload too, so a refused write sends nothing; the checks under the lock below are the ones that hold.
    if (this.blobs) await this.#precheck(key, name, data.byteLength, workspaceMaxBytes, options.expectedSha256)
    const placed = await this.#place(ws, name, data, digest)
    return this.sql.begin(async (tx) => {
      if (options.alongside) await lockSession(tx, sessionId)
      await tx`SELECT pg_advisory_xact_lock(hashtextextended(${`scadbuddy:ai_session_files:${key}`}, 0))`
      const [current] = await tx<{ sha256: string }[]>`
        SELECT sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name} FOR UPDATE`
      if (options.expectedSha256 !== undefined) {
        if (options.expectedSha256 === null && current) {
          throw new WorkspaceError('conflict', `${name} already exists (sha256 ${current.sha256}); nothing was written`)
        }
        if (options.expectedSha256 !== null && current?.sha256 !== options.expectedSha256) {
          throw new WorkspaceError(
            'conflict',
            current
              ? `${name} changed since it was read: its sha256 is ${current.sha256}, not ${options.expectedSha256}; read it again. Nothing was written`
              : `${name} does not exist any more; nothing was written`,
          )
        }
      }
      const [held] = await tx<{ bytes: string }[]>`
        SELECT coalesce(sum(size), 0)::bigint AS bytes FROM ai_session_files WHERE workspace = ${key} AND path <> ${name}`
      const total = Number(held?.bytes ?? 0) + data.byteLength
      if (total > workspaceMaxBytes) {
        throw new WorkspaceError(
          'quota',
          `the workspace would hold ${total} bytes; it holds at most ${workspaceMaxBytes}. Delete files with workspace_delete first`,
        )
      }
      const [row] = await tx<Row[]>`
        INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, blob_key, key_sealed, key_kek_id, sha256, size, source)
        VALUES (${key}, ${sessionId}, ${name}, ${mediaType}, ${placed.data}, ${placed.blobKey}, ${placed.keySealed}, ${placed.kekId},
                ${digest}, ${data.byteLength}, ${source === null ? null : tx.json(source)})
        ON CONFLICT (workspace, path) DO UPDATE SET
          media_type = EXCLUDED.media_type, data = EXCLUDED.data, blob_key = EXCLUDED.blob_key,
          key_sealed = EXCLUDED.key_sealed, key_kek_id = EXCLUDED.key_kek_id, sha256 = EXCLUDED.sha256,
          size = EXCLUDED.size, source = EXCLUDED.source, updated_at = now()
        RETURNING path, media_type, size, sha256, source, created_at, updated_at`
      const file = infoOf(row!)
      await options.alongside?.(tx, file)
      return file
    })
  }

  /**
   * Deletes a file; false when there was none. `conflict` when `expectedSha256` is given and does not hold.
   * `alongside` runs in the delete's transaction once the row is gone.
   */
  async delete(
    ws: WorkspaceRef,
    path: string,
    options: { expectedSha256?: string | undefined; alongside?: ((tx: TransactionSql) => Promise<void>) | undefined } = {},
  ): Promise<boolean> {
    const name = workspacePath(path)
    const { key, sessionId } = keyOf(ws)
    return this.sql.begin(async (tx) => {
      if (options.alongside) await lockSession(tx, sessionId)
      if (options.expectedSha256 === undefined) {
        const rows = await tx`DELETE FROM ai_session_files WHERE workspace = ${key} AND path = ${name} RETURNING path`
        if (rows.length === 0) return false
      } else {
        const [row] = await tx<{ sha256: string; deleted: boolean }[]>`
          WITH target AS (SELECT sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name}),
               gone AS (DELETE FROM ai_session_files WHERE workspace = ${key} AND path = ${name} AND sha256 = ${options.expectedSha256} RETURNING path)
          SELECT target.sha256, EXISTS (SELECT 1 FROM gone) AS deleted FROM target`
        if (!row) return false
        if (!row.deleted) {
          throw new WorkspaceError('conflict', `${name} changed since it was read: its sha256 is ${row.sha256}; nothing was deleted`)
        }
      }
      await options.alongside?.(tx)
      return true
    })
  }

  /** The write's conflict and quota checks, without the lock, before its upload. */
  async #precheck(key: string, name: string, size: number, workspaceMaxBytes: number, expected: string | null | undefined): Promise<void> {
    const [row] = await this.sql<{ current: string | null; held: string }[]>`
      SELECT (SELECT sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name}) AS current,
             (SELECT coalesce(sum(size), 0)::bigint FROM ai_session_files WHERE workspace = ${key} AND path <> ${name}) AS held`
    const current = row?.current ?? null
    if (expected === null && current !== null) {
      throw new WorkspaceError('conflict', `${name} already exists (sha256 ${current}); nothing was written`)
    }
    if (expected !== undefined && expected !== null && current !== expected) {
      throw new WorkspaceError(
        'conflict',
        current !== null
          ? `${name} changed since it was read: its sha256 is ${current}, not ${expected}; read it again. Nothing was written`
          : `${name} does not exist any more; nothing was written`,
      )
    }
    const total = Number(row?.held ?? 0) + size
    if (total > workspaceMaxBytes) {
      throw new WorkspaceError(
        'quota',
        `the workspace would hold ${total} bytes; it holds at most ${workspaceMaxBytes}. Delete files with workspace_delete first`,
      )
    }
  }

  /**
   * A fork's copy of its parent's files. A blob's row names the parent's blob, its file
   * key wrapped again for the child: no bytes are copied (copy-on-write, #4212). All or
   * nothing: every file key is wrapped for the child first (what can fail: no KEK here, a
   * forgotten parent), then every row is written in one transaction.
   */
  async copy(fromSession: string, toSession: string): Promise<void> {
    const from = `session:${fromSession}`
    const to = `session:${toSession}`
    const rows = await this.sql<(Row & Stored)[]>`
      SELECT path, media_type, size, sha256, source, created_at, updated_at, NULL::bytea AS data, blob_key, key_sealed, key_kek_id
      FROM ai_session_files WHERE workspace = ${from} AND blob_key IS NOT NULL`
    const wrapped: { row: Row & Stored; sealed: Buffer }[] = []
    if (rows.length > 0) {
      const { sealing } = this.#sealing()
      const parent: WorkspaceRef = { kind: 'session', sessionId: fromSession }
      const child: WorkspaceRef = { kind: 'session', sessionId: toSession }
      for (const row of rows) {
        const fileKey = await sealing.unwrap(parent, row.path, row.blob_key!, row.key_sealed!, null)
        try {
          wrapped.push({ row, sealed: (await sealing.wrap(child, row.path, row.blob_key!, fileKey)).sealed })
        } finally {
          fileKey.fill(0)
        }
      }
    }
    await this.sql.begin(async (tx) => {
      await tx`
        INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size, source, created_at, updated_at)
        SELECT ${to}, ${toSession}, path, media_type, data, sha256, size, source, created_at, updated_at
        FROM ai_session_files WHERE workspace = ${from} AND data IS NOT NULL
        ON CONFLICT (workspace, path) DO NOTHING`
      for (const { row, sealed } of wrapped) {
        await tx`
          INSERT INTO ai_session_files (workspace, session_id, path, media_type, blob_key, key_sealed, sha256, size, source, created_at, updated_at)
          VALUES (${to}, ${toSession}, ${row.path}, ${row.media_type}, ${row.blob_key}, ${sealed}, ${row.sha256}, ${row.size},
                  ${row.source === null ? null : tx.json(row.source)}, ${row.created_at}, ${row.updated_at})
          ON CONFLICT (workspace, path) DO NOTHING`
      }
    })
  }

  /**
   * Moves every file still kept in `data` to a sealed blob (#4789; main.ts, at start), one
   * row at a time; a row written meanwhile is left as its writer left it. Idempotent: a
   * row once moved is not read again. `local` when the backend keeps no blobs
   * (store_backend local), which moves nothing; `unavailable` (thrown) when it has a blob
   * store this agent cannot use, which moves nothing either.
   */
  async moveToBlobs(batch = 50): Promise<{ moved: number; local: boolean }> {
    if (!(await this.#target())) return { moved: 0, local: true }
    let moved = 0
    for (;;) {
      const rows = await this.sql<{ workspace: string; path: string; sha256: string }[]>`
        SELECT workspace, path, sha256 FROM ai_session_files WHERE data IS NOT NULL ORDER BY workspace, path LIMIT ${batch}`
      if (rows.length === 0) return { moved, local: false }
      for (const { workspace, path, sha256: digest } of rows) {
        const [held] = await this.sql<{ data: Buffer }[]>`
          SELECT data FROM ai_session_files WHERE workspace = ${workspace} AND path = ${path} AND sha256 = ${digest} AND data IS NOT NULL`
        if (!held) continue
        const placed = await this.#place(refOf(workspace), path, held.data, digest)
        if (placed.blobKey === null) return { moved, local: true }
        const updated = await this.sql`
          UPDATE ai_session_files SET data = NULL, blob_key = ${placed.blobKey}, key_sealed = ${placed.keySealed}, key_kek_id = ${placed.kekId}
          WHERE workspace = ${workspace} AND path = ${path} AND sha256 = ${digest} AND data IS NOT NULL`
        moved += updated.count
      }
    }
  }

  /** Deletes principals' files last written more than `ttlMs` ago; how many went. Sessions' files go with their session. */
  async sweep(ttlMs = WORKSPACE_TTL_MS): Promise<number> {
    const rows = await this.sql`
      DELETE FROM ai_session_files
      WHERE session_id IS NULL AND updated_at < now() - (${ttlMs} * interval '1 millisecond') RETURNING path`
    return rows.length
  }

  /** Sweeps every `everyMs` once `ready` says the database is; the returned function stops it. */
  startSweeper(everyMs: number, options: { ready?: () => Promise<boolean>; onError?: (err: unknown) => void } = {}): () => void {
    const ready = options.ready ?? (() => Promise.resolve(true))
    const tick = () => {
      ready()
        .then((ok) => (ok ? this.sweep() : 0))
        .catch(options.onError ?? (() => {}))
    }
    const timer = setInterval(tick, everyMs)
    timer.unref()
    return () => clearInterval(timer)
  }
}
