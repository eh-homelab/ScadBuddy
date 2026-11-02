import { Hono } from 'hono'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import {
  BrowserSessions,
  browserUser,
  CACHE_MS,
  personSessions,
  SESSION_COOKIE,
  SIGNED_OUT_PROBLEM,
  sessionIdHash,
} from '../src/auth/browserSession.js'
import type { BusEvent, EventListener } from '../src/events/bus.js'
import { sameOwner } from '../src/sessions/protocol.js'

// #4975 phase 5 (spec 2026-11-01 §5.6): the signed-in person on /api/v1/ai/*.

const PERSON = 'user:https://idp.test#alice'
const TOKEN = 'a'.repeat(43)

function fakeSql(rows: () => unknown[]): { sql: Sql; reads: () => number; params: unknown[][] } {
  const params: unknown[][] = []
  const sql = ((_strings: TemplateStringsArray, ...values: unknown[]) => {
    params.push(values)
    return Promise.resolve(rows())
  }) as unknown as Sql
  return { sql, reads: () => params.length, params }
}

function app(sessions: BrowserSessions | undefined, mode: 'off' | 'observe' | 'enforce' = 'off', log: (m: string) => void = () => {}) {
  const hono = new Hono()
  hono.use('/api/v1/ai/*', personSessions(sessions, mode, log))
  hono.get('/api/v1/ai/who', (c) => c.json(browserUser(c)))
  hono.get('/api/v1/ai/plugin-oauth/callback', (c) => c.text('ok'))
  return hono
}

const withCookie = { headers: { cookie: `${SESSION_COOKIE}=${TOKEN}` } }

describe('BrowserSessions', () => {
  it('looks the cookie up by its SHA-256, never the cookie', async () => {
    const { sql, params } = fakeSql(() => [{ principal: PERSON, name: 'Alice', kind: 'login' }])
    expect(await new BrowserSessions(sql).resolve(TOKEN)).toEqual({ principal: PERSON, name: 'Alice', kind: 'login' })
    expect(params[0]).toEqual([Buffer.from(sessionIdHash(TOKEN), 'hex')])
    expect(JSON.stringify(params)).not.toContain(TOKEN)
  })

  it('caches an answer a few seconds and drops it on revocation', async () => {
    let now = 0
    const { sql, reads } = fakeSql(() => [{ principal: PERSON, name: null, kind: 'login' }])
    const sessions = new BrowserSessions(sql, () => now)
    let listener: EventListener | undefined
    sessions.follow({ follow: (l) => ((listener = l), () => {}) })
    await sessions.resolve(TOKEN)
    await sessions.resolve(TOKEN)
    expect(reads()).toBe(1)
    now = CACHE_MS
    await sessions.resolve(TOKEN)
    expect(reads()).toBe(2)
    const revoked: BusEvent = { id: '1', kind: 'auth.session_revoked', principal: PERSON, session_id: sessionIdHash(TOKEN) }
    listener?.onEvent(revoked)
    await sessions.resolve(TOKEN)
    expect(reads()).toBe(3)
    // "Sign out everywhere": no session id, every one of the principal's.
    listener?.onEvent({ id: '2', kind: 'auth.session_revoked', principal: PERSON })
    await sessions.resolve(TOKEN)
    expect(reads()).toBe(4)
  })

  it('reads a failed lookup as no session', async () => {
    const sql = (() => Promise.reject(new Error('down'))) as unknown as Sql
    expect(await new BrowserSessions(sql).resolve(TOKEN)).toBeUndefined()
  })
})

describe('personSessions', () => {
  it('makes the signed-in person the browser user', async () => {
    const { sql } = fakeSql(() => [{ principal: PERSON, name: 'Alice', kind: 'login' }])
    const response = await app(new BrowserSessions(sql)).request('/api/v1/ai/who', withCookie)
    expect(await response.json()).toEqual({ kind: 'browser', id: PERSON, label: 'You' })
  })

  it('is the anonymous browser user without a session, a lookup or a delegation', async () => {
    const { sql, reads } = fakeSql(() => [])
    const sessions = new BrowserSessions(sql)
    expect(await (await app(sessions).request('/api/v1/ai/who')).json()).toEqual({ kind: 'browser', id: 'browser', label: 'You' })
    expect(reads()).toBe(0)
    expect(((await (await app(sessions).request('/api/v1/ai/who', withCookie)).json()) as { id: string }).id).toBe('browser')
    const delegated = fakeSql(() => [{ principal: PERSON, name: null, kind: 'delegated' }])
    const answer = await app(new BrowserSessions(delegated.sql)).request('/api/v1/ai/who', withCookie)
    expect(((await answer.json()) as { id: string }).id).toBe('browser')
  })

  it('logs, once a minute, what enforce would refuse in observe', async () => {
    const lines: string[] = []
    const served = app(new BrowserSessions(fakeSql(() => []).sql), 'observe', (m) => lines.push(m))
    expect((await served.request('/api/v1/ai/who')).status).toBe(200)
    await served.request('/api/v1/ai/who')
    expect(lines).toHaveLength(1)
    const off: string[] = []
    await app(undefined, 'off', (m) => off.push(m)).request('/api/v1/ai/who')
    expect(off).toHaveLength(0)
  })
})

describe('personSessions in enforce (#4975 phase 8)', () => {
  it("refuses a request with no live session with the backend's 401 problem", async () => {
    const lines: string[] = []
    const refused = await app(new BrowserSessions(fakeSql(() => []).sql), 'enforce', (m) => lines.push(m)).request(
      '/api/v1/ai/who',
      withCookie,
    )
    expect(refused.status).toBe(401)
    expect(refused.headers.get('content-type')).toContain('application/problem+json')
    expect(((await refused.json()) as { type: string }).type).toBe(SIGNED_OUT_PROBLEM)
    expect((await app(undefined, 'enforce').request('/api/v1/ai/who')).status).toBe(401)
    expect(lines).toHaveLength(0)
  })

  it('serves a signed-in person and a delegated session', async () => {
    const person = fakeSql(() => [{ principal: PERSON, name: 'Alice', kind: 'login' }])
    const served = await app(new BrowserSessions(person.sql), 'enforce').request('/api/v1/ai/who', withCookie)
    expect(((await served.json()) as { id: string }).id).toBe(PERSON)
    const delegated = fakeSql(() => [{ principal: PERSON, name: null, kind: 'delegated' }])
    expect((await app(new BrowserSessions(delegated.sql), 'enforce').request('/api/v1/ai/who', withCookie)).status).toBe(200)
  })

  it('serves the plugin OAuth callback, a cross-site redirect with no session cookie', async () => {
    expect((await app(undefined, 'enforce').request('/api/v1/ai/plugin-oauth/callback?code=c&state=s')).status).toBe(200)
  })
})

describe('sameOwner', () => {
  it('takes every browser-kind principal as the browser user (D8)', () => {
    expect(sameOwner({ kind: 'browser', id: PERSON }, { kind: 'browser', id: 'browser' })).toBe(true)
    expect(sameOwner({ kind: 'bearer', id: 'token:a' }, { kind: 'bearer', id: 'token:b' })).toBe(false)
    expect(sameOwner({ kind: 'browser', id: PERSON }, { kind: 'bearer', id: PERSON })).toBe(false)
  })
})
