import { setTimeout as sleep } from 'node:timers/promises'
import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import { context as otelContext, SpanStatusCode } from '@opentelemetry/api'
import type { AuditLog } from '../audit/log.js'
import { contextFrom, withSpan } from '../telemetry/trace.js'
import { type Tier, TIERS } from '../auth/principal.js'
import { imageOfBlock, RESULT_IMAGES_MAX, type SessionImage } from '../sessions/blobs.js'
import type { Owner } from '../sessions/protocol.js'
import { withApprovalLine } from '../approvals/service.js'
import {
  effectiveTool,
  type FlowImages,
  isRefusal,
  isTransient,
  parsedOrRaw,
  runToolWithOutcome,
  type Tool,
  ToolError,
  toolErrorText,
  unexpectedErrorText,
  type ToolRun,
  type ToolServices,
} from '../tools/registry.js'
import { durableRequestId, flowRequestId, parseRequestId } from '../gate/ids.js'
import { type ToolCallArgs, type ToolCallBlock, type ToolCallResult, toolCallActivity } from './toolCall.js'
import { turnPrincipal } from '../tools/harness.js'
import { FLOW_IMAGES_MAX, type FlowBlobStore, type FlowImage, flowImageOf } from './flowBlobs.js'
import { flowOf, type FlowRuns } from './flowRuns.js'
import { FLOW_TOOL_TIER_ACTIVITY, flowToolTierActivity } from './flowTools.js'
import { BUILTIN_TIER_ACTIVITY, builtinTier, TOOL_TIER_ACTIVITY, toolTierActivity } from './toolTier.js'
import type { AccessResolver } from '../access/resolver.js'
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
// The session is the workflow's: an activity of `session-<id>` runs as that session.
// Every session runs on a session workflow (plan Step 6.3), so no session's calls are
// gated in-process any more and none is refused here by its mode. A flow run's workflow (`flow-<run id>`, #1057) runs as the run's
// starter, in no session, and a gated call needs a person's decision in the backend's
// `workflow_run_decisions` (flowRuns.ts). Any other caller is refused.

/** Where a session's owner is read; undefined when there is no such session. */
export interface SessionOwners {
  /**
   * The owner, the tiers its current turn was sent with (`ai_sessions.turn_tiers`, #2320),
   * and that turn (`durable_turn_id`), which its calls' audit rows carry as a classic turn's do.
   */
  ownerOf(sessionId: string): Promise<(Owner & { turnTiers?: readonly Tier[] | null; turnId?: string | null }) | undefined>
}

export class PgSessionOwners implements SessionOwners {
  private readonly sql: Sql

  constructor(sql: Sql) {
    this.sql = sql
  }

  async ownerOf(sessionId: string): Promise<(Owner & { turnTiers: readonly Tier[] | null; turnId: string | null }) | undefined> {
    const [row] = await this.sql<
      {
        owner_kind: Owner['kind']
        owner_id: string
        owner_label: string
        turn_tiers: { by?: { kind?: string; id?: string }; tiers?: unknown } | null
        durable_turn_id: string | null
      }[]
    >`
      SELECT owner_kind, owner_id, owner_label, turn_tiers, durable_turn_id FROM ai_sessions WHERE id = ${sessionId}`
    if (!row) return undefined
    // Only the sender's own tiers, while it still owns the session (a handoff since
    // leaves the new owner its default), and only tiers there are.
    const sent = row.turn_tiers
    const mine = sent?.by?.kind === row.owner_kind && sent.by.id === row.owner_id && Array.isArray(sent.tiers)
    const turnTiers = mine ? TIERS.filter((t) => (sent.tiers as unknown[]).includes(t)) : null
    return { kind: row.owner_kind, id: row.owner_id, label: row.owner_label, turnTiers, turnId: row.durable_turn_id }
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
  /**
   * Whether the call's approval came from a run grant (responder kind `run_grant`,
   * #2481) now revoked or expired: `approved` is false for it, refused GrantRevoked.
   */
  lapsed?(requestId: string, call?: ApprovalBinding): Promise<boolean>
}

/** A run grant's responder in `ai_input_responses` (#2481): `{kind: 'run_grant', id}`. */
type Responder = { kind?: unknown; id?: unknown }

export class PgApprovalRecords implements ApprovalRecords {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async approved(requestId: string, call?: ApprovalBinding): Promise<boolean> {
    const grant = await this.#approval(requestId, call)
    return grant !== undefined && (grant === null || (await this.#inForce(grant)))
  }

  async lapsed(requestId: string, call?: ApprovalBinding): Promise<boolean> {
    const grant = await this.#approval(requestId, call)
    return typeof grant === 'string' && !(await this.#inForce(grant))
  }

  /** The approval for this call: undefined for none, null for a person's, a grant id for a run grant's. */
  async #approval(requestId: string, call?: ApprovalBinding): Promise<string | null | undefined> {
    // Without the call's hash nothing can be matched, so nothing is approved.
    if (call === undefined) return undefined
    const [row] = await this.#sql<{ responder: Responder }[]>`
      SELECT r.responder FROM ai_input_responses r
      JOIN ai_audit a ON a.request_id = r.request_id AND a.kind = 'approval' AND a.action = 'approved'
      WHERE r.request_id = ${requestId} AND r.kind = 'approval' AND r.outcome = 'approved'
        AND a.input_hash = ${call.inputHash}
      LIMIT 1`
    if (!row) return undefined
    if (row.responder.kind !== 'run_grant') return null
    return typeof row.responder.id === 'string' ? row.responder.id : ''
  }

  /** Whether the run grant is in force; the backend's table, read only for a grant's approval. */
  async #inForce(grantId: string): Promise<boolean> {
    const rows = await this.#sql`
      SELECT 1 FROM flow_grants
      WHERE id = ${grantId} AND status = 'approved' AND revoked_at IS NULL AND now() < expires_at`
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
  /**
   * The waits between a resolving tool's tries to read what it names as an approved call runs
   * (#4442 gate re-review), one more try than waits; RESOLVE_BACKOFF_MS when undefined.
   */
  resolveBackoffMs?: readonly number[]
  /** Tool access (#4757): a package built-in's, which `builtin_tier` answers; every built-in `outward` without it. */
  access?: Pick<AccessResolver, 'builtin'> | undefined
  /**
   * A session turn's own secrets beside `services.secrets` (its remote plugins' and package
   * servers' header values and forwarder URLs, as sessions/turnRunner.ts gathers them), which a
   * ToolCall's tool redacts as the in-process call does (tools/harness.ts `withSecrets`, #2329).
   */
  turnSecrets?: ((session: string, owner: Owner) => Promise<readonly string[]>) | undefined
}

export type ToolActivity = (input: unknown) => Promise<unknown>

/** The failure type of a call that ran and did not succeed: never retried. */
export const TOOL_ERROR = 'ToolError'
/**
 * The failure type of a resolving tool's read that met the backend away or rate-limiting
 * (#4442 gate review): retried, and never taken for the tool's refusal.
 */
export const BACKEND_UNAVAILABLE = 'BackendUnavailable'
/**
 * The waits between an approved call's tries to resolve what it names while the backend is
 * away (#4442 gate re-review): a gated call's activity runs once, so these are its only retries.
 */
export const RESOLVE_BACKOFF_MS: readonly number[] = [500, 2000]
/** The failure type of a call from a workflow that is no known session. */
export const UNKNOWN_SESSION = 'UnknownSession'
/** The failure type of a gated call with no approval recorded for it. */
export const NOT_APPROVED = 'NotApproved'
/** The failure type of a call a run grant approved, revoked or expired before it ran (#2481). */
export const GRANT_REVOKED = 'GrantRevoked'
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
/** Where a flow's call returned an image past the run's FLOW_IMAGES_MAX (#2578). */
export const FLOW_IMAGES_FULL = `[an image this run did not keep: it already keeps ${FLOW_IMAGES_MAX}]`

/** Where a durable activity keeps a result's images: the session's blobs (sessions/blobs.ts). */
export type ResultBlobs = { put(sessionId: string, images: readonly SessionImage[], toolUseId?: string): Promise<void> }

type Block = { type: string; text?: string; data?: unknown; mimeType?: unknown }

/**
 * A result's text with each image named by its bytes (`[image <name>]`), and the images
 * to keep: at most RESULT_IMAGES_MAX, each once. A block that is not one of the four
 * types, or not what it claims, or one past the cap, is UNUSABLE_IMAGE.
 */
/** An image's placeholder in a result's text; flowResult finds a dropped one by it. */
const imagePart = (name: string): string => `[image ${name}]`

function namedImages(content: readonly Block[]): { parts: string[]; images: SessionImage[] } {
  const images: SessionImage[] = []
  const parts = content.map((block) => {
    if (block.type === 'text') return block.text ?? ''
    if (block.type !== 'image') return `[${block.type}]`
    const image = imageOfBlock({ type: 'image', source: { type: 'base64', media_type: block.mimeType, data: block.data } })
    if (!image || images.length >= RESULT_IMAGES_MAX) return UNUSABLE_IMAGE
    if (!images.some((i) => i.name === image.name)) images.push(image)
    return imagePart(image.name)
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
 * keep them every image is FLOW_IMAGE. They are kept as what call `toolUseId` produced,
 * which follow_session names on that call's `tool.result` for the panel (#2478).
 */
export async function resultText(
  content: readonly Block[],
  keep?: { session: string; blobs: ResultBlobs; toolUseId: string },
): Promise<string> {
  if (!keep) {
    return content.map((block) => (block.type === 'text' ? (block.text ?? '') : block.type === 'image' ? FLOW_IMAGE : `[${block.type}]`)).join('\n')
  }
  const { parts, images } = namedImages(content)
  if (images.length) {
    await keep.blobs.put(keep.session, images, keep.toolUseId)
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
 * each image stays FLOW_IMAGE; one the run did not keep (FLOW_IMAGES_MAX) is
 * FLOW_IMAGES_FULL, with no pointer.
 */
export async function flowResult(
  content: readonly Block[],
  keep: { subject: string; blobs: FlowBlobStore } | undefined,
): Promise<string | FlowToolResult> {
  if (!keep) return resultText(content)
  const { parts, images } = namedImages(content)
  if (!images.length) return parts.join('\n')
  const kept = new Set(await keep.blobs.put(keep.subject, images))
  const dropped = new Set(images.filter((i) => !kept.has(i.name)).map((i) => imagePart(i.name)))
  const text = parts.map((part) => (dropped.has(part) ? FLOW_IMAGES_FULL : part)).join('\n')
  const pointers = images.filter((i) => kept.has(i.name)).map(flowImageOf)
  return pointers.length ? { text, images: pointers } : text
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
  return {
    ...Object.fromEntries(tools.map((tool) => [tool.name, (input: unknown) => runAsActivity(tool, input, deps, lookup)])),
    // A flow's tool(...) asks each tool's tier from here before it may run or park (#2704).
    [FLOW_TOOL_TIER_ACTIVITY]: flowToolTierActivity(tools, deps.services),
    // A durable session asks each of ScadBuddy's own tools' tier from here per call (#2171).
    [TOOL_TIER_ACTIVITY]: toolTierActivity(tools, deps.services),
    // And an engine-parked package built-in's access per call (Bash, PowerShell; #4757).
    [BUILTIN_TIER_ACTIVITY]: builtinTier({ access: deps.access }) as ToolActivity,
  }
}

/** The durable session the current activity's workflow is, and its owner; refused otherwise. */
export async function durableSession(
  what: string,
  sessions: SessionOwners,
): Promise<{ session: string; owner: Owner; turnTiers?: readonly Tier[]; turnId?: string }> {
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
  const { turnTiers, turnId, ...who } = owner
  return { session, owner: who, ...(turnTiers ? { turnTiers } : {}), ...(turnId ? { turnId } : {}) }
}

/** Who a call runs as, in which session if any, and the id its gate entry is recorded under. */
type Caller = {
  owner: Owner
  session: string | undefined
  requestId: string
  approvals: ApprovalRecords | undefined
  /** The tiers the call runs with; undefined: the owner's default (`harnessPrincipal`). */
  tiers: readonly Tier[] | undefined
  /** The durable turn the call is part of, for its audit row; none for a flow. */
  turnId?: string | undefined
}

/**
 * A flow run's call: its starter, no session, and the run's decisions as its approvals,
 * each bound to this call's tool and input (#2318): the hash is made from what this call
 * was given, so an approval of the parked call authorises no other tool or input.
 */
async function flowCaller(tool: Tool, flow: string, deps: ToolActivityDeps, toolUseId: string): Promise<Caller> {
  if (tool.name.startsWith('browser_')) {
    throw ApplicationFailure.nonRetryable(`${tool.name} is not for flows: a flow has no session and no browser tab`, NOT_FOR_FLOWS)
  }
  // #2258: without the execution's run id the request id (`flow:<run>:<execution>:<call>`)
  // matches no decision, so the call would be refused NotApproved with no reason given.
  const runId = Context.current().info.workflowExecution?.runId
  if (!runId) {
    throw ApplicationFailure.nonRetryable(`${tool.name}: flow run ${flow}'s call carries no workflow execution run id`, UNKNOWN_FLOW)
  }
  // A lookup that throws (the database is away) is left to the activity's retries.
  const owner = deps.flows ? await deps.flows.startedBy(flow, runId) : undefined
  if (owner === undefined) {
    throw ApplicationFailure.nonRetryable(`${tool.name}: no open flow run ${flow} is recorded for this execution`, UNKNOWN_FLOW)
  }
  // A token starter's run acts with what its token holds now, as its sessions' turns do (#2320).
  const tiers = owner.kind === 'browser' ? undefined : await deps.currentTiers?.(owner)
  const flows = deps.flows
  // The caller's binding is the hash of what this call was given; none approves nothing.
  const approvals = flows && {
    approved: (requestId: string, call?: ApprovalBinding) => flows.approved(requestId, call?.inputHash),
    lapsed: async (requestId: string, call?: ApprovalBinding) => (await flows.lapsed?.(requestId, call?.inputHash)) ?? false,
  }
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
    const { session, owner, turnTiers: sent, turnId } = await durableSession(tool.name, deps.sessions)
    // The call's gate entry, if it parked (spec §6.6): its approver, or its answer, is recorded under this id.
    const requestId = durableRequestId(session, workflowExecution?.runId ?? '', toolUseId)
    caller = { owner, session, requestId, approvals: deps.approvals, tiers: await turnTiers(owner, sent, deps), turnId }
  }
  const { session } = caller
  const run = await runGated(tool, input, deps, lookup, caller, {
    toolUseId,
    flow,
    signal: context.cancellationSignal,
    heartbeat: (details) => context.heartbeat(details),
  })
  if (context.cancellationSignal.aborted) throw new CancelledFailure('the call was cancelled')
  if (run.outcome !== 'ok') throw ApplicationFailure.nonRetryable(failureText(run), TOOL_ERROR)
  if (flow !== undefined) return flowResult(run.result.content, deps.flowBlobs ? { subject: `flow-${flow}`, blobs: deps.flowBlobs } : undefined)
  return resultText(run.result.content, session !== undefined && deps.blobs ? { session, blobs: deps.blobs, toolUseId } : undefined)
}

/** How a gated call is run: its tool_use id, its flow if any, and its activity's signal and heartbeat. */
type GatedRun = {
  toolUseId: string
  flow?: string | undefined
  signal: AbortSignal
  heartbeat: (details?: unknown) => void
  /** The name the call was described under (describe_call's hash); the tool's own by default. */
  describedAs?: string
  /** ToolContext.callKey: the call's Idempotency-Key. */
  callKey?: string
}

/**
 * A call the gate let through, run once: refused (thrown, non-retryable) when its tool is
 * disabled, or gated with no approval recorded for this very call; else run with the turn's
 * tiers, and its audit row written. What it returns is the run, whatever its outcome.
 */
async function runGated(
  tool: Tool,
  input: unknown,
  deps: ToolActivityDeps,
  lookup: (name: string) => Tool | undefined,
  caller: Caller,
  call: GatedRun,
): Promise<ToolRun> {
  const { toolUseId, flow } = call
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
  const refuse = async (detail: string, message: string, type: string, outcome: 'refused' | 'error' = 'refused'): Promise<never> => {
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
      outcome,
      detail,
      startedAt: now,
      finishedAt: now,
    })
    throw ApplicationFailure.nonRetryable(message, type)
  }
  if (effective.disabled) {
    const message = `${tool.name} is disabled in Settings → Tools & plugins; the user can turn it back on there`
    await refuse(message, message, TOOL_ERROR)
  }
  const principal = turnPrincipal(owner, caller.tiers ? { tiers: [...caller.tiers] } : undefined)
  const services = deps.services
  // A tool that resolves what its arguments name (#4442) runs what it resolves now, which
  // must be what the approval was given: a printer re-linked since hashes otherwise. A flow's
  // approval is of the script's raw arguments (the backend hashes those), so a flow's call
  // runs them as sent, resolved only to refuse a pair its registry row no longer links.
  //
  // Its activity runs once (a gated call is never retried), so a backend away or rate-limiting
  // is tried again here, within the one approval: safe, since nothing is checked or sent before
  // the read succeeds, and the approval is then checked against what that read resolved. Still
  // away after the last try, the call fails closed and is audited, and the model is told the
  // approval was spent, so it asks again and a person approves again.
  let runInput = input
  if (effective.gated && tool.resolvesApproval) {
    const ctx = { ...services, principal, ...(session === undefined ? {} : { session }), progress: async () => {}, signal: call.signal, lookup, gate: 'workflow' as const }
    const waits = deps.resolveBackoffMs ?? RESOLVE_BACKOFF_MS
    const unavailable = (tries: number, why: string): Promise<never> =>
      refuse(
        `the backend was unavailable after ${tries} tries: ${why}`,
        `${tool.name} was not run: what it names could not be checked (${why}), so this call's approval is spent; call it again to ask for a new one`,
        BACKEND_UNAVAILABLE,
        'error',
      )
    for (let attempt = 0; ; attempt++) {
      try {
        // Only the input: the line naming the printer was the approval's, so its reads are skipped.
        const resolved = (await tool.prepareApproval(input, ctx, { summary: false })).input
        if (flow === undefined) runInput = resolved
        break
      } catch (err) {
        // A refusal, or an error no retry would change (arguments that do not parse), ends it now.
        if (isRefusal(err) || !(isTransient(err) || call.signal.aborted)) await refuseOrRetry(err, refuse, tool.name)
        const why = err instanceof ToolError ? err.summary : err instanceof Error ? err.message : String(err)
        const wait = waits[attempt]
        if (wait === undefined || call.signal.aborted) await unavailable(attempt + 1, why)
        await sleep(wait, undefined, { signal: call.signal }).catch(() => {})
        // Cancelled while it waited: refused now, never one more read (#4442 review).
        if (call.signal.aborted) await unavailable(attempt + 1, why)
      }
    }
  }
  // The approval binds to this call's tool and input (ApprovalRecords): the hash
  // describe_call gave the parked call, computed again from what this call was given.
  const binding = deps.audit ? { inputHash: deps.audit.hash(call.describedAs ?? tool.name, (runInput ?? {}) as Record<string, unknown>) } : undefined
  if (effective.gated && !(await caller.approvals?.approved(requestId, binding))) {
    if (await caller.approvals?.lapsed?.(requestId, binding)) {
      await refuse(
        "the run's grant was revoked or expired before this call ran",
        `${tool.name}: the run's grant was revoked or expired before this call ran`,
        GRANT_REVOKED,
      )
    }
    await refuse(
      'no approval is recorded for this call',
      `${tool.name} needs an approval, and none is recorded for this call`,
      NOT_APPROVED,
    )
  }
  const heartbeat = setInterval(() => call.heartbeat(), deps.heartbeatMs ?? DEFAULT_HEARTBEAT_MS)
  const startedAt = new Date()
  let run: ToolRun
  try {
    run = await runToolWithOutcome(tool, runInput, {
      ...services,
      // The tab paired with this session (#254), as for a classic turn; a flow has none.
      ...(services.browser && session !== undefined ? { browser: services.browser.forSession(session) } : {}),
      // A flow's own images, which its calls may name as `flow_image` references (#2477).
      ...(flow !== undefined && deps.flowBlobs ? { flowImages: flowImagesOf(deps.flowBlobs, `flow-${flow}`) } : {}),
      // The run and this call, for a turn it sends (#2481): from the context, never the input.
      ...(flow !== undefined ? { flowTurn: { run_id: flow, call_id: toolUseId } } : {}),
      principal,
      ...(session === undefined ? {} : { session }),
      ...(call.callKey === undefined ? {} : { toolUseId, callKey: call.callKey }),
      progress: async (progress, total, message) => call.heartbeat({ progress, total, message }),
      signal: call.signal,
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
    turnId: caller.turnId ?? null,
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
  return run
}

/** A released turn's approved call (unified runtime spec §5.4 step 5), as its continuation names it. */
export type DecidedCall = {
  sessionId: string
  requestId: string
  toolUseId: string
  /** The name it parked under: `mcp__scadbuddy__<tool>`. */
  tool: string
  input: unknown
  signal: AbortSignal
  heartbeat: () => void
}

/** How it went: run (`ok` or not) with what the model reads of it, or refused unrun, and why. */
export type DecidedRun = { ran: true; ok: boolean; text: string } | { ran: false; text: string }

const HARNESS_PREFIX = 'mcp__scadbuddy__'

/**
 * Runs a released turn's approved call once, outside the model, through the same gated path as
 * a durable session's tool activity: the approval is checked against this call's request id and
 * the hash describe_call gave it, the tool's Settings state is read as it runs, the turn's tiers
 * apply, and its audit row is written. Its Idempotency-Key is the request id (spec §7), so a
 * placement retried after the call ran is answered from the backend's operation record. A call
 * to anything but a ScadBuddy tool (a plugin's, http_request) cannot be run here and says so.
 */
export function decidedCalls(tools: readonly Tool[], deps: ToolActivityDeps): (call: DecidedCall) => Promise<DecidedRun> {
  const byName = new Map(tools.map((t) => [t.name, t]))
  const lookup = (name: string): Tool | undefined => byName.get(name)
  return async (call) => {
    const tool = call.tool.startsWith(HARNESS_PREFIX) ? lookup(call.tool.slice(HARNESS_PREFIX.length)) : undefined
    if (!tool) {
      return { ran: false, text: `${call.tool} cannot be run outside the conversation, so it was not run` }
    }
    // The approval is looked up by request id, so the id must be this session's entry for this
    // call (gate/ids.ts durableRequestId), as runAsActivity derives it: another session's approval
    // of the same tool and input authorises nothing here.
    const prefix = `durable:${call.sessionId.toLowerCase()}:`
    const runId = call.requestId.startsWith(prefix) ? call.requestId.slice(prefix.length, -(call.toolUseId.length + 1)) : ''
    if (!runId || runId.includes(':') || call.requestId !== durableRequestId(call.sessionId, runId, call.toolUseId)) {
      return { ran: false, text: `request ${call.requestId} is not session ${call.sessionId}'s entry for call ${call.toolUseId}, so ${call.tool} was not run` }
    }
    const owner = await deps.sessions.ownerOf(call.sessionId)
    if (owner === undefined) return { ran: false, text: `session ${call.sessionId} is no durable session, so ${call.tool} was not run` }
    const { turnTiers: sent, turnId, ...who } = owner
    const caller: Caller = {
      owner: who,
      session: call.sessionId,
      requestId: call.requestId,
      approvals: deps.approvals,
      tiers: await turnTiers(who, sent ?? undefined, deps),
      turnId: turnId ?? undefined,
    }
    let run: ToolRun
    try {
      run = await runGated(tool, call.input, deps, lookup, caller, {
        toolUseId: call.toolUseId,
        signal: call.signal,
        heartbeat: call.heartbeat,
        describedAs: call.tool,
        callKey: call.requestId,
      })
    } catch (err) {
      if (err instanceof ApplicationFailure && err.nonRetryable) return { ran: false, text: err.message }
      throw err
    }
    if (run.outcome !== 'ok') return { ran: true, ok: false, text: failureText(run) }
    const keep = deps.blobs ? { session: call.sessionId, blobs: deps.blobs, toolUseId: call.toolUseId } : undefined
    return { ran: true, ok: true, text: await resultText(run.result.content, keep) }
  }
}

// A session turn's call as a workflow of its own (toolCall.ts): `session-<uuid>-call-<tool_use_id>`.
const TOOL_CALL_WORKFLOW_ID = /^session-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-call-([A-Za-z0-9_-]+)$/

/** The session and tool_use id a ToolCall workflow id names, or undefined for any other workflow. */
export function toolCallOf(workflowId: string | undefined): { session: string; toolUseId: string } | undefined {
  const m = workflowId === undefined ? null : TOOL_CALL_WORKFLOW_ID.exec(workflowId)
  return m ? { session: m[1]!, toolUseId: m[2]! } : undefined
}

/** A result's blocks as a ToolCall returns them: each image kept in the session's blobs and named, never its bytes. */
export async function callBlocks(content: readonly Block[], keep: { session: string; blobs: ResultBlobs | undefined; toolUseId: string }): Promise<ToolCallBlock[]> {
  const images: SessionImage[] = []
  const blocks: ToolCallBlock[] = content.map((block) => {
    if (block.type === 'text') return { type: 'text', text: block.text ?? '' }
    if (block.type !== 'image') return { type: 'text', text: `[${block.type}]` }
    const image = keep.blobs ? imageOfBlock({ type: 'image', source: { type: 'base64', media_type: block.mimeType, data: block.data } }) : undefined
    if (!image || images.length >= RESULT_IMAGES_MAX) return { type: 'text', text: keep.blobs ? UNUSABLE_IMAGE : FLOW_IMAGE }
    if (!images.some((i) => i.name === image.name)) images.push(image)
    return { type: 'image', name: image.name, mimeType: image.mediaType }
  })
  if (images.length) await keep.blobs!.put(keep.session, images, keep.toolUseId)
  return blocks
}

/**
 * Each registry tool as `tool:<name>`, the one activity of a session turn's ToolCall workflow
 * (plan 2026-10-13 Step 9.2). The session and the call are the workflow's id, never its input.
 * The call runs as decidedCalls runs a released one: gated as the tool is now, with the turn's
 * tiers, and an outward call only with an approval recorded for its own entry (`requestId`, which
 * must be this session's entry for this call) and bound to this input, keyed by that entry; any
 * other call is keyed `call:<session>:<tool_use_id>`, as an in-process call is (projections.ts
 * `callIds`). Its audit row is the turn's (audit/turn.ts sees every call's result), so none is
 * written here. A refusal or a failed run is a result the model reads, never a failed workflow.
 */
export function toolCallActivities(tools: readonly Tool[], deps: ToolActivityDeps): Record<string, ToolActivity> {
  const byName = new Map(tools.map((t) => [t.name, t]))
  const lookup = (name: string): Tool | undefined => byName.get(name)
  const audit = deps.audit && { hash: deps.audit.hash.bind(deps.audit), summarise: deps.audit.summarise.bind(deps.audit), record: async () => {} }
  const quiet: ToolActivityDeps = { ...deps, audit }
  const refused = (text: string): ToolCallResult => ({ ok: false, content: [{ type: 'text', text }] })
  return Object.fromEntries(
    tools.map((tool) => [
      toolCallActivity(tool.name),
      async (raw: unknown): Promise<ToolCallResult> => {
        const context = Context.current()
        const call = toolCallOf(context.info.workflowExecution?.workflowId)
        if (call === undefined) {
          throw ApplicationFailure.nonRetryable(
            `${toolCallActivity(tool.name)} runs only for a session's ToolCall workflow; ${context.info.workflowExecution?.workflowId ?? 'this workflow'} is none`,
            UNKNOWN_SESSION,
          )
        }
        const { session, toolUseId } = call
        const args = (raw ?? {}) as Partial<ToolCallArgs>
        const owner = await deps.sessions.ownerOf(session)
        if (owner === undefined) return refused(`session ${session} is gone, so ${tool.name} was not run`)
        const requestId = typeof args.requestId === 'string' ? args.requestId : undefined
        // Only this session's entry for this very call (gate/ids.ts): another's approval authorises nothing here.
        if (requestId !== undefined) {
          const parsed = parseRequestId(requestId)
          if (parsed?.store !== 'durable' || parsed.sessionId !== session || parsed.toolUseId !== toolUseId) {
            return refused(`request ${requestId} is not session ${session}'s entry for call ${toolUseId}, so ${tool.name} was not run`)
          }
        }
        const { turnTiers: sent, turnId, ...who } = owner
        const callKey = requestId ?? `call:${session}:${toolUseId}`
        // Redacted of what the turn redacts in-process: its plugin and package tokens too.
        const base = deps.services.secrets
        const turnSecrets = deps.turnSecrets
        const services = turnSecrets ? { ...deps.services, secrets: async () => [...((await base?.()) ?? []), ...(await turnSecrets(session, who))] } : deps.services
        const caller: Caller = {
          owner: who,
          session,
          requestId: callKey,
          approvals: deps.approvals,
          tiers: await turnTiers(who, sent ?? undefined, deps),
          turnId: turnId ?? undefined,
        }
        let run: ToolRun
        try {
          // Under the call's span in the turn's trace, as an in-process call's backend requests are (spec 2026-10-01 §5.4).
          run = await withSpan(
            `agent.tool/${tool.name}`,
            { parent: contextFrom(args.traceparent, otelContext.active()), attributes: { 'scadbuddy.tool': tool.name, 'scadbuddy.tier': tool.risk } },
            async (span) => {
              const done = await runGated(tool, args.input, { ...quiet, services }, lookup, caller, {
                toolUseId,
                signal: context.cancellationSignal,
                heartbeat: (details) => context.heartbeat(details),
                describedAs: `${HARNESS_PREFIX}${tool.name}`,
                callKey,
              })
              span.setAttribute('scadbuddy.outcome', done.outcome)
              if (done.outcome === 'error') {
                span.setStatus({ code: SpanStatusCode.ERROR })
                span.setAttribute('scadbuddy.failure_class', 'ToolError')
              }
              return done
            },
          )
        } catch (err) {
          if (err instanceof ApplicationFailure && err.nonRetryable) return refused(err.message)
          throw err
        }
        if (context.cancellationSignal.aborted) throw new CancelledFailure('the call was cancelled')
        return { ok: run.outcome === 'ok', content: await callBlocks(run.result.content, { session, blobs: deps.blobs, toolUseId }) }
      },
    ]),
  )
}

/**
 * A resolving tool's failure (#4442 gate review): its own refusal refuses the call, its
 * backend text in the untrusted-data envelope; the backend away, rate-limiting or out of
 * reach is thrown to be retried; anything else (arguments that do not parse) is refused
 * too, saying what went wrong, since no retry would change it.
 */
async function refuseOrRetry(
  err: unknown,
  refuse: (detail: string, message: string, type: string) => Promise<never>,
  tool: string,
): Promise<never> {
  if (err instanceof CancelledFailure) throw err
  if (isRefusal(err)) return refuse(err.summary, `${tool} was not run: ${toolErrorText(err, tool)}`, TOOL_ERROR)
  if (isTransient(err)) throw ApplicationFailure.retryable(err instanceof ToolError ? err.summary : (err as Error).message, BACKEND_UNAVAILABLE)
  const { text, detail } = unexpectedErrorText(err, tool)
  return refuse(detail, `${tool} was not run: ${text}`, TOOL_ERROR)
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
  deps: Pick<ToolActivityDeps, 'audit' | 'sessions' | 'currentTiers'> & {
    workspace?: SessionFiles | undefined
    /**
     * The tools whose own `prepareApproval` decides what a call parks (#4442), and the
     * services it reads with, as the session's owner. Without them, every call parks as sent.
     */
    tools?: readonly Tool[]
    services?: ToolServices
  },
): Record<string, ToolActivity> {
  const byName = new Map((deps.tools ?? []).map((t) => [t.name, t]))
  const lookup = (name: string): Tool | undefined => byName.get(name)
  return {
    [DESCRIBE_CALL_ACTIVITY]: async (args: unknown) => {
      // The hash is keyed by the approvals' HMAC key: only a durable session's workflow
      // gets one (security review of 5b).
      const { session, owner, turnTiers: sent } = await durableSession(DESCRIBE_CALL_ACTIVITY, deps.sessions)
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (!deps.audit) throw ApplicationFailure.nonRetryable('describing a call needs the audit log and its key', TOOL_ERROR)
      if (typeof tool !== 'string' || typeof input !== 'object' || input === null || Array.isArray(input)) {
        throw ApplicationFailure.nonRetryable('describe_call takes {tool: string, input: object}', TOOL_ERROR)
      }
      const fields = input as Record<string, unknown>
      // A tool that resolves what its arguments name (#4442: print_output's registry printer)
      // parks the resolved input, by the line that names it; the call's activity resolves it
      // again and runs only if it hashes the same (runGated). A ToolError refuses it unparked.
      const own = lookup(tool.startsWith(HARNESS_PREFIX) ? tool.slice(HARNESS_PREFIX.length) : tool)
      const services = deps.services
      const audit = deps.audit
      // Resolved only for a call that could run: the tool enabled, and its tier one the owner holds now.
      const effective = own && services ? await effectiveTool(own, services) : undefined
      const tiers = await turnTiers(owner, sent, deps)
      const principal = turnPrincipal(owner, tiers ? { tiers: [...tiers] } : undefined)
      if (own?.resolvesApproval && services && effective && !effective.disabled && principal.tiers.includes(effective.risk)) {
        const { request_id: requestId } = args as { request_id?: unknown }
        const refuse = async (detail: string, message: string, type: string): Promise<never> => {
          const parsed = parsedOrRaw(own, fields)
          const now = new Date()
          await audit.record({
            kind: 'tool_call',
            action: own.name,
            surface: 'harness',
            actor: owner,
            sessionId: session,
            ...(typeof requestId === 'string' ? { requestId } : {}),
            tier: effective.risk,
            inputHash: audit.hash(own.name, parsed),
            inputSummary: audit.summarise(own.name, parsed),
            outcome: 'refused',
            detail,
            startedAt: now,
            finishedAt: now,
          })
          throw ApplicationFailure.nonRetryable(message, type)
        }
        let prepared: { input: Record<string, unknown>; summary: string }
        try {
          const ctx = { ...services, principal, session, progress: async () => {}, signal: Context.current().cancellationSignal, lookup, gate: 'workflow' as const }
          prepared = await own.prepareApproval(fields, ctx)
        } catch (err) {
          return refuseOrRetry(err, refuse, own.name)
        }
        const secrets = (await services.secrets?.()) ?? []
        const shown = await withWorkspaceSize(deps.workspace, { kind: 'session', sessionId: session }, prepared.input)
        return {
          summary: withApprovalLine(prepared.summary, audit.summarise(tool, shown, secrets), secrets),
          input_hash: audit.hash(tool, prepared.input),
        }
      }
      // A workspace upload's size is shown (#2396), from the session's own workspace; the hash is of the call's input.
      const shown = await withWorkspaceSize(deps.workspace, { kind: 'session', sessionId: session }, fields)
      return { summary: deps.audit.summarise(tool, shown), input_hash: deps.audit.hash(tool, fields) }
    },
  }
}
