import { createServer, type IncomingHttpHeaders, type IncomingMessage, type Server, type ServerResponse } from 'node:http'
import type { AddressInfo } from 'node:net'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js'
import { z } from 'zod'

// A local Streamable HTTP MCP server for the plugin tests (#297): the
// official @modelcontextprotocol/sdk server in stateless mode ("Stateless mode
// - explicitly set session ID to undefined", streamableHttp.d.ts 1.30.1), one
// McpServer per request. It serves a memory-shaped tool set:
//
//   recall  (annotated readOnlyHint: true)  → returns "remembered: <query>"
//   retain                                   → records the text
//   forget  (annotated destructiveHint)      → records the id
//
// Every request's headers and every tool call are recorded.

export type FakeMcp = {
  url: string
  requests: { method: string; path: string; headers: IncomingHttpHeaders }[]
  calls: string[]
  close(): Promise<void>
}

export type FakeMcpOptions = {
  path?: string
  /** More tools, each echoing `<name>:<text>`; for the naming tests (dots, collisions). */
  extraTools?: string[]
  /** Answer a request yourself (redirects, 401s); return true when handled. */
  intercept?: (req: IncomingMessage, res: ServerResponse) => boolean
}

function buildServer(calls: string[], extraTools: readonly string[]): McpServer {
  const server = new McpServer({ name: 'fake-memory', version: '0.0.1' })
  for (const name of extraTools) {
    server.registerTool(name, { description: `Extra tool ${name}`, inputSchema: { text: z.string() } }, ({ text }) => {
      calls.push(`${name}:${text}`)
      return { content: [{ type: 'text', text: `${name} ran` }] }
    })
  }
  server.registerTool(
    'recall',
    {
      description: 'Search memories',
      inputSchema: { query: z.string() },
      annotations: { readOnlyHint: true },
    },
    ({ query }) => {
      calls.push(`recall:${query}`)
      return { content: [{ type: 'text', text: `remembered: ${query}` }] }
    },
  )
  server.registerTool('retain', { description: 'Store a memory', inputSchema: { text: z.string() } }, ({ text }) => {
    calls.push(`retain:${text}`)
    return { content: [{ type: 'text', text: 'stored' }] }
  })
  server.registerTool(
    'forget',
    { description: 'Delete a memory', inputSchema: { id: z.string() }, annotations: { destructiveHint: true } },
    ({ id }) => {
      calls.push(`forget:${id}`)
      return { content: [{ type: 'text', text: 'deleted' }] }
    },
  )
  return server
}

export async function startFakeMcp(options: FakeMcpOptions = {}): Promise<FakeMcp> {
  const path = options.path ?? '/mcp/bank-1/'
  const requests: FakeMcp['requests'] = []
  const calls: string[] = []
  const http: Server = createServer((req, res) => {
    requests.push({ method: req.method ?? 'GET', path: req.url ?? '', headers: req.headers })
    if (options.intercept?.(req, res)) return
    if ((req.url ?? '').split('?')[0] !== path) {
      res.writeHead(404).end()
      return
    }
    if (req.method !== 'POST') {
      // Stateless: no standalone SSE stream (the client treats 405 as "none").
      res.writeHead(405, { allow: 'POST' }).end()
      return
    }
    const server = buildServer(calls, options.extraTools ?? [])
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined })
    res.on('close', () => {
      void transport.close()
      void server.close()
    })
    server
      .connect(transport)
      .then(() => transport.handleRequest(req, res))
      .catch(() => {
        if (!res.headersSent) res.writeHead(500).end()
      })
  })
  await new Promise<void>((resolve) => http.listen(0, '127.0.0.1', resolve))
  const { port } = http.address() as AddressInfo
  return {
    url: `http://127.0.0.1:${port}${path}`,
    requests,
    calls,
    close: () =>
      new Promise<void>((resolve) => {
        http.closeAllConnections()
        http.close(() => resolve())
      }),
  }
}
