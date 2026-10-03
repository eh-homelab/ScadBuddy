// frontend/src/lib/traceAction.test.ts
import { SpanStatusCode, trace } from '@opentelemetry/api'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { installTestTracing } from '../test/tracing'
import { failureClass, messageTraceparent, traceAction, traceparentOf } from './traceAction'

describe('traceAction', () => {
  let tracing: ReturnType<typeof installTestTracing>
  beforeEach(() => {
    tracing = installTestTracing()
  })
  afterEach(() => tracing.uninstall())

  it('records one span named after the action, with its attributes', async () => {
    await expect(traceAction('generate', { 'scadbuddy.slug': 'box' }, async () => 7)).resolves.toBe(7)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.name).toBe('generate')
    expect(span?.attributes['scadbuddy.slug']).toBe('box')
    expect(span?.status.code).not.toBe(SpanStatusCode.ERROR)
  })

  it('makes the span active for the synchronous start and for every `within` call', async () => {
    const seen: (string | undefined)[] = []
    await traceAction('send', {}, async (within) => {
      seen.push(trace.getActiveSpan()?.spanContext().spanId)
      await Promise.resolve()
      seen.push(trace.getActiveSpan()?.spanContext().spanId)
      within(() => seen.push(trace.getActiveSpan()?.spanContext().spanId))
    })
    const id = tracing.exporter.getFinishedSpans()[0]?.spanContext().spanId
    // After the await, only `within` restores the action's span.
    expect(seen).toEqual([id, undefined, id])
  })

  it('marks a failure with its class, never its message, and rethrows it', async () => {
    const refused = new ApiError({ type: 'https://scadbuddy.dev/problems/x', title: 't', status: 409, detail: 'SECRET' })
    await expect(traceAction('print', {}, async () => Promise.reject(refused))).rejects.toBe(refused)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.status.code).toBe(SpanStatusCode.ERROR)
    expect(span?.attributes['scadbuddy.failure_class']).toBe('https://scadbuddy.dev/problems/x')
    expect(JSON.stringify(span?.attributes)).not.toContain('SECRET')
    expect(span?.status.message ?? '').not.toContain('SECRET')
  })

  it('does not mark a superseded (aborted) action as an error', async () => {
    const aborted = new DOMException('superseded', 'AbortError')
    await expect(traceAction('print', {}, async () => Promise.reject(aborted))).rejects.toBe(aborted)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.status.code).not.toBe(SpanStatusCode.ERROR)
    expect(span?.attributes['scadbuddy.failure_class']).toBeUndefined()
  })

  it('gives the chat turn a traceparent of a span of its own', () => {
    const traceparent = messageTraceparent()
    expect(traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.name).toBe('assistant.message')
    expect(traceparent).toContain(span?.spanContext().spanId)
  })
})

describe('without a registered provider', () => {
  it('runs the action untraced and has no traceparent to send', async () => {
    await expect(traceAction('generate', {}, async () => 'ok')).resolves.toBe('ok')
    expect(messageTraceparent()).toBeUndefined()
    expect(traceparentOf(trace.getTracer('t').startSpan('x'))).toBeUndefined()
  })
})

describe('failureClass', () => {
  it('names an ApiError by its problem type, or its status when it has none', () => {
    expect(failureClass(new ApiError({ type: 'urn:scadbuddy:unanswered', title: 'No answer', status: 0 }))).toBe(
      'urn:scadbuddy:unanswered',
    )
    expect(failureClass(new ApiError({ type: 'about:blank', title: 'Conflict', status: 409 }))).toBe('http-409')
    expect(failureClass(new ApiError(503, 'down'))).toBe('http-503')
  })

  it('names anything else by its name', () => {
    expect(failureClass(new TypeError('x'))).toBe('TypeError')
    expect(failureClass(new DOMException('gone', 'AbortError'))).toBe('AbortError')
    expect(failureClass('a string')).toBe('error')
  })
})
