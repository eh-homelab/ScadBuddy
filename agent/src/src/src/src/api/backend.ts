import { context, propagation, type Span, SpanKind, SpanStatusCode, trace } from '@opentelemetry/api'
import createClient, { type Middleware } from 'openapi-fetch'
import { recordFailure, tracer } from '../telemetry/trace.js'
import type { paths } from './schema.js'

// Typed client for the Python backend (spec §4.3). `schema.d.ts` is not
// committed (#492): `pnpm gen:api` writes it from the backend's exported spec
// before every typecheck, test and build. So a backend route or model change
// that breaks this client fails the PR's `agent` job.
//
// Tracing (#988, spec 2026-10-01 §4): every request is a CLIENT span under the
// active one (a tool call's `agent.tool/<name>`), and its `traceparent` is
// injected here. This is the ONLY outgoing call the agent gives trace context
// to: outgoing node:http and fetch are not instrumented (telemetry/setup.ts).

export type BackendClient = ReturnType<typeof createClient<paths>>

/** A CLIENT span per request, its context injected as `traceparent`; ended on the response or the error. */
export function tracingMiddleware(): Middleware {
  const open = new Map<string, Span>()
  const finish = (id: string): Span | undefined => {
    const span = open.get(id)
    open.delete(id)
    return span
  }
  return {
    onRequest({ request, schemaPath, id }) {
      const span = tracer().startSpan(`${request.method} ${schemaPath}`, {
        kind: SpanKind.CLIENT,
        attributes: {
          'http.request.method': request.method,
          'url.template': schemaPath,
        },
      })
      open.set(id, span)
      propagation.inject(trace.setSpan(context.active(), span), request.headers, {
        set: (headers, key, value) => headers.set(key, value),
      })
      return request
    },
    onResponse({ response, id }) {
      const span = finish(id)
      if (!span) return undefined
      span.setAttribute('http.response.status_code', response.status)
      if (response.status >= 500) {
        span.setStatus({ code: SpanStatusCode.ERROR })
        span.setAttribute('scadbuddy.failure_class', `http-${response.status}`)
      }
      span.end()
      return undefined
    },
    onError({ error, id }) {
      const span = finish(id)
      if (span) {
        recordFailure(span, error)
        span.end()
      }
      return undefined
    },
  }
}

export function createBackendClient(
  baseUrl: string,
  fetchImpl: typeof fetch = globalThis.fetch,
): BackendClient {
  const client = createClient<paths>({ baseUrl, fetch: (request) => fetchImpl(request) })
  client.use(tracingMiddleware())
  return client
}

/** True when the backend's own /healthz answers 2xx within the timeout. Never throws. */
export async function backendReachable(client: BackendClient, timeoutMs = 2000): Promise<boolean> {
  try {
    const { response } = await client.GET('/healthz', { signal: AbortSignal.timeout(timeoutMs) })
    return response.ok
  } catch {
    return false
  }
}
