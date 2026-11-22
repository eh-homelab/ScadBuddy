import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { marketplaceTarget, PackageInstaller, PackageRefusedError } from '../src/plugins/packages/install.js'
import type { PackageMirror } from '../src/plugins/packages/mirror.js'
import { validateSource, withRepinRef } from '../src/plugins/packages/source.js'
import type { PackagePin, PreparedPackage } from '../src/plugins/packages/store.js'
import { PluginError } from '../src/plugins/registry.js'
import { tar, zip } from './support/archives.js'
import { GREETER, localFetcher, resolver } from './support/gitRepo.js'

// Installing from any URL Claude Code takes, pinned to bytes (#6935, spec 2026-11-21
// §6.6): a bare marketplace.json URL, an archive, an npm package. The bytes are fetched
// once through the egress check, pinned by their sha256 and mirrored; a load reads the
// mirror, never upstream. A local HTTP server stands in for upstream (http to loopback
// passes the egress check, as for git sources).

const sha256 = (b: Buffer) => createHash('sha256').update(b).digest('hex')

function memoryMirror(): PackageMirror & { kept: Map<string, Buffer> } {
  const kept = new Map<string, Buffer>()
  return {
    kept,
    put: async (sha, bytes) => {
      expect(sha256(bytes)).toBe(sha)
      kept.set(sha, Buffer.from(bytes))
    },
    get: async (sha) => {
      const bytes = kept.get(sha)
      if (!bytes) throw new PluginError(`no mirrored copy of the pinned file ${sha} is kept`, 502)
      return bytes
    },
  }
}

type Route = { status?: number; body?: Buffer | string; location?: string; type?: string }
let routes: Record<string, Route>
let server: Server
let base: string
let cacheRoot: string
let mirror: ReturnType<typeof memoryMirror>
let installer: PackageInstaller
let hits: string[]

beforeEach(async () => {
  routes = {}
  hits = []
  server = createServer((req, res) => {
    hits.push(req.url ?? '')
    const route = routes[req.url ?? '']
    if (!route) {
      res.writeHead(404).end()
      return
    }
    res.writeHead(route.status ?? 200, {
      ...(route.location ? { location: route.location } : {}),
      'content-type': route.type ?? 'application/octet-stream',
    })
    res.end(route.body ?? '')
  })
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`
  cacheRoot = mkdtempSync(path.join(os.tmpdir(), 'pkg-bytes-'))
  mirror = memoryMirror()
  installer = new PackageInstaller({ fetcher: localFetcher({}), cacheRoot, resolve: resolver(), mirror: () => mirror })
})

afterEach(async () => {
  server.closeAllConnections()
  await new Promise<void>((resolve) => server.close(() => resolve()))
  rmSync(cacheRoot, { recursive: true, force: true })
})

const greeterZip = (top = 'greeter-1.0/') =>
  zip(Object.entries(GREETER).map(([name, data]) => ({ name: `${top}${name}`, data, deflate: true })))
const greeterTgz = () => tar(Object.entries(GREETER).map(([name, data]) => ({ name: `package/${name}`, data })), { gzip: true })

const pinOf = (p: PreparedPackage): PackagePin => ({
  name: p.review.name,
  fetchUrl: p.fetchUrl,
  fetchPath: p.fetchPath,
  commit: p.commit,
  ...(p.bytesSha256 ? { bytesSha256: p.bytesSha256 } : {}),
  contentHash: p.contentHash,
})

describe('marketplace entries that are not repositories', () => {
  const url = 'https://plugins.test/marketplace.json'
  const m = (source: unknown) => ({ plugins: [{ name: 'p', source }] })

  it('accepts archive and npm entries', () => {
    const sha = 'A'.repeat(64)
    expect(marketplaceTarget(m({ source: 'archive', url: 'https://files.test/p.zip', sha256: sha }), 'p', url)).toEqual({
      kind: 'bytes',
      source: { kind: 'archive', url: 'https://files.test/p.zip', ref: `sha256:${'a'.repeat(64)}` },
    })
    expect(marketplaceTarget(m({ source: 'npm', package: '@acme/p', version: '2.0.0' }), 'p', url)).toEqual({
      kind: 'bytes',
      source: { kind: 'npm', url: 'https://registry.npmjs.org/@acme%2fp', ref: '2.0.0' },
    })
    expect(marketplaceTarget(m({ source: 'npm', package: 'p', registry: 'https://npm.test' }), 'p', url)).toEqual({
      kind: 'bytes',
      source: { kind: 'npm', url: 'https://npm.test/p', ref: 'latest' },
    })
  })

  it('still refuses a command source, and a relative path in a bare marketplace URL', () => {
    expect(() => marketplaceTarget(m({ source: 'command', command: 'make' }), 'p', url)).toThrow(/command/)
    expect(() => marketplaceTarget(m('./plugins/p'), 'p', url, { bare: true })).toThrow(/relative/)
    expect(() => marketplaceTarget(m({ source: 'npm', package: 'https://github.com/o/r/archive/v1.tgz' }), 'p', url)).toThrow(
      /github.com is refused/,
    )
  })
})

describe('sources', () => {
  it('validates archive and npm sources as Claude Code does', () => {
    expect(validateSource({ kind: 'archive', url: 'https://files.test/p.zip' })).toEqual({ kind: 'archive', url: 'https://files.test/p.zip', ref: '' })
    expect(validateSource({ kind: 'npm', package: '@acme/p@1.2.3' })).toEqual({
      kind: 'npm',
      url: 'https://registry.npmjs.org/@acme%2fp',
      ref: '1.2.3',
    })
    // A stored source validates as it is.
    expect(validateSource({ kind: 'npm', url: 'https://registry.npmjs.org/@acme%2fp', ref: '1.2.3' })).toEqual({
      kind: 'npm',
      url: 'https://registry.npmjs.org/@acme%2fp',
      ref: '1.2.3',
    })
    expect(validateSource({ kind: 'npm', package: 'https://gitlab.com/api/v4/projects/1/packages/npm/p/-/p-1.0.0.tgz' }).ref).toBe('')
    for (const bad of [
      { kind: 'npm', package: 'git+https://github.com/o/r.git' },
      { kind: 'npm', package: 'file:../p' },
      { kind: 'npm', package: 'npm:other@1' },
      { kind: 'npm', package: 'p', version: '^1.0.0' },
      { kind: 'npm', package: 'https://bitbucket.org/o/r/p.tgz' },
      { kind: 'npm', package: 'https://git.sr.ht/~o/r/p.tgz' },
      { kind: 'archive', url: 'ftp://files.test/p.zip' },
      { kind: 'archive', url: 'https://files.test/p.zip', sha256: 'abc' },
      { kind: 'marketplace-url', url: 'https://plugins.test/marketplace.json' },
    ] as const) {
      expect(() => validateSource(bad), JSON.stringify(bad)).toThrow(PluginError)
    }
  })

  it('re-pins an npm package to a version, and refuses a ref for an archive', () => {
    expect(withRepinRef({ kind: 'npm', url: 'https://registry.npmjs.org/p', ref: '1.0.0' }, '1.1.0').ref).toBe('1.1.0')
    expect(() => withRepinRef({ kind: 'archive', url: 'https://files.test/p.zip', ref: '' }, 'main')).toThrow(/no ref/)
  })
})

describe('installing bytes', () => {
  it('pins an archive to its sha256, mirrors it, and finds the plugin one directory down', async () => {
    const bytes = greeterZip()
    routes['/greeter.zip'] = { body: bytes }
    const prepared = await installer.prepare(validateSource({ kind: 'archive', url: `${base}/greeter.zip` }))
    expect(prepared.bytesSha256).toBe(sha256(bytes))
    expect(prepared.commit).toBe('')
    expect(prepared.fetchPath).toBe('greeter-1.0')
    expect(prepared.fetchUrl).toBe(`${base}/greeter.zip`)
    expect(prepared.review.name).toBe('greeter')
    expect(Object.keys(prepared.files)).toContain('skills/hello/SKILL.md')
    expect(mirror.kept.get(sha256(bytes))).toEqual(bytes)
  })

  it('refuses an archive whose sha256 is not the one named', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    const source = validateSource({ kind: 'archive', url: `${base}/greeter.zip`, sha256: '0'.repeat(64) })
    await expect(installer.prepare(source)).rejects.toThrow(/sha256/)
    expect(mirror.kept.size).toBe(0)
  })

  it('refuses an archive with a symbolic link or a path out of it', async () => {
    routes['/link.zip'] = { body: zip([...Object.entries(GREETER).map(([name, data]) => ({ name, data })), { name: 'skills/x', data: '/etc', mode: 0o120777 }]) }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/link.zip` }))).rejects.toBeInstanceOf(PackageRefusedError)
    routes['/slip.zip'] = { body: zip([{ name: '../../evil', data: 'x' }]) }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/slip.zip` }))).rejects.toThrow(/leaves the archive/)
    expect(mirror.kept.size).toBe(0)
  })

  it('follows a redirect, but not past five, and not to a refused address', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    for (let i = 0; i < 6; i++) routes[`/r${i}`] = { status: 302, location: i === 5 ? '/greeter.zip' : `/r${i + 1}` }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/r1` }))).resolves.toMatchObject({ fetchPath: 'greeter-1.0' })
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/r0` }))).rejects.toThrow(/more than 5 redirects/)
    routes['/meta'] = { status: 302, location: 'http://169.254.169.254/latest/meta-data' }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/meta` }))).rejects.toThrow(/link-local|metadata/)
  })

  it('refuses a bytes install when no mirror can keep the file', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    const bare = new PackageInstaller({ fetcher: localFetcher({}), cacheRoot, resolve: resolver() })
    await expect(bare.prepare(validateSource({ kind: 'archive', url: `${base}/greeter.zip` }))).rejects.toMatchObject({ status: 503 })
  })

  it('loads a bytes pin from its mirror when upstream is gone', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    const prepared = await installer.prepare(validateSource({ kind: 'archive', url: `${base}/greeter.zip` }))
    delete routes['/greeter.zip']
    await installer.evict('greeter')
    rmSync(cacheRoot, { recursive: true, force: true })
    hits = []
    const dir = await installer.materialise(pinOf(prepared))
    expect(dir.startsWith(cacheRoot)).toBe(true)
    // Nothing was asked of upstream.
    expect(hits).toEqual([])
    // A pin whose mirror is gone does not load, and never falls back to upstream.
    mirror.kept.clear()
    await installer.evict('greeter')
    rmSync(cacheRoot, { recursive: true, force: true })
    await expect(installer.materialise(pinOf(prepared))).rejects.toThrow(/no mirrored copy/)
    expect(hits).toEqual([])
  })

  it('does not load a mirror whose files do not hash to the pin', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    const prepared = await installer.prepare(validateSource({ kind: 'archive', url: `${base}/greeter.zip` }))
    await installer.evict('greeter')
    rmSync(cacheRoot, { recursive: true, force: true })
    await expect(installer.materialise({ ...pinOf(prepared), contentHash: `sha256:${'0'.repeat(64)}` })).rejects.toThrow(/hash to/)
  })

  it('installs an npm package from a registry, checking its integrity', async () => {
    const tgz = greeterTgz()
    const integrity = `sha512-${createHash('sha512').update(tgz).digest('base64')}`
    routes['/greeter/1.0.0'] = { type: 'application/json', body: JSON.stringify({ dist: { tarball: `${base}/t/greeter-1.0.0.tgz`, integrity } }) }
    routes['/t/greeter-1.0.0.tgz'] = { body: tgz }
    const source = validateSource({ kind: 'npm', package: 'greeter', version: '1.0.0', registry: base })
    const prepared = await installer.prepare(source)
    expect(prepared.bytesSha256).toBe(sha256(tgz))
    expect(prepared.fetchPath).toBe('package')
    expect(prepared.fetchUrl).toBe(`${base}/greeter`)
    routes['/greeter/1.0.0'] = { type: 'application/json', body: JSON.stringify({ dist: { tarball: `${base}/t/greeter-1.0.0.tgz`, integrity: 'sha512-AAAA' } }) }
    await expect(installer.prepare(source)).rejects.toThrow(/integrity/)
  })

  it('installs an archive entry of a bare marketplace URL, and records the marketplace file', async () => {
    const bytes = greeterZip('')
    const market = JSON.stringify({ name: 'm', plugins: [{ name: 'greeter', source: { source: 'archive', url: `${base}/g.zip`, sha256: sha256(bytes) } }] })
    routes['/marketplace.json'] = { type: 'application/json', body: market }
    routes['/g.zip'] = { body: bytes }
    const prepared = await installer.prepare(validateSource({ kind: 'marketplace-url', url: `${base}/marketplace.json`, entry: 'greeter' }))
    expect(prepared.bytesSha256).toBe(sha256(bytes))
    expect(prepared.fetchUrl).toBe(`${base}/g.zip`)
    expect(prepared.fetchPath).toBe('')
    expect(prepared.review.marketplace_sha256).toBe(sha256(Buffer.from(market)))
  })
})
