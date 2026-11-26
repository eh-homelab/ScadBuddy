import { crc32 } from 'node:zlib'

// A zip written byte for byte (#6936): the plugin feed serves a bytes pin whose file is not a
// zip (an npm tarball) as one, because Claude Code's `archive` source takes a zip only
// ([marketplace reference, archive plugin source](https://code.claude.com/docs/en/plugins/marketplace-reference.md#archive-plugin-source)).
// The same files give the same bytes, on every replica and Node version: entries in path
// order, stored (no compression, so no zlib version changes them), a fixed time
// (1980-01-01 00:00, the format's first), mode 0644 or 0755, UTF-8 names, no extra fields.
// No zip64: the caps (unpack.ts, 500 files and 20 MiB; a commit pin's mirror, hash.ts, 2,000
// files and 20 MiB, #7907) keep every field in range.

export type ZipFile = { path: string; data: Buffer; executable: boolean }

const DOS_TIME = 0
const DOS_DATE = (0 << 9) | (1 << 5) | 1
const UTF8 = 0x0800
const STORED = 0
const VERSION = 20
const MADE_BY_UNIX = (3 << 8) | VERSION

export function storedZip(files: readonly ZipFile[]): Buffer {
  const sorted = [...files].sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  const locals: Buffer[] = []
  const centrals: Buffer[] = []
  let offset = 0
  for (const file of sorted) {
    const name = Buffer.from(file.path, 'utf8')
    const crc = crc32(file.data) >>> 0
    const size = file.data.length

    const local = Buffer.alloc(30)
    local.writeUInt32LE(0x04034b50, 0)
    local.writeUInt16LE(VERSION, 4)
    local.writeUInt16LE(UTF8, 6)
    local.writeUInt16LE(STORED, 8)
    local.writeUInt16LE(DOS_TIME, 10)
    local.writeUInt16LE(DOS_DATE, 12)
    local.writeUInt32LE(crc, 14)
    local.writeUInt32LE(size, 18)
    local.writeUInt32LE(size, 22)
    local.writeUInt16LE(name.length, 26)
    local.writeUInt16LE(0, 28)
    locals.push(local, name, file.data)

    const central = Buffer.alloc(46)
    central.writeUInt32LE(0x02014b50, 0)
    central.writeUInt16LE(MADE_BY_UNIX, 4)
    central.writeUInt16LE(VERSION, 6)
    central.writeUInt16LE(UTF8, 8)
    central.writeUInt16LE(STORED, 10)
    central.writeUInt16LE(DOS_TIME, 12)
    central.writeUInt16LE(DOS_DATE, 14)
    central.writeUInt32LE(crc, 16)
    central.writeUInt32LE(size, 20)
    central.writeUInt32LE(size, 24)
    central.writeUInt16LE(name.length, 28)
    // extra, comment, disk, internal attributes: 0
    central.writeUInt32LE(((0o100000 | (file.executable ? 0o755 : 0o644)) << 16) >>> 0, 38)
    central.writeUInt32LE(offset, 42)
    centrals.push(central, name)

    offset += local.length + name.length + size
  }
  const directory = Buffer.concat(centrals)
  const end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50, 0)
  end.writeUInt16LE(sorted.length, 8)
  end.writeUInt16LE(sorted.length, 10)
  end.writeUInt32LE(directory.length, 12)
  end.writeUInt32LE(offset, 16)
  return Buffer.concat([...locals, directory, end])
}
