import { spawn } from 'node:child_process'
import type { LookupFunction } from 'node:net'
import WebSocket from 'ws'
import { assertHostAllowed, embeddedIPv4, isLoopbackHost, type Resolver, systemResolver } from '../http/egress.js'

// A Monitor's source, as Claude Code's (its tool description, 2.1.296): "Each stdout line
// is an event", "Stdout lines within 200ms are batched into a single notification",
// "Exit ends the watch (exit code is reported)", "Every monitor expires after
// `timeout_ms` (default 5 minutes, at most 30 minutes)", "Monitors that produce too many
// events are automatically stopped"; or a WebSocket, "Each text frame becomes one
// notification", "Binary frames are reported as `[binary frame, N bytes]`", "Socket
// close ends the watch with the close code surfaced".

export const MONITOR_DEFAULT_MS = 300_000
export const MONITOR_MAX_MS = 1_800_000
export const BATCH_MS = 200
/** One event's text at most: a longer line, or batch, is cut (store.ts EVENT_MAX_CHARS). */
export const EVENT_CHARS = 8000

/**
 * Splits a stream into lines, never holding more than EVENT_CHARS of one: a longer line
 * is cut there and the rest of it dropped, so a command that writes no newline cannot
 * grow the service's memory.
 */
export function lineSplitter(onLine: (line: string) => void): { push(chunk: string): void; end(): void } {
  let line = ''
  let dropping = false
  return {
    push(chunk) {
      let from = 0
      for (;;) {
        const nl = chunk.indexOf('\n', from)
        const part = chunk.slice(from, nl < 0 ? undefined : nl)
        if (!dropping) {
          line += part
          if (line.length > EVENT_CHARS) {
            onLine(`${line.slice(0, EVENT_CHARS)} […cut]`)
            line = ''
            dropping = true
          }
        }
        if (nl < 0) return
        if (!dropping) onLine(line)
        line = ''
        dropping = false
        from = nl + 1
      }
    },
    end() {
      if (line && !dropping) onLine(line)
      line = ''
    },
  }
}

/** How a watch ended. */
export type MonitorEnd =
  | { outcome: 'exited'; code: number | null; signal?: string | undefined }
  | { outcome: 'closed'; code: number; reason?: string | undefined }
  | { outcome: 'expired' }
  | { outcome: 'too_many' }
  | { outcome: 'cancelled' }
  | { outcome: 'failed'; reason: string }

/** Takes one event; false stops the watch (too many). */
export type EventSink = (text: string) => Promise<boolean>

/** The command's environment: no secret of the service, only what a shell needs. */
export function monitorEnv(env: NodeJS.ProcessEnv = process.env): Record<string, string> {
  const kept: Record<string, string> = {}
  for (const name of ['PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'TERM']) {
    const value = env[name]
    if (value !== undefined) kept[name] = value
  }
  return kept
}

/** Runs `command` with bash in `cwd` until it exits, expires, makes too many events, or `signal` aborts. */
export function runCommand(options: { command: string; cwd: string; timeoutMs: number; signal: AbortSignal; onEvent: EventSink }): Promise<MonitorEnd> {
  return new Promise((resolve) => {
    const child = spawn('bash', ['-c', options.command], { cwd: options.cwd, env: monitorEnv(), detached: true, stdio: ['ignore', 'pipe', 'ignore'] })
    let ended: MonitorEnd | undefined
    let batch: string[] = []
    let batchChars = 0
    let dropped = 0
    let timer: NodeJS.Timeout | undefined
    let writing = Promise.resolve()
    const kill = (why: MonitorEnd) => {
      ended ??= why
      try {
        if (child.pid) process.kill(-child.pid, 'SIGKILL')
      } catch {
        // gone already
      }
    }
    const flush = () => {
      timer = undefined
      if (!batch.length) return
      const text = batch.join('\n') + (dropped ? `\n[…${dropped} more line${dropped === 1 ? '' : 's'} in this batch cut]` : '')
      batch = []
      batchChars = 0
      dropped = 0
      writing = writing.then(async () => {
        if (!(await options.onEvent(text))) kill({ outcome: 'too_many' })
      })
    }
    const lines = lineSplitter((line) => {
      // One batch is one event: past EVENT_CHARS its lines are counted, not kept.
      if (batchChars + line.length > EVENT_CHARS) dropped += 1
      else {
        batch.push(line)
        batchChars += line.length + 1
      }
      timer ??= setTimeout(flush, BATCH_MS)
    })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk: string) => lines.push(chunk))
    child.stdout.on('end', () => lines.end())
    const expiry = setTimeout(() => kill({ outcome: 'expired' }), options.timeoutMs)
    const onAbort = () => kill({ outcome: 'cancelled' })
    options.signal.addEventListener('abort', onAbort, { once: true })
    child.on('error', (err) => kill({ outcome: 'failed', reason: err.message }))
    child.on('close', (code, signal) => {
      clearTimeout(expiry)
      if (timer) clearTimeout(timer)
      flush()
      options.signal.removeEventListener('abort', onAbort)
      void writing.finally(() => resolve(ended ?? { outcome: 'exited', code, ...(signal ? { signal } : {}) }))
    })
  })
}

/** Why a WebSocket may not open `url` (ws(s) to an allowed host off this pod), or undefined. */
export async function checkSocketUrl(url: string, resolve: Resolver = systemResolver): Promise<string | undefined> {
  const checked = await socketAddresses(url, resolve)
  return typeof checked === 'string' ? checked : undefined
}

/** The addresses a WebSocket to `url` may connect to, or why it may not. */
export async function socketAddresses(url: string, resolve: Resolver = systemResolver): Promise<string[] | string> {
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return `ws.url is not a URL: ${url}`
  }
  if (parsed.protocol !== 'ws:' && parsed.protocol !== 'wss:') return 'ws.url must be a ws: or wss: URL'
  if (parsed.username || parsed.password) return 'ws.url must not carry a user or password'
  if (isLoopbackHost(parsed.hostname)) return `ws.url host ${parsed.hostname} is this pod's loopback`
  try {
    const addresses = await assertHostAllowed(parsed.href.replace(/^ws/, 'http'), resolve, 'ws.url', 'an allowed host')
    const loopback = addresses.find((a) => isLoopbackHost(embeddedIPv4(a) ?? a))
    if (loopback !== undefined) return `ws.url host ${parsed.hostname} resolves to ${loopback}, this pod's loopback`
    return addresses
  } catch (err) {
    return (err as Error).message
  }
}

/** A `lookup` that answers only with the addresses the egress check passed (DNS rebinding). */
export function pinnedLookup(addresses: readonly string[]): LookupFunction {
  return (_host, options, callback) => {
    const all = addresses.map((address) => ({ address, family: address.includes(':') ? 6 : 4 }))
    if ((options as { all?: boolean }).all) (callback as (e: null, a: typeof all) => void)(null, all)
    else callback(null, all[0]!.address, all[0]!.family)
  }
}

/**
 * Streams a WebSocket's frames until it closes, expires, makes too many events, or
 * `signal` aborts. It connects only to `addresses` (what checkSocketUrl passed), and
 * follows no redirect.
 */
export function runSocket(options: {
  url: string
  protocols?: string[] | undefined
  addresses?: readonly string[] | undefined
  timeoutMs: number
  signal: AbortSignal
  onEvent: EventSink
}): Promise<MonitorEnd> {
  return new Promise((resolve) => {
    let ended: MonitorEnd | undefined
    let writing = Promise.resolve()
    const socket = new WebSocket(options.url, options.protocols, {
      followRedirects: false,
      maxPayload: 1024 * 1024,
      ...(options.addresses?.length ? { lookup: pinnedLookup(options.addresses) } : {}),
    })
    const stop = (why: MonitorEnd) => {
      ended ??= why
      socket.close()
    }
    const expiry = setTimeout(() => stop({ outcome: 'expired' }), options.timeoutMs)
    const onAbort = () => stop({ outcome: 'cancelled' })
    options.signal.addEventListener('abort', onAbort, { once: true })
    socket.on('message', (data: Buffer, isBinary: boolean) => {
      const text = isBinary ? `[binary frame, ${data.byteLength} bytes]` : data.toString('utf8').slice(0, EVENT_CHARS)
      writing = writing.then(async () => {
        if (!(await options.onEvent(text))) stop({ outcome: 'too_many' })
      })
    })
    socket.on('error', (err: Error) => {
      ended ??= { outcome: 'failed', reason: `the WebSocket failed: ${err.message}` }
    })
    socket.on('close', (code: number, reason: Buffer) => {
      clearTimeout(expiry)
      options.signal.removeEventListener('abort', onAbort)
      const why = reason.toString('utf8')
      void writing.finally(() => resolve(ended ?? { outcome: 'closed', code, ...(why ? { reason: why } : {}) }))
    })
  })
}

/** A duration as Claude Code's Monitor names it: "4s", "1m", "30m". */
export function shortDuration(ms: number): string {
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`
  return `${Math.round(ms / 60_000)}m`
}

const notification = (taskId: string, summary: string, event: string) =>
  `<task-notification>\n<task-id>${taskId}</task-id>\n<summary>${summary}</summary>\n<event>${event}</event>\n</task-notification>`

/** One event, as Claude Code's notification of it. */
export function eventNotice(taskId: string, description: string, text: string): string {
  return notification(taskId, `Monitor event: "${description}"`, text)
}

/** The watch's end, with how many events it made. */
export function endNotice(taskId: string, description: string, end: MonitorEnd, events: number, timeoutMs: number): string {
  const count = `${events} event${events === 1 ? '' : 's'}`
  switch (end.outcome) {
    case 'exited':
      return notification(taskId, `Monitor "${description}" stream ended`, `exit code ${end.code ?? end.signal ?? 'unknown'} · ${count}`)
    case 'closed':
      return notification(taskId, `Monitor "${description}" stream ended`, `socket closed with code ${end.code}${end.reason ? ` (${end.reason})` : ''} · ${count}`)
    case 'expired':
      return notification(taskId, `Monitor "${description}" expired after ${shortDuration(timeoutMs)}`, `${count}; re-arm it if you still need the watch`)
    case 'too_many':
      return notification(taskId, `Monitor "${description}" stopped: too many events`, `${count}; restart it with a tighter filter`)
    case 'cancelled':
      return notification(taskId, `Monitor "${description}" stopped`, count)
    case 'failed':
      return notification(taskId, `Monitor "${description}" failed`, `${end.reason} · ${count}`)
  }
}
