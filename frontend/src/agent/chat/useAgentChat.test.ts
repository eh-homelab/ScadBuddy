import { act, renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { installTestTracing } from '../../test/tracing'
import { PROTOCOL_VERSION, type ClientMessage } from './protocol'
import type { ChatTransport, SendResult, TransportHandlers } from './transport'
import { useAgentChat } from './useAgentChat'
import { TAB_ID } from '../tabId'

const owner = { kind: 'browser' as const, id: 'browser', label: 'You' }

/** A transport the test drives: it opens, closes and answers `send` as told. */
function scripted(answer: () => SendResult = () => 'sent') {
  let handlers: TransportHandlers | null = null
  const sent: ClientMessage[] = []
  const transport: ChatTransport = {
    connect: (h) => {
      handlers = h
    },
    send: (m) => {
      const result = answer()
      if (result !== 'refused') sent.push(m)
      return result
    },
    close: () => {},
  }
  return {
    factory: () => transport,
    sent,
    /** What was sent apart from the `tab.bind` that opens every connection. */
    chat: () => sent.filter((m) => m.type !== 'tab.bind'),
    h: () => handlers!,
  }
}

const frame = (body: Record<string, unknown>) => ({ v: PROTOCOL_VERSION, ...body })

/** Answers the respond route (#815) with `status`, keeping what the panel posted. */
function capture(status = 200, detail = '') {
  const posted: { id: string; body: unknown }[] = []
  server.use(
    http.post('/api/v1/ai/pending-input/:id', async ({ params, request }) => {
      posted.push({ id: String(params.id), body: await request.json() })
      return status === 200 ? HttpResponse.json({}) : HttpResponse.json({ detail }, { status })
    }),
  )
  return posted
}

/** A session parked on approval `a1`, as the server would stream it. */
function parked(h: TransportHandlers) {
  h.onFrame(frame({ type: 'session.started', sessionId: 's1', origin: 'chat', owner, title: 't' }))
  h.onFrame(frame({ type: 'tool.call', sessionId: 's1', id: 't1', name: 'mcp__scadbuddy__send', input: {}, risk: 'outward' }))
  h.onFrame(frame({ type: 'approval.required', sessionId: 's1', id: 'a1', tool: 't1', summary: 'Send it?', risk: 'outward' }))
}

const approval = (state: ReturnType<typeof useAgentChat>['state']) =>
  state.sessions.s1?.items.find((i) => i.kind === 'approval')

describe('useAgentChat', () => {
  it('is connected only once the transport opens, and not if it closes first', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    expect(result.current.state.connected).toBe(false)
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    expect(result.current.state.connected).toBe(false)
    expect(result.current.state.notice).toMatch(/reconnecting/)
    act(() => t.h().onOpen?.())
    expect(result.current.state.connected).toBe(true)
    expect(result.current.state.notice).toBeNull()
  })

  it('re-attaches the open session when the socket comes back', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onFrame(frame({ type: 'sessions.snapshot', sessions: [{ sessionId: 's1', title: 't', origin: 'chat', owner, status: 'idle' }] }))
    })
    act(() => result.current.select('s1'))
    expect(t.chat()).toEqual([{ v: 1, type: 'session.attach', sessionId: 's1' }])
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    act(() => t.h().onOpen?.())
    expect(t.chat()).toEqual([
      { v: 1, type: 'session.attach', sessionId: 's1' },
      { v: 1, type: 'session.attach', sessionId: 's1' },
    ])
    expect(result.current.state.connected).toBe(true)
    expect(result.current.state.activeId).toBe('s1')
  })

  it('sends a decision to the respond route, and shows it decided once the route takes it (#815)', async () => {
    const posted = capture()
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
    })
    act(() => result.current.decide('s1', 'a1', true))
    expect(approval(result.current.state)).toMatchObject({ state: 'sent' })
    await waitFor(() => expect(approval(result.current.state)).toMatchObject({ state: 'approved', by: owner }))
    expect(posted).toEqual([{ id: 'approval:a1', body: { kind: 'approval', decision: 'approve' } }])
    // Nothing rides the socket.
    expect(t.chat()).toEqual([])
    // The resolve frame that follows changes nothing.
    act(() => t.h().onFrame(frame({ type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: true, by: owner })))
    expect(approval(result.current.state)).toMatchObject({ state: 'approved' })
  })

  it('decides while the socket is down: the route does not need it, and the card does not wait for it', async () => {
    const posted = capture()
    const t = scripted(() => 'queued')
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.decide('s1', 'a1', false))
    await waitFor(() => expect(approval(result.current.state)).toMatchObject({ state: 'denied' }))
    expect(posted).toEqual([{ id: 'approval:a1', body: { kind: 'approval', decision: 'deny' } }])
  })

  it('puts the card back, with the reason, when the route refuses the decision', async () => {
    capture(400, 'approval:a1 is an approval')
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
    })
    act(() => result.current.decide('s1', 'a1', true))
    await waitFor(() => expect(approval(result.current.state)).toMatchObject({ state: 'pending' }))
    expect(result.current.state.sessions.s1?.items.at(-1)).toMatchObject({
      kind: 'error',
      message: 'Your decision was not taken: approval:a1 is an approval',
    })
  })

  it('keeps the buttons gone when the entry was already decided or expired (409/410)', async () => {
    capture(409, 'approval a1 was already denied')
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
    })
    act(() => result.current.decide('s1', 'a1', true))
    await waitFor(() =>
      expect(result.current.state.sessions.s1?.items.at(-1)).toMatchObject({
        kind: 'error',
        message: 'Your decision was not taken: approval a1 was already denied',
      }),
    )
    expect(approval(result.current.state)).toMatchObject({ state: 'sent' })
    act(() => t.h().onFrame(frame({ type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: false, by: owner })))
    expect(approval(result.current.state)).toMatchObject({ state: 'denied' })
  })

  it('sends a question\'s answers keyed by question, and an attention request\'s as its choice or text (#815)', async () => {
    const posted = capture()
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    const options = [{ label: 'Red', description: '' }, { label: 'Blue', description: '' }]
    act(() => {
      t.h().onOpen?.()
      t.h().onFrame(frame({ type: 'session.started', sessionId: 's1', origin: 'chat', owner, title: 't' }))
      t.h().onFrame(
        frame({
          type: 'question.asked',
          sessionId: 's1',
          id: 'q1',
          tool: 't2',
          questions: [
            { question: 'Colour?', header: '', multiSelect: false, options },
            { question: 'Parts?', header: '', multiSelect: true, options: [{ label: 'Lid', description: '' }, { label: 'Base', description: '' }] },
          ],
        }),
      )
      t.h().onFrame(
        frame({
          type: 'question.asked',
          sessionId: 's1',
          id: 'q2',
          tool: 't3',
          questions: [{ question: 'Tab closed', header: '', multiSelect: false, options: [{ label: "I'm here", description: '' }, { label: 'Carry on without me', description: '' }] }],
          attention: { reason: 'tab_disconnected', onTimeout: 'proceed', expiresAt: '2026-10-04T10:00:00.000Z' },
        }),
      )
    })
    const question = (id: string) => result.current.state.sessions.s1?.items.find((i) => i.kind === 'question' && i.id === id)
    act(() => result.current.answer('s1', 'q1', ['Blue', 'Lid, Base']))
    expect(question('q1')).toMatchObject({ state: 'sent' })
    act(() => result.current.answer('s1', 'q2', ["I'm here"]))
    await waitFor(() => expect(question('q2')).toMatchObject({ state: 'answered', answers: ["I'm here"] }))
    expect(question('q1')).toMatchObject({ state: 'answered', answers: ['Blue', 'Lid, Base'], by: owner })
    expect(posted).toEqual([
      { id: 'question:q1', body: { kind: 'answer', answers: { 'Colour?': 'Blue', 'Parts?': 'Lid, Base' } } },
      { id: 'question:q2', body: { kind: 'answer', choice: "I'm here" } },
    ])
    expect(t.chat()).toEqual([])
  })

  it('puts a question back, with the reason, when the route refuses the answer; own words go as text', async () => {
    const posted = capture(400, '"choice" must be one of ["I\'m here"]')
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onFrame(frame({ type: 'session.started', sessionId: 's1', origin: 'chat', owner, title: 't' }))
      t.h().onFrame(
        frame({
          type: 'question.asked',
          sessionId: 's1',
          id: 'q1',
          tool: 't2',
          questions: [{ question: 'Tab closed', header: '', multiSelect: false, options: [{ label: "I'm here", description: '' }, { label: 'Carry on without me', description: '' }] }],
          attention: { reason: 'tab_disconnected', onTimeout: 'proceed', expiresAt: '2026-10-04T10:00:00.000Z' },
        }),
      )
    })
    act(() => result.current.answer('s1', 'q1', ['back in five']))
    const question = () => result.current.state.sessions.s1?.items.find((i) => i.kind === 'question')
    await waitFor(() => expect(question()).toMatchObject({ state: 'pending' }))
    expect(posted).toEqual([{ id: 'question:q1', body: { kind: 'answer', text: 'back in five' } }])
    expect(result.current.state.sessions.s1?.items.at(-1)).toMatchObject({
      kind: 'error',
      message: 'Your answer was not taken: "choice" must be one of ["I\'m here"]',
    })
  })

  it('says a message sent while disconnected is queued, keeps waiting for its session, and clears that on reconnect', () => {
    let answer: SendResult = 'queued'
    const t = scripted(() => answer)
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.send('hello', { route: '/' }))
    expect(t.sent).toContainEqual({ v: 1, type: 'user.message', text: 'hello', context: { route: '/' } })
    expect(result.current.state.notice).toMatch(/will be sent once it reconnects/)
    expect(result.current.state.awaitingStart).toBe(true)
    // Back: the transport sends what it held, and the session starts as usual.
    answer = 'sent'
    act(() => t.h().onOpen?.())
    expect(result.current.state.notice).toBeNull()
    act(() => t.h().onFrame(frame({ type: 'session.started', sessionId: 's1', origin: 'chat', owner, title: 'hello' })))
    expect(result.current.state.awaitingStart).toBe(false)
    expect(result.current.state.activeId).toBe('s1')
  })

  it('attaches a session picked while disconnected once, on the reconnect', () => {
    const t = scripted(() => 'queued')
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onFrame(frame({ type: 'sessions.snapshot', sessions: [{ sessionId: 's1', title: 't', origin: 'chat', owner, status: 'idle' }] }))
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.select('s1'))
    expect(t.chat()).toEqual([])
    act(() => t.h().onOpen?.())
    expect(t.chat()).toEqual([{ v: 1, type: 'session.attach', sessionId: 's1' }])
  })

  it('names its tab first on every connection, before it re-attaches (#254)', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    const bind = { v: 1, type: 'tab.bind', tabId: TAB_ID }
    act(() => {
      t.h().onOpen?.()
      t.h().onFrame(frame({ type: 'sessions.snapshot', sessions: [{ sessionId: 's1', title: 't', origin: 'chat', owner, status: 'idle' }] }))
    })
    expect(t.sent).toEqual([bind])
    act(() => result.current.select('s1'))
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    act(() => t.h().onOpen?.())
    expect(t.sent).toEqual([bind, { v: 1, type: 'session.attach', sessionId: 's1' }, bind, { v: 1, type: 'session.attach', sessionId: 's1' }])
  })

  it('keeps waiting for a queued first turn through failed reconnect attempts', () => {
    let answer: SendResult = 'queued'
    const t = scripted(() => answer)
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.send('hello', { route: '/' }))
    // The back-off's next handshake fails too; the message is still queued.
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    expect(result.current.state.awaitingStart).toBe(true)
    answer = 'sent'
    act(() => t.h().onOpen?.())
    act(() => t.h().onFrame(frame({ type: 'session.started', sessionId: 's1', origin: 'chat', owner, title: 'hello' })))
    expect(result.current.state.activeId).toBe('s1')
    // Once it went out, a drop before its session starts stops the wait as before.
    act(() => result.current.select(null))
    act(() => result.current.send('again', { route: '/' }))
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    expect(result.current.state.awaitingStart).toBe(false)
  })

  it('says a stop or take-over made while disconnected is queued, and clears that on reconnect', () => {
    let answer: SendResult = 'queued'
    const t = scripted(() => answer)
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.interrupt('s1'))
    expect(t.sent).toContainEqual({ v: 1, type: 'session.interrupt', sessionId: 's1' })
    expect(result.current.state.notice).toMatch(/your stop goes first when it reconnects/)
    act(() => result.current.takeOver('s1'))
    expect(t.sent).toContainEqual({ v: 1, type: 'session.handoff', sessionId: 's1' })
    expect(result.current.state.notice).toMatch(/your take-over will be sent once it reconnects/)
    answer = 'sent'
    act(() => t.h().onOpen?.())
    expect(result.current.state.notice).toBeNull()
  })

  it('says a refused stop or take-over was not sent', () => {
    const t = scripted(() => 'refused')
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => t.h().onOpen?.())
    act(() => result.current.interrupt('s1'))
    expect(result.current.state.notice).toMatch(/try again once it reconnects/)
    act(() => t.h().onOpen?.())
    act(() => result.current.takeOver('s1'))
    expect(result.current.state.notice).toMatch(/try again once it reconnects/)
  })

  it('says a refused message was not sent, and stops waiting for its session', () => {
    const t = scripted(() => 'refused')
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => t.h().onOpen?.())
    act(() => result.current.send('hello', { route: '/' }))
    expect(result.current.state.awaitingStart).toBe(false)
    expect(result.current.state.notice).toMatch(/try again once it reconnects/)
  })

  it('sends each turn with the traceparent of its own assistant.message span once tracing runs', () => {
    const tracing = installTestTracing()
    try {
      const t = scripted()
      const { result } = renderHook(() => useAgentChat(t.factory))
      act(() => t.h().onOpen?.())
      act(() => result.current.send('hello', { route: '/' }))
      act(() => result.current.send('again', { route: '/' }))
      const turns = t.chat().filter((m) => m.type === 'user.message')
      const spans = tracing.exporter.getFinishedSpans()
      expect(spans.map((s) => s.name)).toEqual(['assistant.message', 'assistant.message'])
      expect(turns.map((m) => (m.type === 'user.message' ? m.traceparent : undefined))).toEqual(
        spans.map((s) => `00-${s.spanContext().traceId}-${s.spanContext().spanId}-01`),
      )
      // Each turn is its own trace (§4).
      expect(spans[0]?.spanContext().traceId).not.toBe(spans[1]?.spanContext().traceId)
    } finally {
      tracing.uninstall()
    }
  })

  it('sends a turn with no traceparent before tracing has loaded', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => t.h().onOpen?.())
    act(() => result.current.send('hello', { route: '/' }))
    expect(t.chat()).toContainEqual({ v: 1, type: 'user.message', text: 'hello', context: { route: '/' } })
  })
})
