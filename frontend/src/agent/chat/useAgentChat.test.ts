import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { PROTOCOL_VERSION, type ClientMessage } from './protocol'
import type { ChatTransport, TransportHandlers } from './transport'
import { useAgentChat } from './useAgentChat'

describe('useAgentChat over a reconnecting transport', () => {
  it('re-attaches the open session when the socket comes back, and clears the notice', () => {
    let handlers: TransportHandlers | null = null
    const sent: ClientMessage[] = []
    const transport: ChatTransport = {
      connect: (h) => {
        handlers = h
      },
      send: (m) => sent.push(m),
      close: () => {},
    }
    const factory = () => transport
    const { result } = renderHook(() => useAgentChat(factory))
    const owner = { kind: 'browser' as const, id: 'browser', label: 'You' }
    act(() => {
      handlers!.onOpen?.()
      handlers!.onFrame({ v: PROTOCOL_VERSION, type: 'sessions.snapshot', sessions: [{ sessionId: 's1', title: 't', origin: 'chat', owner, status: 'idle' }] })
    })
    act(() => result.current.select('s1'))
    expect(sent).toEqual([{ v: 1, type: 'session.attach', sessionId: 's1' }])

    act(() => handlers!.onClose?.('Lost the connection to the assistant; reconnecting…'))
    expect(result.current.state.notice).toMatch(/reconnecting/)
    act(() => handlers!.onOpen?.())
    expect(sent.at(-1)).toEqual({ v: 1, type: 'session.attach', sessionId: 's1' })
    expect(sent).toHaveLength(2)
    expect(result.current.state.connected).toBe(true)
    expect(result.current.state.notice).toBeNull()
    expect(result.current.state.activeId).toBe('s1')
  })
})
