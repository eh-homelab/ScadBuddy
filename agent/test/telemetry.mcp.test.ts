import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { Client as McpClient } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { WebStandardStreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import { ROOT_CONTEXT, trace } from '@opentelemetry/api'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { harnessPrincipal } from '../src/auth/principal.js'
import { mcpMethodOf } from '../src/mcp/http.js'
import { bindToolContext, tracer, unbindToolContext } from '../src/telemetry/trace.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createHarnessServer } from '../src/tools/projections.js'
import { appFetch, BACKEND, connect, MCP_URL, services, testApp } from './helpers/mcp.js'
import { browser } from './support/sessions.js'
import { flushTracing, PARENT_SPAN_ID, resetTracing, TRACE_ID, TRACEPARENT, testTracing } from './support/tracing.js'

// /mcp continues the caller's trace (spec 2026-10-01 §4): `agent.mcp/<method>`
// per POST, `agent.tool/<name>` per call, and the backend request under it.
// In-process (harness) calls find their span by the tool_use id in `_meta`.

const spans = testTracing()
const seen: (string | null)[] = []
const server = setupServer(
  http.get(`${BACKEND}/api/v1/models`, ({ request }) => {
    seen.push(request.headers.get('traceparent'))
    return HttpResponse.json([])
  }),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
beforeEach(async () => {
  await resetTracing()
  seen.length = 0
})
const clients: Client[] = []
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.close()))
})

const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)

describe('/mcp spans', () => {
  it('a tools/call continues the caller’s traceparent down to the backend request', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}`, traceparent: TRACEPARENT } })
    clients.push(client)
    await client.callTool({ name: 'list_models', arguments: {} })
    await flushTracing()

    expect(named('agent.mcp/initialize')).toHaveLength(1)
    const [call] = named('agent.mcp/tools/call')
    expect(call!.spanContext().traceId).toBe(TRACE_ID)
    expect(call!.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    const [tool] = named('agent.tool/list_models')
    expect(tool!.parentSpanContext?.spanId).toBe(call!.spanContext().spanId)
    expect(tool!.attributes).toMatchObject({ 'scadbuddy.tool': 'list_models', 'scadbuddy.tier': 'read', 'scadbuddy.outcome': 'ok' })
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(tool!.spanContext().spanId)
    expect(seen).toEqual([`00-${TRACE_ID}-${request!.spanContext().spanId}-01`])
  })

  it('names the span after the JSON-RPC method, and only a well-formed one', () => {
    expect(mcpMethodOf({ jsonrpc: '2.0', id: 1, method: 'tools/list' })).toBe('tools/list')
    expect(mcpMethodOf([{ jsonrpc: '2.0', method: 'notifications/initialized' }])).toBe('notifications/initialized')
    expect(mcpMethodOf({ method: 'x y <script>' })).toBe('unknown')
    for (const body of [undefined, null, 'tools/list', [], 7]) expect(mcpMethodOf(body)).toBe('unknown')
    for (const method of ['a1', 'tools/call2', 'x'.repeat(64), 'notifications/made_up']) {
      expect(mcpMethodOf({ jsonrpc: '2.0', id: 1, method })).toBe('unknown')
    }
  })

  it.each([
    ['sampled', TRACEPARENT],
    ['unsampled', TRACEPARENT.replace(/-01$/, '-00')],
  ])('reads a %s POST body once and hands the transport the parsed body', async (_, traceparent) => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const clone = vi.spyOn(Request.prototype, 'clone')
    const handle = vi.spyOn(WebStandardStreamableHTTPServerTransport.prototype, 'handleRequest')
    try {
      const client = await connect(t.app, { headers: { authorization: `Bearer ${token}`, traceparent } })
      clients.push(client)
      await client.callTool({ name: 'list_models', arguments: {} })
      // msw clones the backend request; no /mcp request is cloned.
      expect(clone.mock.contexts.map((r) => new URL((r as Request).url).pathname).filter((p) => p.startsWith('/mcp'))).toEqual([])
      const posts = handle.mock.calls.filter(([request]) => request.method === 'POST')
      expect(posts.map(([, options]) => mcpMethodOf(options?.parsedBody))).toEqual(
        expect.arrayContaining(['initialize', 'notifications/initialized', 'tools/call']),
      )
      expect(posts.every(([, options]) => options?.parsedBody !== undefined)).toBe(true)
    } finally {
      clone.mockRestore()
      handle.mockRestore()
    }
    await flushTracing()
    const mcpSpans = spans.getFinishedSpans().filter((s) => s.name.startsWith('agent.mcp'))
    if (traceparent === TRACEPARENT) expect(mcpSpans.map((s) => s.name)).toContain('agent.mcp/tools/call')
    else expect(mcpSpans).toEqual([])
  })

  it('a body that is not JSON, too large, or the wrong type gets the transport’s own answer', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const post = (body: string, contentType = 'application/json') =>
      appFetch(t.app, {
        headers: { authorization: `Bearer ${token}`, 'content-type': contentType, accept: 'application/json, text/event-stream' },
      })(MCP_URL, { method: 'POST', body })
    const initialize = JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 't', version: '0' } },
    })

    const garbage = await post('not json')
    expect(garbage.status).toBe(400)
    expect(await garbage.json()).toMatchObject({ error: { code: -32700 } })
    const huge = await post(`{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"${'x'.repeat(4 * 1024 * 1024)}"}}`)
    expect(huge.status).toBe(413)
    expect((await post(initialize, 'text/plain')).status).toBe(415)
  })

  it('the mcp span outlives its tool span, whatever the response framing', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
    clients.push(client)
    await client.callTool({ name: 'list_models', arguments: {} })
    await client.close()
    await flushTracing()
    // End order, not end times: each span's clock is anchored at its own start, so
    // two spans' end times can disagree by a fraction of a millisecond (seen in CI).
    // SimpleSpanProcessor hands spans to the exporter in the order they end.
    const ended = spans.getFinishedSpans().map((s) => s.name)
    expect(ended).toContain('agent.tool/list_models')
    expect(ended.indexOf('agent.mcp/tools/call')).toBeGreaterThan(ended.indexOf('agent.tool/list_models'))
  })

  it('a tool error outcome carries a failure class, never a message', async () => {
    server.use(http.get(`${BACKEND}/api/v1/models`, () => HttpResponse.json({ detail: 'SECRET-DETAIL' }, { status: 500 })))
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
    clients.push(client)
    expect((await client.callTool({ name: 'list_models', arguments: {} })).isError).toBe(true)
    await flushTracing()
    const [tool] = named('agent.tool/list_models')
    expect(tool!.attributes).toMatchObject({ 'scadbuddy.outcome': 'error', 'scadbuddy.failure_class': 'ToolError' })
    expect(JSON.stringify(tool)).not.toContain('SECRET-DETAIL')
  })
})

describe('the harness projection', () => {
  it('runs a call in the span bound for its tool_use id', async () => {
    const headers: (string | null)[] = []
    const { createBackendClient } = await import('../src/api/backend.js')
    const backend = createBackendClient(BACKEND, async (request) => {
      headers.push((request as Request).headers.get('traceparent'))
      return Response.json([])
    })
    const toolSpan = tracer().startSpan('agent.tool/mcp__scadbuddy__list_models')
    bindToolContext('toolu_bound', trace.setSpan(ROOT_CONTEXT, toolSpan))
    const principal = harnessPrincipal(browser)
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createHarnessServer(ALL_TOOLS, services({ backend }), principal).instance.connect(serverSide)
    const client = new McpClient({ name: 'projection-test', version: '0' })
    await client.connect(clientSide)
    await client.callTool({ name: 'list_models', arguments: {}, _meta: { 'claudecode/toolUseId': 'toolu_bound' } })
    await client.close()
    unbindToolContext('toolu_bound')
    toolSpan.end()
    await flushTracing()
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(toolSpan.spanContext().spanId)
    expect(headers).toEqual([`00-${toolSpan.spanContext().traceId}-${request!.spanContext().spanId}-01`])
  })
})
