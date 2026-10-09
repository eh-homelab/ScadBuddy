import { createHash } from 'node:crypto'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AttachmentStore } from '../src/attachments/store.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import { ATTACHMENTS_PATH, registerAttachmentRoutes } from '../src/routes/attachments.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { frontendClientMessages } from './support/frontendProtocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #1941: the panel uploads an image when it is attached, and a user.message
// names it by id. The bytes wait in ai_attachments (per owner, an hour at most),
// and move into the session's ai_session_blobs when the turn that sends them starts.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1]).toString('base64')
const NAME = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`
const preview = { mediaType: 'image/jpeg' as const, data: JPEG }
const image = { mediaType: 'image/png' as const, data: PNG, preview }

const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }
const URL_ = `https://scadbuddy.example${ATTACHMENTS_PATH}`

const settle = () => new Promise((r) => setTimeout(r, 100))

describe.skipIf(skip !== undefined)(`attachments${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let store: AttachmentStore
  let app: Hono

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    store = new AttachmentStore(db.sql)
    app = new Hono()
    registerAttachmentRoutes(app, {
      attachments: store,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
  })
  afterEach(async () => {
    await drop()
  })

  const post = (body: unknown, headers: Record<string, string> = UI, type = 'application/json') =>
    app.request(URL_, { method: 'POST', headers: { ...headers, 'content-type': type }, body: JSON.stringify(body) })

  describe('POST /api/v1/ai/attachments', () => {
    it('stores a checked image for the browser user and answers its id and preview', async () => {
      const res = await post(image)
      expect(res.status).toBe(201)
      const body = (await res.json()) as { id: string; preview: unknown }
      expect(body.preview).toEqual(preview)
      expect(body.id).toMatch(/^[0-9a-f-]{36}$/)
      const [row] = await db.sql`SELECT owner_kind, owner_id, name, media_type, data FROM ai_attachments WHERE id = ${body.id}`
      expect(row).toMatchObject({ owner_kind: 'browser', owner_id: 'browser', name: NAME, media_type: 'image/png' })
      expect(Buffer.from(row!.data as Buffer).equals(Buffer.from(PNG, 'base64'))).toBe(true)
    })

    it('refuses a request that is not from the UI', async () => {
      expect((await post(image, { host: UI.host, 'x-forwarded-proto': 'https' })).status).toBe(403)
      expect((await post(image, { ...UI, origin: 'https://evil.example' })).status).toBe(403)
      expect((await post(image, { ...UI, 'x-forwarded-proto': 'http' })).status).toBe(403)
      expect(await db.sql`SELECT 1 FROM ai_attachments`).toHaveLength(0)
    })

    it('takes JSON only', async () => {
      expect((await post(image, UI, 'text/plain')).status).toBe(415)
    })

    it('refuses what images.ts refuses, without quoting the bytes', async () => {
      const svg = Buffer.from('<svg/>').toString('base64')
      for (const bad of [
        { ...image, mediaType: 'image/svg+xml', data: svg },
        { ...image, data: JPEG },
        { ...image, preview: undefined },
        { mediaType: 'image/png', data: PNG },
      ]) {
        const res = await post(bad)
        expect(res.status).toBe(400)
        expect(await res.text()).not.toContain(JPEG)
      }
      expect(await db.sql`SELECT 1 FROM ai_attachments`).toHaveLength(0)
    })

    it('refuses a body larger than one image and its preview with 413', async () => {
      const res = await app.request(URL_, {
        method: 'POST',
        headers: { ...UI, 'content-type': 'application/json' },
        body: JSON.stringify({ ...image, data: PNG.slice(0, 12) + 'A'.repeat(6 * 1024 * 1024) }),
      })
      expect(res.status).toBe(413)
    })

    it('refuses past the owner’s rows or bytes, with 429', async () => {
      const capped = new AttachmentStore(db.sql, { limits: { rowsMax: 2 } })
      const small = new Hono()
      registerAttachmentRoutes(small, {
        attachments: capped,
        ready: () => Promise.resolve(true),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
      const send = () =>
        small.request(URL_, { method: 'POST', headers: { ...UI, 'content-type': 'application/json' }, body: JSON.stringify(image) })
      expect((await send()).status).toBe(201)
      expect((await send()).status).toBe(201)
      expect((await send()).status).toBe(429)
      const bytes = new AttachmentStore(db.sql, { limits: { bytesMax: Buffer.from(PNG, 'base64').length } })
      await expect(bytes.put(agentA, image)).resolves.toMatchObject({ preview })
      await expect(bytes.put(agentA, image)).rejects.toMatchObject({ code: 'quota' })
    })

    it('answers 503 without the database', async () => {
      const none = new Hono()
      registerAttachmentRoutes(none, {
        attachments: undefined,
        ready: () => Promise.resolve(true),
        remoteAddress: () => '10.0.0.7',
        origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
      })
      const res = await none.request(URL_, { method: 'POST', headers: { ...UI, 'content-type': 'application/json' }, body: '{}' })
      expect(res.status).toBe(503)
    })
  })

  describe('DELETE /api/v1/ai/attachments/:id', () => {
    it('deletes the browser user’s attachment, and nothing else', async () => {
      const { id } = await store.put(browser, image)
      const other = await store.put(agentA, image)
      const del = (which: string, headers: Record<string, string> = UI) =>
        app.request(`${URL_}/${which}`, { method: 'DELETE', headers })
      expect((await del(id, { host: UI.host, 'x-forwarded-proto': 'https' })).status).toBe(403)
      expect((await del(other.id)).status).toBe(404)
      expect((await del('not-an-id')).status).toBe(404)
      expect((await del(id)).status).toBe(204)
      expect((await del(id)).status).toBe(404)
      expect(await db.sql`SELECT id FROM ai_attachments`).toEqual([{ id: other.id }])
    })
  })

  describe('the store', () => {
    it('resolves only the owner’s, unexpired attachments, in order', async () => {
      const a = await store.put(browser, image)
      const b = await store.put(browser, { ...image, mediaType: 'image/jpeg', data: JPEG })
      const resolved = await store.resolve(browser, [
        { kind: 'attachment', id: b.id },
        { kind: 'attachment', id: a.id },
      ])
      expect(resolved.map((r) => r.image)).toEqual([{ mediaType: 'image/jpeg', data: JPEG, preview }, image])
      expect(resolved[1]?.blob).toMatchObject({ name: NAME, mediaType: 'image/png' })

      await expect(store.resolve(agentA, [{ kind: 'attachment', id: a.id }])).rejects.toMatchObject({
        code: 'not_found',
        message: expect.stringContaining('images[0]'),
      })
      await db.sql`UPDATE ai_attachments SET expires_at = now() - interval '1 second' WHERE id = ${a.id}`
      await expect(store.resolve(browser, [{ kind: 'attachment', id: a.id }])).rejects.toMatchObject({ code: 'not_found' })
    })

    it('sweeps expired attachments', async () => {
      const a = await store.put(browser, image)
      const b = await store.put(browser, image)
      await db.sql`UPDATE ai_attachments SET expires_at = now() - interval '1 second' WHERE id = ${a.id}`
      expect(await store.sweep()).toBe(1)
      expect(await db.sql`SELECT id FROM ai_attachments`).toEqual([{ id: b.id }])
    })

    it('expires an attachment after its TTL', async () => {
      const short = new AttachmentStore(db.sql, { limits: { ttlMs: 1 } })
      const { id } = await short.put(browser, image)
      await settle()
      await expect(short.resolve(browser, [{ kind: 'attachment', id }])).rejects.toMatchObject({ code: 'not_found' })
    })
  })

  describe('a chat message that names attachments', () => {
    let m: SessionManager
    let runs: ReturnType<typeof scriptedRunner>['runs']

    beforeEach(async () => {
      const scripted = scriptedRunner(() => ({ reply: 'a pixel' }))
      runs = scripted.runs
      m = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner, pollMs: 20 })
    })
    afterEach(() => {
      m.abortAll()
    })

    async function chat(frame: Record<string, unknown> & { type: string }) {
      const out: ServerEvent[] = []
      const connection = new ChatConnection(m, (e) => out.push(e), { attachments: store, snapshotMs: 60_000 })
      await connection.open()
      const { clientMessage } = await frontendClientMessages()
      await connection.receive(JSON.stringify(clientMessage(frame)))
      return { out, connection }
    }

    async function images(): Promise<unknown[]> {
      const prompt = runs.at(-1)?.prompt
      if (typeof prompt === 'string' || prompt === undefined) return []
      for await (const message of prompt) {
        const content = message.message.content as { type: string }[]
        return content.filter((b) => b.type === 'image')
      }
      return []
    }

    it('gives the model the image, shows its preview, and moves the bytes into the session', async () => {
      const { id } = await store.put(browser, image)
      const { out, connection } = await chat({
        type: 'user.message',
        text: 'what is this?',
        context: { route: '/' },
        images: [{ kind: 'attachment', id }],
      })
      await settle()
      connection.close()
      const started = out.find((e) => e.type === 'session.started') as { sessionId: string } | undefined
      expect(started).toBeDefined()
      const sessionId = started!.sessionId
      expect(await images()).toEqual([{ type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } }])
      const events = (await m.events.read(sessionId, 0)).map((r) => r.event)
      expect(events.find((e) => e.type === 'user.turn')).toMatchObject({ images: [preview] })
      expect(JSON.stringify(events)).not.toContain(PNG.slice(0, 40))
      // Moved, not copied: the session holds the bytes by name, the staging row is gone.
      expect(await m.blob(sessionId, NAME, browser)).toMatchObject({ mediaType: 'image/png' })
      expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(0)
    })

    it('refuses an attachment that is not the sender’s, and starts nothing', async () => {
      const { id } = await store.put(agentA, image)
      const { out, connection } = await chat({
        type: 'user.message',
        text: 'what is this?',
        context: { route: '/' },
        images: [{ kind: 'attachment', id }],
      })
      connection.close()
      expect(out.find((e) => e.type === 'error')).toMatchObject({ code: 'invalid', message: expect.stringContaining('images[0]') })
      expect(out.find((e) => e.type === 'session.started')).toBeUndefined()
      expect(runs).toHaveLength(0)
      expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(1)
    })

    it('sends to an existing session, and keeps the attachment when the send is refused', async () => {
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'hi' })
      await turn!.done
      const { id } = await store.put(browser, image)
      await db.sql`UPDATE ai_sessions SET status = 'done' WHERE id = ${session.id}`
      const refused = await chat({
        type: 'user.message',
        sessionId: session.id,
        text: 'look',
        context: { route: '/' },
        images: [{ kind: 'attachment', id }],
      })
      refused.connection.close()
      expect(refused.out.find((e) => e.type === 'error')).toBeDefined()
      expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(1)

      await db.sql`UPDATE ai_sessions SET status = 'idle' WHERE id = ${session.id}`
      const sent = await chat({
        type: 'user.message',
        sessionId: session.id,
        text: 'look',
        context: { route: '/' },
        images: [{ kind: 'attachment', id }],
      })
      await settle()
      sent.connection.close()
      expect(await images()).toHaveLength(1)
      expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(0)
      expect(await m.blob(session.id, NAME, browser)).toMatchObject({ mediaType: 'image/png' })
    })
  })
})
