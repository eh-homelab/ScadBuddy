import { createHash } from 'node:crypto'
import type { SDKMessage } from '@anthropic-ai/claude-agent-sdk'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { type AppDeps, createApp } from '../src/app.js'
import type { Database } from '../src/db.js'
import { originPolicy } from '../src/http/origins.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { ServerEvent } from '../src/sessions/protocol.js'
import { expectPanelAccepts } from './support/frontendProtocol.js'
import { MemoryCredentials } from './support/memoryCredentials.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, type FakeTurn, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// #782: the images a session's tool results carried, stored per session
// (ai_session_blobs) and served by GET /api/v1/ai/sessions/:id/blobs/:name.

const skip = TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`

const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }
const PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
const NAME = `${createHash('sha256').update(Buffer.from(PNG, 'base64')).digest('hex')}.png`

/** A turn whose one tool call returned an image, as the SDK yields it (measured on 0.3.289). */
function imageTurn(): FakeTurn {
  const toolUse = {
    type: 'assistant',
    message: { id: 'msg_tool', content: [{ type: 'tool_use', id: 'toolu_img', name: 'mcp__scadbuddy__get_render_preview', input: { job_id: 'j1' } }] },
    parent_tool_use_id: null,
    session_id: 's',
  }
  const toolResult = {
    type: 'user',
    message: {
      role: 'user',
      content: [
        {
          type: 'tool_result',
          tool_use_id: 'toolu_img',
          content: [
            { type: 'image', source: { type: 'base64', media_type: 'image/png', data: PNG } },
            { type: 'text', text: '[Image: source: /tmp/claude-resume-x/projects/p/s/tool-results/mcp-scadbuddy-blob-1-abc.png]' },
          ],
        },
      ],
    },
    parent_tool_use_id: null,
    session_id: 's',
  }
  return { reply: 'Here it is.', before: [toolUse, toolResult] as unknown as SDKMessage[] }
}

describe.skipIf(skip !== undefined)(`session images${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let next: FakeTurn
  let app: ReturnType<typeof createApp>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    next = imageTurn()
    const { runner } = scriptedRunner(() => next)
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, titleOf: () => 'Get render preview' })
    app = createApp(deps())
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  function deps(): AppDeps {
    return {
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
  }

  async function logged(id: string): Promise<ServerEvent[]> {
    return (await m.events.read(id, 0)).map((r) => r.event)
  }

  /** A browser session whose turn returned the image. */
  async function withImage(): Promise<string> {
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'show me' })
    await turn!.done
    return session.id
  }

  const get = (path: string, headers: Record<string, string> = UI_READ) => app.request(path, { headers })

  it('logs the result with its image by name and the call with its title, and serves the bytes', async () => {
    const id = await withImage()
    const events = await logged(id)
    expect(events.find((e) => e.type === 'tool.call')).toMatchObject({ title: 'Get render preview' })
    const result = events.find((e) => e.type === 'tool.result')
    expect(result).toMatchObject({ summary: '', images: [{ name: NAME, mediaType: 'image/png' }] })
    // The event names the image; its bytes are not in the log.
    expect(JSON.stringify(events)).not.toContain(PNG.slice(0, 40))
    await expectPanelAccepts(events)

    const res = await get(`/api/v1/ai/sessions/${id}/blobs/${NAME}`)
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toBe('image/png')
    expect(res.headers.get('x-content-type-options')).toBe('nosniff')
    expect(res.headers.get('content-security-policy')).toBe("default-src 'none'; sandbox")
    expect(res.headers.get('cross-origin-resource-policy')).toBe('same-origin')
    expect(res.headers.get('cache-control')).toContain('immutable')
    expect(Buffer.from(await res.arrayBuffer()).equals(Buffer.from(PNG, 'base64'))).toBe(true)
  })

  it('keeps one row for the same image twice', async () => {
    const id = await withImage()
    await (await m.send(id, browser, 'again')).done
    const rows = await db.sql`SELECT name FROM ai_session_blobs WHERE session_id = ${id}`
    expect(rows.map((r) => r.name as string)).toEqual([NAME])
  })

  it('refuses a request that is not from the UI', async () => {
    const id = await withImage()
    const path = `/api/v1/ai/sessions/${id}/blobs/${NAME}`
    expect((await get(path, { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
    expect((await get(path, { ...UI_READ, origin: 'https://evil.example' })).status).toBe(403)
    expect((await get(path, { host: 'scadbuddy.example', 'sec-fetch-site': 'same-origin' })).status).toBe(403)
  })

  it('serves an image only under its own session', async () => {
    const id = await withImage()
    next = { reply: 'nothing to see' }
    const { session: other } = await m.start(browser, { origin: 'chat' })
    expect((await get(`/api/v1/ai/sessions/${other.id}/blobs/${NAME}`)).status).toBe(404)
    // An unknown session, and one that is not a session id at all.
    expect((await get(`/api/v1/ai/sessions/00000000-0000-4000-8000-000000000000/blobs/${NAME}`)).status).toBe(404)
    expect((await get(`/api/v1/ai/sessions/not-a-session/blobs/${NAME}`)).status).toBe(404)
    // A name the session never stored.
    expect((await get(`/api/v1/ai/sessions/${id}/blobs/${'0'.repeat(64)}.png`)).status).toBe(404)
  })

  it('refuses every name that is not a blob’s: no path is ever built from one', async () => {
    const id = await withImage()
    const names = [
      '..%2F..%2Fetc%2Fpasswd',
      '%2E%2E%2F' + NAME,
      encodeURIComponent(`../${id}/${NAME}`),
      encodeURIComponent(`/tmp/claude-resume-x/tool-results/${NAME}`),
      NAME.toUpperCase(),
      NAME.replace('.png', '.svg'),
      NAME.replace('.png', ''),
      `${NAME}%00.png`,
      `${NAME}.png`,
      'x',
    ]
    for (const name of names) {
      const res = await get(`/api/v1/ai/sessions/${id}/blobs/${name}`)
      expect([name, res.status]).toEqual([name, 404])
    }
    // Dot segments are resolved by the URL before routing: this is the session itself, never a file.
    const dotted = await get(`/api/v1/ai/sessions/${id}/blobs/../../${id}`)
    expect(dotted.headers.get('content-type')).toMatch(/^application\/json/)
  })

  it('does not show another principal’s session to a principal that may not see it', async () => {
    const { session, turn } = await m.start(agentA, { origin: 'mcp', prompt: 'show me' })
    await turn!.done
    // The browser user sees every session (spec §6), so the route serves it…
    expect((await get(`/api/v1/ai/sessions/${session.id}/blobs/${NAME}`)).status).toBe(200)
    // …while any other principal may not see what it does not own.
    await expect(m.blob(session.id, NAME, { kind: 'bearer', id: 'token:b', label: 'B' })).rejects.toMatchObject({ code: 'not_found' })
    await expect(m.blob(session.id, NAME, agentA)).resolves.toMatchObject({ mediaType: 'image/png' })
  })

  it('copies a session’s images into its fork, and removes them with the session', async () => {
    const id = await withImage()
    await m.store.append({ projectKey: 'p', sessionId: id }, [{ type: 'user', uuid: 'u1', message: {} }])
    const child = await m.fork(id, browser)
    const childEvents = await logged(child.id)
    expect(childEvents.find((e) => e.type === 'tool.result')).toMatchObject({ images: [{ name: NAME }] })
    expect((await get(`/api/v1/ai/sessions/${child.id}/blobs/${NAME}`)).status).toBe(200)

    await db.sql`DELETE FROM ai_sessions WHERE id = ${id}`
    expect(await db.sql`SELECT 1 FROM ai_session_blobs WHERE session_id = ${id}`).toHaveLength(0)
    expect((await get(`/api/v1/ai/sessions/${child.id}/blobs/${NAME}`)).status).toBe(200)
  })

  describe('the images a user sends with a turn', () => {
    // A JPEG's signature is enough for images.ts; the preview is the panel's own drawing.
    const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46]).toString('base64')
    const SENT = `${createHash('sha256').update(Buffer.from(JPEG, 'base64')).digest('hex')}.jpg`
    const preview = { mediaType: 'image/png' as const, data: PNG }
    const image = { mediaType: 'image/jpeg' as const, data: JPEG, preview }

    /** A browser session whose first turn carried the image; its turn returns none. */
    async function withSent(): Promise<string> {
      next = { reply: 'I see it.' }
      const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'look', images: [image] })
      await turn!.done
      return session.id
    }

    it('stores each before the user.turn that names it beside its preview, and serves it', async () => {
      const id = await withSent()
      const events = await logged(id)
      expect(events.find((e) => e.type === 'user.turn')).toMatchObject({ images: [{ ...preview, name: SENT }] })
      // The log names the image; only the preview is in it.
      expect(JSON.stringify(events)).not.toContain(JPEG)
      await expectPanelAccepts(events)

      const res = await get(`/api/v1/ai/sessions/${id}/blobs/${SENT}`)
      expect(res.status).toBe(200)
      expect(res.headers.get('content-type')).toBe('image/jpeg')
      expect(res.headers.get('x-content-type-options')).toBe('nosniff')
      expect(res.headers.get('content-disposition')).toBe('inline')
      expect(res.headers.get('cache-control')).toMatch(/^private, .*immutable/)
      expect(Buffer.from(await res.arrayBuffer()).equals(Buffer.from(JPEG, 'base64'))).toBe(true)
    })

    it('serves it to the UI only, under its own session, and not to another principal', async () => {
      const id = await withSent()
      const path = `/api/v1/ai/sessions/${id}/blobs/${SENT}`
      expect((await get(path, { ...UI_READ, 'sec-fetch-site': 'cross-site' })).status).toBe(403)
      expect((await get(path, { ...UI_READ, origin: 'https://evil.example' })).status).toBe(403)
      next = { reply: 'nothing to see' }
      const { session: other } = await m.start(browser, { origin: 'chat' })
      expect((await get(`/api/v1/ai/sessions/${other.id}/blobs/${SENT}`)).status).toBe(404)
      // A name this session never stored: the full view falls back to the preview.
      expect((await get(`/api/v1/ai/sessions/${id}/blobs/${'0'.repeat(64)}.jpg`)).status).toBe(404)
      // The owner reads it as the session; any other principal finds no session.
      await expect(m.blob(id, SENT, browser)).resolves.toMatchObject({ mediaType: 'image/jpeg' })
      await expect(m.blob(id, SENT, agentA)).rejects.toMatchObject({ code: 'not_found' })
    })

    it('logs the previews without names when the images cannot be stored', async () => {
      await db.sql`ALTER TABLE ai_session_blobs ADD CONSTRAINT no_more CHECK (false) NOT VALID`
      const id = await withSent()
      const turn = (await logged(id)).find((e) => e.type === 'user.turn')
      expect(turn).toMatchObject({ images: [preview] })
      expect(turn?.type === 'user.turn' && turn.images?.[0] && 'name' in turn.images[0]).toBe(false)
    })
  })

  it('shows the call without its images when they cannot be stored', async () => {
    await db.sql`ALTER TABLE ai_session_blobs ADD CONSTRAINT no_more CHECK (false) NOT VALID`
    const id = await withImage()
    const result = (await logged(id)).find((e) => e.type === 'tool.result')
    expect(result).toMatchObject({ type: 'tool.result', ok: true })
    expect(result && 'images' in result).toBe(false)
  })
})
