import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import type { AuditLog } from '../audit/log.js'
import { harnessPrincipal } from '../auth/principal.js'
import type { Owner } from '../sessions/protocol.js'
import { parsedOrRaw, runToolWithOutcome, type Tool, type ToolRun, type ToolServices } from '../tools/registry.js'

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
// The session is the workflow's: an activity of `session-<id>` runs as that session.
// Any other caller (a flow before phase 6, a workflow no session started) is refused.

/** Where a session's owner is read. */
export interface SessionOwners {
  ownerOf(sessionId: string): Promise<Owner | undefined>
}

export class PgSessionOwners implements SessionOwners {
  constructor(private readonly sql: Sql) {}

  async ownerOf(sessionId: string): Promise<Owner | undefined> {
    const [row] = await this.sql<{ owner_kind: Owner['kind']; owner_id: string; owner_label: string }[]>`
      SELECT owner_kind, owner_id, owner_label FROM ai_sessions WHERE id = ${sessionId}`
    return row ? { kind: row.owner_kind, id: row.owner_id, label: row.owner_label } : undefined
  }
}

export type ToolActivityDeps = {
  services: ToolServices
  sessions: SessionOwners
  audit?: Pick<AuditLog, 'record' | 'hash' | 'summarise'> | undefined
  /** How often a running call heartbeats, so a cancel reaches it (default 10 s). */
  heartbeatMs?: number
}

export type ToolActivity = (input: unknown) => Promise<unknown>

/** The failure type of a call that ran and did not succeed: never retried. */
export const TOOL_ERROR = 'ToolError'
/** The failure type of a call from a workflow that is no known session. */
export const UNKNOWN_SESSION = 'UnknownSession'

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

async function runAsActivity(
  tool: Tool,
  input: unknown,
  deps: ToolActivityDeps,
  lookup: (name: string) => Tool | undefined,
): Promise<unknown> {
  const context = Context.current()
  const { workflowExecution, activityId } = context.info
  const session = sessionOf(workflowExecution?.workflowId)
  // A lookup that throws (the database is away) is left to the activity's retries.
  const owner = session === undefined ? undefined : await deps.sessions.ownerOf(session)
  if (session === undefined || owner === undefined) {
    throw ApplicationFailure.nonRetryable(
      `${tool.name} runs only for a ScadBuddy session; ${workflowExecution?.workflowId ?? 'this workflow'} is none`,
      UNKNOWN_SESSION,
    )
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
    toolUseId: activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId,
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
  return run.result.content
}
