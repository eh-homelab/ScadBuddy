// frontend/src/lib/traceScrub.test.ts
import { SpanStatusCode, trace } from '@opentelemetry/api'
import { ExportResultCode, type ExportResult } from '@opentelemetry/core'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  WebTracerProvider,
  type ReadableSpan,
} from '@opentelemetry/sdk-trace-web'
import { describe, expect, it } from 'vitest'
import { ARRAY_ITEMS_MAX, frameLines, ScrubbingSpanExporter, scrubSpan, SPAN_NAME_MAX } from './traceScrub'

// Copied from backend/scadbuddy/telemetry/payload.py `_BROWSER_FRAME`: what the relay keeps.
const RELAY_CHROME = /^\s+at \S.*:\d+:\d+\)?$/
const RELAY_GECKO = /^[^\s@]*@\S+:\d+:\d+$/

const SENTINEL = 'SENTINEL-4f1c'

/** Spans made by a real (unregistered) provider, so they are the SDK's own objects. */
function record(make: (tracer: ReturnType<WebTracerProvider['getTracer']>) => void): ReadableSpan[] {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] })
  make(provider.getTracer('test'))
  return exporter.getFinishedSpans()
}

const CHROME_STACK = [
  `TypeError: ${SENTINEL}`,
  '    at send (http://localhost:5173/src/api/client.ts:244:18)',
  '    at http://localhost:5173/assets/index-abc.js:1:2345',
  `Caused by: Error: ${SENTINEL}`,
].join('\n')

const FIREFOX_STACK = [
  `send@http://localhost:5173/src/api/client.ts:244:18`,
  `@http://localhost:5173/assets/index-abc.js:1:2345`,
  SENTINEL,
].join('\n')

describe('frameLines', () => {
  it('keeps only Chromium frame lines', () => {
    expect(frameLines(CHROME_STACK)).toBe(
      '    at send (http://localhost:5173/src/api/client.ts:244:18)\n    at http://localhost:5173/assets/index-abc.js:1:2345',
    )
  })

  it('keeps only Firefox and Safari frame lines', () => {
    expect(frameLines(FIREFOX_STACK)).toBe(
      'send@http://localhost:5173/src/api/client.ts:244:18\n@http://localhost:5173/assets/index-abc.js:1:2345',
    )
  })
})

describe('frameLines against the relay', () => {
  it.each([CHROME_STACK, FIREFOX_STACK])('keeps only lines the relay also keeps', (stack) => {
    const lines = frameLines(stack).split('\n')
    expect(lines.length).toBeGreaterThan(0)
    for (const line of lines) expect(RELAY_CHROME.test(line) || RELAY_GECKO.test(line)).toBe(true)
  })
})

describe('scrubSpan', () => {
  it('drops message text that imitates a frame', () => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('x')
      s.recordException({
        name: 'Error',
        message: 'a\n    at SECRET:1:2',
        stack: 'Error: a\n    at SECRET:1:2\n    at real (http://h/a.js:1:2)',
      })
      s.end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.events[0]!.attributes?.['exception.stacktrace']).toBe('    at real (http://h/a.js:1:2)')
    expect(JSON.stringify(scrubbed.events)).not.toContain('SECRET')
  })

  it.each(['a', '1', 'at'])('keeps the frames intact when the message is %j', (message) => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('x')
      s.recordException({
        name: 'Error',
        message,
        stack: `Error: ${message}\n    at sendOutput (http://host/assets/index-abc.js:1:2)\n    at at (http://host/a1.js:3:4)`,
      })
      s.end()
    })
    expect(scrubSpan(span!).events[0]!.attributes?.['exception.stacktrace']).toBe(
      '    at sendOutput (http://host/assets/index-abc.js:1:2)\n    at at (http://host/a1.js:3:4)',
    )
  })

  it('cuts http.target and drops query and user-agent attributes', () => {
    const [span] = record((tracer) => {
      tracer
        .startSpan('x', {
          attributes: {
            'http.target': `/m/box?v=${SENTINEL}`,
            'url.query': SENTINEL,
            'http.user_agent': SENTINEL,
            'user_agent.original': SENTINEL,
          },
        })
        .end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.attributes['http.target']).toBe('/m/box')
    expect(JSON.stringify(scrubbed.attributes)).not.toContain(SENTINEL)
    expect(Object.keys(scrubbed.attributes)).toEqual(['http.target'])
  })

  it('drops the exception message, keeps the type and the frames, and replaces the status description', () => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('generate')
      s.recordException({ name: 'TypeError', message: SENTINEL, stack: CHROME_STACK })
      s.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
      s.end()
    })
    const scrubbed = scrubSpan(span!)
    expect(JSON.stringify({ a: scrubbed.attributes, e: scrubbed.events, s: scrubbed.status })).not.toContain(SENTINEL)
    const event = scrubbed.events[0]!
    expect(event.attributes?.['exception.type']).toBe('TypeError')
    expect(event.attributes?.['exception.stacktrace']).toContain('    at send (')
    expect(scrubbed.status).toEqual({ code: SpanStatusCode.ERROR, message: 'TypeError' })
  })

  it('says `error` for a status description with no exception to name', () => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('send')
      s.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
      s.end()
    })
    expect(scrubSpan(span!).status).toEqual({ code: SpanStatusCode.ERROR, message: 'error' })
  })

  it('records URLs without their query or fragment', () => {
    const [span] = record((tracer) => {
      tracer
        .startSpan('GET', {
          attributes: {
            'url.full': `http://localhost:5173/api/v1/models?q=${SENTINEL}#frag`,
            'http.url': `http://localhost:5173/m/box?values=${SENTINEL}`,
          },
        })
        .end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.attributes['url.full']).toBe('http://localhost:5173/api/v1/models')
    expect(scrubbed.attributes['http.url']).toBe('http://localhost:5173/m/box')
  })

  it('caps the name and arrays the SDK limits cannot, without counting a shortened array as dropped', () => {
    const [span] = record((tracer) => {
      tracer
        .startSpan('x'.repeat(SPAN_NAME_MAX + 1), {
          attributes: { many: Array.from({ length: ARRAY_ITEMS_MAX + 1 }, (_, i) => i), few: [1, 2] },
        })
        .end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.name).toHaveLength(SPAN_NAME_MAX)
    expect(scrubbed.attributes['many']).toHaveLength(ARRAY_ITEMS_MAX)
    expect(scrubbed.attributes['few']).toEqual([1, 2])
    expect(scrubbed.droppedAttributesCount).toBe(span!.droppedAttributesCount)
  })

  it('keeps the span context, so the exported span is still the one that was made', () => {
    const [span] = record((tracer) => tracer.startSpan('print').end())
    expect(scrubSpan(span!).spanContext()).toEqual(span!.spanContext())
    expect(trace.getActiveSpan()).toBeUndefined()
  })
})

describe('ScrubbingSpanExporter', () => {
  it('hands the inner exporter scrubbed spans and passes its result back', async () => {
    const inner = new InMemorySpanExporter()
    const [span] = record((tracer) => {
      const s = tracer.startSpan('send')
      s.recordException(new Error(SENTINEL))
      s.end()
    })
    const result = await new Promise<ExportResult>((resolve) =>
      new ScrubbingSpanExporter(inner).export([span!], resolve),
    )
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(JSON.stringify(inner.getFinishedSpans()[0]?.events)).not.toContain(SENTINEL)
  })
})
