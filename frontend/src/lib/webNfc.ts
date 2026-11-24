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
  data?: BufferSource | string | { records: NDEFRecordInit[] }
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

/** A record this page cannot hand to Web NFC's `write()`; its message says why. */
export class RecordNotWritableError extends Error {
  override name = 'RecordNotWritableError'
}

/** `bytes` as UTF-8 text, or null when they are not valid UTF-8. */
function utf8(bytes: Uint8Array): string | null {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return null
  }
}

/**
 * The URI record's prefixes, by identifier code, as Web NFC expands them when it reads a "U" record
 * (https://w3c-cg.github.io/web-nfc/, "Parsing NDEF well-known U records"; NFC Forum URI RTD).
 */
const URI_PREFIXES = [
  '', 'http://www.', 'https://www.', 'http://', 'https://', 'tel:', 'mailto:', 'ftp://anonymous:anonymous@', 'ftp://ftp.', 'ftps://',
  'sftp://', 'smb://', 'nfs://', 'ftp://', 'dav://', 'news:', 'telnet://', 'imap:', 'rtsp://', 'urn:', 'pop:', 'sip:', 'sips:', 'tftp:',
  'btspp://', 'btl2cap://', 'btgoep://', 'tcpobex://', 'irdaobex://', 'file://', 'urn:epc:id:', 'urn:epc:tag:', 'urn:epc:pat:', 'urn:epc:raw:',
  'urn:epc:', 'urn:nfc:',
]

/** One record of a raw NDEF message: its TNF, type, id and payload bytes. */
interface RawRecord {
  tnf: number
  type: Uint8Array
  id: Uint8Array
  payload: Uint8Array
}

/**
 * The records of a raw NDEF message (a smart poster's payload), from each record's header:
 * MB, ME, CF, SR, IL and the TNF (NFC Forum NDEF 1.0, section 3.2). Null when the bytes are cut
 * short or a record is chunked (CF), which Web NFC does not take.
 */
function parseMessage(bytes: Uint8Array): RawRecord[] | null {
  const records: RawRecord[] = []
  let at = 0
  const take = (n: number) => {
    if (at + n > bytes.length) return null
    const part = bytes.subarray(at, at + n)
    at += n
    return part
  }
  while (at < bytes.length) {
    const head = take(2)
    if (!head) return null
    const flags = head[0]!
    if (flags & 0x20) return null
    const size = take(flags & 0x10 ? 1 : 4)
    if (!size) return null
    const payloadLength = size.reduce((sum, byte) => sum * 256 + byte, 0)
    const idLength = flags & 0x08 ? take(1)?.[0] : 0
    if (idLength === undefined) return null
    const type = take(head[1]!)
    const id = take(idLength)
    const payload = take(payloadLength)
    if (!type || !id || !payload) return null
    records.push({ tnf: flags & 0x07, type, id, payload })
    if (flags & 0x40) break
  }
  return records.length > 0 && at === bytes.length ? records : null
}

/** The URL of a well-known "U" record's payload, its prefix code expanded; null when it is not one. */
function uriOf(payload: Uint8Array): string | null {
  if (payload.length === 0) return null
  const prefix = URI_PREFIXES[payload[0]!]
  const rest = utf8(payload.subarray(1))
  return prefix === undefined || rest === null ? null : prefix + rest
}

/**
 * The Web NFC record type of a record inside a smart poster, as Web NFC names it when it reads one
 * (https://w3c-cg.github.io/web-nfc/, "Parsing NDEF well-known Sp records"): "url", "text",
 * "smart-poster", a local type such as ":act", "mime", "absolute-url", an external type or "unknown".
 */
function nestedType(record: RawRecord): string | null {
  const type = utf8(record.type) ?? ''
  switch (record.tnf) {
    case 0:
      return 'empty'
    case 1:
      return type === 'U' ? 'url' : type === 'T' ? 'text' : type === 'Sp' ? 'smart-poster' : `:${type}`
    case 2:
      return 'mime'
    case 3:
      return 'absolute-url'
    case 4:
      return type
    case 5:
      return 'unknown'
    default:
      return null
  }
}

/** A record inside a smart poster, as a person reads it: its type, its text when it has one, and its payload as hex. */
export interface PosterRecord {
  recordType: string
  text: string | null
  hex: string
}

/** What a smart poster's payload holds, record by record; null when it is no NDEF message this page reads. */
export function posterRecords(dataHex: string): PosterRecord[] | null {
  const records = parseMessage(bytesOf(dataHex))
  if (!records) return null
  const out: PosterRecord[] = []
  for (const record of records) {
    const recordType = nestedType(record)
    if (recordType === null) return null
    let text: string | null = null
    if (recordType === 'url') text = uriOf(record.payload)
    else if (recordType === 'text') text = textOf(record.payload)?.text ?? null
    out.push({ recordType, text, hex: hexOf(record.payload) })
  }
  return out
}

/** A well-known "T" record's payload: its status byte (bit 7 UTF-16, bits 0-5 the language's length), language and text. */
function textOf(payload: Uint8Array): { utf16: boolean; lang: string; bytes: Uint8Array; text: string | null } | null {
  if (payload.length === 0) return null
  const status = payload[0]!
  const langEnd = 1 + (status & 0x3f)
  if (langEnd > payload.length) return null
  const bytes = payload.subarray(langEnd)
  const utf16 = (status & 0x80) !== 0
  let text: string | null
  try {
    text = new TextDecoder(utf16 ? 'utf-16be' : 'utf-8', { fatal: true }).decode(bytes)
  } catch {
    text = null
  }
  return { utf16, lang: utf8(payload.subarray(1, langEnd)) ?? '', bytes, text }
}

/** The init of a record inside a smart poster; throws `RecordNotWritableError` for one Web NFC refuses there. */
function nestedInit(record: RawRecord): NDEFRecordInit {
  const recordType = nestedType(record)
  if (recordType === null) throw new RecordNotWritableError('the smart poster holds a record of a reserved type')
  // "Mapping absolute-URL to NDEF": a TypeError when the context is "smart-poster".
  if (recordType === 'absolute-url') throw new RecordNotWritableError('the smart poster holds an absolute-URL record, which Web NFC refuses inside one')
  if (recordType === 'smart-poster') throw new RecordNotWritableError('the smart poster holds another smart poster')
  const init: NDEFRecordInit = { recordType }
  const id = utf8(record.id)
  if (id) init.id = id
  if (recordType === 'url') {
    const url = uriOf(record.payload)
    if (url === null) throw new RecordNotWritableError('the smart poster holds a URL that is not valid text')
    init.data = url
  } else if (recordType === 'text') {
    const text = textOf(record.payload)
    if (!text) throw new RecordNotWritableError('the smart poster holds a text record that is cut short')
    init.encoding = text.utf16 ? 'utf-16' : 'utf-8'
    init.lang = text.lang
    init.data = new Uint8Array(text.bytes)
  } else if (recordType === 'mime') {
    init.mediaType = utf8(record.type) ?? ''
    init.data = new Uint8Array(record.payload)
  } else if (recordType !== 'empty') init.data = new Uint8Array(record.payload)
  return init
}

/**
 * A record to write; a field the wire leaves empty is left out, as the API refuses `mediaType` on a
 * non-MIME record. Web NFC takes each record type's data in its own shape
 * (https://w3c-cg.github.io/web-nfc/, "Creating NDEF message"): a "url" or "absolute-url" record's URL
 * as a string, a "smart-poster" record's records as a message, any other as bytes. A record it would
 * refuse throws `RecordNotWritableError` here, before anything is written.
 */
export function toInit(record: WireRecord): NDEFRecordInit {
  const init: NDEFRecordInit = { recordType: record.record_type }
  if (record.media_type) init.mediaType = record.media_type
  if (record.id) init.id = record.id
  if (record.encoding) init.encoding = record.encoding
  if (record.lang) init.lang = record.lang
  if (record.record_type === 'url' || record.record_type === 'absolute-url') {
    const url = utf8(bytesOf(record.data_hex))
    if (url === null) throw new RecordNotWritableError(`the ${record.record_type} record's URL is not valid text`)
    init.data = url
  } else if (record.record_type === 'smart-poster') {
    const records = parseMessage(bytesOf(record.data_hex))
    if (!records) throw new RecordNotWritableError('the smart poster holds no NDEF message this page can read')
    init.data = { records: records.map(nestedInit) }
  } else if (record.record_type !== 'empty') init.data = bytesOf(record.data_hex)
  return init
}

/** Why these records cannot be written from this page, or null when they can. */
export function notWritable(records: readonly WireRecord[]): string | null {
  try {
    records.forEach(toInit)
    return null
  } catch (cause) {
    if (cause instanceof RecordNotWritableError) return cause.message
    throw cause
  }
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

  /** True once a scan started and until `stop`. */
  get active(): boolean {
    return this.scanning
  }

  private scanning = false
  private readonly activeListeners = new Set<() => void>()

  private setScanning(on: boolean): void {
    if (this.scanning === on) return
    this.scanning = on
    for (const listener of [...this.activeListeners]) listener()
  }

  /** Called whenever `active` changes, whoever started or stopped the scan (the page's Scan, an agent job's Arm, a write). */
  onActiveChange(listener: () => void): () => void {
    this.activeListeners.add(listener)
    return () => this.activeListeners.delete(listener)
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
    if (this.scan === scan) this.setScanning(true)
  }

  stop(): void {
    this.scan?.abort()
    this.scan = null
    this.setScanning(false)
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
