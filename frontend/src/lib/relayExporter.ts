import { context } from '@opentelemetry/api'
import { ExportResultCode, suppressTracing, type ExportResult } from '@opentelemetry/core'
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer'
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace-web'

/** The backend's browser relay (tracing spec 2026-10-01 §5.2), on the page's own origin. */
export const RELAY_PATH = '/telemetry/v1/traces'
/** The relay's answer with tracing off: `204` and this header set to `off`. */
export const TRACING_HEADER = 'X-ScadBuddy-Tracing'
/** Browsers cap a page's in-flight `keepalive` bodies at 64 KiB in total (§5.3). */
export const MAX_IN_FLIGHT_BYTES = 64 * 1024
/** The largest request body (§5.3), under `MAX_IN_FLIGHT_BYTES` with room for a small flush beside it. */
export const MAX_REQUEST_BYTES = 48 * 1024
/** The attribute that reports, on the next batch's first span, how many spans were dropped. */
const DROPPED_SPANS_ATTRIBUTE = 'scadbuddy.dropped_spans'
/** The relay refuses a request of more spans than this with a 413 (`telemetry.payload.MAX_SPANS`). */
export const MAX_REQUEST_SPANS = 512
/** A request still unanswered after this is abandoned, so a hung relay cannot pin the queue. */
export const REQUEST_TIMEOUT_MS = 10_000

const SUCCESS: ExportResult = { code: ExportResultCode.SUCCESS }
const decoder = new TextDecoder()

function failed(error: unknown): ExportResult {
  return { code: ExportResultCode.FAILED, error: error instanceof Error ? error : new Error(String(error)) }
}

/**
 * The browser's `SpanExporter` (§5.3): OTLP/JSON to the relay with `fetch`, because
 * the stock OTLP exporter does not hand its caller the response headers, and the
 * relay's off signal is one.
 *
 * - A batch whose JSON is over `MAX_REQUEST_BYTES` is split in halves until each part
 *   fits (and holds at most `MAX_REQUEST_SPANS` spans); a single span still over it is
 *   dropped, counted in `droppedSpans`, and reported as `scadbuddy.dropped_spans` on the
 *   first span of the next batch.
 * - A batch's requests go one after another; other batches' start beside them while
 *   the bodies in flight fit in `MAX_IN_FLIGHT_BYTES`, the browser's `keepalive` cap,
 *   and wait in order otherwise. So the small batch flushed as the page hides starts at
 *   once, beside a request already in flight, and `keepalive` lets it outlive the page.
 *   A request that has to wait may never start once the document is unloaded, so a
 *   batch that would pass the cap, or the later parts of a split one, can be lost then.
 *   A request unanswered after `REQUEST_TIMEOUT_MS` is aborted and the batch failed.
 * - `X-ScadBuddy-Tracing: off` switches the exporter off for the rest of the page's
 *   life: every later batch is reported a success and never sent, and `onOff` is
 *   called so the page stops tracing altogether (`startTracing`).
 * - Anything else that is not a 2xx (413, 429, 403, 503), or a `fetch` that rejects,
 *   drops the batch's remaining requests and reports the batch failed. Nothing is
 *   retried: the relay's 429 says not to, and a page going away cannot wait.
 */
export class RelayExporter implements SpanExporter {
  /** Spans dropped because one alone was over `MAX_REQUEST_BYTES`. */
  droppedSpans = 0
  #off = false
  readonly #onOff: (() => void) | undefined
  /** Dropped spans not yet reported on a sent span. */
  #unreported = 0
  /** The bytes of the requests in flight, and the requests waiting for room, in order. */
  #inFlightBytes = 0
  readonly #waiting: { bytes: number; start: () => void }[] = []
  /** Every export not yet settled. */
  readonly #pending = new Set<Promise<unknown>>()

  /** `onOff` is called once, when the relay answers off, so the page can stop tracing. */
  constructor(onOff?: () => void) {
    this.#onOff = onOff
  }

  /** The relay said tracing is off; nothing more is sent. */
  get off(): boolean {
    return this.#off
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    const sent = this.#send(spans).catch(failed)
    this.#pending.add(sent)
    void sent.then((result) => {
      this.#pending.delete(sent)
      resultCallback(result)
    })
  }

  async forceFlush(): Promise<void> {
    await Promise.all(this.#pending)
  }

  async shutdown(): Promise<void> {
    await this.forceFlush()
    this.#off = true
  }

  /** Resolves once `bytes` more fit in flight, after every request that waited before it. */
  #reserve(bytes: number): Promise<void> {
    if (this.#waiting.length === 0 && this.#inFlightBytes + bytes <= MAX_IN_FLIGHT_BYTES) {
      this.#inFlightBytes += bytes
      return Promise.resolve()
    }
    return new Promise((start) => this.#waiting.push({ bytes, start }))
  }

  #release(bytes: number): void {
    this.#inFlightBytes -= bytes
    for (let next = this.#waiting[0]; next; next = this.#waiting[0]) {
      // A lone request always fits: no body is over MAX_REQUEST_BYTES.
      if (this.#inFlightBytes > 0 && this.#inFlightBytes + next.bytes > MAX_IN_FLIGHT_BYTES) return
      this.#waiting.shift()
      this.#inFlightBytes += next.bytes
      next.start()
    }
  }

  async #send(spans: ReadableSpan[]): Promise<ExportResult> {
    if (this.#off) return SUCCESS
    let bodies: { body: string; bytes: number }[]
    try {
      bodies = this.#bodies(spans)
    } catch (error) {
      return failed(error)
    }
    for (const { body, bytes } of bodies) {
      await this.#reserve(bytes)
      // Another batch's answer may have said off while this one waited.
      if (this.#off) {
        this.#release(bytes)
        return SUCCESS
      }
      let response: Response
      const abort = new AbortController()
      const timer = setTimeout(() => abort.abort(new Error('the trace relay timed out')), REQUEST_TIMEOUT_MS)
      try {
        // Untraced: the relay's own request must not become a span to export.
        response = await context.with(suppressTracing(context.active()), () =>
          fetch(RELAY_PATH, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            keepalive: true,
            signal: abort.signal,
          }),
        )
      } catch (error) {
        return failed(error)
      } finally {
        clearTimeout(timer)
        this.#release(bytes)
      }
      if (response.headers.get(TRACING_HEADER) === 'off') {
        this.#off = true
        this.#onOff?.()
        return SUCCESS
      }
      if (!response.ok) return failed(new Error(`the trace relay answered ${response.status}`))
    }
    return SUCCESS
  }

  /** `spans` as request bodies of at most `MAX_REQUEST_BYTES` each. */
  #bodies(spans: ReadableSpan[]): { body: string; bytes: number }[] {
    const [first, ...rest] = spans
    if (first && this.#unreported > 0) {
      spans = [withAttribute(first, DROPPED_SPANS_ATTRIBUTE, this.#unreported), ...rest]
      this.#unreported = 0
    }
    const bodies: { body: string; bytes: number }[] = []
    const visit = (part: ReadableSpan[]) => {
      if (part.length === 0) return
      const bytes = JsonTraceSerializer.serializeRequest(part)
      if (part.length <= MAX_REQUEST_SPANS && bytes && bytes.byteLength <= MAX_REQUEST_BYTES) {
        bodies.push({ body: decoder.decode(bytes), bytes: bytes.byteLength })
        return
      }
      if (part.length === 1) {
        this.droppedSpans += 1
        this.#unreported += 1
        return
      }
      const half = Math.ceil(part.length / 2)
      visit(part.slice(0, half))
      visit(part.slice(half))
    }
    visit(spans)
    return bodies
  }
}

/** `span` with one more attribute; the SDK's spans are finished and must not be written to. */
function withAttribute(span: ReadableSpan, key: string, value: number): ReadableSpan {
  return Object.create(span, { attributes: { value: { ...span.attributes, [key]: value } } }) as ReadableSpan
}
