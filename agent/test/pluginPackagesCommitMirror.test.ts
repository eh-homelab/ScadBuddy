import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { COMMIT_MIRROR_LIMITS, loadPackagesForRun, MirrorNotReadyError, MirrorRefusedError, PackageInstaller, recordPreparedMirror } from '../src/plugins/packages/install.js'
import { MIRROR_MAX_BYTES } from '../src/plugins/packages/mirror.js'
import { readArchive, UnpackError } from '../src/plugins/packages/unpack.js'
import { localMirrorJobs, mirrorWorkflowId, runMirrorJob } from '../src/plugins/packages/mirrorJob.js'
import { validateSource } from '../src/plugins/packages/source.js'
import type { MirrorRecord, PackagePin, PreparedPackage } from '../src/plugins/packages/store.js'
import { storedZip } from '../src/plugins/packages/zip.js'
import { memoryMirror } from './support/memoryMirror.js'
import { gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'
import { MAX_FILES, MAX_TOTAL_BYTES } from '../src/plugins/packages/hash.js'

// A commit pin's mirror (#7907): its files as a zip in the API's area `package-mirrors`,
// recorded on the row. A load (a turn, the warm-up) never fetches git (the owner's decision
// of 2026-11-26): with no usable mirror the package is left out and the mirror job starts,
// which re-checks a recorded mirror and fetches only when it is missing or bad. Local git
// repositories stand in for upstream (test/support/gitRepo.ts); the API's mirror, its record
// and the row are in memory, and the job runs in this process (`localMirrorJobs`).

const sha256 = (b: Buffer) => createHash('sha256').update(b).digest('hex')

const URL = 'https://git.test/greeter.git'

let repo: TestRepo
let fetcher: ReturnType<typeof localFetcher>
let jobFetcher: ReturnType<typeof localFetcher>
let cacheRoot: string
let mirror: ReturnType<typeof memoryMirror>
let records: { name: string; mirror: MirrorRecord }[]
let warnings: string[]
/** The row's pin as the job reads it: set by a test, its mirror moved by a record. */
let row: PackagePin | undefined
let jobs: ReturnType<typeof localMirrorJobs>
let started: string[]
let installer: PackageInstaller

beforeEach(() => {
  if (gitMissing) return
  repo = gitRepo({ ...GREETER, 'bin/run.sh': '#!/bin/sh\necho hi\n' }, { executable: ['bin/run.sh'] })
  fetcher = localFetcher({ greeter: repo })
  jobFetcher = localFetcher({ greeter: repo })
  cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-commit-mirror-'))
  mirror = memoryMirror()
  records = []
  warnings = []
  row = undefined
  started = []
  installer = new PackageInstaller({
    fetcher,
    mirrorFetcher: jobFetcher,
    cacheRoot,
    resolve: resolver(),
    mirror: () => mirror,
    recordMirror: async (name, m) => {
      records.push({ name, mirror: m })
      if (row && row.commit === m.commit && row.contentHash === m.contentHash) row = { ...row, mirrorSha256: m.sha256 }
    },
    warn: (message) => warnings.push(message),
    mirrorJobs: () => ({
      start(job) {
        started.push(mirrorWorkflowId(job))
        jobs.start(job)
      },
    }),
  })
  jobs = localMirrorJobs(
    { packages: { filesOf: async (_name, pending) => (row && !pending ? { pin: row, files: {} } : undefined) }, installer },
    (message) => warnings.push(message),
  )
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

/** A fresh replica: the same mirror, record and row, an empty cache, no fetch yet. */
function cold(): PackageInstaller {
  rmSync(cacheRoot, { recursive: true, force: true })
  fetcher.checkouts.length = 0
  jobFetcher.checkouts.length = 0
  started.length = 0
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
    const wantMirror = vi.fn(() => true)
    await recordPreparedMirror({ recordMirror }, { wantMirror }, prepared)
    expect(recordMirror).toHaveBeenCalledWith('greeter', {
      commit: repo.commit,
      contentHash: prepared.contentHash,
      sha256: prepared.mirrorSha256,
    })
    expect(wantMirror).not.toHaveBeenCalled()
    // A re-pin stored under the request's name is recorded under that name, not the review's.
    await recordPreparedMirror({ recordMirror }, { wantMirror }, prepared, { name: 'renamed' })
    expect(recordMirror).toHaveBeenLastCalledWith('renamed', expect.objectContaining({ sha256: prepared.mirrorSha256 }))
    // A record that fails is told, never thrown, and the mirror job is started instead.
    const told: string[] = []
    await recordPreparedMirror({ recordMirror: () => Promise.reject(new Error('409 pin-mismatch')) }, { wantMirror }, prepared, { warn: (m) => told.push(m) })
    expect(told).toEqual([expect.stringContaining('409 pin-mismatch')])
    expect(wantMirror).toHaveBeenCalledWith({ name: 'greeter', commit: repo.commit, contentHash: prepared.contentHash })
  })

  it('starts the mirror job for an install whose files were not mirrored', async () => {
    mirror.failPut = true
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    expect(prepared.mirrorSha256).toBeUndefined()
    expect(warnings).toEqual([expect.stringContaining('were not mirrored: the API did not answer')])
    const wantMirror = vi.fn(() => true)
    const recordMirror = vi.fn(async () => undefined)
    await recordPreparedMirror({ recordMirror }, { wantMirror }, prepared)
    expect(recordMirror).not.toHaveBeenCalled()
    expect(wantMirror).toHaveBeenCalledWith({ name: 'greeter', commit: repo.commit, contentHash: prepared.contentHash })
  })

  it('gives the same zip for the same files, on every fetch', async () => {
    const a = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    const b = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    expect(b.mirrorSha256).toBe(a.mirrorSha256)
  })

  it('loads a cold cache from the mirror and never fetches git', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    const replica = cold()
    const dir = await replica.materialise(pinOf(prepared))
    expect(fetcher.checkouts).toEqual([])
    expect(jobFetcher.checkouts).toEqual([])
    expect(started).toEqual([])
    expect(statSync(path.join(dir, 'bin/run.sh')).mode & 0o111).not.toBe(0)
    expect(warnings).toEqual([])
  })

  it('leaves a pin with no mirror out, starts its job, and never fetches git on the load', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    mirror.kept.clear()
    row = pinOf(prepared, null)
    const replica = cold()
    const load = replica.materialise(row)
    await expect(load).rejects.toBeInstanceOf(MirrorNotReadyError)
    await expect(load).rejects.toThrow(/its mirror is not ready yet; a new one is being made in the background/)
    // The load itself fetched nothing; the job did, once.
    expect(fetcher.checkouts).toEqual([])
    expect(started).toEqual([mirrorWorkflowId({ name: 'greeter', commit: repo.commit, contentHash: prepared.contentHash })])
    await jobs.settled()
    expect(jobFetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
    expect(records.map((r) => r.mirror.sha256)).toEqual([prepared.mirrorSha256])
    // The next load reads the mirror the job made.
    const again = cold()
    await again.materialise(row)
    expect(fetcher.checkouts).toEqual([])
    expect(jobFetcher.checkouts).toEqual([])
  })

  it('says only that the mirror is not ready when this process has no mirror job', async () => {
    const plain = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver(), mirror: () => mirror, recordMirror: async () => undefined })
    const prepared = await plain.prepare(validateSource({ kind: 'git', url: URL }))
    rmSync(cacheRoot, { recursive: true, force: true })
    fetcher.checkouts.length = 0
    await expect(plain.materialise(pinOf(prepared, null))).rejects.toThrow(/its mirror is not ready yet$/)
    expect(fetcher.checkouts).toEqual([])
  })

  it('makes the mirror again after a run that succeeded, when it is gone or bad', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    row = pinOf(prepared, null)
    mirror.kept.clear()
    // A first run, completed: it fetched and recorded the mirror.
    const deps = { packages: { filesOf: async () => ({ pin: row!, files: {} }) }, installer }
    const job = { name: 'greeter', commit: repo.commit, contentHash: prepared.contentHash }
    expect(await runMirrorJob(deps, job)).toEqual({ outcome: 'mirrored', sha256: prepared.mirrorSha256 })
    expect(row.mirrorSha256).toBe(prepared.mirrorSha256)

    for (const spoil of ['gone', 'corrupt'] as const) {
      // The recorded mirror is lost from the store, or other files are kept under its name.
      mirror.kept.clear()
      if (spoil === 'corrupt') {
        const other = storedZip([{ path: '.claude-plugin/plugin.json', data: Buffer.from('{"name":"greeter"}'), executable: false }])
        mirror.kept.set(prepared.mirrorSha256!, other)
      }
      records.length = 0
      const replica = cold()
      await expect(replica.materialise(row)).rejects.toThrow(/its mirror failed its check .*; a new one is being made in the background/)
      expect(fetcher.checkouts).toEqual([])
      // A new run starts though one completed before; it re-checks the mirror, fetches and rewrites it.
      expect(started).toHaveLength(1)
      await jobs.settled()
      expect(jobFetcher.checkouts).toEqual([`${URL}@${repo.commit}`])
      expect(sha256(mirror.kept.get(prepared.mirrorSha256!)!)).toBe(prepared.mirrorSha256)
      expect(records.map((r) => r.mirror.sha256)).toEqual([prepared.mirrorSha256])
      await cold().materialise(row)
      expect(jobFetcher.checkouts).toEqual([])
    }
  })

  it('runs one job per pin at a time, and a job with a good mirror fetches nothing', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    row = pinOf(prepared)
    const job = { name: 'greeter', commit: repo.commit, contentHash: prepared.contentHash }
    cold()
    jobs.start(job)
    jobs.start(job)
    await jobs.settled()
    expect(jobFetcher.checkouts).toEqual([])
    expect(records).toEqual([])
    expect(await installer.makeMirror(row)).toEqual({ outcome: 'kept', sha256: prepared.mirrorSha256 })
  })

  it('answers not-pinned for a commit the row no longer names', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    row = pinOf(prepared)
    const deps = { packages: { filesOf: async () => ({ pin: row!, files: {} }) }, installer }
    expect(await runMirrorJob(deps, { name: 'greeter', commit: 'f'.repeat(40), contentHash: prepared.contentHash })).toEqual({
      outcome: 'not-pinned',
    })
    expect(jobFetcher.checkouts).toEqual([])
  })

  it('refuses, for good, a fetch whose files do not hash to the pin', async () => {
    const prepared = await installer.prepare(validateSource({ kind: 'git', url: URL }))
    const wrong = { ...pinOf(prepared, null), contentHash: `sha256:${'0'.repeat(64)}` }
    await expect(installer.makeMirror(wrong)).rejects.toBeInstanceOf(MirrorRefusedError)
    expect(records).toEqual([])
  })

  it("mirrors and loads a package past a download's caps but within the content hash's (more than 500 files, one over 2 MiB)", async () => {
    const files: Record<string, string> = { ...GREETER, 'big/blob.txt': 'x'.repeat(3 * 1024 * 1024) }
    for (let i = 0; i < 520; i++) files[`notes/n${i}.md`] = `note ${i}\n`
    const big = gitRepo(files)
    try {
      const fetchers = { greeter: repo, big }
      const bigFetcher = localFetcher(fetchers)
      const bigInstaller = new PackageInstaller({
        fetcher: bigFetcher,
        cacheRoot,
        resolve: resolver(),
        mirror: () => mirror,
        recordMirror: async () => undefined,
        warn: (message) => warnings.push(message),
      })
      const prepared = await bigInstaller.prepare(validateSource({ kind: 'git', url: 'https://git.test/big.git' }))
      expect(prepared.mirrorSha256).toMatch(/^[0-9a-f]{64}$/)
      rmSync(cacheRoot, { recursive: true, force: true })
      bigFetcher.checkouts.length = 0
      const dir = await bigInstaller.materialise(pinOf(prepared))
      expect(bigFetcher.checkouts).toEqual([])
      expect(statSync(path.join(dir, 'big/blob.txt')).size).toBe(3 * 1024 * 1024)
      expect(statSync(path.join(dir, 'notes/n519.md')).isFile()).toBe(true)
      expect(warnings).toEqual([])
    } finally {
      big.remove()
    }
  })

  it("keeps the largest package's zip under the API's cap, with room for long paths", () => {
    // 2,000 files of 20 MiB in all, each path 255 bytes: 76 bytes and twice the path of headers each.
    const size = Math.floor(MAX_TOTAL_BYTES / MAX_FILES)
    const files = Array.from({ length: MAX_FILES }, (_, i) => ({
      path: `${String(i).padStart(4, '0')}/${'p'.repeat(250)}`,
      data: Buffer.alloc(size, 0x61),
      executable: false,
    }))
    const zip = storedZip(files)
    expect(zip.length).toBeGreaterThan(21 * 1024 * 1024) // past the old cap
    expect(zip.length).toBeLessThanOrEqual(MIRROR_MAX_BYTES)
    expect(readArchive(zip, COMMIT_MIRROR_LIMITS).filter((e) => !e.dir)).toHaveLength(MAX_FILES)
    // A download's caps still refuse it.
    expect(() => readArchive(zip)).toThrow(UnpackError)
  })

  it('refuses a mirror whose files hash to the pin but fail the vetting, as the files themselves, and starts no job', async () => {
    const bad = gitRepo({ ...GREETER, 'skills/x/SKILL.md': '!`env`\n' })
    try {
      const badFetcher = localFetcher({ greeter: bad })
      const badInstaller = new PackageInstaller({
        fetcher: badFetcher,
        mirrorFetcher: jobFetcher,
        cacheRoot,
        resolve: resolver(),
        mirror: () => mirror,
        recordMirror: async () => undefined,
        mirrorJobs: () => ({ start: (job) => started.push(mirrorWorkflowId(job)) }),
      })
      const prepared = await badInstaller.prepare(validateSource({ kind: 'git', url: URL }))
      expect(prepared.mirrorSha256).toMatch(/^[0-9a-f]{64}$/)
      rmSync(cacheRoot, { recursive: true, force: true })
      badFetcher.checkouts.length = 0
      const pin = pinOf(prepared)
      await expect(badInstaller.materialise(pin)).rejects.toThrow(/refused/)
      const loaded = await loadPackagesForRun({ enabledPins: () => Promise.resolve([pin]) }, badInstaller)
      expect(loaded.loaded).toEqual([])
      expect(loaded.problems).toEqual([expect.stringMatching(/^plugin package greeter was not loaded: skills\/x\/SKILL.md: runs a shell command/)])
      expect(loaded.problems.join()).not.toMatch(/mirror/)
      // The mirror was good: no job, and no git.
      expect(started).toEqual([])
      expect(badFetcher.checkouts).toEqual([])
      expect(jobFetcher.checkouts).toEqual([])
    } finally {
      bad.remove()
    }
  })

  it('mirrors nothing at install without a record to keep it', async () => {
    const plain = new PackageInstaller({ fetcher, cacheRoot, resolve: resolver(), mirror: () => mirror })
    const prepared = await plain.prepare(validateSource({ kind: 'git', url: URL }))
    expect(prepared.mirrorSha256).toBeUndefined()
    expect(mirror.kept.size).toBe(0)
  })
})
