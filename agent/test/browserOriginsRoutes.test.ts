import { randomUUID } from 'node:crypto'
import { Hono } from 'hono'
import { beforeEach, describe, expect, it } from 'vitest'
import type { AuditContext, AuditEntry } from '../src/audit/log.js'
import { DEFAULT_LISTS } from '../src/browser/origins.js'
import { BrowserOrigins } from '../src/browser/store.js'
import { originPolicy } from '../src/http/origins.js'
import { BROWSER_ORIGINS_PATH, registerBrowserOriginsRoutes } from '../src/routes/browserOrigins.js'
import { SessionError, type SessionRecord } from '../src/sessions/manager.js'
import { MemorySessionOrigins } from './support/memoryOrigins.js'

// #982: the headless browser's allow and deny lists (ai_settings) and each session's
// approved origins (ai_browser_session_origins), through their routes, which only the
// user in the ScadBuddy UI may write. The stores are in memory: the API keeps the rows.

const HOST = 'https://scadbuddy.example'
const UI = { host: 'scadbuddy.example', origin: HOST, 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

type Session = { owner: { kind: string; id: string; label: string }; archivedAt: Date | null }

describe('browser origins', () => {
  let store: BrowserOrigins
  let app: Hono
  let audited: AuditEntry[]
  /** The settings writes, each with the context the route gave. */
  let settingsWrites: { key: string; context: AuditContext }[]
  let rows: Map<string, Session>
  let titles: Map<string, string>

  function session(title = 'Spool lock'): string {
    const id = randomUUID()
    rows.set(id, { owner: { kind: 'browser', id: 'browser', label: 'you' }, archivedAt: null })
    titles.set(id, title)
    return id
  }

  beforeEach(() => {
    audited = []
    settingsWrites = []
    rows = new Map()
    titles = new Map()
    const audit = {
      record: (e: AuditEntry) => {
        audited.push(e)
        return Promise.resolve()
      },
    }
    const values = new Map<string, unknown>()
    const settings = {
      get: <T>(key: string) => Promise.resolve(values.get(key) as T | undefined),
      set: (key: string, value: unknown, context: AuditContext) => {
        values.set(key, value)
        settingsWrites.push({ key, context })
        return Promise.resolve()
      },
    }
    store = new BrowserOrigins(settings, new MemorySessionOrigins(titles))
    app = new Hono()
    // The session's owner and archive mark, as SessionManager.get answers for the user.
    const sessions = {
      async get(id: string): Promise<SessionRecord> {
        const row = rows.get(id)
        if (!row) throw new SessionError('not_found', `no session ${id}`)
        return { id, owner: row.owner, archivedAt: row.archivedAt } as unknown as SessionRecord
      },
    }
    registerBrowserOriginsRoutes(app, {
      store,
      sessions,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy(HOST, '10.0.0.0/8'),
      audit: audit as never,
    })
  })
  const send = (method: string, path: string, body?: unknown, headers: Record<string, string> = UI) =>
    app.request(`${HOST}${path}`, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })

  it('reads the defaults, stores checked lists, and refuses a bad rule naming it', async () => {
    const first = await send('GET', BROWSER_ORIGINS_PATH, undefined, UI_READ)
    expect(first.status).toBe(200)
    expect(await first.json()).toEqual({ ...DEFAULT_LISTS, defaults: DEFAULT_LISTS, approved: [] })

    const saved = await send('PUT', BROWSER_ORIGINS_PATH, { allow: [' makerworld.com', '*.printables.com'], deny: ['10.0.0.0/8'] })
    expect(saved.status).toBe(200)
    expect(await saved.json()).toMatchObject({ allow: ['makerworld.com', '*.printables.com'], deny: ['10.0.0.0/8'] })
    expect(await store.lists()).toEqual({ allow: ['makerworld.com', '*.printables.com'], deny: ['10.0.0.0/8'] })
    // Written as the UI user, from the request's address (the settings store audits it).
    expect(settingsWrites.at(-1)).toMatchObject({ key: 'browser_origins', context: { surface: 'http', clientIp: '10.0.0.7' } })

    const bad = await send('PUT', BROWSER_ORIGINS_PATH, { allow: ['https://a.example/path'], deny: [] })
    expect(bad.status).toBe(400)
    expect(((await bad.json()) as { detail: string }).detail).toMatch(/allow list: "https:\/\/a.example\/path" is not an origin/)
    expect((await store.lists()).allow).toEqual(['makerworld.com', '*.printables.com'])
  })

  it('"always" adds one origin to the allow list, once', async () => {
    for (let i = 0; i < 2; i++) {
      const res = await send('POST', `${BROWSER_ORIGINS_PATH}/allow`, { rule: 'https://example.org' })
      expect(res.status).toBe(200)
    }
    const allow = (await store.lists()).allow
    expect(allow.filter((r) => r === 'https://example.org')).toHaveLength(1)
    expect(allow).toEqual([...DEFAULT_LISTS.allow, 'https://example.org'])
  })

  it('approves an origin for a session, lists it with the session, and revokes it', async () => {
    const id = session()
    const res = await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://Example.org/' })
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ origin: 'https://example.org', scope: 'session' })
    expect(await store.approved(id)).toEqual(new Map([['https://example.org', 'session']]))
    // A once-approval never narrows it.
    await store.approve(id, 'https://example.org', 'once')
    expect((await store.approved(id)).get('https://example.org')).toBe('session')

    const listed = (await (await send('GET', BROWSER_ORIGINS_PATH, undefined, UI_READ)).json()) as { approved: unknown[] }
    expect(listed.approved).toEqual([
      { session_id: id, title: 'Spool lock', origin: 'https://example.org', scope: 'session', approved_at: expect.any(String) },
    ])
    const one = (await (await send('GET', `/api/v1/ai/sessions/${id}/browser-origins`, undefined, UI_READ)).json()) as { origins: unknown[] }
    expect(one.origins).toHaveLength(1)

    const revoked = await send('DELETE', `/api/v1/ai/sessions/${id}/browser-origins?origin=${encodeURIComponent('https://example.org')}`)
    expect(revoked.status).toBe(204)
    expect(await store.approved(id)).toEqual(new Map())
    expect((await send('DELETE', `/api/v1/ai/sessions/${id}/browser-origins?origin=https%3A%2F%2Fexample.org`)).status).toBe(404)
    // Normalised as an approval is: the same text revokes what it approved.
    await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://Example.org/' })
    expect((await send('DELETE', `/api/v1/ai/sessions/${id}/browser-origins?origin=${encodeURIComponent('https://Example.org/')}`)).status).toBe(204)
    expect((await send('DELETE', `/api/v1/ai/sessions/${id}/browser-origins?origin=not-an-origin`)).status).toBe(400)
    expect(audited.filter((e) => e.action === 'browser_session_origin').map((e) => e.detail)).toEqual([
      'https://example.org approved for the session',
      'https://example.org revoked for the session',
      'https://example.org approved for the session',
      'https://example.org revoked for the session',
    ])
  })

  it('refuses a session approval that is not an origin, or for no session', async () => {
    const id = session()
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://example.org/path' })).status).toBe(400)
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'file:///etc' })).status).toBe(400)
    expect(
      (await send('POST', '/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/browser-origins', { origin: 'https://example.org' })).status,
    ).toBe(404)
  })

  it("refuses to approve or revoke in another owner's session, or an archived one", async () => {
    const theirs = session()
    rows.get(theirs)!.owner = { kind: 'bearer', id: 'token-1', label: 'Claude Desktop' }
    const archived = session()
    await store.approve(archived, 'https://example.org', 'session')
    rows.get(archived)!.archivedAt = new Date()

    const other = await send('POST', `/api/v1/ai/sessions/${theirs}/browser-origins`, { origin: 'https://example.org' })
    expect(other.status).toBe(403)
    expect(((await other.json()) as { detail: string }).detail).toMatch(/controlled by Claude Desktop/)
    expect((await send('POST', `/api/v1/ai/sessions/${archived}/browser-origins`, { origin: 'https://example.net' })).status).toBe(409)
    expect((await send('DELETE', `/api/v1/ai/sessions/${archived}/browser-origins?origin=https%3A%2F%2Fexample.org`)).status).toBe(409)
    expect(await store.approved(theirs)).toEqual(new Map())
    expect(await store.approved(archived)).toEqual(new Map([['https://example.org', 'session']]))
  })

  it.each([
    ['another origin', { ...UI, origin: 'https://evil.example' }],
    ['no origin', { host: UI.host, 'x-forwarded-proto': 'https', 'content-type': 'application/json' }],
  ])('refuses every write from %s', async (_label, headers) => {
    const id = session()
    expect((await send('PUT', BROWSER_ORIGINS_PATH, { allow: [], deny: [] }, headers)).status).toBe(403)
    expect((await send('POST', `${BROWSER_ORIGINS_PATH}/allow`, { rule: 'evil.example' }, headers)).status).toBe(403)
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://evil.example' }, headers)).status).toBe(403)
    expect(await store.lists()).toEqual(DEFAULT_LISTS)
    expect(await store.approved(id)).toEqual(new Map())
  })
})
