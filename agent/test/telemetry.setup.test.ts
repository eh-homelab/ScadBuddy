// agent/test/telemetry.setup.test.ts
import { hostname } from 'node:os'
import { BatchSpanProcessor, InMemorySpanExporter, NoopSpanProcessor } from '@opentelemetry/sdk-trace'
import { describe, expect, it } from 'vitest'
import {
  agentResource,
  httpInstrumentation,
  SERVICE_NAME,
  spanProcessors,
  traceListener,
  tracingDisabled,
  untracedIncoming,
} from '../src/telemetry/setup.js'

describe('telemetry setup', () => {
  it('names the service, its build and its host', () => {
    const resource = agentResource({ SCADBUDDY_VERSION: 'sha-abc1234', SCADBUDDY_REVISION: 'a'.repeat(40) })
    expect(resource.attributes).toEqual({
      'service.name': SERVICE_NAME,
      'service.version': 'sha-abc1234',
      'service.instance.id': hostname(),
      'scadbuddy.revision': 'a'.repeat(40),
    })
    expect(agentResource({}).attributes).toMatchObject({ 'service.version': 'dev', 'scadbuddy.revision': 'unknown' })
  })

  it('without an endpoint, creates spans and drops them; with one, batches through the scrub', async () => {
    const none = spanProcessors({})
    expect(none).toHaveLength(1)
    expect(none[0]).toBeInstanceOf(NoopSpanProcessor)
    expect(spanProcessors({ OTEL_EXPORTER_OTLP_ENDPOINT: '  ' })[0]).toBeInstanceOf(NoopSpanProcessor)
    const batched = spanProcessors({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://alloy:4318' }, new InMemorySpanExporter())
    expect(batched[0]).toBeInstanceOf(BatchSpanProcessor)
    await batched[0]!.shutdown()
  })

  it('is disabled only by OTEL_SDK_DISABLED=true', () => {
    expect(tracingDisabled({ OTEL_SDK_DISABLED: 'true' })).toBe(true)
    expect(tracingDisabled({ OTEL_SDK_DISABLED: 'TRUE' })).toBe(true)
    expect(tracingDisabled({ OTEL_SDK_DISABLED: 'false' })).toBe(false)
    expect(tracingDisabled({})).toBe(false)
  })

  it('leaves probes and long-lived streams untraced', () => {
    expect(untracedIncoming('GET', '/healthz')).toBe(true)
    expect(untracedIncoming('GET', '/healthz?x=1')).toBe(true)
    expect(untracedIncoming('GET', '/mcp')).toBe(true)
    expect(untracedIncoming('POST', '/mcp')).toBe(false)
    expect(untracedIncoming('GET', '/api/v1/ai/sessions/0d6c6a3e-0000-4000-8000-000000000000/events?after=3')).toBe(true)
    expect(untracedIncoming('GET', '/api/v1/ai/sessions/0d6c6a3e-0000-4000-8000-000000000000')).toBe(false)
    expect(untracedIncoming('POST', '/api/v1/ai/approvals/x/approve')).toBe(false)
  })

  it('instruments incoming requests only', () => {
    const instrumentation = httpInstrumentation()
    try {
      const config = instrumentation.getConfig()
      expect(config.ignoreOutgoingRequestHook?.({})).toBe(true)
      traceListener(8081)
      const on = (localPort: number, method: string, url: string) =>
        config.ignoreIncomingRequestHook?.({ method, url, socket: { localPort } } as never)
      expect(on(8081, 'GET', '/healthz')).toBe(true)
      expect(on(8081, 'POST', '/mcp')).toBe(false)
      // Any other listener: the plugin forwarder's loopback server.
      expect(on(40123, 'POST', '/p/token')).toBe(true)
      expect(on(40123, 'POST', '/mcp')).toBe(true)
      expect(config.headersToSpanAttributes).toBeUndefined()
    } finally {
      instrumentation.disable()
    }
  })
})
