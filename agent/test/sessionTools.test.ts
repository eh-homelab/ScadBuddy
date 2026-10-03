import type { Sql } from 'postgres'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryEventSource } from '../src/events/bus.js'
import type { EventLog } from '../src/sessions/eventLog.js'
import { busKindOf, followSessionEvents, SessionEventPublisher } from '../src/sessions/busEvents.js'
import { event } from '../src/sessions/protocol.js'
import { turnPrincipal } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool } from '../src/tools/registry.js'
import { condense, HANDOFF_TARGET, sessionTools } from '../src/tools/sessions.js'
import { firstText, services } from './helpers/mcp.js'
import { agentA, browser } from './support/sessions.js'

// The `sessions_*` tools' and the `session.*` bus events' own logic, without
// a database (#300). End to end over /mcp and Postgres: test/sessionTools.pg.test.ts.

const S = '0e5a3c1e-1111-4222-8333-944455556666'

describe('the sessions_* tools', () => {
  it('are in the registry with the tiers spec §8.1 gives them', () => {
    const tiers = Object.fromEntries(ALL_TOOLS.filter((t) => t.name.startsWith('sessions_')).map((t) => [t.name, t.risk]))
    expect(tiers).toEqual({
      sessions_list: 'read',
      sessions_get: 'read',
      sessions_resources: 'read',
      sessions_attach: 'read',
      sessions_list_approvals: 'read',
      sessions_start: 'write',
      sessions_send: 'write',
      sessions_fork: 'write',
      sessions_interrupt: 'write',
      sessions_handoff: 'write',
      sessions_accept_handoff: 'write',
      sessions_cancel_handoff: 'write',
      sessions_approve: 'outward',
      sessions_deny: 'outward',
    })
    // Deciding is the approval path itself, so it is not gated a second time.
    for (const t of sessionTools) expect(t.gated, t.name).toBe(false)
    // Names the Messages API accepts (no dots), so the harness can offer them too.
    for (const t of sessionTools) expect(t.name).toMatch(/^[a-zA-Z0-9_-]{1,64}$/)
  })

  it('say so when there is no database', async () => {
    const tool = sessionTools.find((t) => t.name === 'sessions_list')!
    const result = await runTool(tool, {}, {
      ...services(),
      principal: { id: 'token:a', kind: 'bearer', tiers: ['read'] },
      progress: async () => {},
      signal: new AbortController().signal,
    })
    expect(result.isError).toBe(true)
    expect(firstText(result)).toMatch(/need the database/)
  })

  it('hand off only to principal ids that can exist (PR #715 review)', () => {
    for (const to of [
      'browser',
      'token:0e5a3c1e-1111-4222-8333-944455556666',
      'oidc:https://idp.example/#alice',
      'oidc:http://127.0.0.1:8080/realms/sb#f81d4fae-7dec-11d0-a765-00a0c91e6bf6',
    ]) {
      expect(to, to).toMatch(HANDOFF_TARGET)
    }
    for (const to of [
      '',
      'Browser',
      // Not a UUID: 36 dashes, 36 hex digits, the wrong grouping, upper case (ids are lower case).
      `token:${'-'.repeat(36)}`,
      `token:${'a'.repeat(36)}`,
      'token:0e5a3c1e1-111-4222-8333-944455556666',
      'token:0E5A3C1E-1111-4222-8333-944455556666',
      'token:0e5a3c1e-1111-4222-8333-944455556666 ',
      // Not `oidc:<issuer>#<sub>`: no issuer URL, no `#`, an empty or too long `sub`, whitespace in the issuer.
      'oidc:alice',
      'oidc:https://idp.example/',
      'oidc:https://idp.example/#',
      `oidc:https://idp.example/#${'s'.repeat(256)}`,
      'oidc:idp.example#alice',
      'oidc:https://idp example/#alice',
      'oidc:https://idp.example/#al\nice',
    ]) {
      expect(to, JSON.stringify(to)).not.toMatch(HANDOFF_TARGET)
    }
    const handoff = sessionTools.find((t) => t.name === 'sessions_handoff')!
    expect(() => handoff.parse({ session_id: S, to: 'oidc:alice' })).toThrow(/oidc:<issuer>#<sub>/)
  })

  it('condense streamed text into one entry per message, dropping the envelope', () => {
    const rows = [
      { seq: 1, event: event({ type: 'user.turn', sessionId: S, turnId: 't', text: 'hi', author: browser }) },
      { seq: 2, event: event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm1', delta: 'Hel' }) },
      { seq: 3, event: event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm1', delta: 'lo' }) },
      { seq: 4, event: event({ type: 'assistant.text.done', sessionId: S, messageId: 'm1' }) },
      { seq: 5, event: event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm2', delta: 'Again' }) },
      { seq: 6, event: event({ type: 'session.status', sessionId: S, status: 'idle' }) },
    ]
    expect(condense(rows)).toEqual([
      { seq: 1, type: 'user.turn', turnId: 't', text: 'hi', author: browser },
      { seq: 4, type: 'assistant.text', message_id: 'm1', text: 'Hello', done: true },
      { seq: 5, type: 'assistant.text', message_id: 'm2', text: 'Again', done: false },
      { seq: 6, type: 'session.status', status: 'idle' },
    ])
  })
})

describe("a turn's principal", () => {
  it("is the sender's tiers for a token-owned session, and the owner's default otherwise", () => {
    expect(turnPrincipal(agentA).tiers).toEqual(['read'])
    expect(turnPrincipal(agentA, { tiers: ['write', 'read'] }).tiers).toEqual(['read', 'write'])
    expect(turnPrincipal(agentA, {}).tiers).toEqual(['read'])
    // The browser user holds every tier whatever a turn says.
    expect(turnPrincipal(browser, { tiers: ['read'] }).tiers).toEqual(['read', 'write', 'outward'])
  })
})

describe('session.* on the bus', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('announces each batch as its most significant kind, with the status it leaves', () => {
    const status = (s: 'idle' | 'running' | 'waiting_approval' | 'done') => event({ type: 'session.status', sessionId: S, status: s })
    expect(busKindOf([event({ type: 'session.started', sessionId: S, origin: 'mcp', owner: agentA }), status('idle')])).toEqual({
      kind: 'session.started',
      status: 'idle',
    })
    expect(busKindOf([event({ type: 'session.owner', sessionId: S, owner: browser })])).toEqual({ kind: 'session.owner' })
    expect(busKindOf([status('waiting_approval')])).toEqual({ kind: 'session.waiting', status: 'waiting_approval' })
    expect(busKindOf([event({ type: 'session.result', sessionId: S, turns: 1 }), status('idle')])).toEqual({
      kind: 'session.done',
      status: 'idle',
    })
    expect(busKindOf([status('running')])).toEqual({ kind: 'session.message', status: 'running' })
    expect(busKindOf([event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm', delta: 'x' })])).toEqual({
      kind: 'session.message',
    })
  })

  function fakeSql() {
    const payloads: Record<string, unknown>[] = []
    const sql = ((_strings: TemplateStringsArray, channel: string, payload: string) => {
      expect(channel).toBe('scadbuddy_events')
      payloads.push(JSON.parse(payload) as Record<string, unknown>)
      return Promise.resolve([])
    }) as unknown as Sql
    return { sql, payloads }
  }

  it('sends ids only, and throttles streamed messages per session, never lifecycle changes', () => {
    vi.useFakeTimers()
    const { sql, payloads } = fakeSql()
    const publisher = new SessionEventPublisher(sql, { replica: 'r1', throttleMs: 100 })
    const delta = (n: number) => [event({ type: 'assistant.text.delta', sessionId: S, messageId: 'm', delta: `secret ${n}` })]
    for (let seq = 1; seq <= 20; seq++) publisher.onAppend(S, delta(seq), seq)
    expect(payloads).toHaveLength(1)
    expect(payloads[0]).toEqual({ id: expect.any(String), at: expect.any(String), kind: 'session.message', session_id: S, seq: 1, replica: 'r1' })
    expect(JSON.stringify(payloads)).not.toContain('secret')
    vi.advanceTimersByTime(100)
    // One trailing message with the latest seq.
    expect(payloads.map((p) => p.seq)).toEqual([1, 20])
    // A lifecycle change goes at once, and drops a trailing message it covers.
    publisher.onAppend(S, delta(21), 21)
    publisher.onAppend(S, [event({ type: 'session.status', sessionId: S, status: 'idle' })], 22)
    vi.advanceTimersByTime(1000)
    expect(payloads.map((p) => [p.kind, p.seq])).toEqual([
      ['session.message', 1],
      ['session.message', 20],
      ['session.done', 22],
    ])
    publisher.close()
  })

  it("wakes followers for other replicas' sessions, all of them after a resync or reconnect", () => {
    const woken: string[] = []
    const log = { wake: (id: string) => woken.push(id), wakeAll: () => woken.push('*') } as unknown as EventLog
    const bus = new MemoryEventSource()
    const stop = followSessionEvents(bus, log, 'me')
    bus.emit({ id: '1', kind: 'session.message', session_id: 'a', seq: 3, replica: 'other' })
    bus.emit({ id: '2', kind: 'session.message', session_id: 'b', seq: 3, replica: 'me' })
    bus.emit({ id: '3', kind: 'job.done', job_id: 'j', slug: 's' })
    bus.resync()
    bus.reconnected()
    expect(woken).toEqual(['a', '*', '*'])
    stop()
    bus.emit({ id: '4', kind: 'session.done', session_id: 'c', seq: 1, replica: 'other' })
    expect(woken).toHaveLength(3)
  })
})
