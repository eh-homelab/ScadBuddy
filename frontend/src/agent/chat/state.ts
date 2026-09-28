import type {
  Origin,
  Owner,
  Risk,
  ServerEvent,
  SessionStatus,
  SessionSummary,
  Source,
  VersionLink,
} from './protocol'

/** One entry in a session's transcript and action feed, in arrival order. */
export type FeedItem =
  | { kind: 'user'; id: string; text: string; author: Owner }
  | { kind: 'assistant'; id: string; text: string; done: boolean }
  | {
      kind: 'tool'
      id: string
      name: string
      input: Record<string, unknown>
      risk: Risk
      result?: { ok: boolean; summary: string; sources: Source[]; version?: VersionLink }
    }
  | {
      kind: 'approval'
      id: string
      tool: string
      summary: string
      /**
       * `pending` until the user decides; `sent` once the decision left the panel but
       * the server has not confirmed it; `approved`/`denied` from `approval.resolved`.
       */
      state: 'pending' | 'sent' | 'approved' | 'denied'
      by?: Owner
    }
  | { kind: 'error'; id: string; message: string }

export interface SessionState {
  id: string
  title: string
  origin: Origin
  owner: Owner
  status: SessionStatus
  items: FeedItem[]
  result?: { costUsd?: number; turns: number }
}

export interface ChatState {
  sessions: Record<string, SessionState>
  /** Newest first, as the picker lists them. */
  order: string[]
  activeId: string | null
  /** The panel sent a first turn and waits for its `session.started`. */
  awaitingStart: boolean
  connected: boolean
  /** Errors not tied to a session (a dropped connection, a malformed frame). */
  notice: string | null
}

export type ChatAction =
  | { type: 'server'; event: ServerEvent }
  | { type: 'connected' }
  | { type: 'disconnected'; reason?: string }
  | { type: 'protocol-error'; message: string }
  | { type: 'started-new' }
  | { type: 'select'; sessionId: string | null }
  | { type: 'decided'; sessionId: string; approvalId: string }

export const initialChatState: ChatState = {
  sessions: {},
  order: [],
  activeId: null,
  awaitingStart: false,
  connected: false,
  notice: null,
}

function blankSession(summary: Omit<SessionSummary, 'sessionId'> & { id: string }): SessionState {
  return { ...summary, items: [] }
}

function upsertSummary(state: ChatState, s: SessionSummary): ChatState {
  const existing = state.sessions[s.sessionId]
  const next: SessionState = existing
    ? { ...existing, title: s.title, origin: s.origin, owner: s.owner, status: s.status }
    : blankSession({ id: s.sessionId, ...s })
  return {
    ...state,
    sessions: { ...state.sessions, [s.sessionId]: next },
    order: existing ? state.order : [...state.order, s.sessionId],
  }
}

function patchSession(
  state: ChatState,
  id: string,
  patch: (session: SessionState) => SessionState,
): ChatState {
  const session = state.sessions[id]
  // Events for a session the panel never heard of are dropped: the snapshot or
  // `session.started` always comes first on a well-behaved server.
  if (!session) return state
  return { ...state, sessions: { ...state.sessions, [id]: patch(session) } }
}

function mapItems(session: SessionState, fn: (item: FeedItem) => FeedItem): SessionState {
  return { ...session, items: session.items.map(fn) }
}

function push(session: SessionState, item: FeedItem): SessionState {
  return { ...session, items: [...session.items, item] }
}

function applyServer(state: ChatState, event: ServerEvent): ChatState {
  switch (event.type) {
    case 'sessions.snapshot':
      return event.sessions.reduce(upsertSummary, state)

    case 'session.started': {
      const known = event.sessionId in state.sessions
      const next = upsertSummary(state, {
        sessionId: event.sessionId,
        title: event.title ?? 'New chat',
        origin: event.origin,
        owner: event.owner,
        status: 'running',
      })
      // Newest first (a replay on attach keeps its place); the panel's own new chat
      // becomes the active one.
      const order = known
        ? next.order
        : [event.sessionId, ...next.order.filter((id) => id !== event.sessionId)]
      const adopt = state.awaitingStart && event.owner.kind === 'browser'
      return {
        ...next,
        order,
        activeId: adopt ? event.sessionId : next.activeId,
        awaitingStart: adopt ? false : next.awaitingStart,
      }
    }

    case 'session.owner':
      return patchSession(state, event.sessionId, (s) => ({ ...s, owner: event.owner }))

    case 'user.turn':
      return patchSession(state, event.sessionId, (s) =>
        s.items.some((i) => i.kind === 'user' && i.id === event.turnId)
          ? s
          : push(s, { kind: 'user', id: event.turnId, text: event.text, author: event.author }),
      )

    case 'assistant.text.delta':
      return patchSession(state, event.sessionId, (s) => {
        const found = s.items.some((i) => i.kind === 'assistant' && i.id === event.messageId)
        if (!found) {
          return push(s, { kind: 'assistant', id: event.messageId, text: event.delta, done: false })
        }
        return mapItems(s, (i) =>
          i.kind === 'assistant' && i.id === event.messageId ? { ...i, text: i.text + event.delta } : i,
        )
      })

    case 'assistant.text.done':
      return patchSession(state, event.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'assistant' && i.id === event.messageId ? { ...i, done: true } : i,
        ),
      )

    case 'tool.call':
      return patchSession(state, event.sessionId, (s) =>
        push(s, { kind: 'tool', id: event.id, name: event.name, input: event.input, risk: event.risk }),
      )

    case 'tool.result':
      return patchSession(state, event.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'tool' && i.id === event.id
            ? {
                ...i,
                result: {
                  ok: event.ok,
                  summary: event.summary,
                  sources: event.sources ?? [],
                  version: event.version,
                },
              }
            : i,
        ),
      )

    case 'approval.required':
      return patchSession(state, event.sessionId, (s) =>
        push(s, {
          kind: 'approval',
          id: event.id,
          tool: event.tool,
          summary: event.summary,
          state: 'pending',
        }),
      )

    case 'approval.resolved':
      return patchSession(state, event.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'approval' && i.id === event.id
            ? { ...i, state: event.approved ? 'approved' : 'denied', by: event.by }
            : i,
        ),
      )

    case 'session.status':
      return patchSession(state, event.sessionId, (s) => {
        const settled = event.status !== 'running' && event.status !== 'waiting_approval'
        // An interrupted turn never sends `assistant.text.done`; a settled session
        // has nothing left streaming either way.
        const withStreamsClosed = settled
          ? mapItems(s, (i) => (i.kind === 'assistant' && !i.done ? { ...i, done: true } : i))
          : s
        return { ...withStreamsClosed, status: event.status }
      })

    case 'session.result':
      return patchSession(state, event.sessionId, (s) => ({
        ...s,
        result: { costUsd: event.costUsd, turns: event.turns },
      }))

    case 'error': {
      if (event.sessionId && state.sessions[event.sessionId]) {
        return patchSession(state, event.sessionId, (s) =>
          push(s, { kind: 'error', id: `error-${s.items.length}`, message: event.message }),
        )
      }
      return { ...state, notice: event.message, awaitingStart: false }
    }
  }
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'server':
      return applyServer(state, action.event)
    case 'connected':
      return { ...state, connected: true, notice: null }
    case 'disconnected':
      return {
        ...state,
        connected: false,
        awaitingStart: false,
        notice: action.reason ?? 'Lost the connection to the assistant.',
      }
    case 'protocol-error':
      return { ...state, notice: `Ignored a malformed message from the assistant: ${action.message}` }
    case 'started-new':
      return { ...state, activeId: null, awaitingStart: true }
    case 'select': {
      const next = { ...state, activeId: action.sessionId, awaitingStart: false }
      // Attaching replays the transcript from the start (protocol: `session.attach`),
      // so the feed is rebuilt from the replay rather than appended to.
      return action.sessionId
        ? patchSession(next, action.sessionId, (s) => ({ ...s, items: [] }))
        : next
    }
    case 'decided':
      return patchSession(state, action.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'approval' && i.id === action.approvalId && i.state === 'pending'
            ? { ...i, state: 'sent' }
            : i,
        ),
      )
  }
}

/** A session is busy while a turn runs or waits on a human. */
export function isBusy(session: SessionState | undefined): boolean {
  return session?.status === 'running' || session?.status === 'waiting_approval'
}

/** The panel's own principal: sessions owned by anyone else show "controlled by …". */
export function isOwnedByBrowser(session: SessionState): boolean {
  return session.owner.kind === 'browser'
}
