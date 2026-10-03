import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { Client as McpClient } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { ROOT_CONTEXT, trace } from '@opentelemetry/api'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import { harnessPrincipal } from '../src/auth/principal.js'
import { mcpMethodOf } from '../src/mcp/http.js'
import { bindToolContext, tracer, unbindToolContext } from '../src/telemetry/trace.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createHarnessServer } from '../src/tools/projections.js'
import { BACKEND, connect, services, testApp } from './helpers/mcp.js'
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

  it('names the span after the JSON-RPC method, and only a well-formed one', async () => {
    const post = (body: string) => new Request('http://x/mcp', { method: 'POST', body })
    expect(await mcpMethodOf(post('{"jsonrpc":"2.0","id":1,"method":"tools/list"}'))).toBe('tools/list')
    expect(await mcpMethodOf(post('[{"jsonrpc":"2.0","method":"notifications/initialized"}]'))).toBe('notifications/initialized')
    expect(await mcpMethodOf(post('{"method":"x y <script>"}'))).toBe('unknown')
    expect(await mcpMethodOf(post('not json'))).toBe('unknown')
    for (const method of ['a1', 'tools/call2', 'x'.repeat(64), 'notifications/made_up']) {
      expect(await mcpMethodOf(post(JSON.stringify({ jsonrpc: '2.0', id: 1, method })))).toBe('unknown')
    }
  })

  it('reads no body for a span that is not recording', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    // An unsampled caller: the span is not recording, so it is never named.
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}`, traceparent: TRACEPARENT.replace(/-01$/, '-00') } })
    clients.push(client)
    const clone = vi.spyOn(Request.prototype, 'clone')
    try {
      await client.callTool({ name: 'list_models', arguments: {} })
      // msw clones the backend request; no /mcp request is cloned.
      expect(clone.mock.contexts.map((r) => new URL((r as Request).url).pathname).filter((p) => p.startsWith('/mcp'))).toEqual([])
    } finally {
      clone.mockRestore()
    }
    await flushTracing()
    expect(spans.getFinishedSpans().filter((s) => s.name.startsWith('agent.mcp'))).toEqual([])
  })

  it('the mcp span outlives its tool span, whatever the response framing', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}` } })
    clients.push(client)
    await client.callTool({ name: 'list_models', arguments: {} })
    await client.close()
    await flushTracing()
    const [call] = named('agent.mcp/tools/call')
    const [tool] = named('agent.tool/list_models')
    const ms = (s: { endTime: [number, number] }) => s.endTime[0] * 1e3 + s.endTime[1] / 1e6
    expect(ms(call!)).toBeGreaterThanOrEqual(ms(tool!))
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
