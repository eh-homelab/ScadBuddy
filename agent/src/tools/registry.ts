import type { CallToolResult, ToolAnnotations } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { BackendClient } from '../api/backend.js'
import type { paths } from '../api/schema.js'
import { hasTier, type Principal, type Tier } from '../auth/principal.js'
import { type OutwardActions, PendingStoreFullError } from './pending.js'

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
  /** Binary results above this are returned as a link, not inline (binary.ts; 8 MiB by default). */
  maxInlineBytes?: number
  /** SCADBUDDY_PUBLIC_URL, so a link to a backend route can be absolute. */
  publicBaseUrl?: string | undefined
}

export type ToolContext = ToolServices & {
  principal: Principal
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
   */
  gate?: 'harness'
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
  handler: (args: z.infer<z.ZodObject<S>>, ctx: ToolContext) => Promise<CallToolResult>
}

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
  summarize(args: unknown): string
  /** The arguments as the handler would see them (defaults applied): what an approval's input hash covers. */
  parse(args: unknown): Record<string, unknown>
  /** Parses `args` and runs the handler, with no tier check or gate: call `runTool` instead. */
  execute(args: unknown, ctx: ToolContext): Promise<CallToolResult>
}

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
    parse(args) {
      return spec.input.parse(args) as Record<string, unknown>
    },
    execute(args, ctx) {
      return spec.handler(spec.input.parse(args), ctx)
    },
  }
}

export class ToolError extends Error {
  override name = 'ToolError'
  /** The backend's HTTP status, when the error is a backend answer (call.ts `ok`). */
  readonly status: number | undefined

  constructor(message: string, status?: number) {
    super(message)
    this.status = status
  }
}

export function errorResult(message: string): CallToolResult {
  return { isError: true, content: [{ type: 'text', text: message }] }
}

/**
 * The one entry point both projections use: tier check, then the approval
 * gate for outward tools, then the handler. Errors become `isError` results
 * so the model sees them; they are never thrown into the transport.
 */
export async function runTool(tool: Tool, args: unknown, ctx: ToolContext): Promise<CallToolResult> {
  if (!hasTier(ctx.principal, tool.risk)) {
    return errorResult(
      `${tool.name} needs the "${tool.risk}" tier; this caller has ${ctx.principal.tiers.join(', ') || 'none'}`,
    )
  }
  try {
    if (tool.gated && ctx.gate !== 'harness') {
      // The prepare half of spec §8.2's prepare/confirm: record, do not act.
      const input = tool.parse(args)
      const action = await ctx.pending.prepare(ctx.principal, { tool: tool.name, input, summary: tool.summarize(args) })
      return json({
        status: 'pending_approval',
        pending_action_id: action.id,
        summary: action.summary,
        expires_at: action.expiresAt.toISOString(),
        next:
          'Nothing was sent. Outward actions need a human approval in the ScadBuddy UI. Once the user has ' +
          'approved it there, call confirm_action with this pending_action_id and exactly the same arguments; ' +
          'until then confirm_action answers pending_approval.',
      })
    }
    return await tool.execute(args, ctx)
  } catch (err) {
    if (err instanceof z.ZodError) return errorResult(`invalid arguments: ${z.prettifyError(err)}`)
    if (err instanceof ToolError || err instanceof PendingStoreFullError) return errorResult(err.message)
    if (err instanceof Error && err.name === 'AbortError') return errorResult('the call was cancelled')
    return errorResult(`${tool.name} failed: ${err instanceof Error ? err.message : String(err)}`)
  }
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
