// frontend/src/lib/traceAction.ts
import {
  context,
  isSpanContextValid,
  SpanStatusCode,
  TraceFlags,
  trace,
  type Attributes,
  type Span,
} from '@opentelemetry/api'
import { ApiError } from '../api/client'

/**
 * The browser's spans (tracing spec 2026-10-01 §5.3). This module is in the entry
 * chunk and needs only `@opentelemetry/api`: until the lazily loaded SDK
 * (`./tracing.ts`) registers a provider, every span here is the API's no-op one, so
 * an action started before it loads is simply not traced.
 */
export const TRACER_NAME = 'scadbuddy-web'

/** Runs `call` with the action's span as the active one, so a `fetch` it starts is its child. */
export type Within = <R>(call: () => R) => R

/**
 * Spec §6 `scadbuddy.failure_class`: an `ApiError`'s problem `type`, or
 * `http-<status>` when that is absent or `about:blank`; otherwise the error's name.
 * Never the message. `name`, not the constructor's, which a production build renames.
 */
export function failureClass(error: unknown): string {
  if (error instanceof ApiError) {
    const type = error.problem.type
    return type && type !== 'about:blank' ? type : `http-${error.status}`
  }
  // Not `instanceof Error`: a DOMException (an abort) is not one everywhere.
  const name = typeof error === 'object' && error !== null ? (error as { name?: unknown }).name : undefined
  return typeof name === 'string' && name ? name : 'error'
}

/** The W3C `traceparent` of `span`, or undefined when it is the no-op span. */
export function traceparentOf(span: Span): string | undefined {
  const ctx = span.spanContext()
  if (!isSpanContextValid(ctx)) return undefined
  const flags = ctx.traceFlags & TraceFlags.SAMPLED ? '01' : '00'
  return `00-${ctx.traceId}-${ctx.spanId}-${flags}`
}

/**
 * A user action (Generate, Print, Send) as one span named `name`. `run`'s synchronous
 * start runs inside the span, so the request it issues first is the span's child.
 * The browser has no async context (a `StackContextManager` cannot follow an
 * `await`), so a request issued after an `await` joins the trace only when its call
 * is wrapped in `within`. `span` is for attributes learnt on the way (an output's id).
 * An abort (a superseded action) records nothing. A failure sets `ERROR` and `scadbuddy.failure_class`, and is rethrown unchanged.
 */
export async function traceAction<T>(
  name: string,
  attributes: Attributes,
  run: (within: Within, span: Span) => Promise<T>,
): Promise<T> {
  const span = trace.getTracer(TRACER_NAME).startSpan(name, { attributes })
  const active = trace.setSpan(context.active(), span)
  const within: Within = (call) => context.with(active, call)
  try {
    return await within(() => run(within, span))
  } catch (error) {
    // A superseded action (an aborted print) is not a failure.
    if (failureClass(error) === 'AbortError') throw error
    span.setStatus({ code: SpanStatusCode.ERROR })
    span.setAttribute('scadbuddy.failure_class', failureClass(error))
    throw error
  } finally {
    span.end()
  }
}

/**
 * Spec §4: the assistant's socket cannot carry headers, so each chat turn's first
 * frame carries the `traceparent` of a span of its own, `assistant.message`, which
 * the agent's `agent.turn` continues. A root, so each turn is its own trace.
 * Undefined before the SDK has loaded.
 */
export function messageTraceparent(): string | undefined {
  const span = trace.getTracer(TRACER_NAME).startSpan('assistant.message', { root: true })
  try {
    return traceparentOf(span)
  } finally {
    span.end()
  }
}
