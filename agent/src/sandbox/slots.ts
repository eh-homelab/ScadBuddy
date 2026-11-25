import type { ChildProcess } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { constants } from 'node:fs'
import { type FileHandle, lstat, mkdir, open, rm } from 'node:fs/promises'
import type { ServerResponse } from 'node:http'
import path from 'node:path'
import {
  MAX_SLOTS,
  SLOT_ENDED_KEEP_MS,
  SLOT_FILE_MAX_BYTES,
  SLOT_KEEP_BYTES,
  SLOT_UNWATCHED_MS,
  type SlotRequest,
  type SlotStarted,
  type SlotStatus,
  WATCH_CHUNK_BYTES,
  WATCH_HEARTBEAT_MS,
  type WatchLine,
} from './protocol.js'

// The exec server's background slots (#5916, #7310; protocol.ts "Background slots"): each runs one
// command in its own process group, keeps the newest SLOT_KEEP_BYTES of its output in memory for
// its watch, and writes the first SLOT_FILE_MAX_BYTES of it to a file in the pod's HOME, where a
// later command can read it. Only Node's built-ins: it ships in the `sandbox` image.

type Chunk = { at: number; stream: 'stdout' | 'stderr'; data: Buffer }

type Slot = {
  id: string
  child: ChildProcess
  pgid: number | undefined
  chunks: Chunk[]
  kept: number
  status: SlotStatus
  ended: Promise<void>
  wake: Set<() => void>
  watchers: number
  lastWatched: number
  timer: NodeJS.Timeout
  file: Promise<FileHandle | undefined>
  fileBytes: number
  writing: Promise<void>
}

export type SlotOptions = {
  /** Spawns `request`'s command detached (its own process group) under the pod's process cap, with the commands' environment. */
  spawn: (request: SlotRequest) => ChildProcess
  /** The commands' HOME: each slot's output file is `<home>/background/<id>.output`. */
  home: string
  /** A slot ended: the server sweeps what it left outside its group (reapStrays), sparing the running ones. */
  onEnd: () => void
  /** Tests only. */
  unwatchedMs?: number
  endedKeepMs?: number
  heartbeatMs?: number
  maxSlots?: number
}

const killGroup = (pgid: number | undefined) => {
  if (pgid === undefined) return
  try {
    process.kill(-pgid, 'SIGKILL')
  } catch {
    // The group is gone already.
  }
}

export class SlotManager {
  readonly #slots = new Map<string, Slot>()
  readonly #options: SlotOptions
  readonly #sweep: NodeJS.Timeout

  constructor(options: SlotOptions) {
    this.#options = options
    const unwatched = options.unwatchedMs ?? SLOT_UNWATCHED_MS
    // A running slot that no watch followed for `unwatched` has lost its watcher: stopped.
    this.#sweep = setInterval(
      () => {
        for (const slot of this.#slots.values()) {
          if (slot.status.running && slot.watchers === 0 && Date.now() - slot.lastWatched > unwatched) this.#stop(slot)
        }
      },
      Math.min(unwatched, 30_000),
    )
    this.#sweep.unref()
  }

  /** Whether any slot runs: the pod does not idle out while one does. */
  get running(): number {
    let n = 0
    for (const slot of this.#slots.values()) if (slot.status.running) n++
    return n
  }

  /** Whether `pgid` is a running slot's group (reapStrays spares it). */
  spares(pgid: number): boolean {
    for (const slot of this.#slots.values()) if (slot.status.running && slot.pgid === pgid) return true
    return false
  }

  list(): SlotStatus[] {
    return [...this.#slots.values()].map((s) => ({ ...s.status }))
  }

  status(id: string): SlotStatus | undefined {
    const slot = this.#slots.get(id)
    return slot ? { ...slot.status } : undefined
  }

  /** Starts a slot, or says why not (MAX_SLOTS running). */
  start(request: SlotRequest): SlotStarted | { refused: string } {
    const max = this.#options.maxSlots ?? MAX_SLOTS
    if (this.running >= max) return { refused: `${max} background commands already run in this sandbox; stop one first` }
    const id = randomBytes(16).toString('hex')
    const outputFile = path.join(this.#options.home, 'background', `${id}.output`)
    const child = this.#options.spawn(request)
    let resolveEnded!: () => void
    const slot: Slot = {
      id,
      child,
      pgid: child.pid,
      chunks: [],
      kept: 0,
      status: { id, running: true, exit_code: null, signal: null, timed_out: false, stopped: false, bytes: 0, output_file: outputFile },
      ended: new Promise((r) => (resolveEnded = r)),
      wake: new Set(),
      watchers: 0,
      lastWatched: Date.now(),
      timer: setTimeout(() => {
        slot.status.timed_out = true
        killGroup(slot.pgid)
      }, request.timeout_ms),
      file: this.#openFile(outputFile),
      fileBytes: 0,
      writing: Promise.resolve(),
    }
    this.#slots.set(id, slot)
    const take = (stream: 'stdout' | 'stderr') => (data: Buffer) => this.#append(slot, stream, data)
    child.stdout?.on('data', take('stdout'))
    child.stderr?.on('data', take('stderr'))
    let spawnError: Error | undefined
    child.on('error', (err) => (spawnError = err))
    // What the command left running in its group goes as soon as its shell exits, as a foreground
    // command's does (server.ts runCommand).
    child.on('exit', () => killGroup(slot.pgid))
    child.on('close', (code, signal) => {
      clearTimeout(slot.timer)
      if (spawnError) this.#append(slot, 'stderr', Buffer.from(`${spawnError.message}\n`))
      slot.status.running = false
      slot.status.exit_code = spawnError ? 127 : code
      slot.status.signal = signal ?? null
      slot.writing = slot.writing.then(async () => {
        const handle = await slot.file
        await handle?.close().catch(() => {})
      })
      this.#wake(slot)
      resolveEnded()
      this.#options.onEnd()
      setTimeout(() => this.#forget(slot), this.#options.endedKeepMs ?? SLOT_ENDED_KEEP_MS).unref()
    })
    return { id, output_file: outputFile }
  }

  /** Stops a slot (its group killed) and waits for its end; its status, then it is forgotten. */
  async stop(id: string): Promise<SlotStatus | undefined> {
    const slot = this.#slots.get(id)
    if (!slot) return undefined
    this.#stop(slot)
    await slot.ended
    this.#forget(slot)
    return { ...slot.status }
  }

  /** Every slot stopped (the server closes). */
  close(): void {
    clearInterval(this.#sweep)
    for (const slot of this.#slots.values()) this.#stop(slot)
  }

  #stop(slot: Slot): void {
    if (!slot.status.running) return
    slot.status.stopped = true
    killGroup(slot.pgid)
  }

  #forget(slot: Slot): void {
    if (this.#slots.get(slot.id) === slot && !slot.status.running) this.#slots.delete(slot.id)
  }

  #wake(slot: Slot): void {
    for (const wake of [...slot.wake]) wake()
  }

  #append(slot: Slot, stream: 'stdout' | 'stderr', data: Buffer): void {
    slot.chunks.push({ at: slot.status.bytes, stream, data })
    slot.status.bytes += data.length
    slot.kept += data.length
    // Only the newest SLOT_KEEP_BYTES stay: a flood of output never grows the server's memory.
    while (slot.kept > SLOT_KEEP_BYTES && slot.chunks.length > 1) slot.kept -= slot.chunks.shift()!.data.length
    this.#toFile(slot, data)
    this.#wake(slot)
  }

  async #openFile(file: string): Promise<FileHandle | undefined> {
    const dir = path.dirname(file)
    try {
      // A command may have put something else at the directory's path: a real directory replaces it.
      const st = await lstat(dir).catch(() => undefined)
      if (st && !st.isDirectory()) await rm(dir, { recursive: true, force: true })
      await mkdir(dir, { recursive: true, mode: 0o700 })
      return await open(file, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    } catch {
      // No file: the watch still has the output.
      return undefined
    }
  }

  #toFile(slot: Slot, data: Buffer): void {
    const room = SLOT_FILE_MAX_BYTES - slot.fileBytes
    if (room <= 0) return
    const part = data.length > room ? data.subarray(0, room) : data
    slot.fileBytes += part.length
    const full = slot.fileBytes >= SLOT_FILE_MAX_BYTES
    slot.writing = slot.writing.then(async () => {
      const handle = await slot.file
      if (!handle) return
      await handle.write(part).catch(() => {})
      if (full) await handle.write(`\n[output past ${SLOT_FILE_MAX_BYTES} bytes is not written to this file]\n`).catch(() => {})
    })
  }

  /**
   * Streams a slot's output from byte `from` as WatchLines until it ends, the client goes, or the
   * slot is forgotten: what is no longer kept is a `lost` line, a quiet spell a `beat` every
   * heartbeat, and the end the slot's status. Backpressure is the socket's: a slow reader slows
   * only its own watch.
   */
  async watch(id: string, from: number, res: ServerResponse): Promise<boolean> {
    const slot = this.#slots.get(id)
    if (!slot) return false
    res.writeHead(200, { 'content-type': 'application/x-ndjson', 'cache-control': 'no-store' })
    slot.watchers++
    let closed = false
    let wakeUp: (() => void) | undefined
    res.on('close', () => {
      closed = true
      wakeUp?.()
    })
    const send = async (line: WatchLine, raw?: Buffer) => {
      if (closed) return
      const head = res.write(`${JSON.stringify(line)}\n`)
      if (!(raw ? res.write(raw) && head : head)) {
        await new Promise<void>((r) => {
          const done = () => {
            res.off('drain', done)
            res.off('close', done)
            r()
          }
          res.on('drain', done)
          res.on('close', done)
        })
      }
    }
    // An offset past what the slot wrote so far waits for it (a watch begun before any output).
    let at = Math.max(0, from)
    try {
      for (;;) {
        if (closed) return true
        const oldest = slot.status.bytes - slot.kept
        if (at < oldest) {
          await send({ type: 'lost', from: at, to: oldest })
          at = oldest
        }
        for (const chunk of slot.chunks.slice()) {
          const end = chunk.at + chunk.data.length
          if (end <= at) continue
          for (let off = Math.max(at - chunk.at, 0); off < chunk.data.length && !closed; off += WATCH_CHUNK_BYTES) {
            const piece = chunk.data.subarray(off, off + WATCH_CHUNK_BYTES)
            await send({ type: 'data', at: chunk.at + off, stream: chunk.stream, bytes: piece.length }, piece)
            at = chunk.at + off + piece.length
          }
        }
        if (!slot.status.running && at >= slot.status.bytes) {
          await send({ type: 'end', status: { ...slot.status } })
          return true
        }
        if (at < slot.status.bytes) continue
        const woken = await new Promise<boolean>((r) => {
          const timer = setTimeout(() => {
            slot.wake.delete(wake)
            r(false)
          }, this.#options.heartbeatMs ?? WATCH_HEARTBEAT_MS)
          const wake = () => {
            clearTimeout(timer)
            slot.wake.delete(wake)
            r(true)
          }
          wakeUp = wake
          slot.wake.add(wake)
        })
        if (!woken) await send({ type: 'beat' })
      }
    } finally {
      slot.watchers--
      slot.lastWatched = Date.now()
      res.end()
    }
  }
}
