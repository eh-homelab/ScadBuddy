import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { AuditLog } from '../audit/log.js'
import type { Tier } from '../auth/principal.js'
import { durableRequestId, flowRequestId } from '../gate/ids.js'
import type { RiskTier } from '../harness/permissions.js'
import { harnessToolName } from '../plugins/registry.js'
import { redact } from '../secrets.js'
import type { Owner } from '../sessions/protocol.js'
import { turnPrincipal } from '../tools/harness.js'
import { type FlowRuns, flowOf } from './flowRuns.js'
import {
  type ApprovalRecords,
  durableSession,
  NOT_APPROVED,
  type ResultBlobs,
  resultText,
  type SessionOwners,
  TOOL_ERROR,
  turnTiers,
  UNKNOWN_FLOW,
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
//
// A flow run (`flow-<run id>`, #1057) reaches the same tools through `tool(name, args)`
// (backend/scadbuddy/workflows/flow_tools.py): `turn_tool_tier`, then the call, as the
// run's starter (flowRuns.ts), an outward one only with the backend's
// `workflow_run_decisions` approval, as a flow's ScadBuddy tools are. A flow has no
// session, so no headless browser (TurnContext.flow), and it lists nothing.

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
  /**
   * Whether some call of the tool could be outward, whatever its input. Listed as
   * `may_park`: only a tool listed so is ever asked about, so a tool whose tier depends
   * on its input (http_request's method, the headless browser's origin) must say yes.
   * Absent: every tool may (fail toward asking).
   */
  mayPark?(harnessName: string): boolean
  /**
   * Whether its calls must run on the replica that listed it (`TurnToolsDeps.hostQueue`):
   * a server whose state lives in this process or on this pod's disk (http_request's
   * saved bodies, the headless browser).
   */
  pinned?: boolean
  /** A long-lived client the server owns (the headless browser's process): never closed after a use. */
  keepOpen?: boolean
  /** Why a call must not run at all, by its input (the browser's filename and origin rules); undefined: it may. */
  refusal?(harnessName: string, input: unknown): string | undefined
  /** Whether a tool the server lists is offered (the browser's fixed deny list, Settings → Plugins). */
  offers?(tool: string): boolean
  /** Once an outward call's approval is verified, before it runs (an approved origin is remembered). */
  approved?(harnessName: string, input: unknown, requestId: string): Promise<void>
}

export type TurnServers = {
  servers: TurnServer[]
  /** Why a configured server is not offered (a plugin that failed its check), said in the session. */
  problems: string[]
  /**
   * The turn's secrets (every credential's, each plugin's header and forwarder URL):
   * redacted from every result, error, audit detail and problem before it leaves the
   * agent, as a classic turn scrubs its event log (sessions/sdkEvents.ts `scrubForLog`).
   */
  secrets: readonly string[]
  /** The plugin packages the turn's Claude Code loads; only for a listing (`TurnContext.listing`). */
  packages?: TurnPackage[]
  release(): void | Promise<void>
}

export type TurnContext = {
  /** The session, or `flow-<run id>` for a flow run's call (then `flow` is set). */
  session: string
  /** The flow run a call comes from: it has no session, so no session-bound server (the browser). */
  flow?: string
  owner: Owner
  signal: AbortSignal
  /** The call being made, for a server built for it (the browser's grant names its approval). */
  call?: { requestId: string; toolUseId: string }
  /** The turn's listing (`turn_tools`): the plugin packages are shared with the segment runner. */
  listing?: boolean
}

export type TurnToolsDeps = {
  sessions: SessionOwners
  /** A flow run's starter and its calls' approvals; without it a flow's call is refused. */
  flows?: FlowRuns | undefined
  /** The turn's servers, built as a classic turn builds them, for one listing or one call. */
  servers(context: TurnContext): Promise<TurnServers>
  approvals?: ApprovalRecords | undefined
  audit?: Pick<AuditLog, 'record' | 'hash' | 'summarise'> | undefined
  currentTiers?: ((owner: Owner) => Promise<readonly Tier[] | undefined>) | undefined
  /** Says in the session why a configured server was left out (code `plugin_unavailable`). */
  report?: ((session: string, problems: readonly string[]) => Promise<void>) | undefined
  /** This replica's own queue (`agent-tools.<host>`), where a pinned server's calls run. */
  hostQueue?: string | undefined
  /** Where a call's result images are kept for `view_tool_images` (toolActivities.ts `resultText`). */
  blobs?: ResultBlobs | undefined
}

/** What DurableSession declares for one turn tool. */
export type TurnToolEntry = {
  name: string
  description: string
  input_schema: Record<string, unknown>
  /** Whether a call may be outward: it then parks unless `turn_tool_tier` says otherwise, and runs at most once. */
  may_park: boolean
  /** The queue its calls run on when the server is pinned to the replica that listed it. */
  task_queue?: string
}

/**
 * A plugin package a durable turn loads into its Claude Code (#2320, turnPackages.ts):
 * copied to the payload volume both sidecars mount, under `dir` there, and checked
 * against `content_hash` again by the segment runner before it loads it.
 */
export type TurnPackage = {
  name: string
  dir: string
  content_hash: string
  /** Approved with `allow_refused`: loaded as it is, with `builtin_tools`. */
  allowed: boolean
  builtin_tools: string[]
}

export type TurnToolsResult = { tools: TurnToolEntry[]; packages: TurnPackage[] }

/** A client on an in-process MCP server (an SDK server's `instance`), made fresh for each use. */
export async function connectInProcess(create: () => McpServer): Promise<Client> {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await create().connect(serverSide)
  const client = new Client({ name: 'scadbuddy-durable-turn', version: '1' })
  await client.connect(clientSide)
  return client
}

/** A client on a Streamable HTTP MCP endpoint (a remote plugin's loopback forwarder URL). */
export async function connectHttp(url: string, headers?: Record<string, string>): Promise<Client> {
  const client = new Client({ name: 'scadbuddy-durable-turn', version: '1' })
  await client.connect(
    new StreamableHTTPClientTransport(new URL(url), headers ? { requestInit: { headers } } : undefined),
  )
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
    if (!server.keepOpen) await client.close().catch(() => {})
  }
}

/**
 * The server's tools as {original name, harness name}, as Claude Code would list them.
 * Tools whose harness names collide are left out, as a classic turn hides them
 * (plugins/forwarder.ts), so a name never stands for two tools.
 */
async function listed(server: TurnServer, signal: AbortSignal) {
  return withClient(server, async (client) => {
    const { tools } = await client.listTools(undefined, { signal })
    const named = tools.filter((tool) => server.offers?.(tool.name) ?? true).map((tool) => ({ tool, name: harnessToolName(tool.name) }))
    const seen = new Map<string, number>()
    for (const { name } of named) seen.set(name, (seen.get(name) ?? 0) + 1)
    return named.filter(({ name }) => seen.get(name) === 1)
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

/** Who a call runs as, with which tiers and under which approval: a durable session's turn, or a flow run's starter. */
type TurnCaller = {
  context: Omit<TurnContext, 'signal' | 'call'>
  owner: Owner
  /** The audit row's session; none for a flow. */
  sessionId: string | null
  requestId: string
  tiers: readonly Tier[] | undefined
  approved(requestId: string, binding: { inputHash: string } | undefined): Promise<boolean>
}

async function turnCaller(deps: TurnToolsDeps, name: string, toolUseId: string): Promise<TurnCaller> {
  const { workflowExecution } = Context.current().info
  const runId = workflowExecution?.runId ?? ''
  const flow = flowOf(workflowExecution?.workflowId)
  if (flow !== undefined) {
    const flows = deps.flows
    // A lookup that throws (the database is away) is left to the activity's retries.
    const owner = flows ? await flows.startedBy(flow) : undefined
    if (!flows || owner === undefined) {
      throw ApplicationFailure.nonRetryable(`${name}: no flow run ${flow} is recorded`, UNKNOWN_FLOW)
    }
    return {
      context: { session: `flow-${flow}`, flow, owner },
      owner,
      sessionId: null,
      requestId: flowRequestId(flow, runId, toolUseId),
      // A token starter's run acts with what its token holds now, as toolActivities' flowCaller.
      tiers: owner.kind === 'browser' ? undefined : await deps.currentTiers?.(owner),
      approved: (requestId) => flows.approved(requestId),
    }
  }
  const { session, owner, turnTiers: sent } = await durableSession(name, deps.sessions)
  return {
    context: { session, owner },
    owner,
    sessionId: session,
    requestId: durableRequestId(session, runId, toolUseId),
    tiers: await turnTiers(owner, sent, deps),
    approved: async (requestId, binding) => (await deps.approvals?.approved(requestId, binding)) ?? false,
  }
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
      return withServers(deps, { session, owner, signal, listing: true }, async (set) => {
        const scrub = (text: string) => redact(text, set.secrets)
        if (set.problems.length) await deps.report?.(session, set.problems.map(scrub))
        const tools: TurnToolEntry[] = []
        for (const server of set.servers) {
          let offered: Awaited<ReturnType<typeof listed>>
          try {
            offered = await listed(server, signal)
          } catch (err) {
            await deps.report?.(session, [scrub(`plugin ${server.name} is not available in this turn: ${(err as Error).message}`)])
            continue
          }
          for (const { tool, name } of offered) {
            tools.push({
              name: turnToolName(server.name, tool.name),
              description: tool.description ?? '',
              input_schema: tool.inputSchema as Record<string, unknown>,
              may_park: server.mayPark?.(`mcp__${server.name}__${name}`) ?? true,
              ...(server.pinned && deps.hostQueue ? { task_queue: deps.hostQueue } : {}),
            })
          }
        }
        return { tools, packages: set.packages ?? [] }
      })
    },
    [TURN_TOOL_TIER_ACTIVITY]: async (args: unknown): Promise<{ tier: RiskTier; task_queue?: string }> => {
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (typeof tool !== 'string') throw ApplicationFailure.nonRetryable('turn_tool_tier takes {tool: string, input}', TOOL_ERROR)
      const caller = await turnCaller(deps, TURN_TOOL_TIER_ACTIVITY, '')
      const signal = Context.current().cancellationSignal
      return withServers(deps, { ...caller.context, signal }, async (set) => {
        const { server, harnessName } = await resolve(set, tool, signal)
        // A flow takes its call's queue from here, since it lists nothing (turn_tools).
        const queue = server.pinned && deps.hostQueue ? { task_queue: deps.hostQueue } : {}
        // A call that will be refused is not parked: its activity says why.
        if (server.refusal?.(harnessName, input) !== undefined) return { tier: 'read' as const, ...queue }
        return { tier: server.tierOf(harnessName, input), ...queue }
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
    const { activityType, activityId } = context.info
    if (!activityType.startsWith(TURN_TOOL_PREFIX)) {
      throw ApplicationFailure.nonRetryable(`no activity ${activityType} is served on this queue`, TOOL_ERROR)
    }
    const name = activityType.slice(TURN_TOOL_PREFIX.length)
    const toolUseId = activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId
    const caller = await turnCaller(deps, name, toolUseId)
    const { owner, requestId, sessionId: session } = caller
    const principal = turnPrincipal(owner, caller.tiers ? { tiers: [...caller.tiers] } : undefined)
    const fields = inputObject(input)
    const signal = context.cancellationSignal
    return withServers(deps, { ...caller.context, signal, call: { requestId, toolUseId } }, async (set) => {
      const scrub = (text: string) => redact(text, set.secrets)
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
          ...(detail === undefined ? {} : { detail: scrub(detail) }),
          startedAt,
          finishedAt: new Date(),
        })
      const refuse = async (message: string, type: string): Promise<never> => {
        await record('refused', message)
        throw ApplicationFailure.nonRetryable(message, type)
      }
      const refused = server.refusal?.(harnessName, input)
      if (refused !== undefined) await refuse(refused, TOOL_ERROR)
      if (!principal.tiers.includes(tier)) {
        const who = caller.context.flow ? "this flow run's starter" : "this session's turn"
        await refuse(`${name} needs the "${tier}" tier, which ${who} does not hold`, TOOL_ERROR)
      }
      // As runAsActivity: the approval is checked as the call runs, bound to this call's input.
      const binding = deps.audit ? { inputHash: deps.audit.hash(name, fields) } : undefined
      if (tier === 'outward') {
        if (!(await caller.approved(requestId, binding))) {
          await refuse(`${name} needs an approval, and none is recorded for this call`, NOT_APPROVED)
        }
        await server.approved?.(harnessName, input, requestId)
      }
      const heartbeat = setInterval(() => context.heartbeat(), 10_000)
      let result: Awaited<ReturnType<Client['callTool']>>
      try {
        result = await withClient(server, (client) => client.callTool({ name: tool.name, arguments: fields }, undefined, { signal }))
      } catch (err) {
        if (signal.aborted) throw new CancelledFailure('the call was cancelled')
        await record('error', (err as Error).message)
        throw ApplicationFailure.nonRetryable(scrub(`${name} failed: ${(err as Error).message}`), TOOL_ERROR)
      } finally {
        clearInterval(heartbeat)
      }
      // The images go to the session's blobs, never into the payload; their names and
      // the text go through the same scrub.
      const text = scrub(
        await resultText(
          (result.content ?? []) as { type: string; text?: string; data?: unknown; mimeType?: unknown }[],
          deps.blobs && session ? { session, blobs: deps.blobs } : undefined,
        ),
      )
      await record(result.isError ? 'error' : 'ok')
      if (result.isError) throw ApplicationFailure.nonRetryable(text, TOOL_ERROR)
      return text
    })
  }
}
