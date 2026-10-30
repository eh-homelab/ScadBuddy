import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { describe, expect, it, vi } from 'vitest'
import { Capabilities, CapabilityError } from '../src/access/capabilities.js'
import { listedTool, type PackageListingPut, type StoredTool, toStoredTools } from '../src/access/listings.js'
import { packageServerSource, SOURCE_ITEM } from '../src/access/model.js'
import { packageProvider } from '../src/access/packageProvider.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import { PackageRefusedError, unloadablePackages } from '../src/plugins/packages/install.js'
import { listPackageServer } from '../src/plugins/packages/listing.js'
import type { PackagePin, PackageView } from '../src/plugins/packages/store.js'
import type { PackageReview } from '../src/plugins/packages/vet.js'
import type { PluginForwarder } from '../src/plugins/forwarder.js'
import type { PluginTest } from '../src/plugins/testConnection.js'
import { memoryAccess } from './support/memoryAccess.js'
import { HOOK, memoryPackages, packageReview as review, packageView as pkg } from './support/memoryPackages.js'

// Plugin packages and their MCP servers as capability sources (#4757, Task 9): a package's
// hooks, skills, subagents and allowed built-ins, and each server's tools, read and set.

const CTX = { actor: UI_ACTOR, surface: 'http' as const }
type Listing = { tools: ReturnType<typeof listedTool>[]; listed_at: string; truncated: boolean }

function memoryListings(initial: Record<string, Record<string, unknown>[]> = {}) {
  const store = new Map<string, Listing>(
    Object.entries(initial).map(([s, entries]) => [s, { tools: toStoredTools(entries).map(listedTool), listed_at: '2026-10-20T00:00:00.000Z', truncated: false }]),
  )
  return {
    store,
    get: async (source: string) => store.get(source),
    getMany: async (sources: readonly string[]) => new Map([...store].filter(([s]) => sources.includes(s))),
    exclusiveServer: async <T>(p: string, server: string, fn: (put: PackageListingPut) => Promise<T>) => ({
      value: await fn(async (_hash: string, tools: readonly StoredTool[], options: { truncated?: boolean }) => {
        store.set(packageServerSource(p, server), { tools: tools.map(listedTool), listed_at: '2026-10-29T12:00:00.000Z', truncated: options.truncated ?? false })
        return true
      }),
    }),
  }
}

type RigOptions = {
  packages?: PackageView[]
  listings?: Record<string, Record<string, unknown>[]>
  verified?: (pin: PackagePin) => Promise<PackageReview>
  refresh?: Parameters<typeof packageProvider>[0]['refresh']
  unloadable?: string[]
}

function rig(opts: RigOptions = {}) {
  const mem = memoryAccess()
  const packages = memoryPackages(opts.packages ?? [pkg('notes')])
  const listings = memoryListings(opts.listings ?? { 'package:notes/mcp:api': [{ name: 'recall', description: 'Recalls' }, { name: 'a.b' }] })
  const verifiedReviewOf = vi.fn(opts.verified ?? (() => Promise.reject(new Error('no copy'))))
  const refresh = vi.fn(opts.refresh ?? (() => Promise.reject(new Error('not called'))))
  const provider = packageProvider({
    packages: packages.repo,
    access: mem.access,
    listings,
    verifiedReviewOf,
    refresh,
    ...(opts.unloadable ? { unloadable: async () => new Set(opts.unloadable) } : {}),
  })
  return { caps: new Capabilities([provider]), mem, packages, listings, verifiedReviewOf, refresh }
}

const ids = (items: { id: string }[]) => items.map((i) => i.id)

describe('plugin packages as capability sources (#4757)', () => {
  it('lists a package with its hooks, skills, subagents and servers', async () => {
    const { caps } = rig()
    const [notes] = await caps.tree()
    expect(notes).toMatchObject({ id: 'package:notes', kind: 'package', title: 'notes', enabled: true, switchable: true, listed_at: null })
    expect(ids(notes!.items)).toEqual([`hook:Stop:${HOOK}`, 'skill:jot', 'skill:wave', 'subagent:review'])
    expect(notes!.items[0]).toMatchObject({
      kind: 'hook',
      name: HOOK,
      label: 'Stop hook (http)',
      description: 'https://hooks.example/stop',
      default: 'on',
      access: 'on',
      own_access: 'on',
      allowed: null,
      fixed: null,
    })
    expect(notes!.items[1]).toMatchObject({ kind: 'skill', name: 'jot', harness_name: 'notes:jot', description: 'Jots a note', default: 'on', access: 'on' })
    expect(notes!.items[3]).toMatchObject({ kind: 'subagent', name: 'review', description: 'Reviews' })
    expect(notes!.children).toHaveLength(1)
    const [api] = notes!.children
    expect(api).toMatchObject({ id: 'package:notes/mcp:api', kind: 'package_server', title: 'api', enabled: true, switchable: true, listed_at: '2026-10-20T00:00:00.000Z' })
    expect(api!.items.map((i) => [i.id, i.harness_name, i.default, i.access])).toEqual([
      ['recall', 'mcp__plugin_notes_api__recall', 'outward', 'outward'],
      ['a.b', 'mcp__plugin_notes_api__a_b', 'outward', 'outward'],
    ])
  })

  it("lists an allowed package's built-ins, outward by default", async () => {
    const allowed = pkg('notes', { allow_refused: true }, { builtin_tools: ['Bash'], local_servers: ['db'] })
    const { caps, mem } = rig({ packages: [allowed] })
    const [notes] = await caps.tree()
    const bash = notes!.items.find((i) => i.id === 'builtin:Bash')
    expect(bash).toMatchObject({
      kind: 'builtin',
      name: 'Bash',
      default: 'outward',
      access: 'outward',
      description: "Claude Code's Bash; where several packages name it, the strictest access applies",
    })
    expect(ids(notes!.children)).toEqual(['package:notes/mcp:api', 'package:notes/mcp:db'])
    await caps.setItem('package:notes', 'builtin:Bash', 'read', CTX)
    expect(mem.rows).toContainEqual(expect.objectContaining({ source: 'package:notes', item: 'builtin:Bash', access: 'read' }))
    // Not allowed: no built-ins and no local server, whatever the review names.
    const plain = rig({ packages: [pkg('notes', {}, { builtin_tools: ['Bash'], local_servers: ['db'] })] })
    const [shown] = await plain.caps.tree()
    expect(shown!.items.some((i) => i.kind === 'builtin')).toBe(false)
    expect(ids(shown!.children)).toEqual(['package:notes/mcp:api'])
  })

  it('switches a skill off and back on, deleting the row', async () => {
    const { caps, mem } = rig()
    const off = await caps.setItem('package:notes', 'skill:jot', 'off', CTX)
    expect(off.items.find((i) => i.id === 'skill:jot')).toMatchObject({ access: 'off', own_access: 'off', overridden: true })
    expect(mem.rows).toEqual([expect.objectContaining({ source: 'package:notes', item: 'skill:jot', access: 'off' })])
    const on = await caps.setItem('package:notes', 'skill:jot', 'on', CTX)
    expect(on.items.find((i) => i.id === 'skill:jot')).toMatchObject({ access: 'on', overridden: false })
    expect(mem.rows).toEqual([])
    await expect(caps.setItem('package:notes', 'skill:jot', 'read', CTX)).rejects.toMatchObject({ status: 400 })
  })

  it('a hook off is stored by its id', async () => {
    const { caps, mem } = rig()
    const view = await caps.setItem('package:notes', `hook:Stop:${HOOK}`, 'off', CTX)
    expect(view.items[0]).toMatchObject({ id: `hook:Stop:${HOOK}`, access: 'off' })
    expect(mem.rows).toEqual([expect.objectContaining({ item: `hook:Stop:${HOOK}`, access: 'off' })])
  })

  it("an unapproved package's switch answers 409", async () => {
    const { caps, packages } = rig({ packages: [pkg('notes', { approved: false, approved_at: null, enabled: false })] })
    const [notes] = await caps.tree()
    expect(notes).toMatchObject({ enabled: false, switchable: false })
    await expect(caps.setEnabled('package:notes', true, CTX)).rejects.toMatchObject({ status: 409 })
    expect(packages.setEnabled).not.toHaveBeenCalled()
  })

  it('switching a package goes through PackageRepo.setEnabled', async () => {
    const { caps, packages, mem } = rig()
    const off = await caps.setEnabled('package:notes', false, CTX)
    expect(packages.setEnabled).toHaveBeenCalledWith('notes', false)
    expect(off.enabled).toBe(false)
    expect(off.items.every((i) => i.access === 'off')).toBe(true)
    expect(off.items.find((i) => i.id === 'skill:jot')).toMatchObject({ own_access: 'on', reason: 'Off while "notes" is switched off' })
    expect(mem.rows).toEqual([])
    expect((await caps.setEnabled('package:notes', true, CTX)).enabled).toBe(true)
  })

  it('a server of a switched-off, unapproved or unloadable package reads off by its own id, keeping own_access', async () => {
    const server = 'package:notes/mcp:api'
    const cases: [string, RigOptions][] = [
      ['off', { packages: [pkg('notes', { enabled: false })] }],
      ['unapproved', { packages: [pkg('notes', { approved: false, approved_at: null, enabled: false })] }],
      ['refused at load', { unloadable: ['notes'] }],
    ]
    for (const [what, opts] of cases) {
      const { caps } = rig({
        ...opts,
        refresh: async () => ({ status: 200, body: { ok: true, detail: 'connected', duration_ms: 1, server: null, tools: [], truncated: false, stored: [] } }),
      })
      const view = await caps.source(server)
      expect(view.items.find((i) => i.id === 'recall'), what).toMatchObject({ access: 'off', own_access: 'outward', reason: 'Off while "notes" is switched off' })
      const written = await caps.setItem(server, 'recall', 'read', CTX)
      expect(written.items.find((i) => i.id === 'recall'), what).toMatchObject({ access: 'off', own_access: 'read' })
      const { source } = await caps.refresh(server)
      expect(source.items.find((i) => i.id === 'recall'), what).toMatchObject({ access: 'off', own_access: 'read' })
    }
  })

  it('a server is on by default and switches off', async () => {
    const { caps, mem } = rig()
    const server = 'package:notes/mcp:api'
    expect((await caps.source(server)).enabled).toBe(true)
    const off = await caps.setEnabled(server, false, CTX)
    expect(off).toMatchObject({ id: server, enabled: false })
    expect(off.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'off', own_access: 'outward', reason: 'Off while "api" is switched off' })
    expect(mem.rows).toEqual([expect.objectContaining({ source: server, item: SOURCE_ITEM, access: 'off' })])
    // The package itself is untouched.
    expect((await caps.source('package:notes')).enabled).toBe(true)
  })

  it('a server tool takes a tier; a renamed one off or outward', async () => {
    const { caps, mem } = rig()
    const server = 'package:notes/mcp:api'
    const read = await caps.setItem(server, 'recall', 'read', CTX)
    expect(read.items.find((i) => i.id === 'recall')).toMatchObject({ access: 'read', overridden: true })
    expect(mem.rows).toContainEqual(expect.objectContaining({ source: server, item: 'recall', access: 'read' }))
    const renamed = (await caps.source(server)).items.find((i) => i.id === 'a.b')
    expect(renamed).toMatchObject({ allowed: ['off', 'outward'] })
    await expect(caps.setItem(server, 'a.b', 'read', CTX)).rejects.toMatchObject({ status: 409 })
    expect((await caps.setItem(server, 'a.b', 'off', CTX)).items.find((i) => i.id === 'a.b')).toMatchObject({ access: 'off' })
    await expect(caps.setItem(server, 'recall', 'on', CTX)).rejects.toMatchObject({ status: 400 })
  })

  it('colliding servers are fixed off with the reason', async () => {
    const { caps } = rig({
      packages: [pkg('a.b', {}, { mcp_servers: [{ name: 's', type: 'http', url: 'https://s.example/mcp' }] }), pkg('a_b', {}, { mcp_servers: [{ name: 's', type: 'http', url: 'https://t.example/mcp' }] })],
      listings: { 'package:a.b/mcp:s': [{ name: 'x' }], 'package:a_b/mcp:s': [{ name: 'y' }] },
    })
    const tree = await caps.tree()
    const first = tree.find((s) => s.id === 'package:a.b')!.children[0]!
    expect(first.items[0]).toMatchObject({
      id: 'x',
      access: 'off',
      fixed: { access: 'off', reason: 'Its server is named plugin_a_b_s, as "s" of plugin package a_b is, so none of them is offered' },
    })
    const second = tree.find((s) => s.id === 'package:a_b')!.children[0]!
    expect(second.items[0]!.fixed?.reason).toBe('Its server is named plugin_a_b_s, as "s" of plugin package a.b is, so none of them is offered')
    await expect(caps.setItem('package:a.b/mcp:s', 'x', 'read', CTX)).rejects.toMatchObject({ status: 409 })
    // A switched-off package's servers are not loaded, so nothing collides with them.
    const one = rig({
      packages: [pkg('a.b', {}, { mcp_servers: [{ name: 's', type: 'http', url: 'https://s.example/mcp' }] }), pkg('a_b', { enabled: false }, { mcp_servers: [{ name: 's', type: 'http', url: 'https://t.example/mcp' }] })],
      listings: { 'package:a.b/mcp:s': [{ name: 'x' }] },
    })
    expect((await one.caps.source('package:a.b/mcp:s')).items[0]!.fixed).toBeNull()
  })

  it('a server whose tools would hold __ is fixed off with the shared problem', async () => {
    const { caps } = rig({ packages: [pkg('notes', {}, { mcp_servers: [{ name: 'a__b', type: 'http', url: 'https://s.example/mcp' }] })], listings: { 'package:notes/mcp:a__b': [{ name: 'x' }] } })
    const view = await caps.source('package:notes/mcp:a__b')
    expect(view.items[0]!.fixed).toEqual({ access: 'off', reason: 'plugin package notes: MCP server "a__b" has a name that cannot be offered' })
  })

  it('a local server switches off and shows its stored tools; refresh answers 409', async () => {
    const allowed = pkg('notes', { allow_refused: true }, { local_servers: ['db'] })
    const { caps, mem, refresh } = rig({ packages: [allowed] })
    const server = 'package:notes/mcp:db'
    expect(await caps.source(server)).toMatchObject({ enabled: true, listed_at: null, items: [] })
    await mem.access.setItem(server, 'query', 'read')
    const view = await caps.source(server)
    expect(view.items).toEqual([expect.objectContaining({ id: 'query', harness_name: 'mcp__plugin_notes_db__query', access: 'read', missing: true })])
    expect((await caps.setEnabled(server, false, CTX)).enabled).toBe(false)
    await expect(caps.refresh(server)).rejects.toMatchObject({
      status: 409,
      message: '"package:notes/mcp:db" is started only by a turn, so its tools are not listed here',
    })
    expect(refresh).not.toHaveBeenCalled()
  })

  it('a review stored without hook ids reads them from the verified copy', async () => {
    const old = pkg('notes', {}, { hooks: [{ event: 'Stop', type: 'http', url: 'https://hooks.example/stop' }], described: undefined, skills: ['notes:pkg'] })
    const verified = review({ skills: ['notes:notes'] })
    const { caps, verifiedReviewOf } = rig({ packages: [old], verified: async () => verified })
    const [notes] = await caps.tree()
    expect(ids(notes!.items)).toEqual([`hook:Stop:${HOOK}`, 'skill:notes', 'skill:jot', 'skill:wave', 'subagent:review'])
    expect(verifiedReviewOf).toHaveBeenCalledWith(expect.objectContaining({ name: 'notes', contentHash: 'sha256:notes' }))
    // Kept by its content hash: read once.
    await caps.tree()
    expect(verifiedReviewOf).toHaveBeenCalledTimes(1)
    // A copy that cannot be read lists no item of the older review (no id is invented), and its package reads off.
    const unread = rig({ packages: [old] })
    const [shown] = await unread.caps.tree()
    expect(shown).toMatchObject({ enabled: false, items: [], children: [] })
  })

  it('refresh of a package answers 409', async () => {
    const { caps } = rig()
    await expect(caps.refresh('package:notes')).rejects.toMatchObject({ status: 409, message: '"package:notes" lists no tools to refresh' })
  })

  it('an unknown package or server is 404', async () => {
    const { caps } = rig()
    await expect(caps.source('package:nope')).rejects.toMatchObject({ status: 404 })
    await expect(caps.source('package:notes/mcp:nope')).rejects.toMatchObject({ status: 404 })
    await expect(caps.source('package:nope/mcp:api')).rejects.toMatchObject({ status: 404 })
  })
})

describe('listing a package server (#4757)', () => {
  async function copy(mcp: unknown): Promise<string> {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'pkg-listing-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'notes' }))
    await writeFile(path.join(dir, '.mcp.json'), JSON.stringify(mcp))
    return dir
  }

  const okTest = (entries: Record<string, unknown>[]): PluginTest => {
    const stored = toStoredTools(entries)
    return { ok: true, detail: 'connected', duration_ms: 1, server: { name: 'api', version: '1' }, tools: [], truncated: false, stored }
  }

  async function listingRig(opts: { packages?: PackageView[]; test?: () => Promise<PluginTest>; mcp?: unknown } = {}) {
    const dir = await copy(opts.mcp ?? { mcpServers: { api: { type: 'http', url: 'https://api.example/mcp' }, db: { command: 'db-server' } } })
    const release = vi.fn()
    const installer = { acquire: vi.fn(async () => ({ dir, release, builtinTools: [] })) }
    const test = vi.fn(opts.test ?? (async () => okTest([{ name: 'fresh' }])))
    const base = rig({ ...(opts.packages ? { packages: opts.packages } : {}) })
    const listing = { packages: base.packages.repo, installer, forwarder: {} as PluginForwarder, listings: base.listings, resolve: async () => ['93.184.216.34'], test }
    const provider = packageProvider({
      packages: base.packages.repo,
      access: base.mem.access,
      listings: base.listings,
      verifiedReviewOf: () => Promise.reject(new Error('no copy')),
      refresh: (p, s) => listPackageServer(listing, p, s),
    })
    return { caps: new Capabilities([provider]), installer, release, test, listing, listings: base.listings }
  }

  it('refresh lists a server and stores its tools', async () => {
    const { caps, test, release, installer } = await listingRig()
    const { test: result, source } = await caps.refresh('package:notes/mcp:api')
    expect(result.ok).toBe(true)
    expect(source.listed_at).toBe('2026-10-29T12:00:00.000Z')
    expect(source.items.map((i) => [i.id, i.harness_name])).toEqual([['fresh', 'mcp__plugin_notes_api__fresh']])
    expect(installer.acquire).toHaveBeenCalledWith(expect.objectContaining({ name: 'notes', contentHash: 'sha256:notes' }))
    expect(test).toHaveBeenCalledWith(expect.objectContaining({ name: 'plugin_notes_api', url: 'https://api.example/mcp' }), '93.184.216.34', expect.anything())
    expect(release).toHaveBeenCalledTimes(1)
  })

  it('a failed refresh keeps the last listing', async () => {
    const failed: PluginTest = { ok: false, detail: 'the MCP endpoint answered HTTP 500: boom', duration_ms: 1, server: null, tools: [], truncated: false, stored: [] }
    const { caps } = await listingRig({ test: async () => failed })
    const { test, source } = await caps.refresh('package:notes/mcp:api')
    expect(test.ok).toBe(false)
    expect(source.listed_at).toBe('2026-10-20T00:00:00.000Z')
    expect(source.items.map((i) => i.id)).toEqual(['recall', 'a.b'])
  })

  it('answers 409 for an unapproved pin, 404 for an unknown server, 409 for a local one', async () => {
    const unapproved = await listingRig({ packages: [pkg('notes', { approved: false, approved_at: null, enabled: false })] })
    expect(await listPackageServer(unapproved.listing, 'notes', 'api')).toEqual({ status: 409, body: { detail: 'approve the pin before listing its servers' } })
    expect(unapproved.installer.acquire).not.toHaveBeenCalled()
    const { listing } = await listingRig({ packages: [pkg('notes', { allow_refused: true })] })
    expect((await listPackageServer(listing, 'nope', 'api')).status).toBe(404)
    expect(await listPackageServer(listing, 'notes', 'nope')).toEqual({ status: 404, body: { detail: 'plugin package notes has no MCP server "nope"' } })
    expect(await listPackageServer(listing, 'notes', 'db')).toEqual({
      status: 409,
      body: { detail: '"package:notes/mcp:db" is started only by a turn, so its tools are not listed here' },
    })
    expect(listing.test).not.toHaveBeenCalled()
  })

  it('answers 409 with the problem for a server a turn would not offer, and stores nothing', async () => {
    const other = pkg('notes_x', {}, { mcp_servers: [{ name: 'api', type: 'http', url: 'https://o.example/mcp' }] })
    const colliding = await listingRig({ packages: [pkg('notes.x'), other] })
    // `notes.x`'s copy says `notes`; the review is the package's own record.
    const answer = await listPackageServer(colliding.listing, 'notes.x', 'api')
    expect(answer).toEqual({
      status: 409,
      body: { detail: 'plugin package notes.x: MCP server "api" is named plugin_notes_x_api, as "api" of plugin package notes_x is, so none of them is offered' },
    })
    const blocked = await listingRig({ mcp: { mcpServers: { api: { type: 'http', url: 'https://api.example/mcp' } } } })
    const refused = await listPackageServer({ ...blocked.listing, resolve: async () => ['169.254.169.254'] }, 'notes', 'api')
    expect(refused.status).toBe(409)
    expect((refused.body as { detail: string }).detail).toMatch(/^plugin package notes: MCP server "api" is not offered: /)
    expect(blocked.test).not.toHaveBeenCalled()
  })

  it('answers 429 while a listing of the same server runs', async () => {
    let finish!: () => void
    const { listing } = await listingRig({ test: () => new Promise((resolve) => (finish = () => resolve(okTest([]))) as never) })
    const first = listPackageServer(listing, 'notes', 'api')
    await vi.waitFor(() => expect(listing.test).toHaveBeenCalled())
    expect((await listPackageServer(listing, 'notes', 'api')).status).toBe(429)
    finish()
    expect((await first).status).toBe(200)
  })

  it("a refusal reaches the tree as a CapabilityError with the listing's status", async () => {
    const { caps } = await listingRig({ mcp: { mcpServers: { other: { type: 'http', url: 'https://api.example/mcp' } } } })
    // The review names `api`; the loaded copy does not.
    await expect(caps.refresh('package:notes/mcp:api')).rejects.toMatchObject({ status: 404, message: 'plugin package notes has no MCP server "api"' })
    await expect(caps.refresh('package:notes/mcp:nope')).rejects.toBeInstanceOf(CapabilityError)
  })
})

describe('the packages a turn would not load (#4757)', () => {
  const pin = (name: string, contentHash = `sha256:${name}`): PackagePin => ({ name, fetchUrl: '', fetchPath: '', commit: 'c', contentHash })

  it('names the enabled pins refused at load, read again only when the pins change', async () => {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'pkg-unloadable-'))
    let pins = [pin('good'), pin('bad')]
    const acquire = vi.fn(async (p: PackagePin) => {
      if (p.name === 'bad') throw new PackageRefusedError(['nope'])
      return { dir, release: () => {}, builtinTools: [] }
    })
    const unloadable = unloadablePackages({ enabledPins: async () => pins }, { acquire })
    expect([...(await unloadable())]).toEqual(['bad'])
    expect([...(await unloadable())]).toEqual(['bad'])
    expect(acquire).toHaveBeenCalledTimes(2)
    pins = [pin('good')]
    expect([...(await unloadable())]).toEqual([])
    expect(acquire).toHaveBeenCalledTimes(3)
  })

  it('answers the last read while a slow one runs, and none before the first', async () => {
    let finish!: () => void
    const slow = new Promise<void>((resolve) => (finish = resolve))
    const acquire = vi.fn(async () => {
      await slow
      throw new PackageRefusedError(['nope'])
    })
    const unloadable = unloadablePackages({ enabledPins: async () => [pin('bad')] }, { acquire }, { waitMs: 10 })
    expect([...(await unloadable())]).toEqual([])
    finish()
    await vi.waitFor(async () => expect([...(await unloadable())]).toEqual(['bad']))
  })
})
