import { createHash } from 'node:crypto'
import type { SessionStoreEntry } from '@anthropic-ai/claude-agent-sdk'
import { type Client, isGrpcDeadlineError, WorkflowNotFoundError } from '@temporalio/client'
import type { Sql } from 'postgres'
import type { AuditSink } from '../audit/log.js'
import { DurableGate, DurableUnavailable, sessionWorkflowId } from '../gate/durable.js'
import { cancelOrphanedEntries } from '../gate/sweep.js'
import { SESSION_QUEUE, SESSION_WORKFLOW } from '../temporal/session/names.js'
import { SERVER_NAME } from '../tools/projections.js'
import type { EventLog } from './eventLog.js'
import { SessionError } from './manager.js'

// Moving a DurableSession to AgentSession (unified runtime spec 2026-10-13 §10.2, plan Step 7.2,
// #4772). A DurableSession's conversation is only in its workflow; an AgentSession resumes from
// ai_session_entries. So, under a per-session lock:
//
//   1. `cancel_input(migrated)`: its parked calls end. On the backfill a turn that still runs is
//      then stopped (`interrupt`), and its end awaited; a send never migrates a running one.
//   2. The conversation, paged through `fork_point` (one read gives `total` and `checkpoint`
//      together; the plugin's own `__claude_agent_conversation` Query refuses a page unless it is
//      given the current total), cut where the plugin's next segment would resume (`resumeTranscript`)
//      and appended to ai_session_entries through the sealing store. Idempotent: an append is
//      skipped once the checkpoint's entry is there.
//   3. `end`, and its close awaited. DurableSession completes only on `end`.
//   4. AgentSession started under the same id (the closed run is reused only here), and the
//      row's `runtime` set to `unified`.
//
// Any step may fail; a later run starts again from the workflow's state: an open DurableSession
// redoes 1-4 (2 is skipped once written), a COMPLETED one that the row does not mark done goes on
// at 4. A DurableSession that failed or was terminated lost its conversation: `lost`.
//
// No prompt, entry or tool content is logged here.

export const MIGRATED_REASON = 'migrated'
const DURABLE_WORKFLOW = 'DurableSession'
const FORK_POINT_QUERY = 'fork_point'
const TURN_QUERY = 'turn'
const PENDING_INPUT_QUERY = 'pending_input'
/** The most characters of entries one `fork_point` page carries (sessions/durable.ts). */
const PAGE_CHARS = 384 * 1024
/** Entry types the engine resumes from (the plugin's `_TRANSCRIPT_TYPES`). */
const TRANSCRIPT_TYPES = new Set(['user', 'assistant', 'attachment', 'system'])
const COST_STATE = 'cost-state'
const DEFERRED = 'hook_deferred_tool'

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

/** One page of `fork_point` (agent-durable session/models.py `ForkPoint`). */
type ForkPointPage = {
  session_id: string | null
  checkpoint: string | null
  busy: boolean
  total: number
  entries: string[]
  pending?: Record<string, ToolOutcome>
  fork_next?: boolean
}

/** A DurableSession's committed conversation, as its workflow holds it. */
export type DurableConversation = {
  claudeSessionId: string
  checkpoint: string
  busy: boolean
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

/** A uuid derived from `parts` (version-5 shaped), so a migration run twice writes the same entry. */
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
 * What an AgentSession of `sessionId` resumes from: the conversation as the plugin's next segment
 * would seed it (`_deliver`, else `_seed`; no `cost-state`, which the plugin never seeds), with
 * every entry's `sessionId` the session's own, as the SDK's forkSession rewrites it, and its tool
 * calls named as the AgentSession's turn offers those tools (`turnToolName`). When the
 * checkpoint is a paused call's deferral marker, the result the workflow holds for that call (a
 * stopped turn's) follows it as the call's tool result: the plugin's hook would have handed it to
 * Claude Code when the call resumed, and the TypeScript CLI, given none, answers the call with
 * "[Tool result missing due to internal error]" (measured, test/migrateDurable.sdk.test.ts).
 */
export function resumeTranscript(conversation: DurableConversation, sessionId: string): SessionStoreEntry[] {
  const { entries, checkpoint, pending } = conversation
  const cut = deliver(entries, checkpoint, pending) ?? seed(entries, checkpoint)
  if (!cut) throw new SessionError('invalid', `session ${sessionId}'s checkpoint is not in its conversation, so it cannot be migrated`)
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
      uuid: derivedUuid('migrated-result', sessionId, checkpoint, paused),
      ...(version !== undefined ? { version } : {}),
      message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: paused, content, ...(outcome.is_error ? { is_error: true } : {}) }] },
      toolUseResult: content,
    })
  }
  return out
}

/** Where a DurableSession stands: what the backfill's dry run reports. */
export type DurableState =
  /** A turn runs and waits on a person: an approval or an answer. */
  | 'parked'
  /** A turn runs. */
  | 'busy'
  /** Between turns, its last turn stopped at a paused call: the cut resumes at the call's marker. */
  | 'deferral'
  /** Between turns. */
  | 'idle'
  /** Between turns, with nothing committed yet. */
  | 'empty'

export type DurableSurvey = {
  sessionId: string
  state: DurableState
  /** Claude Code's versions, as its entries name them. */
  versions: string[]
  entries: number
  /** Entry types, and for each the fields it has: a shape the Step 7.1 fixture may not cover. */
  shapes: Record<string, string[]>
}

export type MigrateDeps = {
  client: Client
  sql: Sql
  /** The sealing store (store.ts `PostgresSessionStore`). */
  store: { append(key: { projectKey: string; sessionId: string }, entries: SessionStoreEntry[]): Promise<void> }
  events: EventLog
  audit?: AuditSink | undefined
  /** Tests only: a queue other than `agent-sessions`. */
  sessionQueue?: string
  /** How long each Temporal call may take (default 10 s). */
  callTimeoutMs?: number
  /** How long a stopped turn's end, and the workflow's close, may take (default 120 s). */
  settleTimeoutMs?: number
  /** How often a stopped turn is read while it ends (default 1 s). */
  pollMs?: number
}

/** What a migration found. */
export type MigrateOutcome =
  /** Its DurableSession was migrated now (or a migration that stopped is finished). */
  | 'migrated'
  /** Its workflow is an AgentSession already. */
  | 'agent_session'
  /** No workflow: no turn ever ran (or retention removed one), so there is nothing to move. */
  | 'none'
  /** Its DurableSession failed or was ended: its conversation is gone. */
  | 'lost'

/** The workflow ids of every open DurableSession, from Temporal's visibility. The query is a constant: never put input in it. */
export async function* openDurableSessions(client: Client): AsyncGenerator<string> {
  for await (const w of client.workflow.list({ query: `WorkflowType = '${DURABLE_WORKFLOW}' AND ExecutionStatus = 'Running'` })) {
    yield w.workflowId
  }
}

export class DurableMigration {
  readonly #client: Client
  readonly #sql: Sql
  readonly #deps: MigrateDeps
  readonly #gate: DurableGate
  readonly #callMs: number
  readonly #settleMs: number
  readonly #pollMs: number

  constructor(deps: MigrateDeps) {
    this.#deps = deps
    this.#client = deps.client
    this.#sql = deps.sql
    this.#callMs = deps.callTimeoutMs ?? 10_000
    this.#settleMs = deps.settleTimeoutMs ?? 120_000
    this.#pollMs = deps.pollMs ?? 1_000
    this.#gate = new DurableGate(deps.client, { cancelTimeoutMs: this.#callMs })
  }

  #call<T>(fn: () => Promise<T>, ms = this.#callMs): Promise<T> {
    return this.#client.withDeadline(Date.now() + ms, fn)
  }

  /** The workflow's type and whether it runs; undefined when there is none. */
  async #describe(sessionId: string): Promise<{ type: string; status: string } | undefined> {
    try {
      const d = await this.#call(() => this.#client.workflow.getHandle(sessionWorkflowId(sessionId)).describe())
      return { type: d.type, status: d.status.name }
    } catch (err) {
      if (err instanceof WorkflowNotFoundError) return undefined
      throw err
    }
  }

  /**
   * The committed conversation, a page at a time; read again from the start when a page says the
   * conversation moved (a turn committed meanwhile). Undefined when nothing is committed.
   */
  async conversation(sessionId: string): Promise<DurableConversation | undefined> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    const page = (start: number) => this.#call(() => handle.query<ForkPointPage, [{ start: number; limit: number }]>(FORK_POINT_QUERY, { start, limit: PAGE_CHARS }))
    for (let read = 0; read < 3; read++) {
      const first = await page(0)
      if (!first.session_id || !first.checkpoint || first.total === 0) return undefined
      const texts = [...first.entries]
      let moved = false
      while (texts.length < first.total) {
        const next = await page(texts.length)
        if (next.total !== first.total || next.checkpoint !== first.checkpoint || next.entries.length === 0) {
          moved = true
          break
        }
        texts.push(...next.entries)
      }
      if (moved) continue
      return {
        claudeSessionId: first.session_id,
        checkpoint: first.checkpoint,
        busy: first.busy,
        entries: texts.map((t) => JSON.parse(t) as Entry),
        pending: first.pending ?? {},
      }
    }
    throw new SessionError('busy', `session ${sessionId} kept moving while its conversation was read; try again once its turn ends`)
  }

  /** Whether the workflow holds a turn (running, or a message taken), or one of its calls waits on a person. */
  async #turn(sessionId: string): Promise<{ running: boolean; parked: boolean }> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    const [turn, parked] = await Promise.all([
      this.#call(() => handle.query<string | null>(TURN_QUERY)),
      this.#call(() => handle.query<unknown[]>(PENDING_INPUT_QUERY)),
    ])
    return { running: Boolean(turn), parked: Array.isArray(parked) && parked.length > 0 }
  }

  /** Where an open DurableSession stands, read only (a describe and Queries): the dry run's line. */
  async survey(sessionId: string): Promise<DurableSurvey> {
    const conversation = await this.conversation(sessionId)
    const turn = await this.#turn(sessionId)
    const entries = conversation?.entries ?? []
    const shapes: Record<string, Set<string>> = {}
    for (const e of entries) for (const k of Object.keys(e)) (shapes[e.type] ??= new Set()).add(k)
    const at = entries.find((e) => e.uuid === conversation?.checkpoint)
    const state: DurableState = turn.parked
      ? 'parked'
      : turn.running || conversation?.busy
        ? 'busy'
        : !conversation
          ? 'empty'
          : markerOf(at) !== undefined
            ? 'deferral'
            : 'idle'
    return {
      sessionId,
      state,
      versions: [...new Set(entries.map((e) => (e as { version?: unknown }).version).filter((v): v is string => typeof v === 'string'))].sort(),
      entries: entries.length,
      shapes: Object.fromEntries(Object.entries(shapes).map(([t, ks]) => [t, [...ks].sort()])),
    }
  }

  /**
   * Migrates `sessionId`'s DurableSession (see the header). `stopTurn`: a turn that still runs is
   * stopped and its end awaited (the backfill); without it such a session is refused `busy` (a
   * send, which has claimed the row, so no turn of its own runs). Refused `busy` while another
   * migration of the session runs, `unavailable` when Temporal or the workflow's worker does not answer.
   */
  async migrate(sessionId: string, { stopTurn = false }: { stopTurn?: boolean } = {}): Promise<MigrateOutcome> {
    // A session-level lock on a connection of its own, not an open transaction, held across the
    // Temporal waits.
    const conn = await this.#sql.reserve()
    const lock = `migrate-durable:${sessionId}`
    try {
      const [locked] = await conn<{ ok: boolean }[]>`SELECT pg_try_advisory_lock(hashtext(${lock})) AS ok`
      if (!locked?.ok) throw new SessionError('busy', `session ${sessionId} is being moved to the new session runtime; send it again in a moment`)
      try {
        return await this.#migrate(sessionId, stopTurn)
      } finally {
        await conn`SELECT pg_advisory_unlock(hashtext(${lock}))`
      }
    } catch (err) {
      if (err instanceof SessionError) throw err
      if (err instanceof DurableUnavailable || isGrpcDeadlineError(err)) {
        throw new SessionError('unavailable', `session ${sessionId}'s workflow did not answer while it was moved to the new session runtime; try again in a moment`)
      }
      throw err
    } finally {
      conn.release()
    }
  }

  async #migrate(sessionId: string, stopTurn: boolean): Promise<MigrateOutcome> {
    const [row] = await this.#sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${sessionId}`
    if (!row) throw new SessionError('not_found', `no session ${sessionId}`)
    let described = await this.#describe(sessionId)
    // Gone (retention removed a closed run): a session whose conversation was written already
    // goes on at step 4, as one whose DurableSession is still retained does.
    if (!described) return (await this.#transcribed(sessionId)) ? this.#finish(sessionId, row) : 'none'
    if (described.type !== DURABLE_WORKFLOW) {
      await this.#unified(sessionId)
      return 'agent_session'
    }
    if (described.status === 'RUNNING') {
      await this.#endDurable(sessionId, stopTurn)
      described = await this.#describe(sessionId)
    }
    // DurableSession completes only on `end`: then its conversation was written (step 2) before it.
    // One that failed or was terminated before that lost its conversation.
    if (described?.status !== 'COMPLETED' && !(await this.#transcribed(sessionId))) return 'lost'
    return this.#finish(sessionId, row)
  }

  /** Whether the session's conversation is in ai_session_entries (step 2 ran). */
  async #transcribed(sessionId: string): Promise<boolean> {
    const [any] = await this.#sql`SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '' LIMIT 1`
    return Boolean(any)
  }

  /** Step 4, once the DurableSession is closed or gone. */
  async #finish(sessionId: string, row: { status: string }): Promise<MigrateOutcome> {
    // A session marked done gets no new workflow; its conversation is kept, as a done classic one's is.
    if (row.status === 'done') {
      await this.#unified(sessionId)
      return 'migrated'
    }
    // An archived one gets its AgentSession now, idle until it is unarchived and sent to: open, so
    // no retention removes it in the meantime.
    await this.#startAgentSession(sessionId)
    await this.#unified(sessionId)
    return 'migrated'
  }

  /** Steps 1-3 on an open DurableSession. */
  async #endDurable(sessionId: string, stopTurn: boolean): Promise<void> {
    await this.#gate.cancelInput(sessionId, MIGRATED_REASON)
    let conversation = await this.conversation(sessionId)
    let turn = await this.#turn(sessionId)
    if (turn.running || conversation?.busy) {
      if (!stopTurn) throw new SessionError('busy', `a turn is still running in session ${sessionId}; wait for it to finish or interrupt it`)
      await this.#gate.interrupt(sessionId, MIGRATED_REASON)
      const until = Date.now() + this.#settleMs
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, this.#pollMs))
        conversation = await this.conversation(sessionId)
        turn = await this.#turn(sessionId)
        if (!turn.running && !conversation?.busy) break
        if (Date.now() > until) throw new SessionError('unavailable', `session ${sessionId}'s turn did not end after it was stopped; try again in a moment`)
      }
    }
    if (conversation) {
      const entries = resumeTranscript(conversation, sessionId)
      // Written once: a run that stopped after the append finds its last entry there (the append
      // is one INSERT, all or nothing; the last is the checkpoint, or a paused call's result).
      const last = [...entries].reverse().find((e) => typeof e.uuid === 'string')?.uuid ?? conversation.checkpoint
      const [written] = await this.#sql`
        SELECT 1 FROM ai_session_entries WHERE session_id = ${sessionId} AND subpath = '' AND uuid = ${last} LIMIT 1`
      if (!written) await this.#deps.store.append({ projectKey: '', sessionId }, entries)
    }
    await this.#gate.end(sessionId)
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    await this.#call(() => handle.result(), this.#settleMs).catch((err: unknown) => {
      if (isGrpcDeadlineError(err)) throw err
      // A failed or terminated run, told apart by the describe after this. Its name only: no payload.
      console.warn(`migrate durable: session ${sessionId}'s DurableSession did not complete (${err instanceof Error ? err.name : 'error'})`)
    })
    // Its parked calls' rows, which no run will resolve now: the real workflow resolved them at
    // `cancel_input`; any it left go here, with the same reason.
    await cancelOrphanedEntries({ sql: this.#sql, events: this.#deps.events, audit: this.#deps.audit }, { sessionId }, MIGRATED_REASON)
  }

  /** Step 4: AgentSession under the session's workflow id, which only a migration may reuse. */
  async #startAgentSession(sessionId: string): Promise<void> {
    const [s] = await this.#sql<{ owner_kind: string; owner_id: string; owner_label: string; creator_kind: string; creator_id: string; max_turns: number }[]>`
      SELECT owner_kind, owner_id, owner_label, creator_kind, creator_id, max_turns FROM ai_sessions WHERE id = ${sessionId}`
    if (!s) throw new SessionError('not_found', `no session ${sessionId}`)
    await this.#call(() =>
      this.#client.workflow.start(SESSION_WORKFLOW, {
        workflowId: sessionWorkflowId(sessionId),
        taskQueue: this.#deps.sessionQueue ?? SESSION_QUEUE,
        args: [
          {
            session_id: sessionId,
            creator: { kind: s.creator_kind, id: s.creator_id, label: '' },
            owner: { kind: s.owner_kind, id: s.owner_id, label: s.owner_label },
            max_turns: s.max_turns,
            system_append: null,
          },
        ],
        workflowIdReusePolicy: 'ALLOW_DUPLICATE',
        workflowIdConflictPolicy: 'USE_EXISTING',
      }),
    )
  }

  async #unified(sessionId: string): Promise<void> {
    await this.#sql`UPDATE ai_sessions SET runtime = 'unified', updated_at = now() WHERE id = ${sessionId} AND runtime IS DISTINCT FROM 'unified'`
  }
}

/** One open DurableSession's line in the backfill's report. */
export type BackfillLine = {
  session: string
  row: { status: string; archived: boolean } | null
  /** --dry-run: where it stands (`no_key`: its data key is missing, so it was not asked). */
  state?: DurableState | 'no_key'
  versions?: string[]
  entries?: number
  /** A migration's outcome (`no_row`: no ai_sessions row, so it is left as it is). */
  outcome?: MigrateOutcome | 'no_row'
  error?: string
}

export type BackfillReport = {
  lines: BackfillLine[]
  /** Each line's state, outcome or `error`, counted. */
  counts: Record<string, number>
  failed: number
  /** Every entry type's fields across the sessions (--dry-run): a shape the Step 7.1 fixture may not cover. */
  shapes: Record<string, string[]>
  /** Open DurableSessions after the run (the same list on a dry run): Phase 8 waits for 0. */
  open: number
}

/**
 * The backfill (spec §10.2 step 3): every open DurableSession migrated as its next send would
 * migrate it, a turn that still runs stopped first; or with `dryRun`, each surveyed and nothing
 * changed (Queries only). `only` narrows the list (tests: another test's workflows).
 */
export async function backfill(
  client: Client,
  sql: Sql,
  migration: DurableMigration,
  { dryRun = false, only = () => true }: { dryRun?: boolean; only?: (workflowId: string) => boolean } = {},
): Promise<BackfillReport> {
  const listed: string[] = []
  for await (const id of openDurableSessions(client)) if (only(id)) listed.push(id)
  const lines: BackfillLine[] = []
  const counts: Record<string, number> = {}
  const shapes: Record<string, Set<string>> = {}
  let failed = 0
  for (const workflowId of listed) {
    const session = workflowId.replace(/^session-/, '')
    const line: BackfillLine = { session, row: null }
    try {
      const [row] = await sql<{ status: string; archived: boolean }[]>`
        SELECT status, archived_at IS NOT NULL AS archived FROM ai_sessions WHERE id = ${session}`
      line.row = row ?? null
      if (dryRun) {
        // Sealing a Query's argument would make a data key that is missing: not asked.
        const [key] = await sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${workflowId}`
        if (!key) {
          line.state = 'no_key'
        } else {
          const survey = await migration.survey(session)
          Object.assign(line, { state: survey.state, versions: survey.versions, entries: survey.entries })
          for (const [type, fields] of Object.entries(survey.shapes)) for (const f of fields) (shapes[type] ??= new Set()).add(f)
        }
      } else {
        line.outcome = row ? await migration.migrate(session, { stopTurn: true }) : 'no_row'
      }
    } catch (err) {
      failed++
      // The error's name and the refusal's text: no payload is in either.
      line.error = err instanceof Error ? `${err.name}: ${err.message}`.slice(0, 300) : 'error'
    }
    const key = line.error ? 'error' : String(line.state ?? line.outcome)
    counts[key] = (counts[key] ?? 0) + 1
    lines.push(line)
  }
  let open = listed.length
  if (!dryRun) {
    open = 0
    for await (const id of openDurableSessions(client)) if (only(id)) open++
  }
  return { lines, counts, failed, shapes: Object.fromEntries(Object.entries(shapes).map(([t, s]) => [t, [...s].sort()])), open }
}
