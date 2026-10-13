import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
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
  })

  it('is the stored ranges', async () => {
    const peers = await subagentPeers(repo(IN_CLUSTER).settings)
    expect(peers && inBlockList(peers, '10.42.3.9')).toBe(true)
    expect(peers && inBlockList(peers, '10.43.0.1')).toBe(false)
  })
})
