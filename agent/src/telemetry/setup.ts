// agent/src/telemetry/setup.ts
import os from 'node:os'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto'
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http'
import { envDetector, type Resource, resourceFromAttributes } from '@opentelemetry/resources'
import { NodeSDK } from '@opentelemetry/sdk-node'
import { BatchSpanProcessor, NoopSpanProcessor, type SpanExporter, type SpanProcessor } from '@opentelemetry/sdk-trace'
import { DEFAULT_SAMPLER } from './sampler.js'
import { ScrubbingSpanExporter } from './scrub.js'

// The agent's OpenTelemetry SDK (spec 2026-10-01 §3, §5.4), started by
// src/telemetry.ts before the app loads. Configured by the standard OTEL_*
// variables only:
//   OTEL_EXPORTER_OTLP_ENDPOINT, or the traces-specific
//   OTEL_EXPORTER_OTLP_TRACES_ENDPOINT  neither set (or OTEL_TRACES_EXPORTER=none): a
//                                provider with no exporter, so spans are created
//                                (context propagates) and dropped (§3). The exporter
//                                itself resolves the URL and the (TRACES_)HEADERS.
//   OTEL_SDK_DISABLED=true       no SDK at all: the API's no-op provider
//   OTEL_TRACES_SAMPLER          replaces DEFAULT_SAMPLER (§6)
//   OTEL_RESOURCE_ATTRIBUTES     merged into the resource (envDetector)
// SCADBUDDY_VERSION and SCADBUDDY_REVISION are build provenance stamped into
// the image (Dockerfile `agent` stage), read here for the resource only.
//
// Only incoming HTTP is instrumented. No outgoing node:http/https request is
// touched (`ignoreOutgoingRequestHook`), and the undici instrumentation is not
// installed, so fetch is untouched too: trace context leaves the agent only
// through the backend client's middleware (api/backend.ts, §4). Metrics and
// logs readers are passed empty, or NodeSDK would build OTLP exporters for them.

export const SERVICE_NAME = 'scadbuddy-agent'

export type Env = Readonly<Record<string, string | undefined>>

function present(value: string | undefined): boolean {
  return value !== undefined && value.trim() !== ''
}

export function agentResource(env: Env = process.env): Resource {
  return resourceFromAttributes({
    'service.name': SERVICE_NAME,
    'service.version': env.SCADBUDDY_VERSION?.trim() || 'dev',
    'service.instance.id': os.hostname(),
    'scadbuddy.revision': env.SCADBUDDY_REVISION?.trim() || 'unknown',
  })
}

export function tracingDisabled(env: Env = process.env): boolean {
  return env.OTEL_SDK_DISABLED?.trim().toLowerCase() === 'true'
}

/**
 * No endpoint: one NoopSpanProcessor. NodeSDK registers no tracer provider at
 * all for an empty list (sdk-node 0.222.0 `start()`), which would stop
 * propagation; §3 wants spans created and dropped.
 */
export function spanProcessors(env: Env = process.env, exporter?: SpanExporter): SpanProcessor[] {
  if (env.OTEL_TRACES_EXPORTER?.trim().toLowerCase() === 'none') return [new NoopSpanProcessor()]
  if (!present(env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT) && !present(env.OTEL_EXPORTER_OTLP_ENDPOINT)) return [new NoopSpanProcessor()]
  return [new BatchSpanProcessor({ exporter: new ScrubbingSpanExporter(exporter ?? new OTLPTraceExporter()) })]
}

/**
 * Incoming requests left untraced: the kubelet's probe (a trace per probe
 * otherwise), and the two server-sent-event streams, whose spans would last as
 * long as the stream, the failure §4 rejects session-long traces for. The
 * instrumentation runs these under suppressTracing, so nothing below them is
 * traced either.
 */
export function untracedIncoming(method: string | undefined, url: string | undefined): boolean {
  const path = (url ?? '').split('?')[0] ?? ''
  if (path === '/healthz') return true
  return method === 'GET' && (path === '/mcp' || /^\/api\/v1\/ai\/sessions\/[^/]+\/events$/.test(path))
}

/** The app's own listener (main.ts); requests on any other port are not traced. */
let tracedPort: number | undefined

/**
 * Names the one listener whose requests are traced (main.ts, before it
 * listens). Every other node:http server in the process, the plugin
 * forwarder's loopback server above all (plugins/forwarder.ts: Claude Code
 * sends no traceparent, and its path `/p/<token>` is a capability), is left
 * untraced, as is everything before a listener is named.
 */
export function traceListener(port: number): void {
  tracedPort = port
}

export function httpInstrumentation(): HttpInstrumentation {
  return new HttpInstrumentation({
    ignoreOutgoingRequestHook: () => true,
    ignoreIncomingRequestHook: (request) =>
      request.socket?.localPort !== tracedPort || untracedIncoming(request.method, request.url),
  })
}

let sdk: NodeSDK | undefined

/** Starts the SDK once; undefined when OTEL_SDK_DISABLED=true. */
export function startTelemetry(env: Env = process.env): NodeSDK | undefined {
  if (sdk || tracingDisabled(env)) return sdk
  sdk = new NodeSDK({
    serviceName: SERVICE_NAME,
    resource: agentResource(env),
    resourceDetectors: [envDetector],
    spanProcessors: spanProcessors(env),
    ...(present(env.OTEL_TRACES_SAMPLER) ? {} : { sampler: DEFAULT_SAMPLER }),
    textMapPropagator: new W3CTraceContextPropagator(),
    instrumentations: [httpInstrumentation()],
    metricReaders: [],
    logRecordProcessors: [],
  })
  sdk.start()
  return sdk
}

/** Flushes and stops the SDK, within `timeoutMs`; never throws (main.ts, on SIGTERM). */
export async function shutdownTelemetry(timeoutMs = 2_000): Promise<void> {
  const running = sdk
  sdk = undefined
  if (!running) return
  await Promise.race([
    running.shutdown().catch(() => {}),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs).unref()),
  ])
}
