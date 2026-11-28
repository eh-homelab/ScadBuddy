// ScadBuddy app (#7905): Web NFC's NDEFReader, backed by Android's own NFC through the app's
// native bridge, so the /scan page runs unchanged. Injected at document start, on the configured
// ScadBuddy origin only (Platforms/Android/NfcBridge.cs). A MIFARE Classic tag (a Bambu spool's)
// reads with no records, and its serial number in the AMS's 8-byte form (the 4-byte UID and
// `:00:00:01:00`, as the AMS reports it), where Chrome gives the 4 bytes; the server takes both. The blocks the app could read go
// to window.ScadBuddyNfc.lastMifare and a `scadbuddy-mifare` window event, for the page to post.
// Any other tag's reading carries `scadbuddyTag`: its chip, user memory, NDEF capacity and whether
// it is writable (frontend/src/lib/webNfc.ts `TagInfo`, #8362). A tag whose NDEF read failed is a
// `readingerror`, never a reading with no records.
(() => {
  const port = window.ScadBuddyNfcNative
  if (!port || window.ScadBuddyNfc) return

  let seq = 0
  const pending = new Map()
  const readers = new Set()

  const call = (op, args = {}) =>
    new Promise((resolve, reject) => {
      const id = ++seq
      pending.set(id, { resolve, reject })
      port.postMessage(JSON.stringify({ id, op, ...args }))
    })

  const viewOf = (hex) => {
    const bytes = new Uint8Array(hex.length / 2)
    for (let at = 0; at < bytes.length; at += 1) bytes[at] = parseInt(hex.slice(at * 2, at * 2 + 2), 16)
    return new DataView(bytes.buffer)
  }
  const hexOf = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  const bytesOfSource = (source) =>
    source instanceof ArrayBuffer
      ? new Uint8Array(source)
      : new Uint8Array(source.buffer, source.byteOffset, source.byteLength)

  // An NDEFRecordInit as the native side builds it: the data as text, bytes or nested records.
  const wireInit = (init) => {
    const out = { recordType: String(init.recordType) }
    for (const key of ['mediaType', 'id', 'encoding', 'lang']) if (init[key] != null) out[key] = String(init[key])
    const data = init.data
    if (data == null) return out
    if (typeof data === 'string') out.text = data
    else if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) out.hex = hexOf(bytesOfSource(data))
    else if (Array.isArray(data.records)) out.records = data.records.map(wireInit)
    else throw new TypeError(`the ${out.recordType} record's data is not text, bytes or records`)
    return out
  }

  port.onmessage = (event) => {
    const message = JSON.parse(event.data)
    if (message.type === 'reply') {
      const waiting = pending.get(message.id)
      if (!waiting) return
      pending.delete(message.id)
      if (message.error) waiting.reject(new DOMException(message.error.message, message.error.name))
      else waiting.resolve(message.value)
      return
    }
    if (message.type === 'reading') {
      if (message.mifare) {
        window.ScadBuddyNfc.lastMifare = message.mifare
        window.dispatchEvent(new CustomEvent('scadbuddy-mifare', { detail: message.mifare }))
      }
      for (const reader of [...readers]) reader._reading(message)
      return
    }
    if (message.type === 'readingerror') {
      for (const reader of [...readers]) {
        const error = new Event('readingerror')
        reader.dispatchEvent(error)
        reader.onreadingerror?.(error)
      }
    }
  }

  class NDEFReader extends EventTarget {
    constructor() {
      super()
      this.onreading = null
      this.onreadingerror = null
      this._scanning = false
    }

    async scan(options = {}) {
      const { signal } = options
      if (signal?.aborted) throw signal.reason ?? new DOMException('The scan was aborted.', 'AbortError')
      if (this._scanning) throw new DOMException('A scan is already running.', 'InvalidStateError')
      await call('scan')
      this._scanning = true
      readers.add(this)
      signal?.addEventListener(
        'abort',
        () => {
          this._scanning = false
          readers.delete(this)
          if (readers.size === 0) call('stopScan').catch(() => {})
        },
        { once: true },
      )
    }

    _reading(message) {
      const event = new Event('reading')
      event.serialNumber = message.serialNumber
      event.message = {
        records: message.records.map((r) => ({
          recordType: r.recordType,
          mediaType: r.mediaType ?? undefined,
          id: r.id ?? undefined,
          encoding: r.encoding ?? undefined,
          lang: r.lang ?? undefined,
          data: r.dataHex == null ? null : viewOf(r.dataHex),
        })),
      }
      // Not Web NFC's: the tag's chip and capacity, which only the app can read (#8362).
      if (message.tag) event.scadbuddyTag = message.tag
      this.dispatchEvent(event)
      this.onreading?.(event)
    }

    async write(message, options = {}) {
      const { signal } = options
      if (signal?.aborted) throw signal.reason ?? new DOMException('The write was aborted.', 'AbortError')
      const records = (typeof message === 'string' ? [{ recordType: 'text', data: message }] : message.records).map(wireInit)
      const writeId = ++seq
      const done = call('write', { writeId, records, lang: document.documentElement.lang || 'en' })
      if (!signal) return done
      const aborted = new Promise((_, reject) =>
        signal.addEventListener(
          'abort',
          () => {
            call('cancelWrite', { writeId }).catch(() => {})
            reject(signal.reason ?? new DOMException('The write was aborted.', 'AbortError'))
          },
          { once: true },
        ),
      )
      return Promise.race([done, aborted])
    }

    async makeReadOnly() {
      throw new DOMException('ScadBuddy never makes a tag read-only.', 'NotSupportedError')
    }
  }

  window.NDEFReader = NDEFReader
  // `close()` sends the app to the back, so the page that opened it comes back.
  window.ScadBuddyNfc = { native: true, lastMifare: null, close: () => call('close') }
})()
