# Tracing, frontend (PR #4 of #988) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The browser joins the trace. Generate, Print and Send each become a span, every
same-origin `fetch` carries `traceparent` and nothing cross-origin ever does, each chat
turn's first frame carries the turn's `traceparent`, and the page's spans reach the
backend's relay (`POST /telemetry/v1/traces`) through our own `RelayExporter`. The SDK
loads after the first paint, and it stops exporting for good when the relay says tracing
is off.

**Architecture:** Two halves. The entry chunk gets only `@opentelemetry/api` and
`src/lib/traceAction.ts` (`traceAction`, `messageTraceparent`): until the SDK is loaded,
every span is the API's no-op one. `src/lib/tracing.ts` is a lazy chunk that `main.tsx`
imports after the first paint. It registers a `WebTracerProvider` (W3C trace context
only, `StackContextManager`, the relay's span limits) whose `BatchSpanProcessor` exports
through `ScrubbingSpanExporter` (`src/lib/traceScrub.ts`) and then `RelayExporter`
(`src/lib/relayExporter.ts`). It also registers the fetch instrumentation (same-origin
injection only) and the document-load instrumentation. `vite.config.ts` proxies
`/telemetry` to the backend in `pnpm dev`/`pnpm preview`. msw answers the relay "off"
(`src/mocks/features/telemetry.ts`), so vitest and the mocked e2e run never export.

**Tech Stack:** React 19, Vite 8, TypeScript 6, vitest 5 + msw 2 (jsdom), Playwright
1.63. Exact pins: `@opentelemetry/api` 1.9.1, `@opentelemetry/core` 2.11.0,
`@opentelemetry/resources` 2.11.0, `@opentelemetry/sdk-trace-web` 2.11.0,
`@opentelemetry/otlp-transformer` 0.222.0, `@opentelemetry/instrumentation` 0.222.0,
`@opentelemetry/instrumentation-fetch` 0.222.0,
`@opentelemetry/instrumentation-document-load` 0.67.0. Checked against npm on
2026-10-03 (`pnpm view <pkg> version`): these are the latest versions, and they are the
same ones spec §5.4 recorded on 2026-10-01.

**Spec:** `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md`: §5.3 is
the core, plus §3, §4 and §5.2 (the relay contract), §6, §8 (frontend part) and §9 row 4.
Row 2 (the relay route itself) is planned separately. This plan codes against the relay
contract *as §5.2 defines it*, and every test mocks that contract with msw, so this
row is testable without row 2. Row 4 still merges after row 2 (§9). Until then, a
`pnpm dev` against a real backend gets a non-2xx for every batch, and drops it.

## Global Constraints

- `service.name` is `scadbuddy-web`; `service.version` is the build's version (`VITE_SCADBUDDY_VERSION`, from the Dockerfile's `SCADBUDDY_VERSION`, `dev` when unset) (§3).
- Propagation is W3C Trace Context only (`W3CTraceContextPropagator`); no baggage header ever (§4).
- Trace context never leaves ScadBuddy: the fetch instrumentation injects `traceparent` **only for same-origin URLs** (`propagateTraceHeaderCorsUrls: []`), never on Bambuddy or Google Fonts requests (§4, §5.3).
- Relay contract (§5.2): `POST /telemetry/v1/traces`, same origin, `Content-Type: application/json` (OTLP/JSON), `fetch` in its default `cors` mode (never `no-cors`, never `sendBeacon`). It answers `204` when it accepts a batch, and `204` with `X-ScadBuddy-Tracing: off` when tracing is off. Refusals are `application/problem+json`: `403` (Origin or `Sec-Fetch-Site`), `413` (over 256 KiB or 512 spans), `415`, `429` (with `Retry-After`), and `503` while shutting down. No CORS headers.
- Exporter (§5.3): `maxExportBatchSize` 64. A request is at most 48 KiB (`49152` bytes). One request in flight at a time. `keepalive: true`. A single span over 48 KiB is dropped and counted. On `off` it stops for the page's life and reports success. On 413 or 429 (in fact any non-2xx), or a rejected `fetch`, the batch is dropped and **never retried**.
- Span limits, the same as the relay's caps (§5.2): 64 attributes, 1024-character strings, 16 events with 16 attributes each, and 8 links (`spanLimits`). A span name is capped at 128 characters and an array at 32 items, both by the scrubber.
- Never exported (§6): exception messages (only `exception.type` and the frame lines of `exception.stacktrace` are kept), status-description text (it becomes the exception type, or `error`), URL query strings and fragments, parameter values, prompts and message text.
- `scadbuddy.failure_class`: an `ApiError`'s problem `type`, or `http-<status>` when that is absent or `about:blank`. Otherwise the error's `name` (§6).
- The SDK is loaded lazily, after the first paint. The entry chunk gains only `@opentelemetry/api` (§5.3).
- msw: a new mock is a `src/mocks/features/<feature>.ts` that exports `handlers`, and `handlers.ts` is not edited (CLAUDE.md, #508).
- Never commit `backend/openapi.json` or `frontend/src/api/schema.d.ts` (CLAUDE.md, #492).
- `cd frontend && pnpm lint && pnpm typecheck && pnpm test && pnpm build` must pass, and `pnpm exec playwright test` too (CLAUDE.md "Commands"). Vitest timeouts under heavy load are a known flake: re-run on an idle machine before debugging.

## Review Focus

1. **Tracing turned off after the page loaded** (the backend redeployed with the endpoint unset, while a Bambuddy frame stays open for days): the first later batch answered `off` switches the exporter off for good. Not only the first flush. Test in Task 3 ("switches off when the relay starts saying off…").
2. **The relay refusing for a reason other than 413/429**: a `403` (a misconfigured `SCADBUDDY_PUBLIC_URL`) or a `503` (the API shutting down). The batch is dropped, the exporter stays on, the next batch is sent, and nothing retries. Test in Task 3 (`it.each([403, 413, 429, 503])`).
3. **A query string or fragment in a recorded URL**: the document-load span's `location.href` on `/?q=…`, a resource or fetch URL with `?values=`. It is exported without the query or fragment. Test in Task 2 ("records URLs without their query or fragment").
4. **An action whose request never gets an answer** (offline, or a proxy timeout: `ApiError` status 0): the span ends `ERROR` with `scadbuddy.failure_class` `urn:scadbuddy:unanswered`, and the dialog behaves as before. A refusal carries its problem type, and never its detail. Tests in Task 1 (`failureClass`) and Task 5 ("marks a refused run as an error…").
5. **ScadBuddy inside Bambuddy's sandboxed External Link frame**: same-origin requests still carry `traceparent`, and the exporter posts to ScadBuddy's own origin (the sandbox has `allow-same-origin`). Test in Task 8 ("traces inside Bambuddy's sandboxed frame…").

## Decisions where the spec is silent or the code disagrees

- **No async context in the browser.** `StackContextManager` (the browser SDK's) cannot follow an `await`, and `ZoneContextManager` cannot follow native `async`/`await`, which Vite's target emits. So `traceAction` makes its span active for the synchronous start of the action. Every request issued after an `await` has to be wrapped in the `within` it hands out. The first request of each action is issued synchronously (`api.*` → `request` → `send` → `fetch`, no `await` before it). That makes the write (`POST /outputs`, `POST …/send`, `POST …/run`) a child of the action. Generate also wraps its later thumbnail `PUT` and project filing. Print's `GET /print/runs/{id}` polls are not wrapped: each is a fetch span of its own trace. The backend work they report on is already in the `POST`'s trace.
- **`service.version`** needs the build's version in the bundle. The Dockerfile's `frontend` stage gets `ARG SCADBUDDY_VERSION=dev` right before `pnpm build`, and `build-image.yml` already passes that build arg. Cost: the frontend build step reruns in every image build whose version label changes (`sha-<short>`). This is accepted, and it does not touch `ci.yml`'s builds, which pass no version.
- **The relay's name and array caps** (§5.2: names of 128 characters, arrays of 32 items) are not SDK `spanLimits`. The browser's `ScrubbingSpanExporter` applies them, so "a well-behaved page never hits them" still holds.
- **URL queries.** §6 says URLs are recorded without the query. The fetch instrumentation writes `url.full`, and document-load writes `url.full` with `location.href`. The scrubber strips the query and the fragment from `url.full` and `http.url` before export.
- **Chat `traceparent`** (§4). The client half ships here: an optional `traceparent` on `user.message`, taken from an `assistant.message` span (`root: true`, so each turn is its own trace). The agent's `ClientMessageSchema` (`agent/src/sessions/clientProtocol.ts`) is a zod 4 `z.object`, which strips unknown keys. So until row 3 reads the field, an agent ignores it, and nothing in the agent changes in this PR.
- **`RelayExporter`'s result.** It reports `SUCCESS` when every request got a 2xx, or the exporter is off. Otherwise it reports `FAILED`. `BatchSpanProcessor` never retries, so `FAILED` only reaches the SDK's error handler (a no-op without a `diag` logger).
- **`fetch` + `keepalive`, not `sendBeacon`.** A beacon cannot read the response, so it would never see the off header.
- **The page CSP** (`connect-src 'self'`) already allows the relay. It also blocks every cross-origin `fetch` in production. The e2e test lifts it (`bypassCSP`) to prove that the instrumentation would not inject into one either.
- **Code conflicts with the spec:** none found. The spec's versions are still the latest, and `origin_allowed` (`backend/scadbuddy/api/realtime.py`) accepts a loopback `Origin` as §5.3 says.

---

### Task 1: Dependencies, `traceAction` and the test provider

**Files:**
- Modify: `frontend/package.json`, `frontend/pnpm-lock.yaml` (by `pnpm add`)
- Create: `frontend/src/lib/traceAction.ts`
- Create: `frontend/src/test/tracing.ts`
- Test: `frontend/src/lib/traceAction.test.ts`

**Interfaces:**
- Produces:
  - `export const TRACER_NAME = 'scadbuddy-web'`
  - `export type Within = <R>(call: () => R) => R`
  - `export function failureClass(error: unknown): string`
  - `export function traceparentOf(span: Span): string | undefined`
  - `export async function traceAction<T>(name: string, attributes: Attributes, run: (within: Within, span: Span) => Promise<T>): Promise<T>`
  - `export function messageTraceparent(): string | undefined` (makes and ends an `assistant.message` root span)
  - `src/test/tracing.ts`: `export function installTestTracing(): { exporter: InMemorySpanExporter; uninstall: () => void }`

- [ ] **Step 1: Add the dependencies**

```bash
cd frontend
pnpm add --save-exact @opentelemetry/api@1.9.1 @opentelemetry/core@2.11.0 \
  @opentelemetry/resources@2.11.0 @opentelemetry/sdk-trace-web@2.11.0 \
  @opentelemetry/otlp-transformer@0.222.0 @opentelemetry/instrumentation@0.222.0 \
  @opentelemetry/instrumentation-fetch@0.222.0 \
  @opentelemetry/instrumentation-document-load@0.67.0
```

These are all eight now, so the lockfile changes once. Later tasks import them.
`@opentelemetry/instrumentation` pulls in `import-in-the-middle` and `require-in-the-middle`
for Node, and its `browser` field keeps both out of the bundle (Task 4 checks this). No package needs a build script, so
`pnpm-workspace.yaml`'s `allowBuilds` is unchanged. Confirm with
`pnpm install --frozen-lockfile`. It should finish with no `ERR_PNPM_IGNORED_BUILDS`.

- [ ] **Step 2: Write the test provider**

```ts
// frontend/src/test/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  StackContextManager,
  WebTracerProvider,
} from '@opentelemetry/sdk-trace-web'

/**
 * A registered provider whose spans land in `exporter`, for tests of code that makes
 * spans. `uninstall` (call it in `afterEach` or a `finally`) puts the API back to its
 * no-op state, so the next test starts untraced.
 */
export function installTestTracing(): { exporter: InMemorySpanExporter; uninstall: () => void } {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] })
  provider.register({ contextManager: new StackContextManager(), propagator: null })
  return {
    exporter,
    uninstall: () => {
      void provider.shutdown()
      trace.disable()
      context.disable()
      propagation.disable()
    },
  }
}
```

- [ ] **Step 3: Write the failing tests**

```ts
// frontend/src/lib/traceAction.test.ts
import { SpanStatusCode, trace } from '@opentelemetry/api'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../api/client'
import { installTestTracing } from '../test/tracing'
import { failureClass, messageTraceparent, traceAction, traceparentOf } from './traceAction'

describe('traceAction', () => {
  let tracing: ReturnType<typeof installTestTracing>
  beforeEach(() => {
    tracing = installTestTracing()
  })
  afterEach(() => tracing.uninstall())

  it('records one span named after the action, with its attributes', async () => {
    await expect(traceAction('generate', { 'scadbuddy.slug': 'box' }, async () => 7)).resolves.toBe(7)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.name).toBe('generate')
    expect(span?.attributes['scadbuddy.slug']).toBe('box')
    expect(span?.status.code).not.toBe(SpanStatusCode.ERROR)
  })

  it('makes the span active for the synchronous start and for every `within` call', async () => {
    const seen: (string | undefined)[] = []
    await traceAction('send', {}, async (within) => {
      seen.push(trace.getActiveSpan()?.spanContext().spanId)
      await Promise.resolve()
      seen.push(trace.getActiveSpan()?.spanContext().spanId)
      within(() => seen.push(trace.getActiveSpan()?.spanContext().spanId))
    })
    const id = tracing.exporter.getFinishedSpans()[0]?.spanContext().spanId
    // After the await, only `within` restores the action's span.
    expect(seen).toEqual([id, undefined, id])
  })

  it('marks a failure with its class, never its message, and rethrows it', async () => {
    const refused = new ApiError({ type: 'https://scadbuddy.dev/problems/x', title: 't', status: 409, detail: 'SECRET' })
    await expect(traceAction('print', {}, async () => Promise.reject(refused))).rejects.toBe(refused)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.status.code).toBe(SpanStatusCode.ERROR)
    expect(span?.attributes['scadbuddy.failure_class']).toBe('https://scadbuddy.dev/problems/x')
    expect(JSON.stringify(span?.attributes)).not.toContain('SECRET')
    expect(span?.status.message ?? '').not.toContain('SECRET')
  })

  it('gives the chat turn a traceparent of a span of its own', () => {
    const traceparent = messageTraceparent()
    expect(traceparent).toMatch(/^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/)
    const [span] = tracing.exporter.getFinishedSpans()
    expect(span?.name).toBe('assistant.message')
    expect(traceparent).toContain(span?.spanContext().spanId)
  })
})

describe('without a registered provider', () => {
  it('runs the action untraced and has no traceparent to send', async () => {
    await expect(traceAction('generate', {}, async () => 'ok')).resolves.toBe('ok')
    expect(messageTraceparent()).toBeUndefined()
    expect(traceparentOf(trace.getTracer('t').startSpan('x'))).toBeUndefined()
  })
})

describe('failureClass', () => {
  it('names an ApiError by its problem type, or its status when it has none', () => {
    expect(failureClass(new ApiError({ type: 'urn:scadbuddy:unanswered', title: 'No answer', status: 0 }))).toBe(
      'urn:scadbuddy:unanswered',
    )
    expect(failureClass(new ApiError({ type: 'about:blank', title: 'Conflict', status: 409 }))).toBe('http-409')
    expect(failureClass(new ApiError(503, 'down'))).toBe('http-503')
  })

  it('names anything else by its name', () => {
    expect(failureClass(new TypeError('x'))).toBe('TypeError')
    expect(failureClass(new DOMException('gone', 'AbortError'))).toBe('AbortError')
    expect(failureClass('a string')).toBe('error')
  })
})
```

- [ ] **Step 4: Run them to see them fail**

Run: `cd frontend && pnpm test src/lib/traceAction.test.ts`
Expected: FAIL. The import of `./traceAction` cannot be resolved.

- [ ] **Step 5: Implement `traceAction.ts`**

```ts
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
 * A failure sets `ERROR` and `scadbuddy.failure_class`, and is rethrown unchanged.
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
```

- [ ] **Step 6: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/lib/traceAction.test.ts`
Expected: PASS, 7 tests.

- [ ] **Step 7: Lint and typecheck**

Run: `cd frontend && pnpm lint && pnpm typecheck`
Expected: no errors. The pre-existing `react-refresh` warning in `SessionBudget.tsx` is not ours.

- [ ] **Step 8: Commit**

```bash
git add frontend/package.json frontend/pnpm-lock.yaml frontend/src/lib/traceAction.ts \
  frontend/src/lib/traceAction.test.ts frontend/src/test/tracing.ts
git commit -m "feat(tracing): browser action spans on the OpenTelemetry API (#988)"
```

---

### Task 2: The browser's `ScrubbingSpanExporter`

**Files:**
- Create: `frontend/src/lib/traceScrub.ts`
- Test: `frontend/src/lib/traceScrub.test.ts`

**Interfaces:**
- Consumes: nothing from Task 1 at run time (types from `@opentelemetry/sdk-trace-web`, `@opentelemetry/core`).
- Produces:
  - `export const SPAN_NAME_MAX = 128`, `export const ARRAY_ITEMS_MAX = 32`
  - `export function frameLines(stack: string): string`
  - `export function withoutQuery(url: string): string`
  - `export function scrubSpan(span: ReadableSpan): ReadableSpan`
  - `export class ScrubbingSpanExporter implements SpanExporter { constructor(inner: SpanExporter) }`

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/traceScrub.test.ts
import { SpanStatusCode, trace } from '@opentelemetry/api'
import { ExportResultCode, type ExportResult } from '@opentelemetry/core'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  WebTracerProvider,
  type ReadableSpan,
} from '@opentelemetry/sdk-trace-web'
import { describe, expect, it } from 'vitest'
import { ARRAY_ITEMS_MAX, frameLines, ScrubbingSpanExporter, scrubSpan, SPAN_NAME_MAX } from './traceScrub'

const SENTINEL = 'SENTINEL-4f1c'

/** Spans made by a real (unregistered) provider, so they are the SDK's own objects. */
function record(make: (tracer: ReturnType<WebTracerProvider['getTracer']>) => void): ReadableSpan[] {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({ spanProcessors: [new SimpleSpanProcessor(exporter)] })
  make(provider.getTracer('test'))
  return exporter.getFinishedSpans()
}

const CHROME_STACK = [
  `TypeError: ${SENTINEL}`,
  '    at send (http://localhost:5173/src/api/client.ts:244:18)',
  '    at http://localhost:5173/assets/index-abc.js:1:2345',
  `Caused by: Error: ${SENTINEL}`,
].join('\n')

const FIREFOX_STACK = [
  `send@http://localhost:5173/src/api/client.ts:244:18`,
  `@http://localhost:5173/assets/index-abc.js:1:2345`,
  SENTINEL,
].join('\n')

describe('frameLines', () => {
  it('keeps only Chromium frame lines', () => {
    expect(frameLines(CHROME_STACK)).toBe(
      'at send (http://localhost:5173/src/api/client.ts:244:18)\nat http://localhost:5173/assets/index-abc.js:1:2345',
    )
  })

  it('keeps only Firefox and Safari frame lines', () => {
    expect(frameLines(FIREFOX_STACK)).toBe(
      'send@http://localhost:5173/src/api/client.ts:244:18\n@http://localhost:5173/assets/index-abc.js:1:2345',
    )
  })
})

describe('scrubSpan', () => {
  it('drops the exception message, keeps the type and the frames, and replaces the status description', () => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('generate')
      s.recordException({ name: 'TypeError', message: SENTINEL, stack: CHROME_STACK })
      s.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
      s.end()
    })
    const scrubbed = scrubSpan(span!)
    expect(JSON.stringify({ a: scrubbed.attributes, e: scrubbed.events, s: scrubbed.status })).not.toContain(SENTINEL)
    const event = scrubbed.events[0]!
    expect(event.attributes?.['exception.type']).toBe('TypeError')
    expect(event.attributes?.['exception.stacktrace']).toContain('at send (')
    expect(scrubbed.status).toEqual({ code: SpanStatusCode.ERROR, message: 'TypeError' })
  })

  it('says `error` for a status description with no exception to name', () => {
    const [span] = record((tracer) => {
      const s = tracer.startSpan('send')
      s.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
      s.end()
    })
    expect(scrubSpan(span!).status).toEqual({ code: SpanStatusCode.ERROR, message: 'error' })
  })

  it('records URLs without their query or fragment', () => {
    const [span] = record((tracer) => {
      tracer
        .startSpan('GET', {
          attributes: {
            'url.full': `http://localhost:5173/api/v1/models?q=${SENTINEL}#frag`,
            'http.url': `http://localhost:5173/m/box?values=${SENTINEL}`,
          },
        })
        .end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.attributes['url.full']).toBe('http://localhost:5173/api/v1/models')
    expect(scrubbed.attributes['http.url']).toBe('http://localhost:5173/m/box')
  })

  it('caps the name and arrays the SDK limits cannot, and counts each capped array', () => {
    const [span] = record((tracer) => {
      tracer
        .startSpan('x'.repeat(SPAN_NAME_MAX + 1), {
          attributes: { many: Array.from({ length: ARRAY_ITEMS_MAX + 1 }, (_, i) => i), few: [1, 2] },
        })
        .end()
    })
    const scrubbed = scrubSpan(span!)
    expect(scrubbed.name).toHaveLength(SPAN_NAME_MAX)
    expect(scrubbed.attributes['many']).toHaveLength(ARRAY_ITEMS_MAX)
    expect(scrubbed.attributes['few']).toEqual([1, 2])
    expect(scrubbed.droppedAttributesCount).toBe(span!.droppedAttributesCount + 1)
  })

  it('keeps the span context, so the exported span is still the one that was made', () => {
    const [span] = record((tracer) => tracer.startSpan('print').end())
    expect(scrubSpan(span!).spanContext()).toEqual(span!.spanContext())
    expect(trace.getActiveSpan()).toBeUndefined()
  })
})

describe('ScrubbingSpanExporter', () => {
  it('hands the inner exporter scrubbed spans and passes its result back', async () => {
    const inner = new InMemorySpanExporter()
    const [span] = record((tracer) => {
      const s = tracer.startSpan('send')
      s.recordException(new Error(SENTINEL))
      s.end()
    })
    const result = await new Promise<ExportResult>((resolve) =>
      new ScrubbingSpanExporter(inner).export([span!], resolve),
    )
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(JSON.stringify(inner.getFinishedSpans()[0]?.events)).not.toContain(SENTINEL)
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm test src/lib/traceScrub.test.ts`
Expected: FAIL. `./traceScrub` cannot be resolved.

- [ ] **Step 3: Implement `traceScrub.ts`**

```ts
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
```

(`tsconfig.app.json` sets `erasableSyntaxOnly`, so use a `#private` field and an
explicit constructor, not a constructor parameter property.)

- [ ] **Step 4: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/lib/traceScrub.test.ts`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/traceScrub.ts frontend/src/lib/traceScrub.test.ts
git commit -m "feat(tracing): scrub browser spans before they leave the page (#988)"
```

---

### Task 3: `RelayExporter` and the relay's msw mock

**Files:**
- Create: `frontend/src/mocks/features/telemetry.ts`
- Create: `frontend/src/lib/relayExporter.ts`
- Test: `frontend/src/lib/relayExporter.test.ts`

**Interfaces:**
- Produces:
  - `export const RELAY_PATH = '/telemetry/v1/traces'`
  - `export const TRACING_HEADER = 'X-ScadBuddy-Tracing'`
  - `export const MAX_REQUEST_BYTES = 48 * 1024`
  - `export class RelayExporter implements SpanExporter { droppedSpans: number; get off(): boolean; export(...); forceFlush(); shutdown() }`
  - msw: `POST /telemetry/v1/traces` answers `204` with `X-ScadBuddy-Tracing: off` (the default for every test and the mocked e2e).

- [ ] **Step 1: Add the relay's mock**

```ts
// frontend/src/mocks/features/telemetry.ts
import { HttpResponse, http } from 'msw'

/**
 * The backend's browser relay, `POST /telemetry/v1/traces` (tracing spec 2026-10-01
 * §5.2), answering as it does with tracing off: `204` and `X-ScadBuddy-Tracing: off`.
 * The page's exporter (`src/lib/relayExporter.ts`) then stops for the rest of its
 * life, so vitest and the mocked e2e never export (§5.3). A test of the exporter's
 * other answers overrides this with `server.use`.
 */
export const handlers = [
  http.post('/telemetry/v1/traces', () =>
    new HttpResponse(null, { status: 204, headers: { 'X-ScadBuddy-Tracing': 'off' } }),
  ),
]
```

`src/mocks/features.ts` picks it up without a line in `handlers.ts`, and
`features.test.ts` checks that it serves and that no other module declares the route.

- [ ] **Step 2: Write the failing tests**

```ts
// frontend/src/lib/relayExporter.test.ts
import { ExportResultCode, type ExportResult } from '@opentelemetry/core'
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  WebTracerProvider,
  type ReadableSpan,
} from '@opentelemetry/sdk-trace-web'
import { HttpResponse, delay, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { MAX_REQUEST_BYTES, RELAY_PATH, RelayExporter } from './relayExporter'

/** `count` finished spans, each carrying `padding` characters in one attribute. */
function spans(count: number, padding = 0): ReadableSpan[] {
  const exporter = new InMemorySpanExporter()
  const provider = new WebTracerProvider({
    spanProcessors: [new SimpleSpanProcessor(exporter)],
    // Past the 1024 the page's provider uses, to build a span over MAX_REQUEST_BYTES.
    spanLimits: { attributeValueLengthLimit: 100_000 },
  })
  const tracer = provider.getTracer('test')
  for (let i = 0; i < count; i += 1) {
    tracer.startSpan(`span-${i}`, { attributes: { pad: 'x'.repeat(padding) } }).end()
  }
  return exporter.getFinishedSpans()
}

function exportOnce(exporter: RelayExporter, batch: ReadableSpan[]): Promise<ExportResult> {
  return new Promise((resolve) => exporter.export(batch, resolve))
}

interface Seen {
  bodies: { spanNames: string[]; bytes: number; keepalive: boolean; contentType: string | null }[]
  maxInFlight: number
}

/** The relay accepting every batch (204, tracing on), after `wait` ms, recording what came. */
function acceptingRelay(wait = 0): Seen {
  const seen: Seen = { bodies: [], maxInFlight: 0 }
  let inFlight = 0
  server.use(
    http.post(RELAY_PATH, async ({ request }) => {
      inFlight += 1
      seen.maxInFlight = Math.max(seen.maxInFlight, inFlight)
      const text = await request.text()
      const json = JSON.parse(text) as {
        resourceSpans: { scopeSpans: { spans: { name: string }[] }[] }[]
      }
      seen.bodies.push({
        spanNames: json.resourceSpans.flatMap((r) => r.scopeSpans.flatMap((s) => s.spans.map((x) => x.name))),
        bytes: new TextEncoder().encode(text).byteLength,
        keepalive: request.keepalive,
        contentType: request.headers.get('content-type'),
      })
      await delay(wait)
      inFlight -= 1
      return new HttpResponse(null, { status: 204 })
    }),
  )
  return seen
}

describe('RelayExporter', () => {
  it('posts a batch as OTLP/JSON with keepalive, and reports success on 204', async () => {
    const seen = acceptingRelay()
    const result = await exportOnce(new RelayExporter(), spans(3))
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toEqual([
      { spanNames: ['span-0', 'span-1', 'span-2'], bytes: expect.any(Number), keepalive: true, contentType: 'application/json' },
    ])
  })

  it('splits a batch over 48 KiB into requests that each fit, losing no span', async () => {
    const seen = acceptingRelay()
    const batch = spans(64, 2_000)
    const result = await exportOnce(new RelayExporter(), batch)
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies.length).toBeGreaterThan(1)
    for (const body of seen.bodies) expect(body.bytes).toBeLessThanOrEqual(MAX_REQUEST_BYTES)
    expect(seen.bodies.flatMap((b) => b.spanNames)).toEqual(batch.map((s) => s.name))
  })

  it('drops and counts a single span over 48 KiB, and still sends the rest', async () => {
    const seen = acceptingRelay()
    const exporter = new RelayExporter()
    const [huge] = spans(1, MAX_REQUEST_BYTES)
    const result = await exportOnce(exporter, [huge!, ...spans(2)])
    expect(result.code).toBe(ExportResultCode.SUCCESS)
    expect(exporter.droppedSpans).toBe(1)
    expect(seen.bodies.flatMap((b) => b.spanNames)).toEqual(['span-0', 'span-1'])
  })

  it('sends one request at a time, within a batch and across batches', async () => {
    const seen = acceptingRelay(20)
    const exporter = new RelayExporter()
    const results = await Promise.all([
      exportOnce(exporter, spans(64, 2_000)),
      exportOnce(exporter, spans(2)),
      exportOnce(exporter, spans(2)),
    ])
    expect(results.map((r) => r.code)).toEqual([ExportResultCode.SUCCESS, ExportResultCode.SUCCESS, ExportResultCode.SUCCESS])
    expect(seen.bodies.length).toBeGreaterThan(3)
    expect(seen.maxInFlight).toBe(1)
  })

  it('switches itself off on X-ScadBuddy-Tracing: off and never sends again', async () => {
    // The default mock (src/mocks/features/telemetry.ts) is the relay with tracing off.
    let requests = 0
    const count = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname === RELAY_PATH) requests += 1
    }
    server.events.on('request:start', count)
    try {
      const exporter = new RelayExporter()
      expect((await exportOnce(exporter, spans(64, 2_000))).code).toBe(ExportResultCode.SUCCESS)
      expect(exporter.off).toBe(true)
      expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
      // The first request said off: the rest of the split batch and the next batch stayed home.
      expect(requests).toBe(1)
    } finally {
      server.events.removeListener('request:start', count)
    }
  })

  it('switches off when the relay starts saying off after accepting batches', async () => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return new HttpResponse(null, {
          status: 204,
          headers: calls === 1 ? {} : { 'X-ScadBuddy-Tracing': 'off' },
        })
      }),
    )
    const exporter = new RelayExporter()
    await exportOnce(exporter, spans(2))
    expect(exporter.off).toBe(false)
    await exportOnce(exporter, spans(2))
    expect(exporter.off).toBe(true)
    await exportOnce(exporter, spans(2))
    expect(calls).toBe(2)
  })

  it.each([403, 413, 429, 503])('drops the batch on %i without retrying, and sends the next one', async (status) => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return calls === 1
          ? HttpResponse.json(
              { type: 'about:blank', title: 'refused', status },
              {
                status,
                headers: { 'Content-Type': 'application/problem+json', ...(status === 429 ? { 'Retry-After': '1' } : {}) },
              },
            )
          : new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    const first = await exportOnce(exporter, spans(64, 2_000))
    expect(first.code).toBe(ExportResultCode.FAILED)
    // The refused request ended the batch: its other parts were not sent.
    expect(calls).toBe(1)
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
    expect(calls).toBe(2)
    expect(exporter.off).toBe(false)
  })

  it('drops the batch when fetch rejects, and sends the next one', async () => {
    let calls = 0
    server.use(
      http.post(RELAY_PATH, () => {
        calls += 1
        return calls === 1 ? HttpResponse.error() : new HttpResponse(null, { status: 204 })
      }),
    )
    const exporter = new RelayExporter()
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.FAILED)
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
  })

  it('waits for the request in flight on forceFlush and shutdown, and sends nothing after shutdown', async () => {
    const seen = acceptingRelay(20)
    const exporter = new RelayExporter()
    exporter.export(spans(2), () => {})
    await exporter.forceFlush()
    expect(seen.bodies).toHaveLength(1)
    await exporter.shutdown()
    expect((await exportOnce(exporter, spans(2))).code).toBe(ExportResultCode.SUCCESS)
    expect(seen.bodies).toHaveLength(1)
  })
})
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd frontend && pnpm test src/lib/relayExporter.test.ts`
Expected: FAIL. `./relayExporter` cannot be resolved.

- [ ] **Step 4: Implement `relayExporter.ts`**

```ts
// frontend/src/lib/relayExporter.ts
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
 *   fits; a single span still over it is dropped and counted in `droppedSpans`.
 * - Requests go one at a time, across `export` calls too, so in-flight `keepalive`
 *   bytes stay under the browser's cap. `keepalive` lets a batch flushed as the page
 *   hides still go.
 * - `X-ScadBuddy-Tracing: off` switches the exporter off for the rest of the page's
 *   life: every later batch is reported a success and never sent.
 * - Anything else that is not a 2xx (413, 429, 403, 503), or a `fetch` that rejects,
 *   drops the batch's remaining requests and reports the batch failed. Nothing is
 *   retried: the relay's 429 says not to, and a page going away cannot wait.
 */
export class RelayExporter implements SpanExporter {
  /** Spans dropped because one alone was over `MAX_REQUEST_BYTES`. */
  droppedSpans = 0
  #off = false
  /** The request in flight, if any; the next one starts after it settles. */
  #tail: Promise<unknown> = Promise.resolve()

  /** The relay said tracing is off; nothing more is sent. */
  get off(): boolean {
    return this.#off
  }

  export(spans: ReadableSpan[], resultCallback: (result: ExportResult) => void): void {
    const sent = this.#tail.then(() => this.#send(spans))
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
    for (const body of this.#bodies(spans)) {
      let response: Response
      try {
        // Untraced: the relay's own request must not become a span to export.
        response = await context.with(suppressTracing(context.active()), () =>
          fetch(RELAY_PATH, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body,
            keepalive: true,
          }),
        )
      } catch (error) {
        return failed(error)
      }
      if (response.headers.get(TRACING_HEADER) === 'off') {
        this.#off = true
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
      if (bytes && bytes.byteLength <= MAX_REQUEST_BYTES) {
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
```

Notes for the implementer:
- `fetch` is looked up when the request is sent, not captured at construction. That way
  msw (in tests) and the fetch instrumentation (in the page) both see the global one.
  `suppressTracing` makes the instrumentation's span for this request a non-recording
  one. Task 4 also lists the relay URL in the instrumentation's `ignoreUrls`.
- The body is a string, decoded from the serializer's UTF-8 bytes. A string's UTF-8
  length is exactly `bytes.byteLength`, and a string avoids TS 6's `Uint8Array<ArrayBufferLike>` vs
  `BodyInit` friction.
- The `#tail` chain reports a failure as an `ExportResult`, and never rejects. Every
  path through `#send` returns, so the chain can never get stuck on a rejected promise.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/lib/relayExporter.test.ts src/mocks/features.test.ts`
Expected: PASS: 12 tests in `relayExporter.test.ts`, and `features.test.ts` still green.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/lib/relayExporter.ts frontend/src/lib/relayExporter.test.ts \
  frontend/src/mocks/features/telemetry.ts
git commit -m "feat(tracing): RelayExporter posts browser spans to the relay, off on its signal (#988)"
```

---

### Task 4: The SDK (`tracing.ts`), loaded after the first paint, and the build's version

**Files:**
- Create: `frontend/src/lib/tracing.ts`
- Modify: `frontend/src/main.tsx` (after `createRoot(root).render(…)`)
- Modify: `frontend/src/vite-env.d.ts` (`ImportMetaEnv`)
- Modify: `Dockerfile` (the `frontend` stage, around `COPY frontend/ ./` / `RUN pnpm build`, lines 158–159)
- Modify: `CLAUDE.md` (Layout, after the `frontend/src/` bullet)
- Test: `frontend/src/lib/tracing.test.ts`

**Interfaces:**
- Consumes: `RELAY_PATH`, `RelayExporter` (Task 3); `ScrubbingSpanExporter` (Task 2); `TRACER_NAME`, `traceAction` (Task 1).
- Produces:
  - `export const SPAN_LIMITS: SpanLimits`, `export const MAX_EXPORT_BATCH_SIZE = 64`
  - `export function startTracing(): () => Promise<void>`: idempotent. It returns the function that undoes it (for tests).

- [ ] **Step 1: Write the failing tests**

```ts
// frontend/src/lib/tracing.test.ts
import { trace } from '@opentelemetry/api'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { traceAction } from './traceAction'
import { startTracing } from './tracing'

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/
const CROSS_ORIGIN = 'https://fonts.googleapis.com/css2'

/** The `traceparent` each request to `url` arrived with (null: none). */
function capture(url: string): (string | null)[] {
  const seen: (string | null)[] = []
  server.use(
    http.get(url, ({ request }) => {
      seen.push(request.headers.get('traceparent'))
      return HttpResponse.json({})
    }),
  )
  return seen
}

describe('startTracing', () => {
  let stop: (() => Promise<void>) | undefined
  afterEach(async () => {
    await stop?.()
    stop = undefined
  })

  it('injects traceparent into a same-origin request', async () => {
    const seen = capture('/api/v1/models')
    stop = startTracing()
    await fetch('/api/v1/models')
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatch(TRACEPARENT)
  })

  it('never injects traceparent into a cross-origin request', async () => {
    const foreign = capture(CROSS_ORIGIN)
    const own = capture('/api/v1/models')
    stop = startTracing()
    await fetch(CROSS_ORIGIN)
    await fetch('/api/v1/models')
    expect(foreign).toEqual([null])
    // The same page, the same moment, its own origin: injected, so the absence is the origin's doing.
    expect(own[0]).toMatch(TRACEPARENT)
  })

  it('parents an action’s first request on the action’s span', async () => {
    const seen = capture('/api/v1/outputs')
    stop = startTracing()
    let actionTrace: string | undefined
    await traceAction('generate', {}, async () => {
      actionTrace = trace.getActiveSpan()?.spanContext().traceId
      await fetch('/api/v1/outputs')
    })
    expect(seen[0]?.split('-')[1]).toBe(actionTrace)
  })

  it('carries no baggage header', async () => {
    let baggage: string | null = 'unset'
    server.use(
      http.get('/api/v1/models', ({ request }) => {
        baggage = request.headers.get('baggage')
        return HttpResponse.json([])
      }),
    )
    stop = startTracing()
    await fetch('/api/v1/models')
    expect(baggage).toBeNull()
  })

  it('is started once however often it is called', () => {
    stop = startTracing()
    expect(startTracing()).toBe(stop)
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm test src/lib/tracing.test.ts`
Expected: FAIL. `./tracing` cannot be resolved.

- [ ] **Step 3: Declare the build's version**

In `frontend/src/vite-env.d.ts`, change `ImportMetaEnv` to:

```ts
interface ImportMetaEnv {
  readonly VITE_MOCK_API?: string
  /** The build's version (the Dockerfile's `SCADBUDDY_VERSION`), as the browser's `service.version`. */
  readonly VITE_SCADBUDDY_VERSION?: string
}
```

- [ ] **Step 4: Implement `tracing.ts`**

```ts
// frontend/src/lib/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { registerInstrumentations } from '@opentelemetry/instrumentation'
import { DocumentLoadInstrumentation } from '@opentelemetry/instrumentation-document-load'
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch'
import { resourceFromAttributes } from '@opentelemetry/resources'
import {
  BatchSpanProcessor,
  StackContextManager,
  WebTracerProvider,
  type SpanLimits,
} from '@opentelemetry/sdk-trace-web'
import { RELAY_PATH, RelayExporter } from './relayExporter'
import { TRACER_NAME } from './traceAction'
import { ScrubbingSpanExporter } from './traceScrub'

/** The relay's per-span caps (tracing spec 2026-10-01 §5.2), as the SDK's limits. */
export const SPAN_LIMITS: SpanLimits = {
  attributeCountLimit: 64,
  attributeValueLengthLimit: 1024,
  eventCountLimit: 16,
  attributePerEventCountLimit: 16,
  linkCountLimit: 8,
}
/** §5.3: with `RelayExporter`'s 48 KiB requests, keeps a flush under the keepalive cap. */
export const MAX_EXPORT_BATCH_SIZE = 64

let stop: (() => Promise<void>) | null = null

/**
 * The page's tracing (§5.3), loaded lazily by `main.tsx` after the first paint:
 * a `WebTracerProvider` whose `BatchSpanProcessor` exports through
 * `ScrubbingSpanExporter` → `RelayExporter`; W3C trace context only, no baggage (§4);
 * fetch instrumentation that injects `traceparent` only into same-origin requests
 * (never Bambuddy or Google Fonts), and document-load instrumentation. Calling it
 * again does nothing. Returns the function that undoes it, for tests.
 */
export function startTracing(): () => Promise<void> {
  if (stop) return stop
  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({
      'service.name': TRACER_NAME,
      'service.version': import.meta.env.VITE_SCADBUDDY_VERSION || 'dev',
      'user_agent.original': navigator.userAgent,
    }),
    spanLimits: SPAN_LIMITS,
    spanProcessors: [
      new BatchSpanProcessor(new ScrubbingSpanExporter(new RelayExporter()), {
        maxExportBatchSize: MAX_EXPORT_BATCH_SIZE,
      }),
    ],
  })
  provider.register({
    contextManager: new StackContextManager(),
    propagator: new W3CTraceContextPropagator(),
  })
  const unload = registerInstrumentations({
    tracerProvider: provider,
    instrumentations: [
      new FetchInstrumentation({
        // Empty: same-origin only. The instrumentation always allows the page's own origin.
        propagateTraceHeaderCorsUrls: [],
        ignoreUrls: [`${location.origin}${RELAY_PATH}`],
      }),
      new DocumentLoadInstrumentation(),
    ],
  })
  const undo = async () => {
    unload()
    await provider.shutdown()
    trace.disable()
    context.disable()
    propagation.disable()
    stop = null
  }
  stop = undo
  return undo
}
```

Why these settings:
- `propagator` must be passed. `register()`'s default is a composite of trace context
  *and baggage*, which §4 rules out.
- `propagateTraceHeaderCorsUrls: []`: `shouldPropagateTraceHeaders`
  (`@opentelemetry/sdk-trace-web`) is true for the page's own origin and for nothing
  else in the list.
- `ignoreUrls` takes an exact string, matched against the absolute URL, so only the
  relay is ignored.
- `user_agent.original` and `service.version` are the two resource attributes the
  relay keeps (§5.2). It replaces `service.name` in any case.
- The browser `BatchSpanProcessor` flushes on `visibilitychange`/`pagehide` itself
  (`disableAutoFlushOnDocumentHide` left false). With `keepalive`, that flush still goes.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/lib/tracing.test.ts`
Expected: PASS, 5 tests. (Fetch spans end 300 ms after the response. `afterEach`
awaits `provider.shutdown()`, so nothing leaks into the next test.)

- [ ] **Step 6: Load it after the first paint**

In `frontend/src/main.tsx`, call the loader at the end of `start()` and add the function
below it:

```tsx
  createRoot(root).render(
    <StrictMode>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </StrictMode>,
  )
  loadTracingAfterFirstPaint()
}

/**
 * Tracing spec 2026-10-01 §5.3: the SDK is its own chunk, fetched after the first
 * paint (the frame after the next one), so it never delays the page or the 3D viewer.
 * Until it loads, `lib/traceAction.ts` makes no-op spans. A chunk that fails to load
 * leaves the page untraced; `installStaleChunkReload` handles a stale deploy.
 */
function loadTracingAfterFirstPaint() {
  requestAnimationFrame(() => {
    setTimeout(() => {
      import('./lib/tracing').then(({ startTracing }) => startTracing()).catch(() => undefined)
    }, 0)
  })
}
```

(`void start()` stays the last line of the file. In the mocked build, msw's worker is
started before this runs, so the relay's mock answers the first flush.)

- [ ] **Step 7: Check the bundle split**

Run:

```bash
cd frontend && pnpm build
ls dist/assets | grep -E '^(index|tracing)-.*\.js$'
grep -cE 'import-in-the-middle|require-in-the-middle|ProtobufTraceSerializer' dist/assets/tracing-*.js
```

Expected: a separate `tracing-<hash>.js`. On 2026-10-03 it measured about 61 kB (19.6 kB
gzip). `index-<hash>.js` grows by about 9 kB (3 kB gzip), which is `@opentelemetry/api`
plus `traceAction.ts`. The `grep -c` prints `0`. If the tracing code lands in
`index-*.js` instead, something imports `./tracing` statically: only `main.tsx`'s
dynamic `import()` may.

- [ ] **Step 8: Stamp the version in the image's frontend build**

In `Dockerfile`, replace

```dockerfile
COPY frontend/ ./
RUN pnpm build
```

with

```dockerfile
COPY frontend/ ./
# The browser's `service.version` (tracing spec 2026-10-01 §3): the same label the
# runtime stage gets. Declared here, after the copy, so a new version reruns only this
# build step. build-image.yml passes it; ci.yml's builds keep the default.
ARG SCADBUDDY_VERSION=dev
RUN VITE_SCADBUDDY_VERSION="${SCADBUDDY_VERSION}" pnpm build
```

Verify:

```bash
docker build --target frontend --build-arg SCADBUDDY_VERSION=sha-988test -t scadbuddy-frontend:988 .
docker run --rm scadbuddy-frontend:988 sh -c 'grep -l sha-988test dist/assets/tracing-*.js'
hadolint --config .hadolint.yaml Dockerfile   # if hadolint is installed; the CI `lint` job runs it
```

Expected: the `grep` prints the one tracing chunk, and hadolint reports nothing new.

- [ ] **Step 9: Document where it lives**

In `CLAUDE.md` "Layout", after the `frontend/src/` bullet (the one that ends
"…and must export `handlers` (and optionally `reset`) (#508)."), add:

```markdown
- `frontend/src/lib/tracing.ts` — the browser's OpenTelemetry (#988), a lazy chunk
  `main.tsx` loads after the first paint; spans leave through `traceScrub.ts` and
  `relayExporter.ts` to the backend relay `/telemetry/v1/traces`, and stop for the
  page's life when it answers `X-ScadBuddy-Tracing: off` (msw always does,
  `src/mocks/features/telemetry.ts`). A user action is `traceAction` (`lib/traceAction.ts`,
  entry chunk, API only): a request issued after an `await` joins the action's trace
  only inside its `within`. `traceparent` goes on same-origin requests only.
```

- [ ] **Step 10: Lint, typecheck, commit**

Run: `cd frontend && pnpm lint && pnpm typecheck && pnpm test src/lib`
Expected: no errors, and every `src/lib` test passes.

```bash
git add frontend/src/lib/tracing.ts frontend/src/lib/tracing.test.ts frontend/src/main.tsx \
  frontend/src/vite-env.d.ts Dockerfile CLAUDE.md
git commit -m "feat(tracing): load the browser SDK after first paint, same-origin traceparent only (#988)"
```

---

### Task 5: Generate, Print and Send as spans

**Files:**
- Modify: `frontend/src/lib/saveOutput.ts`
- Modify: `frontend/src/components/ActionBar.tsx` (`generate()`, about line 165)
- Modify: `frontend/src/lib/useRunPrint.ts` (`run()`, the `sourceApi(source).run(…)` call, about line 133)
- Modify: `frontend/src/components/SendDialog.tsx` (`send()`, about line 47)
- Test: `frontend/src/components/ActionBar.test.tsx`, `frontend/src/lib/useRunPrint.test.ts`, `frontend/src/components/SendDialog.test.tsx` (new)

**Interfaces:**
- Consumes: `traceAction`, `type Within` (Task 1); `installTestTracing` (Task 1).
- Produces: spans `generate` (`scadbuddy.slug`, `scadbuddy.job_id`, `scadbuddy.output_id`),
  `print` (`scadbuddy.output_id` or `scadbuddy.library_file_id`, `scadbuddy.printer_id`
  when set, `scadbuddy.plate_id`, `scadbuddy.all_plates`), and `send` (`scadbuddy.output_id`).
  `saveOutput` gains an optional `within?: Within`.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/components/ActionBar.test.tsx`, and replace its import block
with:

```tsx
import { trace } from '@opentelemetry/api'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { NO_EXTRA } from '../lib/inputs'
import { outputs } from '../mocks/fixtures'
import { installTestTracing } from '../test/tracing'
import { renderPage } from '../test/utils'
import { ActionBar } from './ActionBar'
```

```tsx
describe('Generate, traced', () => {
  it('is one generate span, with the output request and the later thumbnail inside it', async () => {
    const tracing = installTestTracing()
    try {
      const active: Record<string, string | undefined> = {}
      vi.spyOn(api, 'createOutput').mockImplementation(async () => {
        active.create = trace.getActiveSpan()?.spanContext().spanId
        return outputs[0]!
      })
      vi.spyOn(api, 'putThumbnail').mockImplementation(async () => {
        active.thumbnail = trace.getActiveSpan()?.spanContext().spanId
      })
      const onGenerated = vi.fn()
      const { user } = setup(false, job, undefined, {
        onGenerated,
        capture: async () => new Blob(['png'], { type: 'image/png' }),
      })
      await user.click(screen.getByRole('button', { name: 'Generate' }))
      await waitFor(() => expect(onGenerated).toHaveBeenCalled())
      await waitFor(() => expect(tracing.exporter.getFinishedSpans()).toHaveLength(1))

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('generate')
      expect(span?.attributes).toEqual({
        'scadbuddy.slug': 'name-keychain',
        'scadbuddy.job_id': job.id,
        'scadbuddy.output_id': outputs[0]!.id,
      })
      // The thumbnail goes after an await (the capture): only `within` keeps it in the trace.
      expect(active).toEqual({ create: span?.spanContext().spanId, thumbnail: span?.spanContext().spanId })
    } finally {
      vi.restoreAllMocks()
      tracing.uninstall()
    }
  })
})
```

Append to `frontend/src/lib/useRunPrint.test.ts`, and add to its imports
`import { SpanStatusCode, trace } from '@opentelemetry/api'` (first line) and
`import { installTestTracing } from '../test/tracing'` (after the `../api/types` import):

```ts
describe('useRunPrint, traced', () => {
  afterEach(() => vi.restoreAllMocks())

  it('records the run as a print span naming the output, printer and plate, with the run request inside it', async () => {
    const tracing = installTestTracing()
    try {
      let active: string | undefined
      vi.spyOn(api, 'runPrint').mockImplementation(async () => {
        active = trace.getActiveSpan()?.spanContext().spanId
        return queuedResult
      })
      vi.spyOn(api, 'putModelChoices').mockResolvedValue(undefined as never)
      vi.spyOn(api, 'putPrinterBedType').mockResolvedValue(undefined as never)
      const { result } = renderHook(() => useRunPrint(input()))
      await act(() => result.current.run())

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('print')
      expect(span?.attributes).toEqual({
        'scadbuddy.output_id': OUTPUT,
        'scadbuddy.printer_id': 1,
        'scadbuddy.plate_id': 1,
        'scadbuddy.all_plates': true,
      })
      expect(active).toBe(span?.spanContext().spanId)
    } finally {
      tracing.uninstall()
    }
  })

  it('marks a refused run as an error with its problem type', async () => {
    const tracing = installTestTracing()
    try {
      vi.spyOn(api, 'runPrint').mockRejectedValue(
        new ApiError({ type: 'https://scadbuddy.dev/problems/unresolvable', title: 'No', status: 422, detail: 'secret' }),
      )
      const { result } = renderHook(() => useRunPrint(input()))
      await act(() => result.current.run())
      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.status.code).toBe(SpanStatusCode.ERROR)
      expect(span?.attributes['scadbuddy.failure_class']).toBe('https://scadbuddy.dev/problems/unresolvable')
    } finally {
      tracing.uninstall()
    }
  })
})
```

Create `frontend/src/components/SendDialog.test.tsx`:

```tsx
import { trace } from '@opentelemetry/api'
import { screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { outputs } from '../mocks/fixtures'
import { installTestTracing } from '../test/tracing'
import { renderPage } from '../test/utils'
import { SendDialog } from './SendDialog'

describe('SendDialog, traced', () => {
  afterEach(() => vi.restoreAllMocks())

  it('records Send as a send span naming the output, with the send request inside it', async () => {
    const tracing = installTestTracing()
    try {
      const output = outputs[0]!
      let active: string | undefined
      const sendOutput = api.sendOutput
      vi.spyOn(api, 'sendOutput').mockImplementation((id, body) => {
        active = trace.getActiveSpan()?.spanContext().spanId
        return sendOutput(id, body)
      })
      const onSent = vi.fn()
      const { user } = renderPage(<SendDialog open output={output} onClose={() => {}} onSent={onSent} />)
      await user.click(screen.getByRole('button', { name: 'Send' }))
      await waitFor(() => expect(onSent).toHaveBeenCalled())

      const [span] = tracing.exporter.getFinishedSpans()
      expect(span?.name).toBe('send')
      expect(span?.attributes).toEqual({ 'scadbuddy.output_id': output.id })
      expect(active).toBe(span?.spanContext().spanId)
    } finally {
      tracing.uninstall()
    }
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm test src/components/ActionBar.test.tsx src/lib/useRunPrint.test.ts src/components/SendDialog.test.tsx`
Expected: the three new "traced" describes FAIL, because no span is recorded
(`span?.name` is `undefined`). Every existing test still passes.

- [ ] **Step 3: `saveOutput` takes the action's `within`**

Replace `frontend/src/lib/saveOutput.ts` with:

```ts
import { api } from '../api/client'
import type { Job, Output } from '../api/types'
import { joinInputs, type InputsExtra } from './inputs'
import type { Within } from './traceAction'

/** Generate (spec §6): keep the render as an output, with the inputs on screen and the preview as its thumbnail. */
export async function saveOutput({
  slug,
  job,
  extra,
  capture,
  within = (call) => call(),
}: {
  slug: string
  job: Job
  extra: InputsExtra
  capture: () => Promise<Blob | null>
  /** Generate's span (`traceAction`), so the thumbnail's request, after an await, joins its trace. */
  within?: Within
}): Promise<Output> {
  const created = await api.createOutput(slug, job.id, undefined, joinInputs(job.params ?? {}, extra))
  const png = await capture()
  // A missing thumbnail is cosmetic: never fail the generate over it.
  if (png) await within(() => api.putThumbnail(created.id, png)).catch(() => undefined)
  return created
}
```

- [ ] **Step 4: Generate**

In `frontend/src/components/ActionBar.tsx`, add
`import { traceAction } from '../lib/traceAction'` after the `saveOutput` import. In
`generate()`, replace the body of the `try`:

```tsx
      const created = await saveOutput({ slug, job, extra, capture })
      onGenerated(created)
      // After the thumbnail, so the file Bambuddy lists carries the plate image.
      await fileIntoProject(created)
      return created
```

with:

```tsx
      return await traceAction(
        'generate',
        { 'scadbuddy.slug': slug, 'scadbuddy.job_id': job.id },
        async (within, span) => {
          const created = await saveOutput({ slug, job, extra, capture, within })
          span.setAttribute('scadbuddy.output_id', created.id)
          onGenerated(created)
          // After the thumbnail, so the file Bambuddy lists carries the plate image.
          await within(() => fileIntoProject(created))
          return created
        },
      )
```

The `catch`/`finally` stay as they are. The assistant's `generate` tool calls the same
`generate()`, so its runs are traced too.

- [ ] **Step 5: Print**

In `frontend/src/lib/useRunPrint.ts`, add `import type { Attributes } from '@opentelemetry/api'`
as the first import and `import { traceAction } from './traceAction'` after the
`./printSource` import. Above the `useRunPrint` doc comment, add:

```ts
/** Spec 2026-10-01 §6: what is printed, on which printer, and which plate. */
function printAttributes(source: PrintSource, body: PrintRunRequest): Attributes {
  return {
    ...(source.kind === 'output'
      ? { 'scadbuddy.output_id': source.output.id }
      : { 'scadbuddy.library_file_id': source.file.id }),
    ...(typeof body.printer_id === 'number' ? { 'scadbuddy.printer_id': body.printer_id } : {}),
    'scadbuddy.plate_id': body.plate_id,
    'scadbuddy.all_plates': body.all_plates,
  }
}
```

In `run()`, replace

```ts
      const ran = await sourceApi(source).run(body, controller.signal)
```

with

```ts
      // The run's POST is the action's child; the polls that follow it are not (traceAction).
      const ran = await traceAction('print', printAttributes(source, body), () =>
        sourceApi(source).run(body, controller.signal),
      )
```

(`api.runPrint` → `followPrintRun` → `reattach` → `request` → `send` → `fetch` makes the
`POST` with no `await` before it, so it is issued inside the span.)

- [ ] **Step 6: Send**

In `frontend/src/components/SendDialog.tsx`, add `import { traceAction } from '../lib/traceAction'`
after the `../lib/embed` import, and replace

```tsx
      const sent = await api.sendOutput(output.id, { mode: 'library' })
```

with

```tsx
      const sent = await traceAction('send', { 'scadbuddy.output_id': output.id }, () =>
        api.sendOutput(output.id, { mode: 'library' }),
      )
```

- [ ] **Step 7: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/components/ActionBar.test.tsx src/lib/useRunPrint.test.ts src/components/SendDialog.test.tsx src/pages/CustomizePage.test.tsx`
Expected: PASS, the existing Generate/Send tests in `CustomizePage.test.tsx` included.

- [ ] **Step 8: Lint, typecheck, commit**

Run: `cd frontend && pnpm lint && pnpm typecheck`

```bash
git add frontend/src/lib/saveOutput.ts frontend/src/components/ActionBar.tsx \
  frontend/src/lib/useRunPrint.ts frontend/src/components/SendDialog.tsx \
  frontend/src/components/ActionBar.test.tsx frontend/src/lib/useRunPrint.test.ts \
  frontend/src/components/SendDialog.test.tsx
git commit -m "feat(tracing): Generate, Print and Send are spans in the browser (#988)"
```

---

### Task 6: Each chat turn carries its `traceparent`

**Files:**
- Modify: `frontend/src/agent/chat/protocol.ts` (`ClientMessageSchema`, the `user.message` member)
- Modify: `frontend/src/agent/chat/useAgentChat.ts` (`send`)
- Test: `frontend/src/agent/chat/protocol.test.ts`, `frontend/src/agent/chat/useAgentChat.test.ts`

**Interfaces:**
- Consumes: `messageTraceparent` (Task 1); `installTestTracing` (Task 1).
- Produces: `user.message` frames gain an optional `traceparent` (W3C, `^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`).
  It is sent only when the SDK has loaded. Row 3 (the agent) reads it. Until then, the agent's
  zod `z.object` strips it, and `agent/test/chat.test.ts`'s parity check still passes,
  because it builds no message with the field.

- [ ] **Step 1: Write the failing tests**

In `frontend/src/agent/chat/protocol.test.ts`, before `it('rejects an empty user message', …)`:

```ts
  it('takes a user message with a W3C traceparent, and refuses a malformed one', () => {
    const base = { v: 1, type: 'user.message', text: 'hi', context: { route: '/' } }
    const traceparent = '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01'
    expect(parseClientMessage({ ...base, traceparent })).toEqual({ ok: true, value: { ...base, traceparent } })
    expect(parseClientMessage({ ...base, traceparent: 'not-a-traceparent' }).ok).toBe(false)
  })
```

In `frontend/src/agent/chat/useAgentChat.test.ts`, add
`import { installTestTracing } from '../../test/tracing'` after the `vitest` import, and,
as the last tests inside `describe('useAgentChat', …)`:

```ts
  it('sends each turn with the traceparent of its own assistant.message span once tracing runs', () => {
    const tracing = installTestTracing()
    try {
      const t = scripted()
      const { result } = renderHook(() => useAgentChat(t.factory))
      act(() => t.h().onOpen?.())
      act(() => result.current.send('hello', { route: '/' }))
      act(() => result.current.send('again', { route: '/' }))
      const turns = t.chat().filter((m) => m.type === 'user.message')
      const spans = tracing.exporter.getFinishedSpans()
      expect(spans.map((s) => s.name)).toEqual(['assistant.message', 'assistant.message'])
      expect(turns.map((m) => (m.type === 'user.message' ? m.traceparent : undefined))).toEqual(
        spans.map((s) => `00-${s.spanContext().traceId}-${s.spanContext().spanId}-01`),
      )
      // Each turn is its own trace (§4).
      expect(spans[0]?.spanContext().traceId).not.toBe(spans[1]?.spanContext().traceId)
    } finally {
      tracing.uninstall()
    }
  })

  it('sends a turn with no traceparent before tracing has loaded', () => {
    const t = scripted()
    const { result } = renderHook(() => useAgentChat(t.factory))
    act(() => t.h().onOpen?.())
    act(() => result.current.send('hello', { route: '/' }))
    expect(t.chat()).toContainEqual({ v: 1, type: 'user.message', text: 'hello', context: { route: '/' } })
  })
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd frontend && pnpm test src/agent/chat/protocol.test.ts src/agent/chat/useAgentChat.test.ts`
Expected: FAIL. The protocol test's `toEqual` loses `traceparent` (zod strips the
unknown key), the malformed one parses `ok`, and the hook test sees no span.

- [ ] **Step 3: The protocol field**

In `frontend/src/agent/chat/protocol.ts`, extend the `user.message` member of
`ClientMessageSchema`:

```ts
  z.object({
    v,
    type: z.literal('user.message'),
    /** Absent: start a new `chat` session owned by the browser user. */
    sessionId: sessionId.optional(),
    text: z.string().min(1),
    context: PageContextSchema,
    /**
     * Tracing spec 2026-10-01 §4: a socket carries no headers, so each turn's first
     * frame carries the W3C `traceparent` the agent's `agent.turn` continues. Absent
     * before the page's tracing has loaded; an agent that predates it ignores it.
     */
    traceparent: z
      .string()
      .regex(/^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$/)
      .optional(),
  }),
```

- [ ] **Step 4: Send it**

In `frontend/src/agent/chat/useAgentChat.ts`, add
`import { messageTraceparent } from '../../lib/traceAction'` after the `../tabId` import.
In `send`, replace the `transport.current.send(clientMessage({ type: 'user.message', … }))`
call with:

```ts
    const traceparent = messageTraceparent()
    const result = transport.current.send(
      clientMessage({
        type: 'user.message',
        ...(activeId ? { sessionId: activeId } : {}),
        text: trimmed,
        context,
        ...(traceparent ? { traceparent } : {}),
      }),
    )
```

The span is made and ended at the send, and it carries no attribute: §6 never records
the prompt. A message queued for a reconnect keeps the `traceparent` it was built with.

- [ ] **Step 5: Run the tests to see them pass**

Run: `cd frontend && pnpm test src/agent/chat`
Expected: PASS: every file under `src/agent/chat`, including the two new hook tests
and the new protocol test.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/agent/chat/protocol.ts frontend/src/agent/chat/useAgentChat.ts \
  frontend/src/agent/chat/protocol.test.ts frontend/src/agent/chat/useAgentChat.test.ts
git commit -m "feat(tracing): each assistant turn sends its traceparent in the first frame (#988)"
```

---

### Task 7: The `/telemetry` proxy rule in `vite.config.ts`

**Files:**
- Modify: `frontend/vite.config.ts` (the `proxy` table and its doc comment)

**Interfaces:**
- Produces: in `pnpm dev` and `pnpm preview` (unmocked), `^/telemetry(?:/|$)` → `SCADBUDDY_BACKEND_URL` with `changeOrigin: true`. The mocked build (`VITE_MOCK_API`) still proxies nothing.

- [ ] **Step 1: Show the gap**

```bash
cd frontend
node -e "require('http').createServer((q,r)=>{r.writeHead(204,{'X-Stub-Path':q.url,'X-Stub-Origin':String(q.headers.origin)});r.end()}).listen(38917,'127.0.0.1')" &
STUB=$!
SCADBUDDY_BACKEND_URL=http://127.0.0.1:38917 pnpm exec vite --port 15173 --strictPort --host 127.0.0.1 &
VITE=$!
sleep 5
curl -si -X POST -H 'Origin: http://localhost:15173' http://127.0.0.1:15173/telemetry/v1/traces | grep -iE 'HTTP/|x-stub'
kill $VITE $STUB
```

Expected before the change: `HTTP/1.1 404 Not Found`, and no `x-stub-*` line. Vite
answered the request itself, and the backend never saw the batch.

- [ ] **Step 2: Add the rule**

In `frontend/vite.config.ts`, replace the end of the doc comment and the table:

```ts
 * `/telemetry` is the backend's browser trace relay (tracing spec 2026-10-01 §5.2),
 * outside `/api` like `/healthz`. Without its entry the page's exporter would post to
 * Vite's SPA fallback and lose every batch without an error (§5.3). `changeOrigin`
 * rewrites `Host`, not `Origin`, and the relay's origin check accepts a loopback one.
 *
 * The mocked build (`VITE_MOCK_API=1`) proxies nothing: msw answers every route.
 */
const proxy: Record<string, ProxyOptions> = {
  '^/api/v1/ai(?:[/?]|$)': { target: agent, ws: true },
  '^/mcp(?:[/?]|$)': { target: agent },
  '/api': { target: backend, changeOrigin: true, ws: true },
  '^/telemetry(?:/|$)': { target: backend, changeOrigin: true },
}
```

(The line "The mocked build (`VITE_MOCK_API=1`) proxies nothing…" already exists. The
new paragraph goes directly above it.)

- [ ] **Step 3: Verify it**

Re-run Step 1's commands, and also run

```bash
curl -si http://127.0.0.1:15173/telemetryx | grep -iE 'HTTP/|x-stub|content-type'
```

before the `kill`.

Expected:

```
HTTP/1.1 204 No Content
x-stub-path: /telemetry/v1/traces
x-stub-origin: http://localhost:15173
```

for the relay path. That shows the browser's `Origin` arrives untouched. For
`/telemetryx`, the output is `HTTP/1.1 200 OK` with `Content-Type: text/html` and no
`x-stub-*` line: the anchored pattern does not over-match. Then run
`cd frontend && pnpm typecheck` (`tsconfig.node.json` covers `vite.config.ts`).

- [ ] **Step 4: Commit**

```bash
git add frontend/vite.config.ts
git commit -m "feat(tracing): proxy /telemetry to the backend in vite dev and preview (#988)"
```

---

### Task 8: Mocked e2e, and the full check

**Files:**
- Create: `frontend/e2e/tracing.spec.ts`

**Interfaces:**
- Consumes: everything above. The mocked relay answers `off` (Task 3). `bambuddyFrame` (`e2e/bambuddyFrame.ts`).

- [ ] **Step 1: Write the e2e spec**

```ts
// frontend/e2e/tracing.spec.ts
import { expect, test, type Frame, type Page } from '@playwright/test'
import { bambuddyFrame } from './bambuddyFrame'

/**
 * #988, tracing spec 2026-10-01 §5.3 and §8: once the page's tracing has loaded (lazily,
 * after the first paint), `traceparent` goes on the page's own requests and never on a
 * request to another origin (Bambuddy, Google Fonts). The mocked relay answers "off"
 * (`src/mocks/features/telemetry.ts`), so nothing is exported after the first flush.
 */
test.describe('tracing', () => {
  test.skip(!!process.env.E2E_BASE_URL, 'msw-backed; the real stack is covered by real-backend.spec.ts')
  // The page CSP's `connect-src 'self'` stops a cross-origin fetch before it is sent,
  // so the test lifts it to see what the instrumentation would have put on one.
  test.use({ bypassCSP: true })

  const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/

  /** The `traceparent` that `where`'s own `fetch(url)` was sent with (undefined: none). */
  async function sentWith(page: Page, where: Page | Frame, url: string): Promise<string | undefined> {
    const target = new URL(url, where.url()).href
    const request = page.waitForRequest((r) => r.url() === target)
    await where.evaluate((u) => void fetch(u).catch(() => undefined), url)
    return (await request).headers()['traceparent']
  }

  test('puts traceparent on same-origin requests only', async ({ page }) => {
    await page.goto('/')
    await expect(page.getByRole('heading', { name: 'Models' })).toBeVisible()

    await expect.poll(() => sentWith(page, page, '/api/v1/models'), { timeout: 15_000 }).toMatch(TRACEPARENT)
    expect(await sentWith(page, page, 'https://fonts.googleapis.com/css2?family=Lobster+Two')).toBeUndefined()
    // And again on the page's own origin, so the absence above is the origin's doing.
    expect(await sentWith(page, page, '/api/v1/models')).toMatch(TRACEPARENT)
  })

  test("traces inside Bambuddy's sandboxed frame and exports to ScadBuddy's own origin", async ({ page, baseURL }) => {
    const origin = new URL('/', baseURL).origin
    const relay = page.waitForRequest((r) => r.url() === `${origin}/telemetry/v1/traces`, { timeout: 20_000 })
    const frame = await bambuddyFrame(page, baseURL, '/')
    await expect(frame.getByRole('heading', { name: 'Models' })).toBeVisible()
    const app = page.frames().find((f) => f.url().startsWith(origin))
    if (!app) throw new Error('the ScadBuddy frame is missing')

    await expect.poll(() => sentWith(page, app, '/api/v1/models'), { timeout: 15_000 }).toMatch(TRACEPARENT)
    // The document-load span's batch, flushed on the processor's schedule.
    const posted = await relay
    expect(posted.method()).toBe('POST')
    expect(posted.headers()['content-type']).toBe('application/json')
    expect((await posted.response())?.headers()['x-scadbuddy-tracing']).toBe('off')
  })
})
```

`expect.poll` waits out the lazy load: requests made before `startTracing()` carry no
header. The relay `waitForRequest` is armed before navigation. The `BatchSpanProcessor`
flushes the document-load spans about 5 s after load (its default `scheduledDelayMillis`).

- [ ] **Step 2: Run it**

Run: `cd frontend && pnpm exec playwright test e2e/tracing.spec.ts`
Expected: 2 passed. (This was measured on 2026-10-03 against this exact code: the
first test takes about 1.3 s, the frame test about 6 s.) Then run the customizer spec,
which drives Generate and Send through the now-instrumented `fetch`:
`pnpm exec playwright test e2e/customize.spec.ts e2e/print.spec.ts`. Expected: all
pass. Under heavy load, a `bbox-readout` 5 s timeout is the known flake; re-run on an
idle machine.

- [ ] **Step 3: The full CI set**

```bash
cd frontend
pnpm lint && pnpm typecheck && pnpm test && pnpm build
pnpm exec playwright test
```

Expected: everything passes, apart from the pre-existing `react-refresh` lint warning.
`git status` shows no `src/api/schema.d.ts` or `backend/openapi.json` staged (both are
gitignored).

- [ ] **Step 4: Commit**

```bash
git add frontend/e2e/tracing.spec.ts
git commit -m "test(e2e): traceparent on same-origin requests only, also inside Bambuddy's frame (#988)"
```

---

## After the last task

Open the PR `feat(tracing): browser tracing through the relay (#988)` with `Refs #988`
(the epic stays open for the remaining rows). Its body notes that it needs row 2 (the
relay route) merged first, per spec §9. It also notes that the `traceparent` on
`user.message` is read by the agent from row 3; until then the agent ignores it.
