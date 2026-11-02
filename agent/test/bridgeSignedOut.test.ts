import { Hono } from 'hono'
import type { UpgradeWebSocket, WSContext, WSEvents } from 'hono/ws'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import { BrowserSessions, CACHE_MS, personSessions, SESSION_COOKIE, sessionIdHash } from '../src/auth/browserSession.js'
import { TabHub } from '../src/bridge/hub.js'
import { LINK_PROTOCOL_VERSION } from '../src/bridge/protocol.js'
import type { AuthMode } from '../src/config.js'
import { MemoryEventSource } from '../src/events/bus.js'
import { originPolicy } from '../src/http/origins.js'
import { registerBridgeRoute } from '../src/routes/bridge.js'
import { SIGNED_OUT } from '../src/routes/chat.js'

// #5217: a person's tab socket (the browser bridge) lives only as long as their session.
// A revocation on the bus closes it with 4401 at once; a missed one is caught by the
// periodic re-read. Once closed, the tab is no longer reachable for that person's calls.

const PERSON = 'user:https://idp.test#alice'
const TOKEN = 'c'.repeat(43)
const TAB = 't'.repeat(22)
const ROW = { principal: PERSON, name: 'Alice', kind: 'login' }

const settle = (ms = 10) => new Promise((r) => setTimeout(r, ms))

async function openTab(mode: AuthMode) {
  let rows: unknown[] = [ROW]
  let failing = false
  let now = 0
  const sql = (() => (failing ? Promise.reject(new Error('down')) : Promise.resolve(rows))) as unknown as Sql
  const browserSessions = new BrowserSessions(sql, () => now)
  const bus = new MemoryEventSource()
  browserSessions.follow(bus)
  const tabs = new TabHub()

  let events: WSEvents | undefined
  const capture = ((create: (c: unknown) => WSEvents | Promise<WSEvents>) => async (c: unknown) => {
    events = await create(c)
    return new Response()
  }) as unknown as UpgradeWebSocket
  const app = new Hono()
  app.use('/api/v1/ai/*', personSessions(browserSessions, mode, () => {}))
  registerBridgeRoute(app, {
    tabs,
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    upgradeWebSocket: capture,
    browserSessions,
    authMode: mode,
    sessionCheckMs: 20,
  })
  const res = await app.request('/api/v1/ai/bridge', {
    headers: {
      host: 'scadbuddy.example',
      origin: 'https://scadbuddy.example',
      'x-forwarded-proto': 'https',
      upgrade: 'websocket',
      cookie: `${SESSION_COOKIE}=${TOKEN}`,
    },
  })
  expect(res.status).toBe(200)
  let closed: [number | undefined, string | undefined] | undefined
  const ws = {
    send: () => {},
    close: (code?: number, reason?: string) => {
      closed = [code, reason]
      events?.onClose?.(new Event('close') as CloseEvent, ws)
    },
  } as unknown as WSContext
  events!.onOpen?.(new Event('open'), ws)
  events!.onMessage?.(
    new MessageEvent('message', { data: JSON.stringify({ v: LINK_PROTOCOL_VERSION, type: 'hello', tabId: TAB, route: '/', live: [] }) }),
    ws,
  )
  await settle()
  expect(tabs.connected()).toEqual([TAB])

  return {
    tabs,
    closed: () => closed,
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
    close: () => events?.onClose?.(new Event('close') as CloseEvent, ws),
  }
}

describe('a bridge tab socket whose session is revoked (#5217)', () => {
  it('closes with 4401 when the bus says the session was revoked, and the tab is gone', async () => {
    const tab = await openTab('observe')
    tab.revoke({ session_id: sessionIdHash(TOKEN) })
    expect(tab.closed()).toEqual([SIGNED_OUT, 'signed out'])
    expect(tab.tabs.connected()).toEqual([])
  })

  it('closes on "sign out everywhere", which names only the principal', async () => {
    const tab = await openTab('observe')
    tab.revoke({})
    expect(tab.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('stays open for another session revoked', async () => {
    const tab = await openTab('observe')
    tab.revoke({ session_id: sessionIdHash('another') })
    expect(tab.closed()).toBeUndefined()
    expect(tab.tabs.connected()).toEqual([TAB])
    tab.close()
  })

  it('re-reads the session on a timer, so a revocation it never heard still closes it', async () => {
    const tab = await openTab('observe')
    tab.revokeUnheard()
    tab.advance(CACHE_MS)
    await settle(80)
    expect(tab.closed()?.[0]).toBe(SIGNED_OUT)
    expect(tab.tabs.connected()).toEqual([])
  })

  it('re-checks when the bus may have missed events', async () => {
    const tab = await openTab('observe')
    tab.revokeUnheard()
    tab.resync()
    await settle()
    expect(tab.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('stays open while the session cannot be read, and closes once it reads as revoked', async () => {
    const tab = await openTab('observe')
    tab.fail(true)
    tab.advance(CACHE_MS)
    await settle(80)
    expect(tab.closed()).toBeUndefined()
    tab.revokeUnheard()
    tab.fail(false)
    await settle(80)
    expect(tab.closed()?.[0]).toBe(SIGNED_OUT)
  })

  it('changes nothing with SCADBUDDY_AUTH_MODE=off', async () => {
    const tab = await openTab('off')
    tab.revoke({ session_id: sessionIdHash(TOKEN) })
    tab.advance(CACHE_MS)
    await settle(80)
    expect(tab.closed()).toBeUndefined()
    expect(tab.tabs.connected()).toEqual([TAB])
    tab.close()
  })
})
