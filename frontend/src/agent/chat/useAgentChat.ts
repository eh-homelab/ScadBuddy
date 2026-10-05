import { useCallback, useEffect, useReducer, useRef } from 'react'
import { clientMessage, parseServerEvent, type ClientMessage, type PageContext, type SessionMode } from './protocol'
import { TAB_ID } from '../tabId'
import { messageTraceparent } from '../../lib/traceAction'
import { chatReducer, initialChatState, type ChatState } from './state'
import type { ChatTransport, ChatTransportFactory } from './transport'

const NOT_SENT = 'The assistant is unreachable and too much is waiting to be sent; try again once it reconnects.'
const QUEUED = 'The assistant is unreachable; your message will be sent once it reconnects.'
const QUEUED_STOP = 'The assistant is unreachable; your stop goes first when it reconnects.'
const QUEUED_TAKE_OVER = 'The assistant is unreachable; your take-over will be sent once it reconnects.'

export interface AgentChat {
  state: ChatState
  /** Sends a user turn to the active session, or starts a new one. */
  send: (text: string, context: PageContext, mode?: SessionMode) => void
  /** Answers an approval. Nothing outward proceeds until this is called (§8.2). */
  decide: (sessionId: string, approvalId: string, approve: boolean) => void
  /** #940 — answers the agent's question: one answer per question, in order. */
  answer: (sessionId: string, questionId: string, answers: string[]) => void
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
  /** A first turn is held by the transport for the reconnect, so its `awaitingStart` survives a failed attempt. */
  const startQueued = useRef(false)
  /** The transport has a connection open now (between onOpen and onClose). */
  const live = useRef(false)
  useEffect(() => {
    latest.current = state
  }, [state])

  useEffect(() => {
    const t = factory()
    let open = true
    transport.current = t
    t.connect({
      onOpen: () => {
        if (!open) return
        // The new connection follows nothing yet, so attach the session on screen
        // (attach replays it, and `select` clears it first). `select` sends no attach
        // while disconnected, so this is the only one: a second would replay again
        // onto the feed and double every reply.
        live.current = true
        // First, so the attach below and every message after it pair with this tab (#254).
        t.send(clientMessage({ type: 'tab.bind', tabId: TAB_ID }))
        const active = latest.current.activeId
        if (active) {
          dispatch({ type: 'select', sessionId: active })
          t.send(clientMessage({ type: 'session.attach', sessionId: active }))
        }
        // What was queued goes out right after this, so from here a drop loses it again.
        startQueued.current = false
        dispatch({ type: 'connected' })
      },
      onFrame: (frame) => {
        if (!open) return
        const parsed = parseServerEvent(frame)
        if (parsed.ok) dispatch({ type: 'server', event: parsed.value })
        else dispatch({ type: 'protocol-error', message: parsed.error })
      },
      onClose: (reason) => {
        live.current = false
        if (open) dispatch({ type: 'disconnected', reason, keepStart: startQueued.current })
      },
    })
    // `connected` comes from onOpen only: the real socket is not open until its
    // handshake completes, and may close first.
    return () => {
      open = false
      live.current = false
      transport.current = null
      t.close()
    }
  }, [factory])

  const send = useCallback((text: string, context: PageContext, mode?: SessionMode) => {
    const trimmed = text.trim()
    if (!trimmed || !transport.current) return
    const activeId = latest.current.activeId
    if (!activeId) dispatch({ type: 'started-new' })
    const traceparent = messageTraceparent()
    const result = transport.current.send(
      clientMessage({
        type: 'user.message',
        ...(activeId ? { sessionId: activeId } : {}),
        text: trimmed,
        context,
        // Fixed when a session starts; the agent refuses it on an existing one.
        ...(!activeId && mode ? { mode } : {}),
        ...(traceparent ? { traceparent } : {}),
      }),
    )
    // Like a decision, a message held for the reconnect is shown as such (the
    // composer has cleared); `connected` clears the notice when it goes out.
    if (result === 'refused') dispatch({ type: 'not-sent', message: NOT_SENT })
    else if (result === 'queued') {
      // A failed reconnect attempt before it goes out must not stop the wait for its session.
      if (!activeId) startQueued.current = true
      dispatch({ type: 'queued', message: QUEUED })
    }
  }, [])

  const decide = useCallback((sessionId: string, approvalId: string, approve: boolean) => {
    if (!transport.current) return
    const result = transport.current.send(
      clientMessage({ type: 'approval.decision', sessionId, id: approvalId, approve }),
    )
    // `sent` or `queued` is shown as such until the server's approval.resolved
    // confirms it; a refused one leaves the card pending, buttons live, to try again.
    if (result === 'refused') {
      dispatch({ type: 'not-sent', message: `Your decision was not sent. ${NOT_SENT}` })
    } else {
      dispatch({ type: 'decided', sessionId, approvalId, queued: result === 'queued' })
    }
  }, [])

  const answer = useCallback((sessionId: string, questionId: string, answers: string[]) => {
    if (!transport.current) return
    const result = transport.current.send(clientMessage({ type: 'question.answer', sessionId, id: questionId, answers }))
    // A refused one leaves the card pending, to try again; a queued one goes first on reconnect.
    if (result === 'refused') dispatch({ type: 'not-sent', message: `Your answer was not sent. ${NOT_SENT}` })
    else dispatch({ type: 'answered', sessionId, questionId, queued: result === 'queued' })
  }, [])

  /** Sends a control frame, saying so when it is refused or held for the reconnect. */
  const control = useCallback((message: ClientMessage, queued: string) => {
    const result = transport.current?.send(message)
    if (result === 'refused') dispatch({ type: 'not-sent', message: NOT_SENT })
    else if (result === 'queued') dispatch({ type: 'queued', message: queued })
  }, [])

  const interrupt = useCallback(
    (sessionId: string) => control(clientMessage({ type: 'session.interrupt', sessionId }), QUEUED_STOP),
    [control],
  )

  const takeOver = useCallback(
    (sessionId: string) => control(clientMessage({ type: 'session.handoff', sessionId }), QUEUED_TAKE_OVER),
    [control],
  )

  const select = useCallback((sessionId: string | null) => {
    dispatch({ type: 'select', sessionId })
    // Offline, the reconnect's onOpen attaches whatever is on screen then.
    if (sessionId && live.current) transport.current?.send(clientMessage({ type: 'session.attach', sessionId }))
  }, [])

  return { state, send, decide, answer, interrupt, takeOver, select }
}
