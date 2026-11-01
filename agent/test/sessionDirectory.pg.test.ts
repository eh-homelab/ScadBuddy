import { randomUUID } from 'node:crypto'
import { existsSync, writeFileSync } from 'node:fs'
import { mkdir, mkdtemp, readdir, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { forgetSubject } from '../src/sessions/forget.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { SessionDirectory } from '../src/workspace/directory.js'
import { SessionFiles, WorkspaceError, type WorkspaceRef } from '../src/workspace/store.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner } from './support/sessions.js'

// A session's working directory as a working copy of its workspace (plan 2026-10-13 Step 9.5,
// decision Q5, workspace/directory.ts): SessionFiles is the record; a placement fills the
// directory from it, and the turn's end writes the directory back by sha256. Two roots stand for
// two pods' disks; one root shared by both stands for the NAS share.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

describe.skipIf(skip !== undefined)(`a session's working directory${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let files: SessionFiles
  let m: SessionManager | undefined
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    files = new SessionFiles(db.sql)
  })
  afterEach(async () => {
    m?.abortAll()
    await m?.warm.close()
    m = undefined
    await drop()
  })

  const root = () => mkdtemp(path.join(os.tmpdir(), 'session-dirs-'))
  const ws = (sessionId: string): WorkspaceRef => ({ kind: 'session', sessionId })

  async function session(): Promise<string> {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'sessions-'))
    m ??= manager({ sql: db.sql, paths: { stateDir } })
    return (await m.start(browser, { origin: 'chat' })).session.id
  }

  it('a file written in turn 1 on pod A is there in turn 2 on pod B', async () => {
    const id = await session()
    const podA = new SessionDirectory(files, await root())
    const podB = new SessionDirectory(files, await root())
    const turn1 = await podA.place(id)
    await mkdir(path.join(turn1.dir, 'notes'), { recursive: true })
    await writeFile(path.join(turn1.dir, 'notes', 'plan.md'), '# the plan\n')
    expect(await podA.writeBack(turn1)).toMatchObject({ written: ['notes/plan.md'], deleted: [] })
    expect((await files.read(ws(id), 'notes/plan.md'))?.bytes.toString()).toBe('# the plan\n')

    const turn2 = await podB.place(id)
    expect(turn2.dir).not.toBe(turn1.dir)
    expect(await readFile(path.join(turn2.dir, 'notes', 'plan.md'), 'utf8')).toBe('# the plan\n')
    // Unchanged there, so nothing is written back.
    expect(await podB.writeBack(turn2)).toMatchObject({ written: [], deleted: [] })
  })

  it('a panel upload or delete between turns is in the directory at the next placement', async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root())
    await files.write(ws(id), 'old.txt', Buffer.from('old'))
    const first = await dirs.place(id)
    expect(await readFile(path.join(first.dir, 'old.txt'), 'utf8')).toBe('old')
    await dirs.writeBack(first)

    await files.write(ws(id), 'uploads/part.scad', Buffer.from('cube(10);'))
    await files.delete(ws(id), 'old.txt')
    const next = await dirs.place(id)
    expect(await readFile(path.join(next.dir, 'uploads', 'part.scad'), 'utf8')).toBe('cube(10);')
    expect(existsSync(path.join(next.dir, 'old.txt'))).toBe(false)
  })

  it('a placement that died mid-turn loses only that turn\'s unsynced writes', async () => {
    const id = await session()
    const share = await root()
    const podA = new SessionDirectory(files, share)
    const podB = new SessionDirectory(files, share)
    const turn1 = await podA.place(id)
    await writeFile(path.join(turn1.dir, 'kept.txt'), 'turn 1')
    await podA.writeBack(turn1)
    const turn2 = await podA.place(id)
    await writeFile(path.join(turn2.dir, 'lost.txt'), 'turn 2')
    await writeFile(path.join(turn2.dir, 'kept.txt'), 'turn 2 changed it')
    // Pod A dies here: no write-back. The turn is placed again on pod B, over the same share.

    const again = await podB.place(id)
    expect(await readFile(path.join(again.dir, 'kept.txt'), 'utf8')).toBe('turn 1')
    expect(existsSync(path.join(again.dir, 'lost.txt'))).toBe(false)
  })

  it('a write-back mid-turn (a park) keeps what was written so far when the pod is lost after it', async () => {
    const id = await session()
    const podA = new SessionDirectory(files, await root())
    const podB = new SessionDirectory(files, await root())
    const turn = await podA.place(id)
    await writeFile(path.join(turn.dir, 'before-park.txt'), 'synced at the park')
    expect(await podA.writeBack(turn)).toMatchObject({ written: ['before-park.txt'] })
    await writeFile(path.join(turn.dir, 'after-park.txt'), 'never synced')
    // The base moved on: a second write-back sends only what changed since.
    await writeFile(path.join(turn.dir, 'after-park.txt'), 'synced at the end')
    expect(await podA.writeBack(turn)).toMatchObject({ written: ['after-park.txt'], deleted: [] })
    await writeFile(path.join(turn.dir, 'lost.txt'), 'pod A is lost here')

    const again = await podB.place(id)
    expect(await readFile(path.join(again.dir, 'before-park.txt'), 'utf8')).toBe('synced at the park')
    expect(await readFile(path.join(again.dir, 'after-park.txt'), 'utf8')).toBe('synced at the end')
    expect(existsSync(path.join(again.dir, 'lost.txt'))).toBe(false)
  })

  it('a file past the workspace caps is refused and removed from the directory, and usage names the cap', async () => {
    const id = await session()
    const capped = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1024, workspaceMaxBytes: 1536 } })
    const dirs = new SessionDirectory(capped, await root())
    const turn = await dirs.place(id)
    await writeFile(path.join(turn.dir, 'a-fits.txt'), Buffer.alloc(1000, 'a'))
    await writeFile(path.join(turn.dir, 'b-over-quota.txt'), Buffer.alloc(1000, 'b'))
    await writeFile(path.join(turn.dir, 'c-too-large.txt'), Buffer.alloc(2000, 'c'))
    // Counted only until past the cap (4000 bytes are there).
    const over = await dirs.usage(turn)
    expect(over.cap).toBe(1536)
    expect(over.bytes).toBeGreaterThan(1536)
    const back = await dirs.writeBack(turn)
    expect(back.written).toEqual(['a-fits.txt'])
    expect(back.skipped.map((s) => [s.path, s.code])).toEqual([
      ['c-too-large.txt', 'too_large'],
      ['b-over-quota.txt', 'quota'],
    ].sort())
    expect(existsSync(path.join(turn.dir, 'b-over-quota.txt'))).toBe(false)
    expect(existsSync(path.join(turn.dir, 'c-too-large.txt'))).toBe(false)
    expect(await dirs.usage(turn)).toEqual({ bytes: 1000, cap: 1536 })
    expect((await capped.list(ws(id))).map((f) => f.path)).toEqual(['a-fits.txt'])
  })

  it("each session's directory is its owner's only, and a closed process's copy is evicted unless a turn is placed in it", async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root())
    await files.write(ws(id), 'sub/f.txt', Buffer.from('x'))
    const turn = await dirs.place(id)
    expect((await stat(turn.dir)).mode & 0o777).toBe(0o700)
    expect((await stat(path.join(turn.dir, 'sub'))).mode & 0o777).toBe(0o700)
    expect((await stat(path.join(turn.dir, 'sub', 'f.txt'))).mode & 0o777).toBe(0o600)
    expect(() => dirs.dirOf('../etc')).toThrow(/not a session id/)
    // Busy: a turn is placed in it.
    expect(await dirs.evict(id)).toBe(false)
    expect(await dirs.sweep(db.sql, 0)).toEqual([])
    dirs.done(turn)
    expect(await dirs.evict(id)).toBe(true)
    expect(existsSync(turn.dir)).toBe(false)
    // Rebuilt from the record at the next placement.
    const next = await dirs.place(id)
    expect(await readFile(path.join(next.dir, 'sub', 'f.txt'), 'utf8')).toBe('x')
  })

  it("a monitor's command keeps its directory across the process's eviction, and what it writes between turns reaches the record", async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root())
    const turn = await dirs.place(id)
    await dirs.writeBack(turn)
    dirs.done(turn)
    // A Monitor command started in the turn runs on in the directory (background/activities.ts).
    const release = dirs.hold(id)
    expect(await dirs.evict(id)).toBe(false)
    expect(await dirs.sweep(db.sql, 0)).toEqual([])
    await writeFile(path.join(turn.dir, 'build.log'), 'written by the monitor')
    // The next turn here: the monitor's output is written back, not removed as unrecorded.
    const next = await dirs.place(id)
    expect(await readFile(path.join(next.dir, 'build.log'), 'utf8')).toBe('written by the monitor')
    expect((await files.read(ws(id), 'build.log'))?.bytes.toString()).toBe('written by the monitor')
    dirs.done(next)
    await writeFile(path.join(next.dir, 'tail.log'), 'after the last turn')
    release()
    // Once the monitor ends and the process has closed, the eviction writes back first.
    expect(await dirs.evict(id)).toBe(true)
    expect((await files.read(ws(id), 'tail.log'))?.bytes.toString()).toBe('after the last turn')
  })

  it('a file whose write-back failed for a while is written back at the next placement here, not removed', async () => {
    const id = await session()
    let down = true
    const flaky = new Proxy(files, {
      get(target, prop, receiver) {
        if (prop === 'write' && down) return () => Promise.reject(new WorkspaceError('unavailable', 'the blob store is down; nothing was written'))
        const value = Reflect.get(target, prop, receiver) as unknown
        return typeof value === 'function' ? (value as (...a: unknown[]) => unknown).bind(target) : value
      },
    })
    const dirs = new SessionDirectory(flaky, await root())
    const turn = await dirs.place(id)
    await writeFile(path.join(turn.dir, 'result.txt'), 'keep me')
    expect((await dirs.writeBack(turn)).skipped.map((s) => s.code)).toEqual(['unavailable'])
    dirs.done(turn)
    // Still down: neither an eviction, nor the idle sweep, nor a placement removes the only copy.
    expect(await dirs.evict(id)).toBe(false)
    expect(await dirs.sweep(db.sql, 0)).toEqual([])
    const meanwhile = await dirs.place(id)
    expect(await readFile(path.join(meanwhile.dir, 'result.txt'), 'utf8')).toBe('keep me')
    dirs.done(meanwhile)
    expect(await dirs.evict(id)).toBe(false)
    down = false
    const next = await dirs.place(id)
    expect(await readFile(path.join(next.dir, 'result.txt'), 'utf8')).toBe('keep me')
    expect((await files.read(ws(id), 'result.txt'))?.bytes.toString()).toBe('keep me')
  })

  it('a deletion in the directory is written back, and a panel write during the turn wins over it', async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root())
    await files.write(ws(id), 'a.txt', Buffer.from('a'))
    await files.write(ws(id), 'b.txt', Buffer.from('b'))
    const turn = await dirs.place(id)
    await writeFile(path.join(turn.dir, 'a.txt'), 'the turn wrote a')
    await writeFile(path.join(turn.dir, 'b.txt'), 'the turn wrote b')
    // The panel replaces a.txt while the turn runs: the record's newer write is kept.
    await files.write(ws(id), 'a.txt', Buffer.from('the panel wrote a'))
    const back = await dirs.writeBack(turn)
    expect(back.written).toEqual(['b.txt'])
    expect(back.skipped).toEqual([{ path: 'a.txt', why: expect.stringContaining('changed'), code: 'conflict' }])
    expect((await files.read(ws(id), 'a.txt'))?.bytes.toString()).toBe('the panel wrote a')

    const next = await dirs.place(id)
    expect(await readFile(path.join(next.dir, 'a.txt'), 'utf8')).toBe('the panel wrote a')
    await rm(path.join(next.dir, 'b.txt'))
    expect(await dirs.writeBack(next)).toMatchObject({ written: [], deleted: ['b.txt'] })
    expect(await files.stat(ws(id), 'b.txt')).toBeUndefined()
  })

  it('writes back only regular files at workspace paths: never a symlink, and never through one', async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root())
    const outside = await mkdtemp(path.join(os.tmpdir(), 'outside-'))
    await writeFile(path.join(outside, 'secret.key'), 'the KEK')
    const turn = await dirs.place(id)
    await symlink(path.join(outside, 'secret.key'), path.join(turn.dir, 'kek.txt'))
    await symlink(outside, path.join(turn.dir, 'linked'))
    await mkdir(path.join(turn.dir, '.hidden'))
    await writeFile(path.join(turn.dir, '.hidden', 'x.txt'), 'not a workspace path')
    await writeFile(path.join(turn.dir, 'ok.txt'), 'fine')
    expect(await dirs.writeBack(turn)).toMatchObject({ written: ['ok.txt'] })
    expect((await files.list(ws(id))).map((f) => f.path)).toEqual(['ok.txt'])

    // A placement over a link the turn left writes the record's file in the directory, not through the link.
    await files.write(ws(id), 'linked/secret.key', Buffer.from('from the record'))
    const next = await dirs.place(id)
    expect(await readFile(path.join(outside, 'secret.key'), 'utf8')).toBe('the KEK')
    expect(await readFile(path.join(next.dir, 'linked', 'secret.key'), 'utf8')).toBe('from the record')
  })

  it('a fork\'s directory starts from the parent\'s files as of the fork', async () => {
    const parent = await session()
    const child = await session()
    const dirs = new SessionDirectory(files, await root())
    const turn = await dirs.place(parent)
    await writeFile(path.join(turn.dir, 'shared.txt'), 'before the fork')
    await dirs.writeBack(turn)
    await files.copy(parent, child)
    await files.write(ws(parent), 'later.txt', Buffer.from('after the fork'))

    const forked = await dirs.place(child)
    expect(await readFile(path.join(forked.dir, 'shared.txt'), 'utf8')).toBe('before the fork')
    expect(existsSync(path.join(forked.dir, 'later.txt'))).toBe(false)
  })

  it('a forgotten session\'s directory is gone, and the sweep removes one whose session is gone', async () => {
    const forgotten = await session()
    const deleted = await session()
    const live = await session()
    const dirs = new SessionDirectory(files, await root())
    for (const id of [forgotten, deleted, live]) {
      const placed = await dirs.place(id)
      await writeFile(path.join(placed.dir, 'f.txt'), id)
      dirs.done(placed)
    }
    await forgetSubject({ sql: db.sql, sessionDirs: (id) => dirs.remove(id) }, `session-${forgotten}`)
    expect(existsSync(dirs.dirOf(forgotten))).toBe(false)

    await db.sql`DELETE FROM ai_sessions WHERE id = ${deleted}`
    expect(await dirs.sweep(db.sql)).toEqual([deleted])
    expect(existsSync(dirs.dirOf(deleted))).toBe(false)
    expect(existsSync(dirs.dirOf(live))).toBe(true)
  })

  it("a write-back during the turn only offers: the turn's dot-files, fifth level and large files stay until its end", async () => {
    const id = await session()
    const capped = new SessionFiles(db.sql, { limits: { fileMaxBytes: 1024 } })
    const dirs = new SessionDirectory(capped, await root())
    const turn = await dirs.place(id)
    await mkdir(path.join(turn.dir, '.git'))
    await writeFile(path.join(turn.dir, '.git', 'HEAD'), 'ref: refs/heads/main\n')
    await mkdir(path.join(turn.dir, 'a', 'b', 'c', 'd'), { recursive: true })
    await writeFile(path.join(turn.dir, 'a', 'b', 'c', 'd', 'deep.txt'), 'fifth level')
    await writeFile(path.join(turn.dir, 'big.bin'), Buffer.alloc(2048))
    await writeFile(path.join(turn.dir, 'notes.txt'), 'synced')
    // A park, or the minute's sync.
    const parked = await dirs.writeBack(turn, { prune: false })
    expect(parked).toMatchObject({ written: ['notes.txt'], strays: 0 })
    expect(parked.skipped.map((s) => s.code)).toEqual(['too_large'])
    for (const kept of ['.git/HEAD', 'a/b/c/d/deep.txt', 'big.bin']) expect(existsSync(path.join(turn.dir, kept))).toBe(true)
    // The turn's end prunes.
    const end = await dirs.writeBack(turn)
    expect(end.strays).toBe(2)
    for (const gone of ['.git', 'a/b/c/d', 'big.bin']) expect(existsSync(path.join(turn.dir, gone))).toBe(false)
    expect(existsSync(path.join(turn.dir, 'notes.txt'))).toBe(true)
  })

  it("a late close's eviction never removes the directory of a process spawned since", async () => {
    const id = await session()
    let warmNow = false
    const dirs = new SessionDirectory(files, await root(), { warm: () => warmNow })
    // The next turn is placed; the old process's onClosed lands before the new one is in the cache.
    const turn = await dirs.place(id)
    expect(await dirs.evict(id)).toBe(false)
    // The new process is spawned, with this directory as its cwd.
    warmNow = true
    dirs.done(turn)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(existsSync(turn.dir)).toBe(true)
    expect(await dirs.evict(id)).toBe(false)
    // Once that process closes too, the directory may go.
    warmNow = false
    expect(await dirs.evict(id)).toBe(true)
  })

  it('an eviction asked while a turn is placed runs once the turn is done, unless a placement comes first', async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root(), { sql: db.sql })
    const turn = await dirs.place(id)
    await writeFile(path.join(turn.dir, 'released.txt'), 'kept in the record')
    // The process closed while the turn still unwinds (a cut placement, a released park).
    expect(await dirs.evict(id)).toBe(false)
    dirs.done(turn)
    await expect.poll(() => existsSync(turn.dir)).toBe(false)
    expect((await files.read(ws(id), 'released.txt'))?.bytes.toString()).toBe('kept in the record')

    // A new placement voids the wish: its own process is warm.
    const again = await dirs.place(id)
    expect(await dirs.evict(id)).toBe(false)
    const third = await dirs.place(id)
    dirs.done(again)
    dirs.done(third)
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(existsSync(third.dir)).toBe(true)
  })

  it("a deleted session's copy is evicted without a write-back", async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root(), { sql: db.sql })
    const turn = await dirs.place(id)
    await writeFile(path.join(turn.dir, 'orphan.txt'), 'no record to go to')
    dirs.done(turn)
    await db.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    expect(await dirs.evict(id)).toBe(true)
    expect(existsSync(turn.dir)).toBe(false)
  })

  it('a monitor that starts before any turn here places the directory first', async () => {
    const id = await session()
    await files.write(ws(id), 'input.txt', Buffer.from('from the record'))
    const dirs = new SessionDirectory(files, await root())
    const release = dirs.hold(id)
    await dirs.adopt(id)
    expect((await stat(dirs.dirOf(id))).mode & 0o777).toBe(0o700)
    expect(await readFile(path.join(dirs.dirOf(id), 'input.txt'), 'utf8')).toBe('from the record')
    await writeFile(path.join(dirs.dirOf(id), 'out.log'), 'the monitor wrote this')
    release()
    expect(await dirs.evict(id)).toBe(true)
    expect((await files.read(ws(id), 'out.log'))?.bytes.toString()).toBe('the monitor wrote this')
  })

  /** A hanging AgentSession turn on TurnHost over `directory` (a stand-in), ended only by a cap stop or `stopAfterMs`. */
  async function cappedTurn(
    directory: ConstructorParameters<typeof TurnHost>[0]['directory'],
    options: { dirCheckMs: number; dirWalkTimeoutMs?: number; stopAfterMs?: number },
  ): Promise<{ result: unknown; warned: string[] }> {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'sessions-'))
    m = manager({ sql: db.sql, paths: { stateDir }, run: scriptedRunner(() => ({ hang: true })).runner, workspace: files })
    const { session: started } = await m.start(browser, { origin: 'chat' })
    const turnId = randomUUID()
    await db.sql`
      UPDATE ai_sessions SET status = 'running', mode = 'durable', runtime = 'unified', durable_turn_id = ${turnId}, updated_at = now()
      WHERE id = ${started.id}`
    const warned: string[] = []
    const warn = console.warn
    console.warn = (...args: unknown[]) => {
      warned.push(args.map(String).join(' '))
    }
    const interrupt = new AbortController()
    const timer = options.stopAfterMs === undefined ? undefined : setTimeout(() => interrupt.abort(), options.stopAfterMs)
    try {
      const host = new TurnHost({ core: m.turnCore, sql: db.sql, images: new SessionBlobs(db.sql), hostQueue: 'agent-sessions.pod-a', directory, dirCheckMs: options.dirCheckMs, ...(options.dirWalkTimeoutMs === undefined ? {} : { dirWalkTimeoutMs: options.dirWalkTimeoutMs }) })
      const result = await host.run(
        { sessionId: started.id, turnId, attempt: 'run:1', message: { turn_id: turnId, author: browser, text: 'go', images: [], system_append: null } },
        { heartbeat: () => {}, cancelled: interrupt.signal, cancelReason: () => 'interrupt' },
      )
      await host.stop({ graceMs: 0, abortWaitMs: 0 })
      return { result, warned }
    } finally {
      clearTimeout(timer)
      console.warn = warn
    }
  }

  const noWriteBack = async () => ({ written: [], deleted: [], skipped: [], strays: 0, bytes: 0 })

  it('a walk that never ends stops the turn at its timeout (fail closed), and no tick walks again under it', async () => {
    const dir = await root()
    let walks = 0
    const directory = {
      place: async (sessionId: string) => ({ sessionId, dir, base: new Map<string, string>() }),
      writeBack: noWriteBack,
      // The first walk never ends (a hung disk): no tick may start another under it, and enforcement must not end with it.
      usage: () => {
        walks += 1
        return new Promise<never>(() => {})
      },
      done: () => {},
    }
    const { result, warned } = await cappedTurn(directory, { dirCheckMs: 20, dirWalkTimeoutMs: 100, stopAfterMs: 2_000 })
    expect(walks).toBe(1)
    expect(JSON.stringify(result)).toContain('could not be measured')
    expect(warned.some((w) => w.startsWith('session directory cap:'))).toBe(true)
  })

  it('a long write-back on the timer does not stop the measuring, and a burst during it stops the turn', async () => {
    const dir = await root()
    let walks = 0
    let timerWriteBacks = 0
    let over = false
    const directory = {
      place: async (sessionId: string) => ({ sessionId, dir, base: new Map<string, string>() }),
      // A write-back during the turn never ends (a slow store); the turn's last one (prune) does.
      writeBack: (_placed: unknown, options: { prune?: boolean } = {}) => {
        if (options.prune !== false) return noWriteBack()
        timerWriteBacks += 1
        over = true
        return new Promise<never>(() => {})
      },
      usage: async () => {
        walks += 1
        return { bytes: over ? 5_000 : 0, cap: 1_000 }
      },
      done: () => {},
    }
    const { result } = await cappedTurn(directory, { dirCheckMs: 20, stopAfterMs: 2_000 })
    expect(timerWriteBacks).toBe(1)
    expect(walks).toBeGreaterThan(4)
    expect(JSON.stringify(result)).toContain('grew past the workspace cap')
  })

  it('a workspace holds at most maxFiles files: a new one past it is refused quota, a rewrite is not', async () => {
    const id = await session()
    const few = new SessionFiles(db.sql, { maxFiles: 3 })
    for (const name of ['a', 'b', 'c']) await few.write(ws(id), name, Buffer.from(name))
    await expect(few.write(ws(id), 'd', Buffer.from('d'))).rejects.toMatchObject({ code: 'quota' })
    await few.write(ws(id), 'a', Buffer.from('a again'))
    await few.delete(ws(id), 'b')
    await few.write(ws(id), 'd', Buffer.from('d'))
    expect((await few.list(ws(id))).map((f) => f.path).sort()).toEqual(['a', 'c', 'd'])
  })

  it('a write-back offers no new file once the workspace holds maxFiles, so a placement never brings back more', async () => {
    const id = await session()
    const few = new SessionFiles(db.sql, { maxFiles: 3 })
    let writes = 0
    const write = few.write.bind(few)
    few.write = (...args: Parameters<SessionFiles['write']>) => {
      writes += 1
      return write(...args)
    }
    const dirs = new SessionDirectory(few, await root())
    const turn = await dirs.place(id)
    for (let i = 0; i < 10; i++) await writeFile(path.join(turn.dir, `f-${i}`), `file ${i}`)
    const back = await dirs.writeBack(turn)
    expect(back.written).toHaveLength(3)
    expect(back.skipped.filter((s) => s.code === 'quota')).toHaveLength(7)
    expect(writes).toBe(3)
    expect((await readdir(turn.dir)).sort()).toEqual(['f-0', 'f-1', 'f-2'])
    dirs.done(turn)
    const next = await new SessionDirectory(few, await root()).place(id)
    expect((await readdir(next.dir)).sort()).toEqual(['f-0', 'f-1', 'f-2'])
  })

  it('a flood of entries stops the turn as bytes past the cap do, and usage reports it', async () => {
    const id = await session()
    const dirs = new SessionDirectory(files, await root(), { maxEntries: 50 })
    const turn = await dirs.place(id)
    for (let i = 0; i < 80; i++) await writeFile(path.join(turn.dir, `empty-${i}`), '')
    const usage = await dirs.usage(turn)
    expect(usage).toMatchObject({ bytes: 0, overEntries: true })
    dirs.done(turn)

    const dir = await root()
    const { result, warned } = await cappedTurn(
      {
        place: async (sessionId: string) => ({ sessionId, dir, base: new Map<string, string>() }),
        writeBack: noWriteBack,
        usage: async () => ({ bytes: 0, cap: 1_000, overEntries: true }),
        done: () => {},
      },
      { dirCheckMs: 20 },
    )
    expect(JSON.stringify(result)).toContain('too many entries')
    expect(warned.some((w) => w.startsWith('session directory cap:'))).toBe(true)
  })

  it('a burst right after a walk far under the cap is caught at the very next check, and the placement still ends', async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'sessions-'))
    const scripted = scriptedRunner(() => ({ hang: true }))
    m = manager({ sql: db.sql, paths: { stateDir }, run: scripted.runner, workspace: files })
    const { session: started } = await m.start(browser, { origin: 'chat' })
    const id = started.id
    const turnId = randomUUID()
    await db.sql`
      UPDATE ai_sessions SET status = 'running', mode = 'durable', runtime = 'unified', durable_turn_id = ${turnId}, updated_at = now()
      WHERE id = ${id}`
    const dir = await root()
    let walks = 0
    let walksAtStop = 0
    let writeBacks = 0
    let writeBacksAtStop = -1
    const done: unknown[] = []
    const directory = {
      place: async () => ({ sessionId: id, dir, base: new Map<string, string>() }),
      writeBack: async () => {
        writeBacks += 1
        return { written: [], deleted: [], skipped: [], strays: 0, bytes: 0 }
      },
      // Far under the cap at the first check (a quarter), then a burst past it: no check is skipped.
      usage: async () => {
        walks += 1
        return walks === 1 ? { bytes: 250, cap: 1_000 } : { bytes: 10_000, cap: 1_000 }
      },
      done: (p: unknown) => {
        done.push(p)
      },
    }
    const warned: string[] = []
    const warn = console.warn
    console.warn = (...args: unknown[]) => {
      const line = args.map(String).join(' ')
      if (line.startsWith('session directory cap:') && walksAtStop === 0) {
        walksAtStop = walks
        writeBacksAtStop = writeBacks
      }
      warned.push(line)
    }
    try {
      const host = new TurnHost({ core: m.turnCore, sql: db.sql, images: new SessionBlobs(db.sql), hostQueue: 'agent-sessions.pod-a', directory, dirCheckMs: 20 })
      const result = await host.run(
        { sessionId: id, turnId, attempt: 'run:1', message: { turn_id: turnId, author: browser, text: 'fill the disk', images: [], system_append: null } },
        { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' },
      )
      await host.stop({ graceMs: 0, abortWaitMs: 0 })
      expect(JSON.stringify(result)).toContain('grew past the workspace cap (1000 bytes)')
    } finally {
      console.warn = warn
    }
    expect(warned.some((w) => w.startsWith('session directory cap:'))).toBe(true)
    // The next check, one DIR_CHECK_MS on, not after skipped ones: that interval
    // bounds how far past the cap a burst can write before the turn stops.
    expect(walksAtStop).toBe(2)
    // No check skipped to the minute's sync: none had run when the turn stopped.
    expect(writeBacksAtStop).toBe(0)
    expect(done).toHaveLength(1)
  })

  it("an AgentSession turn runs in the session's directory and writes it back at its end", async () => {
    const stateDir = await mkdtemp(path.join(os.tmpdir(), 'sessions-'))
    const sessionsDir = await root()
    let cwd: string | undefined
    const scripted = scriptedRunner((run) => {
      cwd = run.cwd
      // What Claude Code's own Write would leave (Step 9.6); here the script writes it.
      writeFileSync(path.join(run.cwd ?? '', 'out.txt'), 'written in the turn')
      return { reply: 'saved' }
    })
    m = manager({ sql: db.sql, paths: { stateDir, sessionsDir }, run: scripted.runner, workspace: files })
    const { session: started } = await m.start(browser, { origin: 'chat' })
    const id = started.id
    const turnId = randomUUID()
    await db.sql`
      UPDATE ai_sessions SET status = 'running', mode = 'durable', runtime = 'unified', durable_turn_id = ${turnId}, updated_at = now()
      WHERE id = ${id}`
    const directory = new SessionDirectory(files, sessionsDir)
    const host = new TurnHost({ core: m.turnCore, sql: db.sql, images: new SessionBlobs(db.sql), hostQueue: 'agent-sessions.pod-a', directory })
    const result = await host.run(
      { sessionId: id, turnId, attempt: 'run:1', message: { turn_id: turnId, author: browser, text: 'save it', images: [], system_append: null } },
      { heartbeat: () => {}, cancelled: new AbortController().signal, cancelReason: () => 'interrupt' },
    )
    await host.stop({ graceMs: 0, abortWaitMs: 0 })
    expect(result).toMatchObject({ status: 'done' })
    expect(cwd).toBe(directory.dirOf(id))
    expect((await files.read(ws(id), 'out.txt'))?.bytes.toString()).toBe('written in the turn')
    // Its warm process closes: its copy here goes too (main.ts wires warm.ts `onClosed` so).
    expect(existsSync(directory.dirOf(id))).toBe(true)
    const evicted = new Promise<string>((resolve) => m!.warm.onClosed(resolve))
    await m.warm.close()
    await directory.evict(await evicted)
    expect(existsSync(directory.dirOf(id))).toBe(false)
  })
})
