// agent/test/telemetry.scrub.test.ts
import { type Span, SpanKind, SpanStatusCode } from '@opentelemetry/api'
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

  it("drops the Host header, the client's address and captured headers, keeping the server's port", async () => {
    const spans = await exported((span) =>
      span.setAttributes({
        'http.host': SENTINEL,
        'http.server_name': SENTINEL,
        'server.address': SENTINEL,
        'server.port': 8787,
        'http.client_ip': SENTINEL,
        'client.address': SENTINEL,
        'net.peer.ip': SENTINEL,
        'net.sock.peer.addr': SENTINEL,
        'network.peer.address': SENTINEL,
        'http.request.header.cookie': SENTINEL,
        'http.response.header.set_cookie': SENTINEL,
        'http.status_text': SENTINEL,
        'http.request.method': 'POST',
      }),
    )
    expect(everything(spans)).not.toContain(SENTINEL)
    expect(spans.getFinishedSpans()[0]!.attributes).toEqual({
      'server.port': 8787,
      'http.request.method': 'POST',
    })
  })

  it("drops a server span's URL, path and host, keeping its route", async () => {
    const inner = new InMemorySpanExporter()
    const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor({ exporter: new ScrubbingSpanExporter(inner) })] })
    const span = provider.getTracer('t').startSpan('POST /p/:token', { kind: SpanKind.SERVER })
    span.setAttributes({
      'url.full': `http://agent.test/p/${SENTINEL}`,
      'http.url': `http://agent.test/p/${SENTINEL}`,
      'http.target': `/p/${SENTINEL}`,
      'url.path': `/p/${SENTINEL}`,
      'net.host.name': SENTINEL,
      'http.route': '/p/:token',
      'http.request.method': 'POST',
    })
    span.end()
    await provider.forceFlush()
    expect(everything(inner)).not.toContain(SENTINEL)
    expect(inner.getFinishedSpans()[0]!.attributes).toEqual({ 'http.route': '/p/:token', 'http.request.method': 'POST' })
  })

  it('scrubs the attributes of every event and link', async () => {
    const spans = await exported((span) => {
      span.addEvent('request', { 'url.query': SENTINEL, 'client.address': SENTINEL, kept: 1 })
      span.addLink({
        context: span.spanContext(),
        attributes: { 'http.request.header.cookie': SENTINEL, 'url.full': `https://x.test/?q=${SENTINEL}` },
      })
    })
    expect(JSON.stringify(spans.getFinishedSpans()[0]!.links)).not.toContain(SENTINEL)
    expect(everything(spans)).not.toContain(SENTINEL)
    expect(spans.getFinishedSpans()[0]!.events[0]!.attributes).toEqual({ kept: 1 })
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

  it.each(['a', '1', 'at', 'f', 'x.js', ' '])('keeps frame lines byte-identical when the message is %j', (message) => {
    const frames = 'at a (file:///app/a1.js:1:2)\nat async at (file:///app/at.js:3:4)'
    const stack = `Error: ${message}\n    ${frames.replace('\n', '\n    ')}`
    expect(framesOnly(stack, message)).toBe(frames)
  })

  it('cuts a multi-line message with a frame-shaped line from the head only', () => {
    const message = `x\n    at ${SENTINEL} (file:1:2)`
    const stack = `Error: ${message}\n    at real (file:///app/x.js:1:2)`
    expect(framesOnly(stack, message)).toBe('at real (file:///app/x.js:1:2)')
  })

  it('drops a message line starting with `at` when err.message was reassigned after construction', () => {
    const err = new Error(`upstream said:\nat least 3 items ${SENTINEL}\nat most 10 (really)`)
    const stack = err.stack ?? '' // V8 formats the stack on first read; read it before the reassignment, as a logger would
    err.message = 'replaced'
    expect(stack).toContain(`at least 3 items ${SENTINEL}`)
    const frames = framesOnly(stack, err.message)
    expect(frames).not.toContain(SENTINEL)
    expect(frames).not.toMatch(/^at (least|most) /m)
    for (const line of frames.split('\n')) expect(line).toMatch(/^at /)
  })

  it.each([
    'at f (file:///app/x.js:1:2)',
    'at async g (node:internal/process/task_queues:95:5)',
    'at file:///app/x.js:1:2',
    'at new Foo (/app/x.js:10:3)',
    'at Array.map (<anonymous>)',
    'at async Promise.all (index 0)',
    'at <anonymous>',
    'at native',
    'at f (native)',
    'at eval (eval at <anonymous> (file:///app/x.js:1:2), <anonymous>:1:1)',
  ])('keeps the V8 frame %j', (frame) => {
    expect(framesOnly(`Error: boom\n    ${frame}`, 'boom')).toBe(frame)
  })

  it.each(['at least 3 items', 'at most 10', 'at noon: 12:30', 'at x:1:2 later'])(
    'drops the frame-like message line %j',
    (line) => {
      expect(framesOnly(`Error: boom\n    ${line}\n    at f (file:///app/x.js:1:2)`)).toBe('at f (file:///app/x.js:1:2)')
    },
  )
})
