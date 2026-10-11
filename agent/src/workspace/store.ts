import { createHash } from 'node:crypto'
import type { Sql } from 'postgres'
import type { SettingsReader } from '../approvals/service.js'

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

function keyOf(ws: WorkspaceRef): { key: string; sessionId: string | null } {
  return ws.kind === 'session'
    ? { key: `session:${ws.sessionId}`, sessionId: ws.sessionId }
    : { key: `principal:${ws.principalKind}:${ws.principalId}`, sessionId: null }
}

/** Where a file came from, for the audit log and the panel: a tool and its ids, or a URL. */
export type WorkspaceSource = { [key: string]: string | number | boolean | null }

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

export type WorkspaceErrorCode = 'invalid_path' | 'not_found' | 'conflict' | 'too_large' | 'quota'

/** A refusal the caller may show as it is: it never quotes a file's bytes. */
export class WorkspaceError extends Error {
  override name = 'WorkspaceError'
  readonly code: WorkspaceErrorCode
  constructor(code: WorkspaceErrorCode, message: string) {
    super(message)
    this.code = code
  }
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
}

export class SessionFiles {
  private readonly sql: Sql
  private readonly settings: SettingsReader | undefined
  private readonly fixed: Partial<WorkspaceLimits>

  constructor(sql: Sql, options: { settings?: SettingsReader | undefined; limits?: Partial<WorkspaceLimits> } = {}) {
    this.sql = sql
    this.settings = options.settings
    this.fixed = options.limits ?? {}
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
    const [row] = await this.sql<(Row & { data: Buffer })[]>`
      SELECT path, media_type, size, sha256, source, created_at, updated_at, data FROM ai_session_files
      WHERE workspace = ${keyOf(ws).key} AND path = ${workspacePath(path)}`
    return row ? { ...infoOf(row), bytes: row.data } : undefined
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
    return this.sql.begin(async (tx) => {
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
        INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size, source)
        VALUES (${key}, ${sessionId}, ${name}, ${mediaType}, ${data}, ${digest}, ${data.byteLength},
                ${source === null ? null : tx.json(source)})
        ON CONFLICT (workspace, path) DO UPDATE SET
          media_type = EXCLUDED.media_type, data = EXCLUDED.data, sha256 = EXCLUDED.sha256,
          size = EXCLUDED.size, source = EXCLUDED.source, updated_at = now()
        RETURNING path, media_type, size, sha256, source, created_at, updated_at`
      return infoOf(row!)
    })
  }

  /** Deletes a file; false when there was none. `conflict` when `expectedSha256` is given and does not hold. */
  async delete(ws: WorkspaceRef, path: string, options: { expectedSha256?: string | undefined } = {}): Promise<boolean> {
    const name = workspacePath(path)
    const { key } = keyOf(ws)
    if (options.expectedSha256 === undefined) {
      const rows = await this.sql`DELETE FROM ai_session_files WHERE workspace = ${key} AND path = ${name} RETURNING path`
      return rows.length > 0
    }
    const rows = await this.sql<{ sha256: string; deleted: boolean }[]>`
      WITH target AS (SELECT sha256 FROM ai_session_files WHERE workspace = ${key} AND path = ${name}),
           gone AS (DELETE FROM ai_session_files WHERE workspace = ${key} AND path = ${name} AND sha256 = ${options.expectedSha256} RETURNING path)
      SELECT target.sha256, EXISTS (SELECT 1 FROM gone) AS deleted FROM target`
    const [row] = rows
    if (!row) return false
    if (!row.deleted) {
      throw new WorkspaceError('conflict', `${name} changed since it was read: its sha256 is ${row.sha256}; nothing was deleted`)
    }
    return true
  }

  /** A fork's copy of its parent's files. */
  async copy(fromSession: string, toSession: string): Promise<void> {
    await this.sql`
      INSERT INTO ai_session_files (workspace, session_id, path, media_type, data, sha256, size, source, created_at, updated_at)
      SELECT ${`session:${toSession}`}, ${toSession}, path, media_type, data, sha256, size, source, created_at, updated_at
      FROM ai_session_files WHERE workspace = ${`session:${fromSession}`}
      ON CONFLICT (workspace, path) DO NOTHING`
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
