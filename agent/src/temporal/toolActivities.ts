import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import type { AuditLog } from '../audit/log.js'
import { harnessPrincipal } from '../auth/principal.js'
import type { Owner } from '../sessions/protocol.js'
import { parsedOrRaw, runToolWithOutcome, type Tool, type ToolRun, type ToolServices } from '../tools/registry.js'
import type { AnswerReader } from '../gate/answers.js'
import { durableRequestId } from '../gate/ids.js'
import { DURABLE_ONLY_NAMES } from '../tools/answerTools.js'

// Every tool as an activity on `agent-tools` (spec 2026-10-01 §6.3, #1055), registered
// under the tool's name, which is how the durable worker's `activity_as_tool` stubs
// reach it (temporalio/ai-integrations `claude_agent_sdk`: `execute_activity(fn,
// call.input, activity_id="tool-<tool_use_id>", task_queue=...)`, one dict in).
//
// Each runs `runToolWithOutcome`, the entry point /mcp uses, so parsing, tiers, scope
// and the untrusted-data envelope are unchanged; and with the context
// `createHarnessServer` gives a classic turn (tools/harness.ts): the session owner's
// principal, `session` (commit trailers, #252; what it touched, #931), the tab paired
// with the session, `lookup` for confirm_action. `gate: 'workflow'`: the session
// workflow approved an outward call before scheduling it (§6.4), so it is not
// prepared again.
//
// The session is the workflow's: an activity of `session-<id>` runs as that session,
// and only when that session is durable (`ai_sessions.mode`, §6.1). A classic
// session's outward calls park in ai_approvals, so a workflow that borrowed its id
// must not skip that. Any other caller (a flow before phase 6, a workflow no session
// started) is refused.

/** Where a durable session's owner is read; undefined for any other session. */
export interface SessionOwners {
  ownerOf(sessionId: string): Promise<Owner | undefined>
}

export class PgSessionOwners implements SessionOwners {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  async ownerOf(sessionId: string): Promise<Owner | undefined> {
    const [row] = await this.sql<{ owner_kind: Owner['kind']; owner_id: string; owner_label: string }[]>`
      SELECT owner_kind, owner_id, owner_label FROM ai_sessions WHERE id = ${sessionId} AND mode = 'durable'`
    return row ? { kind: row.owner_kind, id: row.owner_id, label: row.owner_label } : undefined
  }
}

/**
 * The gate's recorded decisions (spec §6.6): whether `ai_input_responses` holds an
 * `approved` outcome for a durable call's request id, which resolve_input writes
 * before the workflow lets the call through.
 */
export interface ApprovalRecords {
  approved(requestId: string): Promise<boolean>
}

export class PgApprovalRecords implements ApprovalRecords {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async approved(requestId: string): Promise<boolean> {
    const rows = await this.#sql`
      SELECT 1 FROM ai_input_responses
      WHERE request_id = ${requestId} AND kind = 'approval' AND outcome = 'approved'`
    return rows.length > 0
  }
}

export type ToolActivityDeps = {
  services: ToolServices
  sessions: SessionOwners
  /** Where a gated call's approval is checked; a gated call is refused without it. */
  approvals?: ApprovalRecords | undefined
  audit?: Pick<AuditLog, 'record' | 'hash' | 'summarise'> | undefined
  /** The recorded answers an `answer` tool returns (gate/answers.ts); those tools refuse without it. */
  answers?: AnswerReader | undefined
  /** How often a running call heartbeats, so a cancel reaches it (default 10 s). */
  heartbeatMs?: number
}

export type ToolActivity = (input: unknown) => Promise<unknown>

/** The failure type of a call that ran and did not succeed: never retried. */
export const TOOL_ERROR = 'ToolError'
/** The failure type of a call from a workflow that is no known session. */
export const UNKNOWN_SESSION = 'UnknownSession'
/** The failure type of a gated call with no approval recorded for it. */
export const NOT_APPROVED = 'NotApproved'

/** What the model reads in place of an image a tool returned (plan 5c Ruling 4). */
export const DURABLE_IMAGE_NOTE =
  '[The tool returned an image. Durable sessions do not yet receive images from tools; use a classic session to see it.]'

/**
 * A result as the durable plugin hands it to the model: text. The plugin delivers an
 * activity's result as `ToolOutcome(content=result)` and JSON-encodes anything but a
 * string, so content blocks would reach the model as JSON, an image as its base64.
 */
function resultText(content: readonly { type: string; text?: string }[]): string {
  return content.map((block) => (block.type === 'text' ? (block.text ?? '') : DURABLE_IMAGE_NOTE)).join('\n')
}

const SESSION_WORKFLOW = /^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i
const DEFAULT_HEARTBEAT_MS = 10_000

/** The session a workflow ID names, or undefined for any other workflow. */
export function sessionOf(workflowId: string | undefined): string | undefined {
  return workflowId === undefined ? undefined : SESSION_WORKFLOW.exec(workflowId)?.[1]?.toLowerCase()
}

/** The text a non-ok run shows the model: the error result's text blocks. */
function failureText(run: ToolRun): string {
  const texts = run.result.content.flatMap((block) => (block.type === 'text' ? [block.text] : []))
  return texts.join('\n') || run.detail || 'the tool failed'
}

export function toolActivities(tools: readonly Tool[], deps: ToolActivityDeps): Record<string, ToolActivity> {
  const byName = new Map(tools.map((t) => [t.name, t]))
  const lookup = (name: string): Tool | undefined => byName.get(name)
  return Object.fromEntries(tools.map((tool) => [tool.name, (input: unknown) => runAsActivity(tool, input, deps, lookup)]))
}

/** The durable session the current activity's workflow is, and its owner; refused otherwise. */
async function durableSession(what: string, sessions: SessionOwners): Promise<{ session: string; owner: Owner }> {
  const { workflowExecution } = Context.current().info
  const session = sessionOf(workflowExecution?.workflowId)
  // A lookup that throws (the database is away) is left to the activity's retries.
  const owner = session === undefined ? undefined : await sessions.ownerOf(session)
  if (session === undefined || owner === undefined) {
    throw ApplicationFailure.nonRetryable(
      `${what} runs only for a durable ScadBuddy session; ${workflowExecution?.workflowId ?? 'this workflow'} is none`,
      UNKNOWN_SESSION,
    )
  }
  return { session, owner }
}

async function runAsActivity(
  tool: Tool,
  input: unknown,
  deps: ToolActivityDeps,
  lookup: (name: string) => Tool | undefined,
): Promise<unknown> {
  const context = Context.current()
  const { workflowExecution, activityId } = context.info
  const { session, owner } = await durableSession(tool.name, deps.sessions)
  const toolUseId = activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId
  // The call's gate entry, if it parked (spec §6.6): its approver, or its answer, is recorded under this id.
  const requestId = durableRequestId(session, workflowExecution?.runId ?? '', toolUseId)
  if (DURABLE_ONLY_NAMES.has(tool.name)) return answerAsActivity(tool, input, deps, { session, owner, toolUseId, requestId })
  // `gate: 'workflow'` below skips preparing an approval because the workflow parked
  // the call and a person approved it. That is checked here, not assumed: any client of
  // the namespace can name a workflow after a durable session (security review of 5b).
  if (tool.gated && !(await deps.approvals?.approved(requestId))) {
    const parsed = parsedOrRaw(tool, input)
    const now = new Date()
    await deps.audit?.record({
      kind: 'tool_call',
      action: tool.name,
      surface: 'harness',
      actor: owner,
      sessionId: session,
      toolUseId,
      requestId,
      tier: tool.risk,
      inputHash: deps.audit.hash(tool.name, parsed),
      inputSummary: deps.audit.summarise(tool.name, parsed),
      outcome: 'refused',
      detail: 'no approval is recorded for this call',
      startedAt: now,
      finishedAt: now,
    })
    throw ApplicationFailure.nonRetryable(`${tool.name} needs an approval, and none is recorded for this call`, NOT_APPROVED)
  }
  const principal = harnessPrincipal(owner)
  const services = deps.services
  const heartbeat = setInterval(() => context.heartbeat(), deps.heartbeatMs ?? DEFAULT_HEARTBEAT_MS)
  const startedAt = new Date()
  let run: ToolRun
  try {
    run = await runToolWithOutcome(tool, input, {
      ...services,
      // The tab paired with this session (#254), as for a classic turn.
      ...(services.browser ? { browser: services.browser.forSession(session) } : {}),
      principal,
      session,
      progress: async (progress, total, message) => context.heartbeat({ progress, total, message }),
      signal: context.cancellationSignal,
      lookup,
      gate: 'workflow',
    })
  } finally {
    clearInterval(heartbeat)
  }
  const action = run.ran?.tool ?? tool.name
  const parsed = run.ran?.input ?? parsedOrRaw(tool, input)
  // As the harness records a turn's calls (audit/turn.ts); the approval is the workflow's.
  await deps.audit?.record({
    kind: 'tool_call',
    action,
    surface: 'harness',
    actor: owner,
    sessionId: session,
    toolUseId,
    requestId,
    tier: run.ran ? (lookup(run.ran.tool)?.risk ?? tool.risk) : tool.risk,
    inputHash: deps.audit.hash(action, parsed),
    inputSummary: deps.audit.summarise(action, parsed),
    outcome: run.outcome,
    ...(run.detail === undefined ? {} : { detail: run.detail }),
    startedAt,
    finishedAt: new Date(),
  })
  if (context.cancellationSignal.aborted) throw new CancelledFailure('the call was cancelled')
  if (run.outcome !== 'ok') throw ApplicationFailure.nonRetryable(failureText(run), TOOL_ERROR)
  return resultText(run.result.content)
}

/**
 * An `answer` tool's call (ask_user, wait_for_user): DurableSession parked it and the
 * user answered, or its timer ended it, before it was let through; the result is what
 * was recorded, never a handler's (gate/answers.ts).
 */
async function answerAsActivity(
  tool: Tool,
  input: unknown,
  deps: ToolActivityDeps,
  call: { session: string; owner: Owner; toolUseId: string; requestId: string },
): Promise<unknown> {
  if (!deps.answers) throw ApplicationFailure.nonRetryable(`${tool.name} needs the database its answers are recorded in`, TOOL_ERROR)
  const startedAt = new Date()
  const answer = await deps.answers.result(call.requestId, tool.name, input)
  const parsed = parsedOrRaw(tool, input)
  await deps.audit?.record({
    kind: 'tool_call',
    action: tool.name,
    surface: 'harness',
    actor: call.owner,
    sessionId: call.session,
    toolUseId: call.toolUseId,
    requestId: call.requestId,
    tier: tool.risk,
    inputHash: deps.audit.hash(tool.name, parsed),
    inputSummary: deps.audit.summarise(tool.name, parsed),
    outcome: answer.ok ? 'ok' : 'refused',
    startedAt,
    finishedAt: new Date(),
  })
  if (!answer.ok) throw ApplicationFailure.nonRetryable(answer.text, TOOL_ERROR)
  return answer.text
}

/** The activity that describes a call for its gate entry (gateActivities). */
export const DESCRIBE_CALL_ACTIVITY = 'gate.describe_call'

/**
 * What DurableSession needs to open an `approval` entry (spec §6.6): the call's
 * scrubbed summary and its input hash, computed as a classic approval's are (the
 * approvals' HMAC key, audit/log.ts), so the agent alone holds the key and the
 * scrubbing rules. Served on `agent-tools` beside the tools.
 */
export function gateActivities(deps: Pick<ToolActivityDeps, 'audit' | 'sessions'>): Record<string, ToolActivity> {
  return {
    [DESCRIBE_CALL_ACTIVITY]: async (args: unknown) => {
      // The hash is keyed by the approvals' HMAC key: only a durable session's workflow
      // gets one (security review of 5b).
      await durableSession(DESCRIBE_CALL_ACTIVITY, deps.sessions)
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (!deps.audit) throw ApplicationFailure.nonRetryable('describing a call needs the audit log and its key', TOOL_ERROR)
      if (typeof tool !== 'string' || typeof input !== 'object' || input === null || Array.isArray(input)) {
        throw ApplicationFailure.nonRetryable('describe_call takes {tool: string, input: object}', TOOL_ERROR)
      }
      const fields = input as Record<string, unknown>
      return { summary: deps.audit.summarise(tool, fields), input_hash: deps.audit.hash(tool, fields) }
    },
  }
}
