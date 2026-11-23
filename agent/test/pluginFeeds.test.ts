import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import type { McpAuthSettings } from '../src/auth/authenticate.js'
import { defaultOidcConfig, OidcProvider } from '../src/auth/oidc.js'
import {
  ARCHIVE_FAILURE_KEPT_MS,
  ArchiveMaker,
  archiveKey,
  type FeedPackageRow,
  type FeedRead,
  FeedWriteError,
  type FeedZip,
  type PluginFeedReader,
  servedArchive,
} from '../src/plugins/feed.js'
import { readArchive } from '../src/plugins/packages/unpack.js'
import type { PluginFeedView } from '../src/routes/pluginFeeds.js'
import { appFetch, INGRESS, testApp, UNTRUSTED } from './helpers/mcp.js'
import { tar, zip } from './support/archives.js'
import { startFakeIdp } from './support/fakeIdp.js'

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
const sha256 = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex')

const SKILL = '---\nname: hi\ndescription: Says hi\n---\nSay hi.\n'
/** An archive pin's mirrored zip: served as it is. */
const ARCHIVE = zip([{ name: 'skills/hi/SKILL.md', data: SKILL }])
const ARCHIVE_SHA = sha256(ARCHIVE)
/** An npm pin's mirrored tarball: served as a zip of its `package/` folder. */
const TARBALL = tar(
  [
    { name: 'package/', type: '5', mode: 0o755 },
    { name: 'package/skills/hi/SKILL.md', data: SKILL },
    { name: 'package/skills/hi/run.sh', data: 'echo hi\n', mode: 0o755 },
    { name: 'package/package.json', data: '{"name":"hi"}' },
  ],
  { gzip: true },
)
const TARBALL_SHA = sha256(TARBALL)
const TARBALL_ZIP = servedArchive(TARBALL, 'package')
const TARBALL_ZIP_SHA = sha256(TARBALL_ZIP)
const GONE_SHA = 'e'.repeat(64)

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
  // #6936: a git entry of a bare marketplace.json URL is a commit pin.
  pkg('from-url', { source_kind: 'marketplace-url', pin_kind: 'commit' }),
  pkg('zipped', bytes('archive', ARCHIVE_SHA, '')),
  pkg('tarred', { ...bytes('npm', TARBALL_SHA, 'package'), allow_refused: true }),
  pkg('unmirrored', bytes('archive', GONE_SHA, '')),
]

function bytes(kind: string, sha: string, path: string): Partial<FeedPackageRow> {
  return { source_kind: kind, fetch_url: `https://files.test/${sha}`, fetch_path: path, commit_sha: null, pin_kind: 'bytes', bytes_sha256: sha }
}

class FakeFeeds implements PluginFeedReader {
  zips = new Map<string, FeedZip>([['greeter@3', { bytes: ZIP }]])
  mirrors = new Map<string, Buffer>([
    [ARCHIVE_SHA, ARCHIVE],
    [TARBALL_SHA, TARBALL],
  ])
  mirrorReads: string[] = []
  members = new Set(PACKAGES.map((p) => p.name))
  installed = new Set([...PACKAGES.map((p) => p.name), 'outside'])
  reads: (string | undefined)[] = []
  async mirror(name: string, sha: string): Promise<Buffer | undefined> {
    this.mirrorReads.push(name)
    return this.mirrors.get(sha)
  }
  notServed = false
  async add(feed: string, name: string): Promise<boolean> {
    if (this.notServed) throw new FeedWriteError('the API does not serve plugin feed changes yet', 'not-served')
    if (feed !== 'default') throw new FeedWriteError(`no plugin feed "${feed}"`, 'feed-not-found')
    if (!this.installed.has(name)) throw new FeedWriteError(`no plugin package named "${name}"`, 'package-not-found')
    if (this.members.has(name)) return false
    this.members.add(name)
    return true
  }
  async remove(feed: string, name: string): Promise<boolean> {
    if (this.notServed) throw new FeedWriteError('the API does not serve plugin feed changes yet', 'not-served')
    if (feed !== 'default') throw new FeedWriteError(`no plugin feed "${feed}"`, 'feed-not-found')
    return this.members.delete(name)
  }
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
  options: {
    settings?: Partial<McpAuthSettings>
    authSettings?: () => McpAuthSettings
    publicUrl?: string | null
    ready?: boolean
    oidc?: OidcProvider
  } = {},
) {
  const feeds = new FakeFeeds()
  const ready = options.ready ?? true
  const app = testApp({
    settings: options.settings,
    ...(options.authSettings ? { authSettings: options.authSettings } : {}),
    deps: { pluginFeeds: feeds, database: { ping: async () => true, ready: async () => ready, areasReady: async () => ready } },
    mcp: {
      publicUrl: options.publicUrl === null ? undefined : (options.publicUrl ?? PUBLIC),
      ...(options.oidc ? { oidc: options.oidc } : {}),
    },
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
            source: { source: 'archive', url: `${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, sha256: ZIP_SHA },
          },
          {
            name: 'mono',
            description: 'mono plugin',
            source: { source: 'git-subdir', url: 'https://git.test/mono.git', path: 'plugins/mono', sha: COMMIT },
          },
          { name: 'plain', source: { source: 'url', url: 'https://git.test/plain.git', sha: COMMIT } },
          {
            name: 'from-url',
            description: 'from-url plugin',
            source: { source: 'url', url: 'https://git.test/from-url.git', sha: COMMIT },
          },
          {
            name: 'zipped',
            description: 'zipped plugin',
            source: { source: 'archive', url: `${FEED}/plugins/zipped/archives/${ARCHIVE_SHA}.zip`, sha256: ARCHIVE_SHA },
          },
          {
            name: 'tarred',
            description: 'tarred plugin',
            source: { source: 'archive', url: `${FEED}/plugins/tarred/archives/${TARBALL_ZIP_SHA}.zip`, sha256: TARBALL_ZIP_SHA },
          },
        ],
      })
    }
  })

  it('makes a bytes pin\'s zip once per process, and tries a missing mirror again', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    await get(`${FEED}/marketplace.json`, bearer(token))
    await get(`${FEED}/marketplace.json`, bearer(token))
    expect(feeds.mirrorReads.sort()).toEqual(['tarred', 'unmirrored', 'unmirrored', 'zipped'])
    // Once the mirror holds it, the next read lists it.
    feeds.mirrors.set(GONE_SHA, ARCHIVE)
    const listed = (await (await get(`${FEED}/marketplace.json`, bearer(token))).json()) as { plugins: { name: string }[] }
    expect(listed.plugins.map((p) => p.name)).toContain('unmirrored')
  })

  it('serves a mirrored zip as it is, and a tarball as a zip of its plugin root', async () => {
    const { tokens, get } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    const zipped = await get(`${FEED}/plugins/zipped/archives/${ARCHIVE_SHA}.zip`, bearer(token))
    expect(zipped.status).toBe(200)
    expect(zipped.headers.get('content-type')).toBe('application/zip')
    expect(zipped.headers.get('cache-control')).toBe('private, max-age=31536000, immutable')
    expect(Buffer.from(await zipped.arrayBuffer())).toEqual(ARCHIVE)

    const tarred = await get(`${FEED}/plugins/tarred/archives/${TARBALL_ZIP_SHA}.zip`, bearer(token))
    expect(tarred.status).toBe(200)
    const body = Buffer.from(await tarred.arrayBuffer())
    expect(sha256(body)).toBe(TARBALL_ZIP_SHA)
    expect(tarred.headers.get('etag')).toBe(`"${TARBALL_ZIP_SHA}"`)
    const entries = readArchive(body)
    expect(entries.map((e) => [e.path, e.executable])).toEqual([
      ['package.json', false],
      ['skills/hi/SKILL.md', false],
      ['skills/hi/run.sh', true],
    ])
    expect(entries[1]!.data!.toString()).toBe(SKILL)
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
    expect((await get(`${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, bearer(token))).status).toBe(401)
  })

  it('refuses an expired token, as /mcp does', async () => {
    const { tokens, get } = made()
    const { token } = await tokens.mint({ name: 'old', tier: 'read', expiresAt: new Date(Date.now() - 1000) })
    for (const path of ['marketplace.json', `plugins/greeter/versions/3/${ZIP_SHA}.zip`]) {
      const res = await get(`${FEED}/${path}`, bearer(token))
      expect(res.status, path).toBe(401)
      expect(res.headers.get('www-authenticate')).toContain('invalid_token')
    }
  })

  it('takes an OIDC access token in oidc mode, as /mcp does, and refuses an expired one', async () => {
    const idp = await startFakeIdp()
    try {
      const { get } = made({
        settings: { mode: 'oidc', oidc: { ...defaultOidcConfig(idp.issuer), enabled: true } },
        oidc: new OidcProvider(),
      })
      const good = bearer(await idp.sign({ scope: 'scadbuddy:read' }))
      expect((await get(`${FEED}/marketplace.json`, good)).status).toBe(200)
      expect((await get(`${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, good)).status).toBe(200)
      const old = bearer(await idp.sign({ scope: 'scadbuddy:read', exp: Math.floor(Date.now() / 1000) - 600 }))
      const refused = await get(`${FEED}/marketplace.json`, old)
      expect(refused.status).toBe(401)
      expect(refused.headers.get('www-authenticate')).toContain('invalid_token')
      expect((await get(`${FEED}/marketplace.json`)).status).toBe(401)
    } finally {
      await idp.close()
    }
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
    expect(await zipOf(await get(`${lan}/marketplace.json`, bearer(token)))).toBe(`${lan}/plugins/greeter/versions/3/${ZIP_SHA}.zip`)
    // A trusted proxy's X-Forwarded-Host (and its port) is honoured, as the origin checks honour it.
    const forwarded = await get(`${FEED}/marketplace.json`, { ...bearer(token), 'x-forwarded-host': 'feed.lan.test:8443' })
    expect(await zipOf(forwarded)).toBe(`https://feed.lan.test:8443/api/v1/ai/plugin-feeds/default/plugins/greeter/versions/3/${ZIP_SHA}.zip`)
    // A loopback peer with no proxy is plain HTTP to its Host.
    const local = await appFetch(app, { headers: bearer(token) })('http://localhost:8081/api/v1/ai/plugin-feeds/default/marketplace.json')
    expect(await zipOf(local)).toBe(`http://localhost:8081/api/v1/ai/plugin-feeds/default/plugins/greeter/versions/3/${ZIP_SHA}.zip`)
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
    expect((await noUrl.get(`${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, bearer(token))).status).toBe(200)
    const early = made({ ready: false })
    const t2 = await early.tokens.mint({ name: 'x', tier: 'read' })
    expect((await early.get(`${FEED}/marketplace.json`, bearer(t2.token))).status).toBe(503)
  })

  it('serves the listed version of a hosted plugin as an immutable zip', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    const res = await get(`${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, bearer(token))
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
      `${new URL(FEED).pathname}/plugins/greeter/versions/2/${ZIP_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/4/${ZIP_SHA}.zip`,
      // A package the feed does not name, one with no approved pin, a git one, a hosted one with no version.
      `${new URL(FEED).pathname}/plugins/stranger/versions/1/${ZIP_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/waiting/versions/1/${ZIP_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/mono/versions/1/${ZIP_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/lost/versions/1/${ZIP_SHA}.zip`,
      // Not a version's file name.
      `${new URL(FEED).pathname}/plugins/greeter/versions/03/${ZIP_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/3.tar`,
      // The listed version under another zip's sha256 (an earlier plugin of the same name,
      // deleted and made again), or with none (the URL form before #6931's review).
      `${new URL(FEED).pathname}/plugins/greeter/versions/3/${GONE_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/3/${ZIP_SHA.toUpperCase()}.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/versions/3.zip`,
      // An archive that is not the pin, a pin whose mirror is gone, a git package, a hosted one.
      `${new URL(FEED).pathname}/plugins/zipped/archives/${TARBALL_SHA}.zip`,
      // A tarball pin's URL names the served zip's sha256, not the pin's.
      `${new URL(FEED).pathname}/plugins/tarred/archives/${TARBALL_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/unmirrored/archives/${GONE_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/mono/archives/${ARCHIVE_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/greeter/archives/${ARCHIVE_SHA}.zip`,
      `${new URL(FEED).pathname}/plugins/zipped/archives/${ARCHIVE_SHA.toUpperCase()}.zip`,
    ]) {
      expect((await get(`${PUBLIC}${path}`, bearer(token))).status, path).toBe(404)
    }
  })

  it('answers 502 for an archive whose mirror cannot be read, and keeps the failure', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    feeds.mirror = async (name: string) => {
      feeds.mirrorReads.push(name)
      throw new Error('the mirrored copy does not hash to it')
    }
    const url = `${FEED}/plugins/zipped/archives/${ARCHIVE_SHA}.zip`
    expect((await get(url, bearer(token))).status).toBe(502)
    expect((await get(url, bearer(token))).status).toBe(502)
    expect(feeds.mirrorReads).toEqual(['zipped'])
  })

  it('serves no zip whose bytes differ from the recorded sha256', async () => {
    const { tokens, get, feeds } = made()
    const { token } = await tokens.mint({ name: 'x', tier: 'read' })
    feeds.zips.set('greeter@3', { bytes: new Uint8Array([1, 2, 3]) })
    expect((await get(`${FEED}/plugins/greeter/versions/3/${ZIP_SHA}.zip`, bearer(token))).status).toBe(502)
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
    expect(view.packages.map((p) => [p.name, p.listed, p.reason, p.kind, p.version, p.archive, p.allow_refused])).toEqual([
      ['greeter', true, null, 'hosted', 3, null, false],
      ['mono', true, null, 'git', null, null, false],
      ['plain', true, null, 'git', null, null, false],
      ['waiting', false, 'not-approved', null, null, null, false],
      ['sha256-repo', false, 'unpinnable', null, null, null, false],
      ['lost', false, 'no-version', null, null, null, false],
      ['from-url', true, null, 'git', null, null, false],
      ['zipped', true, null, 'archive', null, ARCHIVE_SHA, false],
      ['tarred', true, null, 'archive', null, TARBALL_SHA, true],
      ['unmirrored', false, 'no-mirror', null, null, null, false],
    ])
  })

  it('is a UI read: refused from another origin', async () => {
    const { get } = made()
    expect((await get(FEED, { ...UI, origin: 'https://evil.test' })).status).toBe(403)
    expect((await get(`${PUBLIC}/api/v1/ai/plugin-feeds/other`, UI)).status).toBe(404)
  })
})

describe("Settings' writes of the feed", () => {
  const write = (app: ReturnType<typeof made>, method: string, url: string, headers: Record<string, string> = UI) =>
    appFetch(app.app, { address: INGRESS, headers: { ...HTTPS, 'content-type': 'application/json', ...headers } })(url, {
      method,
      ...(method === 'PUT' ? { body: '{}' } : {}),
    })

  it('adds an installed package and takes it out again', async () => {
    const app = made()
    const url = `${FEED}/packages/outside`
    const added = await write(app, 'PUT', url)
    expect(added.status).toBe(201)
    expect(await added.json()).toEqual({ name: 'outside', added: true })
    expect(app.feeds.members.has('outside')).toBe(true)
    expect((await write(app, 'PUT', url)).status).toBe(200)
    expect((await write(app, 'DELETE', url)).status).toBe(204)
    expect(app.feeds.members.has('outside')).toBe(false)
    expect((await write(app, 'DELETE', url)).status).toBe(204)
  })

  it('answers 404 for a feed or package that is not there', async () => {
    const app = made()
    const nothing = await write(app, 'PUT', `${FEED}/packages/nothing`)
    expect(nothing.status).toBe(404)
    expect(((await nothing.json()) as { reason: string }).reason).toBe('package-not-found')
    const other = await write(app, 'PUT', `${PUBLIC}/api/v1/ai/plugin-feeds/other/packages/outside`)
    expect(((await other.json()) as { reason: string }).reason).toBe('feed-not-found')
    expect((await write(app, 'PUT', `${FEED}/packages/bad%20name`)).status).toBe(404)
  })

  it('answers 503 while the API does not serve the write yet (a rolling deploy)', async () => {
    const app = made()
    app.feeds.notServed = true
    for (const method of ['PUT', 'DELETE']) {
      const res = await write(app, method, `${FEED}/packages/outside`)
      expect(res.status, method).toBe(503)
      expect(((await res.json()) as { reason: string }).reason).toBe('not-served')
    }
  })

  it('is a UI write: refused from another origin, with no Origin, or with a token', async () => {
    const app = made()
    const { token } = await app.tokens.mint({ name: 'x', tier: 'outward' })
    const url = `${FEED}/packages/outside`
    expect((await write(app, 'PUT', url, { ...UI, origin: 'https://evil.test' })).status).toBe(403)
    expect((await write(app, 'PUT', url, { ...HTTPS, ...bearer(token) })).status).toBe(403)
    expect((await write(app, 'DELETE', url, HTTPS)).status).toBe(403)
    expect((await write(app, 'PUT', url, { ...UI, 'content-type': 'text/plain' })).status).toBe(403)
    expect(app.feeds.members.has('outside')).toBe(false)
  })
})

describe('ArchiveMaker', () => {
  const zipped = pkg('zipped', bytes('archive', ARCHIVE_SHA, ''))
  const tarred = pkg('tarred', bytes('npm', TARBALL_SHA, 'package'))
  const broken = pkg('broken', bytes('archive', GONE_SHA, ''))

  /** A mirror whose reads are counted, and that throws for `GONE_SHA` while `failing` is set. */
  function mirror() {
    const state = { reads: 0, failing: true, inFlight: 0, most: 0, gate: Promise.resolve() }
    const reader = {
      async mirror(_name: string, sha: string): Promise<Buffer | undefined> {
        state.reads++
        state.inFlight++
        state.most = Math.max(state.most, state.inFlight)
        try {
          await state.gate
          if (sha === GONE_SHA) {
            if (state.failing) throw new Error('the mirrored copy does not hash to it')
            return ARCHIVE
          }
          return sha === ARCHIVE_SHA ? ARCHIVE : sha === TARBALL_SHA ? TARBALL : undefined
        } finally {
          state.inFlight--
        }
      },
    }
    return { state, reader }
  }

  it('keeps a failed build for a minute, then tries it again', async () => {
    let now = 1_000_000
    const { state, reader } = mirror()
    const maker = new ArchiveMaker(reader, { now: () => now })
    expect((await maker.resolve([broken])).get(archiveKey(broken))).toBeNull()
    expect((await maker.resolve([broken])).get(archiveKey(broken))).toBeNull()
    await expect(maker.make(broken)).rejects.toThrow(/does not hash/)
    expect(state.reads).toBe(1)
    now += ARCHIVE_FAILURE_KEPT_MS + 1
    state.failing = false
    expect((await maker.resolve([broken])).get(archiveKey(broken))).toBe(ARCHIVE_SHA)
    expect(state.reads).toBe(2)
  })

  it('does not keep a missing mirror', async () => {
    const { state, reader } = mirror()
    const maker = new ArchiveMaker(reader)
    const none = pkg('none', bytes('archive', 'a'.repeat(64), ''))
    expect(await maker.make(none)).toBeUndefined()
    expect(await maker.make(none)).toBeUndefined()
    expect(state.reads).toBe(2)
  })

  it('runs at most the limit of builds at once, and shares one build per pin', async () => {
    const { state, reader } = mirror()
    let open!: () => void
    state.gate = new Promise<void>((resolve) => (open = resolve))
    state.failing = false
    const maker = new ArchiveMaker(reader, { buildsAtOnce: 2 })
    const rows = [zipped, tarred, broken, pkg('other', bytes('archive', ARCHIVE_SHA, ''))]
    const reads = Promise.all([maker.resolve(rows), maker.make(tarred), maker.make(tarred)])
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(state.inFlight).toBe(2)
    open()
    const [made, a, b] = await reads
    expect(state.most).toBe(2)
    expect(state.reads).toBe(4)
    expect(a).toBe(b)
    expect(made.get(archiveKey(tarred))).toBe(TARBALL_ZIP_SHA)
  })

  it('answers a download from the zip a feed read made', async () => {
    const { state, reader } = mirror()
    const maker = new ArchiveMaker(reader)
    await maker.resolve([zipped, tarred])
    expect((await maker.make(tarred))!.sha256).toBe(TARBALL_ZIP_SHA)
    expect((await maker.make(zipped))!.bytes).toEqual(ARCHIVE)
    expect(state.reads).toBe(2)
  })
})
