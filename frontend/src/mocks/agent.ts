/**
 * A scripted stand-in for the agent service (#255), speaking the panel protocol in
 * `src/agent/chat/protocol.ts`. Used by vitest and by the mocked build
 * (`VITE_MOCK_API=1`: the Playwright suite and `pnpm dev` against msw); it is never
 * part of the production bundle (`loadChatTransportFactory` imports it dynamically
 * behind that flag).
 *
 * It plays one realistic session: streamed text, a `write` tool call whose result
 * cites its sources and links the version it made, then an `outward` send that pauses
 * on `approval.required` and goes nowhere until the panel sends `approval.decision`
 * (spec §8.2). A first message that mentions a draft gets a question instead (#940):
 * a draft to approve, which waits on `question.asked` until the panel sends
 * `question.answer`. It also lists a session an external MCP agent owns, so the picker's
 * "controlled by …" badge and Take over have something to act on.
 *
 * Each chat has a budget (#790, `budgetUsd`, $1.00 by default) that every turn spends
 * $0.0184 of; once it is spent the agent answers as the real one does. The session
 * routes the panel calls over HTTP for a spent chat (fork, raise its budget) are msw
 * handlers in `features/assistantSessions.ts`, which act on the open mock through
 * `mockAgentSessions()`.
 */
import type { ChatTransport, TransportHandlers } from '../agent/chat/transport'
import {
  PROTOCOL_VERSION,
  type ClientMessage,
  type Owner,
  type ServerEvent,
  type SessionStatus,
  type SessionSummary,
} from '../agent/chat/protocol'

type Body<E> = E extends ServerEvent ? Omit<E, 'v'> : never
type EventBody = Body<ServerEvent>

export const BROWSER_USER: Owner = { kind: 'browser', id: 'browser', label: 'You' }
export const DESKTOP_AGENT: Owner = { kind: 'bearer', id: 'token-desktop', label: 'Claude Desktop' }

export const EXTERNAL_SESSION_ID = 'sess-desktop'

export interface MockAgentOptions {
  /** Delay between scripted steps. 0 in unit tests. */
  stepMs?: number
  /** What a new chat may spend in all; each turn costs COST_PER_TURN. */
  budgetUsd?: number
}

/** What one scripted turn costs. */
export const COST_PER_TURN = 0.0184

/** The session routes' side of the open mock agent (fork, raise), for the msw handlers. */
export interface MockAgentSessions {
  /** The new session's id and title, or an error to answer with. */
  fork(sessionId: string): { id: string; title: string; budgetUsd: number; parentId: string } | { error: string; status: number }
  raise(sessionId: string, addUsd: number): { costUsd: number; budgetUsd: number } | { error: string; status: number }
}

let openAgent: MockAgentSessions | null = null

/** The mock agent the panel is connected to now, if any. */
export function mockAgentSessions(): MockAgentSessions | null {
  return openAgent
}

export interface MockAgentTransport extends ChatTransport {
  /** Every message the panel sent, in order — what the tests assert on. */
  readonly sent: readonly ClientMessage[]
}

interface MockSession extends SessionSummary {
  log: ServerEvent[]
  timers: ReturnType<typeof setTimeout>[]
  /** The approval the script is parked on, with what to run on each answer. */
  pending?: { id: string; toolCallId: string }
  /** The question the script is parked on (#940). */
  asking?: { id: string; toolCallId: string }
  streaming?: string
  turns: number
  costUsd: number
  budgetUsd: number
}

/** Splits text into stream-sized pieces, the way `text_delta`s arrive. */
function chunks(text: string, size = 14): string[] {
  const out: string[] = []
  for (let i = 0; i < text.length; i += size) out.push(text.slice(i, i + size))
  return out
}

export function createMockAgentTransport({ stepMs = 120, budgetUsd = 1 }: MockAgentOptions = {}): MockAgentTransport {
  let handlers: TransportHandlers | null = null
  const sent: ClientMessage[] = []
  const sessions = new Map<string, MockSession>()
  let counter = 0
  const nextId = (prefix: string) => `${prefix}-${++counter}`

  const deliver = (event: ServerEvent) => {
    // Async like a socket, and FIFO so the stream keeps its order.
    queueMicrotask(() => handlers?.onFrame(event))
  }

  const emit = (body: EventBody) => {
    const event = { v: PROTOCOL_VERSION, ...body } as ServerEvent
    if ('sessionId' in event && event.sessionId) sessions.get(event.sessionId)?.log.push(event)
    deliver(event)
  }

  const setStatus = (s: MockSession, status: SessionStatus) => {
    s.status = status
    emit({ type: 'session.status', sessionId: s.sessionId, status })
  }

  /** Runs steps one `stepMs` apart; cancelled by an interrupt. */
  const play = (s: MockSession, steps: Array<() => void>) => {
    steps.forEach((step, i) => {
      s.timers.push(setTimeout(step, stepMs * (i + 1)))
    })
  }

  const say = (s: MockSession, text: string): Array<() => void> => {
    const messageId = nextId('msg')
    return [
      ...chunks(text).map((delta) => () => {
        s.streaming = messageId
        emit({ type: 'assistant.text.delta', sessionId: s.sessionId, messageId, delta })
      }),
      () => {
        s.streaming = undefined
        emit({ type: 'assistant.text.done', sessionId: s.sessionId, messageId })
      },
    ]
  }

  const money = (usd: number) => `$${usd.toFixed(2)}`

  const finish = (s: MockSession) => {
    s.turns += 1
    s.costUsd += COST_PER_TURN
    setStatus(s, 'idle')
    emit({ type: 'session.result', sessionId: s.sessionId, costUsd: s.costUsd, turns: s.turns, budgetUsd: s.budgetUsd })
    if (s.costUsd >= s.budgetUsd) {
      emit({
        type: 'error',
        sessionId: s.sessionId,
        code: 'error_max_budget_usd',
        message: `this chat used its ${money(s.budgetUsd)} budget (${money(s.costUsd)} spent)`,
      })
    }
  }

  const firstTurn = (s: MockSession): Array<() => void> => {
    const writeId = nextId('tool')
    const sendId = nextId('tool')
    const approvalId = nextId('approval')
    return [
      ...say(
        s,
        "I'll make the **name** larger so it reads from across the room, then send it to Bambuddy.\n\n" +
          '- `text_size`: 10 → 14 mm\n- keep the plate colour',
      ),
      () =>
        emit({
          type: 'tool.call',
          sessionId: s.sessionId,
          id: writeId,
          name: 'mcp__scadbuddy__set_parameters',
          input: { slug: 'name-keychain', params: { text_size: 14 } },
          risk: 'write',
        }),
      () =>
        emit({
          type: 'tool.result',
          sessionId: s.sessionId,
          id: writeId,
          ok: true,
          summary: 'Set text_size to 14 mm and rendered: 78.3 × 41.0 × 6.8 mm.',
          sources: [
            { title: 'Parameter text_size', ref: 'models/name-keychain/model.scad' },
            {
              title: 'OpenSCAD customizer parameters',
              url: 'https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Customizer',
            },
          ],
          version: { slug: 'name-keychain', revision: 'a1b2c3d' },
        }),
      ...say(s, 'It still fits the A1 mini plate. Sending it needs your go-ahead.'),
      () =>
        emit({
          type: 'tool.call',
          sessionId: s.sessionId,
          id: sendId,
          name: 'mcp__scadbuddy__print_output',
          input: { slug: 'name-keychain', project: 'Keychains', copies: 2 },
          risk: 'outward',
        }),
      () => {
        s.pending = { id: approvalId, toolCallId: sendId }
        emit({
          type: 'approval.required',
          sessionId: s.sessionId,
          id: approvalId,
          tool: sendId,
          summary: 'Send name-keychain to Bambuddy project "Keychains", 2 copies?',
          risk: 'outward',
        })
        setStatus(s, 'waiting_approval')
      },
      // Parked. Only `approval.decision` moves the script on.
    ]
  }

  /** #940: a draft to approve, parked on `question.asked`. */
  const questionTurn = (s: MockSession): Array<() => void> => {
    const askId = nextId('tool')
    const questionId = nextId('question')
    const questions = [
      {
        question: 'File this issue on the keychain template?',
        header: 'Draft',
        multiSelect: false,
        options: [
          {
            label: 'Approve',
            description: 'File it as written',
            preview: '## Name text too thin\n\nAt **10 mm** the letters break off the plate.',
          },
          { label: 'Cancel', description: 'Do not file it' },
        ],
      },
    ]
    return [
      ...say(s, "Here's a draft. I'll file it once you approve."),
      () =>
        emit({
          type: 'tool.call',
          sessionId: s.sessionId,
          id: askId,
          name: 'AskUserQuestion',
          input: { questions },
          risk: 'read',
        }),
      () => {
        s.asking = { id: questionId, toolCallId: askId }
        emit({ type: 'question.asked', sessionId: s.sessionId, id: questionId, tool: askId, questions })
        setStatus(s, 'waiting_input')
      },
      // Parked. Only `question.answer` moves the script on.
    ]
  }

  const resolveQuestion = (s: MockSession, answers: string[]) => {
    const asking = s.asking
    if (!asking) return
    s.asking = undefined
    emit({ type: 'question.resolved', sessionId: s.sessionId, id: asking.id, answered: true, answers, by: BROWSER_USER })
    setStatus(s, 'running')
    const answer = answers[0] ?? ''
    play(s, [
      () => emit({ type: 'tool.result', sessionId: s.sessionId, id: asking.toolCallId, ok: true, summary: `Answered: ${answer}` }),
      ...say(s, answer === 'Approve' ? 'Filed.' : answer === 'Cancel' ? "OK, I won't file it." : `Updated the draft: "${answer}".`),
      () => finish(s),
    ])
  }

  const followUp = (s: MockSession, text: string): Array<() => void> => [
    ...say(s, `Noted: "${text}". Anything else on this model?`),
    () => finish(s),
  ]

  const resolveApproval = (s: MockSession, approve: boolean) => {
    const pending = s.pending
    if (!pending) return
    s.pending = undefined
    emit({ type: 'approval.resolved', sessionId: s.sessionId, id: pending.id, approved: approve, by: BROWSER_USER })
    setStatus(s, 'running')
    play(s, [
      () =>
        emit({
          type: 'tool.result',
          sessionId: s.sessionId,
          id: pending.toolCallId,
          ok: approve,
          summary: approve ? 'Queued 2 copies in the Keychains project.' : 'Denied: nothing was sent.',
        }),
      ...say(s, approve ? 'Sent. Two copies are in the queue.' : "OK, I didn't send it."),
      () => finish(s),
    ])
  }

  const seedExternal = () => {
    const s: MockSession = {
      sessionId: EXTERNAL_SESSION_ID,
      title: 'Tune the gridfinity bin',
      origin: 'mcp',
      owner: DESKTOP_AGENT,
      status: 'idle',
      log: [],
      timers: [],
      turns: 1,
      costUsd: COST_PER_TURN,
      budgetUsd,
    }
    sessions.set(s.sessionId, s)
    const id = s.sessionId
    s.log.push(
      { v: PROTOCOL_VERSION, type: 'session.started', sessionId: id, origin: 'mcp', owner: DESKTOP_AGENT, title: s.title },
      { v: PROTOCOL_VERSION, type: 'user.turn', sessionId: id, turnId: 'turn-ext-1', text: 'Make the bin 3 units tall.', author: DESKTOP_AGENT },
      { v: PROTOCOL_VERSION, type: 'tool.call', sessionId: id, id: 'tool-ext-1', name: 'mcp__scadbuddy__set_parameters', input: { slug: 'gridfinity-bin', params: { height_units: 3 } }, risk: 'write' },
      { v: PROTOCOL_VERSION, type: 'tool.result', sessionId: id, id: 'tool-ext-1', ok: true, summary: 'Set height_units to 3.', sources: [{ title: 'Gridfinity spec: 7 mm height unit', url: 'https://gridfinity.xyz/specification/' }] },
      { v: PROTOCOL_VERSION, type: 'assistant.text.delta', sessionId: id, messageId: 'msg-ext-1', delta: 'Done: the bin is now 3 units (21 mm) tall.' },
      { v: PROTOCOL_VERSION, type: 'assistant.text.done', sessionId: id, messageId: 'msg-ext-1' },
      { v: PROTOCOL_VERSION, type: 'session.status', sessionId: id, status: 'idle' },
    )
  }

  const onMessage = (msg: ClientMessage) => {
    switch (msg.type) {
      case 'user.message': {
        let s = msg.sessionId ? sessions.get(msg.sessionId) : undefined
        if (msg.sessionId && !s) {
          emit({ type: 'error', code: 'not_found', message: 'That session no longer exists.' })
          return
        }
        if (s && (s.status === 'running' || s.status === 'waiting_approval' || s.status === 'waiting_input')) {
          emit({ type: 'error', sessionId: s.sessionId, code: 'busy', message: 'A turn is already running in this session.' })
          return
        }
        if (s && s.owner.kind !== 'browser') {
          emit({ type: 'error', sessionId: s.sessionId, code: 'not_owner', message: `${s.owner.label} controls this session. Take over first.` })
          return
        }
        if (s && s.costUsd >= s.budgetUsd) {
          // Not logged, as the real agent answers the sender alone.
          const refusal: EventBody[] = [
            { type: 'session.budget', sessionId: s.sessionId, costUsd: s.costUsd, budgetUsd: s.budgetUsd },
            {
              type: 'error',
              sessionId: s.sessionId,
              code: 'budget_exhausted',
              message: `session ${s.sessionId} has spent its budget (${money(s.costUsd)} of ${money(s.budgetUsd)})`,
            },
          ]
          for (const body of refusal) deliver({ v: PROTOCOL_VERSION, ...body } as ServerEvent)
          return
        }
        const isNew = !s
        if (!s) {
          s = {
            sessionId: nextId('chat'),
            title: msg.text.slice(0, 40),
            origin: 'chat',
            owner: BROWSER_USER,
            status: 'running',
            log: [],
            timers: [],
            turns: 0,
            costUsd: 0,
            budgetUsd,
          }
          sessions.set(s.sessionId, s)
          emit({ type: 'session.started', sessionId: s.sessionId, origin: 'chat', owner: BROWSER_USER, title: s.title, budgetUsd })
        }
        emit({ type: 'user.turn', sessionId: s.sessionId, turnId: nextId('turn'), text: msg.text, author: BROWSER_USER })
        setStatus(s, 'running')
        play(s, isNew ? (/draft/i.test(msg.text) ? questionTurn(s) : firstTurn(s)) : followUp(s, msg.text))
        return
      }
      case 'question.answer': {
        const s = sessions.get(msg.sessionId)
        if (s?.asking?.id === msg.id) resolveQuestion(s, msg.answers)
        return
      }
      case 'approval.decision': {
        const s = sessions.get(msg.sessionId)
        if (s?.pending?.id === msg.id) resolveApproval(s, msg.approve)
        return
      }
      case 'session.interrupt': {
        const s = sessions.get(msg.sessionId)
        if (!s) return
        s.timers.forEach(clearTimeout)
        s.timers = []
        if (s.streaming) {
          emit({ type: 'assistant.text.done', sessionId: s.sessionId, messageId: s.streaming })
          s.streaming = undefined
        }
        if (s.pending) {
          emit({ type: 'approval.resolved', sessionId: s.sessionId, id: s.pending.id, approved: false })
          s.pending = undefined
        }
        if (s.asking) {
          emit({ type: 'question.resolved', sessionId: s.sessionId, id: s.asking.id, answered: false, reason: 'interrupted by You' })
          s.asking = undefined
        }
        setStatus(s, 'idle')
        return
      }
      case 'session.handoff': {
        const s = sessions.get(msg.sessionId)
        if (!s) return
        s.owner = BROWSER_USER
        emit({ type: 'session.owner', sessionId: s.sessionId, owner: BROWSER_USER })
        return
      }
      case 'session.attach': {
        // Attach replays the transcript from the start; the panel rebuilds from it.
        sessions.get(msg.sessionId)?.log.forEach(deliver)
        return
      }
    }
  }

  const controls: MockAgentSessions = {
    fork(sessionId) {
      const parent = sessions.get(sessionId)
      if (!parent) return { error: `no session ${sessionId}`, status: 404 }
      const child: MockSession = {
        sessionId: nextId('chat'),
        title: `${parent.title || 'session'} (fork)`,
        origin: parent.origin,
        owner: BROWSER_USER,
        status: 'idle',
        log: [],
        timers: [],
        turns: 0,
        costUsd: 0,
        budgetUsd,
      }
      sessions.set(child.sessionId, child)
      const conversation = new Set(['user.turn', 'assistant.text.delta', 'assistant.text.done', 'tool.call', 'tool.result'])
      child.log.push(
        { v: PROTOCOL_VERSION, type: 'session.started', sessionId: child.sessionId, origin: child.origin, owner: BROWSER_USER, title: child.title, budgetUsd },
        ...parent.log.filter((e) => conversation.has(e.type)).map((e) => ({ ...e, sessionId: child.sessionId }) as ServerEvent),
        { v: PROTOCOL_VERSION, type: 'session.status', sessionId: child.sessionId, status: 'idle' },
      )
      return { id: child.sessionId, title: child.title, budgetUsd, parentId: parent.sessionId }
    },
    raise(sessionId, addUsd) {
      const s = sessions.get(sessionId)
      if (!s) return { error: `no session ${sessionId}`, status: 404 }
      if (s.owner.kind !== 'browser') return { error: `${s.owner.label} controls this session`, status: 403 }
      s.budgetUsd = Math.round((s.budgetUsd + addUsd) * 100) / 100
      emit({ type: 'session.budget', sessionId, costUsd: s.costUsd, budgetUsd: s.budgetUsd })
      return { costUsd: s.costUsd, budgetUsd: s.budgetUsd }
    },
  }

  return {
    sent,
    connect(h) {
      handlers = h
      openAgent = controls
      // In-process, so open at once (the real socket reports it when its handshake is done).
      h.onOpen?.()
      seedExternal()
      deliver({
        v: PROTOCOL_VERSION,
        type: 'sessions.snapshot',
        sessions: [...sessions.values()].map(({ sessionId, title, origin, owner, status }) => ({
          sessionId,
          title,
          origin,
          owner,
          status,
        })),
      })
    },
    send(message) {
      sent.push(message)
      onMessage(message)
      return 'sent'
    },
    close() {
      handlers = null
      if (openAgent === controls) openAgent = null
      sessions.forEach((s) => s.timers.forEach(clearTimeout))
    },
  }
}
