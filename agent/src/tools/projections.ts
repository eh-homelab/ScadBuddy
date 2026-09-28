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
 * The shape as the Agent SDK's in-process server validates it. That server is
 * the SDK's own bundled copy of the MCP server and of zod (4.4.3 in SDK
 * 0.3.283, sdk.mjs), which builds the top-level object itself and treats a key
 * as omittable only when its schema's `_zod.optin` is `"optional"`. Our zod
 * (4.6.5, zod/v4/core/schemas.js `$ZodDefault`) marks a `.default()` field
 * `"defaulted"`, and `.optional()` on top keeps that, so the bundled parser
 * refused every call that left such a field out ("expected nonoptional";
 * test/harnessWiring.test.ts, `delete_model` without `force`). A top-level
 * `.default()` field is therefore offered as its inner type, `.optional()`,
 * with the default (and any description) as metadata, which renders the same
 * JSON Schema (test/projections.test.ts compares the two listings). `runTool`
 * parses the arguments with the tool's own schema, which applies the default.
 * Nested fields are parsed by our zod's own schemas and need nothing.
 */
function sdkShape(shape: z.ZodRawShape): z.ZodRawShape {
  return Object.fromEntries(
    Object.entries(shape).map(([key, field]) => {
      if (!(field instanceof z.ZodDefault)) return [key, field]
      const inner = field.unwrap() as z.ZodType
      return [key, inner.optional().meta({ ...z.globalRegistry.get(field), default: field.def.defaultValue })]
    }),
  )
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
        sdkShape(t.shape),
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
