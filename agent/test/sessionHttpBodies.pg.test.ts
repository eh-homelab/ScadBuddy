import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { sessionWorkDir } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #1292: a response body the session's http_request saved, served to the UI by
// GET /api/v1/ai/sessions/:id/http/:saved (the id its AI activity row names).

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const SAVED = '0b7d4c3e-5f6a-4b8c-9d0e-1f2a3b4c5d6e'

describe.skipIf(skip !== undefined)(`saved HTTP response bodies${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let paths: { stateDir: string }
  let m: SessionManager
  let app: ReturnType<typeof createApp>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    paths = await tempPaths()
    const { runner } = scriptedRunner(() => ({ reply: 'done' }))
    m = manager({ sql: db.sql, paths, run: runner })
    const deps: AppDeps = {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: m.approvals,
      sessions: m,
    }
    app = createApp(deps)
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  /** A session of `owner`'s with one body saved as http_request saves it. */
  async function withBody(contentType: string | null, bytes: Buffer, owner = browser): Promise<string> {
    const { session } = await m.start(owner, { origin: 'chat' })
    const dir = path.join(sessionWorkDir(paths, session.id), 'http')
    await mkdir(dir, { recursive: true })
    await writeFile(path.join(dir, `${SAVED}.body`), bytes)
    await writeFile(
      path.join(dir, `${SAVED}.json`),
      JSON.stringify({ content_type: contentType, size_bytes: bytes.length, complete: true, url: 'https://example.com/x' }),
    )
    return session.id
  }

  const get = (p: string, headers: Record<string, string> = UI_READ) => app.request(p, { headers })

  it('shows a JSON or plain-text body as text, inline', async () => {
    const id = await withBody('application/json; charset=utf-8', Buffer.from('{"a":1}'))
    const res = await get(`/api/v1/ai/sessions/${id}/http/${SAVED}`)
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toBe('text/plain; charset=utf-8')
    expect(res.headers.get('content-disposition')).toBe(`inline; filename="response-${SAVED.slice(0, 8)}.txt"`)
    expect(res.headers.get('x-content-type-options')).toBe('nosniff')
    expect(res.headers.get('content-security-policy')).toBe("default-src 'none'; sandbox")
    expect(await res.text()).toBe('{"a":1}')
  })

  it('only downloads HTML, SVG, binary or an unknown type, never renders it', async () => {
    for (const type of ['text/html', 'image/svg+xml', 'application/octet-stream', null]) {
      const id = await withBody(type, Buffer.from('<script>alert(1)</script>'))
      const res = await get(`/api/v1/ai/sessions/${id}/http/${SAVED}`)
      expect(res.status).toBe(200)
      expect(res.headers.get('content-type')).toBe('application/octet-stream')
      expect(res.headers.get('content-disposition')).toBe(`attachment; filename="response-${SAVED.slice(0, 8)}.bin"`)
    }
  })

  it('serves a body only under its own session, and only by a saved id', async () => {
    const id = await withBody('text/plain', Buffer.from('hi'))
    const { session: other } = await m.start(browser, { origin: 'chat' })
    expect((await get(`/api/v1/ai/sessions/${other.id}/http/${SAVED}`)).status).toBe(404)
    expect((await get(`/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/http/${SAVED}`)).status).toBe(404)
    expect((await get(`/api/v1/ai/sessions/${id}/http/${SAVED}.json`)).status).toBe(404)
    expect((await get(`/api/v1/ai/sessions/${id}/http/..%2F..%2F..%2Fetc%2Fpasswd`)).status).toBe(404)
  })

  it("follows the session's own visibility: a body is served exactly when its session is", async () => {
    const id = await withBody('text/plain', Buffer.from('theirs'), agentA)
    const session = await get(`/api/v1/ai/sessions/${id}`)
    expect((await get(`/api/v1/ai/sessions/${id}/http/${SAVED}`)).status).toBe(session.status)
  })

  it('refuses a request that is not from the UI', async () => {
    const id = await withBody('text/plain', Buffer.from('hi'))
    expect((await get(`/api/v1/ai/sessions/${id}/http/${SAVED}`, { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
  })
})
