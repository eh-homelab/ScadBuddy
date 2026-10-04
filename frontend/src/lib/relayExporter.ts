import { context } from '@opentelemetry/api'
import { ExportResultCode, suppressTracing, type ExportResult } from '@opentelemetry/core'
import { JsonTraceSerializer } from '@opentelemetry/otlp-transformer'
import type { ReadableSpan, SpanExporter } from '@opentelemetry/sdk-trace-web'

/** The backend's browser relay (tracing spec 2026-10-01 §5.2), on the page's own origin. */
export const RELAY_PATH = '/telemetry/v1/traces'
/** The relay's answer with tracing off: `204` and this header set to `off`. */
export const TRACING_HEADER = 'X-ScadBuddy-Tracing'
/**
 * The largest request body (§5.3). Browsers cap a page's in-flight `keepalive` bodies
 * at 64 KiB in total, and this exporter sends one request at a time, so each stays
 * under it with room to spare.
 */
export const MAX_REQUEST_BYTES = 48 * 1024
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
 *   fits (and holds at most `MAX_REQUEST_SPANS` spans); a single span still over it is dropped and counted in `droppedSpans`.
 * - Requests go one at a time, across `export` calls too, so in-flight `keepalive`
 *   bytes stay under the browser's cap. `keepalive` lets a batch flushed as the page
 *   hides still go; a request queued behind one in flight may start after `pagehide`.
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
  /** The request in flight, if any; the next one starts after it settles. */
  #tail: Promise<unknown> = Promise.resolve()

  /** `onOff` is called once, when the relay answers off, so the page can stop tracing. */
  constructor(onOff?: () => void) {
    this.#onOff = onOff
  }

  /** The relay said tracing is off; nothing more is sent. */
  get off(): boolean {
    return this.#off
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    const sent = this.#tail.then(() => this.#send(spans)).catch(failed)
    this.#tail = sent
    void sent.then(resultCallback)
  }

  async forceFlush(): Promise<void> {
    await this.#tail
  }

  async shutdown(): Promise<void> {
    await this.#tail
    this.#off = true
  }

  async #send(spans: ReadableSpan[]): Promise<ExportResult> {
    if (this.#off) return SUCCESS
    let bodies: string[]
    try {
      bodies = this.#bodies(spans)
    } catch (error) {
      return failed(error)
    }
    for (const body of bodies) {
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
  #bodies(spans: ReadableSpan[]): string[] {
    const bodies: string[] = []
    const visit = (part: ReadableSpan[]) => {
      if (part.length === 0) return
      const bytes = JsonTraceSerializer.serializeRequest(part)
      if (part.length <= MAX_REQUEST_SPANS && bytes && bytes.byteLength <= MAX_REQUEST_BYTES) {
        bodies.push(decoder.decode(bytes))
        return
      }
      if (part.length === 1) {
        this.droppedSpans += 1
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
