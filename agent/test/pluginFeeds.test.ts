import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import type { McpAuthSettings } from '../src/auth/authenticate.js'
import type { FeedPackageRow, FeedRead, FeedZip, PluginFeedReader } from '../src/plugins/feed.js'
import type { PluginFeedView } from '../src/routes/pluginFeeds.js'
import { appFetch, INGRESS, testApp, UNTRUSTED } from './helpers/mcp.js'

// The marketplace feed (#6933, spec 2026-11-21 §6.4, routes/pluginFeeds.ts): served on /mcp's
// auth to Claude Code sessions outside ScadBuddy, listing only approved pins, with zip URLs made from
// the origin the feed was asked on. The reads are a fake of the CRUD routes (backend
// tests/api/test_crud_agent_plugin_feeds.py covers those).

const PUBLIC = 'https://scadbuddy.test'
const FEED = `${PUBLIC}/api/v1/ai/plugin-feeds/default`
/** An outside client through the TLS ingress: no Origin. */
const HTTPS = { 'x-forwarded-proto': 'https' }
/** The Settings page through the same ingress. */
const UI = { origin: PUBLIC, 'x-forwarded-proto': 'https' }

const ZIP = new Uint8Array(Buffer.from('PK\u0003\u0004 a zip'))
const ZIP_SHA = createHash('sha256').update(ZIP).digest('hex')
const COMMIT = 'c'.repeat(40)
const HASH = `sha256:${'1'.repeat(64)}`

function pkg(name: string, extra: Partial<FeedPackageRow> = {}): FeedPackageRow {
  return {
    name,
    source_kind: 'git',
    fetch_url: `https://git.test/${name}.git`,
    fetch_path: '',
    commit_sha: COMMIT,
    content_hash: HASH,
    approved: true,
    description: `${name} plugin`,
    hosted: null,
    ...extra,
  }
}

const PACKAGES: FeedPackageRow[] = [
  pkg('greeter', { source_kind: 'hosted', fetch_url: null, fetch_path: null, commit_sha: null, hosted: { version: 3, zip_sha256: ZIP_SHA } }),
  pkg('mono', { fetch_path: 'plugins/mono' }),
  pkg('plain', { description: null }),
  pkg('waiting', { approved: false }),
  pkg('sha256-repo', { commit_sha: 'd'.repeat(64) }),
  pkg('lost', { source_kind: 'hosted', hosted: null }),
]

class FakeFeeds implements PluginFeedReader {
  zips = new Map<string, FeedZip>([['greeter@3', { bytes: ZIP }]])
  reads: (string | undefined)[] = []
  async read(feed: string, name?: string): Promise<FeedRead> {
    this.reads.push(name)
    if (feed !== 'default') return { feed: null, packages: [] }
    const packages = name === undefined ? PACKAGES : PACKAGES.filter((p) => p.name === name)
    return { feed: { slug: 'default', name: 'scadbuddy', owner: 'ScadBuddy' }, packages }
  }
  async zip(name: string, version: number): Promise<FeedZip | undefined> {
    return this.zips.get(`${name}@${version}`)
  }
}

function made(
  options: { settings?: Partial<McpAuthSettings>; authSettings?: () => McpAuthSettings; publicUrl?: string | null; ready?: boolean } = {},
) {
  const feeds = new FakeFeeds()
  const ready = options.ready ?? true
  const app = testApp({
    settings: options.settings,
    ...(options.authSettings ? { authSettings: options.authSettings } : {}),
    deps: { pluginFeeds: feeds, database: { ping: async () => true, ready: async () => ready, areasReady: async () => ready } },
    mcp: { publicUrl: options.publicUrl === null ? undefined : (options.publicUrl ?? PUBLIC) },
  })
  const get = (url: string, headers: Record<string, string> = {}, address = INGRESS) =>
    appFetch(app.app, { address, headers: { ...HTTPS, ...headers } })(url)
  return { ...app, feeds, get }
}

const bearer = (token: string) => ({ authorization: `Bearer ${token}` })

describe('the feed on the /mcp auth', () => {
  it('serves a token of every tier the approved pins', async () => {
    const { tokens, get } = made()
    for (const tier of ['read', 'write', 'outward'] as const) {
      const { token } = await tokens.mint({ name: tier, tier })
      const res = await get(`${FEED}/marketplace.json`, bearer(token))
      expect(res.status, tier).toBe(200)
      expect(res.headers.get('cache-control')).toBe('private, max-age=60')
      expect(await res.json()).toEqual({
        name: 'scadbuddy',
        owner: { name: 'ScadBuddy' },
        plugins: [
          {
            name: 'greeter',
            description: 'greeter plugin',
            version: '3.0.0',
            source: { source: 'archive', url: `${FEED}/plugins/greeter/versions/3.zip`, sha256: ZIP_SHA },
          },
          {
            name: 'mono',
            description: 'mono plugin',
            source: { source: 'git-subdir', url: 'https://git.test/mono.git', path: 'plugins/mono', sha: COMMIT },
          },
          { name: 'plain', source: { source: 'url', url: 'https://git.test/plain.git', sha: COMMIT } },
        ],
      })
    }
  })

  it('stamps the token as an MCP call does, and stops at once when it is revoked', async () => {
    const { tokens, get } = made()
    const { token, record } = await tokens.mint({ name: 'cli', tier: 'read' })
    expect((await get(`${FEED}/marketplace.json`, bearer(token))).status).toBe(200)
    expect((await tokens.list()).find((r) => r.id === record.id)?.lastUsedAt).toBeInstanceOf(Date)

    await tokens.revoke(record.id)
    const feed = await get(`${FEED}/marketplace.json`, bearer(token))
    expect(feed.status).toBe(401)
    expect(feed.headers.get('www-authenticate')).toContain('invalid_token')
    expect((await get(`${FEED}/plugins/greeter/versions/3.zip`, bearer(token))).status).toBe(401)
  })

  it('needs a token in bearer mode, and fails closed when the settings cannot be read', async () => {
    expect((await made().get(`${FEED}/marketplace.json`)).status).toBe(401)
    const broken = made({
      authSettings: () => {
        throw new Error('settings down')
      },
    })
    const { token } = await broken.tokens.mint({ name: 'x', tier: 'outward' })
    expect((await broken.get(`${FEED}/marketplace.json`, bearer(token))).status).toBe(401)
  })

  it('lets anyone read in disabled mode, as /mcp does, whatever the anonymous cap', async () => {
    for (const cap of ['read', 'write', 'outward'] as const) {
      const res = await made({ settings: { mode: 'disabled', anonymousCap: cap } }).get(`${FEED}/marketplace.json`)
      expect(res.status, cap).toBe(200)
    }
  })

  it('passes the transport gates /mcp passes: HTTPS, and no foreign Origin', async () => {
    const { tokens, get } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    // An untrusted peer's X-Forwarded-Proto is not believed: plain HTTP.
    expect((await get(`${FEED}/marketplace.json`, bearer(token), UNTRUSTED)).status).toBe(403)
    expect((await get(`${FEED}/marketplace.json`, { ...bearer(token), origin: 'https://evil.test' })).status).toBe(403)
  })

  it('makes each zip URL from the origin the feed was asked on, not the public URL', async () => {
    // Claude Code sends a marketplace's headers only to its own origin, and the public URL may be
    // an SSO host a token cannot pass, so the zips are on whatever host the client reached.
    const { tokens, get, app } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    const zipOf = async (res: Response) =>
      ((await res.json()) as { plugins: { name: string; source: { url?: string } }[] }).plugins.find((p) => p.name === 'greeter')
        ?.source.url
    const lan = 'https://scadbuddy.lan.test/api/v1/ai/plugin-feeds/default'
    expect(await zipOf(await get(`${lan}/marketplace.json`, bearer(token)))).toBe(`${lan}/plugins/greeter/versions/3.zip`)
    // A trusted proxy's X-Forwarded-Host (and its port) is honoured, as the origin checks honour it.
    const forwarded = await get(`${FEED}/marketplace.json`, { ...bearer(token), 'x-forwarded-host': 'feed.lan.test:8443' })
    expect(await zipOf(forwarded)).toBe('https://feed.lan.test:8443/api/v1/ai/plugin-feeds/default/plugins/greeter/versions/3.zip')
    // A loopback peer with no proxy is plain HTTP to its Host.
    const local = await appFetch(app, { headers: bearer(token) })('http://localhost:8081/api/v1/ai/plugin-feeds/default/marketplace.json')
    expect(await zipOf(local)).toBe('http://localhost:8081/api/v1/ai/plugin-feeds/default/plugins/greeter/versions/3.zip')
  })

  it('answers 400 when the request names no usable host', async () => {
    const { tokens, get } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    const res = await get(`${FEED}/marketplace.json`, { ...bearer(token), 'x-forwarded-host': 'bad host!' })
    expect(res.status).toBe(400)
    expect(await res.text()).toContain('names no host')
  })

  it('needs no public URL, and answers 503 before the CRUD areas are served', async () => {
    const noUrl = made({ publicUrl: null })
    const { token } = await noUrl.tokens.mint({ name: 'x', tier: 'read' })
    expect((await noUrl.get(`${FEED}/marketplace.json`, bearer(token))).status).toBe(200)
    expect((await noUrl.get(`${FEED}/plugins/greeter/versions/3.zip`, bearer(token))).status).toBe(200)
    const early = made({ ready: false })
    const t2 = await early.tokens.mint({ name: 'x', tier: 'read' })
    expect((await early.get(`${FEED}/marketplace.json`, bearer(t2.token))).status).toBe(503)
  })

  it('serves the listed version of a hosted plugin as an immutable zip', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    const res = await get(`${FEED}/plugins/greeter/versions/3.zip`, bearer(token))
    expect(res.status).toBe(200)
    // It reads only its own package of the feed.
    expect(feeds.reads).toEqual(['greeter'])
    expect(res.headers.get('content-type')).toBe('application/zip')
    expect(res.headers.get('cache-control')).toBe('private, max-age=31536000, immutable')
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(ZIP)
  })

  it('answers 404 for whatever the feed does not list', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    feeds.zips.set('greeter@2', { bytes: ZIP })
    for (const path of [
      // Another feed, or not a feed's name at all.
      '/api/v1/ai/plugin-feeds/other/marketplace.json',
      '/api/v1/ai/plugin-feeds/Bad_Feed/marketplace.json',
      // A version that is not the approved pin (an older one, a pending or a draft's next).
      `${new URL(FEED).pathname}/plugins/greeter/versions/2.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/4.zip`,
      // A package the feed does not name, one with no approved pin, a git one, a hosted one with no version.
      `${new URL(FEED).pathname}/plugins/stranger/versions/1.zip`,
      `${new URL(FEED).pathname}/plugins/waiting/versions/1.zip`,
      `${new URL(FEED).pathname}/plugins/mono/versions/1.zip`,
      `${new URL(FEED).pathname}/plugins/lost/versions/1.zip`,
      // Not a version's file name.
      `${new URL(FEED).pathname}/plugins/greeter/versions/03.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/3.tar`,
    ]) {
      expect((await get(`${PUBLIC}${path}`, bearer(token))).status, path).toBe(404)
    }
  })

  it('serves no zip whose bytes differ from the recorded sha256', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    feeds.zips.set('greeter@3', { bytes: new Uint8Array([1, 2, 3]) })
    expect((await get(`${FEED}/plugins/greeter/versions/3.zip`, bearer(token))).status).toBe(502)
  })
})

describe("Settings' read of the feed", () => {
  it('makes the marketplace URL from the request origin, not the public URL', async () => {
    const { get } = made({ publicUrl: 'https://sso.example' })
    const view = (await (await get(FEED, UI)).json()) as PluginFeedView
    expect(view.marketplace_url).toBe(`${FEED}/marketplace.json`)
    const none = made({ publicUrl: null })
    const noUrl = (await (await none.get(FEED, UI)).json()) as PluginFeedView
    expect(noUrl.marketplace_url).toBe(`${FEED}/marketplace.json`)
  })

  it('lists every package the feed names, with whether it is listed and why not', async () => {
    const { get } = made()
    const res = await get(FEED, UI)
    expect(res.status).toBe(200)
    const view = (await res.json()) as PluginFeedView
    expect(view.marketplace_url).toBe(`${FEED}/marketplace.json`)
    expect(view.auth_mode).toBe('bearer')
    expect(view.packages.map((p) => [p.name, p.listed, p.reason, p.kind, p.version])).toEqual([
      ['greeter', true, null, 'hosted', 3],
      ['mono', true, null, 'git', null],
      ['plain', true, null, 'git', null],
      ['waiting', false, 'not-approved', null, null],
      ['sha256-repo', false, 'unpinnable', null, null],
      ['lost', false, 'no-version', null, null],
    ])
  })

  it('is a UI read: refused from another origin', async () => {
    const { get } = made()
    expect((await get(FEED, { ...UI, origin: 'https://evil.test' })).status).toBe(403)
    expect((await get(`${PUBLIC}/api/v1/ai/plugin-feeds/other`, UI)).status).toBe(404)
  })
})
