import { createHash, randomUUID } from 'node:crypto'
import type { AttachmentStore } from '../attachments/store.js'
import type { StoredBlob } from '../sessions/blobs.js'
import type { CallToolResult, ToolAnnotations } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import type { paths } from '../api/schema.js'
import { hasTier, type Principal, type Tier } from '../auth/principal.js'
import type { BrowserTabs } from '../bridge/hub.js'
import type { SessionManager } from '../sessions/manager.js'
import type { TouchedSink } from '../sessions/touched.js'
import { type SessionFiles, workspaceOf } from '../workspace/store.js'
import { DEFAULT_SOURCE, markUntrusted, wrapUntrustedText } from '../safety/untrusted.js'
import { authored, authorHeaders } from './authorship.js'
import { type OutwardActions, PendingStoreFullError } from './pending.js'
import type { RenderLimiter } from './renderLimits.js'

// The tool registry, spec §5.1 and D3
// (docs/superpowers/specs/2026-09-27-ai-integration-design.md): every tool is
// defined once, here, and projected twice — in-process to the harness
// (`harness.ts`) and over `/mcp` to external agents (`../mcp/server.ts`).
// Both projections call `runTool`, so the tier check and the approval gate
// cannot differ between them.

export type Risk = Tier

/** Bambuddy API-key scopes, as `backend/scadbuddy/bambuddy/errors.py` `Scope` names them. */
export type BambuddyScope = 'Read Status' | 'Manage Library' | 'Manage Queue' | 'Manage Projects' | 'Manage Archives'

type HttpMethod = 'get' | 'put' | 'post' | 'delete' | 'patch'
type MethodsOf<P extends keyof paths> = {
  [M in HttpMethod]: NonNullable<paths[P][M]> extends never ? never : Uppercase<M>
}[HttpMethod]

/** `"GET /api/v1/models"`: a backend operation, checked against the generated schema. */
export type Operation = { [P in keyof paths]: `${MethodsOf<P>} ${P & string}` }[keyof paths]

/** Long-running tools report here; a no-op when the caller sent no progress token. */
export type Progress = (progress: number, total?: number, message?: string) => Promise<void>

/** How a browser_* call's wait for its tab ended (ToolServices `waitForTab`). */
export type TabWait =
  /** `why`: the hub saw the session's tab again, or the user said they are back (the tab may still not be here). */
  | { back: true; why: 'reconnected' | 'user_back' }
  | { back: false; message: string }
/**
 * `signal`: the call's own (it stops waiting, the others sharing the wait do not).
 * `isBack`: whether the call's tab is connected now, checked once the wait is
 * recorded, so a tab that came back in between is not waited for; `signal`
 * aborts once the wait no longer needs the answer, and the check stops then.
 */
export type WaitForTab = (request: {
  tool: string
  toolUseId: string | undefined
  signal: AbortSignal
  isBack: (signal: AbortSignal) => Promise<boolean>
}) => Promise<TabWait>

/** Shared by every call: what `main.ts` (or a test) wires up once. */
export type ToolServices = {
  backend: BackendClient
  /**
   * Where a gated outward call is prepared and later confirmed (spec §8.2):
   * `ai_approvals` through approvals/mcp.ts when there is a database, else
   * the in-memory store in pending.ts, whose actions can never be confirmed.
   */
  pending: OutwardActions
  /** How often a render is polled while `render_model` waits. */
  pollIntervalMs: number
  /** How long `render_model` waits before handing back the still-running job. */
  renderWaitMs: number
  /** How long a command follows a 202 (tools/command.ts); `COMMAND_FOLLOW_MS` when unset. */
  commandFollowMs?: number
  /** How long a command follows a 202 `accepting` (tools/command.ts); `ACCEPTING_MS` when unset. */
  acceptingMs?: number
  /** How long a call may run past its own waits before it is answered as timed out; `TOOL_DEADLINE_MS` when unset. */
  toolDeadlineMs?: number
  /** Binary results above this are returned as a link, not inline (binary.ts; 8 MiB by default). */
  maxInlineBytes?: number
  /** SCADBUDDY_PUBLIC_URL, so a link to a backend route can be absolute. */
  publicBaseUrl?: string | undefined
  /** Bounds the renders each principal starts (renderLimits.ts, #252); the process default when unset. */
  renderLimiter?: RenderLimiter
  /** The sessions the `sessions_*` tools act on (tools/sessions.ts, #300); none without a database. */
  sessions?: SessionManager | undefined
  /** Uploaded images `sessions_send` may name (#1941, attachments/store.ts); none without a database. */
  attachments?: AttachmentStore | undefined
  /**
   * The assistant's workspace (#2329, workspace/store.ts): the session's files, or
   * the principal's own outside a session. The workspace_* tools refuse without it.
   */
  workspace?: SessionFiles | undefined
  /**
   * The secrets a tool must never hand the model (#2329): every saved Claude
   * credential's (main.ts), and in a classic turn the turn's plugin tokens too
   * (tools/harness.ts). workspace_read redacts them from text, as the event log
   * is redacted of them (sessions/sdkEvents.ts `scrubForLog`).
   */
  secrets?: (() => Promise<readonly string[]>) | undefined
  /** The tabs the browser_* tools drive (bridge/hub.ts, #254); without it they answer "no browser attached". */
  browser?: BrowserTabs | undefined
  /**
   * #815 §2: parks a browser_* call that found no tab until the session's tab is
   * back, the user replies, or the wait times out. Only a turn of a session the
   * browser user owns has one (sessions/manager.ts); without it the call fails at once.
   */
  waitForTab?: WaitForTab | undefined
  /** Where a session's calls that ran (succeeded or failed) are reported, for what it touched (sessions/touched.ts, #931). */
  touched?: TouchedSink | undefined
  /**
   * Settings → Plugins' overrides of these tools (#1953, plugins/builtInTools.ts):
   * a raised or lowered tier or a disabled tool, from `ai_access` (#2171), read at each call. Every tool runs at its
   * own tier without it.
   */
  toolOverrides?: ToolOverrides | undefined
}

/** One tool's override: a tier other than its own (raised or lowered, #2171), and whether it is disabled. */
export type ToolOverride = { tier?: Risk; disabled: boolean }

export type ToolOverrides = {
  of(tool: string): Promise<ToolOverride>
  /** Every disabled tool's name, for a tool list (/mcp). */
  disabled(): Promise<ReadonlySet<string>>
}

/** What a tool runs at now: its override's tier or its own, whether that gates it, and whether it is disabled. */
export type EffectiveTool = { risk: Risk; gated: boolean; disabled: boolean }

/**
 * A tool's tier and gate with Settings' override applied (#1953, #2171). An
 * override moves the tier either way: a tool raised to `outward` is gated like
 * any outward tool (`approval: 'none'` tools are outward already, so this never
 * gates them), and an outward tool lowered below it is not gated at all.
 */
export async function effectiveTool(tool: Tool, services: Pick<ToolServices, 'toolOverrides'>): Promise<EffectiveTool> {
  const override = await services.toolOverrides?.of(tool.name)
  const risk = override?.tier ?? tool.risk
  return { risk, gated: risk === 'outward' && (tool.gated || tool.risk !== 'outward'), disabled: override?.disabled === true }
}

/** A flow run's own images (#2477, temporal/flowBlobs.ts): what a `flow_image` reference names. */
export type FlowImages = { get(name: string): Promise<StoredBlob | undefined> }

/** A flow run's step that sends a session's turn (#2481; agent-durable models.py `FlowTurn`). */
export type FlowTurn = { run_id: string; call_id: string }

/**
 * The Idempotency-Key a call's command sends: a digest of its `callKey` (64 hex characters, which
 * the backend's follow URL can carry, operations.py `_URL_SAFE_KEY`; a request id's colons it
 * cannot), else a fresh one. A call that sends several commands sends one key: the backend keys
 * an operation by its kind, subject and body as well (`operation_key`).
 */
export function idempotencyKey(ctx: Pick<ToolContext, 'callKey'>): string {
  return ctx.callKey ? createHash('sha256').update(ctx.callKey).digest('hex') : randomUUID().replaceAll('-', '')
}

export type ToolContext = ToolServices & {
  principal: Principal
  /** In a flow run's call only: that run's images, by name. */
  flowImages?: FlowImages | undefined
  /**
   * In a flow run's call only (#2481): the run and the call, from the activity's own
   * context (`flow-<run>`, `tool-<call id>`), never from its input. A turn the call
   * sends (sessions_start, sessions_send) carries it, so the run's grant may cover it.
   */
  flowTurn?: FlowTurn | undefined
  /** The assistant session a harness call runs in (#252: its commits name it; authorship.ts). */
  session?: string | undefined
  /** The harness call's tool_use id, when Claude Code sent one (`_meta`, projections.ts). */
  toolUseId?: string | undefined
  /**
   * One model call's key, the same on every host path that runs it (unified runtime spec
   * 2026-10-13 §7): `call:<session>:<tool_use id>` from the harness server, or the gate's
   * request id for a call a released turn runs once it was approved. A command the call sends
   * carries it as its Idempotency-Key (`idempotencyKey`), so a second run of the same call is
   * answered from the backend's operation record. Absent, each call gets a fresh key.
   */
  callKey?: string | undefined
  progress: Progress
  signal: AbortSignal
  /** The projection's own tools by name, so `confirm_action` can run the approved one. */
  lookup?: (name: string) => Tool | undefined
  /**
   * `harness`: the call came through the harness's permission seam
   * (harness/permissions.ts), which runs before any tool and parks every
   * outward call until a human approves it (or denies it when there is no
   * approval gate), so a call that reaches here was approved and is not
   * prepared a second time. Only the harness projection sets it
   * (projections.ts `createHarnessServer`); the in-process server is reachable
   * only from a harness query.
   *
   * `workflow`: the call is an activity on `agent-tools` (temporal/toolActivities.ts,
   * #1055), which a durable session's workflow schedules only once its own approval
   * (`needs_approval`, spec 2026-10-01 §6.4) has passed. Not prepared again either.
   */
  gate?: 'harness' | 'workflow'
  /**
   * Under `harness`: the tier the turn's permission seam applied to this call. The
   * seam reads each call's access (#2171, access/live.ts) and records the tier it
   * applied (harness/seamLedger.ts); without a record this is the tier the turn read
   * at its start. A tool raised to `outward` after the seam let it through was not
   * parked, and is refused here rather than run unasked (#2100).
   */
  seamTier?: Risk
  /**
   * The tier and Settings state the caller already resolved for this call and checked
   * it against (toolActivities.ts, #2088). Used as given, never read again, so the
   * check and the run cannot see two different overrides.
   */
  effective?: EffectiveTool
  /**
   * What the handler itself knows about how the call went, for the audit row
   * (`ToolRun`): `confirm_action` reports the approval it ran on and the tool
   * it ran, and that a claim answered pending or was refused. Set by
   * `runToolWithOutcome`; a handler that never calls it is judged by its
   * result alone.
   */
  report?: (report: RunReport) => void
}

/** A handler's own account of its run (ToolContext.report), merged into its ToolRun. */
export type RunReport = {
  /** Overrides the outcome derived from the result (a pending claim answers a plain result, but nothing ran). */
  outcome?: ToolOutcome
  detail?: string
  /** The approval the call ran on (ai_approvals id), so the row names who approved it. */
  approvalId?: string
  /** The tool that actually ran, with its parsed input, when it is not the tool called (confirm_action). */
  ran?: { tool: string; input: Record<string, unknown> }
}

export type ToolSpec<S extends z.ZodRawShape> = {
  name: string
  description: string
  input: z.ZodObject<S>
  risk: Risk
  /** Bambuddy scopes the backend call needs, so a 401/403 can be explained before it happens. */
  bambuddyScope?: readonly BambuddyScope[]
  /** → `readOnlyHint`, for batching only; never used for gating (spec §5.1). Defaults to `risk === 'read'`. */
  readOnly?: boolean
  /** The backend operations this tool covers, for the openapi coverage check. */
  routes: readonly Operation[]
  /**
   * `outward` tools stop at the approval gate by default. Only the gate's own
   * tool (`confirm_action`) opts out, because it IS the approval path.
   */
  approval?: 'required' | 'none'
  /** A human-readable line for the pending action a gated call creates. */
  summarize?: (args: z.infer<z.ZodObject<S>>) => string
  /**
   * What a gated call parks for approval, when that needs a read (#4442): the input a
   * person approves, which may name what the arguments resolve to (a registry printer's
   * Bambuddy id), and the line that says it. confirm_action must send that input, so
   * what runs is what was approved. It may refuse the call, before anything is parked.
   * Without it, the call's parsed arguments and `summarize`.
   */
  prepareApproval?: (
    args: z.infer<z.ZodObject<S>>,
    ctx: ToolContext,
    needs: ApprovalNeeds,
  ) => Promise<{ input: z.infer<z.ZodObject<S>>; summary: string }>
  /**
   * What a call does, in a few words, for the assistant panel (#782): worded as
   * the action ("Render cable-clip"), since the panel shows whether it is
   * running or done beside it. Defaults to the name in words, with the `slug`
   * argument when there is one (`defaultTitle`). Shown to everyone watching
   * the session, so never put a secret argument in it.
   */
  title?: (args: z.infer<z.ZodObject<S>>) => string
  /**
   * Where the content this tool returns comes from, for the untrusted-data
   * envelope every text result is wrapped in (safety/untrusted.ts, #258).
   * Say who could have written it, e.g. "the model's README, written by its
   * author or imported from the web". Defaults to DEFAULT_SOURCE.
   */
  source?: string
  /**
   * The longest this call waits by design (a render, the user, a tab, another
   * session's turn), on top of which `TOOL_DEADLINE_MS` is allowed before the
   * call is answered as timed out (#1918). `Infinity` for a wait with no bound
   * of its own (ask_user). None for a tool that only asks and answers.
   */
  waitsMs?: (args: z.infer<z.ZodObject<S>>, ctx: ToolContext) => number
  handler: (args: z.infer<z.ZodObject<S>>, ctx: ToolContext) => Promise<CallToolResult>
}

/**
 * What a prepareApproval caller needs besides the input (#4442 review): `summary: false` when
 * only the input is wanted (a durable call's hash check as it runs), so the tool skips reads
 * made only to word the line a person sees; it still returns a line, worded without them.
 */
export type ApprovalNeeds = { summary: boolean }

/** A registry entry with its argument types erased, so tools of any shape share one list. */
export type Tool = {
  readonly name: string
  readonly description: string
  readonly shape: z.ZodRawShape
  readonly risk: Risk
  readonly bambuddyScope: readonly BambuddyScope[]
  readonly readOnly: boolean
  readonly routes: readonly Operation[]
  readonly gated: boolean
  readonly annotations: ToolAnnotations
  /** Where its content comes from (ToolSpec.source). */
  readonly source: string
  summarize(args: unknown): string
  /** What a gated call parks (ToolSpec.prepareApproval, else its parsed arguments and `summarize`). */
  prepareApproval(args: unknown, ctx: ToolContext, needs?: ApprovalNeeds): Promise<{ input: Record<string, unknown>; summary: string }>
  /** Whether it has its own `prepareApproval`: the session gates resolve only those (#4442). */
  readonly resolvesApproval: boolean
  /** The call's title for the panel (ToolSpec.title); `defaultTitle` when the arguments do not parse. */
  title(args: unknown): string
  /** The arguments as the handler would see them (defaults applied): what an approval's input hash covers. */
  parse(args: unknown): Record<string, unknown>
  /** Parses `args` and runs the handler, with no tier check or gate: call `runTool` instead. */
  execute(args: unknown, ctx: ToolContext): Promise<CallToolResult>
  /** How long the call may run before `runTool` answers it as timed out (ToolSpec.waitsMs). */
  deadlineMs(args: unknown, ctx: ToolContext): number
}

/**
 * How long a call may run past its own waits (ToolSpec.waitsMs) before it is
 * answered as timed out (#1918). Most backend calls carry only the call's
 * abort signal, so a backend that never answers would otherwise hold the turn
 * until the user presses Stop. Generous: a command route alone may wait
 * `ACCEPTING_MS` (4 min) for the backend to accept it (tools/command.ts).
 */
export const TOOL_DEADLINE_MS = 5 * 60_000

/**
 * A tool's commands all send the call's one Idempotency-Key (`idempotencyKey`): two commands of
 * one call with the same kind, subject and body are one operation, and the second gets the
 * first's record back. A tool that means to run the same command twice in a call sends
 * something in its body that tells them apart.
 */
export function defineTool<S extends z.ZodRawShape>(spec: ToolSpec<S>): Tool {
  const readOnly = spec.readOnly ?? spec.risk === 'read'
  const gated = spec.risk === 'outward' && spec.approval !== 'none'
  const bambuddyScope = spec.bambuddyScope ?? []
  return {
    name: spec.name,
    description: spec.description,
    shape: spec.input.shape,
    risk: spec.risk,
    bambuddyScope,
    readOnly,
    routes: spec.routes,
    gated,
    source: spec.source ?? DEFAULT_SOURCE,
    annotations: {
      readOnlyHint: readOnly,
      destructiveHint: spec.risk === 'outward',
      // Bambuddy is a system outside ScadBuddy.
      openWorldHint: bambuddyScope.length > 0,
    },
    summarize(args) {
      const parsed = spec.input.parse(args)
      return spec.summarize ? spec.summarize(parsed) : `${spec.name} ${JSON.stringify(parsed)}`
    },
    resolvesApproval: spec.prepareApproval !== undefined,
    async prepareApproval(args, ctx, needs = { summary: true }) {
      const parsed = spec.input.parse(args)
      if (!spec.prepareApproval) return { input: parsed, summary: this.summarize(args) }
      const prepared = await spec.prepareApproval(parsed, ctx, needs)
      const input = spec.input.parse(prepared.input)
      // It may fill what the call left out, never change what the model chose (#4442 gate review).
      const resolved: Record<string, unknown> = input
      const changed = Object.entries(parsed).find(([key, value]) => value !== undefined && JSON.stringify(resolved[key]) !== JSON.stringify(value))
      // A bug in the tool, never the backend away: a refusal (no status), so it ends at once (#4442 N2).
      if (changed) throw new ToolError(`${spec.name}'s prepareApproval changed the supplied argument ${changed[0]}`)
      return { input, summary: prepared.summary }
    },
    title(args) {
      const parsed = spec.input.safeParse(args)
      if (!parsed.success) return defaultTitle(spec.name, args)
      return spec.title ? spec.title(parsed.data) : defaultTitle(spec.name, parsed.data)
    },
    parse(args) {
      return spec.input.parse(args) as Record<string, unknown>
    },
    execute(args, ctx) {
      return spec.handler(spec.input.parse(args), ctx)
    },
    deadlineMs(args, ctx) {
      const parsed = spec.waitsMs ? spec.input.safeParse(args) : undefined
      const waits = parsed?.success ? spec.waitsMs!(parsed.data, ctx) : 0
      return waits + (ctx.toolDeadlineMs ?? TOOL_DEADLINE_MS)
    },
  }
}

/**
 * A tool's title when it declares none (#782): its name in words, and the model
 * it acts on when it takes a `slug`: `get_readme {slug: 'cable-clip'}` →
 * "Get readme → cable-clip".
 */
export function defaultTitle(name: string, args: unknown): string {
  const words = name.replace(/_/g, ' ')
  const phrase = words.charAt(0).toUpperCase() + words.slice(1)
  const slug = typeof args === 'object' && args !== null ? (args as { slug?: unknown }).slug : undefined
  return typeof slug === 'string' && slug !== '' ? `${phrase} → ${slug}` : phrase
}

export class ToolError extends Error {
  override name = 'ToolError'
  /** The backend's HTTP status, when the error is a backend answer (call.ts `ok`). */
  readonly status: number | undefined
  /**
   * Text ScadBuddy did not write (the backend's problem `detail`, which can
   * relay Bambuddy's own message): the model sees it inside the untrusted-data
   * envelope, after the bare `message` (#258).
   */
  readonly untrusted: string | undefined
  /** The ScadBuddy-authored part of the message. */
  readonly summary: string

  constructor(message: string, status?: number, untrusted?: string) {
    super(untrusted ? `${message}: ${untrusted}` : message)
    this.status = status
    this.untrusted = untrusted
    this.summary = message
  }
}

/**
 * Whether an error is the tool's own refusal (#4442 gate review): a ToolError that is not
 * the backend away (5xx) or rate-limiting (429). Those, and a network error, are worth
 * trying again, so a gate leaves them to its retries rather than refusing the call.
 */
export function isRefusal(err: unknown): err is ToolError {
  return err instanceof ToolError && !(err.status !== undefined && (err.status >= 500 || err.status === 429))
}

/** A socket's codes for a backend it could not reach, on undici's `fetch failed` cause. */
const UNREACHED = new Set(['ECONNREFUSED', 'ECONNRESET', 'ENOTFOUND', 'EAI_AGAIN', 'EADDRNOTAVAIL', 'ETIMEDOUT', 'EPIPE', 'UND_ERR_SOCKET', 'UND_ERR_CONNECT_TIMEOUT'])

/**
 * Whether an error is worth another try (#4442 review): the backend away (5xx) or
 * rate-limiting (429), or a request that never reached it (fetch's network failure, a
 * timeout or abort). Anything else, a ToolError refusal or an error in the tool itself
 * (its arguments not parsing), will fail the same way again.
 */
export function isTransient(err: unknown): boolean {
  if (err instanceof ToolError) return !isRefusal(err)
  if (!(err instanceof Error)) return false
  if (err.name === 'AbortError' || err.name === 'TimeoutError') return true
  // Node's fetch (undici) says `fetch failed`; the WHATWG wording, msw's too, `Failed to fetch`.
  if (err instanceof TypeError && (err.message === 'fetch failed' || err.message === 'Failed to fetch')) return true
  const code = (err.cause as { code?: unknown } | undefined)?.code ?? (err as { code?: unknown }).code
  return typeof code === 'string' && UNREACHED.has(code)
}

/** Where a backend error's reason comes from, for the envelope around it. */
export const ERROR_DETAIL_SOURCE =
  "the backend's error detail, which can relay Bambuddy's or another upstream's own message"

/** Where an unexpected error's message comes from, for the envelope around it. */
export const UNEXPECTED_ERROR_SOURCE = 'the error raised while the tool ran, whose message can quote upstream responses'

/** A ToolError's message as the model may see it: the summary bare, the upstream reason wrapped. */
export function toolErrorText(err: ToolError, tool: string): string {
  return err.untrusted === undefined
    ? err.message
    : `${err.summary}: ${wrapUntrustedText(tool, ERROR_DETAIL_SOURCE, err.untrusted)}`
}

/**
 * What went wrong, for an error that is neither a ToolError nor worth another try (#4442
 * review): arguments that do not parse said as such, else the error's own message, which
 * the model gets in the untrusted-data envelope (it can quote an upstream). `detail` is the
 * audit row's, unwrapped.
 */
export function unexpectedErrorText(err: unknown, tool: string): { text: string; detail: string } {
  if (err instanceof z.ZodError) {
    const text = `its arguments are not valid (${z.prettifyError(err)})`
    return { text, detail: text }
  }
  const reason = err instanceof Error ? err.message : String(err)
  return { text: `it failed: ${wrapUntrustedText(tool, UNEXPECTED_ERROR_SOURCE, reason)}`, detail: `it failed: ${reason}` }
}

export function errorResult(message: string): CallToolResult {
  return { isError: true, content: [{ type: 'text', text: message }] }
}

/** How a call ended, for the audit log (audit/log.ts, #258). */
export type ToolOutcome = 'ok' | 'error' | 'refused' | 'denied'

export type ToolRun = {
  result: CallToolResult
  outcome: ToolOutcome
  /** Why, when it did not succeed: the refusal or error message. */
  detail?: string
  /** The approval an executed outward call ran on (RunReport). */
  approvalId?: string
  /** The tool that actually ran and its parsed input, when not the tool called (RunReport). */
  ran?: { tool: string; input: Record<string, unknown> }
}

function refused(message: string): ToolRun {
  return { result: errorResult(message), outcome: 'refused', detail: message }
}

function failed(message: string): ToolRun {
  return { result: errorResult(message), outcome: 'error', detail: message }
}

/**
 * An error whose `reason` ScadBuddy did not write: the model gets `summary`
 * bare and `reason` in the untrusted-data envelope; the audit row keeps both.
 */
function failedWith(tool: Pick<Tool, 'name'>, summary: string, reason: string, source: string): ToolRun {
  return {
    result: errorResult(`${summary}: ${wrapUntrustedText(tool.name, source, reason)}`),
    outcome: 'error',
    detail: `${summary}: ${reason}`,
  }
}

/**
 * The one entry point both projections use: tier check, then the approval
 * gate for outward tools, then the handler. Errors become `isError` results
 * so the model sees them; they are never thrown into the transport.
 *
 * What a handler returns is re-encoded as untrusted data
 * (safety/untrusted.ts `markUntrusted`, #258): tools hand back READMEs,
 * OpenSCAD source, render logs, library and Bambuddy data, any of which can
 * carry a prompt injection. ScadBuddy's own messages (the tier refusal, the
 * pending-approval notice, a thrown ToolError's summary) are not wrapped;
 * the upstream reason an error carries (ToolError `untrusted`, or an
 * unexpected error's message) is.
 */
export async function runToolWithOutcome(tool: Tool, args: unknown, ctx: ToolContext): Promise<ToolRun> {
  // What the handler reports about its own run (confirm_action) is kept
  // whether the run then answered or threw: an approval it consumed is on the
  // row either way.
  let reported: RunReport = {}
  // The envelope names the tool whose content it is: the one the handler
  // reports it ran (confirm_action runs the approved tool), else the tool called.
  const executed = (): Pick<Tool, 'name' | 'source'> =>
    (reported.ran && ctx.lookup?.(reported.ran.tool)) || tool
  const run = await runJudgedByResult(
    tool,
    args,
    {
      ...ctx,
      // Merged: confirm_action reports the approval and the tool, and the tool it ran may add its own detail.
      report: (r) => {
        reported = { ...reported, ...r }
      },
    },
    executed,
  )
  const done: ToolRun = {
    ...run,
    ...(reported.outcome ? { outcome: reported.outcome } : {}),
    ...(reported.detail ? { detail: reported.detail } : {}),
    ...(reported.approvalId ? { approvalId: reported.approvalId } : {}),
    ...(reported.ran ? { ran: reported.ran } : {}),
  }
  // What the session touched (#931, sessions/touched.ts): here, not in a
  // projection, so every path that runs a session's tool records it (the
  // harness server today; the tool activities a durable session would run,
  // proposed in PR #972's docs/superpowers/specs/
  // 2026-10-01-durable-printing-agents-flows-design.md §5.3, call this too).
  // The tool that ran, as it ran; never fails the call.
  if (ctx.session !== undefined && (done.outcome === 'ok' || done.outcome === 'error') && ctx.touched) {
    // A sink should never throw (TouchedSink); held to that here, so a sink
    // that does cannot turn a call that ran into one that failed.
    try {
      await ctx.touched.record({
        sessionId: ctx.session,
        tool: ranTool(tool, done, ctx),
        input: done.ran?.input ?? parsedOrRaw(tool, args),
        result: done.result,
        ok: done.outcome === 'ok',
      })
    } catch {
      // Nothing to report it to: the sink's own onError is where its failures go.
    }
  }
  return done
}

/**
 * The tool a run executed: the tool called, or the one confirm_action ran.
 * Without `lookup` the ran tool's name is still the one recorded (its tier
 * unknown, so `outward`), never confirm_action's.
 */
function ranTool(tool: Tool, run: ToolRun, ctx: ToolContext): { name: string; risk: Risk } {
  if (!run.ran) return tool
  return ctx.lookup?.(run.ran.tool) ?? { name: run.ran.tool, risk: 'outward' }
}

/** The arguments as the handler saw them (defaults applied), or as sent when they do not parse. */
export function parsedOrRaw(tool: Tool, args: unknown): Record<string, unknown> {
  try {
    return tool.parse(args)
  } catch {
    return (args ?? {}) as Record<string, unknown>
  }
}

async function runJudgedByResult(
  tool: Tool,
  args: unknown,
  ctx: ToolContext,
  executed: () => Pick<Tool, 'name' | 'source'>,
): Promise<ToolRun> {
  let effective: EffectiveTool
  try {
    effective = ctx.effective ?? (await effectiveTool(tool, ctx))
  } catch {
    // Fails closed: a database blip never runs a tool Settings disabled.
    return refused(`${tool.name} is refused: its Settings → Plugins entry cannot be read`)
  }
  if (effective.disabled) {
    return refused(`${tool.name} is disabled in Settings → Plugins; the user can turn it back on there`)
  }
  if (!hasTier(ctx.principal, effective.risk)) {
    return refused(
      `${tool.name} needs the "${effective.risk}" tier; this caller has ${ctx.principal.tiers.join(', ') || 'none'}`,
    )
  }
  if (ctx.gate === 'harness' && effective.risk === 'outward' && ctx.seamTier !== undefined && ctx.seamTier !== 'outward') {
    return refused(
      `${tool.name} was raised to "outward" during this turn, so it needs a human approval this turn did not ask for; ` +
        'nothing was sent. It asks for one from the next turn.',
    )
  }
  try {
    if (effective.gated && ctx.gate === undefined) {
      // The prepare half of spec §8.2's prepare/confirm: record, do not act.
      const { input, summary } = await tool.prepareApproval(args, ctx)
      // The approved input differs from the call's when the tool resolved what an argument
      // names (#4442); confirm_action must then send it, so the answer carries it.
      const resolved = JSON.stringify(input) !== JSON.stringify(tool.parse(args))
      const action = await ctx.pending.prepare(ctx.principal, {
        tool: tool.name,
        input,
        summary,
        workspace: workspaceOf(ctx),
      })
      return {
        result: json({
          status: 'pending_approval',
          pending_action_id: action.id,
          summary: action.summary,
          expires_at: action.expiresAt.toISOString(),
          ...(resolved ? { arguments: input } : {}),
          next:
            'Nothing was sent. Outward actions need a human approval in the ScadBuddy UI. Once the user has ' +
            `approved it there, call confirm_action with this pending_action_id and ${
              resolved ? 'exactly these `arguments` (what the user approves)' : 'exactly the same arguments'
            }; until then confirm_action answers pending_approval.`,
        }),
        outcome: 'refused',
        detail: `waiting for approval (pending action ${action.id}); nothing was sent`,
      }
    }
    // Every backend call names the agent, so each commit it makes is the agent's (#252).
    const deadline = tool.deadlineMs(args, ctx)
    const raw = await withDeadline(deadline, ctx.signal, (signal) =>
      tool.execute(args, {
        ...ctx,
        signal,
        backend: authored(ctx.backend, authorHeaders(ctx.principal, ctx.session, tool.name)),
      }),
    )
    if (raw === TIMED_OUT) {
      const after = `${tool.name} did not answer within ${Math.round(deadline / 1000)} s`
      // A read can simply be asked again. Anything else may have been done by
      // the backend all the same (a handler need not heed the abort), and a
      // retry is a new call with a new idempotency key: a second print.
      return failed(
        tool.risk === 'read'
          ? `${after} and was stopped: what it calls is not answering. Try it again later, or carry on without it.`
          : `${after}. It may still have taken effect: check whether it did before calling ${tool.name} again.`,
      )
    }
    const by = executed()
    const result = markUntrusted(raw, by.name, by.source)
    return result.isError
      ? { result, outcome: 'error', detail: 'the tool returned an error result' }
      : { result, outcome: 'ok' }
  } catch (err) {
    if (err instanceof z.ZodError) return failed(`invalid arguments: ${z.prettifyError(err)}`)
    const by = executed()
    if (err instanceof ToolError && err.untrusted !== undefined) {
      return failedWith(by, err.summary, err.untrusted, ERROR_DETAIL_SOURCE)
    }
    if (err instanceof ToolError || err instanceof PendingStoreFullError) return failed(err.message)
    if (err instanceof Error && err.name === 'AbortError') return failed('the call was cancelled')
    // An unexpected error's message can quote anything (a response body, a path).
    return failedWith(by, `${by.name} failed`, err instanceof Error ? err.message : String(err), UNEXPECTED_ERROR_SOURCE)
  }
}

const TIMED_OUT = Symbol('timed out')

/** How long a stopped call's handler has to answer before the Stop is answered for it (#2099). */
const STOP_GRACE_MS = 1_000

/**
 * `run`'s result, or `TIMED_OUT` once `ms` passes, when the signal it was given
 * is aborted, or an `AbortError` shortly after `outer` is (a user Stop). Both
 * answer whether or not the handler heeds that signal; one that does not is
 * left to finish unobserved.
 */
async function withDeadline<T>(
  ms: number,
  outer: AbortSignal,
  run: (signal: AbortSignal) => Promise<T>,
): Promise<T | typeof TIMED_OUT> {
  const timer = new AbortController()
  let handle: ReturnType<typeof setTimeout> | undefined
  const racers: Promise<T | typeof TIMED_OUT>[] = []
  if (Number.isFinite(ms)) {
    racers.push(
      new Promise<typeof TIMED_OUT>((resolve) => {
        handle = setTimeout(() => {
          // Settled before the abort, so the handler's own AbortError cannot win the race.
          resolve(TIMED_OUT)
          timer.abort()
        }, ms)
      }),
    )
  }
  // A Stop is answered for the handler only once it has had STOP_GRACE_MS to answer
  // itself: one that heeds the signal can say what it had already done (a submitted
  // render's job id), and that is worth more to the model than a bare cancel.
  let grace: ReturnType<typeof setTimeout> | undefined
  let stop = () => {}
  racers.push(
    new Promise<never>((_, reject) => {
      stop = () => {
        grace = setTimeout(() => reject(new DOMException('the call was cancelled', 'AbortError')), STOP_GRACE_MS)
      }
      if (outer.aborted) stop()
      else outer.addEventListener('abort', stop, { once: true })
    }),
  )
  const running = run(AbortSignal.any([outer, timer.signal]))
  running.catch(() => {})
  try {
    return await Promise.race([running, ...racers])
  } finally {
    clearTimeout(handle)
    clearTimeout(grace)
    outer.removeEventListener('abort', stop)
  }
}

/** `runToolWithOutcome`, the result only. */
export async function runTool(tool: Tool, args: unknown, ctx: ToolContext): Promise<CallToolResult> {
  return (await runToolWithOutcome(tool, args, ctx)).result
}

// ── result helpers ─────────────────────────────────────────────────────────

export function json(data: unknown): CallToolResult {
  return { content: [{ type: 'text', text: JSON.stringify(data, null, 2) }] }
}

export function text(value: string): CallToolResult {
  return { content: [{ type: 'text', text: value }] }
}

export function image(bytes: ArrayBuffer, mimeType: string): CallToolResult {
  return { content: [{ type: 'image', data: Buffer.from(bytes).toString('base64'), mimeType }] }
}

/**
 * Binary content (a GLB, a 3MF) as an embedded resource. Size is binary.ts's
 * concern: it links instead of calling this when the bytes are over the cap.
 */
export function blob(uri: string, bytes: ArrayBuffer, mimeType: string): CallToolResult {
  return {
    content: [
      {
        type: 'resource',
        resource: { uri, mimeType, blob: Buffer.from(bytes).toString('base64') },
      },
    ],
  }
}
