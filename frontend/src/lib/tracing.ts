import { context, propagation, trace } from '@opentelemetry/api'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { registerInstrumentations } from '@opentelemetry/instrumentation'
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load'
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch'
import { resourceFromAttributes } from '@opentelemetry/resources'
import {
  BatchSpanProcessor,
  StackContextManager,
  WebTracerProvider,
  type SpanLimits,
} from '@opentelemetry/sdk-trace-web'
import { RELAY_PATH, RelayExporter } from './relayExporter'
import { TRACER_NAME } from './traceAction'
import { ScrubbingSpanExporter } from './traceScrub'

/** The relay's per-span caps (tracing spec 2026-10-01 §5.2), as the SDK's limits. */
export const SPAN_LIMITS: SpanLimits = {
  attributeCountLimit: 64,
  attributeValueLengthLimit: 1024,
  eventCountLimit: 16,
  attributePerEventCountLimit: 16,
  linkCountLimit: 8,
  attributePerLinkCountLimit: 16,
}
/** §5.3: with `RelayExporter`'s 48 KiB requests, keeps a flush under the keepalive cap. */
export const MAX_EXPORT_BATCH_SIZE = 64

let stop: (() => Promise<void>) | null = null

/**
 * The page's tracing (§5.3), loaded lazily by `main.tsx` after the first paint:
 * a `WebTracerProvider` whose `BatchSpanProcessor` exports through
 * `ScrubbingSpanExporter` → `RelayExporter`; W3C trace context only, no baggage (§4);
 * fetch instrumentation that injects `traceparent` only into same-origin requests
 * (never Bambuddy or Google Fonts), and document-load instrumentation. Calling it
 * again does nothing. When the relay answers `X-ScadBuddy-Tracing: off` it undoes itself, so no `traceparent` is sent any more. Spans are flushed when the page is hidden. Returns the function that undoes it, for tests.
 */
export function startTracing(): () => Promise<void> {
  if (stop) return stop
  // The relay's off is the backend's only, but the page's `traceparent` would make every
  // downstream span the child of one nobody exports: so off undoes the tracing itself.
  const exporter = new RelayExporter(() => void undo())
  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      'service.name': TRACER_NAME,
      'service.version': import.meta.env.VITE_SCADBUDDY_VERSION || 'dev',
      'user_agent.original': navigator.userAgent,
    }),
    spanLimits: SPAN_LIMITS,
    spanProcessors: [
      new BatchSpanProcessor(new ScrubbingSpanExporter(exporter), {
        maxExportBatchSize: MAX_EXPORT_BATCH_SIZE,
      }),
    ],
  })
  provider.register({
    contextManager: new StackContextManager(),
    propagator: new W3CTraceContextPropagator(),
  })
  const unload = registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [
      new FetchInstrumentation({
        // Empty: same-origin only. The instrumentation always allows the page's own origin.
        propagateTraceHeaderCorsUrls: [],
        ignoreUrls: [`${location.origin}${RELAY_PATH}`],
      }),
      new DocumentLoadInstrumentation(),
    ],
  })
  // The SDK installs no listeners of its own; `keepalive` lets this flush outlive the page.
  const flush = () => void provider.forceFlush().catch(() => undefined)
  const onVisibility = () => {
    if (document.visibilityState === 'hidden') flush()
  }
  document.addEventListener('visibilitychange', onVisibility)
  window.addEventListener('pagehide', flush)
  async function undo() {
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('pagehide', flush)
    unload()
    await provider.shutdown()
    trace.disable()
    context.disable()
    propagation.disable()
    stop = null
  }
  stop = undo
  return undo
}
