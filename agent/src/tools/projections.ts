import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool as sdkTool } from '@anthropic-ai/claude-agent-sdk'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { context as otelContext, SpanStatusCode } from '@opentelemetry/api'
import type { Principal } from '../auth/principal.js'
import type { AuditLog } from '../audit/log.js'
import { toolContextFor, withSpan } from '../telemetry/trace.js'
import { MCP_UNTRUSTED_CONTENT_POLICY } from '../safety/untrusted.js'
import { errorResult, parsedOrRaw, type Progress, runTool, runToolWithOutcome, type Tool, type ToolRun, type ToolServices } from './registry.js'

// The two projections of the registry (spec §5.1, D3). Both hand every call to
// `runTool`, with the same names, descriptions, input shapes and annotations;
// test/projections.test.ts asserts the two tool lists are identical.

export const SERVER_NAME = 'scadbuddy'
export const SERVER_VERSION = '0.1.0'

type ExtraLike = {
  signal?: AbortSignal
  _meta?: { progressToken?: string | number }
  sendNotification?: (notification: {
    method: 'notifications/progress'
    params: { progressToken: string | number; progress: number; total?: number; message?: string }
  }) => Promise<void>
  authInfo?: { extra?: Record<string, unknown> }
}

/**
 * `notifications/progress` for the call's progress token, per the MCP spec's
 * progress utility; a no-op when the caller sent none. Both projections pass
 * the MCP SDK's request `extra`, so this reads the same fields for each.
 */
export function progressFrom(extra: unknown): Progress {
  const e = (extra ?? {}) as ExtraLike
  const token = e._meta?.progressToken
  const send = e.sendNotification
  if (token === undefined || !send) return async () => {}
  return async (progress, total, message) => {
    await send({
      method: 'notifications/progress',
      params: {
        progressToken: token,
        progress,
        ...(total !== undefined ? { total } : {}),
        ...(message !== undefined ? { message } : {}),
      },
    })
  }
}

/** Name → tool over the projection's own list, for `confirm_action`. */
function lookupIn(tools: readonly Tool[]): (name: string) => Tool | undefined {
  const byName = new Map(tools.map((t) => [t.name, t]))
  return (name) => byName.get(name)
}

/** The row's detail: the run's own, noting a call that confirm_action executed. */
function detailOf(run: ToolRun): { detail?: string } {
  if (!run.ran) return run.detail === undefined ? {} : { detail: run.detail }
  return { detail: `run by confirm_action${run.detail === undefined ? '' : `: ${run.detail}`}` }
}

/** What Claude Code puts in an MCP call's `_meta` (measured on 2.1.283, harness/questions.ts). */
function toolUseIdFrom(extra: unknown): string | undefined {
  const id = (extra as { _meta?: Record<string, unknown> } | undefined)?._meta?.['claudecode/toolUseId']
  return typeof id === 'string' && id ? id : undefined
}

function signalFrom(extra: unknown): AbortSignal {
  return (extra as ExtraLike | undefined)?.signal ?? new AbortController().signal
}

/**
 * Harness projection: an in-process SDK MCP server, so the tools reach Claude
 * as `mcp__scadbuddy__<name>` (custom tools,
 * https://code.claude.com/docs/en/agent-sdk/custom-tools). The principal is the
 * session's own (the browser user, or a flow's declared permissions, spec §8.1).
 * Only for a harness query, whose permission seam gates outward tools before
 * they reach this server: given to anything else, an outward call would run
 * unapproved.
 */
export function createHarnessServer(
  tools: readonly Tool[],
  services: ToolServices,
  principal: Principal,
  /** The session the server's calls run in, for the commits they make (authorship.ts, #252). */
  session?: string,
): McpSdkServerConfigWithInstance {
  const lookup = lookupIn(tools)
  return createSdkMcpServer({
    name: SERVER_NAME,
    version: SERVER_VERSION,
    tools: tools.map((t) =>
      sdkTool(
        t.name,
        t.description,
        // The raw shape, as the SDK's types ask. The server bundled in
        // @anthropic-ai/claude-agent-sdk 0.3.283 rebuilt it with its own copy
        // of zod, which refused an omitted `.default()` field ("expected
        // nonoptional, received undefined") instead of filling the default, so
        // e.g. update_source without `force` never ran (measured 2026-09-28 by
        // the eval harness), and this passed a whole z.object behind a cast.
        // 0.3.287 fills the default (measured 2026-10-06);
        // test/projections.test.ts calls update_source without `force`.
        t.shape,
        (args, extra) =>
          // In the call's own span (telemetry/turn.ts TurnTrace), found by the
          // tool_use id Claude Code sends in `_meta`: the SDK runs this handler
          // in the query's context, not the tool's (telemetry/trace.ts).
          otelContext.with(toolContextFor(extra), () =>
            // `gate: 'harness'`: the query's permission seam has already parked
            // an outward call for approval (registry.ts ToolContext.gate).
            runTool(t, args, {
              ...services,
              principal,
              session,
              toolUseId: toolUseIdFrom(extra),
              progress: progressFrom(extra),
              signal: signalFrom(extra),
              lookup,
              gate: 'harness',
            }),
          ),
        { annotations: t.annotations },
      ),
    ),
  })
}

/** The principal `/mcp` attached to this request's `authInfo` (see ../mcp/http.ts). */
export function principalFrom(extra: unknown): Principal | undefined {
  const principal = (extra as ExtraLike | undefined)?.authInfo?.extra?.principal
  return principal as Principal | undefined
}

/**
 * External projection: one MCP server per `/mcp` session (an `McpServer`
 * connects to exactly one transport). The principal is re-read from every
 * request's auth, so a token revoked mid-session stops working at once.
 */
export function createExternalServer(tools: readonly Tool[], services: ToolServices, audit?: AuditLog): McpServer {
  const lookup = lookupIn(tools)
  const server = new McpServer(
    { name: SERVER_NAME, version: SERVER_VERSION },
    {
      capabilities: { logging: {} },
      instructions:
        'ScadBuddy: an OpenSCAD customizer that sends multi-colour 3MFs to Bambuddy. Outward tools ' +
        '(send, print, delete, settings writes) return a pending action for a human to approve in the ' +
        'ScadBuddy UI instead of acting; once approved, confirm_action with the same arguments runs it once. ' +
        MCP_UNTRUSTED_CONTENT_POLICY,
    },
  )
  for (const t of tools) {
    server.registerTool(
      t.name,
      { description: t.description, inputSchema: t.shape, annotations: t.annotations },
      async (args, extra): Promise<CallToolResult> => {
        const principal = principalFrom(extra)
        if (!principal) return errorResult('unauthenticated')
        const startedAt = new Date()
        // `agent.tool/<name>` under the request's `agent.mcp/tools/call` (spec
        // 2026-10-01 §5.4); never its input or result (§6).
        const run = await withSpan(
          `agent.tool/${t.name}`,
          { attributes: { 'scadbuddy.tool': t.name, 'scadbuddy.tier': t.risk } },
          async (span) => {
            const done = await runToolWithOutcome(t, args, {
              ...services,
              principal,
              progress: progressFrom(extra),
              signal: extra.signal,
              lookup,
            })
            span.setAttribute('scadbuddy.outcome', done.outcome)
            // A ToolRun carries no error class, only a message (never recorded, §6).
            if (done.outcome === 'error') {
              span.setStatus({ code: SpanStatusCode.ERROR })
              span.setAttribute('scadbuddy.failure_class', 'ToolError')
            }
            return done
          },
        )
        // Every /mcp call, whatever became of it (#258, audit/log.ts). The row
        // names the tool that ran: a confirm_action that executed its approved
        // call is recorded as that call, with the approval it ran on, and its
        // hash is of the parsed input (defaults applied), as the approval's is.
        const action = run.ran?.tool ?? t.name
        const input = run.ran?.input ?? parsedOrRaw(t, args)
        await audit?.record({
          kind: 'tool_call',
          action,
          surface: 'mcp',
          actor: { kind: principal.kind, id: principal.id, label: principal.id },
          clientIp: principal.clientIp,
          tier: run.ran ? (lookup(run.ran.tool)?.risk ?? t.risk) : t.risk,
          inputHash: audit.hash(action, input),
          inputSummary: audit.summarise(action, input),
          ...(run.approvalId === undefined ? {} : { approvalId: run.approvalId }),
          outcome: run.outcome,
          ...detailOf(run),
          startedAt,
          finishedAt: new Date(),
        })
        return run.result
      },
    )
  }
  return server
}
