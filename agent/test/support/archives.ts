import { crc32, deflateRawSync, gzipSync } from 'node:zlib'

// Archives built byte by byte for the unpacking tests (#6935): a zip or tar can hold
// what no well-behaved tool writes (a `..` path, a symlink, a device), and the tests
// need exactly that.

export type ZipEntry = {
  name: string
  data?: string | Buffer
  /** The Unix mode, file type bits included; 0o100644 by default (0o040755 for a name ending in `/`). */
  mode?: number
  deflate?: boolean
  encrypted?: boolean
  /** Overrides the uncompressed size written in the headers. */
  claimedSize?: number
}

export function zip(entries: readonly ZipEntry[]): Buffer {
  const locals: Buffer[] = []
  const centrals: Buffer[] = []
  let offset = 0
  for (const e of entries) {
    const name = Buffer.from(e.name, 'utf8')
    const data = Buffer.isBuffer(e.data) ? e.data : Buffer.from(e.data ?? '', 'utf8')
    const packed = e.deflate ? deflateRawSync(data) : data
    const size = e.claimedSize ?? data.length
    const mode = e.mode ?? (e.name.endsWith('/') ? 0o040755 : 0o100644)
    const flags = e.encrypted ? 1 : 0
    const method = e.deflate ? 8 : 0
    const local = Buffer.alloc(30)
    local.writeUInt32LE(0x04034b50, 0)
    local.writeUInt16LE(20, 4)
    local.writeUInt16LE(flags, 6)
    local.writeUInt16LE(method, 8)
    local.writeUInt32LE(crc32(data), 14)
    local.writeUInt32LE(packed.length, 18)
    local.writeUInt32LE(size, 22)
    local.writeUInt16LE(name.length, 26)
    locals.push(local, name, packed)
    const central = Buffer.alloc(46)
    central.writeUInt32LE(0x02014b50, 0)
    central.writeUInt16LE((3 << 8) | 20, 4)
    central.writeUInt16LE(20, 6)
    central.writeUInt16LE(flags, 8)
    central.writeUInt16LE(method, 10)
    central.writeUInt32LE(crc32(data), 16)
    central.writeUInt32LE(packed.length, 20)
    central.writeUInt32LE(size, 24)
    central.writeUInt16LE(name.length, 28)
    central.writeUInt32LE((mode << 16) >>> 0, 38)
    central.writeUInt32LE(offset, 42)
    centrals.push(central, name)
    offset += 30 + name.length + packed.length
  }
  const cd = Buffer.concat(centrals)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50, 0)
  end.writeUInt16LE(entries.length, 8)
  end.writeUInt16LE(entries.length, 10)
  end.writeUInt32LE(cd.length, 12)
  end.writeUInt32LE(offset, 16)
  return Buffer.concat([...locals, cd, end])
}

export type TarEntry = {
  name: string
  data?: string | Buffer
  /** The typeflag: '0' a file (the default), '5' a directory, '1' a hard link, '2' a symlink, '3' a device, 'x' pax. */
  type?: string
  mode?: number
  linkname?: string
}

function header(e: TarEntry, size: number): Buffer {
  const block = Buffer.alloc(512)
  block.write(e.name, 0, 100, 'utf8')
  block.write(`${(e.mode ?? 0o644).toString(8).padStart(7, '0')}\0`, 100, 'ascii')
  block.write('0000000\0', 108, 'ascii')
  block.write('0000000\0', 116, 'ascii')
  block.write(`${size.toString(8).padStart(11, '0')}\0`, 124, 'ascii')
  block.write('00000000000\0', 136, 'ascii')
  block.write('        ', 148, 'ascii')
  block.write(e.type ?? '0', 156, 'ascii')
  if (e.linkname) block.write(e.linkname, 157, 100, 'utf8')
  block.write('ustar\0', 257, 'ascii')
  block.write('00', 263, 'ascii')
  let sum = 0
  for (const b of block) sum += b
  block.write(`${sum.toString(8).padStart(6, '0')}\0 `, 148, 'ascii')
  return block
}

export function tar(entries: readonly TarEntry[], options: { gzip?: boolean } = {}): Buffer {
  const parts: Buffer[] = []
  for (const e of entries) {
    const data = Buffer.isBuffer(e.data) ? e.data : Buffer.from(e.data ?? '', 'utf8')
    parts.push(header(e, data.length), data, Buffer.alloc((512 - (data.length % 512)) % 512))
  }
  parts.push(Buffer.alloc(1024))
  const out = Buffer.concat(parts)
  return options.gzip ? gzipSync(out) : out
}

/** A pax record `<len> path=<value>\n`. */
export function paxPath(value: string): string {
  const body = ` path=${value}\n`
  let length = body.length + 1
  while (`${length}${body}`.length !== length) length++
  return `${length}${body}`
}
