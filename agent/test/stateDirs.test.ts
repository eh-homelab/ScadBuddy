import { mkdtemp, rm, stat, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { claudeConfigDir, scratchDir } from '../src/harness/options.js'
import { ensureSessionDir, ensureStateDirs, sessionWorkDir, StateDirError } from '../src/harness/stateDirs.js'

describe('ensureStateDirs', () => {
  let root: string
  beforeEach(async () => {
    root = await mkdtemp(path.join(tmpdir(), 'agent-state-'))
  })
  afterEach(async () => {
    await rm(root, { recursive: true, force: true })
  })

  it('recreates claude/ and work/ in an empty mounted volume', async () => {
    const paths = { stateDir: root }
    expect(await ensureStateDirs(paths)).toEqual([claudeConfigDir(paths), scratchDir(paths)])
    expect((await stat(claudeConfigDir(paths))).isDirectory()).toBe(true)
    expect((await stat(scratchDir(paths))).isDirectory()).toBe(true)
  })

  it('is idempotent', async () => {
    const paths = { stateDir: root }
    await ensureStateDirs(paths)
    await expect(ensureStateDirs(paths)).resolves.toHaveLength(2)
  })

  it('fails fast, naming the directory, when the state dir cannot hold them', async () => {
    // A file where the state directory should be: mkdir fails with ENOTDIR
    // for any user, root included, unlike a permission-based setup.
    const stateDir = path.join(root, 'not-a-dir')
    await writeFile(stateDir, '')
    const err = await ensureStateDirs({ stateDir }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(StateDirError)
    expect((err as Error).message).toContain(path.join(stateDir, 'claude'))
    expect((err as Error).message).toContain('ENOTDIR')
  })

  it('gives each session a stable working directory under work/sessions', async () => {
    const paths = { stateDir: root }
    const id = '0f8fad5b-d9cb-469f-a165-70867728950e'
    expect(sessionWorkDir(paths, id)).toBe(path.join(scratchDir(paths), 'sessions', id))
    expect(sessionWorkDir(paths, id.toUpperCase())).toBe(sessionWorkDir(paths, id))
    expect(await ensureSessionDir(paths, id)).toBe(sessionWorkDir(paths, id))
    expect((await stat(sessionWorkDir(paths, id))).isDirectory()).toBe(true)
    await expect(ensureSessionDir(paths, id)).resolves.toBe(sessionWorkDir(paths, id))
  })

  it('refuses anything but a UUID as a session directory name', () => {
    for (const bad of ['../../etc', '', 'abc', '0f8fad5b-d9cb-469f-a165-70867728950e/..']) {
      expect(() => sessionWorkDir({ stateDir: root }, bad)).toThrow(StateDirError)
    }
  })
})
