import {
  PROTOCOL_VERSION,
  clientMessage,
  parseClientMessage,
  parseServerEvent,
  type ServerEvent,
} from './protocol'

const owner = { kind: 'browser', id: 'browser', label: 'You' } as const

const valid: ServerEvent[] = [
  { v: 1, type: 'sessions.snapshot', sessions: [{ sessionId: 's1', title: 't', origin: 'mcp', owner, status: 'idle' }] },
  { v: 1, type: 'session.started', sessionId: 's1', origin: 'chat', owner },
  { v: 1, type: 'session.owner', sessionId: 's1', owner },
  { v: 1, type: 'user.turn', sessionId: 's1', turnId: 'u1', text: 'hi', author: owner },
  { v: 1, type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'Hel' },
  { v: 1, type: 'assistant.text.done', sessionId: 's1', messageId: 'm1' },
  { v: 1, type: 'tool.call', sessionId: 's1', id: 't1', name: 'mcp__scadbuddy__render_model', input: { slug: 'x' }, risk: 'write' },
  {
    v: 1,
    type: 'tool.result',
    sessionId: 's1',
    id: 't1',
    ok: true,
    summary: 'ok',
    sources: [{ title: 'Manual', url: 'https://example.org' }],
    version: { slug: 'x', revision: 'abc' },
  },
  { v: 1, type: 'approval.required', sessionId: 's1', id: 'a1', tool: 't2', summary: 'Send?', risk: 'outward' },
  { v: 1, type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: true, by: owner },
  { v: 1, type: 'session.status', sessionId: 's1', status: 'waiting_approval' },
  { v: 1, type: 'session.result', sessionId: 's1', costUsd: 0.02, turns: 3 },
  { v: 1, type: 'error', message: 'boom' },
  { v: 1, type: 'memory', sessionId: 's1', turnId: 'u1', action: 'recall', bank: 'b', outcome: 'ok', count: 3 },
]

describe('parseServerEvent', () => {
  it.each(valid.map((e) => [e.type, e] as const))('accepts %s', (_type, event) => {
    expect(parseServerEvent(event)).toEqual({ ok: true, value: event })
  })

  it('accepts a JSON text frame', () => {
    const event = valid[4]
    expect(parseServerEvent(JSON.stringify(event))).toEqual({ ok: true, value: event })
  })

  it.each([
    ['not JSON', '{nope'],
    ['not an object', 42],
    ['null', null],
    ['an unknown type', { v: 1, type: 'session.explode', sessionId: 's1' }],
    ['a missing version', { type: 'session.status', sessionId: 's1', status: 'idle' }],
    ['a future version', { v: 2, type: 'session.status', sessionId: 's1', status: 'idle' }],
    ['an unknown status', { v: 1, type: 'session.status', sessionId: 's1', status: 'sleeping' }],
    ['a delta without a message id', { v: 1, type: 'assistant.text.delta', sessionId: 's1', delta: 'x' }],
    ['a tool call with an unknown risk', { v: 1, type: 'tool.call', sessionId: 's1', id: 't', name: 'n', input: {}, risk: 'spicy' }],
    ['a tool call whose input is not an object', { v: 1, type: 'tool.call', sessionId: 's1', id: 't', name: 'n', input: 'rm -rf', risk: 'read' }],
    // §8.2: only outward steps pause for approval.
    ['an approval for a write step', { v: 1, type: 'approval.required', sessionId: 's1', id: 'a', tool: 't', summary: 's', risk: 'write' }],
    ['an approval without a summary', { v: 1, type: 'approval.required', sessionId: 's1', id: 'a', tool: 't', summary: '', risk: 'outward' }],
    ['a memory event with an unknown action', { v: 1, type: 'memory', sessionId: 's1', turnId: 'u1', action: 'forget', bank: 'b', outcome: 'ok' }],
    ['a negative cost', { v: 1, type: 'session.result', sessionId: 's1', costUsd: -1, turns: 1 }],
    ['an unknown origin', { v: 1, type: 'session.started', sessionId: 's1', origin: 'email', owner }],
    ['an empty session id', { v: 1, type: 'session.status', sessionId: '', status: 'idle' }],
  ])('rejects %s', (_label, raw) => {
    const result = parseServerEvent(raw)
    expect(result.ok).toBe(false)
  })

  it('names the version it refuses', () => {
    expect(parseServerEvent({ v: 7, type: 'error', message: 'x' })).toEqual({
      ok: false,
      error: 'unsupported protocol version 7',
    })
  })
})

describe('client messages', () => {
  it('stamps the protocol version', () => {
    const message = clientMessage({ type: 'approval.decision', sessionId: 's1', id: 'a1', approve: true })
    expect(message.v).toBe(PROTOCOL_VERSION)
    expect(parseClientMessage(message).ok).toBe(true)
  })

  it('rejects an empty user message', () => {
    expect(
      parseClientMessage({ v: 1, type: 'user.message', text: '', context: { route: '/' } }).ok,
    ).toBe(false)
  })
})
