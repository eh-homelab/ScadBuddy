import { SpanKind, SpanStatusCode } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tracer } from '../src/telemetry/trace.js'
import { flushTracing, resetTracing, testTracing } from './support/tracing.js'

// The one outgoing call that carries trace context (spec 2026-10-01 §4): a
// tool call's backend request is a child of its span.

const spans = testTracing()
beforeEach(() => resetTracing())

async function finished() {
  await flushTracing()
  return spans.getFinishedSpans()
}

function recording(status = 200) {
  const headers: (string | null)[] = []
  const client = createBackendClient('http://backend.test', async (request) => {
    headers.push((request as Request).headers.get('traceparent'))
    return Response.json([], { status })
  })
  return { client, headers }
}

describe('backend client tracing', () => {
  it('opens a CLIENT span under the active one and injects its context', async () => {
    const { client, headers } = recording()
    await tracer().startActiveSpan('agent.tool/list_models', async (span) => {
      await client.GET('/api/v1/models')
      span.end()
    })
    const all = await finished()
    const call = all.find((s) => s.name === 'GET /api/v1/models')!
    const parent = all.find((s) => s.name === 'agent.tool/list_models')!
    expect(call.parentSpanContext?.spanId).toBe(parent.spanContext().spanId)
    expect(call.kind).toBe(SpanKind.CLIENT)
    expect(call.attributes).toMatchObject({
      'http.request.method': 'GET',
      'url.template': '/api/v1/models',
      'server.address': 'backend.test',
      'http.response.status_code': 200,
    })
    expect(headers).toEqual([`00-${parent.spanContext().traceId}-${call.spanContext().spanId}-01`])
  })

  it('marks a 5xx answer as an error with its class', async () => {
    const { client } = recording(503)
    await tracer().startActiveSpan('tool', async (span) => {
      await client.GET('/healthz')
      span.end()
    })
    const call = (await finished()).find((s) => s.name === 'GET /healthz')!
    expect(call.status.code).toBe(SpanStatusCode.ERROR)
    expect(call.attributes['scadbuddy.failure_class']).toBe('http-503')
  })

  it('records a failed fetch by class, never its message, and rethrows', async () => {
    const client = createBackendClient('http://backend.test', async () => {
      throw new TypeError('fetch failed: s3ntinel-77')
    })
    await tracer().startActiveSpan('tool', async (span) => {
      await expect(client.GET('/healthz')).rejects.toThrow('s3ntinel-77')
      span.end()
    })
    const call = (await finished()).find((s) => s.name === 'GET /healthz')!
    expect(call.attributes['scadbuddy.failure_class']).toBe('TypeError')
    expect(JSON.stringify(call.events)).not.toContain('s3ntinel-77')
  })

  it('exports no span for a call made outside any span (the /healthz probe)', async () => {
    const { client } = recording()
    await client.GET('/healthz')
    expect(await finished()).toEqual([])
  })
})
