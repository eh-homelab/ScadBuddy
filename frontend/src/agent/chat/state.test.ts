import type { ServerEvent } from './protocol'
import { budgetUsed, chatReducer, initialChatState, isBusy, type ChatAction, type ChatState } from './state'

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

  it('moves a question pending → sent → answered, and shows a cancelled one as not answered (#940)', () => {
    const questions = [
      {
        question: 'Approve the draft?',
        header: 'Draft',
        multiSelect: false,
        options: [
          { label: 'Approve', description: 'File it', preview: '## Title' },
          { label: 'Cancel', description: 'Do not' },
        ],
      },
    ]
    const waiting = run(
      [
        server({ type: 'question.asked', sessionId: 's1', id: 'q1', tool: 't3', questions }),
        server({ type: 'session.status', sessionId: 's1', status: 'waiting_input' }),
      ],
      started,
    )
    expect(waiting.sessions.s1?.items[0]).toEqual({ kind: 'question', id: 'q1', tool: 't3', questions, state: 'pending' })
    expect(isBusy(waiting.sessions.s1)).toBe(true)

    const sent = run([{ type: 'answered', sessionId: 's1', questionId: 'q1' }], waiting)
    expect(sent.sessions.s1?.items[0]).toMatchObject({ state: 'sent' })
    const answered = run(
      [server({ type: 'question.resolved', sessionId: 's1', id: 'q1', answered: true, answers: ['Approve'], by: you })],
      sent,
    )
    expect(answered.sessions.s1?.items[0]).toMatchObject({ state: 'answered', answers: ['Approve'], by: you })

    const cancelled = run(
      [server({ type: 'question.resolved', sessionId: 's1', id: 'q1', answered: false, reason: 'interrupted by You' })],
      waiting,
    )
    expect(cancelled.sessions.s1?.items[0]).toMatchObject({ state: 'cancelled', reason: 'interrupted by You' })
    expect(cancelled.sessions.s1?.items[0]).not.toHaveProperty('answers')

    // Answered while offline: queued, and still not live after the reconnect's replay.
    const queued = run([{ type: 'answered', sessionId: 's1', questionId: 'q1', queued: true }], waiting)
    expect(queued.sessions.s1?.items[0]).toMatchObject({ state: 'queued' })
    const replayed = run(
      [{ type: 'select', sessionId: 's1' }, server({ type: 'question.asked', sessionId: 's1', id: 'q1', tool: 't3', questions })],
      queued,
    )
    expect(replayed.sessions.s1?.items[0]).toMatchObject({ state: 'sent' })

    // Refused by the agent: answerable again, with the error beside it.
    const refused = run([server({ type: 'error', sessionId: 's1', code: 'invalid', message: 'needs one answer each' })], sent)
    expect(refused.sessions.s1?.items[0]).toMatchObject({ state: 'pending' })
    expect(refused.sessions.s1?.items.at(-1)).toMatchObject({ kind: 'error', message: 'needs one answer each' })
    // A frame the agent could not parse has no session: the open one's card is answerable again.
    const unparsed = run([server({ type: 'error', code: 'invalid', message: 'ignored a malformed message' })], sent)
    expect(unparsed.sessions.s1?.items[0]).toMatchObject({ state: 'pending' })
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

  it('adds memory activity as its own item, a late retain after the turn settled included, and the same on replay', () => {
    const events = [
      server({ type: 'user.turn', sessionId: 's1', turnId: 'u1', text: 'hi', author: you }),
      server({ type: 'memory', sessionId: 's1', turnId: 'u1', action: 'recall', bank: 'b', outcome: 'ok', count: 2 }),
      server({ type: 'assistant.text.delta', sessionId: 's1', messageId: 'm1', delta: 'Hello' }),
      server({ type: 'assistant.text.done', sessionId: 's1', messageId: 'm1' }),
      server({ type: 'session.status', sessionId: 's1', status: 'idle' }),
      server({ type: 'memory', sessionId: 's1', turnId: 'u1', action: 'retain', bank: 'b', outcome: 'ok' }),
    ]
    const state = run(events, started)
    expect(state.sessions.s1?.status).toBe('idle')
    expect(state.sessions.s1?.items.map((i) => i.kind)).toEqual(['user', 'memory', 'assistant', 'memory'])
    expect(state.sessions.s1?.items[1]).toEqual({ kind: 'memory', id: 'memory-1', action: 'recall', bank: 'b', outcome: 'ok', count: 2 })
    expect(state.sessions.s1?.items[3]).toMatchObject({ kind: 'memory', action: 'retain', outcome: 'ok' })
    // Attaching clears the feed and the server replays the log in its order.
    const replayed = run([{ type: 'select', sessionId: 's1' }, ...events], state)
    expect(replayed.sessions.s1?.items).toEqual(state.sessions.s1?.items)
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

  it('takes a later snapshot as a live update: new sessions first, known ones kept and refreshed', () => {
    const state = run(
      [
        server({ type: 'user.turn', sessionId: 's1', turnId: 'u1', text: 'hi', author: you }),
        server({
          type: 'sessions.snapshot',
          sessions: [
            { sessionId: 'x', title: 'Bin', origin: 'mcp', owner: desktop, status: 'running' },
            { sessionId: 's1', title: 'Mine', origin: 'chat', owner: you, status: 'idle' },
          ],
        }),
      ],
      started,
    )
    expect(state.order).toEqual(['x', 's1'])
    expect(state.activeId).toBe('s1')
    // The open session's status comes from its live events, which are newer than
    // a list read before them; the rest of its summary is refreshed.
    expect(state.sessions.s1).toMatchObject({ title: 'Mine', status: 'running' })
    // The transcript already on screen is kept.
    expect(state.sessions.s1?.items).toHaveLength(1)
    const later = run(
      [
        server({
          type: 'sessions.snapshot',
          sessions: [
            { sessionId: 'x', title: 'Bin', origin: 'mcp', owner: desktop, status: 'idle' },
            { sessionId: 's1', title: 'Mine', origin: 'chat', owner: you, status: 'idle' },
          ],
        }),
      ],
      state,
    )
    expect(later.sessions.x?.status).toBe('idle')
  })

  it('drops the sessions a later snapshot leaves out, but never the open one', () => {
    const withOther = run(
      [server({ type: 'session.started', sessionId: 'x', origin: 'mcp', owner: desktop })],
      started,
    )
    const state = run(
      [
        server({
          type: 'sessions.snapshot',
          sessions: [{ sessionId: 'y', title: 'New', origin: 'mcp', owner: desktop, status: 'idle' }],
        }),
      ],
      withOther,
    )
    expect(state.order).toEqual(['y', 's1'])
    expect(Object.keys(state.sessions).sort()).toEqual(['s1', 'y'])
    expect(state.activeId).toBe('s1')
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

  it('says plainly when the agent turned a message away for its connection limits', () => {
    const state = run(
      [
        server({ type: 'error', sessionId: 's1', code: 'busy', message: 'too many messages waiting' }),
        { type: 'started-new' },
        server({ type: 'error', code: 'rate_limited', message: 'too many new chats' }),
      ],
      started,
    )
    const [busy] = state.sessions.s1?.items ?? []
    expect(busy).toMatchObject({ kind: 'error', message: expect.stringMatching(/still working.*send it again/) })
    expect(state.notice).toMatch(/Too many new chats.*Wait a minute/)
    expect(state.awaitingStart).toBe(false)
  })

  describe('the session budget (#790)', () => {
    const budgeted = run([
      { type: 'started-new' },
      server({ type: 'session.started', sessionId: 's1', origin: 'chat', owner: you, budgetUsd: 1 }),
    ])

    it('starts at nothing spent, and follows each turn’s result', () => {
      expect(budgeted.sessions.s1?.budget).toEqual({ costUsd: 0, budgetUsd: 1 })
      const state = run([server({ type: 'session.result', sessionId: 's1', costUsd: 0.74, turns: 2, budgetUsd: 1 })], budgeted)
      expect(state.sessions.s1?.budget).toEqual({ costUsd: 0.74, budgetUsd: 1 })
      expect(budgetUsed(state.sessions.s1)).toBeCloseTo(0.74)
      expect(state.sessions.s1?.budgetSpent).toBe(false)
    })

    it('has no budget for a session whose log never said one', () => {
      expect(started.sessions.s1?.budget).toBeUndefined()
      expect(budgetUsed(started.sessions.s1)).toBeUndefined()
    })

    it.each(['error_max_budget_usd', 'budget_exhausted'])('marks it spent on %s, instead of adding the error', (code) => {
      const state = run(
        [
          server({ type: 'session.result', sessionId: 's1', costUsd: 1.016, turns: 3, budgetUsd: 1 }),
          server({ type: 'error', sessionId: 's1', code, message: 'raw text' }),
        ],
        budgeted,
      )
      expect(state.sessions.s1?.budgetSpent).toBe(true)
      expect(state.sessions.s1?.items.filter((i) => i.kind === 'error')).toEqual([])
    })

    it('is no longer spent once the budget is raised', () => {
      const state = run(
        [
          server({ type: 'session.budget', sessionId: 's1', costUsd: 1.016, budgetUsd: 1 }),
          server({ type: 'error', sessionId: 's1', code: 'budget_exhausted', message: 'spent' }),
          server({ type: 'session.budget', sessionId: 's1', costUsd: 1.016, budgetUsd: 2 }),
        ],
        budgeted,
      )
      expect(state.sessions.s1?.budget).toEqual({ costUsd: 1.016, budgetUsd: 2 })
      expect(state.sessions.s1?.budgetSpent).toBe(false)
    })

    it('recomputes "spent" from the replay on attach, keeping the numbers', () => {
      const spent = run([server({ type: 'error', sessionId: 's1', code: 'budget_exhausted', message: 'spent' })], budgeted)
      const reattached = run([{ type: 'select', sessionId: 's1' }], spent)
      expect(reattached.sessions.s1?.budgetSpent).toBe(false)
      expect(reattached.sessions.s1?.budget).toEqual({ costUsd: 0, budgetUsd: 1 })
    })
  })
})
