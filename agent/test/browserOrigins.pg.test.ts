import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import { DEFAULT_LISTS } from '../src/browser/origins.js'
import { BrowserOrigins } from '../src/browser/store.js'
import { SettingsStore } from '../src/credentials.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { BROWSER_ORIGINS_PATH, registerBrowserOriginsRoutes } from '../src/routes/browserOrigins.js'
import { SessionError, type SessionRecord } from '../src/sessions/manager.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #982: the headless browser's allow and deny lists (ai_settings) and each session's
// approved origins (ai_browser_session_origins), through their routes, which only the
// user in the ScadBuddy UI may write.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const HOST = 'https://scadbuddy.example'
const UI = { host: 'scadbuddy.example', origin: HOST, 'x-forwarded-proto': 'https', 'content-type': 'application/json' }
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

describe.skipIf(skip !== undefined)(`browser origins${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: BrowserOrigins
  let app: Hono
  let audited: AuditEntry[]

  async function session(title = 'Spool lock'): Promise<string> {
    const [row] = await db.sql<{ id: string }[]>`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, title)
      VALUES (gen_random_uuid(), 'chat', 'browser', 'browser', 'you', 'browser', 'browser', 'idle', 10, 1, ${title})
      RETURNING id`
    return row!.id
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    audited = []
    const audit = {
      record: (e: AuditEntry) => {
        audited.push(e)
        return Promise.resolve()
      },
    }
    store = new BrowserOrigins(db.sql, new SettingsStore(db.sql, audit as never))
    app = new Hono()
    // The session's owner and archive mark, as SessionManager.get answers for the user.
    const sessions = {
      async get(id: string): Promise<SessionRecord> {
        const [row] = await db.sql<{ owner_kind: string; owner_id: string; owner_label: string; archived_at: Date | null }[]>`
          SELECT owner_kind, owner_id, owner_label, archived_at FROM ai_sessions WHERE id = ${id}`
        if (!row) throw new SessionError('not_found', `no session ${id}`)
        return { id, owner: { kind: row.owner_kind, id: row.owner_id, label: row.owner_label }, archivedAt: row.archived_at } as unknown as SessionRecord
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
  afterEach(async () => {
    await drop()
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
    // Audited as the UI user, from the request's address.
    expect(audited.at(-1)).toMatchObject({ kind: 'settings', action: 'browser_origins', surface: 'http', clientIp: '10.0.0.7' })

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
    const id = await session()
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
    const id = await session()
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://example.org/path' })).status).toBe(400)
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'file:///etc' })).status).toBe(400)
    expect(
      (await send('POST', '/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/browser-origins', { origin: 'https://example.org' })).status,
    ).toBe(404)
  })

  it("refuses to approve or revoke in another owner's session, or an archived one", async () => {
    const theirs = await session()
    await db.sql`UPDATE ai_sessions SET owner_kind = 'bearer', owner_id = 'token-1', owner_label = 'Claude Desktop' WHERE id = ${theirs}`
    const archived = await session()
    await store.approve(archived, 'https://example.org', 'session')
    await db.sql`UPDATE ai_sessions SET archived_at = now() WHERE id = ${archived}`

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
    const id = await session()
    expect((await send('PUT', BROWSER_ORIGINS_PATH, { allow: [], deny: [] }, headers)).status).toBe(403)
    expect((await send('POST', `${BROWSER_ORIGINS_PATH}/allow`, { rule: 'evil.example' }, headers)).status).toBe(403)
    expect((await send('POST', `/api/v1/ai/sessions/${id}/browser-origins`, { origin: 'https://evil.example' }, headers)).status).toBe(403)
    expect(await store.lists()).toEqual(DEFAULT_LISTS)
    expect(await store.approved(id)).toEqual(new Map())
  })

  it('a session that is forgotten takes its approvals with it', async () => {
    const id = await session()
    await store.approve(id, 'https://example.org', 'once')
    await db.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    expect(await db.sql`SELECT 1 FROM ai_browser_session_origins`).toHaveLength(0)
  })
})
