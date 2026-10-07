import { ExportResultCode, type ExportResult } from '@opentelemetry/core'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  WebTracerProvider,
  type ReadableSpan,
} from '@opentelemetry/sdk-trace-web'
import { HttpResponse, delay, http } from 'msw'
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import {
  MAX_IN_FLIGHT_BYTES,
  MAX_REQUEST_BYTES,
  MAX_REQUEST_SPANS,
  RELAY_PATH,
  REQUEST_TIMEOUT_MS,
  RelayExporter,
} from './relayExporter'

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
  bodies: {
    spanNames: string[]
    bytes: number
    keepalive: boolean
    contentType: string | null
    attributes: Record<string, unknown>[]
  }[]
  maxInFlight: number
  maxInFlightBytes: number
}

/** The relay accepting every batch (204, tracing on), after `wait` ms, recording what came. */
function acceptingRelay(wait = 0): Seen {
  const seen: Seen = { bodies: [], maxInFlight: 0, maxInFlightBytes: 0 }
  let inFlight = 0
  let inFlightBytes = 0
  server.use(
    http.post(RELAY_PATH, async ({ request }) => {
      inFlight += 1
      seen.maxInFlight = Math.max(seen.maxInFlight, inFlight)
      const text = await request.text()
      const bytes = new TextEncoder().encode(text).byteLength
      inFlightBytes += bytes
      seen.maxInFlightBytes = Math.max(seen.maxInFlightBytes, inFlightBytes)
      const json = JSON.parse(text) as {
        resourceSpans: {
          scopeSpans: { spans: { name: string; attributes: { key: string; value: Record<string, unknown> }[] }[] }[]
        }[]
      }
      const sent = json.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans))
      seen.bodies.push({
        spanNames: sent.map((x) => x.name),
        bytes,
        keepalive: request.keepalive,
        contentType: request.headers.get('content-type'),
        attributes: sent.map((x) => Object.fromEntries(x.attributes.map((a) => [a.key, Object.values(a.value)[0]]))),
      })
      await delay(wait)
      inFlight -= 1
      inFlightBytes -= bytes
      return new HttpResponse(null, { status: 204 })
    }),
  )
  return seen
}

describe('RelayExporter', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('times out a request the relay never answers, and still sends the next batch', async () => {
    vi.useFakeTimers()
    let calls = 0
    server.use(
      http.post(RELAY_PATH, async () => {
        calls += 1
        if (calls === 1) await new Promise(() => {})
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    const first = exportOnce(exporter, spans(2))
    const second = exportOnce(exporter, spans(2))
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS + 1)
    expect((await first).code).toBe(ExportResultCode.FAILED)
    await vi.advanceTimersByTimeAsync(1)
    expect((await second).code).toBe(ExportResultCode.SUCCESS)
    expect(calls).toBe(2)
  })

  it('reports a serializer that throws as failed, and sends the next batch', async () => {
    const seen = acceptingRelay()
    vi.spyOn(JsonTraceSerializer, 'serializeRequest').mockImplementationOnce(() => {
      throw new Error('boom')
    })
    const exporter = new RelayExporter()
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.FAILED)
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toHaveLength(1)
  })

  it('splits a batch over 512 spans so no request carries more', async () => {
    const seen = acceptingRelay()
    const batch = spans(MAX_REQUEST_SPANS + 8)
    expect((await exportOnce(new RelayExporter(), batch)).code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies.length).toBeGreaterThan(1)
    for (const body of seen.bodies) expect(body.spanNames.length).toBeLessThanOrEqual(MAX_REQUEST_SPANS)
    expect(seen.bodies.flatMap((b) => b.spanNames)).toEqual(batch.map((s) => s.name))
  })

  it('posts a batch as OTLP/JSON with keepalive, and reports success on 204', async () => {
    const seen = acceptingRelay()
    const result = await exportOnce(new RelayExporter(), spans(3))
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toEqual([
      {
        spanNames: ['span-0', 'span-1', 'span-2'],
        bytes: expect.any(Number),
        keepalive: true,
        contentType: 'application/json',
        attributes: expect.any(Array),
      },
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

  it('reports dropped spans on the next span it sends, once', async () => {
    const seen = acceptingRelay()
    const exporter = new RelayExporter()
    const [huge] = spans(1, MAX_REQUEST_BYTES)
    await exportOnce(exporter, [huge!])
    await exportOnce(exporter, spans(2))
    await exportOnce(exporter, spans(1))
    expect(seen.bodies.flatMap((b) => b.attributes.map((a) => a['scadbuddy.dropped_spans']))).toEqual([
      1,
      undefined,
      undefined,
    ])
  })

  it('keeps in-flight bytes under the keepalive cap, within a batch and across batches', async () => {
    const seen = acceptingRelay(20)
    const exporter = new RelayExporter()
    const results = await Promise.all([
      exportOnce(exporter, spans(64, 2_000)),
      exportOnce(exporter, spans(64, 2_000)),
      exportOnce(exporter, spans(2)),
    ])
    expect(results.map((r) => r.code)).toEqual([ExportResultCode.SUCCESS, ExportResultCode.SUCCESS, ExportResultCode.SUCCESS])
    expect(seen.bodies.length).toBeGreaterThan(3)
    expect(seen.maxInFlightBytes).toBeLessThanOrEqual(MAX_IN_FLIGHT_BYTES)
  })

  it('starts a flush while an earlier request is still unanswered', async () => {
    let answer: () => void = () => undefined
    const posted: string[] = []
    server.use(
      http.post(RELAY_PATH, async () => {
        posted.push(posted.length === 0 ? 'first' : 'flush')
        if (posted.length === 1) await new Promise<void>((resolve) => (answer = resolve))
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    const first = exportOnce(exporter, spans(2))
    await vi.waitFor(() => expect(posted).toEqual(['first']))
    const flush = exportOnce(exporter, spans(1))
    // The page may be going away: the flush cannot wait for the first answer.
    await vi.waitFor(() => expect(posted).toEqual(['first', 'flush']))
    expect((await flush).code).toBe(ExportResultCode.SUCCESS)
    answer()
    expect((await first).code).toBe(ExportResultCode.SUCCESS)
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
