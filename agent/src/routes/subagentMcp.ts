import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import type { Hono } from 'hono'
import type { Sql } from 'postgres'
import type { AuditLog } from '../audit/log.js'
import type { Tier } from '../auth/principal.js'
import { inBlockList, isLoopbackPeer } from '../http/origins.js'
import type { Owner } from '../sessions/protocol.js'
import { ownerDigest, type SubagentClaim, verifySubagentToken } from '../temporal/subagentToken.js'
import { type SessionOwners, turnTiers } from '../temporal/toolActivities.js'
import { turnPrincipal } from '../tools/harness.js'
import { effectiveTool, errorResult, parsedOrRaw, runToolWithOutcome, type Tool, type ToolServices } from '../tools/registry.js'
import { subagentPeers } from './durableSubagents.js'
import type { RouteModule } from './module.js'

// The ScadBuddy tools of a durable segment's subagents (#2320). The Temporal plugin
// denies every durable tool to a subagent (its MAIN_AGENT_ONLY hint): a subagent cannot
// pause the segment, so it cannot run an activity or wait for a human. A classic turn's
// subagents have ScadBuddy's tools through the in-process `scadbuddy` server, so a
// durable segment gives its Claude Code this loopback server under the same name: the
// subagents' tool names, `mcp__scadbuddy__<tool>`, are a classic turn's.
//
// Who calls: a loopback peer (an agent-durable container in the pod), or a peer in the
// ranges Settings names (durableSubagents.ts: an agent-durable Deployment of its own), never
// through a proxy, and only with a token the segment runner minted for one session, turn
// and owner (temporal/subagentToken.ts).
//
// Every call is decided as the main path decides a durable tool's (toolActivities.ts
// `runAsActivity`), from the session as it is when the call runs, never from the token:
// the session must still be durable, running the token's turn, and owned by the token's
// owner (a handoff, an interrupt or the turn's end refuses the token); the call runs as
// that owner with the tiers the turn's sender holds while it still owns the session, cut
// to what its credential holds now (`turnTiers`, PR #2323); and Settings → Tools & plugins applies
// as the call runs (`effectiveTool`). A call that is outward (by its tier now, or because
// it needs an approval) is refused: nothing in a subagent can wait for a human, so the
// main agent makes it, where it parks for approval.

// Outside /api/v1/ai/, the prefix the ingress routes to the agent (docs/ai/operating.md §1.1): no
// ingress or dev proxy reaches it, on top of the loopback-peer check below.
export const SUBAGENT_MCP_PATH = '/internal/durable/subagent-mcp'
export const SUBAGENT_SERVER = 'scadbuddy'

/** What a subagent sees for an outward call. */
export const ASK_THE_MAIN_AGENT =
  'Not an error: this call needs a human approval, and a subagent cannot wait for one. Finish and ' +
  'report back what you want done; the main agent makes the call, and it parks for approval there.'

/** What a caller sees once its token's turn, owner or session no longer holds. */
export const TURN_OVER = 'the token is unknown or expired, or its turn is over or the session changed hands'

export type SubagentMcpDeps = {
  /** The session's current owner and its turn's sender tiers (toolActivities.ts `PgSessionOwners`). */
  sessions: SessionOwners
  /** Whether the session is durable and running `turn` now. */
  running(session: string, turn: string): Promise<boolean>
  /** What a bearer token holds now (auth/tokens.ts `liveTokenTiers`), as the main path cuts a turn's tiers. */
  currentTiers?: ((owner: Owner) => Promise<readonly Tier[] | undefined>) | undefined
  /** The HMAC keys a token may verify under: the KEK's, and the previous KEK's during a rotation. */
  keys: () => readonly Buffer[]
  tools: readonly Tool[]
  services: ToolServices
  audit?: AuditLog | undefined
}

declare module '../app.js' {
  interface AppDeps {
    subagentMcp?: SubagentMcpDeps | undefined
  }
}

/** `running` over Postgres: on that turn, and running or parked. */
export function pgRunning(sql: Sql): SubagentMcpDeps['running'] {
  return async (session, turn) => {
    const rows = await sql`
      SELECT 1 FROM ai_sessions
      WHERE id = ${session} AND durable_turn_id = ${turn}
        AND status IN ('running', 'waiting_approval', 'waiting_input')`
    return rows.length === 1
  }
}

type Caller = { owner: Owner; tiers: readonly Tier[] | undefined }

/** Who a claim's call runs as now, or undefined when the claim no longer holds. */
export async function callerOf(deps: SubagentMcpDeps, claim: SubagentClaim): Promise<Caller | undefined> {
  if (!(await deps.running(claim.session, claim.turn))) return undefined
  const found = await deps.sessions.ownerOf(claim.session)
  if (!found) return undefined
  const { turnTiers: sent, ...owner } = found
  if (ownerDigest(owner) !== claim.owner) return undefined
  return { owner, tiers: await turnTiers(owner, sent ?? undefined, deps) }
}

/** The MCP server for one request: the tools, each call checked and run as the session runs now. */
export function subagentServer(deps: SubagentMcpDeps, claim: SubagentClaim): McpServer {
  const server = new McpServer({ name: SUBAGENT_SERVER, version: '1' })
  const lookup = (name: string) => deps.tools.find((t) => t.name === name)
  for (const tool of deps.tools) {
    server.registerTool(
      tool.name,
      { description: tool.description, inputSchema: tool.shape, annotations: tool.annotations },
      async (args, extra): Promise<CallToolResult> => {
        const caller = await callerOf(deps, claim)
        if (!caller) return errorResult(TURN_OVER)
        const { owner } = caller
        const effective = await effectiveTool(tool, deps.services)
        const startedAt = new Date()
        const record = async (outcome: 'ok' | 'error' | 'refused' | 'denied', detail?: string) => {
          const parsed = parsedOrRaw(tool, args)
          await deps.audit?.record({
            kind: 'tool_call',
            action: tool.name,
            surface: 'harness',
            actor: owner,
            sessionId: claim.session,
            tier: effective.risk,
            inputHash: deps.audit.hash(tool.name, parsed),
            inputSummary: deps.audit.summarise(tool.name, parsed),
            outcome,
            detail: detail === undefined ? 'a subagent' : `a subagent: ${detail}`,
            startedAt,
            finishedAt: new Date(),
          })
        }
        if (effective.disabled) {
          const message = `${tool.name} is disabled in Settings → Tools & plugins; the user can turn it back on there`
          await record('refused', message)
          return errorResult(message)
        }
        if (effective.gated || effective.risk === 'outward') {
          await record('refused', 'outward')
          return errorResult(ASK_THE_MAIN_AGENT)
        }
        const run = await runToolWithOutcome(tool, args, {
          ...deps.services,
          ...(deps.services.browser ? { browser: deps.services.browser.forSession(claim.session) } : {}),
          principal: turnPrincipal(owner, caller.tiers ? { tiers: [...caller.tiers] } : undefined),
          session: claim.session,
          progress: async () => {},
          signal: extra.signal,
          lookup,
          effective,
        })
        await record(run.outcome, run.detail)
        return run.result
      },
    )
  }
  return server
}

function bearerOf(header: string | undefined): string | undefined {
  return header ? /^Bearer\s+(\S+)\s*$/i.exec(header)?.[1] : undefined
}

export const route: RouteModule = {
  register(app: Hono, deps) {
    const sub = deps.subagentMcp
    app.all(SUBAGENT_MCP_PATH, async (c) => {
      // Never from the ingress: agent-durable on the pod's loopback or from a range
      // Settings names, and never through a proxy that names another client.
      if (!sub || c.req.header('x-forwarded-for') !== undefined) return c.json({ error: 'not found' }, 404)
      const peer = deps.remoteAddress(c)
      if (!isLoopbackPeer(peer)) {
        const peers = await subagentPeers(deps.settings)
        if (!peers || !inBlockList(peers, peer)) return c.json({ error: 'not found' }, 404)
      }
      const claim = verifySubagentToken(bearerOf(c.req.header('authorization')) ?? '', sub.keys())
      if (!claim || !(await callerOf(sub, claim))) return c.json({ error: TURN_OVER }, 401)
      // Stateless: each request gets a server of its own, so no MCP session outlives its turn.
      const server = subagentServer(sub, claim)
      const transport = new WebStandardStreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true })
      await server.connect(transport)
      try {
        return await transport.handleRequest(c.req.raw)
      } finally {
        await server.close().catch(() => {})
      }
    })
  },
}
