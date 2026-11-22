import { mkdtemp, readFile, rm, stat } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  entryPath,
  pluginRoot,
  UNPACK_MAX_FILE_BYTES,
  UNPACK_MAX_FILES,
  unpackArchive,
  UnpackError,
} from '../src/plugins/packages/unpack.js'
import { paxPath, tar, zip } from './support/archives.js'

// #6935, spec 2026-11-21 §6.6: zip and tar only; symlinks, hard links, absolute paths,
// `..` and device files are refused, within the draft caps of §6.2.

let dir: string
beforeEach(async () => {
  dir = await mkdtemp(path.join(os.tmpdir(), 'unpack-'))
})
afterEach(async () => {
  await rm(dir, { recursive: true, force: true })
})

const SKILL = '---\nname: hello\ndescription: Says hello\n---\nHello.\n'

let refusals = 0
async function refusal(buf: Buffer): Promise<string> {
  const dest = path.join(dir, `refused-${++refusals}`)
  const err = await unpackArchive(buf, dest).then(
    () => undefined,
    (e: unknown) => e,
  )
  expect(err).toBeInstanceOf(UnpackError)
  // Nothing of a refused archive is written.
  expect(existsSync(dest)).toBe(false)
  return (err as Error).message
}

describe('a zip', () => {
  it('unpacks stored and deflated files, directories and the executable bit', async () => {
    const dest = path.join(dir, 'out')
    await unpackArchive(
      zip([
        { name: 'greeter/' },
        { name: 'greeter/skills/hello/SKILL.md', data: SKILL, deflate: true },
        { name: 'greeter/bin/run.sh', data: '#!/bin/sh\n', mode: 0o100755 },
      ]),
      dest,
    )
    expect(await readFile(path.join(dest, 'greeter/skills/hello/SKILL.md'), 'utf8')).toBe(SKILL)
    expect((await stat(path.join(dest, 'greeter/bin/run.sh'))).mode & 0o111).not.toBe(0)
    expect(await pluginRoot(dest)).toBe('greeter')
  })

  it('refuses a symbolic link', async () => {
    expect(await refusal(zip([{ name: 'skills/x', data: '/etc/passwd', mode: 0o120777 }]))).toMatch(/symbolic link/)
  })

  it('refuses a device file', async () => {
    expect(await refusal(zip([{ name: 'dev', mode: 0o020644 }]))).toMatch(/device/)
  })

  it('refuses a path with .. (zip slip)', async () => {
    expect(await refusal(zip([{ name: '../../evil.sh', data: 'x' }]))).toMatch(/leaves the archive/)
    expect(await refusal(zip([{ name: 'skills/../../evil.sh', data: 'x' }]))).toMatch(/leaves the archive/)
  })

  it('refuses an absolute path', async () => {
    expect(await refusal(zip([{ name: '/etc/cron.d/x', data: 'x' }]))).toMatch(/absolute/)
    expect(await refusal(zip([{ name: 'C:/x', data: 'x' }]))).toMatch(/absolute/)
    expect(await refusal(zip([{ name: 'a\\..\\b', data: 'x' }]))).toMatch(/backslash/)
  })

  it('refuses an encrypted entry, a size that lies and a CRC that does not match', async () => {
    expect(await refusal(zip([{ name: 'a', data: 'x', encrypted: true }]))).toMatch(/encrypted/)
    expect(await refusal(zip([{ name: 'a', data: 'xyz', deflate: true, claimedSize: 1 }]))).toMatch(/inflate|unpacks/)
    const bad = zip([{ name: 'a', data: 'xyz' }])
    bad[bad.indexOf('xyz')] = 0x41
    expect(await refusal(bad)).toMatch(/CRC/)
  })

  it('refuses the same path twice, and a file where a directory is', async () => {
    expect(await refusal(zip([{ name: 'a', data: '1' }, { name: 'a', data: '2' }]))).toMatch(/twice/)
  })

  it('refuses past the caps before inflating', async () => {
    const big = Buffer.alloc(UNPACK_MAX_FILE_BYTES + 1)
    expect(await refusal(zip([{ name: 'big', data: big, deflate: true }]))).toMatch(/larger than/)
    // A bomb: a small deflated entry whose header claims more than the cap.
    expect(await refusal(zip([{ name: 'bomb', data: 'x', deflate: true, claimedSize: 2 ** 31 }]))).toMatch(/larger than/)
    const many = Array.from({ length: UNPACK_MAX_FILES + 1 }, (_, i) => ({ name: `f${i}`, data: 'x' }))
    expect(await refusal(zip(many))).toMatch(/more than/)
    // Directories count too: an archive of empty directories cannot make one per entry.
    const dirs = Array.from({ length: UNPACK_MAX_FILES + 1 }, (_, i) => ({ name: `d${i}/` }))
    expect(await refusal(zip(dirs))).toMatch(/more than/)
  })
})

describe('a tar', () => {
  it('unpacks a gzipped npm tarball under package/', async () => {
    const dest = path.join(dir, 'out')
    await unpackArchive(
      tar(
        [
          { name: 'package/package.json', data: '{"name":"greeter"}' },
          { name: 'package/skills/hello/SKILL.md', data: SKILL },
          { name: 'package/bin/run', data: 'x', mode: 0o755 },
        ],
        { gzip: true },
      ),
      dest,
    )
    expect(await readFile(path.join(dest, 'package/skills/hello/SKILL.md'), 'utf8')).toBe(SKILL)
    expect((await stat(path.join(dest, 'package/bin/run'))).mode & 0o111).not.toBe(0)
    expect(await pluginRoot(dest)).toBe('package')
  })

  it('takes a pax path as the name, and checks it as any name', async () => {
    const dest = path.join(dir, 'out')
    await unpackArchive(tar([{ name: 'PaxHeader', type: 'x', data: paxPath('long/name.md') }, { name: 'short', data: 'x' }]), dest)
    expect(await readFile(path.join(dest, 'long/name.md'), 'utf8')).toBe('x')
    expect(await refusal(tar([{ name: 'PaxHeader', type: 'x', data: paxPath('../out.md') }, { name: 'short', data: 'x' }]))).toMatch(
      /leaves the archive/,
    )
  })

  it('takes a GNU long name, and reads no prefix from a GNU header', async () => {
    const long = `skills/${'n'.repeat(120)}/SKILL.md`
    const dest = path.join(dir, 'out')
    // GNU tar's magic is `ustar  \0`, and bytes 345..500 of its header are not a prefix
    // (they hold atime, ctime and sparse data).
    const gnu = { magic: 'ustar  \0', field345: '12345670123\0' }
    await unpackArchive(tar([{ name: '././@LongLink', type: 'L', data: `${long}\0`, ...gnu }, { name: long.slice(0, 100), data: SKILL, ...gnu }]), dest)
    expect(await readFile(path.join(dest, long), 'utf8')).toBe(SKILL)
    const other = path.join(dir, 'other')
    await unpackArchive(tar([{ name: 'plain.md', data: 'x', ...gnu }]), other)
    expect(await readFile(path.join(other, 'plain.md'), 'utf8')).toBe('x')
    // A ustar prefix is still the start of the name.
    const ustar = path.join(dir, 'ustar')
    await unpackArchive(tar([{ name: 'name.md', data: 'y', field345: 'package/deep' }]), ustar)
    expect(await readFile(path.join(ustar, 'package/deep/name.md'), 'utf8')).toBe('y')
  })

  it('counts directories toward the file cap', async () => {
    const dirs = Array.from({ length: UNPACK_MAX_FILES + 1 }, (_, i) => ({ name: `d${i}/`, type: '5' }))
    expect(await refusal(tar(dirs))).toMatch(/more than/)
  })

  it('refuses symbolic links, hard links and devices', async () => {
    expect(await refusal(tar([{ name: 'link', type: '2', linkname: '/etc/passwd' }]))).toMatch(/symbolic link/)
    expect(await refusal(tar([{ name: 'link', type: '1', linkname: 'other' }]))).toMatch(/hard link/)
    expect(await refusal(tar([{ name: 'dev', type: '3' }]))).toMatch(/device/)
    expect(await refusal(tar([{ name: 'fifo', type: '6' }]))).toMatch(/device or FIFO/)
  })

  it('refuses .. and absolute paths', async () => {
    expect(await refusal(tar([{ name: 'package/../../x', data: 'x' }]))).toMatch(/leaves the archive/)
    expect(await refusal(tar([{ name: '/tmp/x', data: 'x' }]))).toMatch(/absolute/)
  })

  it('refuses a header whose checksum does not match, and a file that is no archive', async () => {
    const bad = tar([{ name: 'a', data: 'x' }])
    bad[0] = 0x62
    expect(await refusal(bad)).toMatch(/checksum/)
    expect(await refusal(Buffer.from('not an archive'))).toMatch(/not a zip or tar/)
  })
})

describe('entryPath', () => {
  it('normalises ./ and empty segments, and keeps dot names', () => {
    expect(entryPath('./a//b/')).toBe('a/b')
    expect(entryPath('.claude-plugin/plugin.json')).toBe('.claude-plugin/plugin.json')
    expect(() => entryPath('a/..')).toThrow(UnpackError)
  })
})

describe('pluginRoot', () => {
  it('is the top when it holds a plugin', async () => {
    const dest = path.join(dir, 'out')
    await unpackArchive(zip([{ name: '.claude-plugin/plugin.json', data: '{"name":"greeter"}' }, { name: 'README.md', data: 'x' }]), dest)
    expect(await pluginRoot(dest)).toBe('')
  })
})
