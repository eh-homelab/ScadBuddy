// agent/test/telemetry.trace.test.ts
import { context, INVALID_SPAN_CONTEXT, ROOT_CONTEXT, SpanKind, SpanStatusCode, trace, TraceFlags } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  bindToolContext,
  contextFrom,
  failureClass,
  linkTo,
  recordFailure,
  spanContextFrom,
  toolContextFor,
  tracer,
  traceparentOf,
  unbindToolContext,
  withSpan,
} from '../src/telemetry/trace.js'
import { exportedText, flushTracing, PARENT_SPAN_ID, resetTracing, TRACE_ID, TRACEPARENT, testTracing } from './support/tracing.js'

const spans = testTracing()
beforeEach(resetTracing)
const SENTINEL = 's3ntinel-41c0'

describe('traceparents', () => {
  it('formats a valid, sampled span context and nothing else', () => {
    const span = tracer().startSpan('x')
    span.end()
    const { traceId, spanId } = span.spanContext()
    expect(traceparentOf(span)).toBe(`00-${traceId}-${spanId}-01`)
    expect(traceparentOf(trace.wrapSpanContext(INVALID_SPAN_CONTEXT))).toBeUndefined()
    const unsampled = trace.wrapSpanContext({ traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.NONE })
    expect(traceparentOf(unsampled)).toBeUndefined()
    expect(traceparentOf(undefined)).toBeUndefined()
  })

  it('parses a valid traceparent as a remote parent and refuses anything else', () => {
    expect(spanContextFrom(TRACEPARENT)).toEqual({
      traceId: TRACE_ID,
      spanId: PARENT_SPAN_ID,
      traceFlags: TraceFlags.SAMPLED,
      isRemote: true,
    })
    for (const bad of ['', 'garbage', `00-${'0'.repeat(32)}-${PARENT_SPAN_ID}-01`, `${TRACEPARENT}${'x'.repeat(200)}`, null, undefined]) {
      expect(spanContextFrom(bad), String(bad)).toBeUndefined()
    }
    expect(trace.getSpanContext(contextFrom(TRACEPARENT))?.spanId).toBe(PARENT_SPAN_ID)
    expect(contextFrom('garbage')).toBe(ROOT_CONTEXT)
    expect(linkTo(TRACEPARENT)?.context.spanId).toBe(PARENT_SPAN_ID)
    expect(linkTo(null)).toBeUndefined()
  })
})

describe('the default sampler', () => {
  it('drops a CLIENT span with no parent and keeps it under a parent; keeps a parentless INTERNAL span', async () => {
    const parentless = tracer().startSpan('probe', { kind: SpanKind.CLIENT })
    expect(parentless.isRecording()).toBe(false)
    parentless.end()
    const root = tracer().startSpan('root')
    expect(root.isRecording()).toBe(true)
    const child = tracer().startSpan('call', { kind: SpanKind.CLIENT }, trace.setSpan(ROOT_CONTEXT, root))
    expect(child.isRecording()).toBe(true)
    child.end()
    root.end()
    await flushTracing()
    expect(spans.getFinishedSpans().map((s) => s.name).sort()).toEqual(['call', 'root'])
  })
})

describe('failures', () => {
  it('records the class, never the message', async () => {
    class OddError extends Error {
      override name = 'OddError'
    }
    expect(failureClass(new OddError('x'))).toBe('OddError')
    class FooError extends Error {}
    expect(failureClass(new FooError('x'))).toBe('FooError')
    expect(failureClass('a string')).toBe('string')
    const span = tracer().startSpan('work')
    recordFailure(span, new OddError(SENTINEL))
    recordFailure(span, SENTINEL)
    span.end()
    await flushTracing()
    const [done] = spans.getFinishedSpans()
    expect(done!.status.code).toBe(SpanStatusCode.ERROR)
    expect(done!.attributes['scadbuddy.failure_class']).toBe('string')
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })

  it('withSpan makes the span active, ends it, and records a failure before rethrowing', async () => {
    const seen = await withSpan('inner', {}, async (span) => trace.getActiveSpan() === span)
    expect(seen).toBe(true)
    await expect(withSpan('failing', {}, async () => Promise.reject(new TypeError(SENTINEL)))).rejects.toThrow(SENTINEL)
    await flushTracing()
    const failing = spans.getFinishedSpans().find((s) => s.name === 'failing')!
    expect(failing.status.code).toBe(SpanStatusCode.ERROR)
    expect(failing.attributes['scadbuddy.failure_class']).toBe('TypeError')
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })
})

describe('tool contexts', () => {
  it('finds a bound tool span by the tool_use id Claude Code sends in _meta, else the active context', () => {
    const span = tracer().startSpan('agent.tool/x')
    const bound = trace.setSpan(ROOT_CONTEXT, span)
    bindToolContext('toolu_1', bound)
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_1' } })).toBe(bound)
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_other' } })).toBe(context.active())
    expect(toolContextFor(undefined)).toBe(context.active())
    unbindToolContext('toolu_1')
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_1' } })).toBe(context.active())
    span.end()
  })

  it('falls back to the turn’s context when given one, not the active context', () => {
    const turnSpan = tracer().startSpan('agent.turn')
    const turnContext = trace.setSpan(ROOT_CONTEXT, turnSpan)
    expect(toolContextFor(undefined, () => turnContext)).toBe(turnContext)
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_unbound' } }, () => turnContext)).toBe(turnContext)
    turnSpan.end()
  })
})
