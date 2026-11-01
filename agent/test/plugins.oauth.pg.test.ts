import { randomBytes } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AgentApp, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { forwardForRun, PluginForwarder } from '../src/plugins/forwarder.js'
import { PluginOAuth } from '../src/plugins/oauthStore.js'
import { loadEnabledPlugins, PluginStore } from '../src/plugins/registry.js'
import { testPlugin } from '../src/plugins/testConnection.js'
import type { PluginView } from '../src/routes/plugins.js'
import { kekFromBase64 } from '../src/secrets.js'
import { type FakeAuthOptions, type FakeAuthServer, startFakeAuthServer, startProtectedMcp } from './support/fakeOAuth.js'
import type { FakeMcp } from './support/fakeMcp.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// A remote plugin's OAuth sign-in (#780) end to end on Postgres: the start route, the
// browser's authorize link and the callback, the sealed grant, the forwarder adding (and
// renewing) the access token, and a revoked grant marked for signing in again. Against
// the local fake authorization server and MCP server of test/support/fakeOAuth.ts.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
/** The authorization server's redirect back: a top-level navigation from another site. */
const CALLBACK = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' }

describe.skipIf(!TEST_DATABASE_URL)(
  `plugin OAuth sign-in on Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    let auth: FakeAuthServer
    let mcp: FakeMcp & { seen: (string | undefined)[] }
    let forwarder: PluginForwarder
    let clock: Date
    let oauth: PluginOAuth
    let store: PluginStore
    let app: AgentApp

    async function setUp(options: FakeAuthOptions = { registration: true, expiresIn: 3600 }) {
      auth = await startFakeAuthServer(options)
      mcp = await startProtectedMcp(auth)
      oauth = new PluginOAuth(db.sql, { now: () => clock })
      store = new PluginStore(db.sql, { oauth })
      app = createApp({
        database: { ping: () => Promise.resolve(true), ready: () => db.ready() },
        backend: () => Promise.resolve(true),
        kek: { ok: true, kek },
        credentials: new MemoryCredentials(),
        plugins: store,
        pluginOAuth: oauth,
        testConnection: () => Promise.resolve({ ok: true, detail: '', duration_ms: 0, model: null }),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
      await store.create({ name: 'github', url: mcp.url }, kek)
      await store.update('github', { enabled: true }, kek)
    }

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
      clock = new Date('2026-11-01T12:00:00Z')
      forwarder = await PluginForwarder.start()
    })
    afterEach(async () => {
      await forwarder.close()
      await mcp?.close()
      await auth?.close()
      await drop()
    })

    const post = (path: string, body: unknown) =>
      app.request(path, { method: 'POST', headers: { ...UI, 'content-type': 'application/json' }, body: JSON.stringify(body) })

    /** Start, open the link, let the fake server answer; the callback's request, not yet sent. */
    async function toCallback(body: unknown = {}): Promise<{ path: string; cookie: string }> {
      const start = await post('/api/v1/ai/plugins/github/oauth/start', body)
      expect(start.status, await start.clone().text()).toBe(200)
      const started = (await start.json()) as { authorize_url: string; redirect_uri: string }
      expect(started.redirect_uri).toBe('https://scadbuddy.example/api/v1/ai/plugin-oauth/callback')
      const opened = await app.request(started.authorize_url, { headers: READ })
      expect(opened.status).toBe(302)
      const cookie = (opened.headers.get('set-cookie') ?? '').split(';')[0] ?? ''
      expect(opened.headers.get('set-cookie')).toMatch(/HttpOnly/i)
      expect(opened.headers.get('set-cookie')).toMatch(/Path=\/api\/v1\/ai\/plugin-oauth/)
      const answered = await fetch(opened.headers.get('location') ?? '', { redirect: 'manual' })
      const back = new URL(answered.headers.get('location') ?? '')
      expect(back.origin).toBe('https://scadbuddy.example')
      return { path: `${back.pathname}${back.search}`, cookie }
    }

    async function signIn(body: unknown = {}): Promise<Response> {
      const { path, cookie } = await toCallback(body)
      return app.request(path, { headers: { ...CALLBACK, cookie } })
    }

    async function grantOf(): Promise<string> {
      const [row] = await db.sql<{ grant_id: string }[]>`SELECT grant_id FROM ai_plugin_oauth WHERE plugin = 'github'`
      return row!.grant_id
    }
    /** The header `instance` gives a run that loaded the plugin's current grant. */
    async function token(instance: PluginOAuth): Promise<string> {
      return (await instance.source('github', mcp.url, await grantOf(), kek).header()).value
    }

    async function view(): Promise<PluginView> {
      const res = await app.request('/api/v1/ai/plugins/github', { headers: READ })
      return (await res.json()) as PluginView
    }

    it('signs in with dynamic registration, PKCE and resource, and the forwarder sends the token', async () => {
      await setUp()
      const done = await signIn()
      expect(done.status).toBe(200)
      expect(await done.text()).toMatch(/Signed in/)
      expect(done.headers.get('cache-control')).toBe('no-store')

      // What went to the authorization server (RFC 7591, RFC 7636 S256, RFC 8707).
      expect(auth.registrations).toEqual([
        expect.objectContaining({
          redirect_uris: ['https://scadbuddy.example/api/v1/ai/plugin-oauth/callback'],
          application_type: 'web',
          grant_types: ['authorization_code', 'refresh_token'],
        }),
      ])
      const resource = `${new URL(mcp.url).origin}/mcp`
      const asked = auth.authorizations[0]!
      expect(asked.get('code_challenge_method')).toBe('S256')
      expect(asked.get('resource')).toBe(resource)
      expect(asked.get('state')?.length).toBeGreaterThanOrEqual(43)
      expect(auth.tokenRequests[0]?.get('resource')).toBe(resource)
      expect(auth.tokenRequests[0]?.get('code_verifier')?.length).toBeGreaterThanOrEqual(43)

      // The view: its state, never a token.
      const seen = await view()
      expect(seen.oauth).toMatchObject({ status: 'connected', issuer: auth.issuer, registration: 'dynamic', refreshes: true })
      const token = auth.issued[0]!
      expect(JSON.stringify(seen)).not.toContain(token)
      // Sealed at rest.
      const [row] = await db.sql<{ secret_sealed: Buffer }[]>`SELECT secret_sealed FROM ai_plugin_oauth WHERE plugin = 'github'`
      expect(row!.secret_sealed.toString('latin1')).not.toContain(token)

      // A run: Claude Code gets a loopback URL, the forwarder adds the token.
      const loaded = await loadEnabledPlugins({ enabled: (k) => store.enabled(k) }, kek, () => Promise.resolve(['127.0.0.1']))
      expect(loaded.problems).toEqual([])
      expect(loaded.plugins.map(({ plugin }) => plugin.oauth?.grant)).toEqual([expect.any(String)])
      const run = forwardForRun(loaded, forwarder)
      try {
        expect(run.plugins[0]!.url).not.toContain(token)
        expect(run.secrets).toContain(token)
      } finally {
        run.release()
      }
      const tested = await testPlugin((await store.reveal('github', kek))!, '127.0.0.1', forwarder, { timeoutMs: 5000 })
      expect(tested.ok, tested.detail).toBe(true)
      expect(mcp.seen).toContain(`Bearer ${token}`)
    })

    it('refreshes an expiring token itself, once across replicas, and rotates the refresh token', async () => {
      await setUp()
      expect((await signIn()).status).toBe(200)
      const first = await token(oauth)
      clock = new Date(clock.getTime() + 3600_000)
      const second = await token(oauth)
      expect(second).not.toBe(first)
      expect(auth.tokenRequests.map((f) => f.get('grant_type'))).toEqual(['authorization_code', 'refresh_token'])
      // Another replica reads the renewed token from the row: no second refresh.
      const other = new PluginOAuth(db.sql, { now: () => clock })
      expect(await token(other)).toBe(second)
      expect(auth.tokenRequests).toHaveLength(2)
      expect(auth.refresh.size).toBe(1)
    })

    it('marks a revoked grant for signing in again, and the run leaves the plugin out', async () => {
      await setUp()
      expect((await signIn()).status).toBe(200)
      auth.revokeAll()
      const tested = await testPlugin((await store.reveal('github', kek))!, '127.0.0.1', forwarder, { timeoutMs: 5000 })
      expect(tested.ok).toBe(false)
      expect(tested.detail).toMatch(/sign in again/)
      expect((await view()).oauth).toMatchObject({ status: 'error', error: expect.stringMatching(/invalid_grant/) as unknown })
      const { plugins, problems } = await store.enabled(kek)
      expect(plugins).toEqual([])
      expect(problems).toEqual([expect.stringMatching(/github was not loaded: it needs signing in again/)])
      // The test route says so too, without contacting anything.
      const res = await post('/api/v1/ai/plugins/github/test', undefined)
      expect(res.status).toBe(409)
      // Signing in again clears it.
      expect((await signIn()).status).toBe(200)
      expect((await view()).oauth).toMatchObject({ status: 'connected', error: null })
    })

    it('refuses a reused state, another browser and a wrong iss', async () => {
      await setUp()
      const { path, cookie } = await toCallback()
      const other = await app.request(path, { headers: { ...CALLBACK, cookie: 'sb_plugin_oauth=someone-else' } })
      expect(other.status).toBe(400)
      expect(await other.text()).toMatch(/another browser/)
      expect((await app.request(path, { headers: CALLBACK })).status).toBe(400)
      expect(auth.tokenRequests).toEqual([])
      // Neither spent it: the browser that opened the link still can, once.
      expect((await app.request(path, { headers: { ...CALLBACK, cookie } })).status).toBe(200)
      expect((await app.request(path, { headers: { ...CALLBACK, cookie } })).status).toBe(400)
      await oauth.forget('github')
      auth.tokenRequests.length = 0

      const again = await toCallback()
      const forged = again.path.replace(/iss=[^&]+/, `iss=${encodeURIComponent('https://evil.example')}`)
      const res = await app.request(forged, { headers: { ...CALLBACK, cookie: again.cookie } })
      expect(res.status).toBe(400)
      expect(await res.text()).toMatch(/\(iss\)/)
      expect(auth.tokenRequests).toEqual([])
      expect((await view()).oauth).toBeNull()
    })

    it('stops serving a cached token in another process once the plugin is signed in again or out', async () => {
      // No expires_in: the GitHub OAuth-app case, a token that would otherwise be cached forever.
      await setUp({ registration: true })
      expect((await signIn()).status).toBe(200)
      // B, another process (the worker beside the api, another replica), loads and caches the grant.
      const worker = new PluginOAuth(db.sql, { now: () => clock })
      const loaded = worker.source('github', mcp.url, await grantOf(), kek)
      const old = (await loaded.header()).value
      expect(old).toBe(`Bearer ${auth.issued[0]}`)

      // A signs in again: a run loading the new grant on B gets the new token at once, and B's
      // old source is refused once B reads the row again (RECHECK_MS).
      expect((await signIn()).status).toBe(200)
      const fresh = await token(worker)
      clock = new Date(clock.getTime() + 61_000)
      await expect(loaded.header()).rejects.toThrow(/signed in again or out/)
      expect(fresh).toBe(`Bearer ${auth.issued[1]}`)

      // A signs out: within RECHECK_MS B reads the row again and stops.
      const current = worker.source('github', mcp.url, await grantOf(), kek)
      await oauth.forget('github')
      clock = new Date(clock.getTime() + 61_000)
      await expect(current.header()).rejects.toThrow(/not signed in/)
    })

    it('spends an authorize link once', async () => {
      await setUp()
      const start = await post('/api/v1/ai/plugins/github/oauth/start', {})
      const { authorize_url } = (await start.json()) as { authorize_url: string }
      expect((await app.request(authorize_url, { headers: READ })).status).toBe(302)
      expect((await app.request(authorize_url, { headers: READ })).status).toBe(400)
      // From another site, refused.
      const cross = await app.request(authorize_url, { headers: { ...READ, 'sec-fetch-site': 'cross-site' } })
      expect(cross.status).toBe(403)
    })

    it('uses a configured client where the server offers no registration (GitHub)', async () => {
      await setUp({ client: { id: 'gh-app', secret: 'gh-secret-0123456789' }, expiresIn: 28_800 })
      const refused = await post('/api/v1/ai/plugins/github/oauth/start', {})
      expect(refused.status).toBe(409)
      expect(((await refused.json()) as { detail: string }).detail).toMatch(/register an OAuth app/)
      expect((await signIn({ client_id: 'gh-app', client_secret: 'gh-secret-0123456789' })).status).toBe(200)
      expect(auth.tokenRequests[0]?.get('client_secret')).toBe('gh-secret-0123456789')
      expect((await view()).oauth).toMatchObject({ registration: 'configured' })
      // The client secret is sealed with the grant and used for the refresh.
      clock = new Date(clock.getTime() + 28_800_000)
      await token(oauth)
      expect(auth.tokenRequests[1]?.get('client_secret')).toBe('gh-secret-0123456789')
    })

    it('keeps the header mode: a header plugin cannot start a sign-in, and a header replaces a grant', async () => {
      await setUp()
      expect((await signIn()).status).toBe(200)
      await store.update('github', { secret: 'Bearer static-token-123456' }, kek)
      expect((await view()).oauth).toBeNull()
      const refused = await post('/api/v1/ai/plugins/github/oauth/start', {})
      expect(refused.status).toBe(409)
      const opened = await store.reveal('github', kek)
      expect(opened?.header).toEqual({ name: 'Authorization', value: 'Bearer static-token-123456' })
      expect(opened?.oauth).toBeUndefined()
    })

    it('drops the grant when the URL changes, and signs out', async () => {
      await setUp()
      expect((await signIn()).status).toBe(200)
      const out = await app.request('/api/v1/ai/plugins/github/oauth', { method: 'DELETE', headers: UI })
      expect(out.status).toBe(204)
      expect((await view()).oauth).toBeNull()
      expect((await signIn()).status).toBe(200)
      await store.update('github', { url: `${mcp.url}other/` }, kek)
      expect((await view()).oauth).toBeNull()
    })
  },
)
