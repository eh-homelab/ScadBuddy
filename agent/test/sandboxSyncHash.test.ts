import { chmod, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { runShell, type ShellContext } from '../src/builtins/sandboxShell.js'
import { UNTRUSTED_KEY } from '../src/safety/untrusted.js'
import type * as directory from '../src/workspace/directory.js'
import { dirFileSha256 } from '../src/workspace/directory.js'
import { LocalSandboxes } from './support/sandbox.js'

// Counts the full hashes of files past the per-file cap (#5944 review): only a kept one is hashed.
const hashed = vi.hoisted(() => [] as string[])
vi.mock('../src/workspace/directory.js', async (importOriginal) => {
  const actual = await importOriginal<typeof directory>()
  return {
    ...actual,
    dirFileSha256: (dir: string, rel: string) => {
      hashed.push(rel)
      return actual.dirFileSha256(dir, rel)
    },
  }
})

const SESSION = '00000000-0000-4000-8000-000000000002'

describe('the sandbox sync and files past the per-file cap', () => {
  let dir: string
  let sandboxes: LocalSandboxes
  let ctx: ShellContext
  beforeEach(async () => {
    hashed.length = 0
    dir = await mkdtemp(path.join(tmpdir(), 'session-'))
    sandboxes = new LocalSandboxes()
    ctx = {
      sandboxes,
      sessionId: SESSION,
      dir,
      limits: async () => ({ fileMaxBytes: 10, workspaceMaxBytes: 1000 }),
      signal: AbortSignal.timeout(60_000),
    }
  })
  afterEach(async () => {
    await sandboxes.close()
    await chmod(path.join(dir, 'locked.txt'), 0o600).catch(() => undefined)
    await rm(dir, { recursive: true, force: true })
  })

  const content = (text: string) => JSON.parse(text)[UNTRUSTED_KEY].content as Record<string, unknown>

  it('never reads a large file that is not kept on each command', async () => {
    await writeFile(path.join(dir, 'big.txt'), 'x'.repeat(100))
    for (let i = 0; i < 3; i++) {
      const body = content((await runShell(ctx, 'bash', { command: 'true' })).text)
      expect(body.files_not_in_sandbox).toEqual([{ path: 'big.txt', reason: expect.stringContaining('per-file cap') }])
    }
    expect(hashed).toEqual([])
  })

  it('still hashes a kept one, so a change to the session copy is seen', async () => {
    await writeFile(path.join(dir, 'big.txt'), 'x'.repeat(100))
    await runShell(ctx, 'bash', { command: 'head -c 50 /dev/zero > big.txt' })
    hashed.length = 0
    const next = content((await runShell(ctx, 'bash', { command: 'wc -c < big.txt' })).text)
    expect(next.stdout).toBe('50\n')
    expect(hashed).toEqual(['big.txt'])
  })

  // The sync reaches it only for a kept file, after readRegular has already given the owner
  // back its read, so the open error is checked on the function itself.
  it.skipIf(process.getuid?.() === 0)('gives no sha256 for a file it cannot open, never an error', async () => {
    const locked = path.join(dir, 'locked.txt')
    await writeFile(locked, 'y'.repeat(100))
    await chmod(locked, 0)
    await expect(dirFileSha256(dir, 'locked.txt')).resolves.toBeUndefined()
  })
})
