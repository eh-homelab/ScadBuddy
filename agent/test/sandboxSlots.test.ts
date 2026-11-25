import { existsSync, readFileSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { followSlot, NOTICE_PATHS_MAX, shellNotice } from '../src/background/sandboxSource.js'
import { type BackgroundTask, NOTICE_MAX_CHARS } from '../src/background/store.js'
import type { BackgroundContext } from '../src/builtins/background.js'
import { runShell, type ShellContext } from '../src/builtins/sandboxShell.js'
import { catchUpInSandbox, runInSandbox, SandboxClient, startInSandbox, WATCH_LINE_MAX_BYTES } from '../src/sandbox/client.js'
import { MAX_SLOTS, SLOT_KEEP_BYTES } from '../src/sandbox/protocol.js'
import { reapStrays } from '../src/sandbox/server.js'
import { UNTRUSTED_KEY } from '../src/safety/untrusted.js'
import { LocalSandboxes, startLocalSandbox } from './support/sandbox.js'

// Background slots in the session's sandbox (#5916, #7310): a Monitor's command and a background
// Bash run in the pod beside the foreground commands, and their writes come back to the session.

const SESSION = '00000000-0000-4000-8000-000000000002'
const limits = { fileMaxBytes: 1024 * 1024, workspaceMaxBytes: 4 * 1024 * 1024 }
/** Whether a process runs: a zombie (killed, not yet reaped by its new parent) does not. */
const alive = (pid: number) => {
  try {
    return !/^\d+ \(.*\) Z /.test(readFileSync(`/proc/${pid}/stat`, 'utf8'))
  } catch {
    return false
  }
}
const until = async (check: () => boolean | Promise<boolean>, ms = 10_000) => {
  const end = Date.now() + ms
  while (!(await check())) {
    if (Date.now() > end) throw new Error('timed out waiting')
    await new Promise((r) => setTimeout(r, 25))
  }
}

/** Every output byte of a slot, by its watch, and its end. */
async function drain(client: SandboxClient, id: string, from = 0) {
  const out: { stdout: string; stderr: string; lost: number; status?: unknown; gone?: boolean } = { stdout: '', stderr: '', lost: 0 }
  for await (const event of client.watch(id, from)) {
    if (event.type === 'data') out[event.stream] += event.data.toString('utf8')
    if (event.type === 'lost') out.lost += event.to - event.from
    if (event.type === 'end') out.status = event.status
    if (event.type === 'gone') out.gone = true
  }
  return out
}

describe('background slots in the exec server', () => {
  let box: Awaited<ReturnType<typeof startLocalSandbox>>
  let client: SandboxClient
  beforeEach(async () => {
    box = await startLocalSandbox({ slots: { heartbeatMs: 200 } })
    client = new SandboxClient(box.endpoint, AbortSignal.timeout(60_000))
  })
  afterEach(async () => {
    box.server.closeAllConnections()
    await box.close()
  })

  it('runs a slot beside a foreground command and streams its output to its end', async () => {
    const slot = await client.startSlot('bash', 'echo out; echo err >&2; sleep 0.3; echo done; exit 3', 10_000)
    // A foreground command runs while the slot does.
    const fg = await client.exec('bash', 'echo fg', 10_000)
    expect(fg.stdout).toBe('fg\n')
    const out = await drain(client, slot.id)
    expect(out.stdout).toBe('out\ndone\n')
    expect(out.stderr).toBe('err\n')
    expect(out.status).toMatchObject({ running: false, exit_code: 3, timed_out: false, stopped: false })
    // Its output file in the pod's HOME holds both streams.
    expect(slot.output_file.startsWith(box.work.replace(/work$/, 'home'))).toBe(true)
    await until(async () => (await readFile(slot.output_file, 'utf8').catch(() => '')).includes('done'))
  })

  it('a watch from an offset gets only what follows it', async () => {
    const slot = await client.startSlot('bash', 'printf abcdef', 10_000)
    const out = await drain(client, slot.id, 3)
    expect(out.stdout).toBe('def')
  })

  it(`refuses a start past ${MAX_SLOTS} running slots`, async () => {
    const ids = []
    for (let i = 0; i < MAX_SLOTS; i++) ids.push((await client.startSlot('bash', 'sleep 30', 60_000)).id)
    await expect(client.startSlot('bash', 'sleep 30', 60_000)).rejects.toThrow(/409/)
    for (const id of ids) await client.stopSlot(id)
    // A stopped slot frees its place.
    const again = await client.startSlot('bash', 'true', 60_000)
    await drain(client, again.id)
  })

  it("a stop kills the slot's whole process group, a forked child too", async () => {
    const pidFile = path.join(box.work, 'child.pid')
    const slot = await client.startSlot('bash', `sleep 60 & echo $! > ${pidFile}; wait`, 60_000)
    await until(() => existsSync(pidFile))
    const child = Number((await readFile(pidFile, 'utf8')).trim())
    await until(() => alive(child))
    const status = await client.stopSlot(slot.id)
    expect(status).toMatchObject({ running: false, stopped: true })
    await until(() => !alive(child))
    // Stopped and forgotten: no such slot now.
    expect(await client.slotStatus(slot.id)).toBeUndefined()
  })

  it('kills a slot at its timeout', async () => {
    const slot = await client.startSlot('bash', 'sleep 60', 300)
    const out = await drain(client, slot.id)
    expect(out.status).toMatchObject({ running: false, timed_out: true })
  })

  it('keeps only the newest output of a flood, and says what it dropped', async () => {
    const total = SLOT_KEEP_BYTES * 3
    const slot = await client.startSlot('bash', `head -c ${total} /dev/zero | tr '\\0' a`, 30_000)
    await until(async () => (await client.slotStatus(slot.id))?.running === false)
    const out = await drain(client, slot.id)
    expect(out.status).toMatchObject({ bytes: total })
    expect(out.lost).toBeGreaterThan(0)
    expect(out.lost + out.stdout.length).toBe(total)
    expect(out.stdout.length).toBeLessThanOrEqual(SLOT_KEEP_BYTES + 64 * 1024)
  })

  it('refuses an id that is not one of its own: a path, another form, a stranger', async () => {
    const res = (p: string, method = 'GET') => fetch(`${box.endpoint.url}${p}`, { method, headers: { authorization: `Bearer ${box.endpoint.token}` } })
    const slot = await client.startSlot('bash', 'sleep 30', 60_000)
    for (const p of [
      '/v1/slots/..%2F..%2Fetc%2Fpasswd',
      `/v1/slots/${slot.id}/watch/extra`,
      `/v1/slots/${slot.id}/other`,
      `/v1/slots/${slot.id.toUpperCase()}`,
      `/v1/slots/${'0'.repeat(32)}`,
    ]) {
      expect((await res(p)).status, p).not.toBe(200)
      expect((await res(p, 'DELETE')).status, p).not.toBe(200)
    }
    // Another session's sandbox does not know this slot, and a stop there leaves it running.
    const other = await startLocalSandbox()
    try {
      const stranger = new SandboxClient(other.endpoint, AbortSignal.timeout(10_000))
      expect(await stranger.stopSlot(slot.id)).toBeUndefined()
      expect(await client.slotStatus(slot.id)).toMatchObject({ running: true })
      // And this token does not reach it.
      const wrong = new SandboxClient({ url: box.endpoint.url, token: other.endpoint.token }, AbortSignal.timeout(10_000))
      await expect(wrong.stopSlot(slot.id)).rejects.toThrow(/401/)
      expect(await client.slotStatus(slot.id)).toMatchObject({ running: true })
    } finally {
      await other.close()
      await client.stopSlot(slot.id)
    }
  })

  it('stops a slot no watch follows', async () => {
    const lonely = await startLocalSandbox({ slots: { unwatchedMs: 300 } })
    try {
      const c = new SandboxClient(lonely.endpoint, AbortSignal.timeout(10_000))
      const slot = await c.startSlot('bash', 'sleep 60', 60_000)
      await until(async () => (await c.slotStatus(slot.id))?.running === false)
      expect(await c.slotStatus(slot.id)).toMatchObject({ stopped: true })
    } finally {
      await lonely.close()
    }
  })

  it('does not idle out while a slot runs', async () => {
    let idled = 0
    const idle = await startLocalSandbox({ idleMs: 200, onIdle: () => idled++ })
    try {
      const c = new SandboxClient(idle.endpoint, AbortSignal.timeout(10_000))
      const slot = await c.startSlot('bash', 'sleep 1', 60_000)
      await new Promise((r) => setTimeout(r, 700))
      expect(idled).toBe(0)
      await drain(c, slot.id)
      await until(() => idled > 0, 3000)
    } finally {
      idle.server.closeAllConnections()
      await idle.close()
    }
  })
})

describe('a watch from a pod that lies', () => {
  /** A pod whose watch sends `body` and then holds the stream open or ends it. */
  async function lying(body: Buffer | string, end = true): Promise<{ client: SandboxClient; close: () => Promise<void> }> {
    const server = createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'application/x-ndjson' })
      res.write(body)
      if (end) res.end()
    })
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
    return {
      client: new SandboxClient({ url, token: 't' }, AbortSignal.timeout(10_000)),
      close: () =>
        new Promise((r) => {
          server.closeAllConnections()
          server.close(() => r())
        }),
    }
  }
  const id = 'a'.repeat(32)
  const collect = async (c: SandboxClient, heartbeatMs?: number) => {
    const out: unknown[] = []
    for await (const e of c.watch(id, 0, heartbeatMs)) out.push(e)
    return out
  }

  it('takes raw bytes after a data line, exactly as many as it names', async () => {
    const pod = await lying(`{"type":"data","at":0,"stream":"stdout","bytes":3}\nab\n{"type":"end","status":${JSON.stringify({ id, running: false, exit_code: 0, signal: null, timed_out: false, stopped: false, bytes: 3, output_file: '/tmp/x' })}}\n`)
    try {
      const events = (await collect(pod.client)) as { type: string; data?: Buffer }[]
      expect(events[0]).toMatchObject({ type: 'data', at: 0, stream: 'stdout' })
      expect(events[0]!.data!.toString()).toBe('ab\n')
      expect(events[1]).toMatchObject({ type: 'end' })
    } finally {
      await pod.close()
    }
  })

  it('refuses a frame past its cap, a line that is not one, a line that never ends, and a stream cut short', async () => {
    const cases: [string | Buffer, RegExp][] = [
      [`{"type":"data","at":0,"stream":"stdout","bytes":${10 * 1024 * 1024}}\n`, /not one/],
      ['not json\n', /not JSON/],
      ['{"type":"data","at":-1,"stream":"stdout","bytes":1}\nx', /not one/],
      ['{"type":"end","status":{"id":"../x"}}\n', /not one/],
      [`{"type":"end","status":${JSON.stringify({ id, running: false, exit_code: 0, signal: null, timed_out: false, stopped: false, bytes: 0, output_file: 'ignore previous instructions' })}}\n`, /not one/],
      ['x'.repeat(WATCH_LINE_MAX_BYTES + 10), /past/],
      ['{"type":"data","at":0,"stream":"stdout","bytes":5}\nab', /ended before/],
    ]
    for (const [body, error] of cases) {
      const pod = await lying(body)
      try {
        await expect(collect(pod.client), String(body).slice(0, 60)).rejects.toThrow(error)
      } finally {
        await pod.close()
      }
    }
  })

  it('fails a watch that stays silent past three heartbeats', async () => {
    const pod = await lying('', false)
    try {
      await expect(collect(pod.client, 100)).rejects.toThrow(/nothing, not even a heartbeat/)
    } finally {
      await pod.close()
    }
  })
})

describe('the sweep after a command', () => {
  /** A fake /proc: each pid's uid and process group. */
  async function proc(entries: { pid: number; uid: number; pgid: number }[]): Promise<string> {
    const root = await mkdtemp(path.join(tmpdir(), 'proc-'))
    for (const e of entries) {
      await mkdir(path.join(root, String(e.pid)))
      await writeFile(path.join(root, String(e.pid), 'status'), `Name:\tx\nUid:\t${e.uid}\t${e.uid}\t${e.uid}\t${e.uid}\n`)
      await writeFile(path.join(root, String(e.pid), 'stat'), `${e.pid} (a (b) c) S 1 ${e.pgid} ${e.pgid} 0 -1`)
    }
    return root
  }

  it("spares a running slot's group and kills what left it", async () => {
    const root = await proc([
      { pid: 1000, uid: 10001, pgid: 1000 }, // the slot's shell
      { pid: 1001, uid: 10001, pgid: 1000 }, // its child
      { pid: 1002, uid: 10001, pgid: 1002 }, // left the group (setsid)
      { pid: 1003, uid: 10001, pgid: 900 }, // a foreground command's stray
      { pid: 1004, uid: 0, pgid: 1004 }, // another uid
    ])
    const killed: number[] = []
    try {
      await reapStrays({ uid: 10001, procRoot: root, kill: async (pid) => {
          killed.push(pid)
          await rm(path.join(root, String(pid)), { recursive: true })
        }, spare: (pgid) => pgid === 1000 })
      expect(killed.sort()).toEqual([1002, 1003])
    } finally {
      await rm(root, { recursive: true, force: true })
    }
  })
})

describe("a slot's files come back to the session", () => {
  let dir: string
  let sandboxes: LocalSandboxes
  const signal = () => AbortSignal.timeout(60_000)
  beforeEach(async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'session-'))
    sandboxes = new LocalSandboxes()
  })
  afterEach(async () => {
    for (const b of sandboxes.started.values()) b.server.closeAllConnections()
    await sandboxes.close()
    await rm(dir, { recursive: true, force: true })
  })
  const call = () => ({ sandboxes, sessionId: SESSION, dir, limits, signal: signal() })

  it("at the next foreground command, never removed as if the session had deleted it", async () => {
    await writeFile(path.join(dir, 'in.txt'), 'session')
    const { slot } = await startInSandbox(call(), 'bash', 'cat in.txt > copy.txt; echo built > out.txt; rm in.txt', 10_000)
    const box = sandboxes.started.get(SESSION)!
    const client = new SandboxClient(box.endpoint, signal())
    await drain(client, slot.id)
    const run = await runInSandbox(call(), 'bash', 'ls', 10_000)
    expect(run.pulled?.written.sort()).toEqual(['copy.txt', 'out.txt'])
    expect(run.pulled?.removed).toEqual(['in.txt'])
    expect(run.result.stdout.split('\n').filter(Boolean).sort()).toEqual(['copy.txt', 'out.txt'])
    expect(await readFile(path.join(dir, 'out.txt'), 'utf8')).toBe('built\n')
    expect(await readFile(path.join(dir, 'copy.txt'), 'utf8')).toBe('session')
    expect(existsSync(path.join(dir, 'in.txt'))).toBe(false)
  })

  it("at its end (catchUp), and the session's own change to the same file counts", async () => {
    await writeFile(path.join(dir, 'both.txt'), 'v0')
    const { slot } = await startInSandbox(call(), 'bash', 'echo slot > both.txt; echo slot > mine.txt', 10_000)
    const client = new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, signal())
    await drain(client, slot.id)
    // The session changed both.txt too (Claude Code's Write) before the slot's end was synced.
    await writeFile(path.join(dir, 'both.txt'), 'session')
    const back = await catchUpInSandbox(call())
    expect(back?.written).toEqual(['mine.txt'])
    expect(await readFile(path.join(dir, 'both.txt'), 'utf8')).toBe('session')
    const run = await runInSandbox(call(), 'bash', 'cat both.txt', 10_000)
    expect(run.result.stdout).toBe('session')
    expect(run.overwritten).toEqual(['both.txt'])
  })

  it("reaches a sync from another worker's directory too, never overwritten by its older copy", async () => {
    await writeFile(path.join(dir, 'out.txt'), 'old')
    const { slot } = await startInSandbox(call(), 'bash', 'echo new > out.txt', 10_000)
    await drain(new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, signal()), slot.id)
    // The worker that followed the slot brings it back into its own directory.
    expect((await catchUpInSandbox(call()))?.written).toEqual(['out.txt'])
    // Another worker's directory still holds the record's older copy: its sync brings the slot's back.
    const other = await mkdtemp(path.join(tmpdir(), 'session-b-'))
    try {
      await writeFile(path.join(other, 'out.txt'), 'old')
      const run = await runInSandbox({ ...call(), dir: other }, 'bash', 'cat out.txt', 10_000)
      expect(run.result.stdout).toBe('new\n')
      expect(run.pulled?.written).toEqual(['out.txt'])
      expect(await readFile(path.join(other, 'out.txt'), 'utf8')).toBe('new\n')
    } finally {
      await rm(other, { recursive: true, force: true })
    }
  })

  it('under the same caps as a foreground command', async () => {
    const { slot } = await startInSandbox({ ...call(), limits: { fileMaxBytes: 10, workspaceMaxBytes: 1000 } }, 'bash', 'head -c 100 /dev/zero > big.bin', 10_000)
    await drain(new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, signal()), slot.id)
    const back = await catchUpInSandbox({ ...call(), limits: { fileMaxBytes: 10, workspaceMaxBytes: 1000 } })
    expect(back?.written).toEqual([])
    expect(back?.left[0]).toMatchObject({ path: 'big.bin', reason: expect.stringContaining('per-file cap of 10 bytes') })
    expect(existsSync(path.join(dir, 'big.bin'))).toBe(false)
  })
})

describe("following a slot from the worker (background/sandboxSource.ts)", () => {
  let dir: string
  let sandboxes: LocalSandboxes
  beforeEach(async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'session-'))
    sandboxes = new LocalSandboxes()
  })
  afterEach(async () => {
    for (const b of sandboxes.started.values()) b.server.closeAllConnections()
    await sandboxes.close()
    await rm(dir, { recursive: true, force: true })
  })
  const call = () => ({ sandboxes, sessionId: SESSION, dir, limits, signal: AbortSignal.timeout(60_000) })
  const task = (spec: Record<string, unknown>): BackgroundTask =>
    ({ id: 'abcd1234', sessionId: SESSION, kind: 'monitor', description: 'watch', spec, state: 'active' }) as unknown as BackgroundTask
  const deps = () => ({ sandboxes, limits: async () => limits })

  it("makes a Monitor's stdout lines its events, batched, and ends on exit", async () => {
    const { slot } = await startInSandbox(call(), 'bash', 'echo a; echo b; echo err >&2; sleep 0.5; echo c', 10_000)
    const events: string[] = []
    const end = await followSlot(deps(), task({ sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } }), {
      dir,
      timeoutMs: 10_000,
      signal: new AbortController().signal,
      onEvent: async (text) => (events.push(text), true),
      onNotice: async () => {},
    })
    expect(end).toEqual({ outcome: 'exited', code: 0 })
    expect(events).toEqual(['a\nb', 'c'])
  })

  it('stops the slot when it makes too many events', async () => {
    const { slot } = await startInSandbox(call(), 'bash', 'while true; do echo tick; sleep 0.25; done', 60_000)
    let n = 0
    const end = await followSlot(deps(), task({ sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } }), {
      dir,
      timeoutMs: 60_000,
      signal: new AbortController().signal,
      onEvent: async () => ++n < 2,
      onNotice: async () => {},
    })
    expect(end).toEqual({ outcome: 'too_many' })
    const client = new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, AbortSignal.timeout(10_000))
    expect(await client.slotStatus(slot.id)).toBeUndefined()
  })

  it('stops the slot in the pod when its task is cancelled', async () => {
    const { slot } = await startInSandbox(call(), 'bash', 'sleep 60', 60_000)
    const cancel = new AbortController()
    setTimeout(() => cancel.abort(), 300)
    const end = await followSlot(deps(), task({ sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } }), {
      dir,
      timeoutMs: 60_000,
      signal: cancel.signal,
      onEvent: async () => true,
      onNotice: async () => {},
    })
    expect(end).toEqual({ outcome: 'cancelled' })
    const client = new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, AbortSignal.timeout(10_000))
    expect(await client.slotStatus(slot.id)).toBeUndefined()
  })

  it("ends with a clear reason when the session's pod is gone", async () => {
    const { slot } = await startInSandbox(call(), 'bash', 'sleep 60', 60_000)
    const box = sandboxes.started.get(SESSION)!
    box.server.closeAllConnections()
    await box.close()
    sandboxes.started.delete(SESSION)
    const end = await followSlot(deps(), task({ sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } }), {
      dir,
      timeoutMs: 60_000,
      signal: new AbortController().signal,
      onEvent: async () => true,
      onNotice: async () => {},
    })
    expect(end).toMatchObject({ outcome: 'failed', reason: expect.stringContaining('sandbox pod ended') })
  })

  it("reports a background shell's end once, as a notice, with its output's tail and the files it changed", async () => {
    const { slot } = await startInSandbox(call(), 'bash', 'echo working; echo oops >&2; echo r > result.txt; exit 1', 10_000)
    const events: string[] = []
    const notices: string[] = []
    const t = task({ background: 'shell', description: 'build', sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } })
    const end = await followSlot(deps(), t, {
      dir,
      timeoutMs: 10_000,
      signal: new AbortController().signal,
      onEvent: async (text) => (events.push(text), true),
      onNotice: async (text) => void notices.push(text),
    })
    expect(end).toEqual({ outcome: 'exited', code: 1 })
    expect(events).toEqual([])
    expect(notices).toHaveLength(1)
    // Claude Code's notice as it is, not wrapped as a Monitor event.
    expect(notices[0]).toMatch(/^<task-notification>\n<task-id>abcd1234<\/task-id>/)
    expect(notices[0]).toContain('<status>failed</status>')
    expect(notices[0]).toContain('Background command "build" failed with exit code 1')
    expect(notices[0]).toContain('working\noops\n')
    expect(notices[0]).toContain('<files-changed>result.txt</files-changed>')
    expect(await readFile(path.join(dir, 'result.txt'), 'utf8')).toBe('r\n')
    expect(shellNotice(t, { outcome: 'cancelled' })).toContain('was stopped')
  })

  it("keeps the end of a long output, the file lists and the closing tags within a notice's cap", async () => {
    const lines = 'for i in $(seq 1 3000); do echo "line $i of the build"; done'
    const { slot } = await startInSandbox(call(), 'bash', `${lines}; echo built > out.js; echo LAST`, 30_000)
    const notices: string[] = []
    const t = task({ background: 'shell', description: 'build', sandbox: { slot: slot.id, shell: 'bash', outputFile: slot.output_file } })
    await followSlot(deps(), t, { dir, timeoutMs: 30_000, signal: new AbortController().signal, onEvent: async () => true, onNotice: async (text) => void notices.push(text) })
    const notice = notices[0]!
    expect(notice.length).toBeLessThanOrEqual(NOTICE_MAX_CHARS)
    expect(notice).toContain('LAST\n</output>')
    expect(notice).toMatch(/\[the first \d+ characters are not shown\]/)
    expect(notice).toContain('<files-changed>out.js</files-changed>')
    expect(notice.endsWith('</task-notification>')).toBe(true)
    // So many changed files that their list alone would not fit: it is capped, and still closes.
    const many = { written: Array.from({ length: 500 }, (_, i) => `dir/file-${i}-${'x'.repeat(150)}.txt`), removed: [], left: [] }
    const capped = shellNotice(t, { outcome: 'exited', code: 0 }, { tail: 'y'.repeat(50_000), cut: 0 }, many)
    expect(capped.length).toBeLessThanOrEqual(NOTICE_MAX_CHARS)
    expect(capped).toContain(`and ${500 - NOTICE_PATHS_MAX} more</files-changed>`)
    expect(capped.endsWith('</task-notification>')).toBe(true)
  })
})

describe('Bash run_in_background in the session sandbox (#7310)', () => {
  let dir: string
  let sandboxes: LocalSandboxes
  let ctx: ShellContext
  beforeEach(async () => {
    dir = await mkdtemp(path.join(tmpdir(), 'session-'))
    sandboxes = new LocalSandboxes()
    ctx = { sandboxes, sessionId: SESSION, dir, limits: async () => limits, signal: AbortSignal.timeout(60_000) }
  })
  afterEach(async () => {
    for (const b of sandboxes.started.values()) b.server.closeAllConnections()
    await sandboxes.close()
    await rm(dir, { recursive: true, force: true })
  })

  function background(created: Record<string, unknown>[], cap = 10): BackgroundContext {
    return {
      sessionId: SESSION,
      owner: { kind: 'browser', id: 'b' },
      tz: 'UTC',
      now: () => new Date(),
      commandQueue: 'agent-tools.pod-a',
      tasks: {
        store: { list: async () => created },
        create: async (t: Record<string, unknown>) => {
          if (created.length >= cap) return undefined
          const row = { ...t, id: `t${created.length}`.padEnd(8, '0') }
          created.push(row)
          return row
        },
      },
    } as unknown as BackgroundContext
  }

  it('starts the command in the sandbox and answers its task and output file', async () => {
    const created: Record<string, unknown>[] = []
    const result = await runShell(ctx, 'bash', { command: 'echo hi; sleep 30', description: 'serve', run_in_background: true }, background(created))
    expect(result.isError).toBeUndefined()
    expect(result.text).toMatch(/^Command running in background with ID: t0000000\. Output is being written to: \//)
    expect(created[0]).toMatchObject({
      kind: 'monitor',
      description: 'serve',
      spec: { command: 'echo hi; sleep 30', background: 'shell', queue: 'agent-tools.pod-a', sandbox: { shell: 'bash' } },
    })
    const slot = (created[0]!.spec as { sandbox: { slot: string; outputFile: string } }).sandbox
    const client = new SandboxClient(sandboxes.started.get(SESSION)!.endpoint, AbortSignal.timeout(10_000))
    expect(await client.slotStatus(slot.slot)).toMatchObject({ running: true })
    // A later foreground command reads its output file.
    await until(async () => {
      const answer = await runShell(ctx, 'bash', { command: `cat ${slot.outputFile}` })
      if (!answer.text.startsWith('{')) throw new Error(answer.text)
      const r = JSON.parse(answer.text)[UNTRUSTED_KEY].content
      return r.stdout === 'hi\n'
    })
    await client.stopSlot(slot.slot)
  })

  it("stops its slot again when the session's task cap refuses the task", async () => {
    const created: Record<string, unknown>[] = []
    const bg = background(created, 0)
    const result = await runShell(ctx, 'bash', { command: 'sleep 30', run_in_background: true }, bg)
    expect(result).toMatchObject({ isError: true })
    const box = sandboxes.started.get(SESSION)
    if (box) {
      const client = new SandboxClient(box.endpoint, AbortSignal.timeout(10_000))
      const res = await fetch(`${box.endpoint.url}/v1/slots`, { headers: { authorization: `Bearer ${box.endpoint.token}` } })
      expect(((await res.json()) as { slots: { running: boolean }[] }).slots.filter((s) => s.running)).toEqual([])
      void client
    }
  })

  it('is refused, saying so, in a session that keeps no background tasks', async () => {
    const result = await runShell(ctx, 'bash', { command: 'sleep 1', run_in_background: true })
    expect(result).toMatchObject({ isError: true, text: expect.stringContaining('keeps no background tasks') })
    expect(sandboxes.acquires).toBe(0)
  })
})
