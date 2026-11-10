import { createHash } from 'node:crypto'
import { dirBytes, dirFiles, readDirFile, removeDirFile, writeDirFile } from '../workspace/directory.js'
import { type ExecResult, type Manifest, SHA_HEADER, type Shell, syncPath } from './protocol.js'

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

/** The sandbox's manifest, its entries at sync paths only: a command controls what it says (`bad` lists the rest). */
async function checkedManifest(client: SandboxClient): Promise<{ files: Manifest['files']; truncated: boolean; bad: string[] }> {
  const raw = await client.manifest()
  const files: Manifest['files'] = []
  const bad: string[] = []
  for (const f of Array.isArray(raw?.files) ? raw.files : []) {
    const ok =
      typeof f?.path === 'string' && syncPath(f.path) && typeof f.sha256 === 'string' && /^[0-9a-f]{64}$/.test(f.sha256) && Number.isSafeInteger(f.size)
    if (ok) files.push(f)
    else bad.push(typeof f?.path === 'string' ? f.path : JSON.stringify(f).slice(0, 200))
  }
  return { files, truncated: raw?.truncated === true || bad.length > 0, bad }
}

const REQUEST_MS = 60_000

export class SandboxClient {
  readonly endpoint: SandboxEndpoint
  readonly signal: AbortSignal

  constructor(endpoint: SandboxEndpoint, signal: AbortSignal) {
    this.endpoint = endpoint
    this.signal = signal
  }

  async #call(method: string, path: string, init: { body?: Buffer | string; headers?: Record<string, string>; timeoutMs?: number } = {}): Promise<Response> {
    let res: Response
    try {
      res = await fetch(`${this.endpoint.url}${path}`, {
        method,
        headers: { authorization: `Bearer ${this.endpoint.token}`, ...init.headers },
        ...(init.body !== undefined ? { body: init.body } : {}),
        signal: AbortSignal.any([this.signal, AbortSignal.timeout(init.timeoutMs ?? REQUEST_MS)]),
      })
    } catch (err) {
      throw new SandboxError(`the sandbox did not answer ${method} ${path}: ${err instanceof Error ? err.message : String(err)}`)
    }
    if (!res.ok) {
      const text = await res.text().catch(() => '')
      throw new SandboxError(`the sandbox answered ${res.status} to ${method} ${path}: ${text.slice(0, 500)}`)
    }
    return res
  }

  async manifest(): Promise<Manifest> {
    return (await (await this.#call('GET', '/v1/manifest')).json()) as Manifest
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

  async delete(rel: string): Promise<void> {
    await this.#call('DELETE', `/v1/files/${encodeURI(rel)}`)
  }

  async exec(shell: Shell, command: string, timeoutMs: number): Promise<ExecResult> {
    const res = await this.#call('POST', '/v1/exec', {
      body: JSON.stringify({ shell, command, timeout_ms: timeoutMs }),
      headers: { 'content-type': 'application/json' },
      timeoutMs: timeoutMs + 30_000,
    })
    return (await res.json()) as ExecResult
  }
}

/** The workspace caps the sync keeps to (workspace/store.ts `limits`). */
export type SyncLimits = { fileMaxBytes: number; workspaceMaxBytes: number }

/** What one sync back did. */
export type SyncBack = { written: string[]; removed: string[]; left: { path: string; reason: string }[] }

/** Each worker file's sha256, by path: the session directory's files that sync. */
async function localManifest(dir: string, limits: SyncLimits): Promise<Map<string, { sha256: string; bytes: Buffer } | { tooLarge: true }>> {
  const out = new Map<string, { sha256: string; bytes: Buffer } | { tooLarge: true }>()
  for (const rel of (await dirFiles(dir)).keys()) {
    if (!syncPath(rel)) continue
    const bytes = await readDirFile(dir, rel, limits.fileMaxBytes)
    if (!bytes) {
      out.set(rel, { tooLarge: true })
      continue
    }
    out.set(rel, { sha256: createHash('sha256').update(bytes).digest('hex'), bytes })
  }
  return out
}

/**
 * Makes the pod's files match the session directory's: each changed file sent, each one the
 * directory no longer has deleted there. Returns the pod's manifest as it is then.
 */
export async function syncIn(client: SandboxClient, dir: string, limits: SyncLimits): Promise<Map<string, string>> {
  const local = await localManifest(dir, limits)
  const remote = await checkedManifest(client)
  const there = new Map(remote.files.map((f) => [f.path, f.sha256]))
  for (const [rel, entry] of local) {
    if ('tooLarge' in entry) continue
    if (there.get(rel) !== entry.sha256) {
      await client.put(rel, entry.bytes)
      there.set(rel, entry.sha256)
    }
  }
  for (const rel of [...there.keys()]) {
    if (!syncPath(rel)) continue
    const here = local.get(rel)
    if (here === undefined) {
      await client.delete(rel)
      there.delete(rel)
    }
  }
  return there
}

/**
 * Brings back what the command changed: each file new or changed in the pod since `before` is
 * written into the session directory (never through a link), each one it deleted is removed. A
 * file past `fileMaxBytes`, or past what `workspaceMaxBytes` leaves for this call, stays in the pod.
 * A truncated manifest removes nothing.
 */
export async function syncBack(client: SandboxClient, dir: string, before: Map<string, string>, limits: SyncLimits): Promise<SyncBack> {
  const after = await checkedManifest(client)
  const out: SyncBack = { written: [], removed: [], left: [] }
  for (const path of after.bad) out.left.push({ path, reason: 'it is not a workspace path that syncs' })
  // What the directory may still take: the cap less what it holds now. A file brought back over an
  // older copy is counted whole, so the budget only errs on the small side.
  let budget = Math.max(limits.workspaceMaxBytes - (await dirBytes(dir)), 0)
  for (const file of after.files) {
    if (before.get(file.path) === file.sha256) continue
    const max = Math.min(limits.fileMaxBytes, budget)
    let bytes: Buffer
    try {
      bytes = await client.get(file.path, max)
    } catch (err) {
      const reason =
        err instanceof SandboxTooLarge
          ? max === limits.fileMaxBytes
            ? `it is past the workspace's per-file cap of ${limits.fileMaxBytes} bytes`
            : `it would take the session directory past the workspace's cap of ${limits.workspaceMaxBytes} bytes`
          : err instanceof Error
            ? err.message
            : String(err)
      out.left.push({ path: file.path, reason })
      continue
    }
    budget -= bytes.length
    await writeDirFile(dir, file.path, bytes)
    out.written.push(file.path)
  }
  if (!after.truncated) {
    const now = new Set(after.files.map((f) => f.path))
    for (const rel of before.keys()) {
      if (now.has(rel) || !syncPath(rel)) continue
      await removeDirFile(dir, rel)
      out.removed.push(rel)
    }
  }
  return out
}

/** One command's run, with its sync. */
export type ShellRun = { result: ExecResult; back: SyncBack }

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
      let before: Map<string, string>
      try {
        before = await syncIn(client, options.dir, options.limits)
      } catch (err) {
        if (!(err instanceof SandboxError)) throw err
        options.sandboxes.release?.(options.sessionId, endpoint)
        if (attempt === 0 && !options.signal.aborted) continue
        throw err
      }
      try {
        const result = await client.exec(shell, command, timeoutMs)
        const back = await syncBack(client, options.dir, before, options.limits)
        return { result, back }
      } catch (err) {
        if (err instanceof SandboxError) options.sandboxes.release?.(options.sessionId, endpoint)
        throw err
      }
    }
  })
}
