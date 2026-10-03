import { ExportResultCode, type ExportResult } from '@opentelemetry/core'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  WebTracerProvider,
  type ReadableSpan,
} from '@opentelemetry/sdk-trace-web'
import { HttpResponse, delay, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { MAX_REQUEST_BYTES, RELAY_PATH, RelayExporter } from './relayExporter'

/** `count` finished spans, each carrying `padding` characters in one attribute. */
function spans(count: number, padding = 0): ReadableSpan[] {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
    // Past the 1024 the page's provider uses, to build a span over MAX_REQUEST_BYTES.
    spanLimits: { attributeValueLengthLimit: 100_000 },
  })
  const tracer = provider.getTracer('test')
  for (let i = 0; i < count; i += 1) {
    tracer.startSpan(`span-${i}`, { attributes: { pad: 'x'.repeat(padding) } }).end()
  }
  return exporter.getFinishedSpans()
}

function exportOnce(exporter: RelayExporter, batch: ReadableSpan[]): Promise<ExportResult> {
  return new Promise((resolve) => exporter.export(batch, resolve))
}

interface Seen {
  bodies: { spanNames: string[]; bytes: number; keepalive: boolean; contentType: string | null }[]
  maxInFlight: number
}

/** The relay accepting every batch (204, tracing on), after `wait` ms, recording what came. */
function acceptingRelay(wait = 0): Seen {
  const seen: Seen = { bodies: [], maxInFlight: 0 }
  let inFlight = 0
  server.use(
    http.post(RELAY_PATH, async ({ request }) => {
      inFlight += 1
      seen.maxInFlight = Math.max(seen.maxInFlight, inFlight)
      const text = await request.text()
      const json = JSON.parse(text) as {
        resourceSpans: { scopeSpans: { spans: { name: string }[] }[] }[]
      }
      seen.bodies.push({
        spanNames: json.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans.map((x) => x.name))),
        bytes: new TextEncoder().encode(text).byteLength,
        keepalive: request.keepalive,
        contentType: request.headers.get('content-type'),
      })
      await delay(wait)
      inFlight -= 1
      return new HttpResponse(null, { status: 204 })
    }),
  )
  return seen
}

describe('RelayExporter', () => {
  it('posts a batch as OTLP/JSON with keepalive, and reports success on 204', async () => {
    const seen = acceptingRelay()
    const result = await exportOnce(new RelayExporter(), spans(3))
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toEqual([
      { spanNames: ['span-0', 'span-1', 'span-2'], bytes: expect.any(Number), keepalive: true, contentType: 'application/json' },
    ])
  })

  it('splits a batch over 48 KiB into requests that each fit, losing no span', async () => {
    const seen = acceptingRelay()
    const batch = spans(64, 2_000)
    const result = await exportOnce(new RelayExporter(), batch)
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies.length).toBeGreaterThan(1)
    for (const body of seen.bodies) expect(body.bytes).toBeLessThanOrEqual(MAX_REQUEST_BYTES)
    expect(seen.bodies.flatMap((b) => b.spanNames)).toEqual(batch.map((s) => s.name))
  })

  it('drops and counts a single span over 48 KiB, and still sends the rest', async () => {
    const seen = acceptingRelay()
    const exporter = new RelayExporter()
    const [huge] = spans(1, MAX_REQUEST_BYTES)
    const result = await exportOnce(exporter, [huge!, ...spans(2)])
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(exporter.droppedSpans).toBe(1)
    expect(seen.bodies.flatMap((b) => b.spanNames)).toEqual(['span-0', 'span-1'])
  })

  it('sends one request at a time, within a batch and across batches', async () => {
    const seen = acceptingRelay(20)
    const exporter = new RelayExporter()
    const results = await Promise.all([
      exportOnce(exporter, spans(64, 2_000)),
      exportOnce(exporter, spans(2)),
      exportOnce(exporter, spans(2)),
    ])
    expect(results.map((r) => r.code)).toEqual([ExportResultCode.SUCCESS, ExportResultCode.SUCCESS, ExportResultCode.SUCCESS])
    expect(seen.bodies.length).toBeGreaterThan(3)
    expect(seen.maxInFlight).toBe(1)
  })

  it('switches itself off on X-ScadBuddy-Tracing: off and never sends again', async () => {
    // The default mock (src/mocks/features/telemetry.ts) is the relay with tracing off.
    let requests = 0
    const count = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === RELAY_PATH) requests += 1
    }
    server.events.on('request:start', count)
    try {
      const exporter = new RelayExporter()
      expect((await exportOnce(exporter, spans(64, 2_000))).code).toBe(ExportResultCode.SUCCESS)
      expect(exporter.off).toBe(true)
      expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
      // The first request said off: the rest of the split batch and the next batch stayed home.
      expect(requests).toBe(1)
    } finally {
      server.events.removeListener('request:start', count)
    }
  })

  it('switches off when the relay starts saying off after accepting batches', async () => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return new HttpResponse(null, {
          status: 204,
          headers: calls === 1 ? {} : { 'X-ScadBuddy-Tracing': 'off' },
        })
      }),
    )
    const exporter = new RelayExporter()
    await exportOnce(exporter, spans(2))
    expect(exporter.off).toBe(false)
    await exportOnce(exporter, spans(2))
    expect(exporter.off).toBe(true)
    await exportOnce(exporter, spans(2))
    expect(calls).toBe(2)
  })

  it.each([403, 413, 429, 503])('drops the batch on %i without retrying, and sends the next one', async (status) => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return calls === 1
          ? HttpResponse.json(
              { type: 'about:blank', title: 'refused', status },
              {
                status,
                headers: { 'Content-Type': 'application/problem+json', ...(status === 429 ? { 'Retry-After': '1' } : {}) },
              },
            )
          : new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    const first = await exportOnce(exporter, spans(64, 2_000))
    expect(first.code).toBe(ExportResultCode.FAILED)
    // The refused request ended the batch: its other parts were not sent.
    expect(calls).toBe(1)
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
    expect(calls).toBe(2)
    expect(exporter.off).toBe(false)
  })

  it('drops the batch when fetch rejects, and sends the next one', async () => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return calls === 1 ? HttpResponse.error() : new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.FAILED)
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
  })

  it('waits for the request in flight on forceFlush and shutdown, and sends nothing after shutdown', async () => {
    const seen = acceptingRelay(20)
    const exporter = new RelayExporter()
    exporter.export(spans(2), () => {})
    await exporter.forceFlush()
    expect(seen.bodies).toHaveLength(1)
    await exporter.shutdown()
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toHaveLength(1)
  })
})
