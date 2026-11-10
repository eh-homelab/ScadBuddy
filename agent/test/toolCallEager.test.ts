import type { Client } from '@temporalio/client'
import { context as otelContext, trace } from '@opentelemetry/api'
import { describe, expect, it } from 'vitest'
import { EAGER_ATTRIBUTE, toolCallStarter } from '../src/tools/activityProjection.js'
import { flushTracing, testTracing } from './support/tracing.js'

// A failed eager start (#5865) as the exported span shows it: `failed`, and an exception event that
// keeps the failure's class and never a non-Error's value. No Temporal: both clients are stubs.

describe('an eager ToolCall start that throws a non-Error', () => {
  it('is recorded as failed with its class, not its value, and the call starts on the shared client', async () => {
    const spans = testTracing()
    spans.reset()
    const result = { ok: true, content: [{ type: 'text', text: 'ok' }] }
    const handle = { result: async () => result, cancel: async () => {} }
    const client = { workflow: { start: async () => handle, getHandle: () => handle } } as unknown as Client
    const eager = { workflow: { start: () => Promise.reject('secret-value-0123') } } as unknown as Client
    const start = toolCallStarter({ client, blobs: { get: async () => undefined }, eager: () => eager })
    const span = trace.getTracer('test').startSpan('call')
    const answer = await otelContext.with(trace.setSpan(otelContext.active(), span), () =>
      start({ sessionId: '00000000-0000-4000-8000-000000000000', toolUseId: 'toolu_x', tool: 'bench', input: {}, signal: new AbortController().signal }),
    )
    span.end()
    await flushTracing()
    expect(answer).toEqual({ content: [{ type: 'text', text: 'ok' }] })
    const [exported] = spans.getFinishedSpans().filter((s) => s.name === 'call')
    expect(exported?.attributes[EAGER_ATTRIBUTE]).toBe('failed')
    expect(exported?.events).toEqual([expect.objectContaining({ name: 'exception', attributes: { 'exception.type': 'string' } })])
    expect(JSON.stringify(exported?.events)).not.toContain('secret-value')
  })
})
