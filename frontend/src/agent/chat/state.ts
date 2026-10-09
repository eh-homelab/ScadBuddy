import type {
  Attention,
  ImagePreview,
  Origin,
  Owner,
  Question,
  Risk,
  ServerEvent,
  SessionStatus,
  SessionSummary,
  Source,
  ToolImage,
  VersionLink,
} from './protocol'

/** One entry in a session's transcript and action feed, in arrival order. */
export type FeedItem =
  /** `images`: previews of the images sent with the turn (#1866). */
  | { kind: 'user'; id: string; text: string; author: Owner; images?: ImagePreview[] }
  | { kind: 'assistant'; id: string; text: string; done: boolean }
  | {
      kind: 'tool'
      id: string
      name: string
      input: Record<string, unknown>
      risk: Risk
      /** `images`: what the result carried (#782), served by the agent (`blobUrl`). */
      result?: { ok: boolean; summary: string; sources: Source[]; version?: VersionLink; images?: ToolImage[] }
      /** A subagent's call (#1108): the session's `Agent` call that spawned it. */
      parent?: string
      /** #782 — what the call does, as the tool declares it; `toolTitle` names the rest. */
      title?: string
    }
  | {
      kind: 'approval'
      id: string
      tool: string
      summary: string
      /**
       * `pending` until the user decides; `sent` while the decision is on its way
       * (`POST /api/v1/ai/pending-input/{id}`, #815), back to `pending` if it was
       * refused; `closed` (with `reason`) when it was refused because the entry was
       * already resolved or expired, so no resolve frame need arrive for the card to
       * end; `approved`/`denied`/`expired`/`cancelled` (with `reason`) from
       * `approval.resolved`, the server's confirmation, which also replaces `closed`
       * when it does arrive. Nobody denied an expired or cancelled one (#979).
       */
      state: 'pending' | 'sent' | 'approved' | 'denied' | 'expired' | 'cancelled' | 'closed'
      by?: Owner
      reason?: string
    }
  /**
   * #940 — the agent asks the user (AskUserQuestion). `pending` until the user answers;
   * `sent` while the answer is on its way, back to `pending` if it was refused,
   * `closed` (`reason`) if it was refused as already resolved or expired;
   * `answered` or `cancelled` (its turn ended first, `reason`) from
   * `question.resolved`, the server's confirmation.
   */
  | {
      kind: 'question'
      id: string
      /** The AskUserQuestion or `ask_user` tool_use id: a `tool.call` in the feed (a subagent's too, #1108). */
      tool: string
      questions: Question[]
      /** #815 — set when this is an attention request rather than a question. */
      attention?: Attention
      state: 'pending' | 'sent' | 'answered' | 'cancelled' | 'closed'
      answers?: string[]
      by?: Owner
      reason?: string
      /** #815 — the tab came back, which ended a tab_disconnected attention request. */
      reconnected?: true
    }
  /**
   * `about`: the approval or question whose response the respond route did not take
   * (#815). Its resolve frame, or a later response the route took, drops the error:
   * a POST that landed but whose answer was lost resolves the card anyway (#1359).
   */
  | { kind: 'error'; id: string; message: string; about?: string }
  /** An automatic memory recall or retain (#818): a quiet line, its query and memories collapsed under it. */
  | {
      kind: 'memory'
      id: string
      action: 'recall' | 'retain'
      bank: string
      outcome: 'ok' | 'timeout' | 'error'
      count?: number
      detail?: string
      input?: string
      memories?: string[]
    }

export interface SessionState {
  id: string
  title: string
  origin: Origin
  owner: Owner
  status: SessionStatus
  items: FeedItem[]
  result?: { costUsd?: number; turns: number }
  /**
   * #790 — what the session has spent and may spend in all, for the header's meter.
   * Absent until an event carries it (sessions started before #790 have none in their log).
   */
  budget?: { costUsd: number; budgetUsd: number }
  /**
   * The budget ran out: a turn stopped at it, or a send was refused because of it. The
   * panel shows one message and its actions instead of the agent's two errors.
   */
  budgetSpent?: boolean
  /**
   * #815 — approvals and questions whose response was still on its way (`sent`) when the
   * feed was cleared for a replay. The replayed card shows `sent`, not live buttons, so
   * the respond route's answer (`responded`/`respond-failed`) still finds it (#1395).
   */
  sending?: string[]
  /** #795 — the session it was forked from, for the switcher's nesting and the "Forked from" link. */
  parentId?: string | null
  /** #795 — when it last changed (the agent's `updated_at`), for the switcher. */
  updatedAt?: string
}

/**
 * #792 — what a session route answered about a session (`AiSessionView` of a fork or
 * an edit, agent `routes/sessions.ts`), laid over what the panel knows. A session the
 * panel has not heard of yet (a fork it is about to open) is added, first in the list.
 */
export interface SessionPatch {
  id: string
  title?: string
  status?: SessionStatus
  parentId?: string | null
  updatedAt?: string
  costUsd?: number
  budgetUsd?: number
  /** For a session new to the panel: where it came from and who holds it. */
  origin?: Origin
  owner?: Owner
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
  /** `keepStart`: the first turn awaiting its session is queued for the reconnect, not lost. */
  | { type: 'disconnected'; reason?: string; keepStart?: boolean }
  | { type: 'protocol-error'; message: string }
  | { type: 'started-new' }
  | { type: 'select'; sessionId: string | null }
  | { type: 'decided'; sessionId: string; approvalId: string }
  /** #940 — the user's answer to a question left the panel. */
  | { type: 'answered'; sessionId: string; questionId: string }
  /**
   * #815 — the respond route took a decision or an answer: the card shows it at once,
   * without waiting for the socket's resolve frame (which may be reconnecting).
   */
  | { type: 'responded'; sessionId: string; id: string; outcome: 'approved' | 'denied' | 'answered'; answers?: string[]; by: Owner }
  /**
   * #815 — the respond route refused a decision or an answer. The card is live again,
   * with `message` beside it, unless `closed` is set: the entry was already resolved
   * or expired, so the card ends there with that reason (a resolve frame, if one
   * still arrives, replaces it with the real outcome).
   */
  | { type: 'respond-failed'; sessionId: string; id: string; message: string; closed?: string }
  /** #792 — a session route's answer (fork, rename, done), shown before the socket says it. */
  | { type: 'session-patched'; patch: SessionPatch }
  /** The transport refused a message (its queue is full): nothing was sent. */
  | { type: 'not-sent'; message: string }
  /** The transport holds a message until the connection is back; it will be sent. */
  | { type: 'queued'; message: string }

export const initialChatState: ChatState = {
  sessions: {},
  order: [],
  activeId: null,
  awaitingStart: false,
  connected: false,
  notice: null,
}

function blankSession(summary: Pick<SessionState, 'id' | 'title' | 'origin' | 'owner' | 'status'>): SessionState {
  return { ...summary, items: [] }
}

/**
 * #795 — a snapshot's parent, last activity and spend, laid over `s`. The spend is
 * taken only when `live` is false: the open session's events are newer than the list.
 */
function withListed(s: SessionState, summary: SessionSummary, live: boolean): SessionState {
  let next = s
  if (summary.parentId !== undefined) next = { ...next, parentId: summary.parentId }
  if (summary.updatedAt !== undefined) next = { ...next, updatedAt: summary.updatedAt }
  if (summary.costUsd !== undefined && summary.budgetUsd !== undefined && (!live || !next.budget)) {
    next = withBudget(next, summary.costUsd, summary.budgetUsd)
  }
  return next
}

function upsertSummary(state: ChatState, s: SessionSummary): ChatState {
  const existing = state.sessions[s.sessionId]
  const next: SessionState = existing
    ? { ...existing, title: s.title, origin: s.origin, owner: s.owner, status: s.status }
    : blankSession({ id: s.sessionId, title: s.title, origin: s.origin, owner: s.owner, status: s.status })
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

/** An error id no item has: the feed can shrink (`dropErrorsAbout`), so its length alone may repeat one. */
function errorId(session: SessionState): string {
  let n = session.items.length
  while (session.items.some((i) => i.id === `error-${n}`)) n += 1
  return `error-${n}`
}

/** Without the respond errors about `id`, which a resolution has made stale (#1359). */
function dropErrorsAbout(session: SessionState, id: string): SessionState {
  return session.items.some((i) => i.kind === 'error' && i.about === id)
    ? { ...session, items: session.items.filter((i) => i.kind !== 'error' || i.about !== id) }
    : session
}

function applyServer(state: ChatState, event: ServerEvent): ChatState {
  switch (event.type) {
    case 'sessions.snapshot': {
      // Sent on connect and again whenever the list changes (the agent re-reads it
      // while the socket is open), so a session started elsewhere shows up live and
      // one deleted elsewhere goes. The list replaces the panel's: what the snapshot
      // leaves out is dropped, except the open session, and sessions already known
      // keep their transcripts. The open session's live events are newer than a
      // list read before them, so its status is not taken from the list.
      const listed = new Set(event.sessions.map((s) => s.sessionId))
      const sessions: Record<string, SessionState> = {}
      for (const s of event.sessions) {
        const existing = state.sessions[s.sessionId]
        const live = s.sessionId === state.activeId
        sessions[s.sessionId] = withListed(
          existing
            ? { ...existing, title: s.title, origin: s.origin, owner: s.owner, status: live ? existing.status : s.status }
            : blankSession({ id: s.sessionId, title: s.title, origin: s.origin, owner: s.owner, status: s.status }),
          s,
          live && !!existing,
        )
      }
      const active = state.activeId ? state.sessions[state.activeId] : undefined
      if (active && !listed.has(active.id)) sessions[active.id] = active
      // Sessions new to the panel go first, newest first as the server lists them;
      // the rest keep their place.
      const fresh = event.sessions.map((s) => s.sessionId).filter((id) => !(id in state.sessions))
      const kept = state.order.filter((id) => id in sessions)
      return { ...state, sessions, order: [...fresh, ...kept] }
    }

    case 'session.started': {
      const known = event.sessionId in state.sessions
      const summarised = upsertSummary(state, {
        sessionId: event.sessionId,
        title: event.title ?? 'New chat',
        origin: event.origin,
        owner: event.owner,
        status: 'running',
      })
      const next =
        event.budgetUsd === undefined
          ? summarised
          : patchSession(summarised, event.sessionId, (s) => withBudget(s, 0, event.budgetUsd!))
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
          : push(s, {
              kind: 'user',
              id: event.turnId,
              text: event.text,
              author: event.author,
              ...(event.images?.length ? { images: event.images } : {}),
            }),
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
        push(s, {
          kind: 'tool',
          id: event.id,
          name: event.name,
          input: event.input,
          risk: event.risk,
          ...(event.parent === undefined ? {} : { parent: event.parent }),
          ...(event.title === undefined ? {} : { title: event.title }),
        }),
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
                  ...(event.images?.length ? { images: event.images } : {}),
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
          state: s.sending?.includes(event.id) ? 'sent' : 'pending',
        }),
      )

    case 'approval.resolved':
      return patchSession(state, event.sessionId, (s) =>
        mapItems(dropErrorsAbout(s, event.id), (i) =>
          i.kind === 'approval' && i.id === event.id
            ? {
                ...withoutReason(i),
                state: event.decision ?? (event.approved ? 'approved' : 'denied'),
                by: event.by,
                ...(event.reason ? { reason: event.reason } : {}),
              }
            : i,
        ),
      )

    case 'question.asked':
      return patchSession(state, event.sessionId, (s) =>
        push(s, {
          kind: 'question',
          id: event.id,
          tool: event.tool,
          questions: event.questions,
          ...(event.attention ? { attention: event.attention } : {}),
          state: s.sending?.includes(event.id) ? 'sent' : 'pending',
        }),
      )

    case 'question.resolved':
      return patchSession(state, event.sessionId, (s) =>
        mapItems(dropErrorsAbout(s, event.id), (i) =>
          i.kind === 'question' && i.id === event.id
            ? event.answered
              ? { ...withoutReason(i), state: 'answered', ...(event.answers ? { answers: event.answers } : {}), ...(event.by ? { by: event.by } : {}) }
              : // A closed card's reason is not this one's (#1401).
                {
                  ...withoutReason(i),
                  state: 'cancelled',
                  ...(event.reason === undefined ? {} : { reason: event.reason }),
                  ...(event.reconnected ? { reconnected: true as const } : {}),
                }
            : i,
        ),
      )

    case 'session.status':
      return patchSession(state, event.sessionId, (s) => {
        const settled = !isLive(event.status)
        // An interrupted turn never sends `assistant.text.done`; a settled session
        // has nothing left streaming either way.
        const withStreamsClosed = settled
          ? mapItems(s, (i) => (i.kind === 'assistant' && !i.done ? { ...i, done: true } : i))
          : s
        return { ...withStreamsClosed, status: event.status }
      })

    case 'session.result':
      return patchSession(state, event.sessionId, (s) => {
        const next = { ...s, result: { costUsd: event.costUsd, turns: event.turns } }
        return event.budgetUsd === undefined || event.costUsd === undefined
          ? next
          : withBudget(next, event.costUsd, event.budgetUsd)
      })

    case 'session.budget':
      return patchSession(state, event.sessionId, (s) => withBudget(s, event.costUsd, event.budgetUsd))

    case 'memory':
      // Appended where it arrives: a retain that finished after its turn lands after
      // the turn's replies, in the same place on a replay (the event log's order).
      return patchSession(state, event.sessionId, (s) =>
        push(s, {
          kind: 'memory',
          id: `memory-${s.items.length}`,
          action: event.action,
          bank: event.bank,
          outcome: event.outcome,
          ...(event.count === undefined ? {} : { count: event.count }),
          ...(event.detail === undefined ? {} : { detail: event.detail }),
          ...(event.input === undefined ? {} : { input: event.input }),
          ...(event.memories === undefined ? {} : { memories: event.memories }),
        }),
      )

    case 'error': {
      if (event.sessionId && BUDGET_CODES.has(event.code ?? '') && state.sessions[event.sessionId]) {
        // Shown once, in the panel's words (`budgetSpent`), not as the agent's text.
        return patchSession(state, event.sessionId, (s) => ({ ...s, budgetSpent: true }))
      }
      const message = errorMessage(event.code, event.message)
      // #940: an answer the agent refused resolves nothing, and a question has no
      // expiry, so its card must be answerable again. The agent names the question
      // (`questionId`) on every error that refused an answer, even a malformed one; no
      // other error touches a sent answer, which may already have been accepted.
      const reopen = (s: SessionState, id: string): SessionState =>
        mapItems(s, (i) => (i.kind === 'question' && i.id === id && i.state === 'sent' ? { ...i, state: 'pending' } : i))
      if (event.sessionId && state.sessions[event.sessionId]) {
        return patchSession(state, event.sessionId, (s) =>
          push(event.questionId ? reopen(s, event.questionId) : s, { kind: 'error', id: errorId(s), message }),
        )
      }
      return { ...state, notice: message, awaitingStart: false }
    }
  }
}

/**
 * The two ways the agent says a session's budget ran out: a turn stopped at it (the
 * SDK's result subtype), or a send was refused because of it (agent `manager.ts`).
 */
const BUDGET_CODES = new Set(['error_max_budget_usd', 'budget_exhausted'])

function patched(state: ChatState, p: SessionPatch): ChatState {
  const existing = state.sessions[p.id]
  const parent = p.parentId ? state.sessions[p.parentId] : undefined
  let s: SessionState = existing ?? {
    id: p.id,
    title: p.title ?? 'New chat',
    origin: p.origin ?? parent?.origin ?? 'chat',
    owner: p.owner ?? { kind: 'browser', id: 'browser', label: 'You' },
    status: p.status ?? 'idle',
    items: [],
  }
  if (p.title !== undefined) s = { ...s, title: p.title }
  if (p.status !== undefined) s = { ...s, status: p.status }
  if (p.parentId !== undefined) s = { ...s, parentId: p.parentId }
  if (p.updatedAt !== undefined) s = { ...s, updatedAt: p.updatedAt }
  if (p.costUsd !== undefined && p.budgetUsd !== undefined) s = withBudget(s, p.costUsd, p.budgetUsd)
  return {
    ...state,
    sessions: { ...state.sessions, [p.id]: s },
    order: existing ? state.order : [p.id, ...state.order],
  }
}

/** New budget numbers; the session is spent exactly when they say so. */
function withBudget(s: SessionState, costUsd: number, budgetUsd: number): SessionState {
  return { ...s, budget: { costUsd, budgetUsd }, budgetSpent: costUsd >= budgetUsd }
}

/**
 * The words for an agent `error` frame. The connection limits (agent
 * `src/routes/chat.ts`) get the panel's own wording, since what was refused is the
 * message the user just sent and they need to know to send it again.
 */
function errorMessage(code: string | undefined, message: string): string {
  switch (code) {
    case 'busy':
      return 'The assistant is still working through the messages already sent, so this one was not taken. Wait a moment, then send it again.'
    case 'rate_limited':
      return 'Too many new chats started in a short time, so this one was not started. Wait a minute, then send it again.'
    default:
      return message
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
        awaitingStart: action.keepStart ? state.awaitingStart : false,
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
        ? patchSession(next, action.sessionId, (s) => ({
            ...s,
            items: [],
            // Replayed from the log; the numbers stay for a log that has none.
            budgetSpent: false,
            sending: s.items.flatMap((i) => ((i.kind === 'approval' || i.kind === 'question') && i.state === 'sent' ? [i.id] : [])),
          }))
        : next
    }
    case 'session-patched':
      return patched(state, action.patch)
    case 'not-sent':
      return { ...state, awaitingStart: false, notice: action.message }
    case 'queued':
      // Still awaiting its session's start, if it starts one: the message goes out on reconnect.
      return { ...state, notice: action.message }
    case 'answered':
      return patchSession(state, action.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'question' && i.id === action.questionId && i.state === 'pending' ? { ...i, state: 'sent' } : i,
        ),
      )
    case 'decided':
      return patchSession(state, action.sessionId, (s) =>
        mapItems(s, (i) =>
          i.kind === 'approval' && i.id === action.approvalId && i.state === 'pending' ? { ...i, state: 'sent' } : i,
        ),
      )
    case 'responded':
      return patchSession(state, action.sessionId, (s) =>
        mapItems(dropErrorsAbout(s, action.id), (i) => {
          if ((i.kind !== 'approval' && i.kind !== 'question') || i.id !== action.id || i.state !== 'sent') return i
          if (i.kind === 'approval' && action.outcome !== 'answered') return { ...i, state: action.outcome, by: action.by }
          if (i.kind === 'question' && action.outcome === 'answered') {
            return { ...i, state: 'answered', by: action.by, ...(action.answers ? { answers: action.answers } : {}) }
          }
          return i
        }),
      )
    case 'respond-failed': {
      const { closed } = action
      if (closed !== undefined) {
        return patchSession(state, action.sessionId, (s) =>
          mapItems(dropErrorsAbout(s, action.id), (i) =>
            (i.kind === 'approval' || i.kind === 'question') && i.id === action.id && i.state === 'sent'
              ? { ...i, state: 'closed', reason: closed }
              : i,
          ),
        )
      }
      return patchSession(state, action.sessionId, (s) =>
        push(
          mapItems(s, (i) =>
            (i.kind === 'approval' || i.kind === 'question') && i.id === action.id && i.state === 'sent' ? { ...i, state: 'pending' } : i,
          ),
          { kind: 'error', id: errorId(s), message: action.message, about: action.id },
        ),
      )
    }
  }
}

/** The item without the reason its previous state gave; a new state brings its own. */
function withoutReason<T extends { reason?: string }>(item: T): Omit<T, 'reason'> {
  const { reason: _reason, ...rest } = item
  return rest
}

/** A turn is live: running, or parked on a human (an approval, or a question, #940). */
function isLive(status: SessionStatus): boolean {
  return status === 'running' || status === 'waiting_approval' || status === 'waiting_input'
}

/**
 * #1109 — who asked a question, from the feed: undefined for the session's own agent
 * (or a call not in the feed), else a subagent's: its `subagent_type` from the `Agent`
 * call that spawned it (#1108's `parent`), or '' when the feed does not show it (a
 * truncated input). Any subagent may ask, a package's too, so the card says so.
 */
export function askedBy(items: readonly FeedItem[], question: Extract<FeedItem, { kind: 'question' }>): string | undefined {
  const call = items.find((i) => i.kind === 'tool' && i.id === question.tool)
  if (call?.kind !== 'tool' || call.parent === undefined) return undefined
  const agent = items.find((i) => i.kind === 'tool' && i.id === call.parent)
  const type = agent?.kind === 'tool' ? agent.input.subagent_type : undefined
  return typeof type === 'string' ? type : ''
}

/** A session is busy while a turn runs or waits on a human. */
export function isBusy(session: SessionState | undefined): boolean {
  return session !== undefined && isLive(session.status)
}

/** How much of its budget the session has spent, 0 to 1 (and past 1 once over); undefined without one. */
export function budgetUsed(session: SessionState | undefined): number | undefined {
  const budget = session?.budget
  return budget ? budget.costUsd / budget.budgetUsd : undefined
}

/** The share of the budget at which the header warns the chat is close to it. */
export const BUDGET_WARNING = 0.8

/** The panel's own principal: sessions owned by anyone else show "controlled by …". */
export function isOwnedByBrowser(session: SessionState): boolean {
  return session.owner.kind === 'browser'
}
