import type { ServerEvent } from './protocol'
import { chatReducer, initialChatState, isBusy, type ChatAction, type ChatState } from './state'

const you = { kind: 'browser', id: 'browser', label: 'You' } as const
const desktop = { kind: 'bearer', id: 'tok', label: 'Claude Desktop' } as const

type Body<E> = E extends ServerEvent ? Omit<E, 'v'> : never
const server = (body: Body<ServerEvent>): ChatAction => ({
  type: 'server',
  event: { v: 1, ...body } as ServerEvent,
})

function run(actions: ChatAction[], from: ChatState = initialChatState): ChatState {
  return actions.reduce(chatReducer, from)
}

const started = run([
  { type: 'started-new' },
  server({ type: 'session.started', sessionId: 's1', origin: 'chat', owner: you }),
])

describe('chatReducer', () => {
  it('adopts the session its own first turn started', () => {
    expect(started.activeId).toBe('s1')
    expect(started.awaitingStart).toBe(false)
    expect(started.sessions.s1?.status).toBe('running')
  })

  it('does not jump to a session another principal starts', () => {
    const state = run([server({ type: 'session.started', sessionId: 'x', origin: 'mcp', owner: desktop })], started)
    expect(state.activeId).toBe('s1')
    expect(state.order).toEqual(['x', 's1'])
  })

  it('accumulates text deltas into one message and closes it on done', () => {
    const state = run(
      [
        server({ type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'Hel' }),
        server({ type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'lo **wor' }),
        server({ type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'ld**' }),
      ],
      started,
    )
    expect(state.sessions.s1?.items).toEqual([{ kind: 'assistant', id: 'm1', text: 'Hello **world**', done: false }])
    const done = run([server({ type: 'assistant.text.done', sessionId: 's1', messageId: 'm1' })], state)
    expect(done.sessions.s1?.items[0]).toMatchObject({ done: true })
  })

  it('attaches a tool result, its sources and its version to the call', () => {
    const state = run(
      [
        server({ type: 'tool.call', sessionId: 's1', id: 't1', name: 'set_parameters', input: { a: 1 }, risk: 'write' }),
        server({
          type: 'tool.result',
          sessionId: 's1',
          id: 't1',
          ok: true,
          summary: 'Set a',
          sources: [{ title: 'why' }],
          version: { slug: 'k', revision: 'r1' },
        }),
      ],
      started,
    )
    expect(state.sessions.s1?.items).toEqual([
      {
        kind: 'tool',
        id: 't1',
        name: 'set_parameters',
        input: { a: 1 },
        risk: 'write',
        result: { ok: true, summary: 'Set a', sources: [{ title: 'why' }], version: { slug: 'k', revision: 'r1' } },
      },
    ])
  })

  it('moves an approval pending → sent → approved, and only the server resolves it', () => {
    const waiting = run(
      [
        server({ type: 'approval.required', sessionId: 's1', id: 'a1', tool: 't2', summary: 'Send?', risk: 'outward' }),
        server({ type: 'session.status', sessionId: 's1', status: 'waiting_approval' }),
      ],
      started,
    )
    expect(waiting.sessions.s1?.items[0]).toMatchObject({ kind: 'approval', state: 'pending' })
    expect(isBusy(waiting.sessions.s1)).toBe(true)

    const sent = run([{ type: 'decided', sessionId: 's1', approvalId: 'a1' }], waiting)
    expect(sent.sessions.s1?.items[0]).toMatchObject({ state: 'sent' })

    const resolved = run(
      [server({ type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: true, by: you })],
      sent,
    )
    expect(resolved.sessions.s1?.items[0]).toMatchObject({ state: 'approved', by: you })
  })

  it('closes a half-streamed message when the session settles (an interrupt)', () => {
    const state = run(
      [
        server({ type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'Half' }),
        server({ type: 'session.status', sessionId: 's1', status: 'idle' }),
      ],
      started,
    )
    expect(state.sessions.s1?.items[0]).toMatchObject({ text: 'Half', done: true })
    expect(isBusy(state.sessions.s1)).toBe(false)
  })

  it('drops events for sessions it never heard of', () => {
    const state = run([server({ type: 'assistant.text.delta', sessionId: 'ghost', messageId: 'm', delta: 'x' })])
    expect(state).toEqual(initialChatState)
  })

  it('ignores a replayed user turn it already has', () => {
    const turn = server({ type: 'user.turn', sessionId: 's1', turnId: 'u1', text: 'hi', author: you })
    expect(run([turn, turn], started).sessions.s1?.items).toHaveLength(1)
  })

  it('clears a session it attaches to, because the server replays it', () => {
    const state = run(
      [server({ type: 'user.turn', sessionId: 's1', turnId: 'u1', text: 'hi', author: you }), { type: 'select', sessionId: 's1' }],
      started,
    )
    expect(state.sessions.s1?.items).toEqual([])
  })

  it('records a handoff', () => {
    const state = run(
      [
        server({ type: 'sessions.snapshot', sessions: [{ sessionId: 'x', title: 'Bin', origin: 'mcp', owner: desktop, status: 'idle' }] }),
        server({ type: 'session.owner', sessionId: 'x', owner: you }),
      ],
    )
    expect(state.sessions.x?.owner).toEqual(you)
  })

  it('puts session errors in the feed and the rest in the notice', () => {
    const state = run(
      [server({ type: 'error', sessionId: 's1', message: 'busy' }), server({ type: 'error', message: 'down' })],
      started,
    )
    expect(state.sessions.s1?.items).toEqual([{ kind: 'error', id: 'error-0', message: 'busy' }])
    expect(state.notice).toBe('down')
  })
})
