import type { NDEFReadingEvent, NDEFRecordInit } from '../lib/webNfc'

/** What one tap of a tag hands the page: its serial number and its records' bytes. */
export interface StubReading {
  serialNumber: string
  records: Array<{ recordType: string; mediaType?: string; data: Uint8Array }>
}

/**
 * #7273 — a stand-in for Web NFC's `NDEFReader` (no test reaches a real one): `tap` is a
 * tag held to the phone, `writes` holds what each `write()` was given.
 */
export interface NdefStub {
  tap(reading: StubReading): void
  /** A tag the reader saw but could not read (`readingerror`). */
  tapError(): void
  writes: NDEFRecordInit[][]
  failNextWrite(error: DOMException): void
  failScan(error: DOMException): void
  restore(): void
  /** How many times `scan()` was called, over every reader. */
  readonly scans: number
  /** Whether a scan runs: started, and its signal not aborted. */
  readonly scanning: boolean
}

export function installNdefStub(): NdefStub {
  const had = 'NDEFReader' in window
  const before = (window as unknown as { NDEFReader?: unknown }).NDEFReader
  const readers = new Set<StubReader>()
  let scanError: DOMException | null = null
  let writeError: DOMException | null = null
  let scans = 0
  const writes: NDEFRecordInit[][] = []

  class StubReader extends EventTarget {
    onreading: ((event: NDEFReadingEvent) => void) | null = null
    onreadingerror: ((event: Event) => void) | null = null
    scanSignal: AbortSignal | null = null

    constructor() {
      super()
      readers.add(this)
    }

    scan(options: { signal?: AbortSignal } = {}): Promise<void> {
      scans += 1
      if (scanError) {
        const error = scanError
        scanError = null
        return Promise.reject(error)
      }
      this.scanSignal = options.signal ?? new AbortController().signal
      return Promise.resolve()
    }

    write(message: { records: NDEFRecordInit[] }, options: { overwrite?: boolean; signal?: AbortSignal } = {}): Promise<void> {
      void options
      if (writeError) {
        const error = writeError
        writeError = null
        return Promise.reject(error)
      }
      writes.push(message.records)
      return Promise.resolve()
    }

    makeReadOnly(): Promise<void> {
      return Promise.resolve()
    }
  }

  ;(window as unknown as { NDEFReader: unknown }).NDEFReader = StubReader

  const live = () => [...readers].filter((reader) => reader.scanSignal && !reader.scanSignal.aborted)

  return {
    tap(reading) {
      for (const reader of live()) {
        const event = Object.assign(new Event('reading'), {
          serialNumber: reading.serialNumber,
          message: {
            records: reading.records.map((record) => ({
              recordType: record.recordType,
              mediaType: record.mediaType,
              id: undefined,
              encoding: undefined,
              lang: undefined,
              data: new DataView(record.data.buffer, record.data.byteOffset, record.data.byteLength),
            })),
          },
        }) as unknown as NDEFReadingEvent
        reader.onreading?.(event)
        reader.dispatchEvent(event as unknown as Event)
      }
    },
    tapError() {
      for (const reader of live()) {
        const event = new Event('readingerror')
        reader.onreadingerror?.(event)
        reader.dispatchEvent(event)
      }
    },
    writes,
    failNextWrite(error) {
      writeError = error
    },
    failScan(error) {
      scanError = error
    },
    restore() {
      if (had) (window as unknown as { NDEFReader: unknown }).NDEFReader = before
      else delete (window as unknown as { NDEFReader?: unknown }).NDEFReader
    },
    get scans() {
      return scans
    },
    get scanning() {
      return live().length > 0
    },
  }
}
