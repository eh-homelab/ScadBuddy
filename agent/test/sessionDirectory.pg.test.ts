import { randomUUID } from 'node:crypto'
import { existsSync, writeFileSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { SessionBlobs } from '../src/sessions/blobs.js'
import { forgetSubject } from '../src/sessions/forget.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { TurnHost } from '../src/sessions/turnHost.js'
import { SessionDirectory } from '../src/workspace/directory.js'
import { SessionFiles, type WorkspaceRef } from '../src/workspace/store.js'
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
    expect(await dirs.usage(turn)).toEqual({ bytes: 4000, cap: 1536 })
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
