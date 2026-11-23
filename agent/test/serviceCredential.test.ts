import { describe, expect, it, vi } from 'vitest'
import { CREDENTIAL_WAIT_MS, type CredentialSource, ExchangeCredential, serviceCredentialMiddleware, withServiceCredential } from '../src/api/serviceCredential.js'

// #4975 phase 4: the agent's service credential on its calls to the backend. It reads no view
// (#5299 step 12): the credential is only ever exchanged for the proof (below).

const TOKEN = `sbsvc_0123456789abcdef.${'a'.repeat(43)}`

/** A credential source that always has `TOKEN`. */
const fixed: CredentialSource = { current: async () => TOKEN, invalidate: () => undefined }

describe('the service credential on a call', () => {
  it('puts it on the workspace blob routes, and on nothing outside the backend', async () => {
    const seen: (string | null)[] = []
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      seen.push(new Headers(init?.headers).get('authorization'))
      return new Response('{}')
    }) as typeof fetch
    const credential = fixed
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    await wrapped('http://backend:8080/api/v1/internal/workspace-blobs/uploads', { method: 'POST' })
    await wrapped('https://bucket.s3.example/blob?X-Amz-Signature=x', { method: 'PUT' })
    expect(seen).toEqual([`Bearer ${TOKEN}`, null])
  })

  it('puts it on the request', async () => {
    const middleware = serviceCredentialMiddleware(fixed)
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
  options: { proof?: 'k8s' | 'bootstrap'; proofText?: () => string } = {},
) {
  return new ExchangeCredential({
    backendUrl: 'http://backend:8080/',
    proofFile: '/var/run/scadbuddy/token',
    proof: options.proof ?? 'k8s',
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

  it('reads no view: a 404 is a failed exchange, with no credential (#6501)', async () => {
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

  // #6647: at pod start the first exchange failed (the API not answering yet), and the calls made in
  // its back-off went out with no credential and got 401.
  function firstExchangeFails(first: { status: number; retryAfter?: string }, sleep?: (ms: number) => Promise<void>) {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const slept: number[] = []
    let exchanges = 0
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: ((input: string | URL | Request, init?: RequestInit) => {
        exchanges += 1
        if (exchanges === 1) {
          return Promise.resolve(
            new Response('{}', { status: first.status, headers: first.retryAfter ? { 'retry-after': first.retryAfter } : {} }),
          )
        }
        return backend.fetchImpl(input, init)
      }) as typeof fetch,
      readProof: async () => 'jwt-1',
      now: () => clock.now,
      log: () => undefined,
      sleep:
        sleep ??
        (async (ms) => {
          slept.push(ms)
          clock.now += ms
        }),
    })
    const seen: (string | null)[] = []
    // The backend refuses a CRUD call that has no credential.
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      const authorization = new Headers(init?.headers).get('authorization')
      seen.push(authorization)
      return new Response('{}', { status: authorization === null ? 401 : 200 })
    }) as typeof fetch
    return { credential, clock, slept, seen, inner }
  }

  it('a call made in the first back-off waits for the next exchange and carries its token (#6647)', async () => {
    const { credential, slept, seen, inner } = firstExchangeFails({ status: 503 })
    expect(await credential.current()).toBeUndefined()
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    const response = await wrapped('http://backend:8080/api/v1/internal/crud/agent/events/head')
    expect(response.status).toBe(200)
    expect(seen).toEqual([expect.stringMatching(/^Bearer sbsvc_/)])
    expect(slept).toEqual([5_000])
  })

  it("the client's middleware waits for the next exchange too (#6647)", async () => {
    const { credential, slept } = firstExchangeFails({ status: 503 })
    expect(await credential.current()).toBeUndefined()
    const middleware = serviceCredentialMiddleware(credential)
    const request = (await middleware.onRequest!({ request: new Request('http://backend/api/v1/models') } as never)) as Request
    expect(request.headers.get('authorization')).toMatch(/^Bearer sbsvc_/)
    expect(slept).toEqual([5_000])
  })

  it('does not wait past the bound: the call goes out as before (#6647)', async () => {
    const { credential, slept, seen, inner } = firstExchangeFails({ status: 429, retryAfter: String(CREDENTIAL_WAIT_MS / 1000 + 1) })
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    expect((await wrapped('http://backend:8080/api/v1/internal/crud/agent/events/head')).status).toBe(401)
    expect(seen).toEqual([null])
    expect(slept).toEqual([])
  })

  it("ends the wait when the call's own signal aborts, not at the next exchange (#7099 review)", async () => {
    // A sleep that never ends: only the abort can end the wait.
    const { credential, seen } = firstExchangeFails({ status: 503 }, () => new Promise(() => {}))
    expect(await credential.current()).toBeUndefined()
    const inner = (async (_input: string | URL | Request, init?: RequestInit) => {
      init?.signal?.throwIfAborted()
      seen.push(new Headers(init?.headers).get('authorization'))
      return new Response('{}')
    }) as typeof fetch
    const wrapped = withServiceCredential(inner, credential, 'http://backend:8080/')
    const started = Date.now()
    const call = wrapped('http://backend:8080/api/v1/internal/crud/agent/events/head', { signal: AbortSignal.timeout(50) })
    await expect(call).rejects.toMatchObject({ name: 'TimeoutError' })
    expect(Date.now() - started).toBeLessThan(2_000)
    expect(seen).toEqual([])

    const controller = new AbortController()
    const middleware = serviceCredentialMiddleware(credential)
    const pending = middleware.onRequest!({ request: new Request('http://backend/api/v1/models', { signal: controller.signal }) } as never)
    controller.abort()
    const request = (await pending) as Request
    expect(request.headers.get('authorization')).toBeNull()
  })

  it('a call made during a slow first exchange still waits for the retry (#7099 review)', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const slept: number[] = []
    let exchanges = 0
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: ((input: string | URL | Request, init?: RequestInit) => {
        exchanges += 1
        if (exchanges === 1) {
          // The first exchange takes its whole 10 s timeout, then fails.
          clock.now += 10_000
          return Promise.reject(Object.assign(new Error('timed out'), { name: 'TimeoutError' }))
        }
        return backend.fetchImpl(input, init)
      }) as typeof fetch,
      readProof: async () => 'jwt-1',
      now: () => clock.now,
      log: () => undefined,
      sleep: async (ms) => {
        slept.push(ms)
        clock.now += ms
      },
    })
    expect(await credential.untilCurrent()).toMatch(/^sbsvc_/)
    expect(slept).toEqual([5_000])
  })

  it('the default wait keeps a one-shot process alive, and an abort clears its timer (#7099 review, #7144)', async () => {
    const credential = new ExchangeCredential({ backendUrl: 'http://backend:8080/', proofFile: '/x', proof: 'k8s' })
    const sleep = (credential as unknown as { sleep: (ms: number, signal?: AbortSignal) => Promise<void> }).sleep
    const set = vi.spyOn(globalThis, 'setTimeout')
    const clear = vi.spyOn(globalThis, 'clearTimeout')
    try {
      const controller = new AbortController()
      const slept = sleep(60_000, controller.signal)
      const timer = set.mock.results.at(-1)?.value as NodeJS.Timeout
      expect(timer.hasRef()).toBe(true)
      controller.abort()
      await slept
      expect(clear).toHaveBeenCalledWith(timer)
    } finally {
      set.mockRestore()
      clear.mockRestore()
    }
  })

  it('a call soon after a 401 waits out the re-exchange hold-off and carries the new token (#7144)', async () => {
    const backend = fakeBackend()
    const clock = { now: 0 }
    const slept: number[] = []
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: backend.fetchImpl,
      readProof: async () => 'jwt-1',
      now: () => clock.now,
      log: () => undefined,
      sleep: async (ms) => {
        slept.push(ms)
        clock.now += ms
      },
    })
    const first = (await credential.current())!
    // A 401 7 s after the exchange: the next exchange is held off until 10 s after it.
    clock.now = 7_000
    credential.invalidate(first)
    const second = await credential.untilCurrent()
    expect(slept).toEqual([3_000])
    expect(second).toMatch(/^sbsvc_/)
    expect(second).not.toBe(first)
    expect(backend.state.calls.length).toBe(2)
  })

  it('counts the exchange after the wait in the bound (#7099 review)', async () => {
    // A next exchange due in 6 s, plus its own 10 s timeout, could end past the 15 s bound.
    const { credential, slept } = firstExchangeFails({ status: 429, retryAfter: '6' })
    expect(await credential.untilCurrent()).toBeUndefined()
    expect(slept).toEqual([])
  })

  it('does not wait for a backend that is not up: that call is refused too (#6647)', async () => {
    let calls = 0
    const credential = new ExchangeCredential({
      backendUrl: 'http://backend:8080/',
      proofFile: '/var/run/scadbuddy/token',
      proof: 'k8s',
      fetchImpl: (() => {
        calls += 1
        return Promise.reject(
          new TypeError('fetch failed', { cause: Object.assign(new Error('connect ECONNREFUSED 127.0.0.1:8080'), { code: 'ECONNREFUSED' }) }),
        )
      }) as typeof fetch,
      readProof: async () => 'jwt-1',
      now: () => 0,
      log: () => undefined,
      sleep: () => Promise.reject(new Error('no wait expected')),
    })
    expect(await credential.untilCurrent()).toBeUndefined()
    expect(calls).toBe(1)
  })
})
