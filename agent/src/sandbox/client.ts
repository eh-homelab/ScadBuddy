import { createHash } from 'node:crypto'
import { Agent, type Dispatcher, fetch } from 'undici'
import { dirBytes, dirFileSha256, dirFiles, readDirFile, removeDirFile, writeDirFile } from '../workspace/directory.js'
import { type ExecResult, type KeptRecord, keptValue, type Manifest, SHA_HEADER, type Shell, syncPath, UNSYNCED_MAX } from './protocol.js'

// The agent's side of a session's sandbox (#5864, issue comment "Design" §2-§3): the client of the
// exec server (server.ts) and the sync between the session's directory on this worker (its cwd,
// workspace/directory.ts) and the pod's /work. The session directory stays the copy that counts:
// before each command the pod is made to match it, and after it what the command changed comes
// back. The turn's own write-back then records it in SessionFiles, as it does for Write and Edit.

/** Where a session's sandbox is: its exec server's URL and the token the agent made for it. */
export type SandboxEndpoint = { url: string; token: string }

/** Finds or makes a session's sandbox (pods.ts `KubernetesSandboxes`; tests run a local server). */
export interface Sandboxes {
  acquire(sessionId: string, signal: AbortSignal): Promise<SandboxEndpoint>
  /** The endpoint failed: the next acquire looks again rather than using it. */
  release?(sessionId: string, endpoint: SandboxEndpoint): void
}

export class SandboxError extends Error {
  override name = 'SandboxError'
}

/** A file the sandbox sent past the cap it was read under. */
export class SandboxTooLarge extends Error {
  override name = 'SandboxTooLarge'
}

/** A path the sandbox named, only ever shown to the model (in the untrusted-data envelope): cut to 200 characters. */
const shown = (value: unknown): string => (typeof value === 'string' ? value : (JSON.stringify(value) ?? '')).slice(0, 200)

/**
 * The sandbox's manifest, its entries at sync paths only: a command controls what it says. `bad`
 * names the entries that are not (shown, never used), `unsynced` the paths it says hold files
 * that never sync.
 */
async function checkedManifest(client: SandboxClient): Promise<{ files: Manifest['files']; truncated: boolean; bad: string[]; unsynced: string[] }> {
  const raw = await client.manifest()
  const files: Manifest['files'] = []
  const bad: string[] = []
  let anyBad = false
  for (const f of Array.isArray(raw?.files) ? raw.files : []) {
    const ok =
      typeof f?.path === 'string' && syncPath(f.path) && typeof f.sha256 === 'string' && /^[0-9a-f]{64}$/.test(f.sha256) && Number.isSafeInteger(f.size)
    if (ok) files.push(f)
    else {
      anyBad = true
      if (bad.length < UNSYNCED_MAX) bad.push(shown(f?.path ?? f))
    }
  }
  const unsynced = (Array.isArray(raw?.unsynced) ? raw.unsynced : []).slice(0, UNSYNCED_MAX).map(shown)
  return { files, truncated: raw?.truncated === true || anyBad, bad, unsynced }
}

const REQUEST_MS = 60_000
/**
 * The most JSON a manifest, an exec result or the kept record may be: a command controls what
 * the pod sends. An exec result holds two streams of at most STREAM_MAX_BYTES each, which JSON
 * escaping can make six times larger; a manifest at most MANIFEST_MAX_ENTRIES short entries.
 */
export const JSON_MAX_BYTES = 16 * 1024 * 1024
/** How long past its own deadline an exec request may wait for its answer (the server's sweep and reply). */
const EXEC_SLACK_MS = 30_000

type SandboxResponse = Awaited<ReturnType<typeof fetch>>

/** How much of an error answer's body is read and shown; the rest is cancelled unread. */
export const ERROR_BODY_MAX_BYTES = 500

/**
 * The first `max` bytes of a body, the rest cancelled: a pod may send an error body that never
 * ends. The cut may split a UTF-8 character, which then reads as U+FFFD at the end.
 */
async function bodyHead(res: SandboxResponse, max: number): Promise<string> {
  const chunks: Buffer[] = []
  let size = 0
  const reader = res.body?.getReader()
  if (!reader) return ''
  try {
    while (size < max) {
      const { done, value } = await reader.read()
      if (done) break
      chunks.push(Buffer.from(value))
      size += value.byteLength
    }
  } finally {
    await reader.cancel().catch(() => {})
  }
  return Buffer.concat(chunks).subarray(0, max).toString('utf8')
}

/** A JSON body read under a cap: past it, a SandboxError, whatever the pod claims. */
async function cappedJson(res: SandboxResponse, max: number, what: string): Promise<unknown> {
  const chunks: Buffer[] = []
  let size = 0
  const reader = res.body?.getReader()
  if (reader) {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      size += value.byteLength
      if (size > max) {
        await reader.cancel().catch(() => {})
        throw new SandboxError(`the sandbox's ${what} is past ${max} bytes`)
      }
      chunks.push(Buffer.from(value))
    }
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown
  } catch {
    throw new SandboxError(`the sandbox's ${what} is not JSON`)
  }
}

const isCount = (v: unknown) => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0

/** An exec result as the pod sent it, checked field by field: a command controls what the pod says. */
export function checkedExecResult(raw: unknown): ExecResult {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const ok =
    (r.exit_code === null || (typeof r.exit_code === 'number' && Number.isInteger(r.exit_code))) &&
    (r.signal === null || typeof r.signal === 'string') &&
    typeof r.timed_out === 'boolean' &&
    typeof r.stdout === 'string' &&
    typeof r.stderr === 'string' &&
    isCount(r.stdout_bytes) &&
    isCount(r.stderr_bytes) &&
    isCount(r.duration_ms)
  if (!ok) throw new SandboxError('the sandbox sent an exec result that is not one')
  return {
    exit_code: r.exit_code as number | null,
    signal: r.signal as string | null,
    timed_out: r.timed_out as boolean,
    stdout: r.stdout as string,
    stderr: r.stderr as string,
    stdout_bytes: r.stdout_bytes as number,
    stderr_bytes: r.stderr_bytes as number,
    duration_ms: r.duration_ms as number,
  }
}

export class SandboxClient {
  readonly endpoint: SandboxEndpoint
  readonly signal: AbortSignal

  constructor(endpoint: SandboxEndpoint, signal: AbortSignal) {
    this.endpoint = endpoint
    this.signal = signal
  }

  async #call(
    method: string,
    path: string,
    init: { body?: Buffer | string; headers?: Record<string, string>; timeoutMs?: number; dispatcher?: Dispatcher } = {},
  ): Promise<SandboxResponse> {
    let res: SandboxResponse
    try {
      res = await fetch(`${this.endpoint.url}${path}`, {
        method,
        headers: { authorization: `Bearer ${this.endpoint.token}`, ...init.headers },
        ...(init.body !== undefined ? { body: init.body } : {}),
        ...(init.dispatcher ? { dispatcher: init.dispatcher } : {}),
        signal: AbortSignal.any([this.signal, AbortSignal.timeout(init.timeoutMs ?? REQUEST_MS)]),
      })
    } catch (err) {
      throw new SandboxError(`the sandbox did not answer ${method} ${path}: ${err instanceof Error ? err.message : String(err)}`)
    }
    if (!res.ok) {
      const text = await bodyHead(res, ERROR_BODY_MAX_BYTES).catch(() => '')
      throw new SandboxError(`the sandbox answered ${res.status} to ${method} ${path}: ${text}`)
    }
    return res
  }

  /** The pod's manifest, read under JSON_MAX_BYTES; its entries are checked by the sync (checkedManifest). */
  async manifest(): Promise<Manifest> {
    const raw = await cappedJson(await this.#call('GET', '/v1/manifest'), JSON_MAX_BYTES, 'manifest')
    if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) throw new SandboxError("the sandbox's manifest is not an object")
    return raw as Manifest
  }

  async put(rel: string, bytes: Buffer): Promise<void> {
    await this.#call('PUT', `/v1/files/${encodeURI(rel)}`, {
      body: bytes,
      headers: { 'content-type': 'application/octet-stream', [SHA_HEADER]: createHash('sha256').update(bytes).digest('hex') },
    })
  }

  /**
   * The file's bytes, read as a stream and stopped once past `max` (a SandboxTooLarge): the size
   * the sandbox reports is never trusted.
   */
  async get(rel: string, max: number): Promise<Buffer> {
    const res = await this.#call('GET', `/v1/files/${encodeURI(rel)}`)
    const chunks: Buffer[] = []
    let size = 0
    const reader = res.body?.getReader()
    if (reader) {
      for (;;) {
        const { done, value } = await reader.read()
        if (done) break
        size += value.byteLength
        if (size > max) {
          await reader.cancel().catch(() => {})
          throw new SandboxTooLarge(`the sandbox's ${rel} is past ${max} bytes`)
        }
        chunks.push(Buffer.from(value))
      }
    }
    const bytes = Buffer.concat(chunks)
    if (res.headers.get(SHA_HEADER) !== createHash('sha256').update(bytes).digest('hex')) {
      throw new SandboxError(`the sandbox's copy of ${rel} changed while it was read`)
    }
    return bytes
  }

  /** The pod's Kept (protocol.ts KeptRecord), its entries checked: a command controls what the pod says. */
  async kept(): Promise<Kept> {
    const raw = await cappedJson(await this.#call('GET', '/v1/kept'), JSON_MAX_BYTES, 'kept record')
    const out: Kept = new Map()
    if (typeof raw !== 'object' || raw === null) return out
    for (const [rel, value] of Object.entries(raw as Record<string, unknown>)) {
      if (syncPath(rel) && keptValue(value)) out.set(rel, value ?? undefined)
    }
    return out
  }

  async setKept(kept: Kept): Promise<void> {
    const record: KeptRecord = Object.fromEntries([...kept].map(([rel, was]) => [rel, was ?? null]))
    await this.#call('PUT', '/v1/kept', { body: JSON.stringify(record), headers: { 'content-type': 'application/json' } })
  }

  async delete(rel: string): Promise<void> {
    await this.#call('DELETE', `/v1/files/${encodeURI(rel)}`)
  }

  /**
   * Runs a command. The server answers only once it ends (up to MAX_TIMEOUT_MS), past undici's
   * default 300 s headers and body timeouts, so the request has a dispatcher of its own whose
   * timeouts outlast `timeoutMs` (`slackMs` is for tests), closed once the answer is read.
   */
  async exec(shell: Shell, command: string, timeoutMs: number, slackMs = EXEC_SLACK_MS): Promise<ExecResult> {
    const waitMs = timeoutMs + slackMs
    const dispatcher = new Agent({ headersTimeout: waitMs + slackMs, bodyTimeout: waitMs + slackMs })
    try {
      const res = await this.#call('POST', '/v1/exec', {
        body: JSON.stringify({ shell, command, timeout_ms: timeoutMs }),
        headers: { 'content-type': 'application/json' },
        timeoutMs: waitMs,
        dispatcher,
      })
      return checkedExecResult(await cappedJson(res, JSON_MAX_BYTES, 'exec result'))
    } finally {
      await dispatcher.close().catch(() => {})
    }
  }
}

/** The workspace caps the sync keeps to (workspace/store.ts `limits`). */
export type SyncLimits = { fileMaxBytes: number; workspaceMaxBytes: number }

/** What one sync back did. */
export type SyncBack = { written: string[]; removed: string[]; left: { path: string; reason: string }[] }

/**
 * The files a sync back left in the pod past a cap, by path: what the session directory held
 * at that path then (its sha256, past the per-file cap too, or undefined for none). Until
 * the session directory's copy changes, a sync in neither sends nor deletes such a path, so
 * the file stays in the pod for as long as the pod lasts. The pod itself keeps the record
 * (GET and PUT /v1/kept), so a sync in on any worker sees it; a new pod starts with none.
 */
export type Kept = Map<string, string | undefined>

/**
 * What a sync in saw: the pod's files after it, the paths it named that never sync, and the
 * session's files it did not send (past the per-file cap; the pod's copy, if any, deleted).
 */
export type SyncedIn = { files: Map<string, string>; unsynced: Set<string>; notSent: string[] }

/** What Kept records for a session file it could not hash (it went away while read). */
const LARGE = 'large'

type LocalFile = { sha256: string; bytes: Buffer } | { sha256: string; tooLarge: true }

/**
 * Each worker file's sha256, by path: the session directory's files that sync. A file past the
 * per-file cap is hashed as a stream, never held, so Kept sees when it changes.
 */
async function localManifest(dir: string, limits: SyncLimits): Promise<Map<string, LocalFile>> {
  const out = new Map<string, LocalFile>()
  for (const rel of (await dirFiles(dir)).keys()) {
    if (!syncPath(rel)) continue
    const bytes = await readDirFile(dir, rel, limits.fileMaxBytes)
    if (!bytes) {
      out.set(rel, { sha256: (await dirFileSha256(dir, rel)) ?? LARGE, tooLarge: true })
      continue
    }
    out.set(rel, { sha256: createHash('sha256').update(bytes).digest('hex'), bytes })
  }
  return out
}

/**
 * Makes the pod's files match the session directory's: each changed file sent, each one the
 * directory no longer has deleted there, except the `kept` ones (see Kept). A session file past
 * the per-file cap is not sent, and the pod's copy of it is deleted, so a command never reads an
 * older copy as if it were the session's; it is named in `notSent`. A kept path is the exception:
 * the pod's own copy stays, and the path is not named. Returns the pod's manifest
 * as it is then.
 */
export async function syncIn(client: SandboxClient, dir: string, limits: SyncLimits, kept: Kept = new Map()): Promise<SyncedIn> {
  const local = await localManifest(dir, limits)
  const remote = await checkedManifest(client)
  const there = new Map(remote.files.map((f) => [f.path, f.sha256]))
  const stays = new Set<string>()
  for (const [rel, was] of kept) {
    const entry = local.get(rel)
    const now = entry?.sha256
    // Gone from the pod (a new pod), or changed in the session since: the session's copy counts again.
    if (!there.has(rel) || now !== was) kept.delete(rel)
    else stays.add(rel)
  }
  const notSent: string[] = []
  for (const [rel, entry] of local) {
    if (stays.has(rel)) continue
    if ('tooLarge' in entry) {
      notSent.push(rel)
      if (there.has(rel)) {
        await client.delete(rel)
        there.delete(rel)
      }
      continue
    }
    if (there.get(rel) !== entry.sha256) {
      await client.put(rel, entry.bytes)
      there.set(rel, entry.sha256)
    }
  }
  for (const rel of [...there.keys()]) {
    if (!syncPath(rel) || stays.has(rel)) continue
    if (local.get(rel) === undefined) {
      await client.delete(rel)
      there.delete(rel)
    }
  }
  return { files: there, unsynced: new Set(remote.unsynced), notSent }
}

/** The session directory's copy at `rel` as Kept records it. */
async function localState(dir: string, rel: string, limits: SyncLimits): Promise<string | undefined> {
  if (!(await dirFiles(dir)).has(rel)) return undefined
  const bytes = await readDirFile(dir, rel, limits.fileMaxBytes)
  return bytes ? createHash('sha256').update(bytes).digest('hex') : ((await dirFileSha256(dir, rel)) ?? LARGE)
}

/**
 * Brings back what the command changed: each file new or changed in the pod since `before` is
 * written into the session directory (never through a link), each one it deleted is removed. A
 * file past `fileMaxBytes`, or past what `workspaceMaxBytes` leaves for this call, stays in the
 * pod and is recorded in `kept`. A path the command made that never syncs is named in `left` too.
 * A truncated manifest removes nothing.
 */
export async function syncBack(client: SandboxClient, dir: string, before: SyncedIn, limits: SyncLimits, kept: Kept = new Map()): Promise<SyncBack> {
  const after = await checkedManifest(client)
  const out: SyncBack = { written: [], removed: [], left: [] }
  for (const path of after.bad) out.left.push({ path, reason: 'it is not a workspace path that syncs' })
  for (const path of after.unsynced) {
    if (before.unsynced.has(path)) continue
    out.left.push({ path, reason: UNSYNCED_REASON })
  }
  // What the directory may still take: the cap less what it holds now. A file brought back over an
  // older copy is counted whole, so the budget only errs on the small side.
  let budget = Math.max(limits.workspaceMaxBytes - (await dirBytes(dir)), 0)
  for (const file of after.files) {
    if (before.files.get(file.path) === file.sha256) continue
    const max = Math.min(limits.fileMaxBytes, budget)
    let bytes: Buffer
    try {
      bytes = await client.get(file.path, max)
    } catch (err) {
      if (err instanceof SandboxTooLarge) {
        const cap =
          max === limits.fileMaxBytes
            ? `it is past the workspace's per-file cap of ${limits.fileMaxBytes} bytes`
            : `it would take the session directory past the workspace's cap of ${limits.workspaceMaxBytes} bytes`
        out.left.push({ path: file.path, reason: `${cap}; ${KEPT_NOTE}` })
        kept.set(file.path, await localState(dir, file.path, limits))
      } else {
        // Not kept: the next command's sync in makes the pod match the session directory again.
        const why = err instanceof Error ? err.message : String(err)
        out.left.push({ path: file.path, reason: `it could not be read back (${why}); the next command's sync removes it from the sandbox` })
      }
      continue
    }
    budget -= bytes.length
    await writeDirFile(dir, file.path, bytes)
    kept.delete(file.path)
    out.written.push(file.path)
  }
  if (!after.truncated) {
    const now = new Set(after.files.map((f) => f.path))
    for (const rel of before.files.keys()) {
      if (now.has(rel) || !syncPath(rel)) continue
      await removeDirFile(dir, rel)
      kept.delete(rel)
      out.removed.push(rel)
    }
  }
  return out
}

/** What becomes of a file left in the sandbox past a cap. */
export const KEPT_NOTE =
  "it stays in the sandbox pod while the pod lasts (until the session's own copy at that path changes); a new pod starts from the session's files"

/** Why a path the sandbox names in `unsynced` is not in the session. */
export const UNSYNCED_REASON =
  'its path never comes back to the session: only names of letters, digits, "_", "." and "-" that do not start with ".", ' +
  'at most 4 levels deep and outside node_modules and __pycache__, sync; it stays in the sandbox pod while the pod lasts'

/** One command's run, with its sync: `notSent` names the session's files the command did not see (SyncedIn). */
export type ShellRun = { result: ExecResult; back: SyncBack; notSent: string[] }

/** Each session's calls, one at a time on this worker: a sync never runs into another. */
const chains = new Map<string, Promise<unknown>>()

function serial<T>(sessionId: string, work: () => Promise<T>): Promise<T> {
  const run = (chains.get(sessionId) ?? Promise.resolve()).then(work, work)
  const settled = run.catch(() => {})
  chains.set(sessionId, settled)
  void settled.then(() => {
    if (chains.get(sessionId) === settled) chains.delete(sessionId)
  })
  return run
}

/** Runs `command` in the session's sandbox: the directory synced in, the command run, its changes synced back. */
export function runInSandbox(
  options: { sandboxes: Sandboxes; sessionId: string; dir: string; limits: SyncLimits; signal: AbortSignal },
  shell: Shell,
  command: string,
  timeoutMs: number,
): Promise<ShellRun> {
  return serial(options.sessionId, async () => {
    // A pod known from an earlier call may have ended since (idle): a failure before the command
    // runs looks for the session's pod again, once.
    for (let attempt = 0; ; attempt++) {
      const endpoint = await options.sandboxes.acquire(options.sessionId, options.signal)
      const client = new SandboxClient(endpoint, options.signal)
      let kept: Kept
      let before: SyncedIn
      try {
        kept = await client.kept()
        before = await syncIn(client, options.dir, options.limits, kept)
      } catch (err) {
        if (!(err instanceof SandboxError)) throw err
        options.sandboxes.release?.(options.sessionId, endpoint)
        if (attempt === 0 && !options.signal.aborted) continue
        throw err
      }
      try {
        const result = await client.exec(shell, command, timeoutMs)
        const back = await syncBack(client, options.dir, before, options.limits, kept)
        await client.setKept(kept)
        return { result, back, notSent: before.notSent }
      } catch (err) {
        if (err instanceof SandboxError) options.sandboxes.release?.(options.sessionId, endpoint)
        throw err
      }
    }
  })
}
