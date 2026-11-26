import { execFileSync } from 'node:child_process'
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { extractSubdir, GitFetcher } from '../src/plugins/packages/git.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import { PackageInstaller, PackageRefusedError } from '../src/plugins/packages/install.js'
import { validateSource } from '../src/plugins/packages/source.js'
import type { PackagePin } from '../src/plugins/packages/store.js'
import { type Files, gitMissing, gitRepo, GREETER, localFetcher, resolver, type TestRepo } from './support/gitRepo.js'

// A package in one folder of a repository gets only that folder's blobs (#7908):
// a partial fetch (`--filter=blob:none`) and a sparse checkout of the folder
// (src/plugins/packages/git.ts). The files, and so the content hash, must be
// those of the full fetch, or no pin made before would verify.

const ENV = { ...process.env, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' }
const git = (dir: string, ...args: string[]) => execFileSync('git', ['-C', dir, ...args], { env: ENV, encoding: 'utf8' }).trim()

/** The blobs the checkout holds itself (`--batch-all-objects` never fetches a missing one). */
function localBlobs(dir: string): Set<string> {
  const out = git(dir, 'cat-file', '--batch-all-objects', '--batch-check=%(objecttype) %(objectname)')
  return new Set(out.split('\n').filter((l) => l.startsWith('blob ')).map((l) => l.slice('blob '.length)))
}

/** The blobs of `commit` under `prefix` in `repo`. */
function blobsUnder(repo: string, commit: string, prefix: string): Set<string> {
  const out = git(repo, 'ls-tree', '-r', '--full-tree', commit, '--', prefix || '.')
  return new Set(out.split('\n').filter(Boolean).map((l) => l.split(/\s+/)[2] as string))
}

const PLUGIN: Files = Object.fromEntries(Object.entries(GREETER).map(([k, v]) => [`tools/greeter/${k}`, v]))
const OTHER: Files = {
  'README.md': '# mono\n',
  'tools/greeter-extra/notes.md': 'a sibling folder whose name starts with the path\n',
  'big/data.bin': 'x'.repeat(256 * 1024),
  'docs/guide.md': '# guide\n',
}

describe.skipIf(gitMissing !== undefined)(`a package's path from git${gitMissing ? ` (skipped: ${gitMissing})` : ''}`, () => {
  const temps: string[] = []
  const repos: TestRepo[] = []
  const temp = () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), 'pkg-sparse-'))
    temps.push(dir)
    return dir
  }
  const repo = (files: Files, options: Parameters<typeof gitRepo>[1] = {}) => {
    const r = gitRepo(files, options)
    repos.push(r)
    return r
  }
  afterEach(() => {
    for (const dir of temps.splice(0)) rmSync(dir, { recursive: true, force: true })
    for (const r of repos.splice(0)) r.remove()
  })
  const fetcher = new GitFetcher({ protocols: ['file'] })

  it("fetches only the path's blobs, and checks out only the path", async () => {
    const r = repo({ ...PLUGIN, ...OTHER }, { executable: ['tools/greeter/README.md'] })
    const checkout = await fetcher.checkout(pathToFileURL(r.dir).href, r.commit, path.join(temp(), 'repo'), undefined, 'tools/greeter')
    expect(checkout.commit).toBe(r.commit)
    expect(localBlobs(checkout.dir)).toEqual(blobsUnder(r.dir, r.commit, 'tools/greeter'))
    expect(existsSync(path.join(checkout.dir, 'tools/greeter/skills/hello/SKILL.md'))).toBe(true)
    for (const other of Object.keys(OTHER)) expect(existsSync(path.join(checkout.dir, other)), other).toBe(false)
    // The tree is still there whole, for the symlink and submodule listing.
    expect(await fetcher.specialPaths(checkout, 'tools/greeter')).toEqual([])
  })

  it('gives the same files and content hash as the full fetch', async () => {
    const r = repo({ ...PLUGIN, ...OTHER, 'tools/greeter/bin/run.sh': '#!/bin/sh\necho hi\n' }, { executable: ['tools/greeter/bin/run.sh'] })
    const url = pathToFileURL(r.dir).href
    const full = await fetcher.checkout(url, r.commit, path.join(temp(), 'repo'))
    const sparse = await fetcher.checkout(url, r.commit, path.join(temp(), 'repo'), undefined, 'tools/greeter')
    expect(localBlobs(full.dir).size).toBeGreaterThan(localBlobs(sparse.dir).size)
    const fullDir = path.join(temp(), 'pkg')
    const sparseDir = path.join(temp(), 'pkg')
    await extractSubdir(full, 'tools/greeter', fullDir)
    await extractSubdir(sparse, 'tools/greeter', sparseDir)
    const [a, b] = [await hashTree(fullDir), await hashTree(sparseDir)]
    expect(b.hash).toBe(a.hash)
    expect(b.files).toEqual(a.files)
    expect(Object.keys(b.files)).toContain('bin/run.sh')
  })

  it('still lists a symlink and a submodule under the path', async () => {
    const r = repo(PLUGIN, { symlinks: { 'tools/greeter/skills/leak/SKILL.md': '/etc/passwd' } })
    git(r.dir, 'update-index', '--add', '--cacheinfo', `160000,${r.commit},tools/greeter/vendor`)
    git(r.dir, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '-m', 'submodule')
    const commit = git(r.dir, 'rev-parse', 'HEAD')
    const checkout = await fetcher.checkout(pathToFileURL(r.dir).href, commit, path.join(temp(), 'repo'), undefined, 'tools/greeter')
    expect((await fetcher.specialPaths(checkout, 'tools/greeter')).sort()).toEqual([
      'tools/greeter/skills/leak/SKILL.md (symlink)',
      'tools/greeter/vendor (submodule)',
    ])
  })

  it('takes every blob from a server without partial fetch, and gets the same files', async () => {
    const r = repo({ ...PLUGIN, ...OTHER }, { filter: false })
    const url = pathToFileURL(r.dir).href
    const checkout = await fetcher.checkout(url, r.commit, path.join(temp(), 'repo'), undefined, 'tools/greeter')
    expect(checkout.commit).toBe(r.commit)
    expect(localBlobs(checkout.dir)).toEqual(blobsUnder(r.dir, r.commit, ''))
    const dest = path.join(temp(), 'pkg')
    await extractSubdir(checkout, 'tools/greeter', dest)
    const full = await fetcher.checkout(url, r.commit, path.join(temp(), 'repo'))
    const fullDest = path.join(temp(), 'pkg')
    await extractSubdir(full, 'tools/greeter', fullDest)
    expect((await hashTree(dest)).hash).toBe((await hashTree(fullDest)).hash)
  })

  it('fetches the whole commit again when the partial fetch fails', async () => {
    const r = repo({ ...PLUGIN, ...OTHER })
    // A git that fails as a server refusing `--filter` would, and runs every other command.
    const bin = temp()
    writeFileSync(
      path.join(bin, 'git'),
      `#!/bin/sh\nfor a in "$@"; do [ "$a" = --filter=blob:none ] && { echo "fatal: filter refused" >&2; exit 128; }; done\nexec git "$@"\n`,
    )
    chmodSync(path.join(bin, 'git'), 0o755)
    const refusing = new GitFetcher({ git: path.join(bin, 'git'), protocols: ['file'] })
    const checkout = await refusing.checkout(pathToFileURL(r.dir).href, r.commit, path.join(temp(), 'repo'), undefined, 'tools/greeter')
    expect(checkout.commit).toBe(r.commit)
    expect(localBlobs(checkout.dir)).toEqual(blobsUnder(r.dir, r.commit, ''))
    expect(existsSync(path.join(checkout.dir, 'tools/greeter/skills/hello/SKILL.md'))).toBe(true)
  })

  it('fetches the whole commit when there is no path', async () => {
    const r = repo({ ...GREETER, ...OTHER })
    const checkout = await fetcher.checkout(pathToFileURL(r.dir).href, r.commit, path.join(temp(), 'repo'))
    expect(localBlobs(checkout.dir)).toEqual(blobsUnder(r.dir, r.commit, ''))
    for (const file of Object.keys(OTHER)) expect(existsSync(path.join(checkout.dir, file)), file).toBe(true)
    expect(readFileSync(path.join(checkout.dir, '.git', 'config'), 'utf8')).not.toContain('promisor')
  })

  describe('through the installer', () => {
    let repos_: Record<string, TestRepo>
    let installer: PackageInstaller
    beforeEach(() => {
      repos_ = {}
      installer = new PackageInstaller({ fetcher: localFetcher(repos_), cacheRoot: temp(), resolve: resolver() })
    })
    afterEach(() => {
      for (const r of Object.values(repos_)) r.remove()
    })
    const pinOf = (p: { review: { name: string }; fetchUrl: string; fetchPath: string; commit: string; contentHash: string }): PackagePin => ({
      name: p.review.name,
      fetchUrl: p.fetchUrl,
      fetchPath: p.fetchPath,
      commit: p.commit,
      contentHash: p.contentHash,
    })

    it('verifies a pin made by the full fetch', async () => {
      repos_.mono = gitRepo({ ...PLUGIN, ...OTHER }, { filter: false })
      const source = validateSource({ kind: 'git', url: 'https://git.test/mono.git', path: 'tools/greeter' })
      const pin = pinOf(await installer.prepare(source))
      // The same server now serves partial fetches: a cold load takes the sparse checkout.
      git(repos_.mono.dir, 'config', 'uploadpack.allowFilter', 'true')
      git(repos_.mono.dir, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
      rmSync(installer.cacheDir(pin), { recursive: true, force: true })
      const dir = await installer.materialise(pin)
      expect(dir).toBe(installer.cacheDir(pin))
      expect(existsSync(path.join(dir, 'skills/hello/SKILL.md'))).toBe(true)
    })

    it('refuses a symlink under the path', async () => {
      repos_.mono = gitRepo({ ...PLUGIN, ...OTHER }, { symlinks: { 'tools/greeter/skills/leak/SKILL.md': '/etc/passwd' } })
      const err = await installer
        .prepare(validateSource({ kind: 'git', url: 'https://git.test/mono.git', path: 'tools/greeter' }))
        .catch((e: unknown) => e)
      expect(err).toBeInstanceOf(PackageRefusedError)
      expect((err as PackageRefusedError).problems.join()).toMatch(/tools\/greeter\/skills\/leak\/SKILL\.md \(symlink\)/)
    })
  })
})
