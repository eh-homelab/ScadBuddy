import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PROTOCOL_VERSION, type ClientMessage } from './protocol'
import type { ChatTransport, SendResult, TransportHandlers } from './transport'
import { useAgentChat } from './useAgentChat'

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
  return { factory: () => transport, sent, h: () => handlers! }
}

const frame = (body: Record<string, unknown>) => ({ v: PROTOCOL_VERSION, ...body })

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
    expect(t.sent).toEqual([{ v: 1, type: 'session.attach', sessionId: 's1' }])
    act(() => t.h().onClose?.('Lost the connection to the assistant; reconnecting…'))
    act(() => t.h().onOpen?.())
    expect(t.sent).toEqual([
      { v: 1, type: 'session.attach', sessionId: 's1' },
      { v: 1, type: 'session.attach', sessionId: 's1' },
    ])
    expect(result.current.state.connected).toBe(true)
    expect(result.current.state.activeId).toBe('s1')
  })

  it('shows a sent decision as sending, then as the server confirms it', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
    })
    act(() => result.current.decide('s1', 'a1', true))
    expect(approval(result.current.state)).toMatchObject({ state: 'sent' })
    act(() => t.h().onFrame(frame({ type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: true, by: owner })))
    expect(approval(result.current.state)).toMatchObject({ state: 'approved' })
  })

  it('shows a decision made while disconnected as queued, until the server confirms it after the reconnect', () => {
    let answer: SendResult = 'queued'
    const t = scripted(() => answer)
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
      t.h().onClose?.('Lost the connection to the assistant; reconnecting…')
    })
    act(() => result.current.decide('s1', 'a1', false))
    expect(approval(result.current.state)).toMatchObject({ state: 'queued' })
    expect(t.sent).toContainEqual({ v: 1, type: 'approval.decision', sessionId: 's1', id: 'a1', approve: false })
    answer = 'sent'
    act(() => t.h().onFrame(frame({ type: 'approval.resolved', sessionId: 's1', id: 'a1', approved: false, by: owner })))
    expect(approval(result.current.state)).toMatchObject({ state: 'denied' })
  })

  it('keeps the card pending, and says so, when the decision is refused', () => {
    let answer: SendResult = 'refused'
    const t = scripted(() => answer)
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => {
      t.h().onOpen?.()
      parked(t.h())
    })
    act(() => result.current.decide('s1', 'a1', true))
    expect(approval(result.current.state)).toMatchObject({ state: 'pending' })
    expect(result.current.state.notice).toMatch(/^Your decision was not sent\./)
    expect(t.sent).toEqual([])
    // The user can decide again.
    answer = 'sent'
    act(() => result.current.decide('s1', 'a1', true))
    expect(approval(result.current.state)).toMatchObject({ state: 'sent' })
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

  it('says a refused message was not sent, and stops waiting for its session', () => {
    const t = scripted(() => 'refused')
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => t.h().onOpen?.())
    act(() => result.current.send('hello', { route: '/' }))
    expect(result.current.state.awaitingStart).toBe(false)
    expect(result.current.state.notice).toMatch(/try again once it reconnects/)
  })
})
