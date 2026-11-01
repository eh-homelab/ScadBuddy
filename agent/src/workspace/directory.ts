import { constants } from 'node:fs'
import { chmod, lstat, mkdir, open, readdir, realpath, rm, stat, utimes } from 'node:fs/promises'
import path from 'node:path'
import type { Sql } from 'postgres'
import { isUuid } from '../harness/uuid.js'
import { sha256, type SessionFiles, WORKSPACE_PATH_MAX, WorkspaceError, type WorkspaceErrorCode, WORKSPACE_MAX_FILES, type WorkspaceRef } from './store.js'

// A session's working directory, Claude Code's `cwd` (plan 2026-10-13 Step 9.5, #4773 R3,
// decision Q5 as built): `<root>/<session id>/`, where root is SCADBUDDY_SESSION_DIR (a capped
// emptyDir of the worker pod's own), else `work/sessions` under the state directory
// (harness/stateDirs.ts `sessionsRoot`). The directory is a WORKING COPY of the session's
// workspace: `SessionFiles` (store.ts, the blob store since #4812) stays the record, and every
// placement refills the copy from it, so a turn placed on another pod finds what the last turn
// wrote back. Q5 put the copy on the NAS share; it is per pod because the share's backup cannot
// leave a subPath out (Velero 1.18's kopia fs-backup) and these copies are plaintext.
//
//   - `place`, when a turn is placed (sessions/turnHost.ts): the directory is made what the
//     record holds. A file whose bytes differ is written from the record; anything else there
//     goes: a file the record does not hold (a panel delete, or what a placement that died
//     never wrote back), and every entry that is not a regular file at a workspace path
//     (a symlink, a dot-file, a fifth level). The record's sha256 of each file placed is the
//     placement's base.
//   - `writeBack`, at each park and at the turn's end (a released or shut-down turn's too):
//     each file whose sha256 is not its base is written to the record, and each base file gone
//     from the directory is deleted there, both only while the record still holds the base
//     (`expectedSha256`); the base then moves on. A record changed meanwhile (a panel upload,
//     a `workspace_*` write during the turn) wins: the next placement brings it in.
//   - `evict`, once the session's warm process here has closed (warm.ts `onClosed`): what changed
//     since the last sync is written back, then the copy goes, so the pod's disk holds at most its
//     warm sessions' directories. Not while a turn is placed in it or a Monitor command runs there
//     (`hold`, background/activities.ts). A placement here also writes back first what changed
//     since this pod's last sync (a monitor's output between turns, a file whose write-back
//     failed), so nothing written here is removed as unrecorded before it was offered.
//
// Caps: the workspace's (`workspace_file_max_bytes`, `workspace_max_bytes`). A file the record
// refuses for its size (`too_large`, `quota`) is removed from the directory at the write-back,
// with every entry that cannot be synced, so after a write-back a directory holds no more than
// its workspace. TurnHost also stops a turn whose directory grows past the cap (`usage`), and
// logs each refusal (`session directory cap:`). Each directory is 0700 and its files 0600.
// Every session's directory is the same uid's, so what keeps a session to its own is that
// nothing here follows a link or takes a path outside it (`..` is no workspace segment), and
// Step 9.6's file-tool seam.
//
// So a panel upload or a `workspace_*` write needs no marker: the next placement compares the
// record with the directory. A pod that dies mid-turn loses only that turn's writes since its
// last park, which the restart message already tells the model to check. The directory holds
// plaintext, where the record's bytes are sealed. What this pod remembers of its last sync is in
// memory: after a container restart (the emptyDir survives it) the next placement here finds no
// base, so it, or the 24 h sweep, drops what the old process never wrote back, as for a placement
// that died. forgetSubject removes it
// (`removeSessionDir`), and `sweep` removes one whose session is gone.

/** A path segment the workspace takes (store.ts WORKSPACE_PATH's). */
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/
/** How deep a workspace path goes (store.ts WORKSPACE_PATH: four segments). */
const MAX_SEGMENTS = 4
/** What a write-back records as a file's source. */
const SOURCE = { kind: 'session_dir' } as const
/** A directory no turn was placed in for this long is removed by the sweep, a backstop for a missed eviction. */
export const IDLE_MS = 24 * 60 * 60 * 1000
/** The refusals that mean a file is past a cap: such a file leaves the directory at the write-back. */
const CAPPED: ReadonlySet<WorkspaceErrorCode> = new Set(['too_large', 'quota'])
/**
 * How many entries (files, directories, links) a session's directory may hold before a turn is
 * stopped, as past the byte cap: empty files and directories cost inodes and walk time but no
 * bytes. A workspace holds a few hundred files in practice and at most store.ts
 * WORKSPACE_MAX_FILES (5,000, each with at most three parent directories), so a placement makes
 * at most 20,000 entries and can never make a directory this check stops; past that is a turn's
 * own scratch tree. A walk of that many lstat calls stays well under the 15 s between two checks.
 */
export const MAX_ENTRIES = MAX_SEGMENTS * WORKSPACE_MAX_FILES

/**
 * The paths a write-back could not offer to the record for now (`unavailable`, say): not past a
 * cap and not lost to a newer record (`conflict`). Their copy here is the only one, so it is kept.
 */
export function pendingPaths(back: WriteBack): Set<string> {
  return new Set(back.skipped.filter((s) => !CAPPED.has(s.code) && s.code !== 'conflict').map((s) => s.path))
}

/** A placement: the directory, and the record's sha256 of each file it holds as synced (moved on by each write-back). */
export type Placement = { sessionId: string; dir: string; base: Map<string, string> }

/** What a write-back did; `skipped` names each file it left, and why. */
export type WriteBack = {
  written: string[]
  deleted: string[]
  /** Each file left unsynced, why, and the refusal's code; a capped one (`too_large`, `quota`) was removed from the directory. */
  skipped: { path: string; why: string; code: WorkspaceErrorCode }[]
  /** How many entries that cannot be synced (links, dot-files, past four levels) were removed. */
  strays: number
  /** The bytes the directory held when it was walked, every entry counted (a link as itself). */
  bytes: number
}

/** What a walk found: the regular files at workspace paths, by path, and every other entry (absolute). */
type Walked = { files: Map<string, string>; strays: string[]; bytes: number }

function ws(sessionId: string): WorkspaceRef {
  return { kind: 'session', sessionId }
}

function missing(err: unknown): boolean {
  return (err as NodeJS.ErrnoException).code === 'ENOENT'
}

/** `<root>/<id>`, for a session id only (a UUID), so no id leaves `root`. */
export function sessionDirOf(root: string, sessionId: string): string {
  if (!isUuid(sessionId)) throw new Error(`not a session id: ${JSON.stringify(sessionId)}`)
  return path.join(root, sessionId.toLowerCase())
}

/** Removes a session's directory and all in it (forgetSubject, the sweep). */
export async function removeSessionDir(root: string, sessionId: string): Promise<void> {
  await rm(sessionDirOf(root, sessionId), { recursive: true, force: true })
}

/** Every entry under `dir`, never following a link: the regular files at workspace paths, and the rest. */
async function walk(dir: string): Promise<Walked> {
  const out: Walked = { files: new Map(), strays: [], bytes: 0 }
  const visit = async (abs: string, rel: string[]): Promise<void> => {
    let entries
    try {
      entries = await readdir(abs, { withFileTypes: true })
    } catch (err) {
      if (missing(err)) return
      throw err
    }
    for (const entry of entries) {
      const at = path.join(abs, entry.name)
      const parts = [...rel, entry.name]
      const named = SEGMENT.test(entry.name) && parts.join('/').length <= WORKSPACE_PATH_MAX
      if (named && entry.isDirectory() && parts.length < MAX_SEGMENTS) await visit(at, parts)
      else {
        if (named && entry.isFile()) out.files.set(parts.join('/'), at)
        else out.strays.push(at)
        out.bytes += entry.isDirectory() ? await sizeOf(at) : await lstat(at).then((st) => st.size, () => 0)
      }
    }
  }
  await visit(dir, [])
  return out
}

/**
 * Every byte under `dir`, never following a link (a link counts as itself), and how many entries;
 * it stops counting once past `limit` bytes or `maxEntries` entries.
 */
async function measure(dir: string, limit = Infinity, maxEntries = Infinity): Promise<{ bytes: number; entries: number }> {
  let total = 0
  let count = 0
  const over = () => total > limit || count > maxEntries
  const visit = async (abs: string): Promise<void> => {
    if (over()) return
    let entries
    try {
      entries = await readdir(abs, { withFileTypes: true })
    } catch (err) {
      if (missing(err)) return
      throw err
    }
    for (const entry of entries) {
      const at = path.join(abs, entry.name)
      if (over()) return
      count += 1
      if (entry.isDirectory()) await visit(at)
      else total += await lstat(at).then((st) => st.size, () => 0)
    }
  }
  await visit(dir)
  return { bytes: total, entries: count }
}

/** Every byte under `dir`, never following a link (a link counts as itself). */
async function sizeOf(dir: string): Promise<number> {
  return (await measure(dir)).bytes
}

/** Whether `at` resolves to `inside` or below it (a directory swapped for a link would not). */
async function within(inside: string, at: string): Promise<boolean> {
  const [root, real] = await Promise.all([realpath(inside), realpath(at)])
  return real === root || real.startsWith(root + path.sep)
}

/**
 * A regular file's bytes, opened without following a link, and only while its directory still
 * resolves inside `inside`; undefined when it is gone (or does not), or past `max` bytes (`size` then).
 */
async function readRegular(inside: string, abs: string, max: number): Promise<{ bytes: Buffer } | { size: number } | undefined> {
  if (!(await within(inside, path.dirname(abs)).catch(() => false))) return undefined
  let handle
  try {
    handle = await open(abs, constants.O_RDONLY | constants.O_NOFOLLOW)
  } catch (err) {
    if (missing(err) || (err as NodeJS.ErrnoException).code === 'ELOOP') return undefined
    throw err
  }
  try {
    const st = await handle.stat()
    if (!st.isFile()) return undefined
    if (st.size > max) return { size: st.size }
    return { bytes: await handle.readFile() }
  } finally {
    await handle.close()
  }
}

/** Writes `rel` under `dir`, replacing any entry in its way (a link above all) rather than writing through it. */
async function writeInside(dir: string, rel: string, bytes: Uint8Array): Promise<void> {
  const parts = rel.split('/')
  let at = dir
  for (const part of parts.slice(0, -1)) {
    at = path.join(at, part)
    const st = await lstat(at).catch((err: unknown) => (missing(err) ? undefined : Promise.reject(err)))
    if (st && !st.isDirectory()) await rm(at, { recursive: true, force: true })
    if (!st || !st.isDirectory()) await mkdir(at, { mode: 0o700 })
  }
  // A directory swapped for a link between its check and the open would be followed (O_NOFOLLOW
  // guards the last part only): the parent must still resolve inside `dir`. A window remains
  // between this check and the open, which a process of the same uid could use; Step 9.6's
  // openat-style walk is to close it.
  if (!(await within(dir, at))) throw new Error(`${rel} resolves outside the session's directory`)
  const target = path.join(at, parts.at(-1)!)
  const st = await lstat(target).catch((err: unknown) => (missing(err) ? undefined : Promise.reject(err)))
  if (st && !st.isFile()) await rm(target, { recursive: true, force: true })
  const handle = await open(target, constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | constants.O_NOFOLLOW, 0o600)
  try {
    await handle.writeFile(bytes)
  } finally {
    await handle.close()
  }
}

/** The directory itself, a real one: a link or a file in its place is removed first. */
async function ensureDir(dir: string): Promise<void> {
  const st = await lstat(dir).catch((err: unknown) => (missing(err) ? undefined : Promise.reject(err)))
  if (st && !st.isDirectory()) await rm(dir, { recursive: true, force: true })
  if (!st?.isDirectory()) await mkdir(dir, { recursive: true, mode: 0o700 })
  // Its owner only, whatever umask made it; and its time is the placement's, which the sweep reads.
  await chmod(dir, 0o700)
  const now = new Date()
  await utimes(dir, now, now)
}

export class SessionDirectory {
  readonly #files: Pick<SessionFiles, 'list' | 'read' | 'write' | 'delete' | 'limits' | 'fileCount' | 'maxFiles'>
  readonly #root: string
  /** Each session's operations on its directory, one at a time: a sweep or an eviction never runs into a placement. */
  readonly #chains = new Map<string, Promise<unknown>>()
  /** The sessions with a turn placed here now (between `place` and `done`), or a monitor command running (`hold`): never evicted or swept. */
  readonly #busy = new Map<string, number>()
  /**
   * Each session's last placement here, its base as last synced: the next placement writes back
   * against it first, so what was written since (a monitor's output between turns, a file whose
   * write-back failed) reaches the record rather than being removed as unrecorded.
   */
  readonly #last = new Map<string, Placement>()
  /** The sessions a Monitor command runs in now, by how many (a subset of `#busy`). */
  readonly #monitors = new Map<string, number>()
  /** Evictions refused while the session was busy: each runs once it is not, unless a placement came first. */
  readonly #evictWanted = new Set<string>()
  /** Whether a session's row still stands; without it (tests), every session's does. */
  readonly #exists: (sessionId: string) => Promise<boolean>
  readonly #maxEntries: number
  readonly #warm: (sessionId: string) => boolean

  constructor(
    files: Pick<SessionFiles, 'list' | 'read' | 'write' | 'delete' | 'limits' | 'fileCount' | 'maxFiles'>,
    root: string,
    options: {
      sql?: Sql | undefined
      maxEntries?: number | undefined
      /** Whether the session has a warm process here now (warm.ts `has`): its cwd is never removed. */
      warm?: ((sessionId: string) => boolean) | undefined
    } = {},
  ) {
    this.#files = files
    this.#root = root
    this.#warm = options.warm ?? (() => false)
    // Four entries a file (MAX_SEGMENTS: the file and at most three parent directories), so a
    // placement of a full workspace never makes a directory the entry check stops.
    this.#maxEntries = options.maxEntries ?? MAX_SEGMENTS * files.maxFiles
    const sql = options.sql
    this.#exists = sql ? async (id) => (await sql`SELECT 1 FROM ai_sessions WHERE id = ${id}`).length > 0 : () => Promise.resolve(true)
  }

  /** The session's directory: the `cwd` its turns run in (harness/stateDirs.ts `sessionWorkDir`, from the same root). */
  dirOf(sessionId: string): string {
    return sessionDirOf(this.#root, sessionId)
  }

  #serial<T>(sessionId: string, work: () => Promise<T>): Promise<T> {
    const id = sessionId.toLowerCase()
    const run = (this.#chains.get(id) ?? Promise.resolve()).then(work, work)
    const settled = run.catch(() => {})
    this.#chains.set(id, settled)
    void settled.then(() => {
      if (this.#chains.get(id) === settled) this.#chains.delete(id)
    })
    return run
  }

  /**
   * Makes the directory what the session's workspace holds (the header); its base is what each
   * write-back compares with. The session is busy here until `done`.
   */
  place(sessionId: string): Promise<Placement> {
    const id = sessionId.toLowerCase()
    // A new process will be spawned for it: an eviction wished for the old one is void.
    this.#evictWanted.delete(id)
    this.#busy.set(id, (this.#busy.get(id) ?? 0) + 1)
    return this.#serial(sessionId, () => this.#place(sessionId)).catch((err: unknown) => {
      this.#unbusy(id)
      throw err
    })
  }

  /** The placement's turn has ended here: its directory may be evicted again. */
  done(placement: Placement, options: { cutOff?: boolean } = {}): void {
    const id = placement.sessionId.toLowerCase()
    // A placement cut off (another holds the turn) never writes back, so nothing later does it for it either.
    if (options.cutOff && this.#last.get(id) === placement) this.#last.delete(id)
    this.#unbusy(id)
  }

  /** The session's copy and what this pod remembers of its last sync, gone. */
  async #drop(id: string): Promise<void> {
    this.#last.delete(id)
    await removeSessionDir(this.#root, id)
  }

  /** A monitor command runs in the session's directory (background/activities.ts): held until the returned release. */
  hold(sessionId: string): () => void {
    const id = sessionId.toLowerCase()
    this.#busy.set(id, (this.#busy.get(id) ?? 0) + 1)
    this.#monitors.set(id, (this.#monitors.get(id) ?? 0) + 1)
    let released = false
    return () => {
      if (released) return
      released = true
      const n = (this.#monitors.get(id) ?? 1) - 1
      if (n > 0) this.#monitors.set(id, n)
      else this.#monitors.delete(id)
      this.#unbusy(id)
    }
  }

  /**
   * A Monitor command is about to run here (`hold` taken first): the directory is placed when this
   * pod has none of the session's yet, so it is 0700, holds the workspace, and what the command
   * writes is offered to the record at the next write-back here.
   */
  adopt(sessionId: string): Promise<void> {
    const id = sessionId.toLowerCase()
    return this.#serial(sessionId, async () => {
      if (!this.#last.has(id)) await this.#place(sessionId)
    })
  }

  #unbusy(id: string): void {
    const n = (this.#busy.get(id) ?? 1) - 1
    if (n > 0) {
      this.#busy.set(id, n)
      return
    }
    this.#busy.delete(id)
    // Its process closed while a turn or a monitor held the directory (a cut placement, a released
    // park): the eviction it then asked for runs now.
    if (this.#evictWanted.delete(id)) {
      void this.evict(id).catch((err: unknown) =>
        console.error(`session ${id}: its working directory could not be removed: ${err instanceof Error ? err.message : String(err)}`),
      )
    }
  }

  async #place(sessionId: string): Promise<Placement> {
    const dir = this.dirOf(sessionId)
    // What changed here since the last sync goes to the record first: compared by sha256 against
    // that base, so a record changed elsewhere since still wins (expectedSha256).
    const id = sessionId.toLowerCase()
    const last = this.#last.get(id)
    // Paths the record could not take now (`unavailable`): their copy here is the only one, so
    // it is neither removed nor overwritten, and keeps its old base for the next try.
    const unsynced = last ? pendingPaths(await this.#writeBack(last)) : new Set<string>()
    // A Monitor command still runs here (`hold`, besides this placement): what it writes from now
    // on is its own, so nothing unrecorded is removed under it; the next write-back offers it.
    const monitored = this.#monitors.has(id)
    await ensureDir(dir)
    const [record, found] = await Promise.all([this.#files.list(ws(sessionId)), walk(dir)])
    if (!monitored) for (const stray of found.strays) await rm(stray, { recursive: true, force: true })
    const base = new Map<string, string>()
    for (const rel of unsynced) {
      const was = last?.base.get(rel)
      if (was !== undefined) base.set(rel, was)
    }
    const held = new Set(record.map((f) => f.path))
    for (const [rel, abs] of found.files) {
      if (!held.has(rel) && !unsynced.has(rel) && !monitored) await rm(abs, { force: true })
    }
    // A file named as another file's directory (a record from before the write refused it, store.ts):
    // a directory cannot be both, so the file is left out here and out of the base, and no
    // write-back can read its absence as a deletion. The tools and the panel still reach it.
    const parents = new Set(record.flatMap((f) => f.path.split('/').slice(0, -1).map((_, i, segments) => segments.slice(0, i + 1).join('/'))))
    for (const file of record) {
      if (unsynced.has(file.path) || parents.has(file.path)) continue
      const there = found.files.get(file.path)
      const current = there === undefined ? undefined : await readRegular(dir, there, file.size)
      if (current && 'bytes' in current && sha256(current.bytes) === file.sha256) {
        base.set(file.path, file.sha256)
        continue
      }
      let read
      try {
        read = await this.#files.read(ws(sessionId), file.path)
      } catch (err) {
        // A blob that cannot be read now (workspace store.ts `unavailable`): left out of the
        // base, so the write-back cannot replace or delete the record's file over it.
        if (err instanceof WorkspaceError) continue
        throw err
      }
      if (!read) continue
      await writeInside(dir, file.path, read.bytes)
      base.set(file.path, read.sha256)
    }
    const placement = { sessionId, dir, base }
    this.#last.set(sessionId.toLowerCase(), placement)
    return placement
  }

  /**
   * Writes the directory's changes since the placement's base to the session's workspace (the
   * header), and moves the base on to what it synced: a turn writes back at each park and again
   * at its end.
   */
  writeBack(placement: Placement, options: { prune?: boolean } = {}): Promise<WriteBack> {
    return this.#serial(placement.sessionId, () => this.#writeBack(placement, options))
  }

  async #writeBack(placement: Placement, options: { prune?: boolean } = {}): Promise<WriteBack> {
    const { sessionId, dir, base } = placement
    const { fileMaxBytes } = await this.#files.limits()
    const found = await walk(dir)
    const out: WriteBack = { written: [], deleted: [], skipped: [], strays: 0, bytes: found.bytes }
    // Only offers, no removal: a write-back during a turn (`prune: false`, at a park or on the
    // timer) leaves what the turn still uses (a .git, a fifth level, a large intermediate file),
    // and so does one while a Monitor command runs (removing a file would leave it writing to an
    // unlinked one). The turn's last write-back, an eviction and a placement prune.
    const keepAll = this.#monitors.has(sessionId.toLowerCase()) || options.prune === false
    // Deletions first: what they free counts toward the writes' quota.
    for (const [rel, digest] of [...base].sort(([a], [b]) => a.localeCompare(b))) {
      if (found.files.has(rel)) continue
      try {
        if (await this.#files.delete(ws(sessionId), rel, { expectedSha256: digest })) out.deleted.push(rel)
        base.delete(rel)
      } catch (err) {
        if (!(err instanceof WorkspaceError)) throw err
        out.skipped.push({ path: rel, why: err.message, code: err.code })
        // Changed in the record meanwhile: the record wins, so this is not tried (and logged) again.
        if (err.code === 'conflict') base.delete(rel)
      }
    }
    // A new file is offered only while the record holds fewer than its file quota (store.ts
    // WORKSPACE_MAX_FILES), so a flood of files costs one count, not one refused write each, and
    // what a placement brings back stays within MAX_ENTRIES. The write's own check is what holds.
    let held = [...found.files.keys()].some((rel) => !base.has(rel)) ? await this.#files.fileCount(ws(sessionId)) : 0
    for (const [rel, abs] of [...found.files].sort(([a], [b]) => a.localeCompare(b))) {
      if (!base.has(rel) && held >= this.#files.maxFiles) {
        out.skipped.push({ path: rel, why: `the workspace holds at most ${this.#files.maxFiles} files`, code: 'quota' })
        if (!keepAll) await rm(abs, { force: true })
        continue
      }
      const read = await readRegular(dir, abs, fileMaxBytes)
      if (!read) continue
      if (!('bytes' in read)) {
        out.skipped.push({ path: rel, why: `${rel} is ${read.size} bytes; a workspace file holds at most ${fileMaxBytes}`, code: 'too_large' })
        if (!keepAll) await rm(abs, { force: true })
        continue
      }
      const was = base.get(rel)
      const digest = sha256(read.bytes)
      if (was === digest) continue
      try {
        await this.#files.write(ws(sessionId), rel, read.bytes, { source: SOURCE, expectedSha256: was ?? null })
        if (was === undefined) held += 1
        out.written.push(rel)
        base.set(rel, digest)
      } catch (err) {
        if (!(err instanceof WorkspaceError)) throw err
        out.skipped.push({ path: rel, why: err.message, code: err.code })
        // Changed in the record meanwhile: the record wins (the next placement brings it in), so this is not tried again.
        if (err.code === 'conflict') base.set(rel, digest)
        // Past a cap: not kept on this disk either.
        if (CAPPED.has(err.code) && !keepAll) await rm(abs, { force: true })
      }
    }
    if (!keepAll) {
      for (const stray of found.strays) await rm(stray, { recursive: true, force: true })
      out.strays = found.strays.length
    }
    return out
  }

  /**
   * The bytes the directory holds now, every entry counted (a link as itself), and the workspace
   * cap (`workspace_max_bytes`); `overEntries` past MAX_ENTRIES entries. lstat only, and the count
   * stops once past either, so a turn's check every 15 s stays cheap however large the tree grows:
   * `bytes` is exact only up to the cap, never a measure of the directory past it.
   */
  async usage(placement: Placement): Promise<{ bytes: number; cap: number; maxEntries: number; overEntries?: boolean }> {
    const { workspaceMaxBytes } = await this.#files.limits()
    const { bytes, entries } = await measure(placement.dir, workspaceMaxBytes, this.#maxEntries)
    const out = { bytes, cap: workspaceMaxBytes, maxEntries: this.#maxEntries }
    return entries > this.#maxEntries ? { ...out, overEntries: true } : out
  }

  /** Removes the session's directory (forgetSubject). */
  remove(sessionId: string): Promise<void> {
    return this.#serial(sessionId, () => this.#drop(sessionId.toLowerCase()))
  }

  /**
   * Removes the session's directory unless a turn is placed in it now: its process here has
   * closed (warm.ts `onClosed`), and the next placement, here or elsewhere, rebuilds it from the
   * record. So this disk holds at most the warm sessions' directories. Whether it removed it.
   */
  evict(sessionId: string): Promise<boolean> {
    const id = sessionId.toLowerCase()
    return this.#serial(sessionId, async () => {
      // A process spawned since the close that asked (a late `onClosed`, a wish deferred past a
      // new placement) works in this directory: it stays, and the wish with the old process goes.
      if (this.#warm(id)) {
        this.#evictWanted.delete(id)
        return false
      }
      if (this.#busy.has(id)) {
        // Asked again once it is not busy any more (`#unbusy`), unless a placement comes first.
        this.#evictWanted.add(id)
        return false
      }
      // A session deleted since has no record to write to: its copy just goes.
      if (!(await this.#exists(id))) {
        await this.#drop(id)
        return true
      }
      // What was written since the last sync (a monitor's output) goes to the record first; if
      // that fails, the copy stays for the next placement here to try again.
      const last = this.#last.get(id)
      if (last && pendingPaths(await this.#writeBack(last)).size > 0) return false
      await this.#drop(id)
      return true
    })
  }

  /**
   * Removes the directories of sessions whose row is gone, and those no turn was placed in for
   * `idleMs` (a backstop for a missed eviction); the ids it removed. Never a busy one, and
   * nothing not named by a session id.
   */
  async sweep(sql: Sql, idleMs = IDLE_MS, now = Date.now()): Promise<string[]> {
    const names = await readdir(this.#root).catch((err: unknown) => (missing(err) ? ([] as string[]) : Promise.reject(err)))
    const ids = names.filter((n) => isUuid(n) && n === n.toLowerCase())
    if (ids.length === 0) return []
    const live = new Set((await sql<{ id: string }[]>`SELECT id::text FROM ai_sessions WHERE id = ANY(${ids}::uuid[])`).map((r) => r.id))
    const removed: string[] = []
    for (const id of ids) {
      const gone = await this.#serial(id, async () => {
        // Never under a warm process here, as `evict`: its cwd stays however long it idles.
        if (this.#busy.has(id) || this.#warm(id)) return false
        if (live.has(id)) {
          const placedAt = await stat(sessionDirOf(this.#root, id)).then((st) => st.mtimeMs, () => now)
          if (now - placedAt < idleMs) return false
          const last = this.#last.get(id)
          if (last && pendingPaths(await this.#writeBack(last)).size > 0) return false
        }
        await this.#drop(id)
        return true
      })
      if (gone) removed.push(id)
    }
    return removed
  }
}
