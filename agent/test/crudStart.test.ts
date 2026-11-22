import { describe, expect, it } from 'vitest'
import { CrudAreas } from '../src/api/crudAreas.js'
import { dataHealthOf } from '../src/api/dataHealth.js'
import { aiStatus } from '../src/aiStatus.js'
import { BrowserSessions, CrudSessionLookup, sessionIdHash } from '../src/auth/browserSession.js'

// What the agent reads per request through the API's CRUD routes (#6501), against a fake API
// that answers as the backend's api/crud_agent_start.py does (the backend's
// tests/crud/test_crud_agent_start.py covers the routes): a signed-in person's session by its
// cookie's hash, and health's probe of the agent's data, which is the API's (#5299 step 12).

const BASE = 'http://api:8080'
const PREFIX = '/api/v1/internal/crud/agent'

type Seen = { method: string; path: string; body: unknown }

class FakeApi {
  seen: Seen[] = []
  answers = new Map<string, { status: number; body: unknown }>()

  readonly fetch: typeof fetch = async (input, init) => {
    const request = new Request(input, init)
    const url = new URL(request.url)
    const path = `${url.pathname.slice(PREFIX.length)}${url.search}`
    const text = request.method === 'POST' ? await request.text() : ''
    this.seen.push({ method: request.method, path, body: text ? JSON.parse(text) : undefined })
    const key = `${request.method} ${path}`
    const answer = this.answers.get(key) ?? { status: 404, body: { detail: `no answer for ${key}` } }
    return new Response(JSON.stringify(answer.body), { status: answer.status, headers: { 'x-scadbuddy-crud': '1' } })
  }

  answer(key: string, body: unknown, status = 200): this {
    this.answers.set(key, { status, body })
    return this
  }
}

const target = (api: FakeApi) => ({ baseUrl: BASE, fetch: api.fetch })

describe("a signed-in person's session through the API", () => {
  const TOKEN = 'cookie-value'

  it('sends only the hash, and answers the live session or none', async () => {
    const hash = sessionIdHash(TOKEN)
    const api = new FakeApi().answer('POST /auth-sessions/lookup', { session: { principal: 'user:ada', name: 'Ada', kind: 'login' } })
    const sessions = new BrowserSessions(new CrudSessionLookup(target(api)))
    expect(await sessions.resolve(TOKEN)).toEqual({ principal: 'user:ada', name: 'Ada', kind: 'login' })
    expect(api.seen).toEqual([{ method: 'POST', path: '/auth-sessions/lookup', body: { id_hash: hash } }])
    expect(JSON.stringify(api.seen)).not.toContain(TOKEN)
    const none = new FakeApi().answer('POST /auth-sessions/lookup', { session: null })
    expect(await new BrowserSessions(new CrudSessionLookup(target(none))).resolve(TOKEN)).toBeUndefined()
  })

  it('never fails open: a refusal or an unreachable API is no session', async () => {
    const refused = new FakeApi().answer('POST /auth-sessions/lookup', { detail: 'no' }, 503)
    expect(await new BrowserSessions(new CrudSessionLookup(target(refused))).resolve(TOKEN)).toBeUndefined()
    expect(await new BrowserSessions(new CrudSessionLookup(target(refused))).live(sessionIdHash(TOKEN), 'user:ada')).toBeUndefined()
  })

  it('sends at most its cap of lookups at once, and the rest in turn', async () => {
    let inFlight = 0
    let most = 0
    const fetch: typeof globalThis.fetch = async () => {
      inFlight++
      most = Math.max(most, inFlight)
      await new Promise((resolve) => setTimeout(resolve, 5))
      inFlight--
      return new Response(JSON.stringify({ session: null }), { headers: { 'x-scadbuddy-crud': '1' } })
    }
    const lookup = new CrudSessionLookup({ baseUrl: BASE, fetch }, 3)
    const hashes = Array.from({ length: 10 }, (_, i) => sessionIdHash(`cookie-${i}`))
    expect(await Promise.all(hashes.map((h) => lookup.byHash(h)))).toEqual(hashes.map(() => undefined))
    expect(most).toBe(3)
  })

  it('fails a lookup that waits past its deadline, and the session is not cached', async () => {
    let release: () => void = () => undefined
    const fetch: typeof globalThis.fetch = async () => {
      await new Promise<void>((resolve) => (release = resolve))
      return new Response(JSON.stringify({ session: null }), { headers: { 'x-scadbuddy-crud': '1' } })
    }
    const lookup = new CrudSessionLookup({ baseUrl: BASE, fetch }, 1, 20)
    const first = lookup.byHash(sessionIdHash('cookie-a'))
    await expect(lookup.byHash(sessionIdHash('cookie-b'))).rejects.toThrow(/no lookup slot within 20 ms/)
    // The late one left the queue: the first lookup's end frees the slot, and the next one runs.
    release()
    expect(await first).toBeUndefined()
    const next = lookup.byHash(sessionIdHash('cookie-c'))
    await new Promise((resolve) => setTimeout(resolve, 0))
    release()
    expect(await next).toBeUndefined()
    // Through BrowserSessions, a late lookup is no session (never fails open).
    const busy = new CrudSessionLookup({ baseUrl: BASE, fetch: () => new Promise<Response>(() => undefined) }, 1, 10)
    const sessions = new BrowserSessions(busy)
    void busy.byHash(sessionIdHash('other'))
    expect(await sessions.resolve(TOKEN)).toBeUndefined()
  })

  it('sends nothing for a hash the API would refuse', async () => {
    const api = new FakeApi()
    expect(await new CrudSessionLookup(target(api)).byHash('../areas')).toBeUndefined()
    expect(api.seen).toEqual([])
  })
})

describe("health's probe of the agent's data", () => {
  it('answers whether the API answers its areas', async () => {
    const api = new FakeApi().answer('GET /areas', { areas: ['migrations'] })
    const areas = new CrudAreas(BASE, { fetch: api.fetch, wanted: ['migrations'], log: () => undefined })
    expect(await areas.answers()).toBe(true)
    expect(await new CrudAreas(BASE, { fetch: new FakeApi().answer('GET /areas', {}, 503).fetch, wanted: [], log: () => undefined }).answers()).toBe(false)
  })

  it('is ready, and answers, only once every area is listed and the API answers them; aiStatus names it', async () => {
    let complete = false
    let api = true
    const logged: string[] = []
    const health = dataHealthOf(
      { get complete() { return complete }, answers: () => Promise.resolve(api) },
      (m) => logged.push(m),
    )
    const status = async () => (await aiStatus({ database: health, credentials: {} } as never, await health.ping())).ai
    // Until the API lists every area, nothing is asked: the agent has no other path.
    expect([await health.ready(), await health.ping()]).toEqual([false, false])
    expect(await status()).toBe("unavailable (the API does not answer the agent's CRUD areas)")
    complete = true
    expect([await health.ready(), await health.ping()]).toEqual([true, true])
    api = false
    expect(await health.ping()).toBe(false)
    expect(await status()).toBe("unavailable (the API does not answer the agent's CRUD areas)")
    api = true
    expect(await health.ping()).toBe(true)
    expect(logged).toEqual([
      "health: the API does not answer the agent's CRUD areas",
      "health: the API answers the agent's CRUD areas again",
    ])
  })

  it('is not ready while the API has not released the input-hash key, though every area is listed', async () => {
    let released = false
    const health = dataHealthOf({ complete: true, answers: () => Promise.resolve(true) }, () => undefined, () => released)
    // A tool call hashes its input for the audit log after it ran: before the key it must not run.
    expect(await health.ready()).toBe(false)
    // The API answers, so /healthz says the data is reachable.
    expect(await health.ping()).toBe(true)
    // `ai` names the wait once the API holds its KEK.
    const credentials = { list: async () => [], opener: async () => ({ ok: true, kek: { id: 'k' } }) }
    expect((await aiStatus({ credentials } as never, await health.ping(), await health.ready())).ai).toBe(
      "unavailable (waiting for the API's input-hash key)",
    )
    released = true
    expect(await health.ready()).toBe(true)
    expect((await aiStatus({ credentials } as never, await health.ping(), await health.ready())).ai).toBe('disabled (no Claude credential)')
  })
})
