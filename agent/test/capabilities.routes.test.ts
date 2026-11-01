import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import type { ItemDetails, SourceView } from '../src/access/capabilities.js'
import { listedTool, toStoredTools, type ListedTool, type PluginListingPut, type StoredTool } from '../src/access/listings.js'
import { type AccessRow, BUILTIN_SOURCES, registrySource, remoteSource, SOURCE_ITEM, type ToolAccess } from '../src/access/model.js'
import { pluginOverrides, sourceEnabled } from '../src/access/resolve.js'
import { createApp } from '../src/app.js'
import { originPolicy } from '../src/http/origins.js'
import type { PluginPatch, PluginRepo, PluginSummary } from '../src/plugins/registry.js'
import { PluginError } from '../src/plugins/registry.js'
import type { PluginTest } from '../src/plugins/testConnection.js'
import { CAPABILITIES_PATH } from '../src/routes/capabilities.js'
import { kekFromBase64 } from '../src/secrets.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { memoryAccess } from './support/memoryAccess.js'
import { memoryPackages, packageView } from './support/memoryPackages.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

// /api/v1/ai/capabilities (#2171, Task 5): the tree over the built-in sources and the
// remote plugins, read and set from Settings → Tools & plugins.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

const outwardTool = ALL_TOOLS.find((t) => t.risk === 'outward')!
const by = (c?: { actor: { kind: string; id: string } }): string => (c ? `${c.actor.kind}:${c.actor.id}` : 'system')

type Listing = { tools: ListedTool[]; listed_at: string; truncated: boolean }

/** A memory PluginRepo over ai_access-shaped rows, as PluginStore's. */
function memoryPlugins(names: readonly string[]) {
  const rows: AccessRow[] = []
  const summary = (name: string): PluginSummary => {
    const { toolTiers, disabledTools } = pluginOverrides(remoteSource(name), rows)
    return {
      name,
      kind: 'remote_mcp',
      url: `https://${name}.example/mcp`,
      enabled: sourceEnabled(remoteSource(name), rows),
      auth_header: null,
      secret_last4: null,
      tool_tiers: toolTiers,
      disabled_tools: disabledTools,
      created_at: '2026-10-10T00:00:00.000Z',
      updated_at: '2026-10-10T00:00:00.000Z',
      kekId: null,
    }
  }
  const put = (source: string, item: string, access: string | null, who: string): void => {
    const i = rows.findIndex((r) => r.source === source && r.item === item)
    if (i >= 0) rows.splice(i, 1)
    if (access !== null) rows.push({ source, item, access, updated_by: who })
  }
  const known = (name: string): void => {
    if (!names.includes(name)) throw new PluginError(`no plugin named "${name}"`, 404)
  }
  const reject = () => Promise.reject(new Error('not called'))
  const setToolAccess = vi.fn(async (name: string, tool: string, access: ToolAccess | null, context?: { actor: { kind: string; id: string } }) => {
    known(name)
    put(remoteSource(name), tool, access, by(context))
    return summary(name)
  })
  const repo: PluginRepo = {
    list: async () => names.map(summary),
    get: async (name) => (names.includes(name) ? summary(name) : undefined),
    create: reject,
    update: async (name: string, patch: PluginPatch, _kek, context) => {
      known(name)
      if (patch.enabled !== undefined) put(remoteSource(name), SOURCE_ITEM, patch.enabled ? 'on' : 'off', by(context))
      return summary(name)
    },
    setToolAccess,
    delete: reject,
    reveal: async (name) => (names.includes(name) ? { name, url: `https://${name}.example/mcp`, toolTiers: {}, disabledTools: [] } : undefined),
  }
  return { repo, rows, setToolAccess }
}

function memoryListings(initial: Record<string, Listing>) {
  const store = new Map(Object.entries(initial))
  const put = async (source: string, tools: readonly StoredTool[], options: { truncated?: boolean } = {}) => {
    store.set(source, { tools: tools.map(listedTool), listed_at: '2026-10-29T12:00:00.000Z', truncated: options.truncated ?? false })
  }
  return {
    get: async (source: string) => store.get(source),
    getMany: async (sources: readonly string[]) => new Map([...store].filter(([s]) => sources.includes(s))),
    put,
    // No other replica here: the lock is always free.
    exclusive: async <T>(name: string, fn: (put: PluginListingPut) => Promise<T>) => ({
      value: await fn(async (_url, tools, options) => {
        await put(remoteSource(name), tools, options)
        return true
      }),
    }),
  }
}

const listing = (entries: Record<string, unknown>[], truncated = false): Listing => ({
  tools: toStoredTools(entries).map(listedTool),
  listed_at: '2026-10-20T00:00:00.000Z',
  truncated,
})

const okTest = (stored: StoredTool[] = []): PluginTest => ({ ok: true, detail: 'connected', duration_ms: 1, server: { name: 'hs', version: '1' }, tools: [], truncated: false, stored })

function rig(opts: { access?: boolean; ready?: boolean; plugins?: boolean; packages?: boolean; testPlugin?: () => Promise<PluginTest> } = {}) {
  const mem = memoryAccess()
  const plugins = memoryPlugins(['hs', 'other'])
  const listings = memoryListings({
    'remote:hs': listing([{ name: 'files.list', description: 'Lists files' }, { name: 'a.b' }, { name: 'a_b' }], true),
  })
  const testPlugin = vi.fn(opts.testPlugin ?? (async () => okTest(toStoredTools([{ name: 'fresh' }]))))
  const app = createApp({
    ...(opts.access === false ? {} : { access: mem.access as never }),
    database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(opts.ready ?? true) },
    backend: () => Promise.resolve(true),
    kek: { ok: true, kek },
    credentials: new MemoryCredentials(),
    ...(opts.plugins === false ? {} : { plugins: plugins.repo }),
    ...(opts.packages ? { pluginPackages: memoryPackages([packageView('p', {}, { mcp_servers: [{ name: 's', type: 'http', url: 'https://s.example/mcp' }] })]).repo } : {}),
    listings: listings as never,
    testPlugin,
    testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    resolveHost: () => Promise.resolve(['203.0.113.10']),
  })
  return { app, ...mem, plugins, testPlugin }
}

type App = ReturnType<typeof rig>['app']
const enc = encodeURIComponent
const get = (app: App, path = '') => app.request(`${CAPABILITIES_PATH}${path}`, { headers: READ })
const put = (app: App, path: string, body: unknown, headers: Record<string, string> = UI) =>
  app.request(`${CAPABILITIES_PATH}${path}`, { method: 'PUT', headers, body: JSON.stringify(body) })
const del = (app: App, path: string) => app.request(`${CAPABILITIES_PATH}${path}`, { method: 'DELETE', headers: UI })
const refresh = (app: App, source: string) => app.request(`${CAPABILITIES_PATH}/${enc(source)}/refresh`, { method: 'POST', headers: UI })
const itemPath = (source: string, item: string) => `/${enc(source)}/items/${enc(item)}`

const OWN = BUILTIN_SOURCES.scadbuddy

describe('/api/v1/ai/capabilities', () => {
  it('lists the built-in sources and the remote plugins', async () => {
    const { app } = rig()
    const res = await get(app)
    expect(res.status).toBe(200)
    const { sources } = (await res.json()) as { sources: SourceView[] }
    expect(sources.map((s) => s.id)).toEqual([
      BUILTIN_SOURCES.scadbuddy,
      BUILTIN_SOURCES.browser,
      BUILTIN_SOURCES.http,
      BUILTIN_SOURCES.ownPlugin,
      BUILTIN_SOURCES.claudeCode,
      'remote:hs',
      'remote:other',
    ])
    const hs = sources.find((s) => s.id === 'remote:hs')!
    expect(hs.items.map((i) => i.id)).toEqual(['files.list', 'a.b', 'a_b'])
    // The stored listing's `truncated` reaches the source view.
    expect(hs.truncated).toBe(true)
    expect(sources.find((s) => s.id === 'remote:other')!.truncated).toBe(false)
    expect(sources[0]!.truncated).toBe(false)
  })

  it('lists packages after the remote plugins', async () => {
    const { app } = rig({ packages: true })
    const { sources } = (await (await get(app)).json()) as { sources: SourceView[] }
    expect(sources.map((s) => s.id).slice(-3)).toEqual(['remote:hs', 'remote:other', 'package:p'])
    const p = sources.at(-1)!
    expect(p).toMatchObject({ kind: 'package', title: 'p', enabled: true, switchable: true })
    expect(p.children.map((c) => [c.id, c.kind])).toEqual([['package:p/mcp:s', 'package_server']])
  })

  it('answers 503 without the database', async () => {
    const none = rig({ access: false }).app
    expect((await get(none)).status).toBe(503)
    expect(await (await get(none)).json()).toEqual({ detail: expect.any(String) as unknown })
    expect((await put(none, itemPath(OWN, outwardTool.name), { access: 'write' })).status).toBe(503)
    const down = rig({ ready: false }).app
    expect((await get(down, `/${enc(OWN)}`)).status).toBe(503)
    expect((await del(down, itemPath(OWN, outwardTool.name))).status).toBe(503)
    // The database first, as the plugin routes do: a bad body is no reason to say 400.
    expect((await put(none, itemPath(OWN, outwardTool.name), { access: 'none' })).status).toBe(503)
    expect((await put(down, `/${enc(BUILTIN_SOURCES.http)}`, { enabled: 'no' })).status).toBe(503)
  })

  it('answers the built-in sources without the plugin registry, and no remote source', async () => {
    const { app } = rig({ plugins: false })
    const res = await get(app)
    expect(res.status).toBe(200)
    const { sources } = (await res.json()) as { sources: SourceView[] }
    expect(sources.map((s) => s.id)).toEqual([
      BUILTIN_SOURCES.scadbuddy,
      BUILTIN_SOURCES.browser,
      BUILTIN_SOURCES.http,
      BUILTIN_SOURCES.ownPlugin,
      BUILTIN_SOURCES.claudeCode,
    ])
    const remote = await get(app, `/${enc('remote:hs')}`)
    expect(remote.status).toBe(404)
    expect(remote.headers.get('content-type')).toContain('application/json')
    expect(await remote.json()).toEqual({ detail: 'no source "remote:hs"' })
  })

  it('refuses a write from another origin (403) and a non-JSON body (403)', async () => {
    const { app, rows } = rig()
    const other = await put(app, itemPath(OWN, outwardTool.name), { access: 'write' }, { ...UI, origin: 'https://evil.example' })
    expect(other.status).toBe(403)
    const form = await put(app, itemPath(OWN, outwardTool.name), { access: 'write' }, { ...UI, 'content-type': 'text/plain' })
    expect(form.status).toBe(403)
    const refreshed = await app.request(`${CAPABILITIES_PATH}/${enc('remote:hs')}/refresh`, {
      method: 'POST',
      headers: { ...UI, origin: 'https://evil.example' },
    })
    expect(refreshed.status).toBe(403)
    const deleted = await app.request(`${CAPABILITIES_PATH}${itemPath(OWN, outwardTool.name)}`, { method: 'DELETE', headers: { ...UI, origin: 'https://evil.example' } })
    expect(deleted.status).toBe(403)
    expect(rows).toEqual([])
  })

  it('refuses a read across sites (403)', async () => {
    const { app } = rig()
    const res = await app.request(CAPABILITIES_PATH, { headers: { ...READ, 'sec-fetch-site': 'cross-site' } })
    expect(res.status).toBe(403)
    const one = await app.request(`${CAPABILITIES_PATH}${itemPath(OWN, outwardTool.name)}`, { headers: { ...READ, 'sec-fetch-site': 'cross-site' } })
    expect(one.status).toBe(403)
  })

  it('PUT an item lowers it and answers the source', async () => {
    const { app, rows } = rig()
    const res = await put(app, itemPath(OWN, outwardTool.name), { access: 'write' })
    expect(res.status).toBe(200)
    const source = (await res.json()) as SourceView
    expect(source.id).toBe(OWN)
    expect(source.items.find((i) => i.id === outwardTool.name)).toMatchObject({ default: 'outward', access: 'write', overridden: true })
    expect(rows).toContainEqual(expect.objectContaining({ source: registrySource(outwardTool.name), item: outwardTool.name, access: 'write' }))
    const details = await get(app, itemPath(OWN, outwardTool.name))
    expect(details.status).toBe(200)
    const body = (await details.json()) as ItemDetails
    expect(body.item).toMatchObject({ id: outwardTool.name, access: 'write' })
    expect(body.input_schema).toMatchObject({ type: 'object' })
  })

  it('DELETE resets it', async () => {
    const { app, rows } = rig()
    await put(app, itemPath(OWN, outwardTool.name), { access: 'read' })
    const res = await del(app, itemPath(OWN, outwardTool.name))
    expect(res.status).toBe(200)
    const source = (await res.json()) as SourceView
    expect(source.items.find((i) => i.id === outwardTool.name)).toMatchObject({ access: 'outward', overridden: false })
    expect(rows).toEqual([])
  })

  it('PUT a source switches it', async () => {
    const { app, rows } = rig()
    const res = await put(app, `/${enc(BUILTIN_SOURCES.http)}`, { enabled: false })
    expect(res.status).toBe(200)
    expect(((await res.json()) as SourceView).enabled).toBe(false)
    expect(rows).toContainEqual(expect.objectContaining({ source: BUILTIN_SOURCES.http, item: SOURCE_ITEM, access: 'off' }))
    const remote = await put(app, `/${enc('remote:hs')}`, { enabled: true })
    expect(remote.status).toBe(200)
    expect(((await remote.json()) as SourceView).enabled).toBe(true)
    // A source that cannot be switched, and a body that is not `{ enabled }`.
    expect((await put(app, `/${enc(OWN)}`, { enabled: false })).status).toBe(409)
    expect((await put(app, `/${enc(BUILTIN_SOURCES.http)}`, { enabled: 'no' })).status).toBe(400)
    expect((await put(app, `/${enc(BUILTIN_SOURCES.http)}`, { enabled: true, extra: 1 })).status).toBe(400)
  })

  it('answers 400 for an access the item does not take, 404 for an unknown source, 409 for a fixed item', async () => {
    const { app, rows } = rig()
    const on = await put(app, itemPath(OWN, outwardTool.name), { access: 'on' })
    expect(on.status).toBe(400)
    expect(((await on.json()) as { detail: string }).detail).toMatch(/takes off, read, write, outward/)
    expect((await put(app, itemPath(OWN, outwardTool.name), { access: 'none' })).status).toBe(400)
    const malformed = await app.request(`${CAPABILITIES_PATH}${itemPath(OWN, outwardTool.name)}`, { method: 'PUT', headers: UI, body: '{' })
    expect(malformed.status).toBe(400)
    const unknown = await put(app, itemPath('builtin:nope', 'x'), { access: 'read' })
    expect(unknown.status).toBe(404)
    expect(await unknown.json()).toEqual({ detail: 'no source "builtin:nope"' })
    expect((await put(app, itemPath(OWN, 'no_such_tool'), { access: 'read' })).status).toBe(404)
    expect((await get(app, itemPath(OWN, 'no_such_tool'))).status).toBe(404)
    // `a.b` and `a_b` are one name to Claude Code: both are fixed off.
    const fixed = await put(app, itemPath('remote:hs', 'a_b'), { access: 'read' })
    expect(fixed.status).toBe(409)
    expect(((await fixed.json()) as { detail: string }).detail).toMatch(/Shares its name with/)
    expect((await del(app, itemPath('remote:hs', 'a.b'))).status).toBe(409)
    expect(rows).toEqual([])
  })

  it('routes an encoded source and item as one segment each', async () => {
    const { app, plugins } = rig()
    const res = await app.request(`${CAPABILITIES_PATH}/remote%3Ahs/items/files.list`, { method: 'PUT', headers: UI, body: JSON.stringify({ access: 'off' }) })
    expect(res.status).toBe(200)
    expect(plugins.setToolAccess).toHaveBeenCalledWith('hs', 'files.list', 'off', expect.anything())
    const source = (await res.json()) as SourceView
    expect(source.id).toBe('remote:hs')
    expect(source.items.find((i) => i.id === 'files.list')).toMatchObject({ access: 'off', overridden: true })
    const slashed = await get(app, `/${enc('package:p/mcp:s')}`)
    expect(slashed.status).toBe(404)
    expect(slashed.headers.get('content-type')).toContain('application/json')
    expect(await slashed.json()).toEqual({ detail: 'no source "package:p/mcp:s"' })
    const item = await get(app, `/${enc('remote:hs')}/items/${enc('a/b')}`)
    expect(item.status).toBe(404)
    expect(await item.json()).toEqual({ detail: 'no item "a/b" in "remote:hs"' })
  })

  it('routes package%3Ap%2Fmcp%3As to its server source', async () => {
    const { app, rows } = rig({ packages: true })
    const res = await app.request(`${CAPABILITIES_PATH}/package%3Ap%2Fmcp%3As`, { headers: READ })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ id: 'package:p/mcp:s', kind: 'package_server', title: 's', enabled: true, listed_at: null, items: [] })
    const off = await app.request(`${CAPABILITIES_PATH}/package%3Ap%2Fmcp%3As`, { method: 'PUT', headers: UI, body: JSON.stringify({ enabled: false }) })
    expect(off.status).toBe(200)
    expect(rows).toEqual([expect.objectContaining({ source: 'package:p/mcp:s', item: SOURCE_ITEM, access: 'off' })])
    const item = await app.request(`${CAPABILITIES_PATH}/package%3Ap%2Fmcp%3As/items/recall`, { method: 'PUT', headers: UI, body: JSON.stringify({ access: 'read' }) })
    // No listing names `recall`: an item the source does not have.
    expect(item.status).toBe(404)
    expect((await get(app, `/${enc('package:p/mcp:nope')}`)).status).toBe(404)
    // Without the cache and forwarder, a server cannot be listed here.
    const refreshed = await refresh(app, 'package:p/mcp:s')
    expect(refreshed.status).toBe(502)
  })

  it('refresh answers the test and the source, and a refresh while a connection test runs answers 429', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    const blocking = async () => {
      await gate
      return okTest(toStoredTools([{ name: 'fresh' }]))
    }
    const { app, testPlugin } = rig({ testPlugin: blocking })
    // The plugins route's connection test holds `hs`.
    const running = app.request('/api/v1/ai/plugins/hs/test', { method: 'POST', headers: UI })
    await vi.waitFor(() => expect(testPlugin).toHaveBeenCalledTimes(1))
    const busy = await refresh(app, 'remote:hs')
    expect(busy.status).toBe(429)
    // A running test ends within its 10 s timeout, and its listing write within 2 s more (#4994).
    expect(busy.headers.get('Retry-After')).toBe('12')
    expect(await busy.json()).toEqual({ detail: expect.stringMatching(/already running/) as unknown })
    release()
    expect((await running).status).toBe(200)

    const res = await refresh(app, 'remote:hs')
    expect(res.status).toBe(200)
    const body = (await res.json()) as { test: PluginTest; source: SourceView }
    expect(body.test).toMatchObject({ ok: true, detail: 'connected' })
    expect(body.test).not.toHaveProperty('stored')
    // The listing is the source's items; the test's own copy is not sent twice (final review M3).
    expect(body.test).not.toHaveProperty('tools')
    expect(body.source.id).toBe('remote:hs')
    expect(body.source.listed_at).toBe('2026-10-29T12:00:00.000Z')
    expect(body.source.truncated).toBe(false)
    expect(body.source.items.map((i) => [i.id, i.missing])).toEqual([['fresh', false]])
  })

  it('a running refresh refuses a second refresh and a connection test of the same plugin (429), but not another plugin', async () => {
    let release!: () => void
    const gate = new Promise<void>((r) => (release = r))
    const { app, testPlugin } = rig({
      testPlugin: async () => {
        if (testPlugin.mock.calls.length === 1) await gate
        return okTest()
      },
    })
    const first = refresh(app, 'remote:hs')
    await vi.waitFor(() => expect(testPlugin).toHaveBeenCalledTimes(1))
    expect((await refresh(app, 'remote:hs')).status).toBe(429)
    // The other direction of the shared set: a running refresh holds the plugins route's test too.
    expect((await app.request('/api/v1/ai/plugins/hs/test', { method: 'POST', headers: UI })).status).toBe(429)
    expect((await refresh(app, 'remote:other')).status).toBe(200)
    release()
    expect((await first).status).toBe(200)
  })

  it('answers a refresh of a built-in source 409, and of an unknown plugin 404', async () => {
    const { app } = rig()
    expect((await refresh(app, BUILTIN_SOURCES.http)).status).toBe(409)
    expect((await refresh(app, 'remote:nope')).status).toBe(404)
  })

  it('records a built-in write as the UI user', async () => {
    const { app, rows } = rig()
    await put(app, itemPath(OWN, outwardTool.name), { access: 'write' })
    const row = rows.find((r) => r.item === outwardTool.name)!
    expect(row.updated_by).toBe('browser:browser')
  })
})
