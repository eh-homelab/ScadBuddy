import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { AuditLog } from '../audit/log.js'
import type { Tier } from '../auth/principal.js'
import { durableRequestId } from '../gate/ids.js'
import type { RiskTier } from '../harness/permissions.js'
import { harnessToolName } from '../plugins/registry.js'
import type { Owner } from '../sessions/protocol.js'
import { turnPrincipal } from '../tools/harness.js'
import {
  type ApprovalRecords,
  durableSession,
  NOT_APPROVED,
  resultText,
  type SessionOwners,
  TOOL_ERROR,
  turnTiers,
} from './toolActivities.js'

// The rest of a turn's tools in a durable session (#2320). A classic turn hands Claude
// Code more MCP servers than ScadBuddy's own (sessions/manager.ts `runTurn`): the remote
// plugins through the loopback forwarder, `http_request`, the headless browser. A
// durable turn's tools are the workflow's durable tools, each an activity on
// `agent-tools`, so the same servers are offered there as durable tools too:
//
//   - `turn_tools` (once per turn, from DurableSession) lists them: each server's tools,
//     named `<server>__<tool>` as Claude Code would name them after `mcp__`;
//   - `turn_tool_tier` (for each call, before it may park) says the call's tier, from
//     its input, as the permission seam would (an http GET is read, a POST outward);
//   - the call itself is the activity `turn:<server>__<tool>`, served by the worker's
//     `default` activity (`turnToolActivity`), which builds the turn's servers again,
//     checks the tier, the principal and the approval as the call runs, and calls the
//     tool through the same code a classic turn's Claude Code reaches.
//
// Every decision is made from the servers as they are when the call runs, never from
// what was listed: a plugin disabled or removed since the list is refused, and a tool
// raised to outward since needs an approval it does not have.

export const TURN_TOOLS_ACTIVITY = 'turn_tools'
export const TURN_TOOL_TIER_ACTIVITY = 'turn_tool_tier'
/** The activity type a turn tool's call runs as: `turn:<server>__<tool>`. */
export const TURN_TOOL_PREFIX = 'turn:'
const SEPARATOR = '__'

/** One MCP server a turn offers beside ScadBuddy's own tools. */
export type TurnServer = {
  /** Its name, as the classic turn's `mcpServers` key. */
  name: string
  /** A client on it, for one listing or one call; the caller closes it. */
  connect(): Promise<Client>
  /** The tier of a call by its harness name (`mcp__<server>__<tool>`) and input, as the permission seam resolves it. */
  tierOf(harnessName: string, input: unknown): RiskTier
}

export type TurnServers = {
  servers: TurnServer[]
  /** Why a configured server is not offered (a plugin that failed its check), said in the session. */
  problems: string[]
  release(): void | Promise<void>
}

export type TurnContext = { session: string; owner: Owner; signal: AbortSignal }

export type TurnToolsDeps = {
  sessions: SessionOwners
  /** The turn's servers, built as a classic turn builds them, for one listing or one call. */
  servers(context: TurnContext): Promise<TurnServers>
  approvals?: ApprovalRecords | undefined
  audit?: Pick<AuditLog, 'record' | 'hash' | 'summarise'> | undefined
  currentTiers?: ((owner: Owner) => Promise<readonly Tier[] | undefined>) | undefined
  /** Says in the session why a configured server was left out (code `plugin_unavailable`). */
  report?: ((session: string, problems: readonly string[]) => Promise<void>) | undefined
}

/** What DurableSession declares for one turn tool. */
export type TurnToolEntry = {
  name: string
  description: string
  input_schema: Record<string, unknown>
  /** Whether a call may be outward: it then parks unless `turn_tool_tier` says otherwise, and runs at most once. */
  may_park: boolean
}

export type TurnToolsResult = { tools: TurnToolEntry[] }

/** A client on an in-process MCP server (an SDK server's `instance`), made fresh for each use. */
export async function connectInProcess(create: () => McpServer): Promise<Client> {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await create().connect(serverSide)
  const client = new Client({ name: 'scadbuddy-durable-turn', version: '1' })
  await client.connect(clientSide)
  return client
}

/** A client on a Streamable HTTP MCP endpoint (a remote plugin's loopback forwarder URL). */
export async function connectHttp(url: string): Promise<Client> {
  const client = new Client({ name: 'scadbuddy-durable-turn', version: '1' })
  await client.connect(new StreamableHTTPClientTransport(new URL(url)))
  return client
}

/** `<server>__<tool>`, the name Claude sees after `mcp__durable__`. */
export function turnToolName(server: string, tool: string): string {
  return `${server}${SEPARATOR}${harnessToolName(tool)}`
}

function split(name: string): { server: string; tool: string } | undefined {
  const at = name.indexOf(SEPARATOR)
  return at > 0 ? { server: name.slice(0, at), tool: name.slice(at + SEPARATOR.length) } : undefined
}

async function withClient<T>(server: TurnServer, use: (client: Client) => Promise<T>): Promise<T> {
  const client = await server.connect()
  try {
    return await use(client)
  } finally {
    await client.close().catch(() => {})
  }
}

/** The server's tools as {original name, harness name}, as Claude Code would list them. */
async function listed(server: TurnServer, signal: AbortSignal) {
  return withClient(server, async (client) => {
    const { tools } = await client.listTools(undefined, { signal })
    return tools.map((tool) => ({ tool, name: harnessToolName(tool.name) }))
  })
}

async function withServers<T>(deps: TurnToolsDeps, context: TurnContext, use: (set: TurnServers) => Promise<T>): Promise<T> {
  const set = await deps.servers(context)
  try {
    return await use(set)
  } finally {
    await set.release()
  }
}

/** The tool a turn tool's name names now, or a non-retryable refusal saying why not. */
async function resolve(set: TurnServers, name: string, signal: AbortSignal) {
  const parts = split(name)
  const server = parts && set.servers.find((s) => s.name === parts.server)
  if (!parts || !server) {
    throw ApplicationFailure.nonRetryable(`${name} is not offered any more: its plugin or setting was turned off`, TOOL_ERROR)
  }
  const found = (await listed(server, signal)).find((t) => t.name === parts.tool)
  if (!found) throw ApplicationFailure.nonRetryable(`${name} is not offered any more by ${server.name}`, TOOL_ERROR)
  return { server, tool: found.tool, harnessName: `mcp__${server.name}__${parts.tool}` }
}

function inputObject(input: unknown): Record<string, unknown> {
  return typeof input === 'object' && input !== null && !Array.isArray(input) ? (input as Record<string, unknown>) : {}
}

/** `turn_tools` and `turn_tool_tier`, beside the tools on `agent-tools`. */
export function turnToolActivities(deps: TurnToolsDeps): Record<string, (input: unknown) => Promise<unknown>> {
  return {
    [TURN_TOOLS_ACTIVITY]: async (): Promise<TurnToolsResult> => {
      const { session, owner } = await durableSession(TURN_TOOLS_ACTIVITY, deps.sessions)
      const signal = Context.current().cancellationSignal
      return withServers(deps, { session, owner, signal }, async (set) => {
        if (set.problems.length) await deps.report?.(session, set.problems)
        const tools: TurnToolEntry[] = []
        for (const server of set.servers) {
          let offered: Awaited<ReturnType<typeof listed>>
          try {
            offered = await listed(server, signal)
          } catch (err) {
            await deps.report?.(session, [`plugin ${server.name} is not available in this turn: ${(err as Error).message}`])
            continue
          }
          for (const { tool, name } of offered) {
            tools.push({
              name: turnToolName(server.name, tool.name),
              description: tool.description ?? '',
              input_schema: tool.inputSchema as Record<string, unknown>,
              may_park: server.tierOf(`mcp__${server.name}__${name}`, undefined) === 'outward',
            })
          }
        }
        return { tools }
      })
    },
    [TURN_TOOL_TIER_ACTIVITY]: async (args: unknown): Promise<{ tier: RiskTier }> => {
      const { session, owner } = await durableSession(TURN_TOOL_TIER_ACTIVITY, deps.sessions)
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (typeof tool !== 'string') throw ApplicationFailure.nonRetryable('turn_tool_tier takes {tool: string, input}', TOOL_ERROR)
      const signal = Context.current().cancellationSignal
      return withServers(deps, { session, owner, signal }, async (set) => {
        const { server, harnessName } = await resolve(set, tool, signal)
        return { tier: server.tierOf(harnessName, input) }
      })
    },
  }
}

/**
 * The `default` activity of the agent-tools worker: a call of a turn tool
 * (`turn:<server>__<tool>`). Any other unregistered activity type is refused.
 */
export function turnToolActivity(deps: TurnToolsDeps): (input: unknown) => Promise<unknown> {
  return async (input: unknown) => {
    const context = Context.current()
    const { activityType, activityId, workflowExecution } = context.info
    if (!activityType.startsWith(TURN_TOOL_PREFIX)) {
      throw ApplicationFailure.nonRetryable(`no activity ${activityType} is served on this queue`, TOOL_ERROR)
    }
    const name = activityType.slice(TURN_TOOL_PREFIX.length)
    const { session, owner, turnTiers: sent } = await durableSession(name, deps.sessions)
    const toolUseId = activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId
    const requestId = durableRequestId(session, workflowExecution?.runId ?? '', toolUseId)
    const tiers = await turnTiers(owner, sent, deps)
    const principal = turnPrincipal(owner, tiers ? { tiers: [...tiers] } : undefined)
    const fields = inputObject(input)
    const signal = context.cancellationSignal
    return withServers(deps, { session, owner, signal }, async (set) => {
      const { server, tool, harnessName } = await resolve(set, name, signal)
      const tier = server.tierOf(harnessName, input)
      const startedAt = new Date()
      const record = (outcome: 'ok' | 'error' | 'refused', detail?: string) =>
        deps.audit?.record({
          kind: 'tool_call',
          action: name,
          surface: 'harness',
          actor: owner,
          sessionId: session,
          toolUseId,
          requestId,
          tier,
          inputHash: deps.audit.hash(name, fields),
          inputSummary: deps.audit.summarise(name, fields),
          outcome,
          ...(detail === undefined ? {} : { detail }),
          startedAt,
          finishedAt: new Date(),
        })
      const refuse = async (message: string, type: string): Promise<never> => {
        await record('refused', message)
        throw ApplicationFailure.nonRetryable(message, type)
      }
      if (!principal.tiers.includes(tier)) {
        await refuse(`${name} needs the "${tier}" tier, which this session's turn does not hold`, TOOL_ERROR)
      }
      // As runAsActivity: the approval is checked as the call runs, bound to this call's input.
      const binding = deps.audit ? { inputHash: deps.audit.hash(name, fields) } : undefined
      if (tier === 'outward' && !(await deps.approvals?.approved(requestId, binding))) {
        await refuse(`${name} needs an approval, and none is recorded for this call`, NOT_APPROVED)
      }
      const heartbeat = setInterval(() => context.heartbeat(), 10_000)
      let result: Awaited<ReturnType<Client['callTool']>>
      try {
        result = await withClient(server, (client) => client.callTool({ name: tool.name, arguments: fields }, undefined, { signal }))
      } catch (err) {
        if (signal.aborted) throw new CancelledFailure('the call was cancelled')
        await record('error', (err as Error).message)
        throw ApplicationFailure.nonRetryable(`${name} failed: ${(err as Error).message}`, TOOL_ERROR)
      } finally {
        clearInterval(heartbeat)
      }
      const text = resultText((result.content ?? []) as { type: string; text?: string }[])
      await record(result.isError ? 'error' : 'ok')
      if (result.isError) throw ApplicationFailure.nonRetryable(text, TOOL_ERROR)
      return text
    })
  }
}
