import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { BUILTINS } from '../src/builtins/server.js'
import { OUTPUT_MAX_CHARS, runShell, type ShellContext } from '../src/builtins/sandboxShell.js'
import { runInSandbox, type Sandboxes } from '../src/sandbox/client.js'
import { UNTRUSTED_KEY } from '../src/safety/untrusted.js'
import { LocalSandboxes } from './support/sandbox.js'

const SESSION = '00000000-0000-4000-8000-000000000001'
const limits = { fileMaxBytes: 1024 * 1024, workspaceMaxBytes: 4 * 1024 * 1024 }

describe('Bash in the session sandbox', () => {
  let dir: string
  let sandboxes: LocalSandboxes
  let ctx: ShellContext
  beforeEach(async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'session-'))
    sandboxes = new LocalSandboxes()
    ctx = { sandboxes, sessionId: SESSION, dir, limits: async () => limits, signal: AbortSignal.timeout(60_000) }
  })
  afterEach(async () => {
    await sandboxes.close()
    await rm(dir, { recursive: true, force: true })
  })

  const content = (text: string) => JSON.parse(text)[UNTRUSTED_KEY].content as Record<string, unknown>

  it("syncs the session's files in, runs the command, and brings its changes back", async () => {
    await writeFile(path.join(dir, 'keep.txt'), 'kept')
    await writeFile(path.join(dir, 'change.txt'), 'old')
    await writeFile(path.join(dir, 'gone.txt'), 'bye')
    const result = await runShell(ctx, 'bash', {
      command:
        'cat keep.txt; echo new > change.txt; rm gone.txt; mkdir -p out && echo made > out/made.txt; ' +
        'mkdir -p node_modules/x && echo s > node_modules/x/i.js; mkdir .cache && echo c > .cache/c',
    })
    expect(result.isError).toBeUndefined()
    const body = content(result.text)
    expect(body).toMatchObject({ exit_code: 0, stdout: 'kept', stderr: '' })
    expect((body.files_changed as string[]).sort()).toEqual(['change.txt', 'out/made.txt'])
    expect(body.files_removed).toEqual(['gone.txt'])
    expect(await readFile(path.join(dir, 'change.txt'), 'utf8')).toBe('new\n')
    expect(await readFile(path.join(dir, 'out/made.txt'), 'utf8')).toBe('made\n')
    await expect(readFile(path.join(dir, 'gone.txt'))).rejects.toThrow()
    await expect(readFile(path.join(dir, 'node_modules/x/i.js'))).rejects.toThrow()
    await expect(readFile(path.join(dir, '.cache/c'))).rejects.toThrow()
    // The scratch tree stays in the sandbox for the next call.
    const again = content((await runShell(ctx, 'bash', { command: 'cat node_modules/x/i.js' })).text)
    expect(again.stdout).toBe('s\n')
  })

  it('makes the sandbox match the session directory before each command', async () => {
    await runShell(ctx, 'bash', { command: 'echo one > a.txt' })
    // Written by Claude Code's own Write between two Bash calls; a.txt removed by the session.
    await writeFile(path.join(dir, 'b.txt'), 'two')
    await rm(path.join(dir, 'a.txt'))
    const body = content((await runShell(ctx, 'bash', { command: 'ls' })).text)
    expect(body.stdout).toBe('b.txt\n')
  })

  it('never writes a file back through a link in the session directory', async () => {
    const outside = await mkdtemp(path.join(tmpdir(), 'outside-'))
    try {
      await symlink(outside, path.join(dir, 'out'))
      await runShell(ctx, 'bash', { command: 'mkdir -p out && echo x > out/f.txt' })
      await expect(readFile(path.join(outside, 'f.txt'))).rejects.toThrow()
      expect(await readFile(path.join(dir, 'out/f.txt'), 'utf8')).toBe('x\n')
    } finally {
      await rm(outside, { recursive: true, force: true })
    }
  })

  it('leaves a file past the per-file cap in the sandbox and says so', async () => {
    ctx.limits = async () => ({ fileMaxBytes: 10, workspaceMaxBytes: 1000 })
    const body = content((await runShell(ctx, 'bash', { command: 'head -c 100 /dev/zero > big.bin' })).text)
    expect(body.files_left_in_sandbox).toEqual([{ path: 'big.bin', reason: expect.stringContaining('per-file cap') }])
    await expect(readFile(path.join(dir, 'big.bin'))).rejects.toThrow()
  })

  it('reports a failing command as an error with its exit code and cuts long output', async () => {
    const failed = await runShell(ctx, 'bash', { command: 'echo no >&2; exit 2' })
    expect(failed.isError).toBe(true)
    expect(content(failed.text)).toMatchObject({ exit_code: 2, stderr: 'no\n' })
    const long = content((await runShell(ctx, 'bash', { command: `head -c ${OUTPUT_MAX_CHARS + 50} /dev/zero | tr '\\0' a` })).text)
    expect((long.stdout as string).length).toBe(OUTPUT_MAX_CHARS)
    expect(long.stdout_cut).toContain(`${OUTPUT_MAX_CHARS + 50} bytes`)
  })

  it('says a command that ran out of time was killed', async () => {
    const body = content((await runShell(ctx, 'bash', { command: 'sleep 20', timeout: 300 })).text)
    expect(body.timed_out).toContain('300 ms')
  })

  it('refuses run_in_background', async () => {
    const result = await runShell(ctx, 'bash', { command: 'sleep 1', run_in_background: true })
    expect(result).toMatchObject({ isError: true, text: expect.stringContaining('Monitor') })
    expect(sandboxes.acquires).toBe(0)
  })

  it("answers an error when the session's sandbox cannot be had", async () => {
    const broken: Sandboxes = { acquire: async () => Promise.reject(new Error('no pod')) }
    const result = await runShell({ ...ctx, sandboxes: broken }, 'bash', { command: 'true' })
    expect(result).toMatchObject({ isError: true, text: expect.stringContaining('no pod') })
  })

  it('looks for the pod again once when a known one stopped answering', async () => {
    let calls = 0
    const live = await sandboxes.acquire(SESSION)
    const released: string[] = []
    const flaky: Sandboxes = {
      acquire: async () => (++calls === 1 ? { url: 'http://127.0.0.1:1', token: live.token } : live),
      release: (_s, e) => released.push(e.url),
    }
    const run = await runInSandbox({ sandboxes: flaky, sessionId: SESSION, dir, limits, signal: AbortSignal.timeout(30_000) }, 'bash', 'echo hi', 5_000)
    expect(run.result.stdout).toBe('hi\n')
    expect(released).toEqual(['http://127.0.0.1:1'])
  })

  it('runs one call at a time per session', async () => {
    const a = runShell(ctx, 'bash', { command: 'echo a >> log.txt; sleep 0.5; echo a2 >> log.txt' })
    const b = runShell(ctx, 'bash', { command: 'echo b >> log.txt' })
    await Promise.all([a, b])
    expect(await readFile(path.join(dir, 'log.txt'), 'utf8')).toBe('a\na2\nb\n')
  })

  it('is served as Bash and PowerShell, and says so when no sandbox is set up', async () => {
    expect(BUILTINS.Bash).toBeDefined()
    expect(BUILTINS.PowerShell).toBeDefined()
    const result = await BUILTINS.Bash!.run({ web: undefined as never }, { command: 'true' } as never)
    expect(result).toMatchObject({ isError: true, text: expect.stringContaining('no sandbox') })
  })

  it.skipIf(!process.env.PATH?.split(':').some((p) => p && existsSync(path.join(p, 'pwsh'))))('runs PowerShell', async () => {
    await mkdir(dir, { recursive: true })
    const body = content((await runShell(ctx, 'pwsh', { command: 'Write-Output (1 + 2)' })).text)
    expect(body).toMatchObject({ exit_code: 0, stdout: '3\n' })
  })
})
