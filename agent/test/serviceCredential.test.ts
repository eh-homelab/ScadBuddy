import { randomBytes } from 'node:crypto'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import {
  credentialAad,
  ExchangeCredential,
  REREAD_MS,
  ServiceCredential,
  serviceCredentialMiddleware,
  withServiceCredential,
} from '../src/api/serviceCredential.js'
import { kekFromBase64, sealSecret } from '../src/secrets.js'

// #4975 phase 4: the agent's service credential, read from its view and unsealed.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const TOKEN = `sbsvc_0123456789abcdef.${'a'.repeat(43)}`

function fakeSql(rows: () => unknown[]): { sql: Sql; reads: () => number } {
  let reads = 0
  const sql = ((..._args: unknown[]) => {
    reads += 1
    return Promise.resolve(rows())
  }) as unknown as Sql
  return { sql, reads: () => reads }
}

function row(token = TOKEN) {
  const scopes = ['internal.workspace', 'api']
  const envelope = sealSecret(kek, token, credentialAad('0123456789abcdef', 'agent', scopes))
  return {
    id: '0123456789abcdef',
    caller: 'agent',
    scopes,
    secret_sealed: envelope.secretSealed,
    dek_sealed: envelope.dekSealed,
    kek_id: envelope.kekId,
  }
}

describe('ServiceCredential', () => {
  it('binds the AAD as the backend does, scopes sorted', () => {
    expect(credentialAad('id', 'agent', ['internal.workspace', 'api'])).toBe(
      'service_credentials|id|agent|api,internal.workspace',
    )
  })

  it('unseals the newest live one and reads again only after an hour', async () => {
    let now = 0
    const { sql, reads } = fakeSql(() => [row()])
    const credential = new ServiceCredential(sql, kek, () => now)
    expect(await credential.current()).toBe(TOKEN)
    expect(await credential.current()).toBe(TOKEN)
    expect(reads()).toBe(1)
    now = REREAD_MS
    await credential.current()
    expect(reads()).toBe(2)
  })

  it('sends nothing without the KEK or a credential', async () => {
    expect(await new ServiceCredential(fakeSql(() => [row()]).sql, undefined).current()).toBeUndefined()
    expect(await new ServiceCredential(fakeSql(() => []).sql, kek).current()).toBeUndefined()
  })

  it('keeps the last token when a read fails', async () => {
    let now = 0
    let fail = false
    const sql = (() => (fail ? Promise.reject(new Error('down')) : Promise.resolve([row()]))) as unknown as Sql
    const credential = new ServiceCredential(sql, kek, () => now, () => undefined)
    expect(await credential.current()).toBe(TOKEN)
    fail = true
    now = REREAD_MS
    expect(await credential.current()).toBe(TOKEN)
  })

  it('puts it on the workspace blob routes, and on nothing outside the backend', async () => {
    const seen: (string | null)[] = []
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      seen.push(new Headers(init?.headers).get('authorization'))
      return new Response('{}')
    }) as typeof fetch
    const credential = new ServiceCredential(fakeSql(() => [row()]).sql, kek)
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    await wrapped('http://backend:8080/api/v1/internal/workspace-blobs/uploads', { method: 'POST' })
    await wrapped('https://bucket.s3.example/blob?X-Amz-Signature=x', { method: 'PUT' })
    expect(seen).toEqual([`Bearer ${TOKEN}`, null])
  })

  it('puts it on the request', async () => {
    const middleware = serviceCredentialMiddleware(new ServiceCredential(fakeSql(() => [row()]).sql, kek))
    const request = new Request('http://backend/api/v1/models')
    const sent = (await middleware.onRequest!({ request } as never)) as Request
    expect(sent.headers.get('authorization')).toBe(`Bearer ${TOKEN}`)
  })
})

// #5494 (spec 2026-11-05 §6.3, §8 step 1): the exchange against a fake backend route.

type Exchange = {
  grant_type: string | null
  subject_token: string | null
  subject_token_type: string | null
  client_id: string | null
}

function fakeBackend() {
  const state = {
    status: 200,
    retryAfter: '7',
    issued: 0,
    calls: [] as Exchange[],
  }
  const fetchImpl = (async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    expect(url).toBe('http://backend:8080/api/v1/auth/service-token')
    expect(init?.method).toBe('POST')
    const form = new URLSearchParams(String(init?.body))
    state.calls.push({
      grant_type: form.get('grant_type'),
      subject_token: form.get('subject_token'),
      subject_token_type: form.get('subject_token_type'),
      client_id: form.get('client_id'),
    })
    if (state.status !== 200) {
      return new Response('{}', {
        status: state.status,
        headers: state.status === 429 ? { 'retry-after': state.retryAfter } : {},
      })
    }
    state.issued += 1
    return Response.json({
      access_token: `sbsvc_${state.issued.toString(16).padStart(16, '0')}.${'a'.repeat(43)}`,
      token_type: 'Bearer',
      expires_in: 900,
      scope: 'api crud.agent internal.workspace',
      issued_token_type: 'urn:ietf:params:oauth:token-type:access_token',
    })
  }) as typeof fetch
  return { state, fetchImpl }
}

function exchanging(
  backend: ReturnType<typeof fakeBackend>,
  clock: { now: number },
  options: { proof?: 'k8s' | 'bootstrap'; fallback?: ServiceCredential; proofText?: () => string } = {},
) {
  return new ExchangeCredential({
    backendUrl: 'http://backend:8080/',
    proofFile: '/var/run/scadbuddy/token',
    proof: options.proof ?? 'k8s',
    ...(options.fallback ? { fallback: options.fallback } : {}),
    fetchImpl: backend.fetchImpl,
    readProof: async () => (options.proofText ?? (() => 'jwt-1\n'))(),
    now: () => clock.now,
    log: () => undefined,
  })
}

describe('ExchangeCredential', () => {
  it('exchanges the projected token and renews it at half-life', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    let proof = 'jwt-1'
    const credential = exchanging(backend, clock, { proofText: () => proof })
    const first = await credential.current()
    expect(first).toMatch(/^sbsvc_/)
    expect(backend.state.calls).toEqual([
      {
        grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange',
        subject_token: 'jwt-1',
        subject_token_type: 'urn:ietf:params:oauth:token-type:jwt',
        client_id: 'agent',
      },
    ])
    clock.now = 449_000
    expect(await credential.current()).toBe(first)
    // The kubelet rotated the projected token: it is read again before each exchange.
    proof = 'jwt-2'
    clock.now = 450_000
    const second = await credential.current()
    expect(second).not.toBe(first)
    expect(backend.state.calls.at(-1)?.subject_token).toBe('jwt-2')
  })

  it('sends the bootstrap token type', async () => {
    const backend = fakeBackend()
    await exchanging(backend, { now: 0 }, { proof: 'bootstrap' }).current()
    expect(backend.state.calls[0]?.subject_token_type).toBe('urn:scadbuddy:params:token-type:bootstrap')
  })

  it('keeps the token through a failed exchange and backs off 5 s doubling to 60 s', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const credential = exchanging(backend, clock)
    const token = await credential.current()
    backend.state.status = 503
    clock.now = 450_000
    expect(await credential.current()).toBe(token)
    let calls = backend.state.calls.length
    for (const delay of [5, 10, 20, 40, 60, 60]) {
      clock.now += delay * 1000 - 500
      await credential.current()
      expect(backend.state.calls.length).toBe(calls)
      clock.now += 500
      await credential.current()
      calls += 1
      expect(backend.state.calls.length).toBe(calls)
    }
    clock.now = 900_000
    expect(await credential.current()).toBeUndefined()
  })

  it('waits for a backend not up yet without back-off, logged once, and recovers as it answers (#5868)', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    let up = false
    const logs: string[] = []
    const refused = () =>
      new TypeError('fetch failed', { cause: Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8080'), { code: 'ECONNREFUSED' }) })
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: ((input: string | URL | Request, init?: RequestInit) =>
        up ? backend.fetchImpl(input, init) : Promise.reject(refused())) as typeof fetch,
      readProof: async () => 'jwt-1',
      now: () => clock.now,
      log: (message) => logs.push(message),
    })
    for (let i = 0; i < 5; i += 1) {
      expect(await credential.current()).toBeUndefined()
      clock.now += 100
    }
    expect(logs).toEqual(["the agent's service credential: waiting for the backend, which is not up yet"])
    // No back-off: the first call once the backend answers exchanges.
    up = true
    expect(await credential.current()).toMatch(/^sbsvc_/)
    expect(backend.state.calls.length).toBe(1)
    // Down again later (a restart): the token holds, and the wait is logged once more.
    up = false
    clock.now = 451_000
    expect(await credential.current()).toMatch(/^sbsvc_/)
    expect(await credential.current()).toMatch(/^sbsvc_/)
    expect(logs.length).toBe(2)
    const logged = logs.join('\n')
    expect(logged).not.toContain('jwt-1')
    expect(logged).not.toContain('sbsvc_')
    expect(logged).not.toContain('backend:8080')
  })

  it('still backs off and logs a failure for any other transport error', async () => {
    const logs: string[] = []
    let calls = 0
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: (() => {
        calls += 1
        return Promise.reject(new TypeError('fetch failed', { cause: Object.assign(new Error('x'), { code: 'ENOTFOUND' }) }))
      }) as typeof fetch,
      readProof: async () => 'jwt-1',
      now: () => 0,
      log: (message) => logs.push(message),
    })
    await credential.current()
    await credential.current()
    expect(calls).toBe(1)
    expect(logs).toEqual(["could not exchange the agent's service credential (TypeError)"])
  })

  it('waits out a 429 for its Retry-After', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const credential = exchanging(backend, clock)
    backend.state.status = 429
    backend.state.retryAfter = '30'
    expect(await credential.current()).toBeUndefined()
    clock.now = 29_000
    await credential.current()
    expect(backend.state.calls.length).toBe(1)
    backend.state.status = 200
    clock.now = 30_000
    expect(await credential.current()).toMatch(/^sbsvc_/)
  })

  it('exchanges again at once after a 401, but not in a loop', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const credential = exchanging(backend, clock)
    const first = (await credential.current())!
    clock.now = 20_000
    credential.invalidate(first)
    const second = (await credential.current())!
    expect(second).not.toBe(first)
    credential.invalidate(second)
    expect(await credential.current()).toBeUndefined()
    expect(backend.state.calls.length).toBe(2)
    clock.now = 30_000
    expect(await credential.current()).toMatch(/^sbsvc_/)
    const calls = backend.state.calls.length
    credential.invalidate(first)
    expect(backend.state.calls.length).toBe(calls)
  })

  it('falls back to the view while an old API answers 404', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const view = new ServiceCredential(fakeSql(() => [row()]).sql, kek, () => clock.now)
    const credential = exchanging(backend, clock, { fallback: view })
    backend.state.status = 404
    expect(await credential.current()).toBe(TOKEN)
    clock.now = 30_000
    expect(await credential.current()).toBe(TOKEN)
    expect(backend.state.calls.length).toBe(1)
    backend.state.status = 200
    clock.now = 60_000
    const token = await credential.current()
    expect(token).toMatch(/^sbsvc_/)
    expect(token).not.toBe(TOKEN)
  })

  it('has nothing on a 404 without a database to fall back to', async () => {
    const backend = fakeBackend()
    backend.state.status = 404
    expect(await exchanging(backend, { now: 0 }).current()).toBeUndefined()
  })

  it('drops a token the backend answered 401 to, without sending the request again', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const credential = exchanging(backend, clock)
    const seen: (string | null)[] = []
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      seen.push(new Headers(init?.headers).get('authorization'))
      return new Response('{}', { status: seen.length === 1 ? 401 : 200 })
    }) as typeof fetch
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    expect((await wrapped('http://backend:8080/api/v1/internal/workspace-blobs/uploads', { method: 'POST' })).status).toBe(401)
    clock.now = 10_000
    expect((await wrapped('http://backend:8080/api/v1/internal/workspace-blobs/uploads', { method: 'POST' })).status).toBe(200)
    expect(seen.length).toBe(2)
    expect(seen[0]).not.toBe(seen[1])
    expect(seen[1]).toMatch(/^Bearer sbsvc_/)
  })

  it("the client's middleware drops a token the backend answered 401 to", async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const credential = exchanging(backend, clock)
    const middleware = serviceCredentialMiddleware(credential)
    const request = (await middleware.onRequest!({ request: new Request('http://backend/api/v1/models') } as never)) as Request
    const first = request.headers.get('authorization')
    await middleware.onResponse!({ request, response: new Response('{}', { status: 401 }) } as never)
    clock.now = 10_000
    const again = (await middleware.onRequest!({ request: new Request('http://backend/api/v1/models') } as never)) as Request
    expect(again.headers.get('authorization')).not.toBe(first)
  })
})
