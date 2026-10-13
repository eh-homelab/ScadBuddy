import { mkdtemp, realpath } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import { WebSocketServer } from 'ws'
import { checkSocketUrl, EVENT_CHARS, endNotice, eventNotice, lineSplitter, monitorEnv, pinnedLookup, runCommand, runSocket, shortDuration } from '../src/background/monitor.js'

// A Monitor's sources (background/monitor.ts), as Claude Code's Monitor describes its own:
// stdout lines batched within 200 ms, exit ends the watch with its code, expiry and too
// many events stop it; a WebSocket's frames, binary ones as a placeholder line.

const collect = () => {
  const events: string[] = []
  return { events, onEvent: async (text: string) => (events.push(text), true) }
}

describe('a monitor command', () => {
  it('batches lines within 200 ms, runs in its directory with no secret, and reports the exit code', async () => {
    const cwd = await mkdtemp(path.join(os.tmpdir(), 'monitor-'))
    const { events, onEvent } = collect()
    process.env.SCADBUDDY_SECRET_PROBE = 'x'
    const end = await runCommand({
      command: 'pwd; echo "${SCADBUDDY_SECRET_PROBE:-none}"; sleep 0.5; echo c; exit 3',
      cwd,
      timeoutMs: 10_000,
      signal: new AbortController().signal,
      onEvent,
    })
    delete process.env.SCADBUDDY_SECRET_PROBE
    expect(end).toEqual({ outcome: 'exited', code: 3 })
    expect(events).toEqual([`${await realpath(cwd)}\nnone`, 'c'])
    expect(Object.keys(monitorEnv({ PATH: '/bin', SCADBUDDY_DATABASE_URL: 'pg://x', HOME: '/h' }))).toEqual(['PATH', 'HOME'])
  })

  it('expires, stops on too many events, and stops when cancelled, killing the command', async () => {
    const cwd = os.tmpdir()
    const slow = await runCommand({ command: 'echo up; sleep 30', cwd, timeoutMs: 500, signal: new AbortController().signal, onEvent: collect().onEvent })
    expect(slow).toEqual({ outcome: 'expired' })
    let n = 0
    const noisy = await runCommand({
      command: 'while true; do echo x; sleep 0.3; done',
      cwd,
      timeoutMs: 10_000,
      signal: new AbortController().signal,
      onEvent: async () => ++n <= 2,
    })
    expect(noisy).toEqual({ outcome: 'too_many' })
    const abort = new AbortController()
    setTimeout(() => abort.abort(), 300)
    expect(await runCommand({ command: 'sleep 30', cwd, timeoutMs: 10_000, signal: abort.signal, onEvent: collect().onEvent })).toEqual({
      outcome: 'cancelled',
    })
  }, 20_000)
})

describe('a monitor socket', () => {
  it('makes each text frame an event, names a binary one, and reports the close', async () => {
    const server = new WebSocketServer({ port: 0 })
    server.on('connection', (socket) => {
      socket.send('deploy started')
      socket.send(Buffer.from([1, 2, 3]))
      socket.send('line one\nline two')
      setTimeout(() => socket.close(4000, 'done'), 100)
    })
    const port = (server.address() as { port: number }).port
    const { events, onEvent } = collect()
    const end = await runSocket({ url: `ws://127.0.0.1:${port}`, timeoutMs: 10_000, signal: new AbortController().signal, onEvent })
    server.close()
    expect(events).toEqual(['deploy started', '[binary frame, 3 bytes]', 'line one\nline two'])
    expect(end).toEqual({ outcome: 'closed', code: 4000, reason: 'done' })
  })

  it('opens only ws(s) to an allowed host off this pod', async () => {
    const pub = async () => ['93.184.215.14']
    expect(await checkSocketUrl('wss://events.example.com/stream', pub)).toBeUndefined()
    expect(await checkSocketUrl('https://events.example.com/', pub)).toMatch(/ws: or wss:/)
    expect(await checkSocketUrl('ws://127.0.0.1:8081/', pub)).toMatch(/loopback/)
    expect(await checkSocketUrl('ws://rebound.example/', async () => ['127.0.0.1'])).toMatch(/loopback/)
    expect(await checkSocketUrl('ws://169.254.169.254/', pub)).toMatch(/link-local/)
  })
})

it("writes events and the end as Claude Code's task notifications", () => {
  expect(eventNotice('ab12cd34', 'ticks', 'tick 1')).toBe(
    '<task-notification>\n<task-id>ab12cd34</task-id>\n<summary>Monitor event: "ticks"</summary>\n<event>tick 1</event>\n</task-notification>',
  )
  expect(endNotice('ab12cd34', 'ticks', { outcome: 'exited', code: 3 }, 4, 4000)).toContain('<summary>Monitor "ticks" stream ended</summary>\n<event>exit code 3 · 4 events</event>')
  expect(endNotice('ab12cd34', 'ticks', { outcome: 'expired' }, 1, 4000)).toContain('expired after 4s')
  expect(shortDuration(60_000)).toBe('1m')
})

it('cuts a line that never ends instead of holding it', () => {
  const out: string[] = []
  const lines = lineSplitter((l) => out.push(l))
  for (let i = 0; i < 100; i++) lines.push('x'.repeat(1000))
  lines.push('\nnext\n')
  expect(out).toEqual([`${'x'.repeat(EVENT_CHARS)} […cut]`, 'next'])
})

it('connects a socket only to the addresses the check passed', async () => {
  const lookup = pinnedLookup(['93.184.215.14'])
  const one = await new Promise<string>((resolve) => lookup('rebound.example', {}, (_e, address) => resolve(address as string)))
  expect(one).toBe('93.184.215.14')
})
