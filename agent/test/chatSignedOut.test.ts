import { Hono } from 'hono'
import type { UpgradeWebSocket, WSContext, WSEvents } from 'hono/ws'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { BrowserSessions, CACHE_MS, personSessions, SESSION_COOKIE, sessionIdHash } from '../src/auth/browserSession.js'
import type { AuthMode } from '../src/config.js'
import { MemoryEventSource } from '../src/events/bus.js'
import { originPolicy } from '../src/http/origins.js'
import { registerChatRoute, SIGNED_OUT } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import type { Owner } from '../src/sessions/protocol.js'

// #5217: the chat socket acts as the signed-in person only while their session lives. A
// revocation (`auth.session_revoked` on the bus, which every replica hears) closes it with
// 4401 at once; a missed one is caught by the re-check before every frame and snapshot.

const PERSON = 'user:https://idp.test#alice'
const TOKEN = 'b'.repeat(43)
const ROW = { principal: PERSON, name: 'Alice', kind: 'login' }

async function openSocket(mode: AuthMode) {
  let rows: unknown[] = [ROW]
  let failing = false
  let now = 0
  const sql = (() => (failing ? Promise.reject(new Error('down')) : Promise.resolve(rows))) as unknown as Sql
  const browserSessions = new BrowserSessions(sql, () => now)
  const bus = new MemoryEventSource()
  browserSessions.follow(bus)

  // Everything the socket does as someone, and who it did it as.
  const actedAs: [string, string][] = []
  const sessions = {
    snapshot: (owner: Owner) => {
      actedAs.push(['snapshot', owner.id])
      return Promise.resolve({ v: 1, type: 'sessions.snapshot', sessions: [] })
    },
    interrupt: (_id: string, owner: Owner) => {
      actedAs.push(['interrupt', owner.id])
      return Promise.resolve()
    },
  } as unknown as SessionManager

  let events: WSEvents | undefined
  const capture = ((create: (c: unknown) => WSEvents | Promise<WSEvents>) => async (c: unknown) => {
    events = await create(c)
    return new Response()
  }) as unknown as UpgradeWebSocket
  const app = new Hono()
  app.use('/api/v1/ai/*', personSessions(browserSessions, mode, () => {}))
  registerChatRoute(app, {
    sessions,
    ready: () => Promise.resolve(true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    upgradeWebSocket: capture,
    snapshotMs: 20,
    browserSessions,
    authMode: mode,
  })
  const res = await app.request('/api/v1/ai/chat', {
    headers: {
      host: 'scadbuddy.example',
      origin: 'https://scadbuddy.example',
      'x-forwarded-proto': 'https',
      upgrade: 'websocket',
      cookie: `${SESSION_COOKIE}=${TOKEN}`,
    },
  })
  expect(res.status).toBe(200)
  const sent: { type: string; code?: string }[] = []
  let closed: [number | undefined, string | undefined] | undefined
  const ws = {
    raw: { bufferedAmount: 0 },
    send: (data: string) => sent.push(JSON.parse(data) as (typeof sent)[number]),
    close: (code?: number, reason?: string) => {
      closed = [code, reason]
      events?.onClose?.(new Event('close') as CloseEvent, ws)
    },
  } as unknown as WSContext
  events!.onOpen?.(new Event('open'), ws)
  await settle()
  expect(actedAs[0]).toEqual(['snapshot', PERSON])

  return {
    actedAs,
    sent,
    closed: () => closed,
    interrupt: () => events!.onMessage?.(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'session.interrupt', sessionId: 's-1' }) }), ws),
    revoke: (event: { session_id?: string }) => {
      rows = []
      bus.emit({ id: 'e1', kind: 'auth.session_revoked', principal: PERSON, ...event })
    },
    revokeUnheard: () => {
      rows = []
    },
    resync: () => bus.resync(),
    fail: (on: boolean) => {
      failing = on
    },
    advance: (ms: number) => {
      now += ms
    },
  }
}

const settle = (ms = 10) => new Promise((r) => setTimeout(r, ms))

describe('a chat socket whose session is revoked (#5217)', () => {
  it('closes with 4401 when the bus says the session was revoked, and acts as no one after', async () => {
    const socket = await openSocket('observe')
    socket.interrupt()
    await settle()
    expect(socket.actedAs.filter(([what]) => what === 'interrupt')).toEqual([['interrupt', PERSON]])
    socket.revoke({ session_id: sessionIdHash(TOKEN) })
    expect(socket.closed()).toEqual([SIGNED_OUT, 'signed out'])
    const before = socket.actedAs.length
    socket.interrupt()
    await settle(60)
    expect(socket.actedAs.length).toBe(before)
  })

  it('closes on "sign out everywhere", which names only the principal', async () => {
    const socket = await openSocket('observe')
    socket.revoke({})
    expect(socket.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('stays open for another session revoked', async () => {
    const socket = await openSocket('observe')
    socket.revoke({ session_id: sessionIdHash('another') })
    expect(socket.closed()).toBeUndefined()
  })

  it('re-checks before a frame, so a revocation it never heard still stops it', async () => {
    const socket = await openSocket('observe')
    socket.revokeUnheard()
    socket.advance(CACHE_MS)
    const before = socket.actedAs.length
    socket.interrupt()
    await settle()
    expect(socket.closed()?.[0]).toBe(SIGNED_OUT)
    expect(socket.actedAs.slice(before)).toEqual([])
  })

  it('re-checks on its snapshot timer, without a frame', async () => {
    const socket = await openSocket('observe')
    socket.revokeUnheard()
    socket.advance(CACHE_MS)
    await settle(60)
    expect(socket.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('re-checks when the bus may have missed events', async () => {
    const socket = await openSocket('observe')
    socket.revokeUnheard()
    socket.resync()
    await settle()
    expect(socket.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('refuses a frame while the session cannot be read, and stays open', async () => {
    const socket = await openSocket('observe')
    socket.fail(true)
    socket.advance(CACHE_MS)
    const before = socket.actedAs.length
    socket.interrupt()
    await settle()
    expect(socket.closed()).toBeUndefined()
    expect(socket.actedAs.slice(before).filter(([what]) => what === 'interrupt')).toEqual([])
    expect(socket.sent.at(-1)).toMatchObject({ type: 'error', code: 'unavailable' })
  })

  it('changes nothing with SCADBUDDY_AUTH_MODE=off', async () => {
    const socket = await openSocket('off')
    socket.revoke({ session_id: sessionIdHash(TOKEN) })
    socket.advance(CACHE_MS)
    socket.interrupt()
    await settle()
    expect(socket.closed()).toBeUndefined()
    expect(socket.actedAs.filter(([what]) => what === 'interrupt')).toEqual([['interrupt', PERSON]])
  })
})
