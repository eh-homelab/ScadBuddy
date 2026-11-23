import { existsSync } from 'node:fs'
import { mkdir, readdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { crc32, gunzipSync, inflateRawSync } from 'node:zlib'

// Unpacking a plugin package's archive (#6935, spec 2026-11-21 §6.6): a zip from an
// `archive` source, or a tar (gzipped or not) from an `npm` one
// (https://code.claude.com/docs/en/plugins/marketplace-reference.md, "archive plugin
// source" and "npm plugin source"). The archive is someone else's file, so it is read
// here entry by entry from a buffer, never handed to an unzip or tar binary, and
// nothing is written until the entry passes every rule:
//
//   - regular files and directories only: a symbolic link, a hard link, a device, a
//     FIFO or any other entry type is refused, never skipped, so the review shows the
//     whole archive;
//   - a relative path: no absolute path (`/x`, `C:\x`), no backslash, no NUL, no `..`
//     segment, and no two entries with one path;
//   - zip: stored or deflated only, not encrypted, no zip64, each entry's size and CRC-32
//     checked against what it inflates to;
//   - tar: ustar or GNU headers with valid checksums, a pax `path` or GNU long name
//     taken as the name, the name prefix (bytes 345..500) only in a POSIX ustar header
//     (magic `ustar\0`, version `00`: a GNU header keeps other fields there), sizes in
//     octal only;
//   - the package caps (spec §6.2): `UNPACK_MAX_FILES` entries (directories count, so an
//     archive cannot make one directory per header; a hosted draft holds one file fewer, for
//     the plugin.json that publish adds),
//     `UNPACK_MAX_FILE_BYTES` per file and `UNPACK_MAX_TOTAL_BYTES` in all, checked
//     before a byte is inflated, so a zip bomb never grows past them.
//
// A refusal is an `UnpackError`, whose message names the entry.

export const UNPACK_MAX_FILES = 500
export const UNPACK_MAX_FILE_BYTES = 2 * 1024 * 1024
export const UNPACK_MAX_TOTAL_BYTES = 20 * 1024 * 1024

export class UnpackError extends Error {
  override name = 'UnpackError'
}

type Entry = { path: string; dir: boolean; executable: boolean; data?: Buffer }

/** `raw` as a relative `/`-separated path ('' for the root), or an UnpackError. */
export function entryPath(raw: string): string {
  if (raw.includes('\0')) throw new UnpackError(`${JSON.stringify(raw)}: a path holds a NUL`)
  if (raw.includes('\\')) throw new UnpackError(`${raw}: a path holds a backslash`)
  if (raw.startsWith('/') || /^[A-Za-z]:/.test(raw)) throw new UnpackError(`${raw}: an absolute path`)
  const segments = raw.split('/').filter((s) => s !== '' && s !== '.')
  if (segments.includes('..')) throw new UnpackError(`${raw}: a path that leaves the archive ("..")`)
  return segments.join('/')
}

class Caps {
  private files = 0
  private total = 0
  dir(): void {
    if (++this.files > UNPACK_MAX_FILES) throw new UnpackError(`the archive has more than ${UNPACK_MAX_FILES} files`)
  }
  file(name: string, size: number): void {
    this.dir()
    if (size > UNPACK_MAX_FILE_BYTES) throw new UnpackError(`${name} is larger than ${UNPACK_MAX_FILE_BYTES} bytes`)
    this.total += size
    if (this.total > UNPACK_MAX_TOTAL_BYTES) throw new UnpackError(`the archive unpacks to more than ${UNPACK_MAX_TOTAL_BYTES} bytes`)
  }
}

// ---------------------------------------------------------------------------
// zip (PKWARE APPNOTE 6.3.10: https://pkwaredownloads.blob.core.windows.net/pem/APPNOTE.txt)

const EOCD = 0x06054b50
const CENTRAL = 0x02014b50
const LOCAL = 0x04034b50
const S_IFMT = 0o170000
const S_IFREG = 0o100000
const S_IFDIR = 0o040000
const S_IFLNK = 0o120000

function zipEntries(buf: Buffer): Entry[] {
  let eocd = -1
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 22 - 0xffff); i--) {
    if (buf.readUInt32LE(i) === EOCD) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new UnpackError('the zip has no end of central directory record')
  const disk = buf.readUInt16LE(eocd + 4)
  const count = buf.readUInt16LE(eocd + 10)
  const offset = buf.readUInt32LE(eocd + 16)
  if (disk !== 0 || buf.readUInt16LE(eocd + 6) !== 0) throw new UnpackError('a zip split across disks is refused')
  if (count === 0xffff || offset === 0xffffffff) throw new UnpackError('a zip64 archive is refused')
  const caps = new Caps()
  const entries: Entry[] = []
  let at = offset
  for (let n = 0; n < count; n++) {
    if (at + 46 > buf.length || buf.readUInt32LE(at) !== CENTRAL) throw new UnpackError('the zip central directory is damaged')
    const madeBy = buf.readUInt16LE(at + 4) >> 8
    const flags = buf.readUInt16LE(at + 8)
    const method = buf.readUInt16LE(at + 10)
    const crc = buf.readUInt32LE(at + 16)
    const csize = buf.readUInt32LE(at + 20)
    const usize = buf.readUInt32LE(at + 24)
    const nameLen = buf.readUInt16LE(at + 28)
    const extraLen = buf.readUInt16LE(at + 30)
    const commentLen = buf.readUInt16LE(at + 32)
    const external = buf.readUInt32LE(at + 38)
    const local = buf.readUInt32LE(at + 42)
    const rawName = buf.subarray(at + 46, at + 46 + nameLen).toString('utf8')
    at += 46 + nameLen + extraLen + commentLen
    const name = rawName || '(an entry with no name)'
    if (csize === 0xffffffff || usize === 0xffffffff || local === 0xffffffff) throw new UnpackError(`${name}: a zip64 entry is refused`)
    // The high 16 bits of the external attributes are the Unix mode when the entry was
    // made on Unix (APPNOTE 4.4.2, 4.4.15).
    const mode = madeBy === 3 ? external >>> 16 : 0
    const type = mode & S_IFMT
    if (type === S_IFLNK) throw new UnpackError(`${name}: a symbolic link; a package holds regular files only`)
    if (type !== 0 && type !== S_IFREG && type !== S_IFDIR) {
      throw new UnpackError(`${name}: a device or other special file; a package holds regular files only`)
    }
    const dir = type === S_IFDIR || (type === 0 && (rawName.endsWith('/') || (madeBy === 0 && (external & 0x10) !== 0)))
    const rel = entryPath(rawName)
    if (dir) {
      caps.dir()
      entries.push({ path: rel, dir: true, executable: false })
      continue
    }
    if (rel === '') throw new UnpackError(`${name}: a file with no name`)
    if (flags & 0x1) throw new UnpackError(`${name}: an encrypted entry is refused`)
    if (method !== 0 && method !== 8) throw new UnpackError(`${name}: compression method ${method} is refused (stored or deflate only)`)
    caps.file(rel, usize)
    if (local + 30 > buf.length || buf.readUInt32LE(local) !== LOCAL) throw new UnpackError(`${name}: its local header is damaged`)
    const start = local + 30 + buf.readUInt16LE(local + 26) + buf.readUInt16LE(local + 28)
    if (start + csize > buf.length) throw new UnpackError(`${name}: its data runs past the end of the zip`)
    const packed = buf.subarray(start, start + csize)
    let data: Buffer
    try {
      data = method === 0 ? Buffer.from(packed) : inflateRawSync(packed, { maxOutputLength: Math.max(usize, 1) })
    } catch {
      throw new UnpackError(`${name}: it does not inflate to the ${usize} bytes its header says`)
    }
    if (data.length !== usize) throw new UnpackError(`${name}: it unpacks to ${data.length} bytes, its header says ${usize}`)
    if (crc32(data) !== crc) throw new UnpackError(`${name}: its CRC-32 does not match`)
    entries.push({ path: rel, dir: false, executable: (mode & 0o111) !== 0, data })
  }
  return entries
}

// ---------------------------------------------------------------------------
// tar (POSIX ustar and pax, https://pubs.opengroup.org/onlinepubs/9799919799/utilities/pax.html;
// GNU long names, https://www.gnu.org/software/tar/manual/html_node/Standard.html)

function text(block: Buffer, start: number, length: number): string {
  const field = block.subarray(start, start + length)
  const end = field.indexOf(0)
  return field.subarray(0, end < 0 ? field.length : end).toString('utf8')
}

function octal(block: Buffer, start: number, length: number, what: string): number {
  if (block[start]! & 0x80) throw new UnpackError(`${what}: a base-256 size is refused`)
  const raw = text(block, start, length).trim()
  if (raw === '') return 0
  if (!/^[0-7]+$/.test(raw)) throw new UnpackError(`${what}: a malformed tar header`)
  return parseInt(raw, 8)
}

function paxPath(data: Buffer): string | undefined {
  let found: string | undefined
  let at = 0
  while (at < data.length) {
    const space = data.indexOf(0x20, at)
    if (space < 0) break
    const length = parseInt(data.subarray(at, space).toString('ascii'), 10)
    if (!Number.isInteger(length) || length <= 0 || at + length > data.length) throw new UnpackError('a malformed pax header')
    const record = data.subarray(space + 1, at + length - 1).toString('utf8')
    const eq = record.indexOf('=')
    if (eq > 0 && record.slice(0, eq) === 'path') found = record.slice(eq + 1)
    at += length
  }
  return found
}

function tarEntries(buf: Buffer): Entry[] {
  const caps = new Caps()
  const entries: Entry[] = []
  let at = 0
  let longName: string | undefined
  while (at + 512 <= buf.length) {
    const block = buf.subarray(at, at + 512)
    if (block.every((b) => b === 0)) break
    let sum = 0
    for (let i = 0; i < 512; i++) sum += i >= 148 && i < 156 ? 0x20 : block[i]!
    const stored = octal(block, 148, 8, 'a tar header')
    if (stored !== sum) throw new UnpackError('a tar header checksum does not match')
    const flag = String.fromCharCode(block[156]!)
    const posix = block.subarray(257, 263).toString('latin1') === 'ustar\0' && block.subarray(263, 265).toString('latin1') === '00'
    const prefix = posix ? text(block, 345, 155) : ''
    const headerName = prefix ? `${prefix}/${text(block, 0, 100)}` : text(block, 0, 100)
    const name = longName ?? headerName
    const size = octal(block, 124, 12, name)
    const mode = octal(block, 100, 8, name)
    const start = at + 512
    const end = start + size
    if (end > buf.length) throw new UnpackError(`${name}: its data runs past the end of the tar`)
    at = start + Math.ceil(size / 512) * 512
    if (flag === 'x' || flag === 'L') {
      // The name of the next entry, kept to the cap of a path.
      if (size > 64 * 1024) throw new UnpackError('a tar long name is larger than 64 KiB')
      const data = buf.subarray(start, end)
      const next = flag === 'x' ? paxPath(data) : text(data, 0, data.length)
      if (next !== undefined) longName = next
      continue
    }
    if (flag === 'g') continue
    longName = undefined
    if (flag === '2') throw new UnpackError(`${name}: a symbolic link; a package holds regular files only`)
    if (flag === '1') throw new UnpackError(`${name}: a hard link; a package holds regular files only`)
    if (flag === '3' || flag === '4' || flag === '6') throw new UnpackError(`${name}: a device or FIFO; a package holds regular files only`)
    const rel = entryPath(name)
    if (flag === '5') {
      caps.dir()
      entries.push({ path: rel, dir: true, executable: false })
      continue
    }
    if (flag !== '0' && flag !== '\0' && flag !== '7') throw new UnpackError(`${name}: tar entry type ${JSON.stringify(flag)} is refused`)
    if (rel === '') throw new UnpackError(`${name}: a file with no name`)
    caps.file(rel, size)
    entries.push({ path: rel, dir: false, executable: (mode & 0o111) !== 0, data: Buffer.from(buf.subarray(start, end)) })
  }
  return entries
}

/** The format of `buf` by its first bytes. */
export function archiveFormat(buf: Buffer): 'zip' | 'gzip' | 'tar' | undefined {
  if (buf.length >= 4 && (buf.readUInt32LE(0) === LOCAL || buf.readUInt32LE(0) === EOCD)) return 'zip'
  if (buf.length >= 2 && buf[0] === 0x1f && buf[1] === 0x8b) return 'gzip'
  if (buf.length >= 263 && buf.subarray(257, 262).toString('ascii') === 'ustar') return 'tar'
  return undefined
}

/** The entries of `buf`, a zip or a (gzipped) tar, every rule above checked. */
export function readArchive(buf: Buffer): Entry[] {
  const format = archiveFormat(buf)
  if (format === 'zip') return zipEntries(buf)
  if (format === 'tar') return tarEntries(buf)
  if (format === 'gzip') {
    let tar: Buffer
    try {
      // The caps, with room for each file's header and padding.
      tar = gunzipSync(buf, { maxOutputLength: UNPACK_MAX_TOTAL_BYTES + UNPACK_MAX_FILES * 1024 + 1024 * 1024 })
    } catch {
      throw new UnpackError(`the gzip does not unpack, or unpacks to more than ${UNPACK_MAX_TOTAL_BYTES} bytes`)
    }
    return tarEntries(tar)
  }
  throw new UnpackError('the file is not a zip or tar archive')
}

/**
 * Unpacks `buf` into `dest` (which must not exist): every directory and file of the
 * archive, a file executable when its mode says so. Throws UnpackError on any rule above.
 */
export async function unpackArchive(buf: Buffer, dest: string): Promise<void> {
  const entries = readArchive(buf)
  const seen = new Set<string>()
  for (const entry of entries) {
    if (entry.path === '') continue
    if (seen.has(entry.path)) throw new UnpackError(`${entry.path}: the archive holds it twice`)
    seen.add(entry.path)
  }
  await mkdir(dest, { recursive: false })
  for (const entry of entries) {
    const target = path.join(dest, entry.path)
    try {
      if (entry.dir) {
        await mkdir(target, { recursive: true })
      } else {
        await mkdir(path.dirname(target), { recursive: true })
        await writeFile(target, entry.data!, { flag: 'wx', mode: entry.executable ? 0o755 : 0o644 })
      }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code
      if (code === 'EEXIST' || code === 'ENOTDIR') throw new UnpackError(`${entry.path}: a file and a directory share its path`)
      throw err
    }
  }
}

/** What a plugin's root holds (https://code.claude.com/docs/en/plugins-reference, "Plugin directory structure"). */
const ROOT_MARKS = ['.claude-plugin', 'skills', 'commands', 'agents', 'hooks', '.mcp.json', 'SKILL.md']

/**
 * The plugin's directory inside an unpacked archive: the top, or the one directory
 * there when the top holds nothing else ("The plugin root may be at the top of the zip
 * or one directory down", marketplace-reference "archive plugin source"; an npm tarball
 * holds its files under `package/`).
 */
export async function pluginRoot(dir: string): Promise<string> {
  if (ROOT_MARKS.some((m) => existsSync(path.join(dir, m)))) return ''
  const entries = await readdir(dir, { withFileTypes: true })
  return entries.length === 1 && entries[0]!.isDirectory() ? entries[0]!.name : ''
}
