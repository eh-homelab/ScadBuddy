import { mkdtempSync, rmSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { OperationRefusal } from '../src/operations/kinds.js'
import { GitFetcher } from '../src/plugins/packages/git.js'
import type { PackageInstaller } from '../src/plugins/packages/install.js'
import { INSTALL_KIND, packageKinds, REPIN_KIND } from '../src/plugins/packages/operations.js'
import type { PackageSource } from '../src/plugins/packages/source.js'
import type { PackageRepo, PreparedPackage } from '../src/plugins/packages/store.js'
import { gitMissing, gitRepo, GREETER, type TestRepo } from './support/gitRepo.js'

// The plugin package kinds (src/plugins/packages/operations.ts, #1055) without Temporal
// or Postgres: the fetch cap answers in the check, before the record, and a run's
// cancellation reaches git.

const temps: string[] = []
const repos: TestRepo[] = []
afterEach(() => {
  for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true })
  for (const repo of repos.splice(0)) repo.remove()
})

/** An installer whose fetches wait until released. */
function heldInstaller() {
  const held: (() => void)[] = []
  const signals: (AbortSignal | undefined)[] = []
  const installer: Pick<PackageInstaller, 'prepare'> = {
    prepare: (source: PackageSource, signal?: AbortSignal) => {
      signals.push(signal)
      return new Promise<PreparedPackage>((resolve) => held.push(() => resolve({ source } as PreparedPackage)))
    },
  }
  return { installer, held, signals }
}

const packages = {
  create: async (p: PreparedPackage) => ({ name: 'x', source: p.source }),
  setPending: async (name: string) => ({ name }),
  pinOf: async () => ({ source: { kind: 'git', url: 'https://git.test/a.git', ref: 'HEAD', path: '' } }),
} as unknown as PackageRepo

const source = (url: string) => ({ source: { kind: 'git', url, ref: 'HEAD', path: '' } })

function kinds(installer: Pick<PackageInstaller, 'prepare'>, maxConcurrentFetches = 1) {
  const all = packageKinds({ packages, installer, maxConcurrentFetches })
  return { install: all.find((k) => k.name === INSTALL_KIND)!, repin: all.find((k) => k.name === REPIN_KIND)! }
}

async function status(work: Promise<unknown>): Promise<number | 'ok'> {
  try {
    await work
    return 'ok'
  } catch (err) {
    if (err instanceof OperationRefusal) return err.problem.status
    throw err
  }
}

describe('the fetch cap', () => {
  it('refuses with 429 in the check, before any record, while the fetches are taken', async () => {
    const { installer, held } = heldInstaller()
    const { install, repin } = kinds(installer)
    const a = source('https://git.test/a.git')
    const first = install.run(a, await install.check(a))
    await expect.poll(() => held.length).toBe(1)
    expect(await status(install.check(source('https://git.test/b.git')))).toBe(429)
    expect(await status(repin.check({ name: 'a' }))).toBe(429)
    held[0]!()
    await first
    expect(await status(install.check(source('https://git.test/b.git')))).toBe('ok')
  })

  it('refuses a second fetch of the same package in the check', async () => {
    const { installer, held } = heldInstaller()
    const { install } = kinds(installer, 2)
    const a = source('https://git.test/a.git')
    const first = install.run(a, await install.check(a))
    await expect.poll(() => held.length).toBe(1)
    expect(await status(install.check(a))).toBe(429)
    held[0]!()
    await first
  })

  it('makes a run that passed its check wait for a fetch, never refuse', async () => {
    const { installer, held } = heldInstaller()
    const { install } = kinds(installer)
    const a = source('https://git.test/a.git')
    const b = source('https://git.test/b.git')
    // Both checked while nothing runs: the record exists for both, so neither run may answer 429.
    const checkedA = await install.check(a)
    const checkedB = await install.check(b)
    const first = install.run(a, checkedA)
    const second = install.run(b, checkedB)
    await expect.poll(() => held.length).toBe(1)
    held[0]!()
    await first
    await expect.poll(() => held.length).toBe(2)
    held[1]!()
    expect(await status(second)).toBe('ok')
  })
})

describe('cancellation', () => {
  it("hands the run's signal to the fetch", async () => {
    const { installer, held, signals } = heldInstaller()
    const { install } = kinds(installer)
    const a = source('https://git.test/a.git')
    const abort = new AbortController()
    const run = install.run(a, await install.check(a), abort.signal)
    await expect.poll(() => held.length).toBe(1)
    expect(signals).toEqual([abort.signal])
    held[0]!()
    await run
  })

  it('stops a waiting run when it is cancelled', async () => {
    const { installer, held } = heldInstaller()
    const { install } = kinds(installer)
    const a = source('https://git.test/a.git')
    const b = source('https://git.test/b.git')
    const checkedB = await install.check(b)
    const first = install.run(a, await install.check(a))
    await expect.poll(() => held.length).toBe(1)
    const abort = new AbortController()
    const second = install.run(b, checkedB, abort.signal)
    abort.abort()
    await expect(second).rejects.toMatchObject({ name: 'AbortError' })
    held[0]!()
    await first
    expect(held).toHaveLength(1)
  })

  it.skipIf(gitMissing !== undefined)('kills git when the signal aborts', async () => {
    const repo = gitRepo(GREETER)
    repos.push(repo)
    const into = mkdtempSync(path.join(os.tmpdir(), 'pkg-abort-'))
    temps.push(into)
    const fetcher = new GitFetcher({ protocols: ['file'] })
    await expect(
      fetcher.checkout(`file://${repo.dir}`, 'HEAD', path.join(into, 'repo'), AbortSignal.abort()),
    ).rejects.toMatchObject({ name: 'AbortError' })
  })
})
