/**
 * #7273 — Web NFC on the phone page (spec 2026-11-23-web-nfc-tags-design.md). Chrome on
 * Android only, in a top-level secure page: the API rejects `scan()` and `write()` in a frame.
 * The interfaces below are the Web NFC IDL's (https://w3c-cg.github.io/web-nfc/, read
 * 2026-11-23); TypeScript's DOM lib has none. ScadBuddy never calls `makeReadOnly`, which
 * "cannot be reversed".
 */
import type { components } from '../api/schema'
import { isEmbedded } from './embed'

export interface NDEFRecord {
  recordType: string
  mediaType?: string | null
  id?: string | null
  encoding?: string | null
  lang?: string | null
  data?: DataView | null
}

export interface NDEFMessage {
  records: readonly NDEFRecord[]
}

export interface NDEFReadingEvent extends Event {
  serialNumber: string
  message: NDEFMessage
}

export interface NDEFRecordInit {
  recordType: string
  mediaType?: string
  id?: string
  encoding?: string
  lang?: string
  data?: BufferSource | string
}

interface NDEFReader extends EventTarget {
  scan(options?: { signal?: AbortSignal }): Promise<void>
  write(message: { records: NDEFRecordInit[] }, options?: { overwrite?: boolean; signal?: AbortSignal }): Promise<void>
  makeReadOnly(options?: { signal?: AbortSignal }): Promise<void>
}

type NDEFReaderConstructor = new () => NDEFReader

/** One NDEF record as the API carries it: its payload as lower-case hex. */
export type WireRecord = components['schemas']['NdefRecord']

/** One tap: the tag's serial number (`''` when the tag gave none) and its records. */
export interface Reading {
  uid: string
  records: WireRecord[]
}

/** `ok`, or why this page cannot scan: no Web NFC in this browser, or inside a frame (Bambuddy's). */
export function nfcSupport(): 'ok' | 'no_api' | 'embedded' {
  if (isEmbedded()) return 'embedded'
  if (!('NDEFReader' in window)) return 'no_api'
  return 'ok'
}

const hexOf = (bytes: Uint8Array) => Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')

function bytesOf(hex: string): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(hex.length / 2)
  for (let at = 0; at < out.length; at += 1) out[at] = parseInt(hex.slice(at * 2, at * 2 + 2), 16)
  return out
}

export function toWire(record: NDEFRecord): WireRecord {
  const data = record.data ? new Uint8Array(record.data.buffer, record.data.byteOffset, record.data.byteLength) : null
  return {
    record_type: record.recordType,
    media_type: record.mediaType ?? null,
    id: record.id ?? null,
    encoding: record.encoding ?? null,
    lang: record.lang ?? null,
    data_hex: data ? hexOf(data) : '',
  }
}

/** A record to write; a field the wire leaves empty is left out, as the API refuses `mediaType` on a non-MIME record. */
export function toInit(record: WireRecord): NDEFRecordInit {
  const init: NDEFRecordInit = { recordType: record.record_type }
  if (record.media_type) init.mediaType = record.media_type
  if (record.id) init.id = record.id
  if (record.encoding) init.encoding = record.encoding
  if (record.lang) init.lang = record.lang
  if (record.record_type !== 'empty') init.data = bytesOf(record.data_hex)
  return init
}

/** Equal when their `(record_type, media_type, data_hex)` are equal in order (Global Constraints). */
export function sameRecords(a: readonly WireRecord[], b: readonly WireRecord[]): boolean {
  return (
    a.length === b.length &&
    a.every((one, at) => {
      const other = b[at]!
      return (
        one.record_type === other.record_type &&
        (one.media_type ?? null) === (other.media_type ?? null) &&
        one.data_hex.toLowerCase() === other.data_hex.toLowerCase()
      )
    })
  )
}

/**
 * #7273 R22 — the SHA-256, as lower-case hex, of the records' canonical JSON, as the backend's
 * `records_sha256` writes it (`NfcJobClaimIn`): each record with every field (an absent one as
 * `null`) and its keys sorted, no spaces, non-ASCII as itself. A write job's claim carries it.
 */
export async function recordsSha256(records: readonly Partial<WireRecord>[]): Promise<string> {
  const canonical = records.map((record) => {
    const full: Record<string, unknown> = { data_hex: null, encoding: null, id: null, lang: null, media_type: null, record_type: null, ...record }
    return Object.fromEntries(
      Object.keys(full)
        .sort()
        .map((key) => [key, full[key] ?? null]),
    )
  })
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(canonical)))
  return hexOf(new Uint8Array(digest))
}

/**
 * One reader for the page's life: `start` scans once and the reader stays on until `stop`
 * (screen 1), so each tap reaches `onReading`'s listeners and any `nextReading` waiting.
 */
export class NfcSession {
  private reader: NDEFReader | null = null
  private scan: AbortController | null = null
  private readonly listeners = new Set<(reading: Reading) => void>()
  private readonly waiters = new Set<(reading: Reading) => void>()
  private readonly errorListeners = new Set<() => void>()
  private claimed = false
  private readonly onReadingErrorEvent = () => {
    for (const listener of [...this.errorListeners]) listener()
  }
  private readonly onReadingEvent = (event: Event) => {
    const { serialNumber, message } = event as NDEFReadingEvent
    const reading: Reading = { uid: serialNumber, records: message.records.map(toWire) }
    for (const waiter of [...this.waiters]) waiter(reading)
    if (this.claimed) return
    for (const listener of [...this.listeners]) listener(reading)
  }

  get active(): boolean {
    return this.scan !== null
  }

  private ensureReader(): NDEFReader {
    if (!this.reader) {
      const Reader = (window as unknown as { NDEFReader: NDEFReaderConstructor }).NDEFReader
      this.reader = new Reader()
      this.reader.addEventListener('reading', this.onReadingEvent)
      this.reader.addEventListener('readingerror', this.onReadingErrorEvent)
    }
    return this.reader
  }

  /** Rejects as `scan()` does: `NotAllowedError` when NFC is refused to the site. */
  async start(): Promise<void> {
    if (this.scan) return
    const reader = this.ensureReader()
    const scan = new AbortController()
    this.scan = scan
    try {
      await reader.scan({ signal: scan.signal })
    } catch (cause) {
      if (this.scan === scan) this.scan = null
      throw cause
    }
  }

  stop(): void {
    this.scan?.abort()
    this.scan = null
  }

  onReading(listener: (reading: Reading) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** A tag the reader saw but could not read (`readingerror`: no NDEF message it understands, or the tag moved away). */
  onReadingError(listener: () => void): () => void {
    this.errorListeners.add(listener)
    return () => this.errorListeners.delete(listener)
  }

  /**
   * One consumer at a time: while a claim holds, taps reach only `nextReading`, not
   * `onReading`'s listeners, so the taps of a write do not also post reads. A second claim
   * while one holds gets `null`, so two writes never share one tap. Returns the release.
   */
  claim(): (() => void) | null {
    if (this.claimed) return null
    this.claimed = true
    let released = false
    return () => {
      if (released) return
      released = true
      this.claimed = false
    }
  }

  /** The next tap while the scan runs; rejects with `signal`'s reason when it aborts first. */
  nextReading(signal?: AbortSignal): Promise<Reading> {
    return new Promise((resolve, reject) => {
      if (signal?.aborted) {
        reject(signal.reason)
        return
      }
      const done = () => {
        off()
        signal?.removeEventListener('abort', aborted)
      }
      const aborted = () => {
        done()
        reject(signal!.reason)
      }
      const waiter = (reading: Reading) => {
        done()
        resolve(reading)
      }
      const off = () => this.waiters.delete(waiter)
      this.waiters.add(waiter)
      signal?.addEventListener('abort', aborted)
    })
  }

  /** Replaces the tag's whole message (`overwrite: true`); never makes it read-only. */
  write(records: WireRecord[], signal?: AbortSignal): Promise<void> {
    return this.ensureReader().write({ records: records.map(toInit) }, { overwrite: true, signal })
  }
}
