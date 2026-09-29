import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentBridge } from './bridge'
import { createTabLink, MAX_RESULT_CHARS } from './link'
import { TAB_ID } from './tabId'

/** Just enough of a browser WebSocket for the link. */
class FakeSocket {
  static instances: FakeSocket[] = []
  readonly OPEN = 1
  readyState = 0
  sent: string[] = []
  onopen: (() => void) | null = null
  onmessage: ((event: { data: unknown }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  closed = false
  readonly url: string
  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }
  send(data: string) {
    this.sent.push(data)
  }
  close() {
    this.closed = true
  }
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  drop(code = 1006) {
    this.readyState = 3
    this.onclose?.({ code })
  }
  frames() {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>)
  }
  deliver(frame: Record<string, unknown>) {
    this.onmessage?.({ data: JSON.stringify({ v: 1, ...frame }) })
  }
}

const Impl = FakeSocket as unknown as typeof WebSocket
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))
const PAIRING = { id: 'p1', label: 'MCP token “laptop”', expiresAt: '2026-09-29T12:00:00.000Z' }

function linked(bridge = new AgentBridge()) {
  bridge.setRoute('/m/box')
  const link = createTabLink({ bridge, WebSocketImpl: Impl, baseMs: 100, maxMs: 1000 })
  link.connect()
  return { bridge, link, ws: () => FakeSocket.instances.at(-1)! }
}

describe('createTabLink', () => {
  beforeEach(() => {
    FakeSocket.instances = []
  })
  afterEach(() => vi.useRealTimers())

  it('connects to the bridge socket on the page origin and says hello with the tab id, route and live tools', () => {
    const bridge = new AgentBridge()
    bridge.register({ get_params: () => ({}) }, { label: 'customize' })
    const { link, ws } = linked(bridge)
    expect(ws().url).toBe(`ws://${window.location.host}/api/v1/ai/bridge`)
    expect(link.getState().connected).toBe(false)
    ws().open()
    expect(ws().frames()).toEqual([{ v: 1, type: 'hello', tabId: TAB_ID, route: '/m/box', live: ['get_params'] }])
    expect(TAB_ID).toMatch(/^[A-Za-z0-9_-]{22}$/)
    expect(link.getState().connected).toBe(true)
  })

  it('reports a route change or a page mounting, once each', async () => {
    const { bridge, ws } = linked()
    ws().open()
    bridge.setRoute('/settings')
    const unregister = bridge.register({ get_form: () => ({}) }, { label: 'settings' })
    await flush()
    expect(ws().frames().slice(1)).toEqual([{ v: 1, type: 'state', route: '/settings', live: ['get_form'] }])
    unregister()
    await flush()
    expect(ws().frames().at(-1)).toEqual({ v: 1, type: 'state', route: '/settings', live: [] })
  })

  it("runs each call through the bridge and answers with the bridge's result, errors included", async () => {
    const bridge = new AgentBridge()
    bridge.register({ set_param: ({ name, value }) => ({ name, value }) }, { label: 'customize' })
    const { ws } = linked(bridge)
    ws().open()
    ws().deliver({ type: 'call', id: 'c1', tool: 'set_param', args: { name: 'w', value: 3 } })
    ws().deliver({ type: 'call', id: 'c2', tool: 'set_param', args: { name: 'w' } })
    ws().deliver({ type: 'call', id: 'c3', tool: 'get_form', args: {} })
    await vi.waitFor(() => expect(ws().frames().filter((f) => f.type === 'result')).toHaveLength(3))
    const results = Object.fromEntries(
      ws()
        .frames()
        .filter((f) => f.type === 'result')
        .map((f) => [f.id, f.outcome]),
    )
    expect(results.c1).toEqual({ ok: true, result: { name: 'w', value: 3 } })
    expect(results.c2).toMatchObject({ ok: false, error: { code: 'invalid_args' } })
    expect(results.c3).toMatchObject({ ok: false, error: { code: 'unavailable' } })
  })

  it('answers a result too large for the socket with an error the agent can act on', async () => {
    const bridge = new AgentBridge()
    bridge.register({ get_editor_text: () => ({ text: 'x'.repeat(MAX_RESULT_CHARS) }) }, { label: 'source' })
    const { ws } = linked(bridge)
    ws().open()
    ws().deliver({ type: 'call', id: 'big', tool: 'get_editor_text', args: {} })
    await vi.waitFor(() => expect(ws().frames().some((f) => f.type === 'result')).toBe(true))
    const result = ws().frames().find((f) => f.type === 'result')!
    expect(result.outcome).toMatchObject({ ok: false, error: { code: 'failed', message: expect.stringMatching(/ask for less/) } })
    expect(ws().sent.at(-1)!.length).toBeLessThan(1000)
  })

  it('keeps the pairings the agent pushes, and sends the user’s answers', () => {
    const { link, ws } = linked()
    const seen = vi.fn()
    link.subscribe(seen)
    expect(link.accept('p1', 'ABCD-EFGH')).toBe(false)
    ws().open()
    ws().deliver({ type: 'pairings', pending: [PAIRING], paired: [] })
    expect(link.getState()).toMatchObject({ pending: [PAIRING], paired: [] })
    expect(seen).toHaveBeenCalled()

    expect(link.accept('p1', 'abcd-efgh')).toBe(true)
    ws().deliver({ type: 'pairing.result', id: 'p1', ok: false, message: 'That is not the code. 4 tries left.' })
    expect(link.getState().results).toEqual({ p1: { ok: false, message: 'That is not the code. 4 tries left.' } })
    ws().deliver({ type: 'pairings', pending: [], paired: [PAIRING] })
    expect(link.getState()).toMatchObject({ pending: [], paired: [PAIRING] })
    expect(link.end('p1')).toBe(true)
    expect(link.deny('p2')).toBe(true)
    expect(ws().frames().slice(1)).toEqual([
      { v: 1, type: 'pairing.accept', id: 'p1', code: 'abcd-efgh' },
      { v: 1, type: 'pairing.end', id: 'p1' },
      { v: 1, type: 'pairing.deny', id: 'p2' },
    ])
  })

  it('drops frames that are not the protocol', () => {
    const { link, ws } = linked()
    ws().open()
    ws().onmessage?.({ data: 'not json' })
    ws().deliver({ type: 'pairings', pending: 'nope' })
    ws().deliver({ type: 'call', id: 'c', tool: 'navigate' })
    expect(link.getState()).toMatchObject({ pending: [], paired: [] })
    expect(ws().frames()).toHaveLength(1)
  })

  it('reconnects with back-off as the same tab, and stops after close()', () => {
    vi.useFakeTimers()
    const { link, ws } = linked()
    const first = ws()
    first.open()
    first.deliver({ type: 'pairings', pending: [PAIRING], paired: [] })
    first.drop()
    expect(link.getState()).toMatchObject({ connected: false, pending: [] })
    vi.advanceTimersByTime(100)
    const second = ws()
    expect(second).not.toBe(first)
    second.open()
    expect(second.frames()[0]).toMatchObject({ type: 'hello', tabId: TAB_ID })

    link.close()
    expect(second.closed).toBe(true)
    second.drop()
    vi.advanceTimersByTime(10_000)
    expect(FakeSocket.instances).toHaveLength(2)
  })
})
