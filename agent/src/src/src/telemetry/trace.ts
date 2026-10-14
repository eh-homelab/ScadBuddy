// agent/src/telemetry/trace.ts
import {
  type Attributes,
  type Context,
  context,
  isSpanContextValid,
  type Link,
  ROOT_CONTEXT,
  type Span,
  type SpanContext,
  SpanKind,
  SpanStatusCode,
  trace,
  TraceFlags,
  type Tracer,
} from '@opentelemetry/api'
import { parseTraceParent } from '@opentelemetry/core'

// What every traced file in the agent uses (spec 2026-10-01 §5.4, §6). Spans go
// through the global API, so with OTEL_SDK_DISABLED (or no `--import`) they are
// the no-op ones and every helper here degrades to "no trace".

export const TRACER_NAME = 'scadbuddy-agent'

export function tracer(): Tracer {
  return trace.getTracer(TRACER_NAME)
}

/** `span`'s W3C traceparent, for a column or a header; undefined unless valid and sampled. */
export function traceparentOf(span: Span | undefined): string | undefined {
  const sc = span?.spanContext()
  if (!sc || !isSpanContextValid(sc) || (sc.traceFlags & TraceFlags.SAMPLED) === 0) return undefined
  return `00-${sc.traceId}-${sc.spanId}-${sc.traceFlags.toString(16).padStart(2, '0')}`
}

/** A remote span context from a traceparent someone sent or a row stored; undefined for anything malformed. */
export function spanContextFrom(traceparent: string | null | undefined): SpanContext | undefined {
  if (!traceparent || traceparent.length > 128) return undefined
  const parsed = parseTraceParent(traceparent)
  if (!parsed || !isSpanContextValid(parsed)) return undefined
  return { ...parsed, isRemote: true }
}

/** A context whose parent is `traceparent`, or `fallback` when it is missing or malformed. */
export function contextFrom(traceparent: string | null | undefined, fallback: Context = ROOT_CONTEXT): Context {
  const sc = spanContextFrom(traceparent)
  return sc ? trace.setSpanContext(ROOT_CONTEXT, sc) : fallback
}

export function linkTo(traceparent: string | null | undefined): Link | undefined {
  const sc = spanContextFrom(traceparent)
  return sc ? { context: sc } : undefined
}

/** `scadbuddy.failure_class` (spec §6): the exception's class name. */
export function failureClass(err: unknown): string {
  if (err instanceof Error) {
    const cls = err.constructor?.name
    if (typeof cls === 'string' && cls !== '' && cls !== 'Object') return cls
    return err.name || 'Error'
  }
  return typeof err
}

/** ERROR status and the failure class. The exception's message is dropped by ScrubbingSpanExporter; a non-Error's value is never recorded. */
export function recordFailure(span: Span, err: unknown): void {
  span.recordException(err instanceof Error ? err : { name: failureClass(err) })
  span.setStatus({ code: SpanStatusCode.ERROR })
  span.setAttribute('scadbuddy.failure_class', failureClass(err))
}

/** Runs `fn` with a new span active (child of `parent`, else the active context), ending it whatever happens. */
export async function withSpan<T>(
  name: string,
  options: { attributes?: Attributes; kind?: SpanKind; parent?: Context },
  fn: (span: Span) => Promise<T>,
): Promise<T> {
  const parent = options.parent ?? context.active()
  const span = tracer().startSpan(
    name,
    { kind: options.kind ?? SpanKind.INTERNAL, ...(options.attributes ? { attributes: options.attributes } : {}) },
    parent,
  )
  try {
    return await context.with(trace.setSpan(parent, span), () => fn(span))
  } catch (err) {
    recordFailure(span, err)
    throw err
  } finally {
    span.end()
  }
}

// In-process tool calls (decision 10 of the plan). The SDK runs an in-process
// MCP handler in the async context the query started in, not in its tool's
// span. Claude Code sends the call's tool_use id in every tools/call's `_meta`
// under this key (measured on the bundled 2.1.283 and 2.1.287 binaries), so telemetry/turn.ts
// binds each tool span's context under its id and tools/projections.ts finds it.
export const TOOL_USE_META = 'claudecode/toolUseId'

const toolContexts = new Map<string, Context>()

export function bindToolContext(toolUseId: string, ctx: Context): void {
  toolContexts.set(toolUseId, ctx)
}

export function unbindToolContext(toolUseId: string): void {
  toolContexts.delete(toolUseId)
}

export function toolUseIdFrom(extra: unknown): string | undefined {
  const meta = (extra as { _meta?: Record<string, unknown> } | undefined)?._meta
  const id = meta?.[TOOL_USE_META]
  return typeof id === 'string' ? id : undefined
}

/**
 * The bound context of the call `extra` belongs to, else `fallback`'s: the
 * turn's open segment for a harness call (tools/projections.ts), so a call
 * whose `_meta` lost its id is still traced under its turn. The active
 * context is the query's start, not the turn's, and only the default.
 */
export function toolContextFor(extra: unknown, fallback: () => Context = () => context.active()): Context {
  const id = toolUseIdFrom(extra)
  return (id !== undefined ? toolContexts.get(id) : undefined) ?? fallback()
}
