// agent/test/support/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { resourceFromAttributes } from '@opentelemetry/resources'
import { InMemorySpanExporter, type ReadableSpan, SimpleSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace'
import { DEFAULT_SAMPLER } from '../../src/telemetry/sampler.js'
import { ScrubbingSpanExporter } from '../../src/telemetry/scrub.js'

// One in-memory provider per test process, registered the way NodeSDK
// registers its own (src/telemetry/setup.ts): the default sampler, the
// AsyncLocalStorage context manager, the W3C propagator, and the scrub in front
// of the exporter, so a test sees exactly what would leave the process. The
// API's globals can be set once per process, hence the guard.

export const TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'
export const PARENT_SPAN_ID = '00f067aa0ba902b7'
export const TRACEPARENT = `00-${TRACE_ID}-${PARENT_SPAN_ID}-01`

const KEY = Symbol.for('scadbuddy.test.tracing')
const PROVIDER_KEY = Symbol.for('scadbuddy.test.tracing.provider')

export function testTracing(): InMemorySpanExporter {
  const store = globalThis as unknown as Record<symbol, InMemorySpanExporter | undefined>
  const existing = store[KEY]
  if (existing) return existing
  const spans = new InMemorySpanExporter()
  const provider = new TracerProvider({
    sampler: DEFAULT_SAMPLER,
    resource: resourceFromAttributes({ 'service.name': 'scadbuddy-agent-test' }),
    spanProcessors: [new SimpleSpanProcessor({ exporter: new ScrubbingSpanExporter(spans) })],
  })
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable())
  propagation.setGlobalPropagator(new W3CTraceContextPropagator())
  trace.setGlobalTracerProvider(provider)
  store[KEY] = spans
  ;(globalThis as unknown as Record<symbol, TracerProvider>)[PROVIDER_KEY] = provider
  return spans
}

/** SimpleSpanProcessor exports asynchronously: await this before reading the exporter. */
export async function flushTracing(): Promise<void> {
  await (globalThis as unknown as Record<symbol, TracerProvider | undefined>)[PROVIDER_KEY]?.forceFlush()
}

/**
 * Flush, then clear the exporter. Use it in `beforeEach` (not a bare
 * `spans.reset()`): a span ended by the previous test but not yet exported
 * would otherwise land in the next test's spans.
 */
export async function resetTracing(): Promise<void> {
  await flushTracing()
  testTracing().reset()
}

/** The first exported span `predicate` accepts, polling until `timeoutMs`. */
export async function waitForSpan(
  spans: InMemorySpanExporter,
  predicate: (s: ReadableSpan) => boolean,
  timeoutMs = 10_000,
): Promise<ReadableSpan> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const found = spans.getFinishedSpans().find(predicate)
    if (found) return found
    if (Date.now() > deadline) {
      throw new Error(`no such span; exported: ${spans.getFinishedSpans().map((s) => s.name).join(', ') || 'none'}`)
    }
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
}

/** Every name, attribute, status, event and link attribute exported, as one string, for sentinel checks. */
export function exportedText(spans: InMemorySpanExporter): string {
  return JSON.stringify(
    spans.getFinishedSpans().map((s) => ({
      name: s.name,
      attributes: s.attributes,
      status: s.status,
      events: s.events.map((e) => ({ name: e.name, attributes: e.attributes ?? {} })),
      links: s.links.map((l) => l.attributes ?? {}),
    })),
  )
}
