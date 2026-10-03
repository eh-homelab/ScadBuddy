// frontend/src/lib/traceScrub.ts
import type { AttributeValue, Attributes } from '@opentelemetry/api'
import type { ExportResult } from '@opentelemetry/core'
import type { ReadableSpan, SpanExporter, TimedEvent } from '@opentelemetry/sdk-trace-web'

/**
 * The browser's `ScrubbingSpanExporter` (tracing spec 2026-10-01 §6), in front of
 * `RelayExporter`: what §6 never records is removed once, here, whichever code made
 * the span. It also applies the relay's two caps the SDK's `spanLimits` cannot
 * (§5.2: a span name of 128 characters, arrays of 32 items), so a well-behaved page
 * never meets them at the relay.
 */
export const SPAN_NAME_MAX = 128
export const ARRAY_ITEMS_MAX = 32

const URL_ATTRIBUTES = ['url.full', 'http.url']
/** Chromium's `    at f (url:1:2)`, and Firefox's and Safari's `f@url:1:2`. */
const FRAME_LINE = [/^\s*at .+:\d+:\d+\)?$/, /^[^\s@]*@.+:\d+:\d+$/]

/** A stack reduced to its frame lines: the message, and a cause's, are dropped. */
export function frameLines(stack: string): string {
  return stack
    .split('\n')
    .filter((line) => FRAME_LINE.some((frame) => frame.test(line)))
    .map((line) => line.trim())
    .join('\n')
}

/** A URL without its query or fragment (§6: "URLs are recorded without the query"). */
export function withoutQuery(url: string): string {
  return url.split(/[?#]/, 1)[0] ?? ''
}

function scrubAttributes(attributes: Attributes): { attributes: Attributes; dropped: number } {
  const out: Attributes = {}
  let dropped = 0
  for (const [key, value] of Object.entries(attributes)) {
    let next: AttributeValue | undefined = value
    if (URL_ATTRIBUTES.includes(key) && typeof value === 'string') next = withoutQuery(value)
    if (Array.isArray(value) && value.length > ARRAY_ITEMS_MAX) {
      next = value.slice(0, ARRAY_ITEMS_MAX) as AttributeValue
      dropped += 1
    }
    out[key] = next
  }
  return { attributes: out, dropped }
}

function scrubEvent(event: TimedEvent): TimedEvent {
  if (event.name !== 'exception' || !event.attributes) return event
  const attributes: Attributes = { ...event.attributes }
  delete attributes['exception.message']
  const stack = attributes['exception.stacktrace']
  if (typeof stack === 'string') attributes['exception.stacktrace'] = frameLines(stack)
  return { ...event, attributes }
}

/** `span` with nothing §6 forbids, as a new `ReadableSpan` (the SDK's own is not rewritten). */
export function scrubSpan(span: ReadableSpan): ReadableSpan {
  const { attributes, dropped } = scrubAttributes(span.attributes)
  const events = span.events.map(scrubEvent)
  const exceptionType = events
    .map((event) => event.attributes?.['exception.type'])
    .find((type): type is string => typeof type === 'string' && type !== '')
  const status = span.status.message
    ? { code: span.status.code, message: exceptionType ?? 'error' }
    : { code: span.status.code }
  return {
    name: span.name.slice(0, SPAN_NAME_MAX),
    kind: span.kind,
    spanContext: () => span.spanContext(),
    parentSpanContext: span.parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status,
    attributes,
    links: span.links,
    events,
    duration: span.duration,
    ended: span.ended,
    resource: span.resource,
    instrumentationScope: span.instrumentationScope,
    droppedAttributesCount: span.droppedAttributesCount + dropped,
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
