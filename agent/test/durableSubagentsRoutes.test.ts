import { Hono } from 'hono'
import { describe, expect, it, vi } from 'vitest'
import { inBlockList, originPolicy } from '../src/http/origins.js'
import {
  DURABLE_SUBAGENTS_PATH,
  registerDurableSubagentsRoutes,
  SETTING_DURABLE_SUBAGENTS,
  subagentPeers,
} from '../src/routes/durableSubagents.js'
import type { SettingsRepo } from '../src/routes/httpRequest.js'

// /api/v1/ai/settings/durable-subagents: where a durable segment's subagents reach
// ScadBuddy's tools, behind the same UI guards as every other AI settings route.

const URL_ = `https://scadbuddy.example${DURABLE_SUBAGENTS_PATH}`
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

function repo(stored?: unknown) {
  const values = new Map<string, unknown>()
  if (stored !== undefined) values.set(SETTING_DURABLE_SUBAGENTS, stored)
  const contexts: unknown[] = []
  const settings: SettingsRepo = {
    get: <T>(key: string) => Promise.resolve(values.get(key) as T),
    set: (key, value, context) => {
      contexts.push(context)
      values.set(key, value)
      return Promise.resolve()
    },
  }
  return { settings, values, contexts }
}

function setup(options: { ready?: boolean; settings?: boolean; stored?: unknown } = {}) {
  const { settings, values, contexts } = repo(options.stored)
  const app = new Hono()
  registerDurableSubagentsRoutes(app, {
    settings: options.settings === false ? undefined : settings,
    ready: () => Promise.resolve(options.ready ?? true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
  })
  return { app, values, contexts }
}

const get = (app: Hono, headers: Record<string, string> = { host: UI.host, 'x-forwarded-proto': 'https' }) =>
  app.request(URL_, { headers })
const put = (app: Hono, body: unknown, headers: Record<string, string> = UI) =>
  app.request(URL_, { method: 'PUT', headers: { ...headers, 'content-type': 'application/json' }, body: JSON.stringify(body) })

const IN_CLUSTER = { url: 'http://scadbuddy.bambuddy.svc:8081/internal/durable/subagent-mcp', peers: ['10.42.0.0/16'] }

describe('the durable subagents setting', () => {
  it('is loopback only until set, and reads back what was stored', async () => {
    const { app, values, contexts } = setup()
    expect(await (await get(app)).json()).toEqual({ url: null, peers: [] })
    const saved = await put(app, { url: IN_CLUSTER.url, peers: [' 10.42.0.0/16 '] })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toEqual(IN_CLUSTER)
    expect(values.get(SETTING_DURABLE_SUBAGENTS)).toEqual(IN_CLUSTER)
    // Audited as the UI user, from the request's address.
    expect(contexts[0]).toMatchObject({ surface: 'http', clientIp: '10.0.0.7' })
    expect(await (await get(app)).json()).toEqual(IN_CLUSTER)
  })

  it('takes ranges as broad as a /8 or an IPv6 /32, and single addresses', async () => {
    const { app, values } = setup()
    const peers = ['10.0.0.0/8', 'fd00::/32', '10.42.1.5', 'fd00::1', '::ffff:10.0.0.0/104', '::ffff:10.42.1.5']
    expect((await put(app, { url: null, peers })).status).toBe(200)
    expect(values.get(SETTING_DURABLE_SUBAGENTS)).toEqual({ url: null, peers })
  })

  it.each([
    ['http:scadbuddy.svc/mcp', 'http://scadbuddy.svc/mcp'],
    [' http://scadbuddy.svc:8081/mcp', 'http://scadbuddy.svc:8081/mcp'],
    ['HTTP://ScadBuddy.svc', 'http://scadbuddy.svc/'],
  ])('stores %j as the URL it parses to, which the durable worker reads the same (#3054)', async (sent, href) => {
    const { app, values } = setup()
    expect((await put(app, { url: sent, peers: [] })).status).toBe(200)
    expect(values.get(SETTING_DURABLE_SUBAGENTS)).toEqual({ url: href, peers: [] })
  })

  it('refuses a URL whose stored form is over 2048 characters, though what was sent is not', async () => {
    const { app, values } = setup()
    // 421 characters as sent; each é is stored as %C3%A9.
    const sent = `http://scadbuddy.svc/${'é'.repeat(400)}`
    expect(sent.length).toBeLessThanOrEqual(2048)
    expect(new URL(sent).href.length).toBeGreaterThan(2048)
    expect((await put(app, { url: sent, peers: [] })).status).toBe(400)
    expect(values.has(SETTING_DURABLE_SUBAGENTS)).toBe(false)
  })

  it('stores a null url and no peers, back to the pod loopback', async () => {
    const { app, values } = setup({ stored: IN_CLUSTER })
    expect((await put(app, { url: null, peers: [] })).status).toBe(200)
    expect(values.get(SETTING_DURABLE_SUBAGENTS)).toEqual({ url: null, peers: [] })
  })

  it.each([
    [{}],
    [{ url: null }],
    [{ peers: [] }],
    [{ url: 'ftp://host/x', peers: [] }],
    [{ url: 'not a url', peers: [] }],
    [{ url: null, peers: ['10.0.0.0/'] }],
    [{ url: null, peers: ['example.com'] }],
    [{ url: null, peers: ['10.0.0.0/33'] }],
    [{ url: null, peers: [1] }],
    [{ url: null, peers: Array.from({ length: 33 }, () => '10.0.0.1') }],
    [{ url: null, peers: [], extra: 1 }],
    // One range per entry, and none so broad it opens the route to every peer.
    [{ url: null, peers: ['10.0.0.1,10.0.0.2'] }],
    [{ url: null, peers: [''] }],
    [{ url: null, peers: ['0.0.0.0/0'] }],
    [{ url: null, peers: ['10.0.0.0/7'] }],
    [{ url: null, peers: ['::/0'] }],
    [{ url: null, peers: ['fd00::/31'] }],
    // IPv6 ranges over the IPv4-mapped block would admit every IPv4 peer.
    [{ url: null, peers: ['::/32'] }],
    [{ url: null, peers: ['::ffff:0.0.0.0/96'] }],
    [{ url: null, peers: ['::ffff:10.0.0.0/103'] }],
    // Covers the mapped block without starting at `::`.
    [{ url: null, peers: ['::fffe:0:0/95'] }],
  ])('refuses the body %j, and stores nothing', async (body) => {
    const { app, values } = setup()
    expect((await put(app, body)).status).toBe(400)
    expect(values.size).toBe(0)
  })

  it.each([['nonsense'], [null], [{ url: 1, peers: 'x' }], [{ url: 'http://a/', peers: [1] }]])(
    'reads a stored %j it could not have written as the unset parts',
    async (stored) => {
      const { app } = setup({ stored })
      const view = (await (await get(app)).json()) as { url: unknown; peers: unknown }
      expect(view.peers).toEqual([])
    },
  )

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
    ['plain HTTP', { ...UI, 'x-forwarded-proto': 'http' }],
  ])('refuses a write from %s, and stores nothing', async (_label, headers) => {
    const { app, values } = setup()
    expect((await put(app, { url: null, peers: ['0.0.0.0/0'] }, headers as Record<string, string>)).status).toBe(403)
    expect(values.has(SETTING_DURABLE_SUBAGENTS)).toBe(false)
  })

  it('refuses a read over plain HTTP', async () => {
    expect((await get(setup().app, { host: UI.host, 'x-forwarded-proto': 'http' })).status).toBe(403)
  })

  it('answers 503 without a database, or before migrations applied', async () => {
    expect((await get(setup({ settings: false }).app)).status).toBe(503)
    expect((await put(setup({ ready: false }).app, { url: null, peers: [] })).status).toBe(503)
  })
})

describe('subagentPeers', () => {
  it('is none without a database, unset, or with a stored entry that is not a range', async () => {
    expect(await subagentPeers(undefined)).toBeUndefined()
    expect(await subagentPeers(repo().settings)).toBeUndefined()
    expect(await subagentPeers(repo({ url: null, peers: [] }).settings)).toBeUndefined()
    expect(await subagentPeers(repo({ url: null, peers: ['10.42.0.0/16', 'bogus'] }).settings)).toBeUndefined()
    expect(await subagentPeers(repo({ url: null, peers: ['0.0.0.0/0'] }).settings)).toBeUndefined()
    expect(await subagentPeers(repo({ url: null, peers: ['::ffff:0.0.0.0/96'] }).settings)).toBeUndefined()
    expect(await subagentPeers(repo({ url: null, peers: ['10.42.0.0/16,10.43.0.0/16'] }).settings)).toBeUndefined()
  })

  it('says once in the log that a stored value was refused', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const { settings } = repo({ url: null, peers: ['10.42.0.0/16', '10.0.0.0/7'] })
      expect(await subagentPeers(settings)).toBeUndefined()
      expect(await subagentPeers(settings)).toBeUndefined()
      expect(warn).toHaveBeenCalledTimes(1)
      expect(warn.mock.calls[0]?.[0]).toContain('["10.0.0.0/7"]')
    } finally {
      warn.mockRestore()
    }
  })

  it('is the stored ranges', async () => {
    const peers = await subagentPeers(repo(IN_CLUSTER).settings)
    expect(peers && inBlockList(peers, '10.42.3.9')).toBe(true)
    expect(peers && inBlockList(peers, '10.43.0.1')).toBe(false)
  })
})
