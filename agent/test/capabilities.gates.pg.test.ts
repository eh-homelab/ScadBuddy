import { randomBytes } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { toStoredTools, ToolListings } from '../src/access/listings.js'
import { AccessStore } from '../src/access/store.js'
import { createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { buildHarnessOptions } from '../src/harness/run.js'
import { originPolicy } from '../src/http/origins.js'
import { PluginForwarder } from '../src/plugins/forwarder.js'
import { PluginStore } from '../src/plugins/registry.js'
import { CAPABILITIES_PATH } from '../src/routes/capabilities.js'
import { kekFromBase64 } from '../src/secrets.js'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { packageServerSource, packageSource } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import type { LoadedPackage } from '../src/plugins/packages/install.js'
import { forwardPackageServers } from '../src/plugins/packages/mcp.js'
import { PackageStore } from '../src/plugins/packages/store.js'
import { vetPackage } from '../src/plugins/packages/vet.js'
import { type FakeMcp, startFakeMcp } from './support/fakeMcp.js'
import { turnRunnerForTest } from './support/turnRunner.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2171, #778 acceptance: a remote tool set off through /api/v1/ai/capabilities is what the
// forwarder enforces: gone from tools/list and refused on tools/call. A tier set there is
// what the classic seam reads.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

describe.skipIf(skip !== '')(`remote access set through the capabilities routes${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let forwarder: PluginForwarder
  let mcp: FakeMcp

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    forwarder = await PluginForwarder.start()
    mcp = await startFakeMcp({ extraTools: ['a', 'b'] })
  })
  afterEach(async () => {
    await mcp.close()
    await forwarder.close()
    await drop()
  })

  async function rig() {
    const store = new PluginStore(db.sql)
    const listings = new ToolListings(db.sql, db.locks)
    const { url } = await store.create({ name: 'hs', url: mcp.url }, kek)
    await listings.exclusive('hs', (put) => put(url, toStoredTools([{ name: 'a' }, { name: 'b' }]), {}))
    const app = createApp({
      access: new AccessStore(db.sql),
      database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
      backend: () => Promise.resolve(true),
      kek: { ok: true, kek },
      credentials: new MemoryCredentials(),
      plugins: store,
      listings,
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    const put = (path: string, body: unknown) =>
      app.request(`${CAPABILITIES_PATH}/${encodeURIComponent('remote:hs')}${path}`, { method: 'PUT', headers: UI, body: JSON.stringify(body) })
    expect((await put('', { enabled: true })).status).toBe(200)
    return { store, put }
  }

  it("a remote tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call", async () => {
    const { store, put } = await rig()
    const off = await put('/items/b', { access: 'off' })
    expect(off.status).toBe(200)

    const [plugin] = (await store.enabled(kek)).plugins
    const registration = forwarder.register(plugin!, '127.0.0.1')
    const client = new Client({ name: 't', version: '0' })
    try {
      await client.connect(new StreamableHTTPClientTransport(new URL(registration.url)))
      const listed = (await client.listTools()).tools.map((t) => t.name)
      expect(listed).toContain('a')
      expect(listed).not.toContain('b')
      await expect(client.callTool({ name: 'b', arguments: { text: 'x' } })).rejects.toThrow(/tool "b" is disabled/)
      await client.callTool({ name: 'a', arguments: { text: 'x' } })
      expect(mcp.calls).toEqual(['a:x'])
    } finally {
      await client.close()
      registration.release()
    }
  })

  it('a remote tool given read in Settings is allowed at the classic seam; one left at the default asks first', async () => {
    const { store, put } = await rig()
    expect((await put('/items/a', { access: 'read' })).status).toBe(200)
    const [plugin] = (await store.enabled(kek)).plugins
    const registration = forwarder.register(plugin!, '127.0.0.1')
    try {
      const options = buildHarnessOptions({
        paths: { stateDir: '/var/lib/scadbuddy-agent' },
        credential: { kind: 'anthropic_api_key', secret: 'sk-ant-api03-unit-test-key-000011112222' },
        prompt: 'hi',
        remotePlugins: [{ name: 'hs', url: registration.url, toolTiers: plugin!.toolTiers, disabledTools: plugin!.disabledTools }],
      })
      const ask = (name: string) => options.canUseTool!(name, {}, { signal: new AbortController().signal, toolUseID: 't', requestId: 'r' })
      expect(await ask('mcp__hs__a')).toMatchObject({ behavior: 'allow' })
      expect(await ask('mcp__hs__b')).toMatchObject({ behavior: 'deny', message: expect.stringMatching(/approval/) as unknown })
    } finally {
      registration.release()
    }
  })

  // #4757: a package's items and servers set through the same routes reach the gates.
  async function packageRig() {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'caps-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'notes' }))
    for (const skill of ['jot', 'wipe']) {
      await mkdir(path.join(dir, 'skills', skill), { recursive: true })
      await writeFile(path.join(dir, 'skills', skill, 'SKILL.md'), `---\nname: ${skill}\ndescription: ${skill}s\n---\n${skill}.\n`)
    }
    await writeFile(path.join(dir, '.mcp.json'), JSON.stringify({ mcpServers: { api: { type: 'http', url: mcp.url } } }))
    const tree = await hashTree(dir)
    const review = vetPackage(dir, 'notes').review!
    const packages = new PackageStore(db.sql)
    const commit = 'a'.repeat(40)
    await packages.create({ source: { kind: 'git', url: 'https://git.example/notes.git', ref: 'main', path: '' }, fetchUrl: 'https://git.example/notes.git', fetchPath: '', commit, contentHash: tree.hash, files: tree.files, review })
    await packages.approve('notes', commit, tree.hash)
    await packages.setEnabled('notes', true)
    const listings = new ToolListings(db.sql, db.locks)
    await listings.exclusiveServer('notes', 'api', (put) => put(tree.hash, toStoredTools([{ name: 'a' }, { name: 'b' }]), {}))
    const access = new AccessStore(db.sql)
    const app = createApp({
      access,
      database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
      backend: () => Promise.resolve(true),
      kek: { ok: true, kek },
      credentials: new MemoryCredentials(),
      pluginPackages: packages,
      listings,
      testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    const put = (source: string, rest: string, body: unknown) =>
      app.request(`${CAPABILITIES_PATH}/${encodeURIComponent(source)}${rest}`, { method: 'PUT', headers: UI, body: JSON.stringify(body) })
    const loaded: LoadedPackage = { name: 'notes', contentHash: tree.hash, dir, allowed: false, builtinTools: [] }
    const loopback = () => Promise.resolve(['127.0.0.1'])
    const forward = () => forwardPackageServers([loaded], forwarder, loopback, { access: new AccessResolver(access) })
    return { put, loaded, access, forward }
  }

  it("a package server tool set off in Settings is absent from the forwarder's tools/list and refused on tools/call", async () => {
    const { put, forward } = await packageRig()
    expect((await put(packageServerSource('notes', 'api'), '/items/b', { access: 'off' })).status).toBe(200)
    const servers = await forward()
    const [plugin] = servers.plugins
    expect(plugin?.name).toBe('plugin_notes_api')
    const client = new Client({ name: 't', version: '0' })
    try {
      await client.connect(new StreamableHTTPClientTransport(new URL(plugin!.url)))
      const listed = (await client.listTools()).tools.map((t) => t.name)
      expect(listed).toContain('a')
      expect(listed).not.toContain('b')
      await expect(client.callTool({ name: 'b', arguments: { text: 'x' } })).rejects.toThrow(/tool "b" is disabled/)
      await client.callTool({ name: 'a', arguments: { text: 'x' } })
      expect(mcp.calls).toEqual(['a:x'])
    } finally {
      await client.close()
      servers.release()
    }
  })

  it("a package skill set off in Settings is absent from the next turn's copy", async () => {
    const { put, loaded, access } = await packageRig()
    expect((await put(packageSource('notes'), '/items/skill%3Awipe', { access: 'off' })).status).toBe(200)
    const rig = await turnRunnerForTest({
      packages: { paths: [loaded.dir], allowedPaths: [], builtinTools: [], loaded: [loaded], problems: [] },
      reader: () => access,
    })
    const turn = await rig.runner.assemble(rig.session, rig.principal)
    turn.release()
    const copy = (turn.run.pluginPaths ?? []).find((d) => path.basename(d) === 'notes')
    expect(copy).toBeDefined()
    expect(existsSync(path.join(copy!, 'skills', 'wipe'))).toBe(false)
    expect(existsSync(path.join(copy!, 'skills', 'jot', 'SKILL.md'))).toBe(true)
  })

  it('a package server switched off in Settings is not forwarded', async () => {
    const { put, forward } = await packageRig()
    const on = await forward()
    expect(on.servers.map((s) => s.name)).toEqual(['plugin_notes_api'])
    on.release()
    expect((await put(packageServerSource('notes', 'api'), '', { enabled: false })).status).toBe(200)
    const off = await forward()
    expect(off.plugins).toEqual([])
    expect(off.servers).toEqual([])
  })
})
