import { randomBytes } from 'node:crypto'
import { describe, expect, it, vi } from 'vitest'
import { type AppDeps, createApp, type Health } from '../src/app.js'
import type { Credential } from '../src/credentials.js'
import type { ConnectionTest } from '../src/harness/testConnection.js'
import { kekFromBase64, type KekStatus } from '../src/secrets.js'
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
    remoteAddress: () => '10.0.0.7',
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
