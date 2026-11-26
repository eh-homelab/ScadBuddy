import { createHash } from 'node:crypto'
import { Agent, type Dispatcher, fetch } from 'undici'
import { dirBytes, dirFileSha256, dirFiles, readDirFile, removeDirFile, writeDirFile } from '../workspace/directory.js'
import {
  type ExecResult,
  type KeptRecord,
  keptValue,
  type Manifest,
  SHA_HEADER,
  type Shell,
  SLOT_ID_RE,
  type SlotStarted,
  type SlotStatus,
  syncPath,
  UNSYNCED_MAX,
  WATCH_CHUNK_BYTES,
  WATCH_HEARTBEAT_MS,
} from './protocol.js'

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
  /**
   * The session's pod if it has one now, never a new one: a background slot's watch looks for the
   * pod its slot runs in, and a new pod would not hold it (#5916). Without it, `acquire` is used.
   */
  find?(sessionId: string, signal: AbortSignal): Promise<SandboxEndpoint | undefined>
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

/** The longest watch line read: a frame's JSON, never its raw bytes (a status's is the longest, well under this). */
export const WATCH_LINE_MAX_BYTES = 4096

/** An output file's path as the pod names it: shown to the model, so a plain absolute path only. */
const outputFile = (value: unknown): value is string => typeof value === 'string' && /^\/[A-Za-z0-9_./-]{1,200}$/.test(value) && !value.includes('..')

/** A slot's status as the pod sent it, checked field by field. */
export function checkedSlotStatus(raw: unknown): SlotStatus {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const ok =
    typeof r.id === 'string' &&
    SLOT_ID_RE.test(r.id) &&
    typeof r.running === 'boolean' &&
    (r.exit_code === null || (typeof r.exit_code === 'number' && Number.isInteger(r.exit_code))) &&
    (r.signal === null || typeof r.signal === 'string') &&
    typeof r.timed_out === 'boolean' &&
    typeof r.stopped === 'boolean' &&
    isCount(r.bytes) &&
    outputFile(r.output_file)
  if (!ok) throw new SandboxError('the sandbox sent a slot status that is not one')
  return {
    id: r.id as string,
    running: r.running as boolean,
    exit_code: r.exit_code as number | null,
    signal: r.signal === null ? null : shown(r.signal).slice(0, 32),
    timed_out: r.timed_out as boolean,
    stopped: r.stopped as boolean,
    bytes: r.bytes as number,
    output_file: r.output_file as string,
  }
}

/** One event of a slot's watch: output (bytes), a range lost, its end, or `gone` (the pod has no such slot). */
export type SlotEvent =
  | { type: 'data'; at: number; stream: 'stdout' | 'stderr'; data: Buffer }
  | { type: 'lost'; from: number; to: number }
  | { type: 'end'; status: SlotStatus }
  | { type: 'gone' }

/** A `data` line's header: its raw bytes follow it. */
type DataHeader = { type: 'header'; at: number; stream: 'stdout' | 'stderr'; bytes: number }

/** One watch line, checked: the pod controls it. A heartbeat is `{ type: 'beat' }`; a `data` line is the header of the raw bytes after it. */
export function checkedWatchLine(line: Buffer): Exclude<SlotEvent, { type: 'data' }> | DataHeader | { type: 'beat' } {
  let raw: unknown
  try {
    raw = JSON.parse(line.toString('utf8'))
  } catch {
    throw new SandboxError('the sandbox sent a watch line that is not JSON')
  }
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  if (r.type === 'beat') return { type: 'beat' }
  if (r.type === 'end') return { type: 'end', status: checkedSlotStatus(r.status) }
  if (r.type === 'lost' && isCount(r.from) && isCount(r.to) && (r.to as number) >= (r.from as number)) {
    return { type: 'lost', from: r.from as number, to: r.to as number }
  }
  if (r.type === 'data' && isCount(r.at) && (r.stream === 'stdout' || r.stream === 'stderr') && isCount(r.bytes) && (r.bytes as number) <= WATCH_CHUNK_BYTES) {
    return { type: 'header', at: r.at as number, stream: r.stream, bytes: r.bytes as number }
  }
  throw new SandboxError('the sandbox sent a watch line that is not one')
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

  /** The pod's base (protocol.ts BaseRecord), its entries checked; null when the pod has none. */
  async base(): Promise<Map<string, string> | null> {
    const raw = await cappedJson(await this.#call('GET', '/v1/base'), JSON_MAX_BYTES, 'base record')
    const record = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
    if (typeof record.base !== 'object' || record.base === null || Array.isArray(record.base)) return null
    const out = new Map<string, string>()
    for (const [rel, sha] of Object.entries(record.base as Record<string, unknown>)) {
      if (syncPath(rel) && typeof sha === 'string' && /^[0-9a-f]{64}$/.test(sha)) out.set(rel, sha)
    }
    return out
  }

  async setBase(base: ReadonlyMap<string, string> | null): Promise<void> {
    const body = { base: base === null ? null : Object.fromEntries(base) }
    await this.#call('PUT', '/v1/base', { body: JSON.stringify(body), headers: { 'content-type': 'application/json' } })
  }

  /** Starts a background slot; its id is checked, since the pod controls it. */
  async startSlot(shell: Shell, command: string, timeoutMs: number): Promise<SlotStarted> {
    const res = await this.#call('POST', '/v1/slots', {
      body: JSON.stringify({ shell, command, timeout_ms: timeoutMs }),
      headers: { 'content-type': 'application/json' },
    })
    const raw = (await cappedJson(res, 64 * 1024, 'slot')) as Record<string, unknown> | null
    if (typeof raw?.id !== 'string' || !SLOT_ID_RE.test(raw.id) || !outputFile(raw.output_file)) {
      throw new SandboxError('the sandbox sent a slot that is not one')
    }
    return { id: raw.id, output_file: raw.output_file }
  }

  /** A slot's status, or undefined when the pod has no such slot (404). */
  async slotStatus(id: string): Promise<SlotStatus | undefined> {
    if (!SLOT_ID_RE.test(id)) return undefined
    try {
      return checkedSlotStatus(await cappedJson(await this.#call('GET', `/v1/slots/${id}`), 64 * 1024, 'slot status'))
    } catch (err) {
      if (err instanceof SandboxError && / answered 404 /.test(err.message)) return undefined
      throw err
    }
  }

  /** Stops a slot (its process group killed) and forgets it; its last status, or undefined when the pod had no such slot. */
  async stopSlot(id: string): Promise<SlotStatus | undefined> {
    if (!SLOT_ID_RE.test(id)) return undefined
    try {
      return checkedSlotStatus(await cappedJson(await this.#call('DELETE', `/v1/slots/${id}`), 64 * 1024, 'slot status'))
    } catch (err) {
      if (err instanceof SandboxError && / answered 404 /.test(err.message)) return undefined
      throw err
    }
  }

  /**
   * A slot's output from byte `from`, as checked events, until its end. The pod sends a heartbeat
   * every WATCH_HEARTBEAT_MS; a watch that hears nothing for three of them fails (a SandboxError),
   * as does a line past WATCH_LINE_MAX_BYTES or one that is not a watch line.
   */
  async *watch(id: string, from: number, heartbeatMs = WATCH_HEARTBEAT_MS): AsyncGenerator<SlotEvent> {
    if (!SLOT_ID_RE.test(id)) throw new SandboxError('not a slot id')
    const quiet = new AbortController()
    let timer: NodeJS.Timeout | undefined
    const arm = () => {
      clearTimeout(timer)
      timer = setTimeout(() => quiet.abort(new Error('the sandbox sent nothing, not even a heartbeat')), heartbeatMs * 3)
    }
    // No overall timeout: a watch lasts as long as its slot, and its heartbeats show it is alive.
    const dispatcher = new Agent({ headersTimeout: heartbeatMs * 3, bodyTimeout: heartbeatMs * 3 })
    const signal = AbortSignal.any([this.signal, quiet.signal])
    arm()
    let res: SandboxResponse
    try {
      res = await fetch(`${this.endpoint.url}/v1/slots/${id}/watch?from=${Math.max(0, Math.floor(from))}`, {
        headers: { authorization: `Bearer ${this.endpoint.token}` },
        dispatcher,
        signal,
      })
    } catch (err) {
      clearTimeout(timer)
      await dispatcher.close().catch(() => {})
      throw new SandboxError(`the sandbox did not answer the watch of slot ${id}: ${err instanceof Error ? err.message : String(err)}`)
    }
    if (res.status === 404) {
      clearTimeout(timer)
      await res.body?.cancel().catch(() => {})
      await dispatcher.close().catch(() => {})
      yield { type: 'gone' }
      return
    }
    const reader = res.body?.getReader()
    try {
      if (!res.ok || !reader) throw new SandboxError(`the sandbox answered ${res.status} to the watch of slot ${id}`)
      let pending = Buffer.alloc(0)
      // A data line's header, while its raw bytes are still to come.
      let header: DataHeader | undefined
      for (;;) {
        let read
        try {
          read = await reader.read()
        } catch (err) {
          throw new SandboxError(`the watch of slot ${id} broke: ${quiet.signal.aborted ? String(quiet.signal.reason) : err instanceof Error ? err.message : String(err)}`)
        }
        if (read.done) throw new SandboxError(`the watch of slot ${id} ended before the slot did`)
        arm()
        pending = Buffer.concat([pending, Buffer.from(read.value)])
        for (;;) {
          if (header) {
            if (pending.length < header.bytes) break
            const data = Buffer.from(pending.subarray(0, header.bytes))
            pending = pending.subarray(header.bytes)
            yield { type: 'data', at: header.at, stream: header.stream, data }
            header = undefined
            continue
          }
          const nl = pending.indexOf(10)
          if (nl < 0) break
          const line = pending.subarray(0, nl)
          pending = pending.subarray(nl + 1)
          const event = checkedWatchLine(line)
          if (event.type === 'beat') continue
          if (event.type === 'header') {
            header = event
            continue
          }
          yield event
          if (event.type === 'end') return
        }
        if (!header && pending.length > WATCH_LINE_MAX_BYTES) throw new SandboxError(`the watch of slot ${id} sent a line past ${WATCH_LINE_MAX_BYTES} bytes`)
      }
    } finally {
      clearTimeout(timer)
      await reader?.cancel().catch(() => {})
      await dispatcher.close().catch(() => {})
    }
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

/**
 * What one sync back did. `after`: the pod's files as it left them, the next base (protocol.ts
 * BaseRecord); undefined when the manifest was truncated, so no base can be trusted.
 */
export type SyncBack = { written: string[]; removed: string[]; left: { path: string; reason: string }[]; after?: Map<string, string> | undefined }

/**
 * The files a sync back left in the pod past a cap, by path: what the session directory held
 * at that path then (its sha256, past the per-file cap too; `large` when it could not be hashed;
 * or undefined for none). Until
 * the session directory's copy changes, a sync in neither sends nor deletes such a path, so
 * the file stays in the pod for as long as the pod lasts. The pod itself keeps the record
 * (GET and PUT /v1/kept), so a sync in on any worker sees it; a new pod starts with none.
 */
export type Kept = Map<string, string | undefined>

/**
 * What a sync in saw: the pod's files after it, the paths it named that never sync, and the
 * session's files it did not send (past the per-file cap; the pod's copy, if any, deleted).
 * With a base (a background slot ran in the pod): `pulled`, what it first brought back of what
 * changed in the pod since the last sync, and `overwritten`, the paths that changed in both
 * places, where the session's copy replaced the pod's.
 */
export type SyncedIn = { files: Map<string, string>; unsynced: Set<string>; notSent: string[]; pulled?: SyncBack; overwritten?: string[] }

/**
 * What Kept records for a session file it could not hash (it went away, or could not be opened),
 * and what the manifest holds for a file past the cap that is not kept.
 */
const LARGE = 'large'

type LocalFile = { sha256: string; bytes: Buffer } | { sha256: string; tooLarge: true }

/**
 * Each worker file's sha256, by path: the session directory's files that sync. A file past the
 * per-file cap is hashed as a stream, never held, and only when it is in `kept`, the one place
 * its sha256 is compared (so Kept sees when it changes); any other is recorded as `large`, and
 * a large file costs no read on every command.
 */
async function localManifest(dir: string, limits: SyncLimits, kept: Kept): Promise<Map<string, LocalFile>> {
  const out = new Map<string, LocalFile>()
  for (const rel of (await dirFiles(dir)).keys()) {
    if (!syncPath(rel)) continue
    const bytes = await readDirFile(dir, rel, limits.fileMaxBytes)
    if (!bytes) {
      const sha256 = kept.has(rel) ? ((await dirFileSha256(dir, rel)) ?? LARGE) : LARGE
      out.set(rel, { sha256, tooLarge: true })
      continue
    }
    out.set(rel, { sha256: createHash('sha256').update(bytes).digest('hex'), bytes })
  }
  return out
}

/** The paths a kept record still holds in the pod: kept, and neither gone from the pod nor changed in the session since (see Kept). */
function keptStays(kept: Kept, local: Map<string, LocalFile>, there: Map<string, string>): Set<string> {
  const stays = new Set<string>()
  for (const [rel, was] of kept) {
    const now = local.get(rel)?.sha256
    // Gone from the pod (a new pod), or changed in the session since: the session's copy counts again.
    if (!there.has(rel) || now !== was) kept.delete(rel)
    else stays.add(rel)
  }
  return stays
}

/**
 * Brings back what changed in the pod since `base` (a background slot wrote it, #5916, #7310)
 * while the session's copy did not: each such file is written into the session directory, under
 * the same caps as a sync back (past one, it stays in the pod and is kept), and each one the pod
 * deleted is removed. `local` and `stays` are updated to match, so the sync in that follows
 * sends none of it back. A path that changed in both places is `overwritten`: the session's copy
 * counts. A truncated manifest removes nothing. `settled`: each path's base from now on (undefined: none).
 */
async function pullChanges(
  client: SandboxClient,
  dir: string,
  limits: SyncLimits,
  kept: Kept,
  base: ReadonlyMap<string, string>,
  remote: { files: Map<string, string>; truncated: boolean },
  local: Map<string, LocalFile>,
  stays: Set<string>,
): Promise<{ back: SyncBack; overwritten: string[]; settled: Map<string, string | undefined> }> {
  const back: SyncBack = { written: [], removed: [], left: [] }
  const overwritten: string[] = []
  const settled = new Map<string, string | undefined>()
  let budget = Math.max(limits.workspaceMaxBytes - (await dirBytes(dir)), 0)
  for (const rel of new Set([...remote.files.keys(), ...base.keys()])) {
    if (!syncPath(rel) || stays.has(rel)) continue
    const pod = remote.files.get(rel)
    const was = base.get(rel)
    if (pod === was) continue
    if (pod === undefined && remote.truncated) continue
    const now = local.get(rel)?.sha256
    if (now === pod) {
      settled.set(rel, pod)
      continue
    }
    if (now !== was) {
      overwritten.push(rel)
      continue
    }
    if (pod === undefined) {
      await removeDirFile(dir, rel)
      local.delete(rel)
      kept.delete(rel)
      back.removed.push(rel)
      settled.set(rel, undefined)
      continue
    }
    const max = Math.min(limits.fileMaxBytes, budget)
    let bytes: Buffer
    try {
      bytes = await client.get(rel, max)
    } catch (err) {
      if (!(err instanceof SandboxTooLarge)) {
        const why = err instanceof Error ? err.message : String(err)
        back.left.push({ path: rel, reason: `it could not be read back (${why}); the session's copy replaces it in the sandbox` })
        continue
      }
      const cap =
        max === limits.fileMaxBytes
          ? `it is past the workspace's per-file cap of ${limits.fileMaxBytes} bytes`
          : `it would take the session directory past the workspace's cap of ${limits.workspaceMaxBytes} bytes`
      back.left.push({ path: rel, reason: `${cap}; ${KEPT_NOTE}` })
      kept.set(rel, now)
      stays.add(rel)
      settled.set(rel, pod)
      continue
    }
    budget -= bytes.length
    await writeDirFile(dir, rel, bytes)
    local.set(rel, { sha256: createHash('sha256').update(bytes).digest('hex'), bytes })
    kept.delete(rel)
    back.written.push(rel)
    settled.set(rel, pod)
  }
  return { back, overwritten, settled }
}

/**
 * Makes the pod's files match the session directory's: each changed file sent, each one the
 * directory no longer has deleted there, except the `kept` ones (see Kept). A session file past
 * the per-file cap is not sent, and the pod's copy of it is deleted, so a command never reads an
 * older copy as if it were the session's; it is named in `notSent`. A kept path is the exception:
 * the pod's own copy stays, and the path is not named. With a `base` (the pod's, protocol.ts
 * BaseRecord), what changed in the pod since it is first brought back (pullChanges), so a
 * background slot's writes are never overwritten by an older session copy. Returns the pod's
 * manifest as it is then.
 */
export async function syncIn(
  client: SandboxClient,
  dir: string,
  limits: SyncLimits,
  kept: Kept = new Map(),
  base: ReadonlyMap<string, string> | null = null,
): Promise<SyncedIn> {
  const local = await localManifest(dir, limits, kept)
  const remote = await checkedManifest(client)
  const there = new Map(remote.files.map((f) => [f.path, f.sha256]))
  const stays = keptStays(kept, local, there)
  const pull = base ? await pullChanges(client, dir, limits, kept, base, { files: there, truncated: remote.truncated }, local, stays) : undefined
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
  return { files: there, unsynced: new Set(remote.unsynced), notSent, ...(pull ? { pulled: pull.back, overwritten: pull.overwritten } : {}) }
}

/**
 * Brings back what changed in the pod since its base, without sending anything (a background
 * slot's end, #5916, #7310): pullChanges into this worker's session directory. The base stays
 * as it was: the change has reached only this directory, not the record, so a sync in from
 * another worker's directory must still see it as the pod's and bring it back, never push its
 * older copy over it. Here the next sync in finds the directory and the pod alike and does
 * nothing. A pod with no base has had no slot, so there is nothing to bring back.
 */
export async function catchUp(client: SandboxClient, dir: string, limits: SyncLimits, kept: Kept): Promise<SyncBack | undefined> {
  const base = await client.base()
  if (base === null) return undefined
  const local = await localManifest(dir, limits, kept)
  const remote = await checkedManifest(client)
  const there = new Map(remote.files.map((f) => [f.path, f.sha256]))
  const stays = keptStays(kept, local, there)
  const pull = await pullChanges(client, dir, limits, kept, base, { files: there, truncated: remote.truncated }, local, stays)
  return pull.back
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
    out.after = new Map(after.files.map((f) => [f.path, f.sha256]))
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

/**
 * One command's run, with its sync: `notSent` names the session's files the command did not see
 * (SyncedIn), `pulled` what the sync in first brought back of a background slot's writes, and
 * `overwritten` the paths where the session's copy replaced a slot's (SyncedIn).
 */
export type ShellRun = { result: ExecResult; back: SyncBack; notSent: string[]; pulled?: SyncBack; overwritten?: string[] }

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

type SandboxCall = { sandboxes: Sandboxes; sessionId: string; dir: string; limits: SyncLimits; signal: AbortSignal }

/**
 * The session's pod, synced in from its directory (with the pod's base, so a background slot's
 * writes come back first), then `work`. A pod known from an earlier call may have ended since
 * (idle): a failure before `work` looks for the session's pod again, once.
 */
async function withSyncedPod<T>(
  options: SandboxCall,
  work: (client: SandboxClient, kept: Kept, base: Map<string, string> | null, before: SyncedIn) => Promise<T>,
): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    const endpoint = await options.sandboxes.acquire(options.sessionId, options.signal)
    const client = new SandboxClient(endpoint, options.signal)
    let kept: Kept
    let base: Map<string, string> | null
    let before: SyncedIn
    try {
      kept = await client.kept()
      base = await client.base()
      before = await syncIn(client, options.dir, options.limits, kept, base)
    } catch (err) {
      if (!(err instanceof SandboxError)) throw err
      options.sandboxes.release?.(options.sessionId, endpoint)
      if (attempt === 0 && !options.signal.aborted) continue
      throw err
    }
    try {
      return await work(client, kept, base, before)
    } catch (err) {
      if (err instanceof SandboxError) options.sandboxes.release?.(options.sessionId, endpoint)
      throw err
    }
  }
}

/** Runs `command` in the session's sandbox: the directory synced in, the command run, its changes synced back. */
export function runInSandbox(options: SandboxCall, shell: Shell, command: string, timeoutMs: number): Promise<ShellRun> {
  return serial(options.sessionId, () =>
    withSyncedPod(options, async (client, kept, base, before) => {
      const result = await client.exec(shell, command, timeoutMs)
      const back = await syncBack(client, options.dir, before, options.limits, kept)
      await client.setKept(kept)
      // The base is kept once a slot has run here (startInSandbox set it): the pod as this sync
      // left it. A truncated manifest gives none to trust, and the previous one stays: cleared, a
      // slot that still runs would have its writes taken for the session's to overwrite. The
      // command ran and its changes are in the session: a base that cannot be set is not its failure.
      if (base !== null && back.after) {
        await client.setBase(back.after).catch((err: unknown) => {
          console.warn(`session ${options.sessionId}: the sandbox's base could not be set after a command: ${err instanceof Error ? err.message : String(err)}`)
        })
      }
      return {
        result,
        back,
        notSent: before.notSent,
        ...(before.pulled ? { pulled: before.pulled } : {}),
        ...(before.overwritten?.length ? { overwritten: before.overwritten } : {}),
      }
    }),
  )
}

/** A background slot's start, with the sync before it (ShellRun's fields). */
export type SlotRun = { slot: SlotStarted; notSent: string[]; pulled?: SyncBack; overwritten?: string[] }

/**
 * Starts `command` as a background slot in the session's sandbox (#5916, #7310): the directory
 * synced in first, as for a foreground command, and the pod's base set to what the sync left, so
 * the slot's writes are told apart from then on and come back at the next sync (syncIn) or at
 * its end (catchUpInSandbox).
 */
export function startInSandbox(options: SandboxCall, shell: Shell, command: string, timeoutMs: number): Promise<SlotRun> {
  return serial(options.sessionId, () =>
    withSyncedPod(options, async (client, kept, _base, before) => {
      await client.setKept(kept)
      await client.setBase(before.files)
      const slot = await client.startSlot(shell, command, timeoutMs)
      return {
        slot,
        notSent: before.notSent,
        ...(before.pulled ? { pulled: before.pulled } : {}),
        ...(before.overwritten?.length ? { overwritten: before.overwritten } : {}),
      }
    }),
  )
}

/** The session's pod as it is now, never a new one (Sandboxes.find; `acquire` without it). */
export function findSandbox(sandboxes: Sandboxes, sessionId: string, signal: AbortSignal): Promise<SandboxEndpoint | undefined> {
  return sandboxes.find ? sandboxes.find(sessionId, signal) : sandboxes.acquire(sessionId, signal)
}

/**
 * What a background slot wrote, brought back into the session directory (catchUp), one at a
 * time with the session's other calls on this worker. Undefined when the session has no pod now.
 */
export function catchUpInSandbox(options: SandboxCall): Promise<SyncBack | undefined> {
  return serial(options.sessionId, async () => {
    const endpoint = await findSandbox(options.sandboxes, options.sessionId, options.signal)
    if (!endpoint) return undefined
    const client = new SandboxClient(endpoint, options.signal)
    const kept = await client.kept()
    const back = await catchUp(client, options.dir, options.limits, kept)
    await client.setKept(kept)
    return back
  })
}
