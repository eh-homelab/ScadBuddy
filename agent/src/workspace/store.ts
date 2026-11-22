import { createHash } from 'node:crypto'
import type { SettingsReader } from '../approvals/service.js'
import type { ServerEvent } from '../sessions/protocol.js'

// The assistant's workspace (#2329): files of its own, in `ai_session_files` on the API,
// that the workspace_* tools (tools/workspace.ts) list, read, write, edit and delete, that
// workspace_save and http_request's `save_to_workspace` fill, and that uploads (#2329 part 2)
// and the panel's Files tab (part 3) read. Never on the agent's disk as the record: every
// replica, every session and /mcp see the same files.
//
// A workspace is a session's (`session:<id>`, deleted with the session) or, for a call outside
// any session (/mcp, a flow run), its principal's own (`principal:<kind>:<id>`), swept
// WORKSPACE_TTL_MS after a file was last written. Paths are checked here (`workspacePath`) and
// again by the API; no path is ever joined to a directory. Sizes are capped per file and per
// workspace by the settings WORKSPACE_FILE_MAX_SETTING and WORKSPACE_MAX_SETTING.
//
// The rows and the bytes are the API's (#5299 step 7, area `workspace`): `CrudSessionFiles`
// (workspace/crudStore.ts) reads and writes them through `…/crud/agent/workspace/*` (backend
// api/crud_agent_workspace.py), which seals and opens each file with the API's KEK and the
// session's payload key (D5), holds its blob in the row's own transaction and logs its events
// there too. This file holds the `WorkspaceFiles` interface and the checks both sides share.

/** `ai_settings` key: the largest file (bytes). */
export const WORKSPACE_FILE_MAX_SETTING = 'workspace_file_max_bytes'
/** `ai_settings` key: the most one workspace holds (bytes). */
export const WORKSPACE_MAX_SETTING = 'workspace_max_bytes'
export const DEFAULT_FILE_MAX_BYTES = 25 * 1024 * 1024
export const DEFAULT_WORKSPACE_MAX_BYTES = 200 * 1024 * 1024
/**
 * The most files one workspace holds. Each file brings at most three parent directories with it
 * (WORKSPACE_PATH: four segments), so a placement of a full workspace (workspace/directory.ts)
 * makes at most 4 × 5,000 = 20,000 entries, its MAX_ENTRIES: no placement can make a directory
 * the turn's entry check would stop.
 */
export const WORKSPACE_MAX_FILES = 5_000
/** How long a principal's workspace file lives after its last write. */
export const WORKSPACE_TTL_MS = 24 * 60 * 60 * 1000
/** How often principals' expired files are swept. */
export const WORKSPACE_SWEEP_MS = 10 * 60 * 1000
/**
 * How old an orphan hold must be before `reconcile` releases it: longer than any write
 * takes from its hold to its commit, so a write about to land keeps its hold.
 */
export const RECONCILE_GRACE_MS = 60 * 60 * 1000
/** How often the sweeper's tick also runs `reconcile`. */
export const RECONCILE_EVERY_MS = 60 * 60 * 1000
/** The advisory lock that lets one replica at a time run `reconcile`. */
export const RECONCILE_LOCK = 'scadbuddy:ai_session_files:reconcile'
/** Why `reconcile` did nothing: another replica holds RECONCILE_LOCK, or no lock connection was free. */
export type ReconcileSkipped = 'held' | 'no_connection'
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
  files: WorkspaceFiles | undefined,
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

/** The type `path`'s extension names, or undefined when the extension is not a known one. */
export function mediaTypeOfName(path: string): string | undefined {
  const ext = /\.([A-Za-z0-9]+)$/.exec(path)?.[1]?.toLowerCase()
  return ext === undefined ? undefined : BY_EXTENSION[ext]
}

/** What `bytes` at `path` most likely are: the image signature, then the extension, then text or not. */
export function guessMediaType(path: string, bytes: Uint8Array): string {
  const image = sniffImage(bytes)
  if (image) return image
  const known = mediaTypeOfName(path)
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

function positive(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : fallback
}

/** What a write's event says of the file (the panel's `workspace.file`): known before the write. */
export type LoggedFile = Pick<WorkspaceFileInfo, 'path' | 'size' | 'mediaType' | 'sha256'>

/** Called once the events a write or a delete logged have committed, with their seqs (EventLog `committed`). */
export type EventsCommitted = (events: readonly ServerEvent[], seqs: readonly number[]) => void

export type WriteOptions = {
  /** Stored as given; guessed from the bytes and the path when omitted. */
  mediaType?: string | undefined
  source?: WorkspaceSource | undefined
  /**
   * The sha256 the file must have now, or `null` for "must not exist yet":
   * otherwise a `conflict`. Omitted, the write replaces whatever is there.
   */
  expectedSha256?: string | null | undefined
  /**
   * Events of the session's log (a session's workspace only), written in the write's
   * transaction, so they commit with the file or not at all; `committed` hears of them after.
   */
  log?: { events: (file: LoggedFile) => readonly ServerEvent[]; committed: EventsCommitted } | undefined
}

export type DeleteOptions = {
  /** The sha256 the file must have now, else a `conflict`. */
  expectedSha256?: string | undefined
  /** Events of the session's log, written in the delete's transaction once the row is gone. */
  log?: { events: readonly ServerEvent[]; committed: EventsCommitted } | undefined
}

/** What `reconcile` did, or why it did nothing. */
export type Reconciled = { released: number; held: number; skipped?: ReconcileSkipped }

/** The workspace files, through the API's CRUD routes (`CrudSessionFiles`). */
export interface WorkspaceFiles {
  /** The most files one workspace holds. */
  readonly maxFiles: number
  limits(): Promise<WorkspaceLimits>
  fileCount(ws: WorkspaceRef): Promise<number>
  bytesStatus(): Promise<WorkspaceBytesStatus>
  list(ws: WorkspaceRef, prefix?: string): Promise<WorkspaceFileInfo[]>
  usage(ws: WorkspaceRef): Promise<{ files: number; bytes: number }>
  listSessions(sessionIds: readonly string[]): Promise<Map<string, (WorkspaceFileInfo & { sealed: boolean })[]>>
  sealedUnreadable(): Promise<string | undefined>
  stat(ws: WorkspaceRef, path: string): Promise<WorkspaceFileInfo | undefined>
  read(ws: WorkspaceRef, path: string): Promise<WorkspaceFile | undefined>
  chunks(ws: WorkspaceRef, path: string, expectedSha256: string, chunkBytes?: number): AsyncGenerator<Buffer>
  write(ws: WorkspaceRef, path: string, bytes: Uint8Array, options?: WriteOptions): Promise<WorkspaceFileInfo>
  delete(ws: WorkspaceRef, path: string, options?: DeleteOptions): Promise<boolean>
  copy(fromSession: string, toSession: string): Promise<void>
  moveToBlobs(batch?: number): Promise<{ moved: number; local: boolean }>
  sweep(ttlMs?: number): Promise<number>
  reconcile(graceMs?: number): Promise<Reconciled>
  startSweeper(everyMs: number, options?: SweeperOptions): () => void
}

export type SweeperOptions = { ready?: () => Promise<boolean>; onError?: (err: unknown) => void }

/** The caps: the fixed ones, then the settings, else the defaults (25 MB a file, 200 MB a workspace). */
export async function limitsOf(fixed: Partial<WorkspaceLimits>, settings: SettingsReader | undefined): Promise<WorkspaceLimits> {
  const [file, total] = await Promise.all([
    fixed.fileMaxBytes ?? settings?.get<unknown>(WORKSPACE_FILE_MAX_SETTING),
    fixed.workspaceMaxBytes ?? settings?.get<unknown>(WORKSPACE_MAX_SETTING),
  ])
  return {
    fileMaxBytes: positive(file, DEFAULT_FILE_MAX_BYTES),
    workspaceMaxBytes: positive(total, DEFAULT_WORKSPACE_MAX_BYTES),
  }
}

/**
 * Sweeps `files` every `everyMs` once `ready` says the database is, and with `reconciles`
 * reconciles the holds on the first tick past RECONCILE_EVERY_MS since the last; a reconcile
 * that skipped `no_connection` or failed is tried again at the next tick. The returned
 * function stops it.
 */
export function startWorkspaceSweeper(
  files: Pick<WorkspaceFiles, 'sweep' | 'reconcile'>,
  everyMs: number,
  options: SweeperOptions & { reconciles: boolean },
): () => void {
  const ready = options.ready ?? (() => Promise.resolve(true))
  let reconciledAt = 0
  const tick = () => {
    ready()
      .then(async (ok) => {
        if (!ok) return
        await files.sweep()
        if (!options.reconciles || Date.now() - reconciledAt < RECONCILE_EVERY_MS) return
        reconciledAt = Date.now()
        // No replica ran it this hour when no lock connection was free or it failed: try at the next tick.
        let skipped: ReconcileSkipped | undefined
        try {
          skipped = (await files.reconcile()).skipped
        } catch (err) {
          reconciledAt = 0
          throw err
        }
        if (skipped === 'no_connection') reconciledAt = 0
      })
      .catch(options.onError ?? (() => {}))
  }
  const timer = setInterval(tick, everyMs)
  timer.unref()
  return () => clearInterval(timer)
}

/**
 * Where a write's bytes go now (main.ts logs it at start; GET /api/v1/ai/status shows it):
 * the blob store, Postgres (`store_backend` local, or no blob store configured), or
 * nowhere, `reason` saying why every write is refused.
 */
export type WorkspaceBytesStatus =
  | { store: 'blobs'; backend: string }
  | { store: 'postgres'; reason: string; unreadable_files?: number }
  | { store: 'refused'; reason: string; unreadable_files?: number }
