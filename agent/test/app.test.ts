import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp, type Health } from '../src/app.js'
import type { Credential } from '../src/credentials.js'
import type { ConnectionTest } from '../src/harness/testConnection.js'
import { originPolicy } from '../src/http/origins.js'
import { kekFromBase64, type KekStatus, loadKek } from '../src/secrets.js'
import { MemoryCredentials } from './support/memoryCredentials.js'

const up = () => Promise.resolve(true)
const down = () => Promise.resolve(false)
const kek = kekFromBase64(randomBytes(32).toString('base64'))
const withKek: KekStatus = { ok: true, kek }
const noKek: KekStatus = { ok: false, reason: 'SCADBUDDY_SECRET_KEY_FILE is not set' }
const SECRET = 'sk-ant-api03-route-test-secret-7e1c'
const GATEWAY_TOKEN = 'gw-token-route-test-0000abcd'

function deps(overrides: Partial<AppDeps> = {}): AppDeps {
  return {
    database: { ping: up, ready: up },
    backend: up,
    kek: withKek,
    credentials: new MemoryCredentials(),
    testConnection: () => Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' }),
    // The ingress: a trusted proxy in front of https://scadbuddy.example.
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    resolveHost: () => Promise.resolve(['203.0.113.10']),
    testCooldownMs: 0,
    ...overrides,
  }
}

async function health(app: ReturnType<typeof createApp>): Promise<{ status: number; body: Health }> {
  const res = await app.request('/healthz')
  return { status: res.status, body: (await res.json()) as Health }
}

/** Headers of a request the UI makes through the TLS ingress. */
const UI = {
  host: 'scadbuddy.example',
  origin: 'https://scadbuddy.example',
  'x-forwarded-proto': 'https',
}

function put(app: ReturnType<typeof createApp>, body: unknown, headers: Record<string, string> = UI) {
  return app.request('/api/v1/ai/credentials', {
    method: 'PUT',
    headers: { ...headers, 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
}

describe('GET /healthz', () => {
  it('reports AI disabled when no database is configured', async () => {
    const { status, body } = await health(createApp(deps({ database: undefined, credentials: undefined, kek: noKek })))
    expect(status).toBe(200)
    expect(body).toEqual({
      status: 'ok',
      ai: 'disabled (no database)',
      database: 'not configured',
      backend: 'ok',
      secret_key: 'not configured',
      credential: 'unknown',
    })
  })

  it('stays 200 and says why when the database or backend is down', async () => {
    const { status, body } = await health(createApp(deps({ database: { ping: down, ready: up }, backend: down })))
    expect(status).toBe(200)
    expect(body).toMatchObject({
      ai: 'unavailable (database unreachable)',
      database: 'unreachable',
      backend: 'unreachable',
    })
  })

  it('says when migrations have not applied', async () => {
    const { body } = await health(createApp(deps({ database: { ping: up, ready: down } })))
    expect(body.ai).toBe('unavailable (database migrations failed)')
  })

  it('names the missing key-encryption key', async () => {
    const { body } = await health(createApp(deps({ kek: noKek })))
    expect(body.ai).toBe('disabled (no key-encryption key: SCADBUDDY_SECRET_KEY_FILE is not set)')
    expect(body.secret_key).toBe('not configured')
  })

  it('needs a credential', async () => {
    const { body } = await health(createApp(deps()))
    expect(body).toMatchObject({ ai: 'disabled (no Claude credential)', credential: 'not configured' })
  })

  it('is enabled only with database, key and credential', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
    const { body } = await health(createApp(deps({ credentials })))
    expect(body).toMatchObject({ ai: 'enabled', database: 'ok', secret_key: 'ok', credential: 'configured' })
  })

  it('notices a credential sealed under a different key', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kekFromBase64(randomBytes(32).toString('base64')))
    const { body } = await health(createApp(deps({ credentials })))
    expect(body.ai).toBe('unavailable (stored credential was sealed with a different key-encryption key)')
  })
})

describe('/api/v1/ai/credentials', () => {
  it('reads as unconfigured, and says whether saving is possible', async () => {
    const res = await createApp(deps({ kek: noKek })).request('/api/v1/ai/credentials')
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({
      configured: false,
      kind: null,
      base_url: null,
      last4: null,
      updated_at: null,
      usable: false,
      can_save: false,
      cannot_save_reason: 'no key-encryption key: SCADBUDDY_SECRET_KEY_FILE is not set',
    })
  })

  it('saves an API key and never returns it', async () => {
    const app = createApp(deps())
    const saved = await put(app, { kind: 'anthropic_api_key', secret: SECRET })
    expect(saved.status).toBe(200)
    const text = await saved.text()
    expect(text).not.toContain(SECRET)
    expect(JSON.parse(text)).toMatchObject({ configured: true, kind: 'anthropic_api_key', last4: '7e1c', usable: true })

    const read = await app.request('/api/v1/ai/credentials')
    const readText = await read.text()
    expect(readText).not.toContain(SECRET)
    expect(readText).not.toContain('kekId')
    expect(JSON.parse(readText)).toMatchObject({ configured: true, kind: 'anthropic_api_key', base_url: null })
  })

  it('saves a gateway and normalises its base URL', async () => {
    const app = createApp(deps())
    const res = await put(app, { kind: 'gateway', base_url: 'https://llm.example/anthropic/', secret: GATEWAY_TOKEN })
    expect(await res.json()).toMatchObject({ kind: 'gateway', base_url: 'https://llm.example/anthropic', last4: 'abcd' })
  })

  it('keeps the stored secret when PUT omits it, but not for a new destination', async () => {
    const credentials = new MemoryCredentials()
    const app = createApp(deps({ credentials }))
    await put(app, { kind: 'gateway', base_url: 'https://llm.example', secret: GATEWAY_TOKEN })
    const before = credentials.row?.envelope

    const same = await put(app, { kind: 'gateway', base_url: 'https://llm.example' })
    expect(same.status).toBe(200)
    expect(credentials.row?.envelope).toBe(before)

    const moved = await put(app, { kind: 'gateway', base_url: 'https://attacker.example' })
    expect(moved.status).toBe(409)
    expect(await moved.json()).toEqual({ detail: expect.stringMatching(/needs the secret again/) })
    const kind = await put(app, { kind: 'anthropic_api_key' })
    expect(kind.status).toBe(409)
    expect((await credentials.reveal(kek))?.secret).toBe(GATEWAY_TOKEN)
  })

  it('refuses to save without a key-encryption key, and says why', async () => {
    const res = await put(createApp(deps({ kek: noKek })), { kind: 'anthropic_api_key', secret: SECRET })
    expect(res.status).toBe(503)
    expect(await res.json()).toEqual({ detail: expect.stringMatching(/no key-encryption key.*SCADBUDDY_SECRET_KEY_FILE/) })
  })

  it.each([
    [{ kind: 'gateway', secret: GATEWAY_TOKEN }, /needs base_url/],
    [{ kind: 'gateway', base_url: 'ftp://llm.example', secret: GATEWAY_TOKEN }, /http\(s\)/],
    [{ kind: 'gateway', base_url: 'https://u:p@llm.example', secret: GATEWAY_TOKEN }, /must not carry credentials/],
    [{ kind: 'anthropic_api_key', base_url: 'https://llm.example', secret: SECRET }, /applies to kind "gateway" only/],
    [{ kind: 'anthropic_api_key', secret: '  ' }, /empty/],
    [{ kind: 'anthropic_api_key' }, /secret is required/],
    [{ kind: 'bedrock', secret: SECRET }, /kind/],
    [{ kind: 'anthropic_api_key', secret: SECRET, extra: 1 }, /extra|Unrecognized/i],
  ])('rejects %j', async (body, detail) => {
    const res = await put(createApp(deps()), body)
    expect(res.status).toBe(400)
    expect(((await res.json()) as { detail: string }).detail).toMatch(detail)
  })

  it('deletes', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
    const res = await createApp(deps({ credentials })).request('/api/v1/ai/credentials', { method: 'DELETE', headers: UI })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ configured: false })
    expect(credentials.row).toBeUndefined()
  })

  it('answers 503 with the reason when there is no database', async () => {
    const app = createApp(deps({ database: undefined, credentials: undefined }))
    const res = await app.request('/api/v1/ai/credentials')
    expect(res.status).toBe(503)
    expect(await res.json()).toEqual({ detail: expect.stringMatching(/need the database: SCADBUDDY_DATABASE_URL/) })
  })

  describe('writes only through the UI ingress path (interim, until #258)', () => {
    const body = { kind: 'anthropic_api_key', secret: SECRET }

    it.each([
      ['plain HTTP through the ingress', { ...UI, 'x-forwarded-proto': 'http' }],
      ['no forwarded proto from a non-loopback peer', { host: UI.host, origin: UI.origin }],
      ['no Origin', { host: UI.host, 'x-forwarded-proto': 'https' }],
      ['a cross-origin page', { ...UI, origin: 'https://evil.example' }],
      ['a malformed Origin', { ...UI, origin: 'null' }],
    ])('refuses %s', async (_name, headers) => {
      const credentials = new MemoryCredentials()
      const res = await put(createApp(deps({ credentials })), body, headers)
      expect(res.status).toBe(403)
      expect(credentials.row).toBeUndefined()
    })

    it('refuses a non-JSON body', async () => {
      const res = await createApp(deps()).request('/api/v1/ai/credentials', {
        method: 'PUT',
        headers: { ...UI, 'content-type': 'text/plain' },
        body: JSON.stringify(body),
      })
      expect(res.status).toBe(403)
    })

    it('guards DELETE and the connection test too', async () => {
      const app = createApp(deps())
      for (const [method, path] of [
        ['DELETE', '/api/v1/ai/credentials'],
        ['POST', '/api/v1/ai/credentials/test'],
      ] as const) {
        const res = await app.request(path, { method, headers: { host: UI.host } })
        expect(res.status).toBe(403)
      }
    })

    it('accepts loopback without the ingress (local development)', async () => {
      const app = createApp(deps({ remoteAddress: () => '127.0.0.1' }))
      const res = await put(app, body, { host: 'localhost:8081', origin: 'http://localhost:8081' })
      expect(res.status).toBe(200)
    })

    it('uses X-Forwarded-Host when the ingress sets it', async () => {
      const res = await put(createApp(deps()), body, {
        ...UI,
        host: 'scadbuddy-agent:8081',
        'x-forwarded-host': 'scadbuddy.example',
      })
      expect(res.status).toBe(200)
    })
  })

  describe('POST /test', () => {
    it('runs the connection test with the decrypted credential and returns no secret', async () => {
      const credentials = new MemoryCredentials()
      await credentials.put({ kind: 'gateway', base_url: 'https://llm.example', secret: GATEWAY_TOKEN }, kek)
      const testConnection = vi.fn(
        (_c: Credential): Promise<ConnectionTest> =>
          Promise.resolve({ ok: true, detail: 'connected', duration_ms: 5, model: 'claude-x' }),
      )
      const res = await createApp(deps({ credentials, testConnection })).request('/api/v1/ai/credentials/test', {
        method: 'POST',
        headers: UI,
      })
      expect(res.status).toBe(200)
      const text = await res.text()
      expect(text).not.toContain(GATEWAY_TOKEN)
      expect(JSON.parse(text)).toEqual({ ok: true, detail: 'connected', duration_ms: 5, model: 'claude-x' })
      expect(testConnection).toHaveBeenCalledWith({ kind: 'gateway', baseUrl: 'https://llm.example', secret: GATEWAY_TOKEN })
    })

    it('is 404 without a credential and 409 when it was sealed under another key', async () => {
      const credentials = new MemoryCredentials()
      const app = createApp(deps({ credentials }))
      expect((await app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })).status).toBe(404)
      await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kekFromBase64(randomBytes(32).toString('base64')))
      const res = await app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })
      expect(res.status).toBe(409)
      expect(await res.text()).not.toContain(SECRET)
    })
  })
})

describe('/healthz is bounded (review of #354, finding 3)', () => {
  const never = () => new Promise<boolean>(() => {})

  it('reports a hung migration step as unavailable instead of hanging', async () => {
    const started = Date.now()
    const { status, body } = await health(createApp(deps({ database: { ping: up, ready: never }, healthTimeoutMs: 50 })))
    expect(status).toBe(200)
    expect(body).toMatchObject({ ai: 'unavailable (database timed out)', credential: 'unknown' })
    expect(Date.now() - started).toBeLessThan(1500)
  })

  it('reports a hung credential read as unavailable instead of hanging', async () => {
    const credentials = new MemoryCredentials()
    credentials.hang = new Promise(() => {})
    const { body } = await health(createApp(deps({ credentials, healthTimeoutMs: 50 })))
    expect(body.ai).toBe('unavailable (database timed out)')
  })
})

describe('health and settings name the key problem, not the key file (finding 9)', () => {
  it('gives a generic reason in /healthz and cannot_save_reason; the path and errno stay in the log detail', async () => {
    const missing = await loadKek('/run/secrets/some/where/scadbuddy.key')
    expect(missing.ok).toBe(false)
    if (missing.ok) return
    expect(missing.detail).toMatch(/some\/where.*ENOENT/)
    const app = createApp(deps({ kek: missing }))
    const healthText = JSON.stringify((await health(app)).body)
    const getText = await (await app.request('/api/v1/ai/credentials')).text()
    for (const text of [healthText, getText]) {
      expect(text).toContain('SCADBUDDY_SECRET_KEY_FILE cannot be read')
      expect(text).not.toContain('some/where')
      expect(text).not.toContain('ENOENT')
    }
  })
})

describe('a credential in #354 format (finding 2, clean break)', () => {
  it('is reported as outdated and refused by the connection test', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
    // What #354 wrote: version byte 0x01.
    credentials.row!.envelope.secretSealed[0] = 0x01
    const app = createApp(deps({ credentials }))
    expect((await health(app)).body.ai).toBe('unavailable (stored credential is in an outdated format; save it again)')
    expect(await (await app.request('/api/v1/ai/credentials')).json()).toMatchObject({ usable: false })
    const res = await app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })
    expect(res.status).toBe(409)
    expect(await res.json()).toEqual({ detail: expect.stringMatching(/older format.*save it again/) })
    // A PUT without the secret cannot keep it either.
    expect((await put(app, { kind: 'anthropic_api_key' })).status).toBe(409)
  })
})

describe('origin allowlist and DNS rebinding (finding 1)', () => {
  const body = { kind: 'anthropic_api_key', secret: SECRET }
  /** What a rebinding page sends: its own name in Host and Origin, HTTPS claimed, JSON set by page JS. */
  const REBOUND = { host: 'evil.test:8081', origin: 'http://evil.test:8081', 'x-forwarded-proto': 'https' }

  async function status(overrides: Partial<AppDeps>, headers: Record<string, string>): Promise<number> {
    const credentials = new MemoryCredentials()
    const res = await put(createApp(deps({ credentials, ...overrides })), body, headers)
    if (res.status !== 200) expect(credentials.row).toBeUndefined()
    return res.status
  }

  it.each([
    ['a LAN peer', '192.168.1.50'],
    ['loopback', '127.0.0.1'],
    ['the trusted proxy', '10.0.0.7'],
  ])('refuses a rebound request from %s', async (_name, peer) => {
    expect(await status({ remoteAddress: () => peer }, REBOUND)).toBe(403)
  })

  it('refuses a rebound request when no public URL is configured (loopback only)', async () => {
    const origins = originPolicy(undefined, undefined)
    expect(await status({ origins, remoteAddress: () => '127.0.0.1' }, REBOUND)).toBe(403)
    expect(await status({ origins, remoteAddress: () => '192.168.1.50' }, REBOUND)).toBe(403)
  })

  it('refuses an allowed Origin sent to a Host that is not on the list', async () => {
    expect(await status({}, { ...UI, host: 'evil.test' })).toBe(403)
    expect(await status({}, { ...UI, host: 'scadbuddy-agent:8081', 'x-forwarded-host': 'evil.test' })).toBe(403)
  })

  it('accepts the UI through the trusted proxy', async () => {
    expect(await status({}, UI)).toBe(200)
  })

  it('ignores forwarded headers from a peer that is not a trusted proxy', async () => {
    // The UI's exact headers, but from a LAN peer: X-Forwarded-Proto is not believed.
    expect(await status({ remoteAddress: () => '192.168.1.50' }, UI)).toBe(403)
    // From loopback, X-Forwarded-Host is not believed either: Host is localhost.
    expect(
      await status(
        { remoteAddress: () => '127.0.0.1' },
        { ...UI, host: 'localhost:8081', 'x-forwarded-host': 'scadbuddy.example' },
      ),
    ).toBe(403)
    // With no trusted proxies configured, the ingress's own address is just a peer.
    expect(await status({ origins: originPolicy('https://scadbuddy.example', undefined) }, UI)).toBe(403)
  })

  it('uses the last X-Forwarded-Proto value, the one the proxy appended', async () => {
    expect(await status({}, { ...UI, 'x-forwarded-proto': 'https, http' })).toBe(403)
    expect(await status({}, { ...UI, 'x-forwarded-proto': 'http, https' })).toBe(200)
  })

  it('normalises default ports on both sides', async () => {
    expect(await status({}, { ...UI, host: 'scadbuddy.example:443' })).toBe(200)
    expect(await status({}, { ...UI, origin: 'https://scadbuddy.example:443' })).toBe(200)
    expect(
      await status({ origins: originPolicy('https://SCADBUDDY.example:443/some/path', '10.0.0.0/8') }, UI),
    ).toBe(200)
    expect(await status({}, { ...UI, host: 'scadbuddy.example:8443' })).toBe(403)
  })

  it('accepts the loopback pair, including IPv6, only from a loopback peer', async () => {
    const origins = originPolicy(undefined, undefined)
    const v6 = { host: '[::1]:8081', origin: 'http://[::1]:8081' }
    expect(await status({ origins, remoteAddress: () => '::1' }, v6)).toBe(200)
    const local = { host: 'localhost:8081', origin: 'http://localhost:8081' }
    expect(await status({ origins, remoteAddress: () => '::ffff:127.0.0.1' }, local)).toBe(200)
    expect(await status({ origins, remoteAddress: () => '192.168.1.50' }, local)).toBe(403)
    // Another local app's page (a different port) is a different origin.
    expect(
      await status({ origins, remoteAddress: () => '127.0.0.1' }, { ...local, origin: 'http://localhost:3000' }),
    ).toBe(403)
  })
})

describe('gateway base_url egress check (finding 6)', () => {
  async function save(baseUrl: string, resolved: string[] | Error = ['203.0.113.10']) {
    const credentials = new MemoryCredentials()
    const resolveHost = () => (resolved instanceof Error ? Promise.reject(resolved) : Promise.resolve(resolved))
    const res = await put(createApp(deps({ credentials, resolveHost })), {
      kind: 'gateway',
      base_url: baseUrl,
      secret: GATEWAY_TOKEN,
    })
    return { status: res.status, detail: ((await res.json()) as { detail?: string }).detail, credentials }
  }

  it.each([
    ['the metadata address', 'http://169.254.169.254/latest'],
    ['another link-local address', 'https://169.254.10.1'],
    ['IPv6 link-local', 'http://[fe80::1]:4000'],
    ['an IPv4-mapped link-local', 'http://[::ffff:169.254.169.254]'],
    ["AWS's IPv6 metadata", 'http://[fd00:ec2::254]'],
    ["GCP's metadata name", 'http://metadata.google.internal/computeMetadata'],
    ["Alibaba's metadata", 'http://100.100.100.200'],
  ])('refuses %s', async (_name, url) => {
    const { status, credentials } = await save(url)
    expect(status).toBe(400)
    expect(credentials.row).toBeUndefined()
  })

  it('checks what the name resolves to, not only the literal', async () => {
    const { status, detail } = await save('https://llm.example', ['203.0.113.10', '169.254.169.254'])
    expect(status).toBe(400)
    expect(detail).toMatch(/resolves to 169\.254\.169\.254/)
    expect((await save('https://llm.example', new Error('ENOTFOUND'))).status).toBe(400)
  })

  it.each([
    ['a private LAN gateway', 'http://10.1.2.3:4000', ['10.1.2.3']],
    ['a loopback gateway', 'http://localhost:4000', ['127.0.0.1', '::1']],
    ['a cluster-local name', 'http://litellm.ai.svc.cluster.local:4000', ['10.96.0.12']],
    ['a public gateway', 'https://llm.example/anthropic', ['203.0.113.10']],
  ])('allows %s', async (_name, url, resolved) => {
    expect((await save(url, resolved)).status).toBe(200)
  })

  it('checks again when the connection test runs', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'gateway', base_url: 'https://llm.example', secret: GATEWAY_TOKEN }, kek)
    const testConnection = vi.fn(() => Promise.resolve({ ok: true, detail: 'connected', duration_ms: 1, model: 'm' }))
    const app = createApp(deps({ credentials, testConnection, resolveHost: () => Promise.resolve(['169.254.169.254']) }))
    const res = await app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })
    expect(res.status).toBe(400)
    expect(testConnection).not.toHaveBeenCalled()
  })
})

describe('POST /test is single-flight with a cooldown (finding 7)', () => {
  it('refuses a second test while one runs, and within the cooldown after it', async () => {
    const credentials = new MemoryCredentials()
    await credentials.put({ kind: 'anthropic_api_key', secret: SECRET }, kek)
    let release!: () => void
    const gate = new Promise<void>((resolve) => (release = resolve))
    const testConnection = vi.fn(async (): Promise<ConnectionTest> => {
      await gate
      return { ok: true, detail: 'connected', duration_ms: 1, model: 'm' }
    })
    let clock = 1_000_000
    const app = createApp(deps({ credentials, testConnection, testCooldownMs: 10_000, now: () => clock }))
    const test = () => app.request('/api/v1/ai/credentials/test', { method: 'POST', headers: UI })

    const first = test()
    await vi.waitFor(() => expect(testConnection).toHaveBeenCalledTimes(1))
    const concurrent = await test()
    expect(concurrent.status).toBe(429)
    expect(concurrent.headers.get('retry-after')).toBe('10')
    release()
    expect((await first).status).toBe(200)

    clock += 4_000
    const soon = await test()
    expect(soon.status).toBe(429)
    expect(soon.headers.get('retry-after')).toBe('6')
    clock += 6_000
    expect((await test()).status).toBe(200)
    expect(testConnection).toHaveBeenCalledTimes(2)
  })
})

describe('/api/v1/ai/approvals (#258; the store is covered in test/approvals.pg.test.ts)', () => {
  const id = '00000000-0000-4000-8000-000000000001'

  it('answers 503 without the database, and guards the writes first', async () => {
    const app = createApp(deps({ database: undefined, credentials: undefined }))
    expect((await app.request('/api/v1/ai/approvals')).status).toBe(503)
    const bare = await app.request(`/api/v1/ai/approvals/${id}/approve`, { method: 'POST' })
    expect(bare.status).toBe(403)
    expect(await bare.json()).toEqual({ detail: 'approval decisions must come through the HTTPS ingress' })
    expect((await app.request(`/api/v1/ai/approvals/${id}/approve`, { method: 'POST', headers: UI })).status).toBe(503)
  })
})
