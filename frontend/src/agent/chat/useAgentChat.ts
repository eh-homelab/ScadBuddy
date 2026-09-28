import { useCallback, useEffect, useReducer, useRef } from 'react'
import { clientMessage, parseServerEvent, type PageContext } from './protocol'
import { chatReducer, initialChatState, type ChatState } from './state'
import type { ChatTransport, ChatTransportFactory } from './transport'

export interface AgentChat {
  state: ChatState
  /** Sends a user turn to the active session, or starts a new one. */
  send: (text: string, context: PageContext) => void
  /** Answers an approval. Nothing outward proceeds until this is called (§8.2). */
  decide: (sessionId: string, approvalId: string, approve: boolean) => void
  interrupt: (sessionId: string) => void
  /** Take over a session another principal controls (§6 handoff). */
  takeOver: (sessionId: string) => void
  /** Open a session to watch it live; null starts a fresh chat on the next send. */
  select: (sessionId: string | null) => void
}

/** Binds a transport to the stream reducer. One transport per mount. */
export function useAgentChat(factory: ChatTransportFactory): AgentChat {
  const [state, dispatch] = useReducer(chatReducer, initialChatState)
  const transport = useRef<ChatTransport | null>(null)
  // The reducer's view, for callbacks that must not re-create on every delta.
  const latest = useRef(state)
  useEffect(() => {
    latest.current = state
  }, [state])

  useEffect(() => {
    const t = factory()
    let open = true
    let opened = false
    transport.current = t
    t.connect({
      onOpen: () => {
        if (!open) return
        // A reconnect: the new connection follows nothing yet, so re-attach the
        // session on screen (attach replays it, and `select` clears it first).
        const active = latest.current.activeId
        if (opened && active) {
          dispatch({ type: 'select', sessionId: active })
          t.send(clientMessage({ type: 'session.attach', sessionId: active }))
        }
        opened = true
        dispatch({ type: 'connected' })
      },
      onFrame: (frame) => {
        if (!open) return
        const parsed = parseServerEvent(frame)
        if (parsed.ok) dispatch({ type: 'server', event: parsed.value })
        else dispatch({ type: 'protocol-error', message: parsed.error })
      },
      onClose: (reason) => {
        if (open) dispatch({ type: 'disconnected', reason })
      },
    })
    dispatch({ type: 'connected' })
    return () => {
      open = false
      transport.current = null
      t.close()
    }
  }, [factory])

  const send = useCallback((text: string, context: PageContext) => {
    const trimmed = text.trim()
    if (!trimmed || !transport.current) return
    const activeId = latest.current.activeId
    if (!activeId) dispatch({ type: 'started-new' })
    transport.current.send(
      clientMessage({
        type: 'user.message',
        ...(activeId ? { sessionId: activeId } : {}),
        text: trimmed,
        context,
      }),
    )
  }, [])

  const decide = useCallback((sessionId: string, approvalId: string, approve: boolean) => {
    if (!transport.current) return
    transport.current.send(
      clientMessage({ type: 'approval.decision', sessionId, id: approvalId, approve }),
    )
    dispatch({ type: 'decided', sessionId, approvalId })
  }, [])

  const interrupt = useCallback((sessionId: string) => {
    transport.current?.send(clientMessage({ type: 'session.interrupt', sessionId }))
  }, [])

  const takeOver = useCallback((sessionId: string) => {
    transport.current?.send(clientMessage({ type: 'session.handoff', sessionId }))
  }, [])

  const select = useCallback((sessionId: string | null) => {
    dispatch({ type: 'select', sessionId })
    if (sessionId) transport.current?.send(clientMessage({ type: 'session.attach', sessionId }))
  }, [])

  return { state, send, decide, interrupt, takeOver, select }
}
