/**
 * The assistant panel's wire protocol, version 1 — the contract the agent service's
 * harness (#255) implements and the realtime WebSocket (#266) carries.
 *
 * Design: `docs/superpowers/specs/2026-09-27-ai-integration-design.md` — §2 D1 (the
 * harness is the Claude Agent SDK), §5 (tools carry a `read | write | outward` risk
 * tier), §6 (sessions: origin, owner, status, one writer at a time, handoff), §8.2
 * (outward tools always pause for a human approval in the UI), §10 (plugins add tools,
 * so a tool name here is free text and an unrecognised one is `outward`, §8.1).
 *
 * Every frame is one JSON object `{ v: 1, type, … }`. Anything that fails these
 * schemas is dropped by the panel (see `parseServerEvent`), so the server can never
 * push an approval the UI would render wrongly.
 *
 * How the server derives these from the Agent SDK message stream. The SDK is run with
 * `includePartialMessages: true`, which yields `stream_event` messages wrapping raw
 * Claude API events alongside the complete assistant, user and result messages
 * (https://code.claude.com/docs/en/agent-sdk/streaming-output, read 2026-09-27):
 *
 * | SDK message                                                  | panel event            |
 * |--------------------------------------------------------------|------------------------|
 * | `system` / `init` (carries the session id)                   | `session.started`      |
 * | `stream_event` → `content_block_delta` with `text_delta`     | `assistant.text.delta` |
 * | `stream_event` → `content_block_stop` of a text block        | `assistant.text.done`  |
 * | complete `assistant` message with a `tool_use` block         | `tool.call`            |
 * | `user` message with the matching `tool_result` block         | `tool.result`          |
 * | `canUseTool` / `PreToolUse` for an `outward` tool (§8.2)      | `approval.required`    |
 * | `canUseTool` for AskUserQuestion (#940)                       | `question.asked`       |
 * | a subagent's `ask_user` MCP call (#940)                      | `question.asked`       |
 * | `result` (total cost, number of turns)                       | `session.result`       |
 *
 * The exact SDK field names (for example the result message's cost and turn fields)
 * are verified by #255 against the pinned SDK version, per the spec's §3.2 rule; the
 * panel depends only on the events below, never on SDK types.
 *
 * Beyond the events the issue lists, three small additions make the session picker
 * (§6, #300) work over the same socket: `sessions.snapshot` (the sessions the browser
 * user may see), `session.owner` (ownership moved: handoff) and `user.turn` (a user
 * turn in the transcript, so a watcher sees turns another principal sent), plus the
 * panel's `session.attach`.
 */
import { z } from 'zod'

export const PROTOCOL_VERSION = 1 as const

// ---------------------------------------------------------------------------------
// Shared shapes
// ---------------------------------------------------------------------------------

/** §8.1 tiers. A plugin tool ScadBuddy doesn't recognise is `outward`. */
export const RiskSchema = z.enum(['read', 'write', 'outward'])
export type Risk = z.infer<typeof RiskSchema>

/** §6 origins. */
export const OriginSchema = z.enum(['chat', 'mcp', 'analyzer', 'hook'])
export type Origin = z.infer<typeof OriginSchema>

/** Plan 5d — how a session runs: in the agent's harness, or as a durable Temporal workflow. */
export const SessionModeSchema = z.enum(['classic', 'durable'])
export type SessionMode = z.infer<typeof SessionModeSchema>

/** §6 statuses. */
export const SessionStatusSchema = z.enum([
  'running',
  'waiting_input',
  'waiting_approval',
  'idle',
  'done',
  'failed',
])
export type SessionStatus = z.infer<typeof SessionStatusSchema>

/** §8.1 principals. `label` is what the "controlled by …" badge reads. */
export const OwnerSchema = z.object({
  kind: z.enum(['browser', 'bearer', 'oidc', 'anonymous', 'flow']),
  id: z.string().min(1),
  label: z.string().min(1),
})
export type Owner = z.infer<typeof OwnerSchema>

/** D10: a suggestion cites what it rests on. `url` is rendered only if http(s). */
export const SourceSchema = z.object({
  title: z.string().min(1),
  url: z.string().optional(),
  /** A repository reference, e.g. `models/name-keychain/model.scad:12`. */
  ref: z.string().optional(),
})
export type Source = z.infer<typeof SourceSchema>

/** A history-backed edit links to the version it made, so the step can be undone. */
export const VersionLinkSchema = z.object({
  slug: z.string().min(1),
  revision: z.string().min(1),
})
export type VersionLink = z.infer<typeof VersionLinkSchema>

/**
 * The longest answer the agent takes (agent `src/harness/questions.ts` `ANSWER_MAX`). A
 * longer one is refused as a malformed frame and never reaches the question, so the
 * panel never sends one: the card treats a longer answer (typed words plus any picked
 * labels) as unfinished.
 */
export const ANSWER_MAX = 20_000
/** The agent's other bounds on a question (`src/harness/questions.ts`); its tests pin them to these. */
export const QUESTIONS_MAX = 4
export const OPTIONS_MIN = 2
export const OPTIONS_MAX = 4
export const QUESTION_TEXT_MAX = 2_000
export const PREVIEW_MAX = 20_000

/**
 * #940 — one question the agent asks the user (Claude Code's AskUserQuestion). The user
 * picks an option (several when `multiSelect`) or types their own answer. An option's
 * `preview` is Markdown it shows, e.g. a draft to approve.
 */
export const QuestionSchema = z.object({
  question: z.string().min(1).max(QUESTION_TEXT_MAX),
  header: z.string().max(200),
  multiSelect: z.boolean(),
  options: z
    .array(
      z.object({
        label: z.string().min(1).max(200),
        description: z.string().max(QUESTION_TEXT_MAX),
        preview: z.string().max(PREVIEW_MAX).optional(),
      }),
    )
    .min(OPTIONS_MIN)
    .max(OPTIONS_MAX)
    // The card tells options apart by label.
    .refine((options) => new Set(options.map((o) => o.label)).size === options.length, 'each option needs its own label'),
})
  // A multi-select answer joins labels with ", " (agent questions.ts): no comma in one.
  .refine((q) => !q.multiSelect || q.options.every((o) => !o.label.includes(',')), 'no comma in a multi-select label')
export type Question = z.infer<typeof QuestionSchema>

/** #815 — what makes a `question.asked` an attention request (agent `src/sessions/protocol.ts` `AttentionView`). */
export const AttentionSchema = z.union([
  z.object({
    reason: z.enum(['tab_disconnected', 'question', 'blocked', 'done']),
    onTimeout: z.enum(['proceed', 'wait', 'stop']),
    expiresAt: z.string().min(1),
  }),
  /**
   * A `done` summary (#815 §4): no timer, nothing waits on it, and it stays until the
   * user dismisses it. `summary` is ScadBuddy's own Markdown list of what the turn
   * touched (agent `src/questions/doneSummary.ts`).
   */
  z.object({ reason: z.literal('done'), summary: z.string() }),
])
export type Attention = z.infer<typeof AttentionSchema>
/** A `done` summary's attention block. */
export type DoneAttention = Extract<Attention, { summary: string }>

/**
 * #1383 — the one rule for "a `done` summary, not a wait": reason `done` and no timer. An
 * older replica's timed `done` row is an attention request its turn is parked on. The
 * agent's own copy is its `src/questions/waiting.ts`.
 */
export const isDoneSummary = (reason: string, timed: boolean): boolean => reason === 'done' && !timed

/** Whether `a` is a `done` summary, which is dismissed rather than answered. */
export const isDone = (a: Attention | undefined): a is DoneAttention =>
  a !== undefined && isDoneSummary(a.reason, 'onTimeout' in a)

/**
 * #1200 — `GET /api/v1/ai/pending-input`'s body as the badge reads it (agent
 * `src/routes/pendingInput.ts` `PendingInputPage`): only what it counts by.
 */
export const PendingInputSchema = z.object({
  entries: z.array(
    z.object({
      kind: z.enum(['approval', 'answer']),
      /** A durable entry past its timer whose worker has not resolved it yet: not waiting on anyone. */
      expiring: z.boolean().optional(),
      attention: z
        .object({ reason: z.string(), on_timeout: z.string().nullable(), summary: z.string().optional() })
        .optional(),
    }),
  ),
  /** Absent from an older agent. */
  summaries_truncated: z.boolean().optional(),
})
export type PendingInput = z.infer<typeof PendingInputSchema>

export const SessionSummarySchema = z.object({
  sessionId: z.string().min(1),
  title: z.string(),
  origin: OriginSchema,
  owner: OwnerSchema,
  status: SessionStatusSchema,
  /** #795 — for the switcher: its parent (forks nest under it), last activity, and spend; absent from an older agent. */
  parentId: z.string().nullable().optional(),
  updatedAt: z.string().optional(),
  costUsd: z.number().nonnegative().optional(),
  budgetUsd: z.number().positive().optional(),
  /** Plan 5d — how it runs; absent from an older agent (classic). */
  mode: SessionModeSchema.optional(),
})
export type SessionSummary = z.infer<typeof SessionSummarySchema>

/** Where the user is, sent with each turn (the "page context" in #256). */
export const PageContextSchema = z.object({
  route: z.string(),
  modelSlug: z.string().optional(),
  /** #254 — the browser tools live on this page (the bridge's `liveNames()`). */
  tools: z.array(z.string()).optional(),
  /** #254 — open dialogs, by accessible name; the last is on top. */
  dialogs: z.array(z.string()).optional(),
  /**
   * #254 — what each mounted page reports about itself (the bridge snapshot's `page`):
   * the customizer's changed values and render state, the settings form, and so on.
   * Never a credential: no page describes one.
   */
  page: z.record(z.string(), z.unknown()).optional(),
})
export type PageContext = z.infer<typeof PageContextSchema>

/**
 * #1866 — an image the user sends with a message: base64 for the model, and a small
 * preview the transcript shows (`user.turn`). The agent checks the bytes, the types and
 * the caps (agent `src/sessions/images.ts`); the composer stays under them (`images.ts`).
 */
export const IMAGE_MEDIA_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'] as const
export const ImagePreviewSchema = z.object({
  mediaType: z.enum(['image/png', 'image/jpeg', 'image/webp']),
  data: z.string().min(1),
})
export type ImagePreview = z.infer<typeof ImagePreviewSchema>
export const UserImageSchema = z.object({
  mediaType: z.enum(IMAGE_MEDIA_TYPES),
  data: z.string().min(1),
  preview: ImagePreviewSchema,
})
export type UserImage = z.infer<typeof UserImageSchema>

/**
 * #1941 — an image the composer uploaded when it was attached (agent `POST
 * /api/v1/ai/attachments`), sent by its id: no image travels in the socket's frames.
 */
export const AttachmentRefSchema = z.object({ kind: z.literal('attachment'), id: z.uuid() })
export type AttachmentRef = z.infer<typeof AttachmentRefSchema>

/**
 * #782 — an image a tool result carried, by name: the agent keeps the bytes (agent
 * `src/sessions/blobs.ts`) and serves them on the panel's own origin (`blobUrl`).
 */
export const ToolImageSchema = z.object({
  name: z.string().regex(/^[0-9a-f]{64}\.(png|jpg|gif|webp)$/),
  mediaType: z.enum(IMAGE_MEDIA_TYPES),
})
export type ToolImage = z.infer<typeof ToolImageSchema>

/** Where the agent serves one of a session's tool images (agent `GET /api/v1/ai/sessions/:id/blobs/:name`). */
export function blobUrl(sessionId: string, name: string): string {
  return `/api/v1/ai/sessions/${encodeURIComponent(sessionId)}/blobs/${encodeURIComponent(name)}`
}

const v = z.literal(PROTOCOL_VERSION)
const sessionId = z.string().min(1)

// ---------------------------------------------------------------------------------
// Server → panel
// ---------------------------------------------------------------------------------

export const ServerEventSchema = z.discriminatedUnion('type', [
  z.object({
    v,
    type: z.literal('sessions.snapshot'),
    sessions: z.array(SessionSummarySchema),
  }),
  z.object({
    v,
    type: z.literal('session.started'),
    sessionId,
    origin: OriginSchema,
    owner: OwnerSchema,
    title: z.string().optional(),
    /** What the session may spend in all (#790); absent on sessions started before it. */
    budgetUsd: z.number().positive().optional(),
    /** Plan 5d — how it runs; absent on sessions started before it (classic). */
    mode: SessionModeSchema.optional(),
    /** Plan 5d — why a session whose mode came from the default runs classic, not durable. */
    modeFallback: z.string().optional(),
  }),
  z.object({ v, type: z.literal('session.owner'), sessionId, owner: OwnerSchema }),
  z.object({
    v,
    type: z.literal('user.turn'),
    sessionId,
    turnId: z.string().min(1),
    text: z.string(),
    author: OwnerSchema,
    /** #1866 — previews of the images sent with the turn. */
    images: z.array(ImagePreviewSchema).optional(),
  }),
  z.object({
    v,
    type: z.literal('assistant.text.delta'),
    sessionId,
    messageId: z.string().min(1),
    delta: z.string(),
  }),
  z.object({
    v,
    type: z.literal('assistant.text.done'),
    sessionId,
    messageId: z.string().min(1),
  }),
  z.object({
    v,
    type: z.literal('tool.call'),
    sessionId,
    id: z.string().min(1),
    name: z.string().min(1),
    input: z.record(z.string(), z.unknown()),
    risk: RiskSchema,
    /** A subagent's call (#1108): the id of the session's `Agent` call that spawned it. */
    parent: z.string().min(1).optional(),
    /**
     * #782 — what the call does, in words, as the tool itself declares it (agent
     * `src/tools/registry.ts` `ToolSpec.title`). Absent for a tool outside ScadBuddy's
     * registry, which the panel names itself (`labels.ts` `toolTitle`).
     */
    title: z.string().min(1).max(200).optional(),
  }),
  z.object({
    v,
    type: z.literal('tool.result'),
    sessionId,
    id: z.string().min(1),
    ok: z.boolean(),
    summary: z.string(),
    sources: z.array(SourceSchema).optional(),
    version: VersionLinkSchema.optional(),
    /** #782 — the images the result carried, each served by the agent's `blobUrl` route. */
    images: z.array(ToolImageSchema).max(16).optional(),
  }),
  z.object({
    v,
    type: z.literal('approval.required'),
    sessionId,
    id: z.string().min(1),
    /** The `tool.call` id this approval gates. */
    tool: z.string().min(1),
    summary: z.string().min(1),
    // Only outward steps pause (§8.2); anything else here is a server bug.
    risk: z.literal('outward'),
  }),
  z.object({
    v,
    type: z.literal('approval.resolved'),
    sessionId,
    id: z.string().min(1),
    approved: z.boolean(),
    /** How it ended (#979); absent from events logged before it existed, where `approved` says it. */
    decision: z.enum(['approved', 'denied', 'expired', 'cancelled']).optional(),
    by: OwnerSchema.optional(),
    /** Why an expired or cancelled one ended. */
    reason: z.string().optional(),
  }),
  /**
   * #940 — the agent asks the user; the turn waits (`waiting_input`) for the answer.
   * `tool` is the AskUserQuestion or `ask_user` tool_use id, a `tool.call` in the
   * feed (a subagent's, tagged with `parent`, since #1108).
   */
  z.object({
    v,
    type: z.literal('question.asked'),
    sessionId,
    id: z.string().min(1),
    tool: z.string().min(1),
    questions: z.array(QuestionSchema).min(1).max(QUESTIONS_MAX),
    /**
     * #815 — an attention request (`request_user_attention`), not a question: one card,
     * and a timer that resolves it at `expiresAt` without an answer (`onTimeout` says
     * what the agent does then; never an approval).
     */
    attention: AttentionSchema.optional(),
  }),
  /**
   * Answered (`answers`, one per question in order, and `by`), or cancelled with its
   * turn (`reason`). A question never answers itself.
   */
  z.object({
    v,
    type: z.literal('question.resolved'),
    sessionId,
    id: z.string().min(1),
    answered: z.boolean(),
    answers: z.array(z.string()).optional(),
    by: OwnerSchema.optional(),
    reason: z.string().optional(),
    /** #815 — an attention request for a disconnected tab ended because the tab is back. */
    reconnected: z.literal(true).optional(),
  }),
  z.object({ v, type: z.literal('session.status'), sessionId, status: SessionStatusSchema }),
  z.object({
    v,
    type: z.literal('session.result'),
    sessionId,
    costUsd: z.number().nonnegative().optional(),
    turns: z.number().int().nonnegative(),
    budgetUsd: z.number().positive().optional(),
  }),
  /**
   * #790 — the session's budget changed (the user raised it), or a send was refused
   * because it is spent: what it has spent and may spend, for the header's meter.
   */
  z.object({
    v,
    type: z.literal('session.budget'),
    sessionId,
    costUsd: z.number().nonnegative(),
    budgetUsd: z.number().positive(),
  }),
  z.object({
    v,
    type: z.literal('error'),
    sessionId: sessionId.optional(),
    code: z.string().optional(),
    message: z.string().min(1),
    /** #940 — the error refused the panel's answer to this question: its card is answerable again. */
    questionId: z.string().min(1).optional(),
  }),
  /**
   * An automatic Hindsight recall or retain (#818): memory the agent read or wrote
   * without a tool call. A retain finishes after its turn, so this can arrive after
   * the turn's last `session.status`. `input` is what was sent (a recall's query, the
   * start of a retain's content) and `memories` what a recall injected, redacted and
   * capped by the agent; shown only in Advanced mode.
   */
  z.object({
    v,
    type: z.literal('memory'),
    sessionId,
    turnId: z.string().min(1),
    action: z.enum(['recall', 'retain']),
    bank: z.string(),
    outcome: z.enum(['ok', 'timeout', 'error']),
    count: z.number().int().nonnegative().optional(),
    detail: z.string().optional(),
    input: z.string().optional(),
    memories: z.array(z.string()).optional(),
  }),
])
export type ServerEvent = z.infer<typeof ServerEventSchema>
export type ServerEventOf<T extends ServerEvent['type']> = Extract<ServerEvent, { type: T }>

// ---------------------------------------------------------------------------------
// Panel → server
// ---------------------------------------------------------------------------------

export const ClientMessageSchema = z.discriminatedUnion('type', [
  z.object({
    v,
    type: z.literal('user.message'),
    /** Absent: start a new `chat` session owned by the browser user. */
    sessionId: sessionId.optional(),
    text: z.string().min(1),
    context: PageContextSchema,
    /** #1941 — uploaded images for the model, by id (#1866 sent them inline). */
    images: z.array(AttachmentRefSchema).min(1).optional(),
    /** Plan 5d — a new chat's mode, from the composer's picker; absent, the agent's default applies. */
    mode: SessionModeSchema.optional(),
    /**
     * Tracing spec 2026-10-01 §4: a socket carries no headers, so each turn's first
     * frame carries the W3C `traceparent` the agent's `agent.turn` continues. Absent
     * before the page's tracing has loaded; an agent that predates it ignores it.
     */
    traceparent: z
      .string()
      .regex(/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/)
      .optional(),
  }),
  z.object({
    v,
    type: z.literal('approval.decision'),
    sessionId,
    id: z.string().min(1),
    approve: z.boolean(),
  }),
  /** #940 — the user's answer to a `question.asked`: one per question, in order. */
  z.object({
    v,
    type: z.literal('question.answer'),
    sessionId,
    id: z.string().min(1),
    answers: z.array(z.string().min(1).max(ANSWER_MAX)).min(1).max(QUESTIONS_MAX),
  }),
  z.object({ v, type: z.literal('session.interrupt'), sessionId }),
  z.object({ v, type: z.literal('session.handoff'), sessionId }),
  /** Watch a session: the server replays its transcript as events, then streams live. */
  z.object({ v, type: z.literal('session.attach'), sessionId }),
  /**
   * Which tab this panel is in (`../tabId.ts`), sent first on every connection: the
   * sessions it chats with are then paired with this tab, so their browser tools drive it
   * (#254; AI spec §8.5, "The browser user's own chat sessions pair with their tab
   * automatically"; agent `src/routes/chat.ts`).
   */
  z.object({ v, type: z.literal('tab.bind'), tabId: z.string().regex(/^[A-Za-z0-9_-]{22,64}$/) }),
])
export type ClientMessage = z.infer<typeof ClientMessageSchema>

/** Builds a client message, stamping the version so callers can't forget it. */
export function clientMessage<T extends ClientMessage['type']>(
  body: Omit<Extract<ClientMessage, { type: T }>, 'v'> & { type: T },
): Extract<ClientMessage, { type: T }> {
  return { v: PROTOCOL_VERSION, ...body } as Extract<ClientMessage, { type: T }>
}

export type ParseResult<T> = { ok: true; value: T } | { ok: false; error: string }

/** Accepts a decoded object or a JSON text frame. Never throws. */
export function parseServerEvent(raw: unknown): ParseResult<ServerEvent> {
  let value = raw
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw) as unknown
    } catch {
      return { ok: false, error: 'frame is not JSON' }
    }
  }
  if (typeof value === 'object' && value !== null && 'v' in value && value.v !== PROTOCOL_VERSION) {
    return { ok: false, error: `unsupported protocol version ${String(value.v)}` }
  }
  const parsed = ServerEventSchema.safeParse(value)
  if (!parsed.success) {
    return { ok: false, error: z.prettifyError(parsed.error) }
  }
  return { ok: true, value: parsed.data }
}

export function parseClientMessage(raw: unknown): ParseResult<ClientMessage> {
  const parsed = ClientMessageSchema.safeParse(raw)
  return parsed.success
    ? { ok: true, value: parsed.data }
    : { ok: false, error: z.prettifyError(parsed.error) }
}
