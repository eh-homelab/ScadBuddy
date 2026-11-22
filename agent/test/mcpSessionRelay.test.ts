import { afterEach, describe, expect, it } from 'vitest'
import type { Principal } from '../src/auth/principal.js'
import type { RelayRows } from '../src/mcp/relayRows.js'
import { SharedMcpSessionRelay, type SessionOwner } from '../src/mcp/sessionRelay.js'

// The relay's liveness over an in-memory channel the test can hold (#6563). Every
// listener hears every message, its sender's too, in send order, as on the API's
// change stream. A held channel delivers nothing until it is let go.

type Header = { t: string; req: string; to: string; from?: string }

class Channel {
  readonly #listeners: ((payload: string) => void)[] = []
  #held: string[] | undefined
  /** Drops a message before a listener hears it, when it returns true. */
  drop: (payload: string, listener: number) => boolean = () => false
  readonly sent: Header[] = []

  listen(onNotify: (payload: string) => void): void {
    this.#listeners.push(onNotify)
  }

  publish(payload: string): void {
    this.sent.push(JSON.parse(payload) as Header)
    if (this.#held) this.#held.push(payload)
    else this.#deliver(payload)
  }

  hold(): void {
    this.#held ??= []
  }

  letGo(): void {
    const held = this.#held ?? []
    this.#held = undefined
    for (const payload of held) this.#deliver(payload)
  }

  #deliver(payload: string): void {
    this.#listeners.forEach((f, i) => {
      if (!this.drop(payload, i)) queueMicrotask(() => f(payload))
    })
  }
}

class MemoryRows implements RelayRows {
  readonly owners = new Map<string, string>()
  readonly #rows = new Map<string, unknown>()
  readonly #channel: Channel
  constructor(channel: Channel) {
    this.#channel = channel
  }
  async register(hash: string, replica: string): Promise<void> {
    this.owners.set(hash, replica)
  }
  async touch(): Promise<void> {}
  async remove(hash: string, replica?: string): Promise<boolean> {
    if (replica !== undefined && this.owners.get(hash) !== replica) return false
    return this.owners.delete(hash)
  }
  async owner(hash: string): Promise<string | undefined> {
    return this.owners.get(hash)
  }
  async counts(): Promise<{ total: number; caller: number }> {
    return { total: this.owners.size, caller: 0 }
  }
  async send(header: object, body?: { row: string; value: unknown }): Promise<void> {
    if (body) this.#rows.set(body.row, body.value)
    this.#channel.publish(JSON.stringify(header))
  }
  async take(row: string): Promise<unknown> {
    const value = this.#rows.get(row)
    this.#rows.delete(row)
    return value
  }
  async withdraw(row: string): Promise<boolean> {
    return this.#rows.delete(row)
  }
  async sweep(): Promise<void> {}
}

const BEAT_MS = 10
const PRINCIPAL: Principal = { id: 'token:t', kind: 'bearer', tiers: ['read'] }
const REQUEST = { method: 'POST', url: 'http://agent/mcp', headers: [] as [string, string][], body: '{}' }

/** Resolves once `check` holds, looked at every beat; no deadline but the test's own. */
async function until(check: () => boolean): Promise<void> {
  while (!check()) await new Promise((r) => setTimeout(r, BEAT_MS))
}

describe('the /mcp relay counts a peer silent only while the channel carries its own messages (#6563)', () => {
  const relays: SharedMcpSessionRelay[] = []
  afterEach(() => {
    for (const relay of relays.splice(0)) relay.close()
  })

  /** Owner A (listener 0) and relaying replica B (listener 1) on one channel; A's run waits for `answer`. */
  function pair() {
    const channel = new Channel()
    const rows = new MemoryRows(channel)
    const options = { ackTimeoutMs: 1_000, beatMs: BEAT_MS, log: () => {} }
    const a = new SharedMcpSessionRelay(rows, { ...options, listen: (_c, f) => channel.listen(f) })
    const b = new SharedMcpSessionRelay(rows, { ...options, listen: (_c, f) => channel.listen(f) })
    relays.push(a, b)
    let answer!: () => void
    const answered = new Promise<void>((r) => (answer = r))
    let running = false
    const owner: SessionOwner = {
      run: async () => {
        running = true
        await answered
        return new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } })
      },
    }
    a.serve(owner)
    const beatsFrom = (relay: SharedMcpSessionRelay) => channel.sent.filter((h) => h.t === 'beat' && h.to !== relay.replica).length
    return { channel, rows, a, b, answer, isRunning: () => running, beatsFrom }
  }

  it('keeps a live owner while the channel delivers nothing, however many beats that lasts', async () => {
    const { channel, rows, a, b, answer, isRunning, beatsFrom } = pair()
    await rows.register('hash', a.replica)
    const pending = b.forward('hash', PRINCIPAL, REQUEST, new AbortController().signal)
    let settled = false
    void pending.finally(() => (settled = true))
    await until(() => isRunning() && channel.sent.some((h) => h.t === 'ack'))
    // Nothing is delivered for many times the beats that make a peer gone: both sides tick, and
    // both still talk, but neither hears the other or itself. (A side that took the other for
    // gone stops beating, and B then answers 503 at once.)
    channel.hold()
    const sentA = beatsFrom(a)
    const sentB = beatsFrom(b)
    await until(() => settled || (beatsFrom(a) >= sentA + 12 && beatsFrom(b) >= sentB + 12))
    channel.letGo()
    answer()
    const res = (await pending)!
    expect(res.status).toBe(200)
    expect(await res.text()).toBe('{"ok":true}')
    expect(rows.owners.get('hash')).toBe(a.replica)
  })

  it('still finds an owner gone when its own beats come back and the owner says nothing', async () => {
    const { channel, rows, a, b, isRunning } = pair()
    await rows.register('hash', a.replica)
    const pending = b.forward('hash', PRINCIPAL, REQUEST, new AbortController().signal)
    await until(() => isRunning() && channel.sent.some((h) => h.t === 'ack'))
    // A dies: nothing more from it, and it hears nothing.
    a.close()
    channel.drop = (payload) => (JSON.parse(payload) as Header).to === b.replica
    const res = (await pending)!
    expect(res.status).toBe(503)
    expect(rows.owners.has('hash')).toBe(false)
  })

  it('still stops an answer when its own beats come back and the relaying replica says nothing', async () => {
    const { channel, rows, a, b } = pair()
    await rows.register('hash', a.replica)
    let aborted = false
    a.serve({
      run: async (_hash, _principal, _request, signal) => {
        await new Promise<void>((r) => signal.addEventListener('abort', () => r(), { once: true }))
        aborted = true
        return new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } })
      },
    })
    // A hears none of B's beats, but hears everything else, its own messages included.
    channel.drop = (payload, listener) => {
      const header = JSON.parse(payload) as Header
      return listener === 0 && header.t === 'beat' && header.to === a.replica
    }
    const pending = b.forward('hash', PRINCIPAL, REQUEST, new AbortController().signal)
    const res = (await pending)!
    expect(aborted).toBe(true)
    expect(res.status).toBe(200)
    await expect(res.text()).rejects.toThrow(/failed while answering/)
  })
})
