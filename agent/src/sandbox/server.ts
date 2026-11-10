import { spawn } from 'node:child_process'
import { createHash, timingSafeEqual } from 'node:crypto'
import { constants } from 'node:fs'
import { lstat, mkdir, open, readdir, readFile, rename, rm, unlink } from 'node:fs/promises'
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import path from 'node:path'
import {
  type ExecRequest,
  type ExecResult,
  MANIFEST_MAX_ENTRIES,
  type Manifest,
  type ManifestEntry,
  MAX_SEGMENTS,
  MAX_TIMEOUT_MS,
  SCRATCH_DIRS,
  SEGMENT_RE,
  SHA_HEADER,
  SHELLS,
  STREAM_MAX_BYTES,
  syncPath,
} from './protocol.js'

// The exec server in a session's sandbox pod (#5864, issue comment "Design"). It holds no secret:
// its token is one the agent made for this pod, which reaches only this pod. It runs each command
// in `work` (the session's files, synced in and out by the agent, client.ts) with a bare
// environment, kills the command's process group at its deadline and after it exits, and exits
// itself after `idleMs` with no request, so the pod ends and the agent's sweep deletes it.

export type SandboxServerOptions = {
  work: string
  token: string
  /** The command's HOME (a writable directory outside `work`). */
  home: string
  /** The egress proxy, `http://<address>:<port>`, given to commands as HTTPS_PROXY; none in tests. */
  proxy?: string | undefined
  /** How long the server waits with no request before `onIdle`. */
  idleMs: number
  onIdle: () => void
  /** Where the shells are found. */
  pathEnv?: string
  /**
   * `ulimit -u` for each command (protocol.ts MAX_PROCESSES in the pod, whose user namespace makes
   * it count the pod's processes only). Unset: none (a test on a shared machine, where the uid's
   * other processes count too).
   */
  maxProcesses?: number
  /**
   * After each command, kill every process of this uid but the server and PID 1 (`reapStrays`):
   * a command that left its process group (`setsid`, a double fork) must not outlive it. Set in
   * the pod (main.ts); never in a test on a shared machine, where it would kill the user's own.
   */
  reapUid?: number
}

/**
 * Kills every process whose real uid is `uid`, except PID 1 (tini), this process and `keep`, and
 * looks again until none is left (a process may fork while it is being killed; `ulimit -u` bounds
 * that), at most 50 rounds. Returns how many it killed. A sandbox runs one session's commands, one
 * at a time, so between two commands nothing of that uid but the server should run (#5864: the pod
 * cannot make a PID namespace per command, see the design comment).
 */
export async function reapStrays(options: {
  uid: number
  keep?: readonly number[]
  procRoot?: string
  kill?: (pid: number) => Promise<void> | void
}): Promise<number> {
  const root = options.procRoot ?? '/proc'
  const keep = new Set([1, process.pid, ...(options.keep ?? [])])
  const kill =
    options.kill ??
    ((pid: number) => {
      try {
        process.kill(pid, 'SIGKILL')
      } catch {
        // Gone already.
      }
    })
  let killed = 0
  for (let round = 0; round < 50; round++) {
    const found: number[] = []
    for (const name of await readdir(root).catch(() => [] as string[])) {
      if (!/^\d+$/.test(name)) continue
      const pid = Number(name)
      if (keep.has(pid)) continue
      const status = await readFile(path.join(root, name, 'status'), 'utf8').catch(() => '')
      const uid = /^Uid:\s+(\d+)/m.exec(status)?.[1]
      if (uid !== undefined && Number(uid) === options.uid) found.push(pid)
    }
    if (found.length === 0) break
    for (const pid of found) await kill(pid)
    killed += found.length
    // Killed processes leave /proc once reaped (tini reaps orphans); give them a moment.
    if (!options.kill) await new Promise((r) => setTimeout(r, 20))
  }
  return killed
}

const BODY_MAX_BYTES = 256 * 1024 * 1024

/** The environment each command gets: nothing of the server's own. */
export function commandEnv(options: Pick<SandboxServerOptions, 'home' | 'proxy' | 'pathEnv'>): Record<string, string> {
  return {
    PATH: options.pathEnv ?? '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
    HOME: options.home,
    LANG: 'C.UTF-8',
    TERM: 'dumb',
    // No telemetry call from pwsh or dotnet: it would only be refused at the proxy.
    POWERSHELL_TELEMETRY_OPTOUT: '1',
    DOTNET_CLI_TELEMETRY_OPTOUT: '1',
    POWERSHELL_UPDATECHECK: 'Off',
    ...(options.proxy ? { HTTPS_PROXY: options.proxy, https_proxy: options.proxy, ALL_PROXY: options.proxy, all_proxy: options.proxy } : {}),
  }
}

/** The argv that runs `command` in `shell` under the process cap. */
function argv(shell: ExecRequest['shell'], command: string, maxProcesses: number | undefined): string[] {
  const inner = shell === 'bash' ? ['bash', '-c', command] : ['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command]
  const cap = maxProcesses === undefined ? '' : `ulimit -u ${Math.floor(maxProcesses)} && `
  return ['-c', `${cap}exec "$@"`, 'sandbox', ...inner]
}

/** Runs one command; never rejects. */
export function runCommand(options: Pick<SandboxServerOptions, 'work' | 'home' | 'proxy' | 'pathEnv' | 'maxProcesses' | 'reapUid'>, request: ExecRequest): Promise<ExecResult> {
  const started = Date.now()
  return new Promise((resolve) => {
    const child = spawn('bash', argv(request.shell, request.command, options.maxProcesses), {
      cwd: options.work,
      env: commandEnv(options),
      detached: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    const streams = { stdout: { chunks: [] as Buffer[], kept: 0, total: 0 }, stderr: { chunks: [] as Buffer[], kept: 0, total: 0 } }
    const keep = (which: 'stdout' | 'stderr') => (chunk: Buffer) => {
      const s = streams[which]
      s.total += chunk.length
      const room = STREAM_MAX_BYTES - s.kept
      if (room <= 0) return
      const part = chunk.length > room ? chunk.subarray(0, room) : chunk
      s.chunks.push(part)
      s.kept += part.length
    }
    child.stdout.on('data', keep('stdout'))
    child.stderr.on('data', keep('stderr'))
    let timedOut = false
    const killGroup = () => {
      if (child.pid === undefined) return
      try {
        process.kill(-child.pid, 'SIGKILL')
      } catch {
        // The group is gone already.
      }
    }
    // The group, then (in the pod) every other process of the sandbox's uid: what left the group too.
    const killAll = () => {
      killGroup()
      if (options.reapUid !== undefined) void reapStrays({ uid: options.reapUid })
    }
    const timer = setTimeout(() => {
      timedOut = true
      killAll()
    }, request.timeout_ms)
    let spawnError: Error | undefined
    child.on('error', (err) => (spawnError = err))
    // What the command left running in its group goes as soon as the shell exits: it would hold
    // the pipes open, and its output would arrive after the result.
    child.on('exit', killAll)
    child.on('close', (code, signal) => {
      clearTimeout(timer)
      void finish(code, signal)
    })
    const finish = async (code: number | null, signal: NodeJS.Signals | null) => {
      // Nothing of the command runs on once its result is out: a second sweep after the pipes closed.
      if (options.reapUid !== undefined) await reapStrays({ uid: options.reapUid })
      resolve({
        exit_code: spawnError ? 127 : code,
        signal: signal ?? null,
        timed_out: timedOut,
        stdout: Buffer.concat(streams.stdout.chunks).toString('utf8'),
        stderr: spawnError ? `${spawnError.message}\n` : Buffer.concat(streams.stderr.chunks).toString('utf8'),
        stdout_bytes: streams.stdout.total,
        stderr_bytes: streams.stderr.total,
        duration_ms: Date.now() - started,
      })
    }
  })
}

/** A sha256 cache by (path, inode, size, mtime), so a manifest does not read every file again. */
type ShaCache = Map<string, { key: string; sha256: string }>

async function fileSha(abs: string): Promise<string | undefined> {
  let handle
  try {
    handle = await open(abs, constants.O_RDONLY | constants.O_NOFOLLOW)
  } catch {
    return undefined
  }
  try {
    const hash = createHash('sha256')
    for await (const chunk of handle.createReadStream({ autoClose: false })) hash.update(chunk as Buffer)
    return hash.digest('hex')
  } finally {
    await handle.close()
  }
}

/** Every regular file at a sync path under `work`, never following a link. */
export async function manifestOf(work: string, cache: ShaCache = new Map()): Promise<Manifest> {
  const files: ManifestEntry[] = []
  let visited = 0
  let truncated = false
  const seen = new Set<string>()
  const visit = async (abs: string, rel: string[]): Promise<void> => {
    let entries
    try {
      entries = await readdir(abs, { withFileTypes: true })
    } catch {
      return
    }
    for (const entry of entries) {
      if (++visited > MANIFEST_MAX_ENTRIES) {
        truncated = true
        return
      }
      if (!SEGMENT_RE.test(entry.name)) continue
      const parts = [...rel, entry.name]
      const at = path.join(abs, entry.name)
      if (entry.isDirectory()) {
        if (parts.length < MAX_SEGMENTS && !SCRATCH_DIRS.has(entry.name)) await visit(at, parts)
        continue
      }
      const relPath = parts.join('/')
      if (!entry.isFile() || !syncPath(relPath)) continue
      const st = await lstat(at).catch(() => undefined)
      if (!st?.isFile()) continue
      const key = `${st.ino}:${st.size}:${st.mtimeMs}:${st.ctimeMs}`
      let sha = cache.get(relPath)?.key === key ? cache.get(relPath)!.sha256 : undefined
      if (sha === undefined) {
        sha = await fileSha(at)
        if (sha === undefined) continue
        cache.set(relPath, { key, sha256: sha })
      }
      seen.add(relPath)
      files.push({ path: relPath, sha256: sha, size: st.size })
    }
  }
  await visit(work, [])
  for (const key of cache.keys()) if (!seen.has(key)) cache.delete(key)
  return { files, truncated }
}

/** Writes `rel` under `work` through a temporary file, replacing whatever is in its way. */
async function writeFileAt(work: string, rel: string, bytes: Buffer): Promise<void> {
  const parts = rel.split('/')
  let at = work
  for (const part of parts.slice(0, -1)) {
    at = path.join(at, part)
    const st = await lstat(at).catch(() => undefined)
    if (st && !st.isDirectory()) await rm(at, { recursive: true, force: true })
    if (!st || !st.isDirectory()) await mkdir(at, { mode: 0o755 })
  }
  const target = path.join(at, parts.at(-1)!)
  const temp = path.join(at, `.sandbox-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`)
  const handle = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o644)
  try {
    await handle.writeFile(bytes)
  } finally {
    await handle.close()
  }
  const st = await lstat(target).catch(() => undefined)
  if (st?.isDirectory()) await rm(target, { recursive: true, force: true })
  await rename(temp, target)
}

function readBody(req: IncomingMessage, max: number): Promise<Buffer | undefined> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    req.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > max) {
        resolve(undefined)
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks)))
    req.on('error', reject)
  })
}

function json(res: ServerResponse, status: number, body: unknown): void {
  const text = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) })
  res.end(text)
}

function authorized(req: IncomingMessage, token: string): boolean {
  const header = req.headers.authorization ?? ''
  const given = Buffer.from(header.startsWith('Bearer ') ? header.slice(7) : '')
  const want = Buffer.from(token)
  return given.length === want.length && timingSafeEqual(given, want)
}

function parseExec(body: unknown): ExecRequest | string {
  if (typeof body !== 'object' || body === null) return 'the body must be a JSON object'
  const { shell, command, timeout_ms } = body as Record<string, unknown>
  if (typeof shell !== 'string' || !(SHELLS as readonly string[]).includes(shell)) return `shell must be one of ${SHELLS.join(', ')}`
  if (typeof command !== 'string' || command.length === 0) return 'command must be a non-empty string'
  if (typeof timeout_ms !== 'number' || !Number.isInteger(timeout_ms) || timeout_ms < 1 || timeout_ms > MAX_TIMEOUT_MS) {
    return `timeout_ms must be an integer from 1 to ${MAX_TIMEOUT_MS}`
  }
  return { shell: shell as ExecRequest['shell'], command, timeout_ms }
}

/** The exec server; listen on it yourself. */
export function sandboxServer(options: SandboxServerOptions): Server {
  const cache: ShaCache = new Map()
  let running = 0
  let idle: NodeJS.Timeout | undefined
  const touch = () => {
    if (idle) clearTimeout(idle)
    idle = setTimeout(() => {
      if (running === 0) options.onIdle()
      else touch()
    }, options.idleMs)
    idle.unref()
  }
  touch()
  const server = createServer((req, res) => {
    void (async () => {
      const url = new URL(req.url ?? '/', 'http://sandbox')
      if (req.method === 'GET' && url.pathname === '/healthz') return json(res, 200, { ok: true })
      if (!authorized(req, options.token)) return json(res, 401, { error: 'unauthorized' })
      touch()
      if (req.method === 'GET' && url.pathname === '/v1/manifest') return json(res, 200, await manifestOf(options.work, cache))
      if (url.pathname.startsWith('/v1/files/')) {
        const rel = decodeURIComponent(url.pathname.slice('/v1/files/'.length))
        if (!syncPath(rel)) return json(res, 400, { error: `${JSON.stringify(rel)} is not a path that syncs` })
        const abs = path.join(options.work, rel)
        if (req.method === 'PUT') {
          const bytes = await readBody(req, BODY_MAX_BYTES)
          if (!bytes) return json(res, 413, { error: 'the file is too large' })
          const sha = createHash('sha256').update(bytes).digest('hex')
          if (req.headers[SHA_HEADER] !== sha) return json(res, 400, { error: 'the sha256 does not match the bytes' })
          await writeFileAt(options.work, rel, bytes)
          return json(res, 200, { path: rel, sha256: sha, size: bytes.length })
        }
        if (req.method === 'DELETE') {
          const st = await lstat(abs).catch(() => undefined)
          if (st && !st.isDirectory()) await unlink(abs)
          return json(res, 200, { path: rel })
        }
        if (req.method === 'GET') {
          let handle
          try {
            handle = await open(abs, constants.O_RDONLY | constants.O_NOFOLLOW)
          } catch {
            return json(res, 404, { error: 'no such file' })
          }
          try {
            const st = await handle.stat()
            if (!st.isFile()) return json(res, 404, { error: 'no such file' })
            const bytes = await handle.readFile()
            res.writeHead(200, {
              'content-type': 'application/octet-stream',
              'content-length': bytes.length,
              [SHA_HEADER]: createHash('sha256').update(bytes).digest('hex'),
            })
            res.end(bytes)
            return
          } finally {
            await handle.close()
          }
        }
        return json(res, 405, { error: 'method not allowed' })
      }
      if (req.method === 'POST' && url.pathname === '/v1/exec') {
        const raw = await readBody(req, 1024 * 1024)
        let parsed: ExecRequest | string
        try {
          parsed = raw ? parseExec(JSON.parse(raw.toString('utf8'))) : 'the body is too large'
        } catch {
          parsed = 'the body is not JSON'
        }
        if (typeof parsed === 'string') return json(res, 400, { error: parsed })
        // One command at a time: the sweep after each would kill another's processes.
        if (running > 0) return json(res, 409, { error: 'a command is running in this sandbox' })
        running++
        try {
          return json(res, 200, await runCommand(options, parsed))
        } finally {
          running--
          touch()
        }
      }
      return json(res, 404, { error: 'not found' })
    })().catch((err: unknown) => {
      if (!res.headersSent) json(res, 500, { error: err instanceof Error ? err.message : String(err) })
      else res.destroy()
    })
  })
  // A command may run MAX_TIMEOUT_MS; Node's default request timeout (300 s) would cut it off.
  server.requestTimeout = MAX_TIMEOUT_MS + 60_000
  server.on('close', () => idle && clearTimeout(idle))
  return server
}
