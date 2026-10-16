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

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1])
const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')
const NAME = `${sha(PNG)}.png`
const PREVIEW_NAME = `${sha(JPEG)}.jpg`
const preview = { mediaType: 'image/jpeg' as const, bytes: JPEG }
const image = { mediaType: 'image/png' as const, bytes: PNG, preview }
const view = { name: NAME, mediaType: 'image/png', preview: { name: PREVIEW_NAME, mediaType: 'image/jpeg' } }

/** The upload as the panel sends it (#2479): two binary parts, never base64 in JSON. */
function form(
  parts: { image?: [Buffer, string]; preview?: [Buffer, string] } = { image: [PNG, 'image/png'], preview: [JPEG, 'image/jpeg'] },
): FormData {
  const body = new FormData()
  if (parts.image) body.append('image', new Blob([new Uint8Array(parts.image[0])], { type: parts.image[1] }), 'image')
  if (parts.preview) body.append('preview', new Blob([new Uint8Array(parts.preview[0])], { type: parts.preview[1] }), 'preview')
  return body
}

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

  const post = (body: FormData = form(), headers: Record<string, string> = UI, to: Hono = app) =>
    to.request(URL_, { method: 'POST', headers, body })

  describe('POST /api/v1/ai/attachments', () => {
    it('stores the uploaded bytes for the browser user and answers pointers, never bytes (#2479)', async () => {
      const res = await post()
      expect(res.status).toBe(201)
      const body = (await res.json()) as { id: string }
      expect(body).toEqual({ id: expect.stringMatching(/^[0-9a-f-]{36}$/), ...view })
      const [row] = await db.sql`
        SELECT owner_kind, owner_id, name, media_type, data, preview_media_type, preview FROM ai_attachments WHERE id = ${body.id}`
      expect(row).toMatchObject({
        owner_kind: 'browser',
        owner_id: 'browser',
        name: NAME,
        media_type: 'image/png',
        preview_media_type: 'image/jpeg',
      })
      expect(Buffer.from(row!.data as Buffer).equals(PNG)).toBe(true)
      expect(Buffer.from(row!.preview as Buffer).equals(JPEG)).toBe(true)
    })

    it('refuses a request that is not from the UI', async () => {
      expect((await post(form(), { host: UI.host, 'x-forwarded-proto': 'https' })).status).toBe(403)
      expect((await post(form(), { ...UI, origin: 'https://evil.example' })).status).toBe(403)
      expect((await post(form(), { ...UI, 'x-forwarded-proto': 'http' })).status).toBe(403)
      expect(await db.sql`SELECT 1 FROM ai_attachments`).toHaveLength(0)
    })

    it('takes multipart only: base64 in JSON is refused (#2479)', async () => {
      const json = await app.request(URL_, {
        method: 'POST',
        headers: { ...UI, 'content-type': 'application/json' },
        body: JSON.stringify({
          mediaType: 'image/png',
          data: PNG.toString('base64'),
          preview: { mediaType: 'image/jpeg', data: JPEG.toString('base64') },
        }),
      })
      expect(json.status).toBe(415)
      expect(await json.text()).toContain('multipart/form-data')
      expect(await db.sql`SELECT 1 FROM ai_attachments`).toHaveLength(0)
    })

    it('refuses what images.ts refuses, without quoting the bytes', async () => {
      for (const bad of [
        form({ image: [Buffer.from('<svg/>'), 'image/svg+xml'], preview: [JPEG, 'image/jpeg'] }),
        form({ image: [JPEG, 'image/png'], preview: [JPEG, 'image/jpeg'] }),
        form({ image: [PNG, 'image/png'] }),
        form({ preview: [JPEG, 'image/jpeg'] }),
        form({ image: [PNG, 'image/png'], preview: [PNG, 'image/gif'] }),
      ]) {
        const res = await post(bad)
        expect(res.status).toBe(400)
        expect(await res.text()).not.toContain(JPEG.toString('base64'))
      }
      // Text fields are not files: base64 in a form field is refused too.
      const text = new FormData()
      text.append('image', PNG.toString('base64'))
      text.append('preview', JPEG.toString('base64'))
      expect((await post(text)).status).toBe(400)
      expect(await db.sql`SELECT 1 FROM ai_attachments`).toHaveLength(0)
    })

    it('refuses a body larger than one image and its preview with 413', async () => {
      const big = Buffer.concat([PNG, Buffer.alloc(4 * 1024 * 1024)])
      const res = await post(form({ image: [big, 'image/png'], preview: [JPEG, 'image/jpeg'] }))
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
      const send = () => post(form(), UI, small)
      expect((await send()).status).toBe(201)
      expect((await send()).status).toBe(201)
      expect((await send()).status).toBe(429)
      const bytes = new AttachmentStore(db.sql, { limits: { bytesMax: PNG.length } })
      await expect(bytes.put(agentA, image)).resolves.toMatchObject(view)
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
      const res = await post(form(), UI, none)
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
      const b = await store.put(browser, { ...image, mediaType: 'image/jpeg', bytes: JPEG })
      const resolved = await store.resolve(browser, [
        { kind: 'attachment', id: b.id },
        { kind: 'attachment', id: a.id },
      ])
      expect(resolved.map((r) => r.image)).toEqual([{ mediaType: 'image/jpeg', bytes: JPEG, preview }, image])
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
    let prompts: ReturnType<typeof scriptedRunner>['prompts']

    beforeEach(async () => {
      const scripted = scriptedRunner(() => ({ reply: 'a pixel' }))
      runs = scripted.runs
      prompts = scripted.prompts
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

    function images(): unknown[] {
      const message = prompts.at(-1)
      if (!message) return []
      const content = message.message.content as { type: string }[]
      return typeof content === 'string' ? [] : content.filter((b) => b.type === 'image')
    }

    it('gives the model the image, names it and its preview in the turn, and moves the bytes into the session', async () => {
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
      expect(images()).toEqual([
        { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG.toString('base64') } },
      ])
      const events = (await m.events.read(sessionId, 0)).map((r) => r.event)
      expect(events.find((e) => e.type === 'user.turn')).toMatchObject({ images: [view] })
      expect(JSON.stringify(events)).not.toContain(PNG.toString('base64').slice(0, 40))
      expect(JSON.stringify(events)).not.toContain(JPEG.toString('base64').slice(0, 12))
      // Moved, not copied: the session holds the bytes by name, the staging row is gone.
      expect(await m.blob(sessionId, NAME, browser)).toMatchObject({ mediaType: 'image/png' })
      expect(await m.blob(sessionId, PREVIEW_NAME, browser)).toMatchObject({ mediaType: 'image/jpeg', bytes: JPEG })
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

    it('refuses uploads that together pass the 8 MiB total, though each fit alone, and starts nothing', async () => {
      // Two 3.375 MiB PNGs: each under IMAGE_BYTES_MAX, together over IMAGES_BYTES_TOTAL_MAX.
      const big = (fill: number) => Buffer.concat([PNG, Buffer.alloc(3.375 * 1024 * 1024, fill)])
      const ids = []
      for (const fill of [1, 2]) ids.push((await store.put(browser, { ...image, bytes: big(fill) })).id)
      const { out, connection } = await chat({
        type: 'user.message',
        text: 'what are these?',
        context: { route: '/' },
        images: ids.map((id) => ({ kind: 'attachment', id })),
      })
      connection.close()
      const refused = out.find((e) => e.type === 'error') as { code?: string; message: string } | undefined
      expect(refused).toMatchObject({ code: 'invalid', message: expect.stringContaining('together') })
      expect(refused!.message.length).toBeLessThan(500)
      expect(out.find((e) => e.type === 'session.started')).toBeUndefined()
      expect(runs).toHaveLength(0)
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
      expect(images()).toHaveLength(1)
      expect(await db.sql`SELECT 1 FROM ai_attachments WHERE id = ${id}`).toHaveLength(0)
      expect(await m.blob(session.id, NAME, browser)).toMatchObject({ mediaType: 'image/png' })
    })
  })
})
