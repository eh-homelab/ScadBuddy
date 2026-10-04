// frontend/src/lib/traceScrub.ts
import type { AttributeValue, Attributes } from '@opentelemetry/api'
import type { ExportResult } from '@opentelemetry/core'
import type { ReadableSpan, SpanExporter, TimedEvent } from '@opentelemetry/sdk-trace-web'

/**
 * The browser's `ScrubbingSpanExporter` (tracing spec 2026-10-01 §6), in front of
 * `RelayExporter`: it removes, whichever code made the span, what the page must never
 * send (query strings, user agents, captured headers, error messages and a stack's
 * message lines). It is not the whole of §6: the relay's `prepare` is, and it also
 * reduces `url.path`, host names and the URLs in events and links. It also applies the relay's two caps the SDK's `spanLimits` cannot
 * (§5.2: a span name of 128 characters, arrays of 32 items), so a well-behaved page
 * never meets them at the relay.
 */
export const SPAN_NAME_MAX = 128
export const ARRAY_ITEMS_MAX = 32

const URL_ATTRIBUTES = ['url.full', 'http.url', 'http.target']
/** `http.status_text`: the fetch instrumentation writes a rejected fetch's error message there. */
const DROPPED_ATTRIBUTES = ['url.query', 'http.user_agent', 'user_agent.original', 'http.status_text']
/** §6: captured headers are dropped whatever the instrumentation's capture settings say. */
const DROPPED_PREFIXES = ['http.request.header.', 'http.response.header.']
/** Chromium's `    at f (url:1:2)`, and Firefox's and Safari's `f@url:1:2`. */
const CHROME_FRAME = /^\s+at \S.*:\d+:\d+\)?$/
const GECKO_FRAME = /^[^\s@]*@\S+:\d+:\d+$/

/** A stack reduced to its frame lines: the message, and a cause's, are dropped. */
export function frameLines(stack: string): string {
  return stack
    .split('\n')
    .filter((line) => CHROME_FRAME.test(line) || GECKO_FRAME.test(line))
    .map((line) => (CHROME_FRAME.test(line) ? `    ${line.trim()}` : line))
    .join('\n')
}

/**
 * `stack` without its header, the leading `<type>: <message>` block that Chromium
 * prepends (as many lines as the message has). Only there: a short message must not be
 * cut out of the frames. A stack with no such header (Firefox, Safari) is left as is.
 */
function withoutHeader(stack: string, message: string): string {
  const lines = stack.split('\n')
  const count = message.split('\n').length
  const head = lines.slice(0, count).join('\n')
  const first = lines[0] ?? ''
  if (!head.endsWith(message) || CHROME_FRAME.test(first) || GECKO_FRAME.test(first)) return stack
  return lines.slice(count).join('\n')
}

/** A URL without its query or fragment (§6: "URLs are recorded without the query"). */
export function withoutQuery(url: string): string {
  return url.split(/[?#]/, 1)[0] ?? ''
}

function scrubAttributes(attributes: Attributes): Attributes {
  const out: Attributes = {}
  for (const [key, value] of Object.entries(attributes)) {
    if (DROPPED_ATTRIBUTES.includes(key) || DROPPED_PREFIXES.some((prefix) => key.startsWith(prefix))) continue
    let next: AttributeValue | undefined = value
    if (URL_ATTRIBUTES.includes(key) && typeof value === 'string') next = withoutQuery(value)
    if (Array.isArray(value) && value.length > ARRAY_ITEMS_MAX) {
      next = value.slice(0, ARRAY_ITEMS_MAX) as AttributeValue
    }
    out[key] = next
  }
  return out
}

function scrubEvent(event: TimedEvent): TimedEvent {
  if (event.name !== 'exception' || !event.attributes) return event
  const attributes: Attributes = { ...event.attributes }
  delete attributes['exception.message']
  const message = event.attributes['exception.message']
  const stack = attributes['exception.stacktrace']
  if (typeof stack === 'string') {
    const bare = typeof message === 'string' && message !== '' ? withoutHeader(stack, message) : stack
    attributes['exception.stacktrace'] = frameLines(bare)
  }
  return { ...event, attributes }
}

/** `span` with nothing §6 forbids, as a new `ReadableSpan` (the SDK's own is not rewritten). */
export function scrubSpan(span: ReadableSpan): ReadableSpan {
  const attributes = scrubAttributes(span.attributes)
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
