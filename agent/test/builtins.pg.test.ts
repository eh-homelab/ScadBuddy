import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { BUILTIN_SOURCES } from '../src/access/model.js'
import { AccessStore } from '../src/access/store.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import type { Database } from '../src/db.js'
import { BuiltInPackages, ownPluginEnabled } from '../src/plugins/packages/builtins.js'
import { OWN_PLUGIN_NAME } from '../src/plugins/packages/vet.js'
import type { BuiltinResult } from '../src/builtins/result.js'
import { BUILTIN_SERVER, builtinServer, type BuiltinContext } from '../src/builtins/server.js'
import { CACHE_PAGES, type NestedQueries, webFetch } from '../src/builtins/web.js'
import { unwrapUntrusted } from '../src/safety/untrusted.js'
import { connectInProcess } from '../src/temporal/turnTools.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { AGENT_SESSIONS_SKIP, agentSessions } from './support/agentSessions.js'

// #2393: the built-ins the session serves in place of Claude Code's, over the `builtin` MCP
// server: WebFetch through the egress-checked exchange and WebSearch, each answering in Claude
// Code's words (measured against the bundled Claude Code 2.1.296 in a classic turn). The file
// tools and the task list are Claude Code's own since plan 2026-10-13 Step 9.6
// (test/nativeBuiltins.sdk.test.ts).

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const ALL = ['WebFetch', 'WebSearch']

type Content = { type: string; text?: string; data?: string; mimeType?: string }

describe.skipIf((skip !== undefined) || AGENT_SESSIONS_SKIP !== '')(`the session's own WebFetch and WebSearch${skip ? ` (skipped: ${skip})` : ''}${AGENT_SESSIONS_SKIP}`, () => {
  agentSessions()
  let db: Database
  let drop: () => Promise<void>
  let client: Client
  const asked: string[] = []
  const searched: unknown[] = []
  const nested: NestedQueries = {
    ask: async (prompt) => {
      asked.push(prompt)
      return 'the answer'
    },
    search: async (input): Promise<BuiltinResult> => {
      searched.push(input)
      return { text: 'Web search results for query: "openscad"' }
    },
  }
  const servers: Server[] = []

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
    const { session } = await m.start(browser, { origin: 'chat' })
    const context = (): BuiltinContext => ({
      web: { nested, secrets: () => ['sk-secret-token'], actor: browser, sessionId: session.id, signal: new AbortController().signal, resolve: async () => ['127.0.0.1'], allowLoopback: true },
    })
    client = await connectInProcess(() => builtinServer(ALL, context))
    asked.length = 0
    searched.length = 0
  })
  afterEach(async () => {
    await client.close()
    await Promise.all(servers.splice(0).map((s) => new Promise((r) => s.close(r))))
    await drop()
  })

  async function call(name: string, args: Record<string, unknown>): Promise<{ text: string; isError: boolean; content: Content[] }> {
    const result = (await client.callTool({ name, arguments: args })) as { content: Content[]; isError?: boolean }
    return { text: result.content[0]?.text ?? '', isError: result.isError === true, content: result.content }
  }

  it("lists Claude Code's names with its input schemas", async () => {
    const { tools } = await client.listTools()
    expect(tools.map((t) => t.name)).toEqual(ALL)
    expect(tools.find((t) => t.name === 'WebFetch')?.inputSchema).toMatchObject({ required: ['url', 'prompt'] })
    expect(BUILTIN_SERVER).toBe('builtin')
  })

  it('fetches a page through the egress check, as markdown, and asks the small model about it', async () => {
    const pages = createServer((req, res) => {
      if (req.url === '/moved') {
        res.writeHead(302, { location: 'http://elsewhere.test/page' })
        res.end()
        return
      }
      res.writeHead(200, { 'content-type': 'text/html' })
      res.end('<html><head><script>alert(1)</script></head><body><h1>Title</h1><p>Some <b>bold</b> text.</p></body></html>')
    })
    servers.push(pages)
    await new Promise<void>((r) => pages.listen(0, '127.0.0.1', r))
    const port = (pages.address() as AddressInfo).port
    const result = await call('WebFetch', { url: `http://pages.test:${port}/`, prompt: 'What is the title?' })
    expect(result.isError).toBe(false)
    expect(unwrapUntrusted(result.text)).toBe('the answer')
    expect(asked[0]).toContain('# Title\n\nSome **bold** text.')
    expect(asked[0]).not.toContain('alert(1)')
    expect(asked[0]).toContain('What is the title?')
    expect((await call('WebFetch', { url: `http://pages.test:${port}/moved`, prompt: 'p' })).text).toContain('REDIRECT DETECTED')
    expect(await call('WebFetch', { url: 'http://localhost/', prompt: 'p' })).toMatchObject({ isError: true })
    expect(asked).toHaveLength(1)
    expect(await call('WebFetch', { url: `http://pages.test:${port}/?k=sk-secret-token`, prompt: 'p' })).toMatchObject({ isError: true })
    expect(asked).toHaveLength(1)
  })

  it("refuses the pod's loopback, by literal or by what a name resolves to", async () => {
    const web = { nested, secrets: () => [], actor: browser, signal: new AbortController().signal }
    expect(await webFetch({ ...web, resolve: async () => ['93.184.215.14'] }, { url: 'http://127.0.0.1:9/', prompt: 'p' })).toMatchObject({
      isError: true,
      text: expect.stringContaining('loopback'),
    })
    expect(await webFetch({ ...web, resolve: async () => ['127.0.0.1'] }, { url: 'http://rebound.example/', prompt: 'p' })).toMatchObject({
      isError: true,
      text: expect.stringContaining('loopback'),
    })
    expect(asked).toEqual([])
  })

  describe('the page cache (#3199)', () => {
    let hits: string[]
    let port: number
    beforeEach(async () => {
      hits = []
      const pages = createServer((req, res) => {
        hits.push(req.url ?? '')
        res.writeHead(200, { 'content-type': 'text/plain' })
        res.end(`page ${req.url}`)
      })
      servers.push(pages)
      await new Promise<void>((r) => pages.listen(0, '127.0.0.1', r))
      port = (pages.address() as AddressInfo).port
    })
    const web = (sessionId: string) => ({
      nested,
      secrets: () => [],
      actor: browser,
      sessionId,
      signal: new AbortController().signal,
      resolve: async () => ['127.0.0.1'],
      allowLoopback: true,
    })

    it("is a session's own: another session's fetch of the page fetches it, through the egress check", async () => {
      const url = `http://pages.test:${port}/shared`
      await webFetch(web('session-a'), { url, prompt: 'p' })
      await webFetch(web('session-a'), { url, prompt: 'p' })
      expect(hits).toEqual(['/shared'])
      await webFetch(web('session-b'), { url, prompt: 'p' })
      expect(hits).toEqual(['/shared', '/shared'])
    })

    it('refuses a cached page whose URL has since become a known secret, as a fetch of it would be', async () => {
      const url = `http://pages.test:${port}/?k=tok-later-secret`
      expect((await webFetch(web('session-s'), { url, prompt: 'p' })).isError).toBeUndefined()
      const refused = await webFetch({ ...web('session-s'), secrets: () => ['tok-later-secret'] }, { url, prompt: 'p' })
      expect(refused).toMatchObject({ isError: true, text: expect.stringContaining('credential') })
      expect(hits).toHaveLength(1)
    })

    it(`keeps at most ${CACHE_PAGES} pages, dropping the oldest`, async () => {
      const ctx = web('session-c')
      for (let i = 0; i <= CACHE_PAGES; i++) await webFetch(ctx, { url: `http://pages.test:${port}/${i}`, prompt: 'p' })
      expect(hits).toHaveLength(CACHE_PAGES + 1)
      await webFetch(ctx, { url: `http://pages.test:${port}/${CACHE_PAGES}`, prompt: 'p' })
      expect(hits).toHaveLength(CACHE_PAGES + 1)
      await webFetch(ctx, { url: `http://pages.test:${port}/0`, prompt: 'p' })
      expect(hits).toHaveLength(CACHE_PAGES + 2)
    })
  })

  it("stops a WebFetch's nested query when its caller gives up on the call (#3199)", async () => {
    const pages = createServer((_req, res) => {
      res.writeHead(200, { 'content-type': 'text/plain' })
      res.end('slow page')
    })
    servers.push(pages)
    await new Promise<void>((r) => pages.listen(0, '127.0.0.1', r))
    const port = (pages.address() as AddressInfo).port
    let stopped: () => void
    const askStopped = new Promise<void>((r) => (stopped = r))
    const slow: NestedQueries = {
      ...nested,
      ask: (_prompt, signal) =>
        new Promise((resolve) => {
          signal.addEventListener('abort', () => (stopped(), resolve('stopped')), { once: true })
        }),
    }
    const own = await connectInProcess(() =>
      builtinServer(['WebFetch'], () => ({
        web: { nested: slow, secrets: () => [], actor: browser, signal: new AbortController().signal, resolve: async () => ['127.0.0.1'], allowLoopback: true },
      })),
    )
    try {
      await expect(own.callTool({ name: 'WebFetch', arguments: { url: `http://slow.test:${port}/`, prompt: 'p' } }, undefined, { timeout: 300 })).rejects.toThrow(
        /timed out/i,
      )
      await expect(Promise.race([askStopped.then(() => 'stopped'), new Promise((r) => setTimeout(() => r('still running'), 2_000))])).resolves.toBe('stopped')
    } finally {
      await own.close()
    }
  })

  it('hands a search to the nested query as it was asked', async () => {
    expect((await call('WebSearch', { query: 'openscad' })).text).toBe('Web search results for query: "openscad"')
    expect(searched).toEqual([{ query: 'openscad' }])
  })
})

// #2171: ScadBuddy's own plugin is the `builtin:own-plugin` source; its switch is that source's `*` row.
describe.skipIf(skip !== undefined)(`ScadBuddy's own plugin in ai_access${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it("switching ScadBuddy's own plugin off stores builtin:own-plugin '*' off, and ownPluginEnabled answers false", async () => {
    const access = new AccessStore(db.sql)
    const packages = new BuiltInPackages(access)
    expect(await ownPluginEnabled(access)).toBe(true)
    await access.setEnabled(BUILTIN_SOURCES.ownPlugin, false, { actor: UI_ACTOR, surface: 'http' })
    expect(await db.sql`SELECT source, item, access FROM ai_access WHERE source = ${BUILTIN_SOURCES.ownPlugin}`).toEqual([
      { source: BUILTIN_SOURCES.ownPlugin, item: '*', access: 'off' },
    ])
    expect(await ownPluginEnabled(access)).toBe(false)
    expect((await packages.get(OWN_PLUGIN_NAME))?.enabled).toBe(false)
    await access.setEnabled(BUILTIN_SOURCES.ownPlugin, true, { actor: UI_ACTOR, surface: 'http' })
    expect(await ownPluginEnabled(access)).toBe(true)
  })
})
