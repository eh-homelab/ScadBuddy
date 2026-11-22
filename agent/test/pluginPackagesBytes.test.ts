import { createHash } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ARCHIVE_LIMITS, egressDownload, MARKETPLACE_LIMITS } from '../src/plugins/packages/download.js'
import { marketplaceTarget, PackageInstaller, PackageRefusedError } from '../src/plugins/packages/install.js'
import type { PackageMirror } from '../src/plugins/packages/mirror.js'
import { validateSource, withRepinRef } from '../src/plugins/packages/source.js'
import { UNPACK_MAX_TOTAL_BYTES } from '../src/plugins/packages/unpack.js'
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

/** The API's mirror, in memory: a file is read back only by the package it was sent for. */
function memoryMirror(): PackageMirror & { kept: Map<string, Buffer>; names: Map<string, string> } {
  const kept = new Map<string, Buffer>()
  const names = new Map<string, string>()
  return {
    kept,
    names,
    put: async (name, sha, bytes) => {
      expect(sha256(bytes)).toBe(sha)
      kept.set(sha, Buffer.from(bytes))
      names.set(sha, name)
    },
    get: async (name, sha) => {
      const bytes = kept.get(sha)
      if (!bytes || names.get(sha) !== name) throw new PluginError(`no mirrored copy of the pinned file ${sha} is kept`, 502)
      return bytes
    },
  }
}

/** `chunked`: the body is written with no Content-Length, so only the stream's cap stops it. */
type Route = { status?: number; body?: Buffer | string; location?: string; type?: string; chunked?: boolean }
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
    if (route.chunked) {
      res.write(route.body ?? '')
      res.end()
    } else res.end(route.body ?? '')
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

  it('takes plain http only to loopback for archives, marketplace URLs and npm', () => {
    for (const bad of [
      { kind: 'archive', url: 'http://files.test/p.zip' },
      { kind: 'archive', url: 'http://10.0.0.5/p.zip' },
      { kind: 'marketplace-url', url: 'http://plugins.test/marketplace.json', entry: 'p' },
      { kind: 'npm', package: 'p', registry: 'http://npm.test' },
      { kind: 'npm', package: 'http://files.test/p-1.0.0.tgz' },
    ] as const) {
      expect(() => validateSource(bad), JSON.stringify(bad)).toThrow(/https/)
    }
    expect(validateSource({ kind: 'archive', url: 'http://127.0.0.1:8080/p.zip' }).url).toBe('http://127.0.0.1:8080/p.zip')
    expect(validateSource({ kind: 'archive', url: 'http://localhost/p.zip' }).url).toBe('http://localhost/p.zip')
    expect(() =>
      marketplaceTarget({ plugins: [{ name: 'p', source: { source: 'archive', url: 'http://files.test/p.zip' } }] }, 'p', 'https://plugins.test/m.json'),
    ).toThrow(/https/)
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
    // Sent for the package it names, which alone reads it back.
    expect(mirror.names.get(sha256(bytes))).toBe('greeter')
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

  it('refuses a redirect to a refused address, and to loopback http (plain http is for the first hop only)', async () => {
    routes['/greeter.zip'] = { body: greeterZip() }
    routes['/r0'] = { status: 302, location: '/greeter.zip' }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/r0` }))).rejects.toThrow(/https/)
    routes['/r1'] = { status: 302, location: `${base}/greeter.zip` }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/r1` }))).rejects.toThrow(/https/)
    routes['/meta'] = { status: 302, location: 'http://169.254.169.254/latest/meta-data' }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/meta` }))).rejects.toThrow(/link-local|metadata/)
    expect(hits).not.toContain('/greeter.zip')
  })

  it('names an archive whose URL has a malformed percent escape, with no 500', async () => {
    routes['/bad%zz.zip'] = { body: greeterZip() }
    const prepared = await installer.prepare(validateSource({ kind: 'archive', url: `${base}/bad%zz.zip` }))
    expect(prepared.review.name).toBeTruthy()
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

  it('checks an npm tarball against the shasum when there is no integrity, and refuses one with neither', async () => {
    const tgz = greeterTgz()
    const tarball = `${base}/t/greeter-1.0.0.tgz`
    routes['/t/greeter-1.0.0.tgz'] = { body: tgz }
    const doc = (dist: Record<string, string>) => ({ type: 'application/json', body: JSON.stringify({ dist: { tarball, ...dist } }) })
    const source = validateSource({ kind: 'npm', package: 'greeter', version: '1.0.0', registry: base })
    routes['/greeter/1.0.0'] = doc({ shasum: createHash('sha1').update(tgz).digest('hex') })
    await expect(installer.prepare(source)).resolves.toMatchObject({ bytesSha256: sha256(tgz) })
    routes['/greeter/1.0.0'] = doc({ shasum: '0'.repeat(40) })
    await expect(installer.prepare(source)).rejects.toThrow(/shasum/)
    mirror.kept.clear()
    routes['/greeter/1.0.0'] = doc({})
    await expect(installer.prepare(source)).rejects.toThrow(/neither an integrity nor a shasum/)
    expect(mirror.kept.size).toBe(0)
  })

  it('refuses a redirect or a registry tarball link to plain http off loopback', async () => {
    routes['/r'] = { status: 302, location: 'http://192.168.1.10/p.zip' }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/r` }))).rejects.toThrow(/https/)
    const integrity = `sha512-${createHash('sha512').update(greeterTgz()).digest('base64')}`
    routes['/greeter/1.0.0'] = { type: 'application/json', body: JSON.stringify({ dist: { tarball: 'http://10.1.2.3/greeter.tgz', integrity } }) }
    await expect(installer.prepare(validateSource({ kind: 'npm', package: 'greeter', version: '1.0.0', registry: base }))).rejects.toThrow(/https/)
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

describe('download caps', () => {
  it('caps an archive at the unpacked cap plus 1 MiB, and a marketplace file at 5 MiB', () => {
    expect(ARCHIVE_LIMITS.maxBytes).toBe(UNPACK_MAX_TOTAL_BYTES + 1024 * 1024)
    expect(MARKETPLACE_LIMITS.maxBytes).toBe(5 * 1024 * 1024)
  })

  it('stops a file past its cap, by its Content-Length or as it streams', async () => {
    for (const [limits, what] of [
      [MARKETPLACE_LIMITS, 'the marketplace'],
      [ARCHIVE_LIMITS, 'archive'],
    ] as const) {
      routes['/at-cap'] = { body: Buffer.alloc(limits.maxBytes) }
      const got = await egressDownload(`${base}/at-cap`, { what, ...limits })
      expect(got.bytes.length).toBe(limits.maxBytes)
      for (const chunked of [false, true]) {
        routes['/past-cap'] = { body: Buffer.alloc(limits.maxBytes + 1), chunked }
        await expect(egressDownload(`${base}/past-cap`, { what, ...limits }), `${what} chunked=${chunked}`).rejects.toMatchObject({
          status: 422,
          message: `${what} is larger than ${limits.maxBytes} bytes`,
        })
      }
    }
  })

  it('refuses an archive install past the download cap', async () => {
    routes['/big.zip'] = { body: Buffer.alloc(ARCHIVE_LIMITS.maxBytes + 1), chunked: true }
    await expect(installer.prepare(validateSource({ kind: 'archive', url: `${base}/big.zip` }))).rejects.toThrow(/larger than/)
    expect(mirror.kept.size).toBe(0)
  })
})
