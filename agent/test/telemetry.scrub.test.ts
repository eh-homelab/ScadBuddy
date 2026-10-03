// agent/test/telemetry.scrub.test.ts
import { type Span, SpanStatusCode } from '@opentelemetry/api'
import { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace'
import { describe, expect, it } from 'vitest'
import { framesOnly, ScrubbingSpanExporter } from '../src/telemetry/scrub.js'

// The scrub in front of every exporter (spec 2026-10-01 §6): no exception
// message, query string or user agent leaves the process, in any form.

const SENTINEL = 's3ntinel-7d2a'

async function exported(work: (span: Span) => void): Promise<InMemorySpanExporter> {
  const inner = new InMemorySpanExporter()
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter: new ScrubbingSpanExporter(inner) })] })
  const span = provider.getTracer('t').startSpan('work')
  work(span)
  span.end()
  await provider.forceFlush()
  return inner
}

function everything(spans: InMemorySpanExporter): string {
  return JSON.stringify(
    spans.getFinishedSpans().map((s) => ({
      attributes: s.attributes,
      status: s.status,
      events: s.events.map((e) => e.attributes ?? {}),
    })),
  )
}

describe('ScrubbingSpanExporter', () => {
  it('drops the message of an exception and of its cause, keeping the type and the frames', async () => {
    const spans = await exported((span) => {
      span.recordException(new TypeError(SENTINEL, { cause: new Error(`${SENTINEL}-cause`) }))
      span.setStatus({ code: SpanStatusCode.ERROR, message: `failed: ${SENTINEL}` })
    })
    expect(everything(spans)).not.toContain(SENTINEL)
    const [span] = spans.getFinishedSpans()
    const event = span!.events[0]!
    expect(event.name).toBe('exception')
    expect(event.attributes?.['exception.type']).toBe('TypeError')
    expect(event.attributes).not.toHaveProperty('exception.message')
    const frames = String(event.attributes?.['exception.stacktrace'])
    expect(frames).toMatch(/^at /)
    expect(frames.split('\n').every((line) => line.startsWith('at '))).toBe(true)
  })

  it('replaces a status description with the exception type, or with "error"', async () => {
    const withException = await exported((span) => {
      span.recordException(new RangeError(SENTINEL))
      span.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
    })
    expect(withException.getFinishedSpans()[0]!.status).toEqual({ code: SpanStatusCode.ERROR, message: 'RangeError' })
    const without = await exported((span) => span.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL }))
    expect(without.getFinishedSpans()[0]!.status).toEqual({ code: SpanStatusCode.ERROR, message: 'error' })
  })

  it('drops a message that imitates frame lines', async () => {
    const spans = await exported((span) => span.recordException(new Error(`${SENTINEL}\n    at evil (${SENTINEL}.js:1:1)`)))
    expect(everything(spans)).not.toContain(SENTINEL)
  })

  it('drops query strings and user agents from HTTP attributes', async () => {
    const spans = await exported((span) =>
      span.setAttributes({
        'url.full': `https://scadbuddy.test/mcp?token=${SENTINEL}`,
        'http.target': `/mcp?token=${SENTINEL}`,
        'url.query': `token=${SENTINEL}`,
        'url.path': '/mcp',
        'user_agent.original': SENTINEL,
        'http.request.method': 'POST',
      }),
    )
    expect(everything(spans)).not.toContain(SENTINEL)
    expect(spans.getFinishedSpans()[0]!.attributes).toEqual({
      'url.full': 'https://scadbuddy.test/mcp',
      'http.target': '/mcp',
      'url.path': '/mcp',
      'http.request.method': 'POST',
    })
  })

  it('passes a clean span through unchanged', async () => {
    const spans = await exported((span) => span.setAttribute('scadbuddy.tool', 'list_models'))
    const [span] = spans.getFinishedSpans()
    expect(span!.name).toBe('work')
    expect(span!.attributes).toEqual({ 'scadbuddy.tool': 'list_models' })
    expect(span!.status.code).toBe(SpanStatusCode.UNSET)
    expect(span!.spanContext().traceId).toMatch(/^[0-9a-f]{32}$/)
  })
})

describe('framesOnly', () => {
  it('keeps the `at` lines of a Node stack and nothing else', () => {
    const stack = `Error: ${SENTINEL}\n    at f (file:///app/x.js:1:2)\n    at async g (file:///app/y.js:3:4)`
    expect(framesOnly(stack, SENTINEL)).toBe('at f (file:///app/x.js:1:2)\nat async g (file:///app/y.js:3:4)')
  })
})
