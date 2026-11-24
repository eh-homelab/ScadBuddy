import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installNdefStub, type NdefStub } from '../test/ndefStub'
import { NfcSession, nfcSupport, sameRecords, toInit, toWire, type NDEFRecord, type Reading } from './webNfc'

const bytes = (text: string) => new TextEncoder().encode(text)

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
