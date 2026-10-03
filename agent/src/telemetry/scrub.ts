// agent/src/telemetry/scrub.ts
import type { Attributes } from '@opentelemetry/api'
import type { ExportResult } from '@opentelemetry/core'
import type { ReadableSpan, SpanExporter, TimedEvent } from '@opentelemetry/sdk-trace'

// The scrub every exported span passes through (spec 2026-10-01 §6), the agent's
// twin of backend/scadbuddy/core/trace_scrub.py. Exception messages carry what
// must never be recorded (a tool's upstream error, a prompt fragment), and the
// HTTP instrumentation records a request's query string and user agent on its
// own. So the rule is enforced here, once, in front of the exporter, not at
// each call site:
//   - `exception.message` is dropped from every exception event;
//   - `exception.stacktrace` keeps only its `at …` frame lines, after every
//     occurrence of the message is cut out (a message can imitate a frame);
//   - a non-empty status description becomes the exception's type, or `error`;
//   - `url.query` and user agents are dropped; URLs lose their query.

const DROPPED: ReadonlySet<string> = new Set([
  'url.query',
  'user_agent.original',
  'user_agent.synthetic.type',
  'http.user_agent',
])
const URLS: ReadonlySet<string> = new Set(['url.full', 'http.url', 'http.target'])

/** The `at …` lines of a Node stack, with `message` cut out first; nothing else. */
export function framesOnly(stacktrace: string, message?: string): string {
  const text = message ? stacktrace.split(message).join('') : stacktrace
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => /^at \S/.test(line))
    .join('\n')
}

export function scrubAttributes(attributes: Attributes): Attributes {
  const out: Attributes = {}
  for (const [key, value] of Object.entries(attributes)) {
    if (DROPPED.has(key)) continue
    out[key] = URLS.has(key) && typeof value === 'string' ? (value.split('?')[0] ?? '') : value
  }
  return out
}

function scrubEvent(event: TimedEvent): TimedEvent {
  if (event.name !== 'exception' || !event.attributes) return event
  const { 'exception.message': message, ...rest } = event.attributes
  const stack = rest['exception.stacktrace']
  if (typeof stack === 'string') {
    rest['exception.stacktrace'] = framesOnly(stack, typeof message === 'string' ? message : undefined)
  }
  return { ...event, attributes: rest }
}

function exceptionType(events: readonly TimedEvent[]): string | undefined {
  for (const event of events) {
    const type = event.name === 'exception' ? event.attributes?.['exception.type'] : undefined
    if (typeof type === 'string' && type) return type
  }
  return undefined
}

/** A copy of `span` with nothing §6 forbids. ReadableSpan is an interface over the SDK's class, so it is rebuilt field by field. */
export function scrubSpan(span: ReadableSpan): ReadableSpan {
  const events = span.events.map(scrubEvent)
  const status = span.status.message ? { code: span.status.code, message: exceptionType(events) ?? 'error' } : span.status
  return {
    name: span.name,
    kind: span.kind,
    spanContext: () => span.spanContext(),
    ...(span.parentSpanContext ? { parentSpanContext: span.parentSpanContext } : {}),
    startTime: span.startTime,
    endTime: span.endTime,
    status,
    attributes: scrubAttributes(span.attributes),
    links: span.links,
    events,
    duration: span.duration,
    ended: span.ended,
    resource: span.resource,
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
  }
}

export class ScrubbingSpanExporter implements SpanExporter {
  readonly #inner: SpanExporter

  constructor(inner: SpanExporter) {
    this.#inner = inner
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    this.#inner.export(spans.map(scrubSpan), resultCallback)
  }

  shutdown(): Promise<void> {
    return this.#inner.shutdown()
  }

  forceFlush(): Promise<void> {
    return this.#inner.forceFlush?.() ?? Promise.resolve()
  }
}
