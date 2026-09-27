import { randomUUID } from 'node:crypto'
import type { AuthInfo } from '@modelcontextprotocol/sdk/server/auth/types.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import type { Context, Hono } from 'hono'
import { authenticate, checkOrigin, checkTransport, type McpAuthSettings } from '../auth/authenticate.js'
import type { TokenStore } from '../auth/tokens.js'
import { createExternalServer } from '../tools/projections.js'
import type { Tool, ToolServices } from '../tools/registry.js'
import { BoundedEventStore } from './eventStore.js'

// `/mcp`: the external projection over the MCP Streamable HTTP transport
// (spec D5, §8.3–§8.4; https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
// POST carries requests and is answered as SSE when a call streams progress;
// GET opens the server→client stream (resource notifications, #264); DELETE
// ends the session. Every request, on every method, passes the same gates in
// this order: HTTPS → Origin → auth mode → principal; only then does the MCP
// SDK's web-standard transport see it.

export type McpEndpointDeps = {
  tools: readonly Tool[]
  services: ToolServices
  tokens: TokenStore
  /** Read per request, so a Settings change (#255) applies without a restart. */
  authSettings: () => McpAuthSettings | Promise<McpAuthSettings>
  clientAddress: (c: Context) => string | undefined
  maxSessions?: number
  idleSessionMs?: number
}

type Session = {
  transport: WebStandardStreamableHTTPServerTransport
  server: McpServer
  principalId: string
  lastSeen: number
}

/** The socket's peer address under @hono/node-server, which passes `incoming` as the env binding. */
export function nodeClientAddress(c: Context): string | undefined {
  const env = c.env as { incoming?: { socket?: { remoteAddress?: string } } } | undefined
  return env?.incoming?.socket?.remoteAddress
}

function jsonRpcError(status: number, code: number, message: string): Response {
  return Response.json({ jsonrpc: '2.0', error: { code, message }, id: null }, { status })
}

export function mountMcp(app: Hono, deps: McpEndpointDeps): { sessions: () => number; close: () => Promise<void> } {
  const sessions = new Map<string, Session>()
  const maxSessions = deps.maxSessions ?? 200
  const idleSessionMs = deps.idleSessionMs ?? 60 * 60_000

  async function end(id: string): Promise<void> {
    const session = sessions.get(id)
    sessions.delete(id)
    await session?.server.close().catch(() => {})
  }

  async function sweep(now: number): Promise<void> {
    for (const [id, session] of sessions) {
      if (now - session.lastSeen > idleSessionMs) await end(id)
    }
  }

  app.all('/mcp', async (c) => {
    const request = c.req.raw
    const clientAddress = deps.clientAddress(c)

    const insecure = checkTransport(request, clientAddress)
    if (insecure) return insecure
    const settings = await deps.authSettings()
    const badOrigin = checkOrigin(request, clientAddress, settings)
    if (badOrigin) return badOrigin
    if (!['GET', 'POST', 'DELETE'].includes(request.method)) {
      return new Response(null, { status: 405, headers: { Allow: 'GET, POST, DELETE' } })
    }

    const auth = await authenticate(request, { settings, tokens: deps.tokens, clientAddress })
    if (!auth.ok) return auth.response
    const { principal } = auth
    // The token itself is not carried further; tools only need who is calling.
    const authInfo: AuthInfo = { token: '', clientId: principal.id, scopes: [...principal.tiers], extra: { principal } }

    const sessionId = request.headers.get('mcp-session-id')
    if (sessionId !== null) {
      const session = sessions.get(sessionId)
      if (!session) return jsonRpcError(404, -32001, 'Session not found')
      // A session belongs to whoever opened it; another principal (even a
      // valid one) may not ride on it.
      if (session.principalId !== principal.id) return jsonRpcError(403, -32001, 'Session belongs to another caller')
      session.lastSeen = Date.now()
      return session.transport.handleRequest(request, { authInfo })
    }

    if (request.method !== 'POST') return jsonRpcError(400, -32000, 'Mcp-Session-Id header is required')
    await sweep(Date.now())
    if (sessions.size >= maxSessions) return jsonRpcError(503, -32000, 'Too many MCP sessions; try again later')

    const server = createExternalServer(deps.tools, deps.services)
    const transport: WebStandardStreamableHTTPServerTransport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      eventStore: new BoundedEventStore(),
      onsessioninitialized: (id) => {
        sessions.set(id, { transport, server, principalId: principal.id, lastSeen: Date.now() })
      },
      onsessionclosed: (id) => {
        void end(id)
      },
    })
    await server.connect(transport)
    const response = await transport.handleRequest(request, { authInfo })
    // Not an initialize request: the transport answered 400 and no session exists.
    if (transport.sessionId === undefined) await server.close().catch(() => {})
    return response
  })

  return {
    sessions: () => sessions.size,
    close: async () => {
      await Promise.all([...sessions.keys()].map(end))
    },
  }
}
