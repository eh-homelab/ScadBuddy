import { randomUUID } from 'node:crypto'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { type HttpRequestContext, readSavedResponse, runHttpRequest, withDefaults } from '../src/harness/httpRequest.js'
import { originPolicy } from '../src/http/origins.js'
import { PgHttpBodies } from '../src/sessions/httpBodies.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #2374: a body http_request saved because it was too long to return inline is in
// Postgres, with its session, not on the pod that made the request: another replica
// serves AI activity's "Download body" and the agent's http_response_read, after a
// deploy too.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`
const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const META = { content_type: 'application/json', size_bytes: 7, complete: true, url: 'https://example.test/a' }

describe.skipIf(skip !== undefined)(`saved http bodies${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let replica: SessionManager
  const servers: Server[] = []

  /** A replica: its own state directory, the shared database. */
  async function newReplica(): Promise<SessionManager> {
    return manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
  }

  function app(sessions: SessionManager) {
    const deps: AppDeps = {
      database: { ping: () => Promise.resolve(true), ready: () => Promise.resolve(true) },
      backend: () => Promise.resolve(true),
      kek: { ok: false, reason: 'unused' },
      credentials: new MemoryCredentials(),
      testConnection: () => Promise.resolve({ ok: true, detail: 'ok', duration_ms: 0, model: 'm' }),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      approvals: sessions.approvals,
      sessions,
    }
    return createApp(deps)
  }

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    replica = await newReplica()
  })
  afterEach(async () => {
    for (const server of servers.splice(0)) server.close()
    replica.abortAll()
    await drop()
  })

  it('serves a body another replica saved, on a replica that never had it on disk', async () => {
    const { session } = await replica.start(browser, { origin: 'chat' })
    const id = randomUUID()
    await new PgHttpBodies(db.sql, session.id).save(id, Buffer.from('{"a":1}'), META)

    const other = await newReplica()
    const res = await app(other).request(`/api/v1/ai/sessions/${session.id}/http/${id}`, { headers: UI_READ })
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toBe('text/plain; charset=utf-8')
    expect(await res.text()).toBe('{"a":1}')
    // Only under its own session.
    const { session: elsewhere } = await other.start(browser, { origin: 'chat' })
    expect((await app(other).request(`/api/v1/ai/sessions/${elsewhere.id}/http/${id}`, { headers: UI_READ })).status).toBe(404)
    other.abortAll()
  })

  it('keeps the newest ten, pages one, and goes with its session', async () => {
    const { session } = await replica.start(browser, { origin: 'chat' })
    const bodies = new PgHttpBodies(db.sql, session.id)
    const ids: string[] = []
    for (let n = 0; n < 12; n++) {
      const id = randomUUID()
      ids.push(id)
      await bodies.save(id, Buffer.from(`body ${n}`), { ...META, size_bytes: `body ${n}`.length })
    }
    expect(await bodies.meta(ids[0]!)).toBeUndefined()
    expect(await bodies.meta(ids[1]!)).toBeUndefined()
    expect(await bodies.meta(ids[2]!)).toMatchObject({ url: META.url, size_bytes: 6 })
    expect((await bodies.read(ids[11]!, 5, 100)).toString()).toBe('11')
    expect((await bodies.read(ids[11]!, 50, 10)).length).toBe(0)
    expect((await bodies.whole(ids[11]!))?.bytes.toString()).toBe('body 11')

    await db.sql`DELETE FROM ai_sessions WHERE id = ${session.id}`
    expect(await db.sql`SELECT 1 FROM ai_session_http_bodies`).toHaveLength(0)
  })

  it("saves a long response in Postgres and pages it from another replica's tool", async () => {
    const { session } = await replica.start(browser, { origin: 'chat' })
    const big = Buffer.alloc(3000, 'a')
    const server = createServer((_req, res) => res.writeHead(200, { 'content-type': 'text/plain' }).end(big))
    servers.push(server)
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r))
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/long`
    const context: HttpRequestContext = {
      saved: new PgHttpBodies(db.sql, session.id),
      secrets: () => [],
      actor: { kind: 'browser' as const, id: 'user', label: 'User' },
      limits: { inlineMaxBytes: 1000 },
    }
    const result = await runHttpRequest(withDefaults({ url }), context)
    const saved = (JSON.parse((result.content[0] as { text: string }).text) as { saved: { id: string } }).saved.id

    const page = await readSavedResponse({ id: saved, offset: 2000, length: 2000 }, { saved: new PgHttpBodies(db.sql, session.id) })
    expect(JSON.parse((page.content[0] as { text: string }).text)).toMatchObject({
      size_bytes: 3000,
      offset: 2000,
      next_offset: null,
      content: 'a'.repeat(1000),
    })
  })
})
