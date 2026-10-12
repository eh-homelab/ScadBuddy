import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import type { AuditLog } from '../audit/log.js'
import { type Tier, TIERS } from '../auth/principal.js'
import { imageOfBlock, RESULT_IMAGES_MAX, type SessionImage } from '../sessions/blobs.js'
import type { Owner } from '../sessions/protocol.js'
import { effectiveTool, type FlowImages, parsedOrRaw, runToolWithOutcome, type Tool, type ToolRun, type ToolServices } from '../tools/registry.js'
import type { AnswerReader } from '../gate/answers.js'
import { durableRequestId, flowRequestId } from '../gate/ids.js'
import { DURABLE_ONLY_NAMES } from '../tools/answerTools.js'
import { turnPrincipal } from '../tools/harness.js'
import { type FlowBlobStore, type FlowImage, flowImageOf } from './flowBlobs.js'
import { flowOf, type FlowRuns } from './flowRuns.js'
import { type SessionFiles, withWorkspaceSize } from '../workspace/store.js'

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
// must not skip that. A flow run's workflow (`flow-<run id>`, #1057) runs as the run's
// starter, in no session, and a gated call needs a person's decision in the backend's
// `workflow_run_decisions` (flowRuns.ts). Any other caller is refused.

/** Where a durable session's owner is read; undefined for any other session. */
export interface SessionOwners {
  /** The owner, and the tiers its current turn was sent with (`ai_sessions.turn_tiers`, #2320). */
  ownerOf(sessionId: string): Promise<(Owner & { turnTiers?: readonly Tier[] | null }) | undefined>
}

export class PgSessionOwners implements SessionOwners {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  async ownerOf(sessionId: string): Promise<(Owner & { turnTiers: readonly Tier[] | null }) | undefined> {
    const [row] = await this.sql<
      {
        owner_kind: Owner['kind']
        owner_id: string
        owner_label: string
        turn_tiers: { by?: { kind?: string; id?: string }; tiers?: unknown } | null
      }[]
    >`
      SELECT owner_kind, owner_id, owner_label, turn_tiers FROM ai_sessions WHERE id = ${sessionId} AND mode = 'durable'`
    if (!row) return undefined
    // Only the sender's own tiers, while it still owns the session (a handoff since
    // leaves the new owner its default), and only tiers there are.
    const sent = row.turn_tiers
    const mine = sent?.by?.kind === row.owner_kind && sent.by.id === row.owner_id && Array.isArray(sent.tiers)
    const turnTiers = mine ? TIERS.filter((t) => (sent.tiers as unknown[]).includes(t)) : null
    return { kind: row.owner_kind, id: row.owner_id, label: row.owner_label, turnTiers }
  }
}

/** What an approval was given for: the call's input hash, which keys the tool and its input. */
export type ApprovalBinding = { inputHash: string }

/**
 * The gate's recorded decisions (spec §6.6): whether `ai_input_responses` holds an
 * `approved` outcome for a durable call's request id, which resolve_input writes
 * before the workflow lets the call through, given for this very call (#2088): the
 * approval's audit row, written in the same transaction, carries the input hash
 * describe_call computed for the parked call (the approvals' HMAC key over the tool's
 * name and its input), so an approval never authorises another tool or other input.
 */
export interface ApprovalRecords {
  approved(requestId: string, call?: ApprovalBinding): Promise<boolean>
}

export class PgApprovalRecords implements ApprovalRecords {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async approved(requestId: string, call?: ApprovalBinding): Promise<boolean> {
    // Without the call's hash nothing can be matched, so nothing is approved.
    if (call === undefined) return false
    const rows = await this.#sql`
      SELECT 1 FROM ai_input_responses r
      JOIN ai_audit a ON a.request_id = r.request_id AND a.kind = 'approval' AND a.action = 'approved'
      WHERE r.request_id = ${requestId} AND r.kind = 'approval' AND r.outcome = 'approved'
        AND a.input_hash = ${call.inputHash}`
    return rows.length > 0
  }
}

export type ToolActivityDeps = {
  services: ToolServices
  sessions: SessionOwners
  /** Where a gated call's approval is checked; a gated call is refused without it. */
  approvals?: ApprovalRecords | undefined
  audit?: Pick<AuditLog, 'record' | 'hash' | 'summarise'> | undefined
  /** Flow runs' starters and decisions (flowRuns.ts); a flow's call is refused without it. */
  flows?: FlowRuns | undefined
  /** The recorded answers an `answer` tool returns (gate/answers.ts); those tools refuse without it. */
  answers?: AnswerReader | undefined
  /**
   * What an owner holds now (auth/tokens.ts `liveTokenTiers`): a turn's tiers are cut to
   * it, and a flow's token starter runs with it (#2320). Undefined: not kept anywhere.
   */
  currentTiers?: ((owner: Owner) => Promise<readonly Tier[] | undefined>) | undefined
  /** Where a durable call's result images are kept for `view_tool_images`; placeholders without it. */
  blobs?: ResultBlobs | undefined
  /** Where a flow call's result images are kept, by the run's subject (#2477); placeholders without it. */
  flowBlobs?: FlowBlobStore | undefined
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
/** The failure type of a call from a `flow-<id>` workflow with no flow run recorded. */
export const UNKNOWN_FLOW = 'UnknownFlow'
/** The failure type of a tool a flow cannot use: a browser tool (no tab) or an answer tool (no session). */
export const NOT_FOR_FLOWS = 'NotForFlows'

/**
 * The durable segment's in-process tool that shows the model the images a tool returned
 * (agent-durable session/runner.py `view_tool_images`, #2320).
 */
export const TOOL_IMAGES_TOOL = 'view_tool_images'
/** What stands where a tool returned an image the model could not take (not one of the four types, or not what it claims). */
export const UNUSABLE_IMAGE = '[an image the model cannot take]'
/** Where a flow's call returned an image and this worker has no store to keep it in (#2477: `flowBlobs`). */
export const FLOW_IMAGE = '[an image]'

/** Where a durable activity keeps a result's images: the session's blobs (sessions/blobs.ts). */
export type ResultBlobs = { put(sessionId: string, images: readonly SessionImage[]): Promise<void> }

type Block = { type: string; text?: string; data?: unknown; mimeType?: unknown }

/**
 * A result's text with each image named by its bytes (`[image <name>]`), and the images
 * to keep: at most RESULT_IMAGES_MAX, each once. A block that is not one of the four
 * types, or not what it claims, or one past the cap, is UNUSABLE_IMAGE.
 */
function namedImages(content: readonly Block[]): { parts: string[]; images: SessionImage[] } {
  const images: SessionImage[] = []
  const parts = content.map((block) => {
    if (block.type === 'text') return block.text ?? ''
    if (block.type !== 'image') return `[${block.type}]`
    const image = imageOfBlock({ type: 'image', source: { type: 'base64', media_type: block.mimeType, data: block.data } })
    if (!image || images.length >= RESULT_IMAGES_MAX) return UNUSABLE_IMAGE
    if (!images.some((i) => i.name === image.name)) images.push(image)
    return `[image ${image.name}]`
  })
  return { parts, images }
}

/**
 * A result as the durable plugin hands it to the model: text. The plugin delivers an
 * activity's result as `ToolOutcome(content=result)` and JSON-encodes anything but a
 * string, so content blocks would reach the model as JSON, an image as its base64.
 * So each image is kept in the session's blobs, named by its bytes (sessions/blobs.ts),
 * and named in the text, which tells the model to call `view_tool_images` with the
 * names: that tool returns them as image blocks, as a classic turn's tool result is.
 * At most RESULT_IMAGES_MAX are kept; one past it is a placeholder, and with nowhere to
 * keep them every image is FLOW_IMAGE.
 */
export async function resultText(content: readonly Block[], keep?: { session: string; blobs: ResultBlobs }): Promise<string> {
  if (!keep) {
    return content.map((block) => (block.type === 'text' ? (block.text ?? '') : block.type === 'image' ? FLOW_IMAGE : `[${block.type}]`)).join('\n')
  }
  const { parts, images } = namedImages(content)
  if (images.length) {
    await keep.blobs.put(keep.session, images)
    const n = images.length
    parts.push(
      `[The tool returned ${n === 1 ? 'an image' : `${n} images`}. Call ${TOOL_IMAGES_TOOL} with ${n === 1 ? 'its name' : 'their names'} (${images
        .map((i) => i.name)
        .join(', ')}) to see ${n === 1 ? 'it' : 'them'}.]`,
    )
  }
  return parts.join('\n')
}

/** A flow call's result when it returned images: its text, and a pointer to each image. */
export type FlowToolResult = { text: string; images: FlowImage[] }

/**
 * A flow call's result (#2477): its text alone, as before, when it returned no image.
 * With images, each is kept under the run's subject (flowBlobs.ts) and the flow gets
 * `{text, images}`, the images as pointers `{name, mediaType, bytes}`: the bytes never
 * enter the activity's result, the run's history or its stored result. Without a store,
 * each image stays FLOW_IMAGE.
 */
export async function flowResult(
  content: readonly Block[],
  keep: { subject: string; blobs: FlowBlobStore } | undefined,
): Promise<string | FlowToolResult> {
  if (!keep) return resultText(content)
  const { parts, images } = namedImages(content)
  const text = parts.join('\n')
  if (!images.length) return text
  await keep.blobs.put(keep.subject, images)
  return { text, images: images.map(flowImageOf) }
}

// Lowercase only, as the codec's subjects are (payloadCodec.ts): a session's payloads are sealed.
const SESSION_WORKFLOW = /^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/
const DEFAULT_HEARTBEAT_MS = 10_000

/** The session a workflow ID names, or undefined for any other workflow. */
export function sessionOf(workflowId: string | undefined): string | undefined {
  return workflowId === undefined ? undefined : SESSION_WORKFLOW.exec(workflowId)?.[1]
}

/** The images of one flow run, and of no other. */
function flowImagesOf(blobs: FlowBlobStore, subject: string): FlowImages {
  return { get: (name) => blobs.get(subject, name) }
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
export async function durableSession(what: string, sessions: SessionOwners): Promise<{ session: string; owner: Owner; turnTiers?: readonly Tier[] }> {
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
  const { turnTiers, ...who } = owner
  return { session, owner: who, ...(turnTiers ? { turnTiers } : {}) }
}

/** Who a call runs as, in which session if any, and the id its gate entry is recorded under. */
type Caller = {
  owner: Owner
  session: string | undefined
  requestId: string
  approvals: ApprovalRecords | undefined
  /** The tiers the call runs with; undefined: the owner's default (`harnessPrincipal`). */
  tiers: readonly Tier[] | undefined
}

/**
 * A flow run's call: its starter, no session, and the run's decisions as its approvals,
 * each bound to this call's tool and input (#2318): the hash is made from what this call
 * was given, so an approval of the parked call authorises no other tool or input.
 */
async function flowCaller(tool: Tool, flow: string, deps: ToolActivityDeps, toolUseId: string): Promise<Caller> {
  if (DURABLE_ONLY_NAMES.has(tool.name) || tool.name.startsWith('browser_')) {
    throw ApplicationFailure.nonRetryable(`${tool.name} is not for flows: a flow has no session and no browser tab`, NOT_FOR_FLOWS)
  }
  // A lookup that throws (the database is away) is left to the activity's retries.
  const owner = deps.flows ? await deps.flows.startedBy(flow) : undefined
  if (owner === undefined) {
    throw ApplicationFailure.nonRetryable(`${tool.name}: no flow run ${flow} is recorded`, UNKNOWN_FLOW)
  }
  const runId = Context.current().info.workflowExecution?.runId ?? ''
  // A token starter's run acts with what its token holds now, as its sessions' turns do (#2320).
  const tiers = owner.kind === 'browser' ? undefined : await deps.currentTiers?.(owner)
  const flows = deps.flows
  // The caller's binding is the hash of what this call was given; none approves nothing.
  const approvals = flows && { approved: (requestId: string, call?: ApprovalBinding) => flows.approved(requestId, call?.inputHash) }
  return { owner, session: undefined, requestId: flowRequestId(flow, runId, toolUseId), approvals, tiers }
}

/**
 * The tiers a durable turn's call runs with (#2320): those its sender sent it with
 * (`turn_tiers`), cut to what the owner holds now, so a token revoked or lowered since
 * the turn was sent takes effect at once. None recorded: the owner's default.
 */
export async function turnTiers(
  owner: Owner,
  sent: readonly Tier[] | undefined,
  deps: Pick<ToolActivityDeps, 'currentTiers'>,
): Promise<readonly Tier[] | undefined> {
  if (!sent || owner.kind === 'browser') return undefined
  const now = await deps.currentTiers?.(owner)
  return now ? sent.filter((t) => now.includes(t)) : sent
}

async function runAsActivity(
  tool: Tool,
  input: unknown,
  deps: ToolActivityDeps,
  lookup: (name: string) => Tool | undefined,
): Promise<unknown> {
  const context = Context.current()
  const { workflowExecution, activityId } = context.info
  const toolUseId = activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId
  const flow = flowOf(workflowExecution?.workflowId)
  let caller: Caller
  if (flow !== undefined) {
    caller = await flowCaller(tool, flow, deps, toolUseId)
  } else {
    const { session, owner, turnTiers: sent } = await durableSession(tool.name, deps.sessions)
    // The call's gate entry, if it parked (spec §6.6): its approver, or its answer, is recorded under this id.
    const requestId = durableRequestId(session, workflowExecution?.runId ?? '', toolUseId)
    if (DURABLE_ONLY_NAMES.has(tool.name)) return answerAsActivity(tool, input, deps, { session, owner, toolUseId, requestId })
    caller = { owner, session, requestId, approvals: deps.approvals, tiers: await turnTiers(owner, sent, deps) }
  }
  const { owner, session, requestId } = caller
  // `gate: 'workflow'` below skips preparing an approval because the workflow parked
  // the call and a person approved it. That is checked here, not assumed: any client of
  // the namespace can name a workflow after a durable session (security review of 5b).
  // Gated as the tool runs now (#1953): a tier Settings raised to outward needs the
  // approval too, whatever tier the durable worker's manifest declared it at.
  //
  // One read decides the call (#2088): the tier and disabled state are resolved here, as
  // the call runs, never taken from the workflow's decision to park it, and that same
  // resolution is handed to runToolWithOutcome (`effective`), which does not read them
  // again. So a tool raised to outward after the workflow let it through unparked has no
  // approval and is refused NotApproved; one disabled after its approval is refused as
  // disabled; and no second read can disagree with the one that was checked.
  const effective = await effectiveTool(tool, deps.services)
  const refuse = async (detail: string, message: string, type: string): Promise<never> => {
    const parsed = parsedOrRaw(tool, input)
    const now = new Date()
    await deps.audit?.record({
      kind: 'tool_call',
      action: tool.name,
      surface: 'harness',
      actor: owner,
      sessionId: session ?? null,
      toolUseId,
      requestId,
      tier: effective.risk,
      inputHash: deps.audit.hash(tool.name, parsed),
      inputSummary: deps.audit.summarise(tool.name, parsed),
      outcome: 'refused',
      detail,
      startedAt: now,
      finishedAt: now,
    })
    throw ApplicationFailure.nonRetryable(message, type)
  }
  if (effective.disabled) {
    const message = `${tool.name} is disabled in Settings → Plugins; the user can turn it back on there`
    await refuse(message, message, TOOL_ERROR)
  }
  // The approval binds to this call's tool and input (ApprovalRecords): the hash
  // describe_call gave the parked call, computed again from what this call was given.
  const binding = deps.audit ? { inputHash: deps.audit.hash(tool.name, (input ?? {}) as Record<string, unknown>) } : undefined
  if (effective.gated && !(await caller.approvals?.approved(requestId, binding))) {
    await refuse(
      'no approval is recorded for this call',
      `${tool.name} needs an approval, and none is recorded for this call`,
      NOT_APPROVED,
    )
  }
  const principal = turnPrincipal(owner, caller.tiers ? { tiers: [...caller.tiers] } : undefined)
  const services = deps.services
  const heartbeat = setInterval(() => context.heartbeat(), deps.heartbeatMs ?? DEFAULT_HEARTBEAT_MS)
  const startedAt = new Date()
  let run: ToolRun
  try {
    run = await runToolWithOutcome(tool, input, {
      ...services,
      // The tab paired with this session (#254), as for a classic turn; a flow has none.
      ...(services.browser && session !== undefined ? { browser: services.browser.forSession(session) } : {}),
      // A flow's own images, which its calls may name as `flow_image` references (#2477).
      ...(flow !== undefined && deps.flowBlobs ? { flowImages: flowImagesOf(deps.flowBlobs, `flow-${flow}`) } : {}),
      principal,
      ...(session === undefined ? {} : { session }),
      progress: async (progress, total, message) => context.heartbeat({ progress, total, message }),
      signal: context.cancellationSignal,
      lookup,
      gate: 'workflow',
      effective,
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
    sessionId: session ?? null,
    toolUseId,
    requestId,
    tier: run.ran ? (lookup(run.ran.tool)?.risk ?? effective.risk) : effective.risk,
    inputHash: deps.audit.hash(action, parsed),
    inputSummary: deps.audit.summarise(action, parsed),
    outcome: run.outcome,
    ...(run.detail === undefined ? {} : { detail: run.detail }),
    startedAt,
    finishedAt: new Date(),
  })
  if (context.cancellationSignal.aborted) throw new CancelledFailure('the call was cancelled')
  if (run.outcome !== 'ok') throw ApplicationFailure.nonRetryable(failureText(run), TOOL_ERROR)
  if (flow !== undefined) return flowResult(run.result.content, deps.flowBlobs ? { subject: `flow-${flow}`, blobs: deps.flowBlobs } : undefined)
  return resultText(run.result.content, session !== undefined && deps.blobs ? { session, blobs: deps.blobs } : undefined)
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
 * scrubbed summary (with a workspace upload's size, workspace/store.ts
 * `withWorkspaceSize`) and its input hash, computed as a classic approval's are (the
 * approvals' HMAC key, audit/log.ts), so the agent alone holds the key and the
 * scrubbing rules. Served on `agent-tools` beside the tools.
 */
export function gateActivities(
  deps: Pick<ToolActivityDeps, 'audit' | 'sessions'> & { workspace?: SessionFiles | undefined },
): Record<string, ToolActivity> {
  return {
    [DESCRIBE_CALL_ACTIVITY]: async (args: unknown) => {
      // The hash is keyed by the approvals' HMAC key: only a durable session's workflow
      // gets one (security review of 5b).
      const { session } = await durableSession(DESCRIBE_CALL_ACTIVITY, deps.sessions)
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (!deps.audit) throw ApplicationFailure.nonRetryable('describing a call needs the audit log and its key', TOOL_ERROR)
      if (typeof tool !== 'string' || typeof input !== 'object' || input === null || Array.isArray(input)) {
        throw ApplicationFailure.nonRetryable('describe_call takes {tool: string, input: object}', TOOL_ERROR)
      }
      const fields = input as Record<string, unknown>
      // A workspace upload's size is shown (#2396), from the session's own workspace; the hash is of the call's input.
      const shown = await withWorkspaceSize(deps.workspace, { kind: 'session', sessionId: session }, fields)
      return { summary: deps.audit.summarise(tool, shown), input_hash: deps.audit.hash(tool, fields) }
    },
  }
}
