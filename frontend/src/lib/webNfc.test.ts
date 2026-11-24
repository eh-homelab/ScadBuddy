import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installNdefStub, type NdefStub } from '../test/ndefStub'
import { NfcSession, nfcSupport, posterRecords, recordsSha256, RecordNotWritableError, sameRecords, toInit, toWire, type NDEFRecord, type NDEFRecordInit, type Reading } from './webNfc'

const bytes = (text: string) => new TextEncoder().encode(text)
const hex = (text: string) => Array.from(bytes(text), (b) => b.toString(16).padStart(2, '0')).join('')

describe('nfcSupport', () => {
  it('reports no_api without NDEFReader', () => {
    expect('NDEFReader' in window).toBe(false)
    expect(nfcSupport()).toBe('no_api')
  })

  it('reports embedded inside a frame', () => {
    const stub = installNdefStub()
    const top = window.top
    Object.defineProperty(window, 'top', { value: {}, configurable: true })
    try {
      expect(nfcSupport()).toBe('embedded')
    } finally {
      Object.defineProperty(window, 'top', { value: top, configurable: true })
      stub.restore()
    }
    const again = installNdefStub()
    expect(nfcSupport()).toBe('ok')
    again.restore()
  })
})

describe('records', () => {
  it('turns a record into hex and back', () => {
    const data = new Uint8Array([0x00, 0x0f, 0xab, 0xff, 0x7b])
    const record: NDEFRecord = { recordType: 'mime', mediaType: 'application/json', data: new DataView(data.buffer) }
    const wire = toWire(record)
    expect(wire).toMatchObject({ record_type: 'mime', media_type: 'application/json', data_hex: '000fabff7b' })
    const init = toInit(wire)
    expect(init.recordType).toBe('mime')
    expect(init.mediaType).toBe('application/json')
    expect([...(init.data as Uint8Array)]).toEqual([...data])
    expect(toWire({ recordType: 'empty', data: null }).data_hex).toBe('')
  })

  it('gives a URL record its URL as a string, as Web NFC requires', () => {
    // https://w3c-cg.github.io/web-nfc/ "Mapping URL to NDEF": data that is not a string is a TypeError.
    expect(toInit({ record_type: 'url', data_hex: hex('https://example.com/x') })).toEqual({ recordType: 'url', data: 'https://example.com/x' })
    expect(toInit({ record_type: 'absolute-url', data_hex: hex('https://example.com/a') })).toEqual({ recordType: 'absolute-url', data: 'https://example.com/a' })
    expect(() => toInit({ record_type: 'url', data_hex: 'ff' })).toThrow(RecordNotWritableError)
  })

  it('gives a smart poster its records as a message, as Web NFC requires', () => {
    // A "U" record (0x04 "https://", "example.org"), a "T" record ("en", "Hi") and an "act" record (0x00).
    const poster = '91010c5504' + hex('example.org') + '1101055402' + hex('en') + hex('Hi') + '510301' + hex('act') + '00'
    const init = toInit({ record_type: 'smart-poster', id: 'p1', data_hex: poster })
    expect(init.recordType).toBe('smart-poster')
    expect(init.id).toBe('p1')
    const [url, text, act] = (init.data as { records: NDEFRecordInit[] }).records
    expect(url).toEqual({ recordType: 'url', data: 'https://example.org' })
    expect(text).toMatchObject({ recordType: 'text', encoding: 'utf-8', lang: 'en' })
    expect(new TextDecoder().decode(text!.data as Uint8Array)).toBe('Hi')
    expect(act!.recordType).toBe(':act')
    expect([...(act!.data as Uint8Array)]).toEqual([0])
  })

  it('names the smart posters it cannot write', () => {
    // Chunked (CF set), cut short, or an absolute URL, which Web NFC refuses inside a poster.
    expect(() => toInit({ record_type: 'smart-poster', data_hex: 'b1010155' })).toThrow(RecordNotWritableError)
    expect(() => toInit({ record_type: 'smart-poster', data_hex: '91010c5504' })).toThrow(RecordNotWritableError)
    expect(() => toInit({ record_type: 'smart-poster', data_hex: 'd3010078' })).toThrow(RecordNotWritableError)
  })

  it('reads the records a smart poster holds', () => {
    const poster = '91010c5504' + hex('example.org') + '5101055402' + hex('en') + hex('Hi')
    expect(posterRecords(poster)).toEqual([
      { recordType: 'url', text: 'https://example.org', hex: '04' + hex('example.org') },
      { recordType: 'text', text: 'Hi', hex: '02' + hex('en') + hex('Hi') },
    ])
    expect(posterRecords('91')).toBeNull()
  })

  it('compares records by type, media type and data', () => {
    const one = { record_type: 'mime', media_type: 'application/json', data_hex: 'ab' }
    expect(sameRecords([one], [{ ...one, data_hex: 'AB', id: 'x' }])).toBe(true)
    expect(sameRecords([one], [{ ...one, media_type: 'text/plain' }])).toBe(false)
    expect(sameRecords([one], [{ ...one, record_type: 'text' }])).toBe(false)
    expect(sameRecords([one], [{ ...one, data_hex: 'ac' }])).toBe(false)
    expect(sameRecords([one], [one, one])).toBe(false)
    expect(sameRecords([{ record_type: 'empty', data_hex: '' }], [{ record_type: 'empty', media_type: null, data_hex: '' }])).toBe(true)
  })
})

describe('NfcSession', () => {
  let stub: NdefStub
  beforeEach(() => {
    stub = installNdefStub()
  })
  afterEach(() => stub.restore())

  it('keeps one scan running and hands each tap to listeners', async () => {
    const session = new NfcSession()
    const seen: Reading[] = []
    session.onReading((reading) => seen.push(reading))
    await session.start()
    await session.start()
    expect(stub.scans).toBe(1)
    expect(session.active).toBe(true)
    stub.tap({ serialNumber: '04:a2:b3', records: [{ recordType: 'mime', mediaType: 'application/json', data: bytes('{}') }] })
    const next = session.nextReading()
    stub.tap({ serialNumber: '04:a2:b4', records: [] })
    expect(await next).toEqual({ uid: '04:a2:b4', records: [] })
    expect(seen.map((one) => one.uid)).toEqual(['04:a2:b3', '04:a2:b4'])
    expect(seen[0]!.records).toEqual([{ record_type: 'mime', media_type: 'application/json', id: null, encoding: null, lang: null, data_hex: '7b7d' }])
    session.stop()
    expect(session.active).toBe(false)
    expect(stub.scanning).toBe(false)
  })

  it('hands a tag it could not read to the error listeners', async () => {
    const session = new NfcSession()
    let errors = 0
    const off = session.onReadingError(() => (errors += 1))
    await session.start()
    stub.tapError()
    expect(errors).toBe(1)
    off()
    stub.tapError()
    expect(errors).toBe(1)
    session.stop()
  })

  it('tells its listeners when the scan starts and stops', async () => {
    const session = new NfcSession()
    const seen: boolean[] = []
    const off = session.onActiveChange(() => seen.push(session.active))
    await session.start()
    await session.start()
    session.stop()
    session.stop()
    off()
    await session.start()
    expect(seen).toEqual([true, false])
    session.stop()
  })

  it('lets one claim hold at a time', async () => {
    const session = new NfcSession()
    const first = session.claim()
    expect(first).not.toBeNull()
    expect(session.claim()).toBeNull()
    first!()
    const second = session.claim()
    expect(second).not.toBeNull()
    // A release called twice does not free a later claim.
    first!()
    expect(session.claim()).toBeNull()
    second!()
  })

  it('writes with overwrite and never makes a tag read-only', async () => {
    const NDEFReader = (window as unknown as { NDEFReader: { prototype: { makeReadOnly: () => Promise<void>; write: () => Promise<void> } } }).NDEFReader
    const readOnly = vi.spyOn(NDEFReader.prototype, 'makeReadOnly')
    const write = vi.spyOn(NDEFReader.prototype, 'write')
    const session = new NfcSession()
    await session.start()
    const signal = new AbortController().signal
    await session.write([{ record_type: 'mime', media_type: 'application/json', data_hex: '7b7d' }], signal)
    expect(write).toHaveBeenCalledWith({ records: [{ recordType: 'mime', mediaType: 'application/json', data: new Uint8Array([0x7b, 0x7d]) }] }, { overwrite: true, signal })
    expect(stub.writes).toHaveLength(1)
    expect(readOnly).not.toHaveBeenCalled()
    session.stop()
  })
})

describe('recordsSha256 (#7273 R22)', () => {
  // The records as GET /nfc-jobs/{id} answers them; the hex is what the backend's
  // `scadbuddy.filament.models.records_sha256` gives for the same list (computed 2026-11-24
  // with `uvx uv@0.13.9 run python` in backend/).
  it('hashes a record list as the backend does', async () => {
    const records = [
      { record_type: 'text', media_type: null, id: null, encoding: 'utf-8', lang: 'en', data_hex: '5368656c66204233' },
      { record_type: 'mime', media_type: 'application/json', id: 'naïve "x" ✓', encoding: null, lang: null, data_hex: '7b7d' },
    ]
    expect(await recordsSha256(records)).toBe('430aac9b54748997f382a364eb3234b32759d096d10f59a9ae47b49cfc4525e7')
  })

  it('escapes a backslash, a newline and a tab as the backend does', async () => {
    const records = [{ record_type: 'mime', media_type: 'application/x\\y\n\tz', id: 'a\\b\nc\td', encoding: null, lang: 'e\\n\n\t', data_hex: '00ff' }]
    expect(await recordsSha256(records)).toBe('54639c29ef8bb4dc8ab3eeee256fe2753bc9dea0bbc054fee504247f752616c5')
  })

  it('writes an absent field as null, as the backend dumps it', async () => {
    expect(await recordsSha256([{ record_type: 'mime', data_hex: '7b7d' }])).toBe('6b20428036a4096aeef2e2fe7084d6c7c53e2c476789c6643b343a13c563ad29')
  })
})
