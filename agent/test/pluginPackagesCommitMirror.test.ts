import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PackageInstaller, recordPreparedMirror } from '../src/plugins/packages/install.js'
import type { PackageMirror } from '../src/plugins/packages/mirror.js'
import { validateSource } from '../src/plugins/packages/source.js'
import type { MirrorRecord, PackagePin, PreparedPackage } from '../src/plugins/packages/store.js'
import { storedZip } from '../src/plugins/packages/zip.js'
import { PluginError } from '../src/plugins/registry.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'

// A commit pin's mirror (#7907): its files as a zip in the API's area `package-mirrors`,
// recorded on the row, so a cold cache does not fetch the repository again. Local git
// repositories stand in for upstream (test/support/gitRepo.ts), and the API's mirror and
// its record are in memory.

const sha256 = (b: Buffer) => createHash('sha256').update(b).digest('hex')

/** The API's mirror, in memory: a file is read back only by the package it was sent for. */
function memoryMirror(): PackageMirror & { kept: Map<string, Buffer>; failPut: boolean } {
  const kept = new Map<string, Buffer>()
  const names = new Map<string, string>()
  const mirror = {
    kept,
    failPut: false,
    put: async (name: string, sha: string, bytes: Buffer) => {
      if (mirror.failPut) throw new PluginError('the API did not answer', 502)
      expect(sha256(bytes)).toBe(sha)
      kept.set(sha, Buffer.from(bytes))
      names.set(sha, name)
    },
    get: async (name: string, sha: string) => {
      const bytes = kept.get(sha)
      if (!bytes || names.get(sha) !== name) throw new PluginError(`no mirrored copy of the pinned file ${sha} is kept`, 502)
      return bytes
    },
  }
  return mirror
}

const URL = 'https://git.test/greeter.git'

let repo: TestRepo
let fetcher: ReturnType<typeof localFetcher>
let cacheRoot: string
let mirror: ReturnType<typeof memoryMirror>
let records: { name: string; mirror: MirrorRecord }[]
let warnings: string[]
let installer: PackageInstaller

beforeEach(() => {
  if (gitMissing) return
  repo = gitRepo({ ...GREETER, 'bin/run.sh': '#!/bin/sh\necho hi\n' }, { executable: ['bin/run.sh'] })
  fetcher = localFetcher({ greeter: repo })
  cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-commit-mirror-'))
  mirror = memoryMirror()
  records = []
  warnings = []
  installer = new PackageInstaller({
    fetcher,
    cacheRoot,
    resolve: resolver(),
    mirror: () => mirror,
    recordMirror: async (name, m) => {
      records.push({ name, mirror: m })
    },
    warn: (message) => warnings.push(message),
  })
})

afterEach(() => {
  if (gitMissing) return
  repo.remove()
  rmSync(cacheRoot, { recursive: true, force: true })
})

const pinOf = (p: PreparedPackage, mirrorSha256: string | null | undefined = p.mirrorSha256): PackagePin => ({
  name: p.review.name,
  fetchUrl: p.fetchUrl,
  fetchPath: p.fetchPath,
  commit: p.commit,
  ...(mirrorSha256 ? { mirrorSha256 } : {}),
  contentHash: p.contentHash,
})

/** A fresh replica: the same mirror and record, an empty cache. */
function coldInstaller(): PackageInstaller {
  rmSync(cacheRoot, { recursive: true, force: true })
  fetcher.checkouts.length = 0
  return installer
}

describe.skipIf(gitMissing)('commit pin mirrors', () => {
  it('sends the files of an install to the mirror, and the caller records them once stored', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    expect(prepared.mirrorSha256).toMatch(/^[0-9a-f]{64}$/)
    expect([...mirror.kept.keys()]).toEqual([prepared.mirrorSha256])
    // Not recorded before the pin is stored.
    expect(records).toEqual([])

    const recordMirror = vi.fn(async () => undefined)
    await recordPreparedMirror({ recordMirror }, prepared)
    expect(recordMirror).toHaveBeenCalledWith('greeter', {
      commit: repo.commit,
      contentHash: prepared.contentHash,
      sha256: prepared.mirrorSha256,
    })
    // A record that fails is told, never thrown: the next load from git records it again.
    const told: string[] = []
    await recordPreparedMirror({ recordMirror: () => Promise.reject(new Error('409 pin-mismatch')) }, prepared, (m) => told.push(m))
    expect(told).toEqual([expect.stringContaining('409 pin-mismatch')])
  })

  it('gives the same zip for the same files, on every fetch', async () => {
    const a = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    const b = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    expect(b.mirrorSha256).toBe(a.mirrorSha256)
  })

  it('loads a cold cache from the mirror and never fetches git', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    const cold = coldInstaller()
    const dir = await cold.materialise(pinOf(prepared))
    expect(fetcher.checkouts).toEqual([])
    expect(statSync(path.join(dir, 'bin/run.sh')).mode & 0o111).not.toBe(0)
    await cold.mirrorsSent()
    expect(records).toEqual([])
    expect(warnings).toEqual([])
  })

  it('fetches git when the mirror is gone, then mirrors and records the files again', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    mirror.kept.clear()
    const cold = coldInstaller()
    await cold.materialise(pinOf(prepared))
    expect(fetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
    expect(warnings).toEqual([expect.stringContaining('was not used, so it is fetched again')])
    await cold.mirrorsSent()
    expect([...mirror.kept.keys()]).toEqual([prepared.mirrorSha256])
    expect(records).toEqual([
      { name: 'greeter', mirror: { commit: repo.commit, contentHash: prepared.contentHash, sha256: prepared.mirrorSha256 } },
    ])
  })

  it('never uses a mirror whose files do not hash to the pin, and fetches git instead', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    // Another package's files kept under the pin's name: a sound zip that hashes to something else.
    const other = storedZip([{ path: '.claude-plugin/plugin.json', data: Buffer.from('{"name":"greeter"}'), executable: false }])
    await mirror.put('greeter', sha256(other), other)
    const cold = coldInstaller()
    const dir = await cold.materialise(pinOf(prepared, sha256(other)))
    expect(fetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
    expect(warnings).toEqual([expect.stringContaining(`hash to`)])
    expect(statSync(path.join(dir, 'README.md')).isFile()).toBe(true)
    // And bytes that are no zip at all.
    const junk = Buffer.from('not a zip')
    await mirror.put('greeter', sha256(junk), junk)
    warnings.length = 0
    await coldInstaller().materialise(pinOf(prepared, sha256(junk)))
    expect(fetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
    expect(warnings).toEqual([expect.stringContaining('was not used')])
  })

  it('mirrors a pin made before #7907 on its next load from git', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    mirror.kept.clear()
    const cold = coldInstaller()
    await cold.materialise(pinOf(prepared, null))
    expect(fetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
    expect(warnings).toEqual([])
    await cold.mirrorsSent()
    expect(records.map((r) => r.mirror.sha256)).toEqual([prepared.mirrorSha256])
    // The next cold replica reads it from there.
    const again = coldInstaller()
    await again.materialise(pinOf(prepared))
    expect(fetcher.checkouts).toEqual([])
  })

  it('loads from git when the mirror cannot take the files, and only says so', async () => {
    mirror.failPut = true
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    expect(prepared.mirrorSha256).toBeUndefined()
    const cold = coldInstaller()
    await cold.materialise(pinOf(prepared))
    await cold.mirrorsSent()
    expect(records).toEqual([])
    expect(warnings).toEqual([
      expect.stringContaining('were not mirrored: the API did not answer'),
      expect.stringContaining('were not mirrored: the API did not answer'),
    ])
  })

  it('mirrors nothing without a record to keep it', async () => {
    const plain = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver(), mirror: () => mirror })
    const prepared = await plain.prepare(validateSource({ kind: 'git', url: URL }))
    expect(prepared.mirrorSha256).toBeUndefined()
    expect(mirror.kept.size).toBe(0)
  })
})
