import { createHash } from 'node:crypto'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { hashTree } from '../src/plugins/packages/hash.js'
import { CrudHostedPlugins, type HostedPlugins, type HostedVersion } from '../src/plugins/packages/hosted.js'
import { PackageInstaller, PackageRefusedError } from '../src/plugins/packages/install.js'
import { offerHosted } from '../src/plugins/packages/operations.js'
import { validateSource, withRepinRef } from '../src/plugins/packages/source.js'
import type { PackagePin, PackageRepo, PackageView, PreparedPackage } from '../src/plugins/packages/store.js'
import { PluginError } from '../src/plugins/registry.js'
import { zip } from './support/archives.js'
import { GREETER, localFetcher, resolver } from './support/gitRepo.js'
import { packageView } from './support/memoryPackages.js'

// A hosted plugin as an installed package (#6934, spec 2026-11-21 §6.3 step 5, §6.5): its
// latest version's zip read over CRUD (hosted.ts), checked against the version's zip sha256
// and content hash, vetted, pinned to the version; a cache miss reads that version's zip
// again. An install of a hosted source installs, or re-pins the installed hosted package.

const sha256 = (b: Buffer) => createHash('sha256').update(b).digest('hex')

/** A published version: its zip (files at the root, as the API builds it) and its content hash. */
async function version(n: number, extra: Record<string, string> = {}): Promise<{ zip: Buffer; row: HostedVersion }> {
  const files: Record<string, string> = {
    ...GREETER,
    '.claude-plugin/plugin.json': JSON.stringify({ name: 'greeter', description: 'Says hello.', version: `${n}.0.0` }),
    ...extra,
  }
  const dir = mkdtempSync(path.join(os.tmpdir(), 'hosted-tree-'))
  try {
    for (const [rel, data] of Object.entries(files)) {
      mkdirSync(path.dirname(path.join(dir, rel)), { recursive: true })
      writeFileSync(path.join(dir, rel), data)
    }
    const { hash } = await hashTree(dir)
    const bytes = zip(Object.entries(files).map(([name, data]) => ({ name, data, deflate: true })))
    return { zip: bytes, row: { version: n, zip_sha256: sha256(bytes), content_hash: hash } }
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
}

/** The API's hosted plugins, in memory; `reads` counts the zips read. */
function memoryHosted(): HostedPlugins & { versions: Map<number, { zip: Buffer; row: HostedVersion }>; reads: number[] } {
  const versions = new Map<number, { zip: Buffer; row: HostedVersion }>()
  const reads: number[] = []
  return {
    versions,
    reads,
    latest: async (name) => {
      if (name !== 'greeter') return undefined
      const newest = Math.max(0, ...versions.keys())
      return newest ? versions.get(newest)!.row : null
    },
    zip: async (_name, n, zipSha256) => {
      reads.push(n)
      const found = versions.get(n)
      if (!found) throw new PluginError(`the hosted plugin greeter has no version ${n}`, 502)
      if (zipSha256 !== undefined && sha256(found.zip) !== zipSha256) throw new PluginError('zip differs', 502)
      return found.zip
    },
  }
}

let cacheRoot: string
let hosted: ReturnType<typeof memoryHosted>
let installer: PackageInstaller

beforeEach(() => {
  cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-hosted-'))
  hosted = memoryHosted()
  installer = new PackageInstaller({ fetcher: localFetcher({}), cacheRoot, resolve: resolver(), hosted: () => hosted })
})

afterEach(() => {
  rmSync(cacheRoot, { recursive: true, force: true })
})

const pinOf = (p: PreparedPackage): PackagePin => ({
  name: p.review.name,
  fetchUrl: p.fetchUrl,
  fetchPath: p.fetchPath,
  commit: p.commit,
  ...(p.version !== undefined ? { version: p.version } : {}),
  contentHash: p.contentHash,
})

describe('the hosted source', () => {
  it('names a hosted plugin and takes no url or ref', () => {
    expect(validateSource({ kind: 'hosted', name: 'greeter' })).toEqual({ kind: 'hosted', name: 'greeter' })
    expect(() => validateSource({ kind: 'hosted', name: 'Greeter' })).toThrow(/hosted plugin name/)
    expect(() => validateSource({ kind: 'hosted', name: 'a--b' })).toThrow(/hosted plugin name/)
    expect(() => validateSource({ kind: 'hosted', name: 'greeter', url: 'https://x.test' })).toThrow(/not a url/)
    expect(() => validateSource({ kind: 'hosted', name: 'greeter', ref: 'main' })).toThrow(/ref is not/)
    expect(withRepinRef({ kind: 'hosted', name: 'greeter' }, undefined)).toEqual({ kind: 'hosted', name: 'greeter' })
    expect(() => withRepinRef({ kind: 'hosted', name: 'greeter' }, 'main')).toThrow(/latest version/)
  })
})

describe('installing a hosted plugin', () => {
  it('pins its latest version, checked against the content hash and vetted', async () => {
    const v1 = await version(1)
    hosted.versions.set(1, v1)
    const prepared = await installer.prepare({ kind: 'hosted', name: 'greeter' })
    expect(prepared).toMatchObject({ version: 1, commit: '', fetchUrl: 'hosted:greeter', fetchPath: '', contentHash: v1.row.content_hash })
    expect(prepared.bytesSha256).toBeUndefined()
    expect(prepared.review.name).toBe('greeter')
    expect(prepared.review.skills).toContain('greeter:hello')

    const v2 = await version(2, { 'commands/bow.md': '---\ndescription: Bows.\n---\n\nBow.\n' })
    hosted.versions.set(2, v2)
    const next = await installer.prepare({ kind: 'hosted', name: 'greeter' })
    expect(next).toMatchObject({ version: 2, contentHash: v2.row.content_hash })
  })

  it('refuses files that do not hash to the version, and a plugin with no version', async () => {
    await expect(installer.prepare({ kind: 'hosted', name: 'greeter' })).rejects.toMatchObject({ status: 409 })
    await expect(installer.prepare({ kind: 'hosted', name: 'nobody' })).rejects.toMatchObject({ status: 404 })
    const v1 = await version(1)
    hosted.versions.set(1, { zip: v1.zip, row: { ...v1.row, content_hash: `sha256:${'0'.repeat(64)}` } })
    await expect(installer.prepare({ kind: 'hosted', name: 'greeter' })).rejects.toThrow(/hash to/)
  })

  it('vets the version as every package is vetted', async () => {
    const bad = await version(1, { 'hooks/hooks.json': JSON.stringify({ hooks: { Stop: [{ hooks: [{ type: 'command', command: 'true' }] }] } }) })
    hosted.versions.set(1, bad)
    // A command hook is allowable only with `allow_refused`: the install stores it in the review.
    const prepared = await installer.prepare({ kind: 'hosted', name: 'greeter' })
    expect(prepared.review.refused?.join(' ')).toMatch(/hook/)
  })

  it('refuses a version whose zip leaves its folder', async () => {
    const bytes = zip([
      { name: '.claude-plugin/plugin.json', data: JSON.stringify({ name: 'greeter' }), deflate: true },
      { name: '../escape.md', data: 'out', deflate: true },
    ])
    hosted.versions.set(1, { zip: bytes, row: { version: 1, zip_sha256: sha256(bytes), content_hash: `sha256:${'0'.repeat(64)}` } })
    await expect(installer.prepare({ kind: 'hosted', name: 'greeter' })).rejects.toBeInstanceOf(PackageRefusedError)
  })

  it('needs the API to serve the versions', async () => {
    const without = new PackageInstaller({ fetcher: localFetcher({}), cacheRoot, resolve: resolver() })
    await expect(without.prepare({ kind: 'hosted', name: 'greeter' })).rejects.toMatchObject({ status: 503 })
  })
})

describe('loading a hosted pin', () => {
  it("reads the pinned version's zip again on a cache miss, never a newer one", async () => {
    hosted.versions.set(1, await version(1))
    const prepared = await installer.prepare({ kind: 'hosted', name: 'greeter' })
    hosted.versions.set(2, await version(2, { 'commands/bow.md': 'Bow.\n' }))
    const pin = pinOf(prepared)
    const dir = await installer.materialise(pin)
    expect(dir).toContain(`${path.sep}v1-`)
    expect(hosted.reads).toEqual([1])

    rmSync(dir, { recursive: true, force: true })
    const again = await installer.materialise(pin)
    expect(again).toBe(dir)
    expect(hosted.reads).toEqual([1, 1])

    // A version whose zip no longer hashes to the pin is not loaded.
    rmSync(dir, { recursive: true, force: true })
    await expect(installer.materialise({ ...pin, contentHash: `sha256:${'0'.repeat(64)}` })).rejects.toThrow(/hash to/)
  })
})

describe('offering a hosted plugin', () => {
  function repo(current: PackageView | undefined) {
    const calls: string[] = []
    const packages: Pick<PackageRepo, 'get' | 'create' | 'setPending'> = {
      get: async () => current,
      create: async (p) => {
        calls.push(`create ${p.version}`)
        return packageView('greeter')
      },
      setPending: async (name, p) => {
        calls.push(`pending ${name} ${p.version}`)
        return packageView('greeter')
      },
    }
    return { packages, calls }
  }

  it('installs when no package has the name, and re-pins the installed hosted package', async () => {
    hosted.versions.set(1, await version(1))
    const fresh = repo(undefined)
    await offerHosted(fresh.packages, installer, 'greeter')
    expect(fresh.calls).toEqual(['create 1'])

    hosted.versions.set(2, await version(2, { 'commands/bow.md': 'Bow.\n' }))
    const installed = repo(packageView('greeter', { source: { kind: 'hosted', name: 'greeter' }, pin_kind: 'version', version: 1 }))
    await offerHosted(installed.packages, installer, 'greeter')
    expect(installed.calls).toEqual(['pending greeter 2'])
  })

  it('refuses a package of another kind under the name, before any read', async () => {
    const other = repo(packageView('greeter'))
    await expect(offerHosted(other.packages, installer, 'greeter')).rejects.toMatchObject({ status: 409 })
    expect(other.calls).toEqual([])
    expect(hosted.reads).toEqual([])
  })
})

describe('the hosted versions through CRUD routes', () => {
  const BASE = 'http://api:8080'
  const PREFIX = '/api/v1/internal/crud/agent'

  function api(zipBytes: Buffer) {
    const seen: string[] = []
    const fetch: typeof globalThis.fetch = async (input) => {
      const url = new URL(String(input))
      const path = url.pathname.slice(PREFIX.length)
      seen.push(path)
      const crud = { 'x-scadbuddy-crud': '1' }
      if (path === '/hosted-plugins/greeter') {
        const latest = { version: 3, zip_sha256: sha256(zipBytes), content_hash: `sha256:${'1'.repeat(64)}`, files: {}, published_at: 'x', published_by: 'y' }
        return new Response(JSON.stringify({ plugin: { name: 'greeter', latest_version: 3, latest } }), {
          headers: { ...crud, 'content-type': 'application/json' },
        })
      }
      if (path === '/hosted-plugins/drafty') {
        return new Response(JSON.stringify({ plugin: { name: 'drafty', latest_version: null, latest: null } }), {
          headers: { ...crud, 'content-type': 'application/json' },
        })
      }
      if (path === '/hosted-plugins/greeter/versions/3.zip') return new Response(new Uint8Array(zipBytes), { headers: crud })
      if (path.startsWith('/hosted-plugins/')) {
        return new Response(JSON.stringify(path.endsWith('.zip') ? { detail: 'gone', reason: 'hosted-version-not-found' } : { plugin: null }), {
          status: path.endsWith('.zip') ? 404 : 200,
          headers: { ...crud, 'content-type': 'application/json' },
        })
      }
      return new Response('{}', { status: 404 })
    }
    return { fetch, seen }
  }

  it('reads the latest version and its zip, checked against its sha256', async () => {
    const bytes = Buffer.from('a zip')
    const { fetch } = api(bytes)
    const client = new CrudHostedPlugins({ baseUrl: BASE, fetch })
    const latest = await client.latest('greeter')
    expect(latest).toEqual({ version: 3, zip_sha256: sha256(bytes), content_hash: `sha256:${'1'.repeat(64)}` })
    expect(await client.latest('drafty')).toBeNull()
    expect(await client.latest('nobody')).toBeUndefined()
    expect(await client.zip('greeter', 3, latest!.zip_sha256)).toEqual(bytes)
    await expect(client.zip('greeter', 3, '0'.repeat(64))).rejects.toThrow(/does not hash/)
    await expect(client.zip('greeter', 4)).rejects.toMatchObject({ status: 502 })
  })
})
