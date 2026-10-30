import { Context } from '@temporalio/activity'
import { ApplicationFailure, CancelledFailure } from '@temporalio/common'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { screenedText } from '../browser/origins.js'
import type { Transport } from '@modelcontextprotocol/sdk/shared/transport.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { AuditLog } from '../audit/log.js'
import { CALL_ID_META } from '../builtins/callId.js'
import type { Tier } from '../auth/principal.js'
import { durableRequestId, flowRequestId } from '../gate/ids.js'
import { GUARD_DEADLINE_MS, type GuardVerdict, judgeInput, type RiskTier, tierAfter, within as withinMs } from '../harness/permissions.js'
import { harnessToolName } from '../plugins/registry.js'
import { toolOffReason } from '../access/items.js'
import { redact } from '../secrets.js'
import type { Owner } from '../sessions/protocol.js'
import { turnPrincipal } from '../tools/harness.js'
import type { FlowBlobStore } from './flowBlobs.js'
import { type FlowRuns, flowOf } from './flowRuns.js'
import { endClient, type KeptClient, type KeptConnection, neverSent, type SessionClients, sessionGone, transportBroken } from './sessionClients.js'
import {
  type ApprovalRecords,
  durableSession,
  flowResult,
  GRANT_REVOKED,
  NOT_APPROVED,
  type ResultBlobs,
  resultText,
  sessionOf,
  type SessionOwners,
  TOOL_ERROR,
  turnTiers,
  UNKNOWN_FLOW,
} from './toolActivities.js'

// The rest of a turn's tools in a durable session (#2320). A classic turn hands Claude
// Code more MCP servers than ScadBuddy's own (sessions/manager.ts `runTurn`): the remote
// plugins through the loopback forwarder and `http_request`. A
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
// session, and it lists nothing.

export const TURN_TOOLS_ACTIVITY = 'turn_tools'
export const TURN_TOOL_TIER_ACTIVITY = 'turn_tool_tier'
/**
 * Ends the session's kept MCP clients on this replica (#2581): DurableSession runs it on
 * the replica its plugin calls were pinned to when the session ends.
 */
export const TURN_TOOLS_CLOSE_ACTIVITY = 'turn_tools_close'
/** The activity type a turn tool's call runs as: `turn:<server>__<tool>`. */
export const TURN_TOOL_PREFIX = 'turn:'
const SEPARATOR = '__'
/**
 * The longest turn-tool name the durable session can declare: the Temporal Claude
 * Agent SDK refuses longer ones (`_check_tool_name`, 1 to 50 of [A-Za-z0-9_-]), and one
 * refused there failed every workflow task of its session.
 */
export const MAX_TURN_TOOL_NAME = 50
/** How long one server's tool listing may take before it is left out of the turn. */
const LIST_TIMEOUT_MS = 20_000
/** How long a keyed server's listing is reused (TurnServer.listingKey). */
export const LISTING_KEPT_MS = 30_000

/** One MCP server a turn offers beside ScadBuddy's own tools. */
export type TurnServer = {
  /** Its name, as the classic turn's `mcpServers` key. */
  name: string
  /** A client on it, for one listing or one call; the caller closes it. */
  connect(): Promise<Client>
  /** The tier of a call by its harness name (`mcp__<server>__<tool>`) and input, as the permission seam resolves it. */
  tierOf(harnessName: string, input: unknown): RiskTier
  /**
   * The call's tier in code, whatever its access says (#2171): what says whether a lost
   * answer may be asked again. Absent: `tierOf`.
   */
  declaredTierOf?(harnessName: string, input: unknown): RiskTier
  /**
   * Judges a call by its input before its tier, as a classic turn's input guard does
   * (harness/permissions.ts; the headless browser's lists, #982): `deny` refuses it,
   * `outward` or `allow` move its tier (`tierAfter`).
   */
  guard?(harnessName: string, input: unknown): Promise<GuardVerdict | undefined>
  /**
   * Why a call of the tool (its harness name) is refused now, read at each tier check and
   * each call (#4757: a package server or a served built-in switched off in Settings → Tools
   * & plugins), or undefined. A read that fails refuses the call, at the tier check (so it
   * never parks) and at the call.
   */
  refusal?(harnessName: string): Promise<string | undefined>
  /**
   * Known, but offering nothing now (#4757 final review M2: a package server switched off):
   * left out of the listing, never connected to, its calls refused by `refusal`.
   */
  unlisted?: true
  /**
   * Its tools set off in Settings → Tools & plugins, by their own (bare) names (#4757: a
   * local package server's): left out of the listing and refused on call, as the
   * forwarder does for an http server's disabled tools.
   */
  offTools?: ReadonlySet<string>
  /** The text the model reads instead of a call's result (#982: a page the lists do not let it read), or undefined. */
  screen?(harnessName: string, input: unknown, text: string): Promise<string | undefined>
  /**
   * Whether some call of the tool could be outward, whatever its input. Listed as
   * `may_park`: only a tool listed so is ever asked about, so a tool whose tier depends
   * on its input (http_request's method) must say yes.
   * Absent: every tool may (fail toward asking).
   */
  mayPark?(harnessName: string): boolean
  /**
   * The tool's tier when it does not depend on the call's input, for the session's
   * transcript to show (`TurnToolEntry.tier`); undefined: it depends on the input.
   */
  fixedTier?(harnessName: string): RiskTier | undefined
  /**
   * Whether its calls must run on the replica that listed it (`TurnToolsDeps.hostQueue`):
   * a server whose state lives in this process or on this pod's disk (http_request's
   * saved bodies).
   */
  pinned?: boolean
  /**
   * What its tool list depends on (a remote plugin's name and the tools Settings disabled):
   * with one, a listing is kept for LISTING_KEPT_MS, so a tier check and the call after it
   * do not list the plugin again (#2497). The forwarder still refuses a disabled tool's call.
   */
  listingKey?: string
  /**
   * A remote plugin whose MCP session a durable session keeps from one call, and one
   * turn, to the next (#2581, sessionClients.ts), as a classic turn's Claude Code keeps
   * it for the turn: a stateful plugin (Playwright MCP) keeps its page. `fingerprint`
   * says what the session depends on (URL, header, address, tool settings): a change
   * starts a new one. `connect` opens a client that outlives the turn's servers (its own
   * forwarder route, freed by `release` once the client is ended). With
   * `TurnToolsDeps.clients`, its calls are pinned to the replica that holds the client.
   */
  keep?: { fingerprint: string; connect(): Promise<KeptConnection> }
  /**
   * Whether each call carries its tool_use id in `_meta` (CALL_ID_META), so a call its
   * activity retries after a lost answer is known for the same call (#3201). Only a
   * server of our own: a remote one is never told it.
   */
  callIds?: boolean
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
  /** Said to the model in the turn's system prompt (the built-ins offered as turn tools, #2393). */
  notes?: string[]
  release(): void | Promise<void>
}

export type TurnContext = {
  /** The session, or `flow-<run id>` for a flow run's call (then `flow` is set). */
  session: string
  /** The flow run a call comes from: it has no session. */
  flow?: string
  /** The durable turn running now (`ai_sessions.durable_turn_id`), for audit rows; none for a flow. */
  turnId?: string
  owner: Owner
  signal: AbortSignal
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
  /** How long one server's listing may take (LIST_TIMEOUT_MS by default). */
  listTimeoutMs?: number | undefined
  /** Where a call's result images are kept for `view_tool_images` (toolActivities.ts `resultText`). */
  blobs?: ResultBlobs | undefined
  /** Where a flow's call keeps the images it returned, as its ScadBuddy tools' do (#2578, flowBlobs.ts). */
  flowBlobs?: FlowBlobStore | undefined
  /** This replica's kept MCP clients, per durable session and plugin (#2581); without it every call opens its own. */
  clients?: SessionClients<ListedTools> | undefined
}

/** What DurableSession declares for one turn tool. */
export type TurnToolEntry = {
  name: string
  description: string
  input_schema: Record<string, unknown>
  /** Whether a call may be outward: it then parks unless `turn_tool_tier` says otherwise, and runs at most once. */
  may_park: boolean
  /** Its tier when that does not depend on the input, as the transcript labels its calls; absent: it does. */
  tier?: RiskTier
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
  /**
   * Its skills and subagents (`skill:<name>`, `subagent:<name>`) and `PackageItems.aliases`,
   * read from the loaded pin, the off ones too (#4757): the segment runner's call rule
   * (agent-durable access.py `off_call_item`) refuses a Skill or Agent call naming an off one.
   * The aliases are keyed by the whole frontmatter names of the loaded pin (`PackageItems.declared`),
   * as the classic turn's call rule has them: for matching only, never shown.
   */
  items: string[]
  aliases: Record<string, string[]>
  /** The built-ins it names, before `builtin_tools` leaves out the switched-off ones: a Skill, Agent or Task call takes their access per call, an outward one parked at the gate. */
  named_builtins: string[]
}

export type TurnToolsResult = {
  tools: TurnToolEntry[]
  packages: TurnPackage[]
  /** Appended to the turn's system prompt (TurnServers.notes); absent when there is nothing to say. */
  system_note?: string
}

/** A client on an in-process MCP server (an SDK server's `instance`), made fresh for each use. */
export async function connectInProcess(create: () => { connect(transport: Transport): Promise<void> }): Promise<Client> {
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

/** A client for one use, ended after it with the session DELETE (#2581), not left on the server. */
async function withClient<T>(server: TurnServer, use: (client: Client) => Promise<T>): Promise<T> {
  const client = await server.connect()
  try {
    return await use(client)
  } finally {
    await endClient(client)
  }
}

/**
 * The server's tools as {original name, harness name}, as Claude Code would list them.
 * Tools whose harness names collide are left out, as a classic turn hides them
 * (plugins/forwarder.ts), so a name never stands for two tools.
 */
export type ListedTools = { tool: Awaited<ReturnType<Client['listTools']>>['tools'][number]; name: string }[]
type Listed = ListedTools

const kept = new Map<string, { at: number; tools: Listed }>()

function named(tools: Awaited<ReturnType<Client['listTools']>>['tools']): Listed {
  const all = tools.map((tool) => ({ tool, name: harnessToolName(tool.name) }))
  const seen = new Map<string, number>()
  for (const { name } of all) seen.set(name, (seen.get(name) ?? 0) + 1)
  return all.filter(({ name }) => seen.get(name) === 1)
}

async function listed(server: TurnServer, signal: AbortSignal, reuse = false): Promise<Listed> {
  const key = server.listingKey === undefined ? undefined : `${server.name}\0${server.listingKey}`
  const hit = key === undefined ? undefined : kept.get(key)
  if (reuse && hit && Date.now() - hit.at < LISTING_KEPT_MS) return hit.tools
  const tools = await withClient(server, async (client) => named((await client.listTools(undefined, { signal })).tools))
  if (key !== undefined) {
    for (const [k, v] of kept) if (Date.now() - v.at >= LISTING_KEPT_MS) kept.delete(k)
    kept.set(key, { at: Date.now(), tools })
  }
  return tools
}

/** A durable session's kept client on a server (#2581): none for a flow run, a server that keeps none, or without `clients`. */
type KeptTarget = {
  clients: SessionClients<Listed>
  session: string
  name: string
  keep: NonNullable<TurnServer['keep']>
}

function keptTarget(deps: TurnToolsDeps, context: Pick<TurnContext, 'session' | 'flow'>, server: TurnServer): KeptTarget | undefined {
  if (!deps.clients || !server.keep || context.flow !== undefined) return undefined
  return { clients: deps.clients, session: context.session, name: server.name, keep: server.keep }
}

/** Whether a server's calls run on this replica's own queue: its state lives here. */
function pinned(deps: TurnToolsDeps, server: TurnServer): boolean {
  return Boolean(deps.hostQueue && (server.pinned || (server.keep && deps.clients)))
}

function openKept(t: KeptTarget): Promise<KeptClient<Listed>> {
  return t.clients.open(t.session, t.name, t.keep.fingerprint, () => t.keep.connect())
}

/** Lists the server on the session's kept client, keeping the list with it; a listing has no side effects, so a broken session is reconnected once. */
async function listKept(t: KeptTarget, signal: AbortSignal): Promise<{ client: KeptClient<Listed>; tools: Listed }> {
  for (let attempt = 0; ; attempt++) {
    const client = await openKept(t)
    try {
      const { tools } = await t.clients.use(client, (c) => c.listTools(undefined, { signal }))
      client.tools = named(tools)
      return { client, tools: client.tools }
    } catch (err) {
      if (signal.aborted || !(transportBroken(err) || neverSent(err))) throw err
      await t.clients.drop(client)
      if (attempt > 0) throw err
    }
  }
}

/** What a call on a kept client says once its MCP session was reset. */
const resetNote = (server: string) =>
  `ScadBuddy: the MCP session with plugin ${server} was reset, so state from earlier calls (such as an open browser page) is gone`

/**
 * A call on the session's kept client. If the server no longer knows the session (404),
 * or the request could not be sent at all, the call never ran: it is retried once on a
 * new session, and `reset` says so. Any other broken transport may have run it, so it is
 * never retried; the client is ended and the error says the session was reset.
 */
async function callKept(
  t: KeptTarget,
  first: KeptClient<Listed>,
  call: (client: Client) => ReturnType<Client['callTool']>,
  signal: AbortSignal,
): Promise<{ result: Awaited<ReturnType<Client['callTool']>>; reset?: string }> {
  try {
    return { result: await t.clients.use(first, call) }
  } catch (err) {
    if (signal.aborted || !(transportBroken(err) || neverSent(err))) throw err
    await t.clients.drop(first)
    const reason = (err as Error).message
    if (!sessionGone(err) && !neverSent(err)) {
      throw new Error(`${reason}. ${resetNote(t.name)}; the call was not retried, since it may have run`, { cause: err })
    }
    const again = await openKept(t)
    try {
      return { result: await t.clients.use(again, call), reset: `${resetNote(t.name)} (${reason}); this call ran on a new one.` }
    } catch (retried) {
      if (!signal.aborted && transportBroken(retried)) await t.clients.drop(again)
      throw new Error(`${(retried as Error).message}. ${resetNote(t.name)}, and the call failed again on a new one`, {
        cause: retried,
      })
    }
  }
}

/** `promise`, or the signal's reason once it aborts (a connect that stalls takes no signal). */
function within<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => reject(signal.reason as Error)
    if (signal.aborted) abort()
    signal.addEventListener('abort', abort, { once: true })
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort))
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

/** Why a turn tool's server is not in the turn's build (switched off, removed, or its name now taken). */
const goneReason = (name: string) => `${name} is not offered any more: its plugin or setting was turned off`

/** The server a turn tool's name names now, or undefined. */
function findServer(set: TurnServers, name: string) {
  const parts = split(name)
  const server = parts && set.servers.find((s) => s.name === parts.server)
  return parts && server ? { parts, server } : undefined
}

/** The server a turn tool's name names now, or a non-retryable refusal saying why not. */
function serverOf(set: TurnServers, name: string) {
  const found = findServer(set, name)
  if (!found) throw ApplicationFailure.nonRetryable(goneReason(name), TOOL_ERROR)
  return found
}

/** Why a tool its server is in the build for no longer lists is refused. */
const unlistedReason = (name: string, server: string) => `${name} is not offered any more by ${server}`

/**
 * The tool a turn tool's name names now, or a non-retryable refusal saying why not. A
 * call (`use: 'call'`) on a kept server lists it on the session's kept client, which it
 * then runs on; a tier check uses that client's list only if this replica holds one, so
 * a replica the calls are not pinned to keeps none. `tool` is undefined where the server
 * no longer lists it.
 */
async function lookup(
  deps: TurnToolsDeps,
  context: Pick<TurnContext, 'session' | 'flow'>,
  set: TurnServers,
  name: string,
  signal: AbortSignal,
  use: 'call' | 'tier',
) {
  const { parts, server } = serverOf(set, name)
  const target = keptTarget(deps, context, server)
  let client: KeptClient<Listed> | undefined
  let tools: Listed
  if (target && use === 'call') {
    client = await openKept(target)
    if (client.tools) tools = client.tools
    else ({ client, tools } = await listKept(target, signal))
  } else {
    const held = target && target.clients.peek(target.session, target.name, target.keep.fingerprint)
    tools = held?.tools ?? (await listed(server, signal, true))
  }
  const found = tools.find((t) => t.name === parts.tool)
  return { server, tool: found?.tool, harnessName: `mcp__${server.name}__${parts.tool}`, client }
}

/** `lookup`, refused (non-retryable) where the server no longer lists the tool. */
async function resolve(...args: Parameters<typeof lookup>) {
  const found = await lookup(...args)
  const { tool } = found
  if (!tool) throw ApplicationFailure.nonRetryable(unlistedReason(args[3], found.server.name), TOOL_ERROR)
  return { ...found, tool }
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
  /** Whether a call approved by a grant now revoked or expired would otherwise have run (#2871). */
  lapsed(requestId: string, binding: { inputHash: string } | undefined): Promise<boolean>
}

async function turnCaller(deps: TurnToolsDeps, name: string, toolUseId: string): Promise<TurnCaller> {
  const { workflowExecution } = Context.current().info
  const runId = workflowExecution?.runId ?? ''
  const flow = flowOf(workflowExecution?.workflowId)
  if (flow !== undefined) {
    // #2258: without it the request id matches no decision (toolActivities.ts flowCaller).
    if (!runId) {
      throw ApplicationFailure.nonRetryable(`${name}: flow run ${flow}'s call carries no workflow execution run id`, UNKNOWN_FLOW)
    }
    const flows = deps.flows
    // A lookup that throws (the database is away) is left to the activity's retries.
    const owner = flows ? await flows.startedBy(flow, runId) : undefined
    if (!flows || owner === undefined) {
      throw ApplicationFailure.nonRetryable(`${name}: no open flow run ${flow} is recorded for this execution`, UNKNOWN_FLOW)
    }
    return {
      context: { session: `flow-${flow}`, flow, owner },
      owner,
      sessionId: null,
      requestId: flowRequestId(flow, runId, toolUseId),
      // A token starter's run acts with what its token holds now, as toolActivities' flowCaller.
      tiers: owner.kind === 'browser' ? undefined : await deps.currentTiers?.(owner),
      approved: (requestId, binding) => flows.approved(requestId, binding?.inputHash),
      lapsed: async (requestId, binding) => (await flows.lapsed?.(requestId, binding?.inputHash)) ?? false,
    }
  }
  const { session, owner, turnTiers: sent, turnId } = await durableSession(name, deps.sessions)
  return {
    context: { session, owner, ...(turnId ? { turnId } : {}) },
    owner,
    sessionId: session,
    requestId: durableRequestId(session, runId, toolUseId),
    tiers: await turnTiers(owner, sent, deps),
    approved: async (requestId, binding) => (await deps.approvals?.approved(requestId, binding)) ?? false,
    lapsed: async (requestId, binding) => (await deps.approvals?.lapsed?.(requestId, binding)) ?? false,
  }
}

/** `server.screen`'s replacement text; a screen that fails withholds the result (fail closed). */
async function screened(server: TurnServer, harnessName: string, input: unknown, text: string): Promise<string | undefined> {
  try {
    return await withinMs(server.screen!(harnessName, input, text), GUARD_DEADLINE_MS)
  } catch (err) {
    return `ScadBuddy withheld this result: the page it came from could not be checked (${(err as Error).message}).`
  }
}

function inputObject(input: unknown): Record<string, unknown> {
  return typeof input === 'object' && input !== null && !Array.isArray(input) ? (input as Record<string, unknown>) : {}
}

/** `turn_tools` and `turn_tool_tier`, beside the tools on `agent-tools`. */
export function turnToolActivities(deps: TurnToolsDeps): Record<string, (input: unknown) => Promise<unknown>> {
  return {
    [TURN_TOOLS_ACTIVITY]: async (): Promise<TurnToolsResult> => {
      const { session, owner, turnId } = await durableSession(TURN_TOOLS_ACTIVITY, deps.sessions)
      const signal = Context.current().cancellationSignal
      return withServers(deps, { session, owner, signal, listing: true, ...(turnId ? { turnId } : {}) }, async (set) => {
        const scrub = (text: string) => redact(text, set.secrets)
        // Listed side by side, each within LIST_TIMEOUT_MS: a plugin that stalls is
        // reported and left out, never the whole listing's deadline.
        const listings = await Promise.all(
          set.servers.filter((server) => !server.unlisted).map(async (server) => {
            try {
              const bounded = AbortSignal.any([signal, AbortSignal.timeout(deps.listTimeoutMs ?? LIST_TIMEOUT_MS)])
              const target = keptTarget(deps, { session }, server)
              const offered = target ? listKept(target, bounded).then((k) => k.tools) : listed(server, bounded)
              return { server, offered: await within(offered, bounded) }
            } catch (err) {
              if (signal.aborted) throw new CancelledFailure('the listing was cancelled')
              return { server, problem: `plugin ${server.name} is not available in this turn: ${(err as Error).message}` }
            }
          }),
        )
        const tools: TurnToolEntry[] = []
        const tooLong: string[] = []
        for (const { server, offered } of listings) {
          for (const { tool, name } of offered ?? []) {
            if (isOffTool(server, tool.name)) continue
            const turnName = turnToolName(server.name, tool.name)
            const tier = server.fixedTier?.(`mcp__${server.name}__${name}`)
            if (turnName.length > MAX_TURN_TOOL_NAME) {
              tooLong.push(
                `tool ${tool.name} of plugin ${server.name} is not available in this turn: its name ` +
                  `${turnName} is over ${MAX_TURN_TOOL_NAME} characters`,
              )
              continue
            }
            tools.push({
              name: turnName,
              description: tool.description ?? '',
              input_schema: tool.inputSchema as Record<string, unknown>,
              may_park: server.mayPark?.(`mcp__${server.name}__${name}`) ?? true,
              ...(tier ? { tier } : {}),
              ...(pinned(deps, server) ? { task_queue: deps.hostQueue! } : {}),
            })
          }
        }
        // A plugin gone since, or configured differently now, takes its session with it.
        await deps.clients?.retain(session, new Map(set.servers.flatMap((s) => (s.keep ? [[s.name, s.keep.fingerprint] as const] : []))))
        // Said once, after the listing, so a retried attempt does not say it again.
        const problems = [...set.problems, ...listings.flatMap((l) => (l.problem ? [l.problem] : [])), ...tooLong]
        if (problems.length) await deps.report?.(session, problems.map(scrub))
        const notes = set.notes ?? []
        return { tools, packages: set.packages ?? [], ...(notes.length ? { system_note: notes.join('\n\n') } : {}) }
      })
    },
    [TURN_TOOLS_CLOSE_ACTIVITY]: async (): Promise<void> => {
      // The session's own id, from its workflow: its row may be gone (forgotten) by now.
      const session = sessionOf(Context.current().info.workflowExecution?.workflowId)
      if (session !== undefined) await deps.clients?.closeSession(session)
    },
    // `declared` is the call's tier in code (TurnServer.declaredTierOf): a flow retries a call
    // only when it and `tier` are both read (#2171).
    [TURN_TOOL_TIER_ACTIVITY]: async (args: unknown): Promise<{ tier: RiskTier; declared: RiskTier; task_queue?: string; refused?: string }> => {
      const { tool, input } = (args ?? {}) as { tool?: unknown; input?: unknown }
      if (typeof tool !== 'string') throw ApplicationFailure.nonRetryable('turn_tool_tier takes {tool: string, input}', TOOL_ERROR)
      const caller = await turnCaller(deps, TURN_TOOL_TIER_ACTIVITY, '')
      const signal = Context.current().cancellationSignal
      return withServers(deps, { ...caller.context, signal }, async (set) => {
        // As a guard's deny below: not parked, since its call refuses it with the reason. A
        // switch that cannot be read is refused too: an approval could not run the call.
        // Read before the listing. In a durable session a server not in the build (switched
        // off, removed, or its name now another's) is refused the same way (#4757): never
        // parked for an approval its call could not use; its call still rebuilds, re-tiers
        // and needs an approval. A flow, which never parks, is told there is no such tool.
        const present = caller.context.flow === undefined ? findServer(set, tool) : serverOf(set, tool)
        if (!present) return { tier: 'write', declared: 'outward', refused: goneReason(tool) }
        const named = present.server
        // A flow takes its call's queue from here, since it lists nothing (turn_tools).
        const queue = pinned(deps, named) ? { task_queue: deps.hostQueue! } : {}
        const offName = `mcp__${named.name}__${present.parts.tool}`
        const off = await refusalOf(named, offName)
        if (off !== undefined) return { tier: 'write', declared: 'outward', refused: off, ...queue }
        // A tool set off, or one the server no longer lists (#4757 final review I1: an http
        // package server's tool its forwarder now hides), is refused the same way.
        if (isOffTool(named, present.parts.tool)) return { tier: 'write', declared: 'outward', refused: toolOffReason(offName), ...queue }
        const { server, harnessName, tool: found } = await lookup(deps, caller.context, set, tool, signal, 'tier')
        if (!found) {
          if (caller.context.flow !== undefined) throw ApplicationFailure.nonRetryable(unlistedReason(tool, server.name), TOOL_ERROR)
          return { tier: 'write', declared: 'outward', refused: unlistedReason(tool, server.name), ...queue }
        }
        if (isOffTool(server, found.name)) return { tier: 'write', declared: 'outward', refused: toolOffReason(harnessName), ...queue }
        const verdict = server.guard ? await judgeInput(server.guard.bind(server), harnessName, input) : undefined
        // A refused call is not parked, since no approval could run it: its call refuses
        // it with the reason (turnToolActivity). It is answered as not outward; `refused`
        // says why, for diagnostics only (the durable workflow reads `tier`, and the
        // transcript shows the call's own refusal).
        if (verdict && 'deny' in verdict) return { tier: 'write', declared: 'outward', refused: verdict.deny, ...queue }
        const declared = server.declaredTierOf?.(harnessName, input) ?? server.tierOf(harnessName, input)
        return { tier: tierAfter(server.tierOf(harnessName, input), verdict), declared, ...queue }
      })
    },
  }
}

/** Whether the tool (its own name) is set off on the server (`TurnServer.offTools`), by that name or the one Claude Code gives it. */
function isOffTool(server: TurnServer, raw: string): boolean {
  const off = server.offTools
  if (!off || off.size === 0) return false
  const name = harnessToolName(raw)
  return off.has(raw) || [...off].some((d) => harnessToolName(d) === name)
}

/** Why `server` refuses a call of the tool now; a refusal read that fails refuses it too (fail closed). */
async function refusalOf(server: TurnServer, harnessName: string): Promise<string | undefined> {
  try {
    return await server.refusal?.(harnessName)
  } catch {
    return `${server.name}: could not read the server's switch, so the call is refused`
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
      // Retryable, as the SDK's own not-found is: a newer worker's activity during a deploy heals.
      throw ApplicationFailure.retryable(`no activity ${activityType} is served on this queue`, 'NotFoundError')
    }
    const name = activityType.slice(TURN_TOOL_PREFIX.length)
    const toolUseId = activityId.startsWith('tool-') ? activityId.slice('tool-'.length) : activityId
    const caller = await turnCaller(deps, name, toolUseId)
    const { owner, requestId, sessionId: session } = caller
    const principal = turnPrincipal(owner, caller.tiers ? { tiers: [...caller.tiers] } : undefined)
    const fields = inputObject(input)
    const signal = context.cancellationSignal
    return withServers(deps, { ...caller.context, signal }, async (set) => {
      const scrub = (text: string) => redact(text, set.secrets)
      const startedAt = new Date()
      const record = (tier: RiskTier, outcome: 'ok' | 'error' | 'refused', detail?: string) =>
        deps.audit?.record({
          kind: 'tool_call',
          action: name,
          surface: 'harness',
          actor: owner,
          sessionId: session,
          turnId: caller.context.turnId ?? null,
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
      // Its server's switch first: a server switched off is not listed (turnServers.ts).
      const { parts, server: named } = serverOf(set, name)
      const offName = `mcp__${named.name}__${parts.tool}`
      const off = (await refusalOf(named, offName)) ?? (isOffTool(named, parts.tool) ? toolOffReason(offName) : undefined)
      if (off !== undefined) {
        await record(named.tierOf(offName, input), 'refused', off)
        throw ApplicationFailure.nonRetryable(off, TOOL_ERROR)
      }
      const { server, tool, harnessName, client: keptClient } = await resolve(deps, caller.context, set, name, signal, 'call')
      const verdict = server.guard ? await judgeInput(server.guard.bind(server), harnessName, input) : undefined
      const tier = tierAfter(server.tierOf(harnessName, input), verdict)
      const refuse = async (message: string, type: string): Promise<never> => {
        await record(tier, 'refused', message)
        throw ApplicationFailure.nonRetryable(message, type)
      }
      if (isOffTool(server, tool.name)) await refuse(toolOffReason(harnessName), TOOL_ERROR)
      if (verdict && 'deny' in verdict) await refuse(verdict.deny, TOOL_ERROR)
      if (!principal.tiers.includes(tier)) {
        const who = caller.context.flow ? "this flow run's starter" : "this session's turn"
        await refuse(`${name} needs the "${tier}" tier, which ${who} does not hold`, TOOL_ERROR)
      }
      // As runAsActivity: the approval is checked as the call runs, bound to this call's input.
      const binding = deps.audit ? { inputHash: deps.audit.hash(name, fields) } : undefined
      if (tier === 'outward') {
        if (!(await caller.approved(requestId, binding))) {
          // As runAsActivity: a call its grant approved, revoked or expired since, says so.
          if (await caller.lapsed(requestId, binding)) {
            await refuse(`${name}: the run's grant was revoked or expired before this call ran`, GRANT_REVOKED)
          }
          await refuse(`${name} needs an approval, and none is recorded for this call`, NOT_APPROVED)
        }
      }
      const heartbeat = setInterval(() => context.heartbeat(), 10_000)
      let result: Awaited<ReturnType<Client['callTool']>>
      let reset: string | undefined
      const meta = server.callIds ? { _meta: { [CALL_ID_META]: toolUseId } } : {}
      // The activity's own limit, not the MCP client's 60 s default: a WebFetch alone may take that long (#3199).
      const timeout = context.info.startToCloseTimeoutMs
      const callTool = (client: Client) =>
        client.callTool({ name: tool.name, arguments: fields, ...meta }, undefined, { signal, ...(timeout > 0 ? { timeout } : {}) })
      const target = keptTarget(deps, caller.context, server)
      try {
        if (target && keptClient) {
          ;({ result, reset } = await callKept(target, keptClient, callTool, signal))
        } else {
          result = await withClient(server, callTool)
        }
      } catch (err) {
        if (signal.aborted) throw new CancelledFailure('the call was cancelled')
        await record(tier, 'error', (err as Error).message)
        throw ApplicationFailure.nonRetryable(scrub(`${name} failed: ${(err as Error).message}`), TOOL_ERROR)
      } finally {
        clearInterval(heartbeat)
      }
      // The images go to the session's blobs, or a flow's to its run's (#2578), never into
      // the payload; their names and the text go through the same scrub.
      const content = (result.content ?? []) as { type: string; text?: string; data?: unknown; mimeType?: unknown }[]
      // A page the lists do not let the model read is withheld, its images with it, so
      // none is kept (#982). It reads what a classic turn's hook reads (`screenedText`).
      const withheld = server.screen ? await screened(server, harnessName, input, screenedText(content)) : undefined
      if (withheld !== undefined) {
        await record(tier, result.isError ? 'error' : 'ok')
        if (result.isError) throw ApplicationFailure.nonRetryable(withheld, TOOL_ERROR)
        return withheld
      }
      const flow = caller.context.flow
      // An errored call keeps no image: nothing would point at it, yet a flow's would count
      // toward its run's FLOW_IMAGES_MAX.
      const answer =
        flow !== undefined && !result.isError
          ? await flowResult(content, deps.flowBlobs ? { subject: `flow-${flow}`, blobs: deps.flowBlobs } : undefined)
          : await resultText(
              content,
              flow === undefined && deps.blobs && session ? { session, blobs: deps.blobs, toolUseId } : undefined,
            )
      const text = scrub((reset ? `[${reset}]\n\n` : '') + (typeof answer === 'string' ? answer : answer.text))
      await record(tier, result.isError ? 'error' : 'ok')
      if (result.isError) throw ApplicationFailure.nonRetryable(text, TOOL_ERROR)
      return typeof answer === 'string' ? text : { text, images: answer.images }
    })
  }
}
