import { createSdkMcpServer, type McpSdkServerConfigWithInstance, tool as sdkTool } from '@anthropic-ai/claude-agent-sdk'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { z } from 'zod'
import type { Principal } from '../auth/principal.js'
import { errorResult, type Progress, runTool, type Tool, type ToolServices } from './registry.js'

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
): McpSdkServerConfigWithInstance {
  const lookup = lookupIn(tools)
  return createSdkMcpServer({
    name: SERVER_NAME,
    version: SERVER_VERSION,
    tools: tools.map((t) =>
      sdkTool(
        t.name,
        t.description,
        // A whole z.object, not the raw shape the SDK's types ask for. Given a
        // raw shape, the server bundled in @anthropic-ai/claude-agent-sdk
        // 0.3.283 rebuilds the object with its own copy of zod, and that copy
        // refuses an omitted `.default()` field ("expected nonoptional,
        // received undefined") instead of filling the default, so e.g.
        // update_source without `force` never ran. Measured 2026-09-28 by the
        // eval harness (evals/, test/evals.test.ts); test/projections.test.ts
        // keeps it fixed. The server accepts any zod schema at runtime
        // (it validates with the schema's own `safeParseAsync`), and the
        // listed JSON Schema is unchanged (same test). The cast hides that
        // from the types, so the same file pins the SDK version: a bump fails
        // there until someone re-checks this (and drops it if fixed).
        z.object(t.shape) as unknown as typeof t.shape,
        (args, extra) =>
          // `gate: 'harness'`: the query's permission seam has already parked
          // an outward call for approval (registry.ts ToolContext.gate).
          runTool(t, args, {
            ...services,
            principal,
            progress: progressFrom(extra),
            signal: signalFrom(extra),
            lookup,
            gate: 'harness',
          }),
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
export function createExternalServer(tools: readonly Tool[], services: ToolServices): McpServer {
  const lookup = lookupIn(tools)
  const server = new McpServer(
    { name: SERVER_NAME, version: SERVER_VERSION },
    {
      capabilities: { logging: {} },
      instructions:
        'ScadBuddy: an OpenSCAD customizer that sends multi-colour 3MFs to Bambuddy. Outward tools ' +
        '(send, print, delete, settings writes) return a pending action for a human to approve in the ' +
        'ScadBuddy UI instead of acting; once approved, confirm_action with the same arguments runs it once.',
    },
  )
  for (const t of tools) {
    server.registerTool(
      t.name,
      { description: t.description, inputSchema: t.shape, annotations: t.annotations },
      async (args, extra): Promise<CallToolResult> => {
        const principal = principalFrom(extra)
        if (!principal) return errorResult('unauthenticated')
        return runTool(t, args, { ...services, principal, progress: progressFrom(extra), signal: extra.signal, lookup })
      },
    )
  }
  return server
}
