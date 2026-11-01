import { mkdir, mkdtemp, readdir, symlink, utimes, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { PluginForwarder } from '../src/plugins/forwarder.js'
import { hashTree } from '../src/plugins/packages/hash.js'
import { type LoadedPackage, loadPackagesForRun, type PackagesForRun } from '../src/plugins/packages/install.js'
import {
  collidingServerNames,
  forwardPackageServers,
  packageHeaderSecrets,
  type PackageServers,
  serverAccess,
} from '../src/plugins/packages/mcp.js'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { MockActivityEnvironment } from '@temporalio/testing'
import { z } from 'zod'
import { packageServerSource, packageSource, type AccessRow } from '../src/access/model.js'
import { AccessResolver } from '../src/access/resolver.js'
import { memoryAccess } from './support/memoryAccess.js'
import { buildHarnessOptions } from '../src/harness/run.js'
import { PluginRefusedError } from '../src/harness/plugins.js'
import { localPackageServers, PACKAGE_COPY_KEPT_MS, sweepPackageCopies } from '../src/temporal/packageServers.js'
import { turnServers } from '../src/temporal/turnServers.js'
import { builtinsOffOf, derivedPackage, NOTHING_OMITTED } from '../src/plugins/packages/derived.js'
import { LISTING_ITEMS_BUDGET, LISTING_PACKAGES_BUDGET, TEMPORAL_PAYLOAD_LIMIT, vetPackage } from '../src/plugins/packages/vet.js'
import { ALIAS_KEY_CAP } from '../src/access/items.js'
import {
  connectHttp,
  connectInProcess,
  listTurnTools,
  TURN_TOOL_TIER_ACTIVITY,
  turnToolActivities,
  turnToolActivity,
  type TurnServer,
} from '../src/temporal/turnTools.js'
import { startFakeMcp } from './support/fakeMcp.js'

// A plugin package's MCP servers as turn tools (#2320): checked rather than trusted, and their
// http MCP servers reached only through the loopback forwarder, as a remote plugin's are
// (security review of #2324's sibling); and the sweep of package copies (#2395).

async function fakePackage(mcp: unknown): Promise<LoadedPackage> {
  const dir = await mkdtemp(path.join(os.tmpdir(), 'turn-pkg-'))
  await mkdir(path.join(dir, '.claude-plugin'))
  await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name: 'notes' }))
  await mkdir(path.join(dir, 'skills', 'jot'), { recursive: true })
  await writeFile(path.join(dir, 'skills', 'jot', 'SKILL.md'), '---\nname: jot\ndescription: jots\n---\nJot it down.\n')
  if (mcp) await writeFile(path.join(dir, '.mcp.json'), JSON.stringify(mcp))
  return { name: 'notes', contentHash: (await hashTree(dir)).hash, dir, allowed: false, builtinTools: [] }
}

const OWNER = { kind: 'browser' as const, id: 'browser', label: 'You' }
const HEADER = 'Bearer pkg-header-secret-0123456789'
const toLoopback = async () => ['127.0.0.1']

let forwarder: PluginForwarder
beforeAll(async () => {
  forwarder = await PluginForwarder.start()
})
afterAll(async () => {
  await forwarder.close()
})

describe("a plugin package's MCP servers as turn tools", () => {
  it('sweeps copies no turn used for a week, and half-made ones; a used one is kept (#2395)', async () => {
    const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: 'https://mcp.example.test/' } } })
    // What durable sessions left on the payload volume: swept until it is empty.
    const root = await mkdtemp(path.join(os.tmpdir(), 'turn-share-'))
    const week = Date.now() + PACKAGE_COPY_KEPT_MS + 60_000
    const old = new Date(Date.now() - PACKAGE_COPY_KEPT_MS - 60_000)
    const kept = 'a'.repeat(64)
    const stale = 'f'.repeat(64)
    for (const name of [kept, stale, '.tmp-half', 'lost+found']) await mkdir(path.join(root, name))
    for (const name of [stale, '.tmp-half']) await utimes(path.join(root, name), old, old)
    expect((await sweepPackageCopies(root)).sort()).toEqual(['.tmp-half', stale].sort())
    expect((await readdir(root)).sort()).toEqual([kept, 'lost+found'].sort())
    // The stripped copy under the pod's own folder is kept the same way, and goes once unused.
    const local = await mkdtemp(path.join(os.tmpdir(), 'turn-local-'))
    await derivedPackage(pkg.dir, pkg.contentHash, local, NOTHING_OMITTED, pkg.name)
    const [localCopy] = await readdir(local)
    expect(await sweepPackageCopies(local)).toEqual([])
    expect(await sweepPackageCopies(local, week)).toEqual([localCopy])
    expect(await sweepPackageCopies(path.join(root, 'missing'))).toEqual([])
    // A link on the shared volume is never followed or removed, nor what it points at.
    const outside = await mkdtemp(path.join(os.tmpdir(), 'turn-outside-'))
    await writeFile(path.join(outside, 'keep'), 'x')
    await symlink(outside, path.join(root, 'e'.repeat(64)))
    expect(await sweepPackageCopies(root, week)).toEqual([kept])
    expect(await readdir(outside)).toEqual(['keep'])
  })

  it("forwards a package's http server, never handing out its URL or header, and redacts both", async () => {
    const mcp = await startFakeMcp({})
    try {
      const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: mcp.url, headers: { Authorization: HEADER } } } })
      const servers = await forwardPackageServers([pkg], forwarder, toLoopback)
      expect(servers.problems).toEqual([])
      const [plugin] = servers.plugins
      expect(plugin?.name).toBe('plugin_notes_api')
      expect(plugin?.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/p\//)
      expect(plugin?.url).not.toContain(new URL(mcp.url).port)
      expect(servers.secrets).toEqual(expect.arrayContaining([HEADER, plugin!.url]))
      const client = await connectHttp(plugin!.url)
      expect((await client.listTools()).tools.length).toBeGreaterThan(0)
      await client.close()
      // The forwarder added the header; the package's server got it, Claude Code never did.
      expect(mcp.requests.some((r) => r.headers.authorization === HEADER)).toBe(true)
      servers.release()
    } finally {
      await mcp.close()
    }
  })

  it("says a package http server's header value and its bare token with no route or address check, as a ToolCall redacts them", async () => {
    const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: 'http://169.254.169.254/', headers: { Authorization: HEADER } } } })
    expect(packageHeaderSecrets([pkg])).toEqual(expect.arrayContaining([HEADER, HEADER.slice('Bearer '.length)]))
    expect(packageHeaderSecrets([await fakePackage({ mcpServers: { local: { command: 'node', headers: { A: 'x-local-only' } } } })])).toEqual([])
  })

  it('refuses a metadata or link-local address before anything is registered', async () => {
    for (const url of ['http://169.254.169.254/latest/meta-data', 'http://metadata.google.internal/mcp', 'https://notes.example/mcp']) {
      const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url } } })
      const resolve = async () => ['169.254.169.254']
      const servers = await forwardPackageServers([pkg], forwarder, resolve)
      expect(servers.plugins).toEqual([])
      expect(servers.problems.join('\n')).toMatch(/metadata|link-local/)
    }
  })

  it('follows no redirect: a 307 fails, and its target is never contacted', async () => {
    const other = await startFakeMcp({ path: '/never' })
    const mcp = await startFakeMcp({
      intercept: (_req, res) => {
        res.writeHead(307, { location: other.url }).end()
        return true
      },
    })
    try {
      const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: mcp.url } } })
      const servers = await forwardPackageServers([pkg], forwarder, toLoopback)
      await expect(connectHttp(servers.plugins[0]!.url)).rejects.toThrow()
      expect(mcp.requests.length).toBeGreaterThan(0)
      expect(other.requests).toEqual([])
      servers.release()
    } finally {
      await mcp.close()
      await other.close()
    }
  })

  it('starts a local server only for a package approved with allow_refused', async () => {
    const pkg = await fakePackage({ mcpServers: { local: { command: 'node', args: ['${CLAUDE_PLUGIN_ROOT}/s.mjs'] } } })
    const problems: string[] = []
    expect(localPackageServers([pkg], problems, await serverAccess([pkg]))).toEqual([])
    const allowed = localPackageServers([{ ...pkg, allowed: true }], problems, await serverAccess([{ ...pkg, allowed: true }]))
    expect(allowed.map((s) => s.name)).toEqual(['plugin_notes_local'])
    expect(allowed[0]!.tierOf('mcp__plugin_notes_local__ping', {})).toBe('outward')
    expect(problems).toEqual([])
  })

  it("forwards a package's http server for a call, and puts its URL and token in secrets (#2620)", async () => {
    const pkg = { ...(await fakePackage({ mcpServers: { api: { type: 'http', url: 'https://notes.example/mcp' } } })), allowed: true }
    const load = async (): Promise<PackagesForRun> => ({ paths: [], allowedPaths: [pkg.dir], builtinTools: [], loaded: [pkg], problems: [], release: () => {} })
    const URL = 'http://127.0.0.1:9/forward/notes-token-0123456789'
    const handed: string[][] = []
    const forward = async (loaded: readonly LoadedPackage[]): Promise<PackageServers> => {
      handed.push(loaded.map((p) => p.name))
      const plugins = loaded.map((p) => ({ name: `plugin_${p.name}_api`, url: URL, toolTiers: {}, disabledTools: [] }))
      // As forwardPackageServers does: the header values and the forwarder URLs are its secrets.
      return { plugins, servers: loaded.map((p) => ({ name: `plugin_${p.name}_api`, pkg: p.name, server: 'api' })), problems: [], secrets: plugins.flatMap((p) => [p.url, 'notes-token-0123456789']), terminate: async () => {}, release: () => {} }
    }
    const call = await turnServers({ packages: { load, forward } })({ session: 's', owner: OWNER, signal: new AbortController().signal })
    expect(handed).toEqual([['notes']])
    expect(call.servers.map((x) => x.name)).toContain('plugin_notes_api')
    expect(call.secrets).toEqual(expect.arrayContaining([URL, 'notes-token-0123456789']))
    await call.release()
  })

  it("refuses a package header or URL a remote plugin's registration would refuse", async () => {
    const bad: [string, Record<string, unknown>][] = [
      ['crlf value', { type: 'http', url: 'https://notes.example/mcp', headers: { Authorization: 'Bearer a\r\nX-Evil: 1' } }],
      ['host', { type: 'http', url: 'https://notes.example/mcp', headers: { Host: 'other.example' } }],
      ['cookie', { type: 'http', url: 'https://notes.example/mcp', headers: { Cookie: 'a=b' } }],
      ['forwarded', { type: 'http', url: 'https://notes.example/mcp', headers: { 'X-Forwarded-For': '10.0.0.1' } }],
      ['proxy', { type: 'http', url: 'https://notes.example/mcp', headers: { 'Proxy-Authorization': 'x' } }],
      ['bad name', { type: 'http', url: 'https://notes.example/mcp', headers: { 'Bad Name': 'x' } }],
      ['two headers', { type: 'http', url: 'https://notes.example/mcp', headers: { A: '1', B: '2' } }],
      ['credentials in url', { type: 'http', url: 'https://u:p@notes.example/mcp' }],
      ['query', { type: 'http', url: 'https://notes.example/mcp?token=x' }],
    ]
    for (const [what, config] of bad) {
      const servers = await forwardPackageServers([await fakePackage({ mcpServers: { api: config } })], forwarder, async () => ['203.0.113.7'])
      expect(servers.plugins, what).toEqual([])
      expect(servers.problems, what).toHaveLength(1)
    }
  })

  it('never lets a classic query start a plugin http server itself, and hands Claude Code forwarder URLs only', async () => {
    const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: 'http://169.254.169.254/' } } })
    const paths = { stateDir: await mkdtemp(path.join(os.tmpdir(), 'turn-run-')) }
    const base = { paths, credential: { kind: 'anthropic_api_key' as const, secret: 'sk-ant-test-0000' }, prompt: 'hi' }
    // strictMcpConfig is off with an allowed package: the original would be started by Claude Code.
    expect(() => buildHarnessOptions({ ...base, allowedPluginPaths: [pkg.dir] })).toThrow(PluginRefusedError)
    const { dir: copy } = await derivedPackage(pkg.dir, pkg.contentHash, path.join(paths.stateDir, 'plugins-local'), NOTHING_OMITTED, pkg.name)
    const servers = await forwardPackageServers([pkg], forwarder, async () => ['203.0.113.7'])
    expect(servers.problems.join('\n')).toMatch(/metadata|link-local/)
    const ok = await forwardPackageServers(
      [await fakePackage({ mcpServers: { api: { type: 'http', url: 'https://notes.example/mcp' } } })],
      forwarder,
      async () => ['203.0.113.7'],
    )
    const options = buildHarnessOptions({ ...base, allowedPluginPaths: [copy], remotePlugins: ok.plugins })
    // The one URL in its env is the credential gateway's (gateway/server.ts); every other is the forwarder's.
    const { env, ...rest } = options
    expect(env?.ANTHROPIC_BASE_URL).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/t\//)
    expect(JSON.stringify({ ...env, ANTHROPIC_BASE_URL: undefined })).not.toMatch(/https?:\/\//)
    const urls = JSON.stringify(rest).match(/https?:\/\/[^"\\]+/g) ?? []
    expect(urls.length).toBeGreaterThan(0)
    for (const url of urls) expect(url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/p\//)
    ok.release()
  })
})

// #4757: a package's MCP servers take their `*` switch at turn start, and servers whose
// names Claude Code would make equal are none of them offered, in both runtimes.
describe("a package's MCP servers: their switch and their names (#4757)", () => {
  const SESSION = '0b6c1e4e-7d3a-4f5e-9a51-3f1c2d4e5f60'
  const off = (pkg: string, server: string): AccessRow => ({ source: packageServerSource(pkg, server), item: '*', access: 'off', updated_by: 'test' })

  async function listedNames(pkg: LoadedPackage, rows: AccessRow[]): Promise<string[]> {
    const { access } = memoryAccess(rows)
    const resolver = new AccessResolver(access)
    const load = async (): Promise<PackagesForRun> => ({ paths: [pkg.dir], allowedPaths: [], builtinTools: [], loaded: [pkg], problems: [], release: () => {} })
    const servers = turnServers({
      access: resolver,
      packages: { load, forward: (loaded) => forwardPackageServers(loaded, forwarder, toLoopback, { access: resolver }) },
    })
    const { tools } = await listTurnTools(
      { sessions: { ownerOf: async (id) => (id === SESSION ? (OWNER as never) : undefined) }, servers },
      { session: SESSION, owner: OWNER, signal: new AbortController().signal },
    )
    return tools.map((t) => t.name)
  }

  it('a package server switched off is not forwarded and its tools are not listed', async () => {
    const mcp = await startFakeMcp({})
    try {
      const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: mcp.url } } })
      const { access } = memoryAccess([off('notes', 'api')])
      const servers = await forwardPackageServers([pkg], forwarder, toLoopback, { access: new AccessResolver(access) })
      expect(servers.plugins).toEqual([])
      expect(servers.servers).toEqual([])
      expect(servers.problems).toEqual([])
      expect(await listedNames(pkg, [off('notes', 'api')])).toEqual([])
      expect(mcp.requests).toEqual([])
    } finally {
      await mcp.close()
    }
  })

  it('a package server is forwarded with no row (on by default)', async () => {
    const mcp = await startFakeMcp({})
    try {
      const pkg = await fakePackage({ mcpServers: { api: { type: 'http', url: mcp.url } } })
      const { access } = memoryAccess()
      const servers = await forwardPackageServers([pkg], forwarder, toLoopback, { access: new AccessResolver(access) })
      expect(servers.plugins.map((p) => p.name)).toEqual(['plugin_notes_api'])
      servers.release()
      expect(await listedNames(pkg, [])).toContain('plugin_notes_api__recall')
    } finally {
      await mcp.close()
    }
  })

  it('servers named alike through harnessToolName are none of them offered, each said', async () => {
    const http = { mcpServers: { s: { type: 'http', url: 'https://notes.example/mcp' } } }
    const dotted = { ...(await fakePackage(http)), name: 'a.b' }
    const plain = { ...(await fakePackage(http)), name: 'a_b' }
    expect([...collidingServerNames([dotted, plain]).entries()]).toEqual([
      [
        'plugin_a_b_s',
        [
          { pkg: 'a.b', server: 's' },
          { pkg: 'a_b', server: 's' },
        ],
      ],
    ])
    const servers = await forwardPackageServers([dotted, plain], forwarder, toLoopback)
    expect(servers.plugins).toEqual([])
    expect(servers.problems).toEqual([
      'plugin package a.b: MCP server "s" is named plugin_a_b_s, as "s" of plugin package a_b is, so none of them is offered',
      'plugin package a_b: MCP server "s" is named plugin_a_b_s, as "s" of plugin package a.b is, so none of them is offered',
    ])
    // With several others, they are named together.
    const third = { ...(await fakePackage(http)), name: 'a+b' }
    expect((await forwardPackageServers([dotted, plain, third], forwarder, toLoopback)).problems[0]).toBe(
      'plugin package a.b: MCP server "s" is named plugin_a_b_s, as "s" of plugin package a_b, "s" of plugin package a+b are, so none of them is offered',
    )
    // One package alone is offered.
    const alone = await forwardPackageServers([plain], forwarder, toLoopback)
    expect(alone.plugins.map((p) => p.name)).toEqual(['plugin_a_b_s'])
    alone.release()
  })

  it("a local server named like another package's is not started", async () => {
    const local = { ...(await fakePackage({ mcpServers: { s: { command: 'node', args: ['s.mjs'] } } })), name: 'a.b', allowed: true }
    const http = { ...(await fakePackage({ mcpServers: { s: { type: 'http', url: 'https://notes.example/mcp' } } })), name: 'a_b' }
    const problems: string[] = []
    expect(localPackageServers([local, http], problems, await serverAccess([local, http]))).toEqual([])
    expect(problems).toEqual(['plugin package a.b: MCP server "s" is named plugin_a_b_s, as "s" of plugin package a_b is, so none of them is offered'])
    const forwarded = await forwardPackageServers([local, http], forwarder, toLoopback)
    expect(forwarded.plugins).toEqual([])
    expect(forwarded.problems).toEqual(['plugin package a_b: MCP server "s" is named plugin_a_b_s, as "s" of plugin package a.b is, so none of them is offered'])
  })

  it('a local server switched off is not started: it is known, its calls refused', async () => {
    const pkg = { ...(await fakePackage({ mcpServers: { local: { command: 'node', args: ['s.mjs'] } } })), allowed: true }
    const { access } = memoryAccess([off('notes', 'local')])
    const problems: string[] = []
    const switches = await serverAccess([pkg], new AccessResolver(access))
    expect(switches.get(packageServerSource('notes', 'local'))).toEqual({ enabled: false, toolTiers: {}, disabledTools: [] })
    const [known, ...rest] = localPackageServers([pkg], problems, switches)
    expect(rest).toEqual([])
    expect(known).toMatchObject({ name: 'plugin_notes_local', unlisted: true })
    await expect(known!.connect()).rejects.toThrow(/switched off/)
    expect(await known!.refusal!('mcp__plugin_notes_local__look')).toBe(
      'mcp__plugin_notes_local__look is disabled in Settings → Tools & plugins; the user can turn it back on there',
    )
    expect(problems).toEqual([])
    // On by default.
    expect(localPackageServers([pkg], problems, await serverAccess([pkg], new AccessResolver(memoryAccess().access))).map((s) => s.name)).toEqual([
      'plugin_notes_local',
    ])
  })

  it("a local server's tool takes its stored tier, and an off one is neither listed nor run", async () => {
    const pkg = { ...(await fakePackage({ mcpServers: { local: { command: 'node', args: ['s.mjs'] } } })), allowed: true }
    const source = packageServerSource('notes', 'local')
    const { access } = memoryAccess([
      { source, item: 'look', access: 'read', updated_by: 'test' },
      { source, item: 'wipe', access: 'off', updated_by: 'test' },
    ])
    const [server] = localPackageServers([pkg], [], await serverAccess([pkg], new AccessResolver(access)))
    expect(server!.tierOf('mcp__plugin_notes_local__look', {})).toBe('read')
    expect(server!.tierOf('mcp__plugin_notes_local__other', {})).toBe('outward')
    expect([...(server!.offTools ?? [])]).toEqual(['wipe'])
    // The same server over an in-process MCP server with those tools, as the turn lists and calls it.
    const calls: string[] = []
    const mcp = () => {
      const s = new McpServer({ name: 'local', version: '1' })
      for (const name of ['look', 'wipe']) {
        s.registerTool(name, { description: name, inputSchema: { x: z.string().optional() } }, async () => {
          calls.push(name)
          return { content: [{ type: 'text', text: name }] }
        })
      }
      return s
    }
    const inProcess: TurnServer = { ...server!, connect: () => connectInProcess(mcp) }
    const deps = {
      sessions: { ownerOf: async (id: string) => (id === SESSION ? (OWNER as never) : undefined) },
      servers: async () => ({ servers: [inProcess], problems: [], secrets: [], release: () => {} }),
      approvals: { approved: async () => true },
    }
    const env = (type: string) => new MockActivityEnvironment({ activityType: type, activityId: 'tool-t1', workflowExecution: { workflowId: `session-${SESSION}`, runId: 'r' } })
    const activities = turnToolActivities(deps)
    const { tools } = await listTurnTools(deps, { session: SESSION, owner: OWNER, signal: new AbortController().signal })
    expect(tools.map((t) => [t.name, t.tier])).toEqual([['plugin_notes_local__look', 'read']])
    expect(await env(TURN_TOOL_TIER_ACTIVITY).run(activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'plugin_notes_local__look', input: {} })).toEqual({
      tier: 'read',
      declared: 'read',
    })
    expect(await env(TURN_TOOL_TIER_ACTIVITY).run(activities[TURN_TOOL_TIER_ACTIVITY]!, { tool: 'plugin_notes_local__wipe', input: {} })).toMatchObject({
      tier: 'write',
      refused: 'mcp__plugin_notes_local__wipe is disabled in Settings → Tools & plugins; the user can turn it back on there',
    })
    await expect(env('turn:plugin_notes_local__wipe').run(turnToolActivity(deps), {})).rejects.toThrow(
      'mcp__plugin_notes_local__wipe is disabled in Settings → Tools & plugins; the user can turn it back on there',
    )
    expect(await env('turn:plugin_notes_local__look').run(turnToolActivity(deps), {})).toBe('look')
    expect(calls).toEqual(['look'])
  })
})

describe("a package's items (#4757)", () => {
  const BY = 'browser:b'
  const row = (source: string, item: string, access: string): AccessRow => ({ source, item, access, updated_by: BY })

  /** A second allowed package that names `builtins`. */
  async function namer(name: string, builtins: string[]): Promise<LoadedPackage> {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'turn-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name }))
    await mkdir(path.join(dir, 'skills', 'go'), { recursive: true })
    await writeFile(path.join(dir, 'skills', 'go', 'SKILL.md'), `---\nname: go\ndescription: goes\nallowed-tools: ${builtins.join(', ')}\n---\nGo.\n`)
    return { name, contentHash: (await hashTree(dir)).hash, dir, allowed: true, builtinTools: builtins }
  }

  /** A package `name` with `count` subagents, each with a frontmatter name of `width` copies of `unit`. */
  async function bulky(name: string, count: number, unit: string, width: number): Promise<LoadedPackage> {
    const dir = await mkdtemp(path.join(os.tmpdir(), 'turn-pkg-'))
    await mkdir(path.join(dir, '.claude-plugin'))
    await writeFile(path.join(dir, '.claude-plugin', 'plugin.json'), JSON.stringify({ name }))
    await mkdir(path.join(dir, 'agents'), { recursive: true })
    for (let i = 0; i < count; i++) await writeFile(path.join(dir, 'agents', `a${i}.md`), `---\nname: ${i}-${unit.repeat(width)}\ndescription: d\n---\nA.\n`)
    return { name, contentHash: (await hashTree(dir)).hash, dir, allowed: false, builtinTools: [] }
  }

  it('refuses a skill or subagent name over ALIAS_KEY_CAP at vet, past any approval (Task 6 re-review R7)', async () => {
    const named = async (file: string, name: string) => {
      const pkg = await fakePackage(undefined)
      await mkdir(path.dirname(path.join(pkg.dir, file)), { recursive: true })
      await writeFile(path.join(pkg.dir, file), `---\nname: ${name}\ndescription: d\n---\nD.\n`)
      return pkg
    }
    // A name at the bound passes, so the call rule compares it whole.
    const atCap = `${'deep_'.repeat(204)}deep`
    expect(atCap.length).toBe(ALIAS_KEY_CAP)
    expect(vetPackage((await named('agents/deep.md', atCap)).dir).fatal).toEqual([])
    // The re-review's 1,100-unit name, and a skill's one unit over: fatal (no allow_refused passes it).
    for (const [file, name] of [
      ['agents/deep.md', 'deep_'.repeat(220)],
      ['skills/jot/SKILL.md', 'j'.repeat(ALIAS_KEY_CAP + 1)],
    ] as const) {
      expect(vetPackage((await named(file, name)).dir).fatal).toContainEqual(expect.stringContaining(`longer than ${ALIAS_KEY_CAP}`))
    }
  })

  it("refuses a package whose names take more than its budget of a listing in UTF-8 bytes, at vet (Task 6 re-review R1, R9)", async () => {
    // The per-package budget leaves room for four packages in the listing's, and that is half Temporal's payload limit.
    expect(LISTING_ITEMS_BUDGET * 4).toBeLessThanOrEqual(LISTING_PACKAGES_BUDGET)
    expect(LISTING_PACKAGES_BUDGET * 2).toBeLessThanOrEqual(TEMPORAL_PAYLOAD_LIMIT)
    // 100 names of 1,000 CJK characters: about 100 K UTF-16 units, but about 300 KB of UTF-8.
    expect(vetPackage((await bulky('wide', 100, '名', 1000)).dir).fatal).toContainEqual(expect.stringContaining("turn's listing"))
    expect(vetPackage((await bulky('narrow', 60, '名', 1000)).dir).fatal).toEqual([])
  })

  it("leaves out the packages past the listing's total budget, each said, in load order (Task 6 re-review R9, fix round 5)", async () => {
    // Five packages each under its own budget (100 names of ~2.5 KB) take more than the listing's together.
    const pkgs = [...(await Promise.all(['p1', 'p2', 'p3', 'p4', 'p5'].map((n) => bulky(n, 100, 'ü名', 500)))), await bulky('p6', 1, 'x', 1)]
    const pins = pkgs.map((p) => ({ name: p.name, fetchUrl: 'https://git.test/x.git', fetchPath: '', commit: 'c', contentHash: p.contentHash }))
    const installer = { acquire: async (pin: { name: string }) => ({ dir: pkgs.find((p) => p.name === pin.name)!.dir, release: () => {}, builtinTools: [] }) }
    const load = () => loadPackagesForRun({ enabledPins: async () => pins }, installer)
    const note = expect.stringMatching(/^plugin package p5 was not loaded: .*listing/)
    // Classic: the directories Claude Code is handed.
    const classic = await load()
    expect(classic.paths).toEqual(['p1', 'p2', 'p3', 'p4', 'p6'].map((n) => pkgs.find((p) => p.name === n)!.dir))
    expect(classic.problems).toEqual([note])
  })

  it('one package off, one write: Bash is not offered', async () => {
    const a = await namer('a', ['Bash', 'Grep'])
    const b = await namer('b', ['Bash'])
    const rows = [row(packageSource('a'), 'builtin:Bash', 'write'), row(packageSource('b'), 'builtin:Bash', 'off')]
    // The turn's half: turnRunner.packages.test.ts, over the same rows.
    expect(builtinsOffOf([a, b], await new AccessResolver(memoryAccess(rows).access).packageAccess(['package:a', 'package:b']))).toEqual(new Set(['Bash']))
    expect(builtinsOffOf([a, b], await new AccessResolver(memoryAccess([]).access).packageAccess(['package:a', 'package:b']))).toEqual(new Set())
  })
})

