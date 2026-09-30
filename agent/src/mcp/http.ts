import { randomBytes } from 'node:crypto'
import type { AuthInfo } from '@modelcontextprotocol/sdk/server/auth/types.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import type { Context, Hono } from 'hono'
import type { AuditLog } from '../audit/log.js'
import {
  authenticate,
  DEFAULT_MCP_AUTH,
  type McpAuthSettings,
  mcpTransportProblem,
  oidcContext,
  type OidcRuntime,
} from '../auth/authenticate.js'
import { type OidcProvider, protectedResourceMetadata, RESOURCE_METADATA_PATH } from '../auth/oidc.js'
import type { Principal } from '../auth/principal.js'
import { FailClosedTokenStore, type TokenStore } from '../auth/tokens.js'
import type { OriginPolicy } from '../http/origins.js'
import { requestFacts, type RemoteAddress } from '../routes/guard.js'
import { installResources } from '../resources/server.js'
import type { ResourceHub } from '../resources/hub.js'
import { createExternalServer } from '../tools/projections.js'
import type { Tool, ToolServices } from '../tools/registry.js'
import { BoundedEventStore } from './eventStore.js'

// `/mcp`: the external projection over the MCP Streamable HTTP transport
// (spec D5, §8.3–§8.4; https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
// POST carries requests and is answered as SSE when a call streams progress;
// GET opens the server→client stream (resource notifications, #264,
// src/resources/); DELETE
// ends the session. Every request, on every method, passes the same gates in
// this order: HTTPS → Origin → auth mode → principal; only then does the MCP
// SDK's web-standard transport see it. HTTPS and Origin come from the one shared
// allowlist (src/http/origins.ts), the same one the credential routes use.
//
// Session ids are credentials here (see `newSessionId`): nothing in this
// module logs them, and nothing added to it may.

export type McpEndpointDeps = {
  tools: readonly Tool[]
  services: ToolServices
  tokens: TokenStore
  /** Every tool call over /mcp is recorded here (#258, audit/log.ts). */
  audit?: AuditLog | undefined
  /**
   * Read per request, so a Settings change (#255) applies without a restart.
   * If it throws (settings unreadable, database blip), the request is handled
   * fail-closed: `bearer` mode with a token store that verifies nothing.
   */
  authSettings: () => McpAuthSettings | Promise<McpAuthSettings>
  /**
   * `oidc` mode (#262): the JWT verifier with its metadata and JWKS caches.
   * Left out, JWTs are refused and only bearer tokens work in that mode.
   */
  oidc?: OidcProvider | undefined
  /**
   * SCADBUDDY_PUBLIC_URL: the resource URI (`<origin>/mcp`) and the metadata
   * URL are made from it, never from the request's Host.
   */
  publicUrl?: string | undefined
  /**
   * The `scadbuddy://` resources and their subscriptions (#264,
   * src/resources/). Left out, the server offers tools only.
   */
  resources?: ResourceHub | undefined
  /** Open sessions across everyone: a backstop (default 200). */
  maxSessions?: number
  /**
   * Open sessions per caller (default 20), so one caller cannot take the
   * global allowance from everyone else. A caller is its token; in `disabled`
   * mode, where every session is its own principal, it is the client address.
   */
  maxSessionsPerCaller?: number
  /** A session with no request for this long is ended (default 1 h). */
  idleSessionMs?: number
  /** How often idle sessions are swept, on a timer of its own (default 1 min). */
  sweepIntervalMs?: number
}

type Session = {
  transport: WebStandardStreamableHTTPServerTransport
  server: McpServer
  /** Stops this session's resource notifications. */
  detach: () => void
  principalId: string
  /** What `maxSessionsPerCaller` counts by. */
  callerKey: string
  lastSeen: number
}

/** What `mountMcp` hands back: the open-session count and a shutdown hook. */
export type McpHandle = { sessions: () => number; close: () => Promise<void> }

function callerKey(principal: Principal): string {
  return principal.kind === 'anonymous' ? `anonymous@${principal.clientIp ?? 'unknown'}` : principal.id
}

/**
 * A new `Mcp-Session-Id`: 256 bits from the OS CSPRNG (`crypto.randomBytes`),
 * base64url so it stays within the visible-ASCII range the transport spec
 * allows. The MCP spec says the id "SHOULD be globally unique and
 * cryptographically secure" (Streamable HTTP, Session Management,
 * https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#session-management),
 * as does the SDK's `sessionIdGenerator` doc. A random UUID has only 122
 * random bits, hence not `randomUUID()`.
 */
export function newSessionId(): string {
  return randomBytes(32).toString('base64url')
}

/**
 * `disabled` mode has no credential, so every caller authenticates as the
 * same `anonymous`. To keep anonymous clients apart, the session id itself is
 * the capability: each anonymous session gets its own principal id,
 * `anonymous:<sessionId>`, and with it its own pending actions and session.
 * Holding the id is what lets a request act in that session, so in
 * `disabled` mode the session id is the ONLY thing separating LAN clients
 * from one another. That follows from the operator's decision to trust the
 * network (spec §8.3); their tier (full access by default) is unchanged.
 */
function sessionPrincipal(principal: Principal, sessionId: string): Principal {
  return principal.kind === 'anonymous' ? { ...principal, id: `anonymous:${sessionId}` } : principal
}

function authInfoFor(principal: Principal): AuthInfo {
  // The token itself is not carried further; tools only need who is calling.
  return { token: '', clientId: principal.id, scopes: [...principal.tiers], extra: { principal } }
}

const FAIL_CLOSED_TOKENS = new FailClosedTokenStore()

function jsonRpcError(status: number, code: number, message: string): Response {
  return Response.json({ jsonrpc: '2.0', error: { code, message }, id: null }, { status })
}

/** What /mcp shares with the rest of the app: the origin allowlist and how to read the peer. */
export type McpHttpContext = { origins: OriginPolicy; remoteAddress: RemoteAddress }

export function mountMcp(
  app: Hono,
  deps: McpEndpointDeps,
  http: McpHttpContext,
): McpHandle {
  const sessions = new Map<string, Session>()
  const maxSessions = deps.maxSessions ?? 200
  const maxPerCaller = deps.maxSessionsPerCaller ?? 20
  const idleSessionMs = deps.idleSessionMs ?? 60 * 60_000
  // Sweeps on a timer, not only when a session opens, so an abandoned session
  // (dropped without DELETE) is ended promptly even when nobody else connects.
  // unref'd: it never keeps the process alive; `close` stops it.
  const sweeper = setInterval(() => void sweep(Date.now()), deps.sweepIntervalMs ?? 60_000)
  sweeper.unref()

  async function end(id: string): Promise<void> {
    const session = sessions.get(id)
    sessions.delete(id)
    session?.detach()
    await session?.server.close().catch(() => {})
  }

  async function sweep(now: number): Promise<void> {
    for (const [id, session] of sessions) {
      if (now - session.lastSeen > idleSessionMs) await end(id)
    }
  }

  /** The configured auth, or fail-closed bearer when it cannot be read. */
  async function resolveAuth(): Promise<{ settings: McpAuthSettings; tokens: TokenStore }> {
    try {
      return { settings: await deps.authSettings(), tokens: deps.tokens }
    } catch {
      return { settings: { ...DEFAULT_MCP_AUTH, mode: 'bearer' }, tokens: FAIL_CLOSED_TOKENS }
    }
  }

  const oidc: OidcRuntime | undefined = deps.oidc ? { provider: deps.oidc, publicUrl: deps.publicUrl } : undefined

  // RFC 9728 Protected Resource Metadata for /mcp: at the well-known root,
  // which the 401 challenge names, and at the path-inserted form of §3.1
  // (`/.well-known/oauth-protected-resource/mcp`) that clients also probe.
  // Served only while `oidc` mode can run; in `bearer` and `disabled` mode
  // there is no authorization server to name, so both are 404 (#262). The
  // document is public, like the IdP's own metadata.
  const metadata = async (c: Context) => {
    let settings: McpAuthSettings
    try {
      settings = await deps.authSettings()
    } catch {
      return c.json({ error: 'the MCP auth settings cannot be read' }, 503)
    }
    const ctx = oidcContext(settings, oidc)
    if (!ctx || !deps.publicUrl) return c.json({ error: 'OAuth is not enabled for /mcp' }, 404)
    c.header('Cache-Control', 'max-age=300')
    return c.json(protectedResourceMetadata(ctx.config, deps.publicUrl))
  }
  app.get(RESOURCE_METADATA_PATH, metadata)
  app.get(`${RESOURCE_METADATA_PATH}/mcp`, metadata)

  app.all('/mcp', async (c) => {
    const request = c.req.raw
    const facts = requestFacts(c, http.remoteAddress)
    const clientAddress = facts.peer

    const refused = mcpTransportProblem(facts, http.origins, request.url)
    if (refused) return refused
    const { settings, tokens } = await resolveAuth()
    if (!['GET', 'POST', 'DELETE'].includes(request.method)) {
      return new Response(null, { status: 405, headers: { Allow: 'GET, POST, DELETE' } })
    }

    const auth = await authenticate(request, { settings, tokens, clientAddress, oidc })
    if (!auth.ok) return auth.response

    const sessionId = request.headers.get('mcp-session-id')
    if (sessionId !== null) {
      const session = sessions.get(sessionId)
      if (!session) return jsonRpcError(404, -32001, 'Session not found')
      const principal = sessionPrincipal(auth.principal, sessionId)
      // A session belongs to whoever opened it: the same token for bearer
      // callers, the holder of the session id for anonymous ones. Another
      // principal, even a valid one, may not ride on it.
      if (session.principalId !== principal.id) return jsonRpcError(403, -32001, 'Session belongs to another caller')
      session.lastSeen = Date.now()
      return session.transport.handleRequest(request, { authInfo: authInfoFor(principal) })
    }

    if (request.method !== 'POST') return jsonRpcError(400, -32000, 'Mcp-Session-Id header is required')
    await sweep(Date.now())
    const key = callerKey(auth.principal)
    const own = [...sessions.values()].filter((s) => s.callerKey === key).length
    if (own >= maxPerCaller) {
      return jsonRpcError(
        429,
        -32000,
        `This caller already has ${own} open MCP sessions (the limit is ${maxPerCaller}); end one with DELETE /mcp`,
      )
    }
    if (sessions.size >= maxSessions) return jsonRpcError(503, -32000, 'Too many MCP sessions; try again later')

    // Minted before the transport so the anonymous principal can carry it
    // from the initialize request on.
    const id = newSessionId()
    const principal = sessionPrincipal(auth.principal, id)
    const server = createExternalServer(deps.tools, deps.services, deps.audit)
    const { detach } = deps.resources
      ? installResources(server, { tools: deps.tools, services: deps.services, hub: deps.resources, audit: deps.audit })
      : { detach: () => {} }
    const transport: WebStandardStreamableHTTPServerTransport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: () => id,
      eventStore: new BoundedEventStore(),
      onsessioninitialized: (sid) => {
        sessions.set(sid, { transport, server, detach, principalId: principal.id, callerKey: key, lastSeen: Date.now() })
      },
      onsessionclosed: (sid) => {
        void end(sid)
      },
    })
    await server.connect(transport)
    const response = await transport.handleRequest(request, { authInfo: authInfoFor(principal) })
    // Not an initialize request: the transport answered 400 and no session exists.
    if (transport.sessionId === undefined) {
      detach()
      await server.close().catch(() => {})
    }
    return response
  })

  return {
    sessions: () => sessions.size,
    close: async () => {
      clearInterval(sweeper)
      await Promise.all([...sessions.keys()].map(end))
    },
  }
}
