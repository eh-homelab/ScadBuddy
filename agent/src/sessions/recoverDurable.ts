import { createHash } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { type Client, isGrpcServiceError, type WorkflowHandle, WorkflowNotFoundError } from '@temporalio/client'
import { defaultPayloadConverter, type Payload, type PayloadCodec } from '@temporalio/common'
import type { Sql } from 'postgres'
import { sessionWorkflowId } from '../gate/durable.js'
import { SERVER_NAME } from '../tools/projections.js'

// Recovering the conversation of a DurableSession that closed before plan Step 7.2 moved the
// open ones (#4772). Six such rows were left `runtime = durable` with no ai_session_entries, so
// a send is refused `closed` (durable.ts) and the model never gets their old turns. Their
// workflows are gone from Temporal's persistence (7-day retention) but kept by the namespace's
// history archival, and the Python worker that answered the plugin's
// `__claude_agent_conversation` Query is retired (plan Step 8.1). So the conversation is rebuilt
// from the closed run's history instead, as the plugin's workflow built it
// (temporalio-claude-agent-sdk `_workflow.py` `_loop`): every committed `run_claude_segment`
// result cuts the conversation to `transcript_keep` entries and appends `transcript_add`; its
// `checkpoint` is where the next segment resumes. The results the next segment would deliver
// (`pending`) are the next segment's own `injected` when one was scheduled, else what the paused
// calls' activities (`tool-<tool_use_id>`) returned. From there it is Step 7.2's cut
// (`resumeTranscript`), appended through the sealing store, and the row is set `unified`: the
// session's next send starts an AgentSession that resumes it.
//
// No prompt, entry or tool content is logged or returned here: counts and kinds only.

const DURABLE_WORKFLOW = 'DurableSession'
const SEGMENT_ACTIVITY = 'run_claude_segment'
/** A Claude Code tool's own step (the plugin's TOOL_STEP_ACTIVITY_NAME): its result is a ToolOutcome. */
const TOOL_STEP_ACTIVITY = 'run_claude_tool_step'
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/
/** Entry types the engine resumes from (the plugin's `_TRANSCRIPT_TYPES`). */
const TRANSCRIPT_TYPES = new Set(['user', 'assistant', 'attachment', 'system'])
const COST_STATE = 'cost-state'
const DEFERRED = 'hook_deferred_tool'
/** Why a paused call has no result: the plugin's `_end_task` reason, for a run that closed between segments. */
const STOP_REASON = 'the session ended'

type Entry = SessionStoreEntry & {
  uuid?: string
  parentUuid?: string | null
  isSidechain?: boolean
  sessionId?: string
  attachment?: { type?: string; toolUseID?: string }
  message?: { role?: string; content?: unknown }
  toolUseResult?: unknown
}
type Block = { type?: string; tool_use_id?: string; content?: unknown; is_error?: boolean }

/** A tool result the plugin holds for Claude (`ToolOutcome`). */
export type ToolOutcome = { content: unknown; is_error: boolean; blocks: unknown[] | null }

/** A DurableSession's committed conversation, as its workflow held it when it closed. */
export type DurableConversation = {
  claudeSessionId: string
  checkpoint: string
  entries: Entry[]
  /** The results its next segment would deliver (a stopped turn's paused calls, #4173). */
  pending: Record<string, ToolOutcome>
}

const isTranscript = (e: Entry): boolean => typeof e.uuid === 'string' && TRANSCRIPT_TYPES.has(e.type) && e.isSidechain !== true

function blocksOf(e: Entry): Block[] {
  const content = e.message?.content
  return Array.isArray(content) ? (content as Block[]) : []
}

/** The tool calls a user entry answers (the plugin's `_result_ids`). */
function resultIds(e: Entry): string[] {
  if (e.type !== 'user') return []
  return blocksOf(e)
    .filter((b) => b.type === 'tool_result')
    .map((b) => String(b.tool_use_id))
}

/** The paused call's id, when `e` is a deferral marker (the plugin's `_marker_of`). */
function markerOf(e: Entry | undefined): string | undefined {
  return e?.type === 'attachment' && e.attachment?.type === DEFERRED ? String(e.attachment.toolUseID) : undefined
}

/** A result's content for Claude: its blocks as they are, else text (the plugin's `_result_content`). */
function resultContent(outcome: ToolOutcome): unknown {
  if (outcome.blocks !== null && outcome.blocks !== undefined) return outcome.blocks
  return typeof outcome.content === 'string' ? outcome.content : JSON.stringify(outcome.content ?? null)
}

/** The plugin's own MCP server, whose tools a DurableSession's turns called (agent-durable session/runner.py `DURABLE_PREFIX`). */
const DURABLE_PREFIX = 'mcp__durable__'

/**
 * A DurableSession's tool name as an AgentSession turn offers the same tool: ScadBuddy's own
 * `mcp__durable__<tool>` is `mcp__scadbuddy__<tool>` (tools/projections.ts SERVER_NAME), and a
 * turn tool `mcp__durable__<server>__<tool>` (a plugin's, `scadbuddy_http__http_request`) is
 * `mcp__<server>__<tool>`, as a classic turn names it. Built-ins (`Skill`, `Agent`, `Bash`) keep theirs.
 */
export function turnToolName(name: string): string {
  if (!name.startsWith(DURABLE_PREFIX)) return name
  const rest = name.slice(DURABLE_PREFIX.length)
  return rest.includes('__') ? `mcp__${rest}` : `mcp__${SERVER_NAME}__${rest}`
}

/** `e` with its tool calls, and its hook entries' tool, named as an AgentSession's turn names them. */
function renamed(e: Entry): Entry {
  let out = e
  if (e.type === 'assistant' && blocksOf(e).some((b) => (b as { type?: string; name?: string }).type === 'tool_use' && (b as { name?: string }).name?.startsWith(DURABLE_PREFIX))) {
    const content = blocksOf(e).map((b) => {
      const name = (b as { name?: unknown }).name
      return b.type === 'tool_use' && typeof name === 'string' ? { ...b, name: turnToolName(name) } : b
    })
    out = { ...e, message: { ...e.message, content } }
  }
  const tool = (e.attachment as { toolName?: unknown } | undefined)?.toolName
  if (typeof tool === 'string' && tool.startsWith(DURABLE_PREFIX)) out = { ...out, attachment: { ...out.attachment, toolName: turnToolName(tool) } as Entry['attachment'] }
  return out
}

/** A uuid derived from `parts` (version-5 shaped), so a recovery run twice writes the same entry. */
function derivedUuid(...parts: string[]): string {
  const h = createHash('sha256').update(parts.join('\0')).digest()
  h[6] = (h[6]! & 0x0f) | 0x50
  h[8] = (h[8]! & 0x3f) | 0x80
  const x = h.subarray(0, 16).toString('hex')
  return `${x.slice(0, 8)}-${x.slice(8, 12)}-${x.slice(12, 16)}-${x.slice(16, 20)}-${x.slice(20)}`
}

/**
 * The plugin's `_deliver`: after a pause in a message with several calls the conversation ends
 * with the paused call's hook entries (the marker last, the checkpoint), then the denials of the
 * calls after it. Those denials move before the paused call's hook entries, with each held
 * result in place of its denial. Undefined when the checkpoint is not a marker followed by them.
 */
function deliver(entries: Entry[], checkpoint: string, results: Record<string, ToolOutcome>): Entry[] | undefined {
  const marker = entries.findIndex((e) => e.uuid === checkpoint)
  const paused = marker < 0 ? undefined : markerOf(entries[marker])
  if (paused === undefined) return undefined
  let first = marker
  while (first > 0 && entries[first - 1]?.attachment?.toolUseID === paused) first--
  const moved: Entry[] = []
  for (const entry of entries.slice(marker + 1)) {
    const ids = resultIds(entry)
    if (ids.length === 0 || ids.includes(paused)) continue
    const copy = structuredClone(entry)
    for (const block of blocksOf(copy)) {
      const outcome = results[String(block.tool_use_id)]
      if (block.type !== 'tool_result' || !outcome) continue
      block.content = resultContent(outcome)
      if (outcome.is_error) block.is_error = true
      else delete block.is_error
      copy.toolUseResult = block.content
    }
    moved.push(copy)
  }
  if (moved.length === 0) return undefined
  return [...entries.slice(0, first), ...moved, ...entries.slice(first, marker + 1)]
}

/** The plugin's `_seed`: the conversation through the checkpoint, where the next run resumes. */
function seed(entries: Entry[], checkpoint: string): Entry[] | undefined {
  const last = [...entries].reverse().find(isTranscript)
  if (last?.uuid === checkpoint) return entries
  for (let i = entries.length - 1; i >= 0; i--) {
    if (isTranscript(entries[i]!) && entries[i]!.uuid === checkpoint) return entries.slice(0, i + 1)
  }
  return undefined
}

/**
 * What an AgentSession of `sessionId` resumes from (plan Step 7.2's cut, #5064): the
 * conversation as the plugin's next segment would seed it (`_deliver`, else `_seed`; no
 * `cost-state`, which the plugin never seeds), with every entry's `sessionId` the session's own,
 * as the SDK's forkSession rewrites it, and its tool calls named as the AgentSession's turn
 * offers those tools (`turnToolName`). When the checkpoint is a paused call's deferral marker,
 * the result held for that call follows it as the call's tool result: the plugin's hook would
 * have handed it to Claude Code when the call resumed, and the TypeScript CLI, given none,
 * answers the call with "[Tool result missing due to internal error]" (measured in #5064).
 * Undefined when the checkpoint is not in the conversation.
 */
export function resumeTranscript(conversation: DurableConversation, sessionId: string): SessionStoreEntry[] | undefined {
  const { entries, checkpoint, pending } = conversation
  const cut = deliver(entries, checkpoint, pending) ?? seed(entries, checkpoint)
  if (!cut) return undefined
  const out: Entry[] = cut.filter((e) => e.type !== COST_STATE).map((e) => renamed(typeof e.sessionId === 'string' ? { ...e, sessionId } : e))
  const marker = out.find((e) => e.uuid === checkpoint)
  const paused = markerOf(marker)
  const outcome = paused === undefined ? undefined : pending[paused]
  if (marker && paused !== undefined && outcome) {
    const content = resultContent(outcome)
    const { cwd, entrypoint, gitBranch, userType, version, timestamp } = marker as Entry & Record<string, unknown>
    out.push({
      ...(cwd !== undefined ? { cwd } : {}),
      ...(entrypoint !== undefined ? { entrypoint } : {}),
      ...(gitBranch !== undefined ? { gitBranch } : {}),
      isSidechain: false,
      parentUuid: marker.uuid,
      sessionId,
      ...(timestamp !== undefined ? { timestamp } : {}),
      type: 'user',
      ...(userType !== undefined ? { userType } : {}),
      uuid: derivedUuid('recovered-result', sessionId, checkpoint, paused),
      ...(version !== undefined ? { version } : {}),
      message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: paused, content, ...(outcome.is_error ? { is_error: true } : {}) }] },
      toolUseResult: content,
    })
  }
  return out
}

type HistoryEvent = NonNullable<Awaited<ReturnType<WorkflowHandle['fetchHistory']>>['events']>[number]
/** A payload list decoded to its value (the codec, then the JSON converter). */
export type DecodePayloads = (payloads: readonly Payload[] | null | undefined) => Promise<unknown>

/** The plugin's DeferredCall, SegmentInput and SegmentOutput, as far as the rebuild reads them (`_models.py`). */
type DeferredCall = { id: string; name: string; kind?: string }
type SegmentInput = { conversation?: { entries?: number } | null; injected?: Record<string, ToolOutcome> | null }
type SegmentOutput = {
  session_id?: string | null
  checkpoint?: string | null
  is_error?: boolean
  transcript_keep?: number | null
  transcript_add?: Entry[] | null
  deferred?: DeferredCall | null
  siblings?: DeferredCall[] | null
}

/** Where the held results came from: the next segment's input, the calls' own activities, or none held. */
export type PendingSource = 'next_segment' | 'tool_activities' | 'none'

/** What a closed run's history gave. */
export type Rebuilt = {
  conversation: DurableConversation | undefined
  /** Model segments scheduled, and how many of them committed. */
  segments: number
  committed: number
  pending: PendingSource
  /**
   * The held results this rebuild derives from tool activities, checked against every later
   * segment's own `injected`: how many segments it was checked on, and how many agreed.
   */
  derivedChecked: number
  derivedMatched: number
  /**
   * False when a segment's input named another entry count than the rebuild had (the history is
   * not the whole story: a run that continued as new starts from its predecessor's conversation),
   * or when the held results are derived and the derivation disagreed with a later segment's own.
   */
  consistent: boolean
}

const outcomeAfterStop = (scheduled: boolean): ToolOutcome => ({
  content: scheduled
    ? `This tool call was interrupted (${STOP_REASON}); whether it took effect is unknown. Check before running it again.`
    : `This tool call did not run: ${STOP_REASON}.`,
  is_error: true,
  blocks: null,
})

type ToolRun = { name: string; scheduled: boolean; outcome?: ToolOutcome }

/** The outcome a paused call's activity gives, as the plugin's `_execute_tool` makes it. */
async function toolOutcome(e: HistoryEvent, schedule: { name: string }, decode: DecodePayloads): Promise<ToolOutcome | undefined> {
  const done = e.activityTaskCompletedEventAttributes
  if (done) {
    const result = await decode(done.result?.payloads)
    if (schedule.name === TOOL_STEP_ACTIVITY) {
      const o = (result ?? {}) as Partial<ToolOutcome>
      return { content: o.content ?? null, is_error: o.is_error === true, blocks: o.blocks ?? null }
    }
    return { content: result ?? null, is_error: false, blocks: null }
  }
  const failure = e.activityTaskFailedEventAttributes?.failure ?? e.activityTaskTimedOutEventAttributes?.failure
  if (failure) {
    // An ApplicationError's message, else the failure's own (`str(cause)`).
    const message = failure.cause?.applicationFailureInfo ? failure.cause.message : (failure.cause?.message ?? failure.message)
    return { content: `Tool failed: ${message ?? ''}`, is_error: true, blocks: null }
  }
  return undefined
}

/**
 * Rebuilds the conversation a closed DurableSession's workflow held from its history (see the
 * header). Undefined `conversation` when no segment committed.
 */
export async function rebuildConversation(events: readonly HistoryEvent[], decode: DecodePayloads): Promise<Rebuilt> {
  let conversation: Entry[] = []
  let checkpoint: string | undefined
  let claudeSessionId = ''
  let consistent = true
  let segments = 0
  let committed = 0
  let derivedChecked = 0
  let derivedMatched = 0
  /** The calls the last committed segment paused at, and their activities since. */
  let calls: DeferredCall[] = []
  let runs = new Map<string, ToolRun>()
  /** The next segment's `injected`, once one is scheduled after the last commit. */
  let injected: Record<string, ToolOutcome> | undefined
  const scheduled = new Map<string, { name: string; activityId: string }>()

  const derived = (): Record<string, ToolOutcome> =>
    Object.fromEntries(calls.map((c) => [c.id, runs.get(c.id)?.outcome ?? outcomeAfterStop(runs.get(c.id)?.scheduled ?? false)]))

  for (const e of events) {
    const s = e.activityTaskScheduledEventAttributes
    if (s) {
      const name = s.activityType?.name ?? ''
      const activityId = s.activityId ?? ''
      scheduled.set(String(e.eventId), { name, activityId })
      if (name === SEGMENT_ACTIVITY) {
        segments++
        const input = ((await decode(s.input?.payloads)) ?? {}) as SegmentInput
        if (input.conversation?.entries !== undefined && input.conversation.entries !== conversation.length) consistent = false
        if (calls.length > 0) {
          injected = input.injected ?? {}
          derivedChecked++
          if (JSON.stringify(sorted(derived())) === JSON.stringify(sorted(injected))) derivedMatched++
        }
      } else if (activityId.startsWith('tool-')) {
        const id = activityId.slice('tool-'.length)
        if (calls.some((c) => c.id === id)) runs.set(id, { name, scheduled: true })
      }
      continue
    }
    const closed = e.activityTaskCompletedEventAttributes ?? e.activityTaskFailedEventAttributes ?? e.activityTaskTimedOutEventAttributes ?? e.activityTaskCanceledEventAttributes
    if (!closed) continue
    const schedule = scheduled.get(String(closed.scheduledEventId))
    if (!schedule) continue
    if (schedule.name === SEGMENT_ACTIVITY) {
      const done = e.activityTaskCompletedEventAttributes
      if (!done) continue
      const out = ((await decode(done.result?.payloads)) ?? {}) as SegmentOutput
      // The plugin's commit: a segment that is no error and has a checkpoint (`_loop`).
      if (out.is_error || !out.checkpoint) continue
      committed++
      checkpoint = out.checkpoint
      claudeSessionId = out.session_id || claudeSessionId
      if (out.transcript_keep !== null && out.transcript_keep !== undefined) {
        conversation = [...conversation.slice(0, out.transcript_keep), ...(out.transcript_add ?? [])]
      }
      calls = out.deferred ? [out.deferred, ...(out.siblings ?? [])] : []
      runs = new Map()
      injected = undefined
      continue
    }
    if (schedule.activityId.startsWith('tool-')) {
      const id = schedule.activityId.slice('tool-'.length)
      const run = runs.get(id)
      if (run) run.outcome = await toolOutcome(e, schedule, decode)
    }
  }
  let pending: Record<string, ToolOutcome> = {}
  let source: PendingSource = 'none'
  if (calls.length > 0) {
    if (injected) {
      pending = injected
      source = 'next_segment'
    } else {
      pending = derived()
      source = 'tool_activities'
      // Derived results that disagreed with a real `injected` earlier in the run are not trusted here either.
      if (derivedMatched < derivedChecked) consistent = false
    }
  }
  return {
    conversation: checkpoint ? { claudeSessionId, checkpoint, entries: conversation, pending } : undefined,
    segments,
    committed,
    pending: source,
    derivedChecked,
    derivedMatched,
    consistent,
  }
}

/** `o` with its keys in order, so two outcomes compare by value. */
function sorted(o: unknown): unknown {
  if (Array.isArray(o)) return o.map(sorted)
  if (o && typeof o === 'object') return Object.fromEntries(Object.keys(o).sort().map((k) => [k, sorted((o as Record<string, unknown>)[k])]))
  return o
}

/** A codec's decode, then the JSON converter: what `rebuildConversation` reads payloads with. */
export function decodeWith(codec: PayloadCodec): DecodePayloads {
  return async (payloads) => {
    if (!payloads?.length) return undefined
    const [p] = await codec.decode([...payloads])
    return p ? defaultPayloadConverter.fromPayload(p) : undefined
  }
}

/** The closed DurableSession run of a session: retained, else in the namespace's archive. */
export type ClosedRun = { runId: string; status: string; archived: boolean }

/**
 * The session's closed DurableSession run. A retained one first (a describe, then visibility for
 * an earlier run under the same id); else Archival, which keeps a run past retention. Undefined
 * when there is none. The queries name only the session's uuid, checked first.
 */
export async function closedRun(client: Client, sessionId: string): Promise<ClosedRun | undefined> {
  if (!UUID.test(sessionId)) throw new Error('not a session id')
  const workflowId = sessionWorkflowId(sessionId)
  try {
    const d = await client.workflow.getHandle(workflowId).describe()
    if (d.type === DURABLE_WORKFLOW && d.status.name !== 'RUNNING') return { runId: d.runId, status: d.status.name, archived: false }
  } catch (err) {
    if (!(err instanceof WorkflowNotFoundError)) throw err
  }
  const query = `WorkflowId = '${workflowId}' AND WorkflowType = '${DURABLE_WORKFLOW}'`
  for await (const w of client.workflow.list({ query })) {
    if (w.status.name !== 'RUNNING') return { runId: w.runId, status: w.status.name, archived: false }
  }
  let nextPageToken: Uint8Array | undefined
  let latest: { runId: string; status: string; close: number } | undefined
  do {
    let page: Awaited<ReturnType<Client['workflowService']['listArchivedWorkflowExecutions']>>
    try {
      page = await client.workflowService.listArchivedWorkflowExecutions({
        namespace: client.options.namespace,
        pageSize: 100,
        query: `WorkflowId = '${workflowId}'`,
        ...(nextPageToken ? { nextPageToken } : {}),
      })
    } catch (err) {
      // gRPC 3 INVALID_ARGUMENT: the cluster or namespace keeps no archive (a dev server), so there is none to read.
      if (isGrpcServiceError(err) && err.code === 3) return undefined
      throw err
    }
    for (const x of page.executions ?? []) {
      if (x.type?.name !== DURABLE_WORKFLOW || !x.execution?.runId) continue
      const close = Number(x.closeTime?.seconds ?? 0)
      if (!latest || close > latest.close) {
        latest = { runId: x.execution.runId, status: workflowStatusName(x.status), close }
      }
    }
    nextPageToken = page.nextPageToken?.length ? page.nextPageToken : undefined
  } while (nextPageToken)
  return latest ? { runId: latest.runId, status: latest.status, archived: true } : undefined
}

/** temporal.api.enums.v1.WorkflowExecutionStatus, as the client names it. */
function workflowStatusName(status: number | null | undefined): string {
  return ['UNSPECIFIED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'TERMINATED', 'CONTINUED_AS_NEW', 'TIMED_OUT'][status ?? 0] ?? 'UNSPECIFIED'
}

export type RecoverDeps = {
  client: Client
  sql: Sql
  /** Opens the run's payloads (payloadCodec.ts `SubjectPayloadCodec`, with the payload store for references). */
  decode: DecodePayloads
  /** The sealing store (store.ts `PostgresSessionStore`). */
  store: { append(key: { projectKey: string; sessionId: string }, entries: SessionStoreEntry[]): Promise<void> }
}

/** What recovering one session found, and with `dryRun` false, did. */
export type RecoverOutcome =
  /** Its entries were written and its row set `unified` (or they would be, on a dry run). */
  | 'recovered'
  /** Its row is not a closed DurableSession's: nothing is done. */
  | 'not_durable'
  /** Its run's history has no committed segment: the model never answered, so there is nothing to give it. */
  | 'empty'
  /** No closed DurableSession run is retained or archived for it. */
  | 'no_history'
  /** The rebuild does not hold together (a segment saw another conversation, derived results disagreed, or the checkpoint is missing): nothing is written. */
  | 'inconsistent'

export type RecoverLine = {
  session: string
  outcome: RecoverOutcome
  row?: { status: string; archived: boolean; entries: number }
  run?: ClosedRun
  segments?: number
  committed?: number
  /** Entries in the rebuilt conversation, and those the cut writes. */
  conversation?: number
  entries?: number
  /** Where the cut resumes: an assistant message, or a paused call's marker. */
  checkpoint?: string
  pending?: PendingSource
  derived_checked?: number
  derived_matched?: number
  /** Claude Code's versions, as the entries name them. */
  versions?: string[]
  /** The append was skipped: its last entry was there already. */
  already_written?: boolean
}

/** Every session still `runtime = durable`, oldest first. */
export async function durableRows(sql: Sql): Promise<string[]> {
  const rows = await sql<{ id: string }[]>`SELECT id FROM ai_sessions WHERE runtime = 'durable' ORDER BY created_at, id`
  return rows.map((r) => r.id)
}

/**
 * Recovers one closed DurableSession's conversation (see the header). With `dryRun` it reads the
 * run's history and reports what it would write, and writes nothing.
 */
export async function recoverDurableSession(deps: RecoverDeps, sessionId: string, { dryRun = false }: { dryRun?: boolean } = {}): Promise<RecoverLine> {
  const { sql } = deps
  const [row] = await sql<{ status: string; archived: boolean; runtime: string | null; entries: number }[]>`
    SELECT status, archived_at IS NOT NULL AS archived, runtime,
      (SELECT count(*)::int FROM ai_session_entries e WHERE e.session_id = s.id::text AND e.subpath = '') AS entries
    FROM ai_sessions s WHERE id = ${sessionId}`
  if (!row || row.runtime !== 'durable') return { session: sessionId, outcome: 'not_durable' }
  const line: RecoverLine = { session: sessionId, outcome: 'no_history', row: { status: row.status, archived: row.archived, entries: row.entries } }
  const run = await closedRun(deps.client, sessionId)
  if (!run) return line
  line.run = run
  const history = await deps.client.workflow.getHandle(sessionWorkflowId(sessionId), run.runId).fetchHistory()
  const rebuilt = await rebuildConversation(history.events ?? [], deps.decode)
  Object.assign(line, {
    segments: rebuilt.segments,
    committed: rebuilt.committed,
    pending: rebuilt.pending,
    derived_checked: rebuilt.derivedChecked,
    derived_matched: rebuilt.derivedMatched,
  })
  const { conversation } = rebuilt
  if (!conversation) return { ...line, outcome: 'empty' }
  line.conversation = conversation.entries.length
  const at = conversation.entries.find((e) => e.uuid === conversation.checkpoint)
  line.checkpoint = markerOf(at) !== undefined ? 'deferral' : (at?.type ?? 'missing')
  line.versions = [...new Set(conversation.entries.map((e) => (e as { version?: unknown }).version).filter((v): v is string => typeof v === 'string'))].sort()
  const entries = rebuilt.consistent ? resumeTranscript(conversation, sessionId) : undefined
  if (!entries) return { ...line, outcome: 'inconsistent' }
  line.entries = entries.length
  line.outcome = 'recovered'
  if (dryRun) return line
  // Written once: a run that stopped after the append finds its last entry there (one INSERT, all
  // or nothing), and the store skips an entry whose uuid it holds.
  const last = [...entries].reverse().find((e) => typeof e.uuid === 'string')?.uuid
  const [written] = last ? await sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '' AND uuid = ${last} LIMIT 1` : []
  if (written) line.already_written = true
  else await deps.store.append({ projectKey: '', sessionId }, entries)
  // updated_at is left as it is: the chat keeps its place in the session list.
  await sql`UPDATE ai_sessions SET runtime = 'unified' WHERE id = ${sessionId} AND runtime = 'durable'`
  return line
}
