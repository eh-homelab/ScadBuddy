# Tracing, agent (PR #3 of #988) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The agent sidecar emits OpenTelemetry traces: one trace per chat turn (the
browser's child when the first frame carries a `traceparent`, otherwise the root),
`agent.tool/<name>` spans whose backend requests carry their context, `agent.mcp/<method>`
spans for `/mcp`, and approvals that **end and link**. A parked call ends its tool span
and the open turn segment at once, stores the tool span's context on `ai_approvals`,
and the human's decision is its own trace (`agent.approval`, linked to the parked span)
from which `agent.turn.resume` continues. No prompt, tool input, tool result, header,
query string or exception message is exported.

**Architecture:** `src/telemetry.ts` is the `node --import` entry: it registers
`@opentelemetry/instrumentation/hook.mjs` and then starts the `NodeSDK` built in
`src/telemetry/setup.ts` (resource, sampler, processors, incoming-only HTTP
instrumentation). `src/telemetry/scrub.ts` (`ScrubbingSpanExporter`) sits in front of
the OTLP exporter. `src/telemetry/trace.ts` holds the helpers every traced file uses,
and the tool-context registry the in-process tools find their span in.
`src/telemetry/turn.ts` (`TurnTrace`) is the per-turn segment machine the session
manager drives through the approval gate (`GateTrace`/`ParkTrace` in
`approvals/service.ts`), SDK hooks and the turn's mapped events. The backend client
injects `traceparent` through an `openapi-fetch` middleware; nothing else outgoing is
touched.

**Tech Stack:** Node 24, TypeScript (ESM, `module: NodeNext`), Hono, vitest,
`@opentelemetry/api` 1.9.1, `@opentelemetry/sdk-node` / `instrumentation` /
`instrumentation-http` / `exporter-trace-otlp-proto` 0.222.0,
`@opentelemetry/sdk-trace` / `core` / `resources` / `context-async-hooks` 2.11.0.

**Spec:** `docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` (§3, §4,
§5.4, §6, §8 agent part, §9 row 3). Row 1 (backend core) is on this branch's base
(`backend/scadbuddy/core/tracing.py`, `core/trace_scrub.py`); rows 2, 4, 5 get their
own plans. This row needs none of them for its own tests (§9).

## Decisions where the spec is silent or the code forces a choice

Each is binding for the tasks below; the PR description repeats them.

1. **A second column, `ai_approvals.decision_traceparent`.** §5.4 names one new column
   (`traceparent`, the parked tool span). `agent.turn.resume` must be a child of the
   decision span, and the decision can land on another replica (the parked gate polls
   the row) or after a restart (orphan resume). The only way its context reaches the
   turn is the row, so the same migration adds `decision_traceparent`, written in the
   decision's own transaction.
2. **Where a decided call's execution goes.** §5.4 says both "the rest of the turn
   (the tool's execution …) is `agent.turn.resume`, a child of the decision" and, for
   parallel parks, "its tool execution is a child of that decision". Reconciled: the
   execution of the call whose decision opens the next segment is a child of that new
   `agent.turn.resume` (itself the decision's child); an earlier-decided call of the
   same segment runs as a direct child of its own decision span.
3. **A resume follows any decision that lets the turn go on** (approved, denied,
   expired): in each the harness continues and the model is told. Only an approved,
   consumed call gets an execution span.
4. **Orphan resumes** (approved after the asking turn is gone) start a new turn with a
   new `turn_id`: traced as `agent.turn` (segment 0) whose parent is the decision span.
5. **`agent.approval` parentage.** A human decision (`by` set) is a child of the
   active context (the approve request's server span, the `sessions_approve` tool span,
   or none for the panel socket). A system decision (expired, cancelled) is a root
   span (`root: true`). Both link to the parked span.
6. **Sampler.** §6 states the parentless-`CLIENT` drop rule for the backend. The agent
   adopts it: `backendReachable` probes the backend from every `/healthz`, and each
   probe would otherwise be a trace.
7. **Incoming requests not traced:** `/healthz` (a probe), `GET /mcp` and
   `GET /api/v1/ai/sessions/:id/events` (SSE streams that last hours: §4's reason for
   per-turn traces). The HTTP instrumentation runs ignored requests under
   `suppressTracing`, so nothing below them is traced either.
8. **The agent's scrub also drops `url.query`, `user_agent.original`,
   `user_agent.synthetic.type`, `http.user_agent` and strips `?…` from `url.full`,
   `http.url`, `http.target`.** `@opentelemetry/instrumentation-http` 0.222.0 records
   `url.query` and the user agent on incoming spans (`build/src/utils.js`, lines
   529–537); §6 forbids query strings and headers.
9. **`agent.mcp/<method>` only for `POST /mcp`** (the JSON-RPC method from the body);
   GET is the SSE stream (decision 7) and DELETE carries no method. When no HTTP server
   span is active (vitest, or a future change), the span continues the request's
   `traceparent` header itself.
10. **In-process tools find their span by `_meta["claudecode/toolUseId"]`.** The SDK
    runs an in-process MCP handler in the async context the query was started in, not
    the tool span's. Claude Code 2.1.283 sends the tool_use id in every `tools/call`'s
    `_meta` under `claudecode/toolUseId` (found in the bundled binary:
    `grep -a 'claudecode/toolUseId'` on `@anthropic-ai/claude-agent-sdk-linux-x64`).
    `TurnTrace` binds each tool span's context under its id; the harness projection
    looks it up. Task 8's e2e test proves it end to end.
11. **`sdk-node` installs every OTLP exporter transitively** (§3: services "ship only
    the http/protobuf exporter"). Only `@opentelemetry/exporter-trace-otlp-proto` is
    constructed; `metricReaders: []` and `logRecordProcessors: []` stop `NodeSDK` from
    building the default OTLP metrics and logs exporters, and `spanProcessors` is
    always passed so it never reads `OTEL_TRACES_EXPORTER`.
12. **No endpoint → `NoopSpanProcessor`.** `NodeSDK.start()` registers no tracer
    provider when `spanProcessors` is empty (`build/src/sdk.js`: "Only register if
    there is a span processor"), which would stop propagation. §3 requires spans to be
    created and dropped, so the no-endpoint case passes one `NoopSpanProcessor`.
13. **The agent image gets `SCADBUDDY_VERSION`/`SCADBUDDY_REVISION`.** §3 requires them
    on the resource; the Dockerfile's `agent` stage had neither. The stage gets the same
    `ARG`/`ENV` pair as `runtime`, and `build-image.yml`'s `agent` job passes them. The
    service reads them in `src/telemetry/setup.ts`, not `config.ts` (they are build
    provenance, not configuration; `test/config.test.ts`'s `ENV_VARS` rule is about
    `loadConfig`).
14. **Parallel parks are unit-tested on `TurnTrace`**, not end to end: the fake
    Anthropic endpoint answers one `tool_use` block per reply
    (`test/support/fakeAnthropic.ts` `Reply`).

## Global Constraints

- Only standard `OTEL_*` variables configure tracing; no `SCADBUDDY_` alias (§3).
- No `OTEL_EXPORTER_OTLP_ENDPOINT`: a provider with no exporter; spans are created (so context propagates) and dropped (§3).
- `OTEL_SDK_DISABLED=true`: the API's no-op provider; no spans; `ai_approvals.traceparent` stays null (§3).
- Resource: `service.name` `scadbuddy-agent`; `service.version` the build's version; `service.instance.id` `os.hostname()`; `scadbuddy.revision` from `SCADBUDDY_REVISION`; `OTEL_RESOURCE_ATTRIBUTES` merged in (§3).
- W3C Trace Context only (`tracecontext`), no baggage (§4).
- Trace context never leaves ScadBuddy: outgoing `node:http`/`https` untouched (`ignoreOutgoingRequestHook: () => true`), no undici instrumentation; only the backend client's middleware injects. The remote-plugin forwarder, `http_request`, the headless browser, plugin package fetches and Anthropic get nothing (§4, §5.4).
- Entry: `node --import ./dist/telemetry.js dist/main.js`, in the Dockerfile's agent `CMD`, `agent/package.json` `start`, and the setup comments of `frontend/e2e/agent-link.real.spec.ts` (§5.4).
- `src/telemetry.ts` registers `@opentelemetry/instrumentation/hook.mjs` with `node:module`'s `register()` before the SDK starts (§5.4).
- Exact pins: `@opentelemetry/api` 1.9.1; `@opentelemetry/sdk-node`, `@opentelemetry/instrumentation`, `@opentelemetry/instrumentation-http`, `@opentelemetry/exporter-trace-otlp-proto` 0.222.0; `@opentelemetry/sdk-trace`, `@opentelemetry/core`, `@opentelemetry/resources` 2.11.0; dev: `@opentelemetry/context-async-hooks` 2.11.0 (verified against npm on 2026-10-03; each declares `node ^18.19.0 || >=20.6.0`).
- Manual spans: `agent.turn`, `agent.turn.resume`, `agent.tool/<name>`, `agent.mcp/<method>`, `agent.approval`; every turn segment carries `scadbuddy.turn_id` and `scadbuddy.segment` (§5.4).
- Approvals end and link, never nest the wait: a park ends its tool span and the open segment with `scadbuddy.outcome=parked` and the approval id; the decision is its own trace with a link to the parked tool span and the wait in seconds (§5.4).
- Never recorded: prompts, model output, tool inputs and results, headers, cookies, query strings, exception messages (§6).
- Sampler: parent-based; the default root sampler drops a `CLIENT` span with no parent; `OTEL_TRACES_SAMPLER`, when set, replaces it (§6).
- `scadbuddy.failure_class`: the exception's class name (§6).
- Migrations: a NEW file `agent/src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_<slug>.sql`; never edit a merged one (sha256-checked at start).
- `cd agent && pnpm lint && pnpm typecheck && pnpm test && pnpm build` must pass. Tests never call Anthropic; Postgres tests skip without `SCADBUDDY_TEST_DATABASE_URL`; real-SDK tests skip without the bundled CLI.
- Commit titles are conventional and end `(#988)`.

## Review Focus

1. **Collector unreachable** (endpoint set, nothing listening): the agent starts, serves, and its backend calls work; the export failure never reaches a request or a turn. Test in Task 3 (`an unreachable collector costs the process nothing`).
2. **A garbage `traceparent` in a chat frame** (any client can send one): ignored; the message is still handled and the turn is a root trace. Test in Task 8.
3. **A decision with no trace context** (a row written before the migration, or decided on a replica with tracing off): no link, no error, and the resume segment is a fresh root. Tests in Task 6 (NULL `traceparent`) and Task 7 (NULL `decision_traceparent`).
4. **A turn interrupted or shut down while parked**: the parked spans are already exported, nothing is ended twice, a sibling call still open is ended `unfinished`, and an orphan approved after a restart still lands in a trace (the new turn under the decision). Tests in Task 7 (interrupt) and Task 8 (orphan resume).
5. **`OTEL_SDK_DISABLED=true`** (or an unsampled parent): no `traceparent` injected and none stored on the row. Tests in Task 3 (process) and Task 7 (unsampled parent → `ParkTrace.traceparent` undefined).

---

### Task 1: Dependencies and `ScrubbingSpanExporter`

**Files:**
- Modify: `agent/package.json`, `agent/pnpm-lock.yaml` (by `pnpm add`), `agent/pnpm-workspace.yaml` (only if pnpm reports an ignored build, Step 1)
- Create: `agent/src/telemetry/scrub.ts`
- Test: `agent/test/telemetry.scrub.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces (used by Tasks 2, 3):
  - `function framesOnly(stacktrace: string, message?: string): string`
  - `function scrubAttributes(attributes: Attributes): Attributes`
  - `function scrubSpan(span: ReadableSpan): ReadableSpan`
  - `class ScrubbingSpanExporter implements SpanExporter` with `constructor(inner: SpanExporter)`

- [ ] **Step 1: Add the dependencies, exactly pinned**

```bash
cd agent
corepack enable
pnpm add --save-exact @opentelemetry/api@1.9.1 @opentelemetry/core@2.11.0 \
  @opentelemetry/resources@2.11.0 @opentelemetry/sdk-trace@2.11.0 \
  @opentelemetry/sdk-node@0.222.0 @opentelemetry/instrumentation@0.222.0 \
  @opentelemetry/instrumentation-http@0.222.0 @opentelemetry/exporter-trace-otlp-proto@0.222.0
pnpm add --save-dev --save-exact @opentelemetry/context-async-hooks@2.11.0
pnpm install --frozen-lockfile
```

`sdk-node` brings the gRPC exporters in transitively (decision 11), and with them
`protobufjs`, which has a `postinstall` script. If either `pnpm add` or the frozen
install prints `ERR_PNPM_IGNORED_BUILDS` or "Ignored build scripts: …", add each named
package to `agent/pnpm-workspace.yaml` as declined, with a comment, then rerun
`pnpm install --frozen-lockfile` until it is clean:

```yaml
# msw's install script only copies its browser service worker into a web app's
# public directory (package.json "msw.workerDirectory"). The agent uses msw from
# node, in vitest (msw/node), and has no such directory, so the script is not run.
# protobufjs (via @opentelemetry/sdk-node's gRPC exporters, #988) runs a CLI
# version check on install; the agent never constructs a gRPC exporter.
allowBuilds:
  msw: false
  protobufjs: false
```

Confirm `agent/package.json` lists each package with an exact version (no `^`), then
`node -e "import('@opentelemetry/instrumentation/hook.mjs').then(() => console.log('ok'))"`
from `agent/` prints `ok`.

- [ ] **Step 2: Write the failing tests**

```ts
// agent/test/telemetry.scrub.test.ts
import { type Span, SpanStatusCode } from '@opentelemetry/api'
import { InMemorySpanExporter, SimpleSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace'
import { describe, expect, it } from 'vitest'
import { framesOnly, ScrubbingSpanExporter } from '../src/telemetry/scrub.js'

// The scrub in front of every exporter (spec 2026-10-01 §6): no exception
// message, query string or user agent leaves the process, in any form.

const SENTINEL = 's3ntinel-7d2a'

function exported(work: (span: Span) => void): InMemorySpanExporter {
  const inner = new InMemorySpanExporter()
  const provider = new TracerProvider({ spanProcessors: [new SimpleSpanProcessor(new ScrubbingSpanExporter(inner))] })
  const span = provider.getTracer('t').startSpan('work')
  work(span)
  span.end()
  return inner
}

function everything(spans: InMemorySpanExporter): string {
  return JSON.stringify(
    spans.getFinishedSpans().map((s) => ({
      attributes: s.attributes,
      status: s.status,
      events: s.events.map((e) => e.attributes ?? {}),
    })),
  )
}

describe('ScrubbingSpanExporter', () => {
  it('drops the message of an exception and of its cause, keeping the type and the frames', () => {
    const spans = exported((span) => {
      span.recordException(new TypeError(SENTINEL, { cause: new Error(`${SENTINEL}-cause`) }))
      span.setStatus({ code: SpanStatusCode.ERROR, message: `failed: ${SENTINEL}` })
    })
    expect(everything(spans)).not.toContain(SENTINEL)
    const [span] = spans.getFinishedSpans()
    const event = span!.events[0]!
    expect(event.name).toBe('exception')
    expect(event.attributes?.['exception.type']).toBe('TypeError')
    expect(event.attributes).not.toHaveProperty('exception.message')
    const frames = String(event.attributes?.['exception.stacktrace'])
    expect(frames).toMatch(/^at /)
    expect(frames.split('\n').every((line) => line.startsWith('at '))).toBe(true)
  })

  it('replaces a status description with the exception type, or with "error"', () => {
    const withException = exported((span) => {
      span.recordException(new RangeError(SENTINEL))
      span.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL })
    })
    expect(withException.getFinishedSpans()[0]!.status).toEqual({ code: SpanStatusCode.ERROR, message: 'RangeError' })
    const without = exported((span) => span.setStatus({ code: SpanStatusCode.ERROR, message: SENTINEL }))
    expect(without.getFinishedSpans()[0]!.status).toEqual({ code: SpanStatusCode.ERROR, message: 'error' })
  })

  it('drops a message that imitates frame lines', () => {
    const spans = exported((span) => span.recordException(new Error(`${SENTINEL}\n    at evil (${SENTINEL}.js:1:1)`)))
    expect(everything(spans)).not.toContain(SENTINEL)
  })

  it('drops query strings and user agents from HTTP attributes', () => {
    const spans = exported((span) =>
      span.setAttributes({
        'url.full': `https://scadbuddy.test/mcp?token=${SENTINEL}`,
        'http.target': `/mcp?token=${SENTINEL}`,
        'url.query': `token=${SENTINEL}`,
        'url.path': '/mcp',
        'user_agent.original': SENTINEL,
        'http.request.method': 'POST',
      }),
    )
    expect(everything(spans)).not.toContain(SENTINEL)
    expect(spans.getFinishedSpans()[0]!.attributes).toEqual({
      'url.full': 'https://scadbuddy.test/mcp',
      'http.target': '/mcp',
      'url.path': '/mcp',
      'http.request.method': 'POST',
    })
  })

  it('passes a clean span through unchanged', () => {
    const spans = exported((span) => span.setAttribute('scadbuddy.tool', 'list_models'))
    const [span] = spans.getFinishedSpans()
    expect(span!.name).toBe('work')
    expect(span!.attributes).toEqual({ 'scadbuddy.tool': 'list_models' })
    expect(span!.status.code).toBe(SpanStatusCode.UNSET)
    expect(span!.spanContext().traceId).toMatch(/^[0-9a-f]{32}$/)
  })
})

describe('framesOnly', () => {
  it('keeps the `at` lines of a Node stack and nothing else', () => {
    const stack = `Error: ${SENTINEL}\n    at f (file:///app/x.js:1:2)\n    at async g (file:///app/y.js:3:4)`
    expect(framesOnly(stack, SENTINEL)).toBe('at f (file:///app/x.js:1:2)\nat async g (file:///app/y.js:3:4)')
  })
})
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.scrub.test.ts`
Expected: FAIL with `Failed to load url ../src/telemetry/scrub.js` (or "Cannot find module").

- [ ] **Step 4: Write the exporter**

```ts
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
```

- [ ] **Step 5: Run the tests**

Run: `cd agent && pnpm exec vitest run test/telemetry.scrub.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS, lint and typecheck clean.

- [ ] **Step 6: Commit**

```bash
git add agent/package.json agent/pnpm-lock.yaml agent/pnpm-workspace.yaml agent/src/telemetry/scrub.ts agent/test/telemetry.scrub.test.ts
git commit -m "feat(tracing): pin the agent's OpenTelemetry packages and scrub spans before export (#988)"
```

---

### Task 2: Sampler, trace helpers and the test harness

**Files:**
- Create: `agent/src/telemetry/sampler.ts`
- Create: `agent/src/telemetry/trace.ts`
- Create: `agent/test/support/tracing.ts`
- Test: `agent/test/telemetry.trace.test.ts`

**Interfaces:**
- Consumes: `ScrubbingSpanExporter` (Task 1).
- Produces (used by Tasks 3–8):
  - `sampler.ts`: `class NoParentlessClients implements Sampler`; `const DEFAULT_SAMPLER: Sampler`
  - `trace.ts`:
    - `const TRACER_NAME = 'scadbuddy-agent'`; `function tracer(): Tracer`
    - `function traceparentOf(span: Span | undefined): string | undefined` (valid and sampled only)
    - `function spanContextFrom(traceparent: string | null | undefined): SpanContext | undefined`
    - `function contextFrom(traceparent: string | null | undefined, fallback?: Context): Context` (`ROOT_CONTEXT` fallback by default)
    - `function linkTo(traceparent: string | null | undefined): Link | undefined`
    - `function failureClass(err: unknown): string`; `function recordFailure(span: Span, err: unknown): void`
    - `function withSpan<T>(name: string, options: { attributes?: Attributes; kind?: SpanKind; parent?: Context }, fn: (span: Span) => Promise<T>): Promise<T>`
    - `const TOOL_USE_META = 'claudecode/toolUseId'`; `function bindToolContext(toolUseId: string, ctx: Context): void`; `function unbindToolContext(toolUseId: string): void`; `function toolUseIdFrom(extra: unknown): string | undefined`; `function toolContextFor(extra: unknown): Context`
  - `test/support/tracing.ts`: `function testTracing(): InMemorySpanExporter`; `function waitForSpan(spans: InMemorySpanExporter, predicate: (s: ReadableSpan) => boolean, timeoutMs?: number): Promise<ReadableSpan>`; `function exportedText(spans: InMemorySpanExporter): string`; `const TRACE_ID`, `PARENT_SPAN_ID`, `TRACEPARENT` (`00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01`)

- [ ] **Step 1: Write the failing tests**

```ts
// agent/test/telemetry.trace.test.ts
import { context, INVALID_SPAN_CONTEXT, ROOT_CONTEXT, SpanKind, SpanStatusCode, trace, TraceFlags } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  bindToolContext,
  contextFrom,
  failureClass,
  linkTo,
  recordFailure,
  spanContextFrom,
  toolContextFor,
  tracer,
  traceparentOf,
  unbindToolContext,
  withSpan,
} from '../src/telemetry/trace.js'
import { exportedText, PARENT_SPAN_ID, TRACE_ID, TRACEPARENT, testTracing } from './support/tracing.js'

const spans = testTracing()
beforeEach(() => spans.reset())
const SENTINEL = 's3ntinel-41c0'

describe('traceparents', () => {
  it('formats a valid, sampled span context and nothing else', () => {
    const span = tracer().startSpan('x')
    span.end()
    const { traceId, spanId } = span.spanContext()
    expect(traceparentOf(span)).toBe(`00-${traceId}-${spanId}-01`)
    expect(traceparentOf(trace.wrapSpanContext(INVALID_SPAN_CONTEXT))).toBeUndefined()
    const unsampled = trace.wrapSpanContext({ traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.NONE })
    expect(traceparentOf(unsampled)).toBeUndefined()
    expect(traceparentOf(undefined)).toBeUndefined()
  })

  it('parses a valid traceparent as a remote parent and refuses anything else', () => {
    expect(spanContextFrom(TRACEPARENT)).toEqual({
      traceId: TRACE_ID,
      spanId: PARENT_SPAN_ID,
      traceFlags: TraceFlags.SAMPLED,
      isRemote: true,
    })
    for (const bad of ['', 'garbage', `00-${'0'.repeat(32)}-${PARENT_SPAN_ID}-01`, `${TRACEPARENT}${'x'.repeat(200)}`, null, undefined]) {
      expect(spanContextFrom(bad), String(bad)).toBeUndefined()
    }
    expect(trace.getSpanContext(contextFrom(TRACEPARENT))?.spanId).toBe(PARENT_SPAN_ID)
    expect(contextFrom('garbage')).toBe(ROOT_CONTEXT)
    expect(linkTo(TRACEPARENT)?.context.spanId).toBe(PARENT_SPAN_ID)
    expect(linkTo(null)).toBeUndefined()
  })
})

describe('the default sampler', () => {
  it('drops a CLIENT span with no parent and keeps it under a parent; keeps a parentless INTERNAL span', () => {
    const parentless = tracer().startSpan('probe', { kind: SpanKind.CLIENT })
    expect(parentless.isRecording()).toBe(false)
    parentless.end()
    const root = tracer().startSpan('root')
    expect(root.isRecording()).toBe(true)
    const child = tracer().startSpan('call', { kind: SpanKind.CLIENT }, trace.setSpan(ROOT_CONTEXT, root))
    expect(child.isRecording()).toBe(true)
    child.end()
    root.end()
    expect(spans.getFinishedSpans().map((s) => s.name).sort()).toEqual(['call', 'root'])
  })
})

describe('failures', () => {
  it('records the class, never the message', () => {
    class OddError extends Error {
      override name = 'OddError'
    }
    expect(failureClass(new OddError('x'))).toBe('OddError')
    expect(failureClass('a string')).toBe('string')
    const span = tracer().startSpan('work')
    recordFailure(span, new OddError(SENTINEL))
    recordFailure(span, SENTINEL)
    span.end()
    const [done] = spans.getFinishedSpans()
    expect(done!.status.code).toBe(SpanStatusCode.ERROR)
    expect(done!.attributes['scadbuddy.failure_class']).toBe('string')
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })

  it('withSpan makes the span active, ends it, and records a failure before rethrowing', async () => {
    const seen = await withSpan('inner', {}, async (span) => trace.getActiveSpan() === span)
    expect(seen).toBe(true)
    await expect(withSpan('failing', {}, async () => Promise.reject(new TypeError(SENTINEL)))).rejects.toThrow(SENTINEL)
    const failing = spans.getFinishedSpans().find((s) => s.name === 'failing')!
    expect(failing.status.code).toBe(SpanStatusCode.ERROR)
    expect(failing.attributes['scadbuddy.failure_class']).toBe('TypeError')
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })
})

describe('tool contexts', () => {
  it('finds a bound tool span by the tool_use id Claude Code sends in _meta, else the active context', () => {
    const span = tracer().startSpan('agent.tool/x')
    const bound = trace.setSpan(ROOT_CONTEXT, span)
    bindToolContext('toolu_1', bound)
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_1' } })).toBe(bound)
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_other' } })).toBe(context.active())
    expect(toolContextFor(undefined)).toBe(context.active())
    unbindToolContext('toolu_1')
    expect(toolContextFor({ _meta: { 'claudecode/toolUseId': 'toolu_1' } })).toBe(context.active())
    span.end()
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.trace.test.ts`
Expected: FAIL: `./support/tracing.js` and `../src/telemetry/trace.js` cannot be loaded.

- [ ] **Step 3: Write the sampler**

```ts
// agent/src/telemetry/sampler.ts
import { type Attributes, type Context, type Link, SpanKind } from '@opentelemetry/api'
import { ParentBasedSampler, type Sampler, SamplingDecision, type SamplingResult } from '@opentelemetry/sdk-trace'

// The default sampler (spec 2026-10-01 §6): parent-based, so the backend's and
// the browser's decisions are honoured, and every trace that starts at a
// request or a named span is kept at homelab volume. The root sampler adds the
// backend's one rule: a CLIENT span with no parent is dropped. In the agent
// that is `backendReachable`'s probe from every /healthz, which would
// otherwise be a trace of its own. OTEL_TRACES_SAMPLER, when set, replaces
// this entirely (setup.ts).

export class NoParentlessClients implements Sampler {
  shouldSample(
    _context: Context,
    _traceId: string,
    _spanName: string,
    spanKind: SpanKind,
    _attributes: Attributes,
    _links: Link[],
  ): SamplingResult {
    // Only ever asked about root spans: ParentBasedSampler answers for the rest.
    return { decision: spanKind === SpanKind.CLIENT ? SamplingDecision.NOT_RECORD : SamplingDecision.RECORD_AND_SAMPLED }
  }

  toString(): string {
    return 'NoParentlessClients'
  }
}

export const DEFAULT_SAMPLER: Sampler = new ParentBasedSampler({ root: new NoParentlessClients() })
```

- [ ] **Step 4: Write the helpers**

```ts
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
  if (err instanceof Error) return err.name || err.constructor.name || 'Error'
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
// under this key (measured on the bundled 2.1.283 binary), so telemetry/turn.ts
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

/** The bound context of the call `extra` belongs to, else the active one. */
export function toolContextFor(extra: unknown): Context {
  const id = toolUseIdFrom(extra)
  return (id !== undefined ? toolContexts.get(id) : undefined) ?? context.active()
}
```

- [ ] **Step 5: Write the test harness**

```ts
// agent/test/support/tracing.ts
import { context, propagation, trace } from '@opentelemetry/api'
import { AsyncLocalStorageContextManager } from '@opentelemetry/context-async-hooks'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { resourceFromAttributes } from '@opentelemetry/resources'
import { InMemorySpanExporter, type ReadableSpan, SimpleSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace'
import { DEFAULT_SAMPLER } from '../../src/telemetry/sampler.js'
import { ScrubbingSpanExporter } from '../../src/telemetry/scrub.js'

// One in-memory provider per test process, registered the way NodeSDK
// registers its own (src/telemetry/setup.ts): the default sampler, the
// AsyncLocalStorage context manager, the W3C propagator, and the scrub in front
// of the exporter, so a test sees exactly what would leave the process. The
// API's globals can be set once per process, hence the guard.

export const TRACE_ID = '4bf92f3577b34da6a3ce929d0e0e4736'
export const PARENT_SPAN_ID = '00f067aa0ba902b7'
export const TRACEPARENT = `00-${TRACE_ID}-${PARENT_SPAN_ID}-01`

const KEY = Symbol.for('scadbuddy.test.tracing')

export function testTracing(): InMemorySpanExporter {
  const store = globalThis as unknown as Record<symbol, InMemorySpanExporter | undefined>
  const existing = store[KEY]
  if (existing) return existing
  const spans = new InMemorySpanExporter()
  const provider = new TracerProvider({
    sampler: DEFAULT_SAMPLER,
    resource: resourceFromAttributes({ 'service.name': 'scadbuddy-agent-test' }),
    spanProcessors: [new SimpleSpanProcessor(new ScrubbingSpanExporter(spans))],
  })
  context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable())
  propagation.setGlobalPropagator(new W3CTraceContextPropagator())
  trace.setGlobalTracerProvider(provider)
  store[KEY] = spans
  return spans
}

/** The first exported span `predicate` accepts, polling until `timeoutMs`. */
export async function waitForSpan(
  spans: InMemorySpanExporter,
  predicate: (s: ReadableSpan) => boolean,
  timeoutMs = 10_000,
): Promise<ReadableSpan> {
  const deadline = Date.now() + timeoutMs
  for (;;) {
    const found = spans.getFinishedSpans().find(predicate)
    if (found) return found
    if (Date.now() > deadline) {
      throw new Error(`no such span; exported: ${spans.getFinishedSpans().map((s) => s.name).join(', ') || 'none'}`)
    }
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
}

/** Every name, attribute, status, event and link attribute exported, as one string, for sentinel checks. */
export function exportedText(spans: InMemorySpanExporter): string {
  return JSON.stringify(
    spans.getFinishedSpans().map((s) => ({
      name: s.name,
      attributes: s.attributes,
      status: s.status,
      events: s.events.map((e) => ({ name: e.name, attributes: e.attributes ?? {} })),
      links: s.links.map((l) => l.attributes ?? {}),
    })),
  )
}
```

- [ ] **Step 6: Run the tests**

Run: `cd agent && pnpm exec vitest run test/telemetry.trace.test.ts test/telemetry.scrub.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS, lint and typecheck clean.

- [ ] **Step 7: Commit**

```bash
git add agent/src/telemetry/sampler.ts agent/src/telemetry/trace.ts agent/test/support/tracing.ts agent/test/telemetry.trace.test.ts
git commit -m "feat(tracing): the agent's sampler, trace helpers and test harness (#988)"
```

---

### Task 3: The SDK, the `--import` entry, and every way the agent starts

**Files:**
- Create: `agent/src/telemetry/setup.ts`
- Create: `agent/src/telemetry.ts`
- Modify: `agent/src/main.ts` (shutdown flush)
- Modify: `agent/package.json` (`start`)
- Modify: `Dockerfile` (agent stage: `ARG`/`ENV`, `CMD`)
- Modify: `.github/workflows/build-image.yml` (`agent` job: version label, `build-args`)
- Modify: `frontend/e2e/agent-link.real.spec.ts` (setup comments, lines 15 and 21)
- Test: `agent/test/telemetry.setup.test.ts`, `agent/test/telemetry.process.test.ts`

**Interfaces:**
- Consumes: `ScrubbingSpanExporter` (Task 1); `DEFAULT_SAMPLER` (Task 2).
- Produces:
  - `const SERVICE_NAME = 'scadbuddy-agent'`
  - `function agentResource(env?: Env): Resource`
  - `function tracingDisabled(env?: Env): boolean`
  - `function spanProcessors(env?: Env, exporter?: SpanExporter): SpanProcessor[]`
  - `function untracedIncoming(method: string | undefined, url: string | undefined): boolean`
  - `function httpInstrumentation(): HttpInstrumentation`
  - `function startTelemetry(env?: Env): NodeSDK | undefined`
  - `function shutdownTelemetry(timeoutMs?: number): Promise<void>`
  - (`type Env = Readonly<Record<string, string | undefined>>`)

- [ ] **Step 1: Write the failing unit tests**

```ts
// agent/test/telemetry.setup.test.ts
import { hostname } from 'node:os'
import { BatchSpanProcessor, InMemorySpanExporter, NoopSpanProcessor } from '@opentelemetry/sdk-trace'
import { describe, expect, it } from 'vitest'
import {
  agentResource,
  httpInstrumentation,
  SERVICE_NAME,
  spanProcessors,
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
      expect(config.ignoreIncomingRequestHook?.({ method: 'GET', url: '/healthz' } as never)).toBe(true)
      expect(config.ignoreIncomingRequestHook?.({ method: 'POST', url: '/mcp' } as never)).toBe(false)
      expect(config.headersToSpanAttributes).toBeUndefined()
    } finally {
      instrumentation.disable()
    }
  })
})
```

- [ ] **Step 2: Write the failing process test**

The `--import` hook and the HTTP instrumentation only act in a real Node process, so
this test compiles the entry and the backend client with `tsc` into a folder under
`node_modules/.cache` (so bare imports resolve from `agent/node_modules`), and runs a
child under `node --import`.

```ts
// agent/test/telemetry.process.test.ts
import { execFile, spawn } from 'node:child_process'
import { rm, writeFile } from 'node:fs/promises'
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { promisify } from 'node:util'
import { afterAll, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { TRACE_ID, TRACEPARENT } from './support/tracing.js'

// The entry as it runs in the image (spec 2026-10-01 §5.4):
// `node --import ./dist/telemetry.js dist/main.js`. Incoming requests continue
// the caller's trace; only the backend client injects `traceparent`; fetch
// and node:http requests carry none; with no endpoint nothing is exported,
// and with OTEL_SDK_DISABLED nothing is traced at all.

const AGENT = fileURLToPath(new URL('..', import.meta.url))
const OUT = path.join(AGENT, 'node_modules', '.cache', 'scadbuddy-telemetry-test', String(process.pid))
const TSC = path.join(AGENT, 'node_modules', 'typescript', 'bin', 'tsc')

const CHILD = `
import http from 'node:http'
import { trace } from '@opentelemetry/api'
const { createBackendClient } = await import('./api/backend.js')
const { shutdownTelemetry } = await import('./telemetry/setup.js')

const target = process.argv[2]
const get = (url, headers = {}) =>
  new Promise((resolve, reject) => {
    http
      .get(url, { headers }, (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk })
        res.on('end', () => resolve(body))
      })
      .on('error', reject)
  })

const server = http.createServer((_req, res) => {
  res.end(JSON.stringify({ traceId: trace.getActiveSpan()?.spanContext().traceId ?? null }))
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const incoming = JSON.parse(await get('http://127.0.0.1:' + server.address().port + '/', { traceparent: '${TRACEPARENT}' }))

const client = createBackendClient(target)
const work = await trace.getTracer('child').startActiveSpan('work', async (span) => {
  await client.GET('/healthz')
  await fetch(target + '/plain')
  await get(target + '/node')
  span.end()
  return span.spanContext().traceId
})
server.close()
await shutdownTelemetry()
process.stdout.write(JSON.stringify({ incoming: incoming.traceId, work }) + '\\n')
`

type Hit = { path: string; traceparent: string | undefined; contentType: string | undefined }

let target: Server
let url: string
const hits: Hit[] = []

beforeAll(async () => {
  await rm(OUT, { recursive: true, force: true })
  await promisify(execFile)(
    process.execPath,
    [
      TSC,
      'src/telemetry.ts',
      'src/api/backend.ts',
      '--outDir', OUT,
      '--rootDir', 'src',
      '--module', 'NodeNext',
      '--moduleResolution', 'NodeNext',
      '--target', 'ES2024',
      '--lib', 'ES2024',
      '--types', 'node',
      '--strict',
      '--verbatimModuleSyntax',
      '--skipLibCheck',
    ],
    { cwd: AGENT },
  )
  await writeFile(path.join(OUT, 'child.mjs'), CHILD)
  target = createServer((req, res) => {
    hits.push({
      path: (req.url ?? '').split('?')[0] ?? '',
      traceparent: req.headers.traceparent as string | undefined,
      contentType: req.headers['content-type'],
    })
    req.resume()
    req.on('end', () => {
      if (req.url === '/v1/traces') {
        res.writeHead(200, { 'content-type': 'application/x-protobuf' }).end()
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' }).end('{"status":"ok"}')
    })
  })
  await new Promise<void>((resolve) => target.listen(0, '127.0.0.1', resolve))
  url = `http://127.0.0.1:${(target.address() as AddressInfo).port}`
}, 180_000)

afterAll(async () => {
  await new Promise<void>((resolve) => target.close(() => resolve()))
  await rm(OUT, { recursive: true, force: true })
})

beforeEach(() => {
  hits.length = 0
})

async function runChild(env: Record<string, string>): Promise<{ code: number | null; out: { incoming: string | null; work: string } }> {
  const child = spawn(
    process.execPath,
    ['--import', pathToFileURL(path.join(OUT, 'telemetry.js')).href, path.join(OUT, 'child.mjs'), url],
    { env: { PATH: process.env.PATH ?? '', ...env }, stdio: ['ignore', 'pipe', 'inherit'] },
  )
  let stdout = ''
  child.stdout.setEncoding('utf8')
  child.stdout.on('data', (chunk: string) => {
    stdout += chunk
  })
  const code = await new Promise<number | null>((resolve) => child.on('exit', resolve))
  const last = stdout.trim().split('\n').at(-1) ?? '{}'
  return { code, out: JSON.parse(last) as { incoming: string | null; work: string } }
}

const hit = (p: string) => hits.find((h) => h.path === p)

describe('the agent under `node --import ./dist/telemetry.js`', () => {
  it('continues incoming traces, injects only on backend calls, and exports OTLP/protobuf', async () => {
    const { code, out } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(hit('/healthz')?.traceparent).toMatch(new RegExp(`^00-${out.work}-[0-9a-f]{16}-01$`))
    expect(hit('/plain')).toMatchObject({ traceparent: undefined })
    expect(hit('/node')).toMatchObject({ traceparent: undefined })
    expect(hit('/v1/traces')?.contentType).toBe('application/x-protobuf')
  }, 30_000)

  it('without an endpoint still propagates, and exports nothing', async () => {
    const { code, out } = await runChild({})
    expect(code).toBe(0)
    expect(out.incoming).toBe(TRACE_ID)
    expect(hit('/healthz')?.traceparent).toMatch(new RegExp(`^00-${out.work}-`))
    expect(hit('/v1/traces')).toBeUndefined()
  }, 30_000)

  it('with OTEL_SDK_DISABLED=true traces nothing, injects nothing, exports nothing', async () => {
    const { code, out } = await runChild({ OTEL_SDK_DISABLED: 'true', OTEL_EXPORTER_OTLP_ENDPOINT: url })
    expect(code).toBe(0)
    expect(out.incoming).toBeNull()
    expect(hit('/healthz')).toMatchObject({ traceparent: undefined })
    expect(hit('/v1/traces')).toBeUndefined()
  }, 30_000)

  it('an unreachable collector costs the process nothing', async () => {
    const { code } = await runChild({ OTEL_EXPORTER_OTLP_ENDPOINT: 'http://127.0.0.1:9' })
    expect(code).toBe(0)
    expect(hit('/healthz')?.traceparent).toMatch(/^00-/)
  }, 30_000)
})
```

- [ ] **Step 3: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.setup.test.ts test/telemetry.process.test.ts`
Expected: FAIL: `../src/telemetry/setup.js` cannot be loaded; the process test's `beforeAll` fails with `error TS6053: File 'src/telemetry.ts' not found`.

- [ ] **Step 4: Write the setup module**

```ts
// agent/src/telemetry/setup.ts
import os from 'node:os'
import { W3CTraceContextPropagator } from '@opentelemetry/core'
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto'
import { HttpInstrumentation } from '@opentelemetry/instrumentation-http'
import { envDetector, type Resource, resourceFromAttributes } from '@opentelemetry/resources'
import { NodeSDK } from '@opentelemetry/sdk-node'
import { BatchSpanProcessor, NoopSpanProcessor, type SpanExporter, type SpanProcessor } from '@opentelemetry/sdk-trace'
import { DEFAULT_SAMPLER } from './sampler.js'
import { ScrubbingSpanExporter } from './scrub.js'

// The agent's OpenTelemetry SDK (spec 2026-10-01 §3, §5.4), started by
// src/telemetry.ts before the app loads. Configured by the standard OTEL_*
// variables only:
//   OTEL_EXPORTER_OTLP_ENDPOINT  unset: a provider with no exporter, so spans are
//                                created (context propagates) and dropped (§3)
//   OTEL_SDK_DISABLED=true       no SDK at all: the API's no-op provider
//   OTEL_TRACES_SAMPLER          replaces DEFAULT_SAMPLER (§6)
//   OTEL_RESOURCE_ATTRIBUTES     merged into the resource (envDetector)
// SCADBUDDY_VERSION and SCADBUDDY_REVISION are build provenance stamped into
// the image (Dockerfile `agent` stage), read here for the resource only.
//
// Only incoming HTTP is instrumented. No outgoing node:http/https request is
// touched (`ignoreOutgoingRequestHook`), and the undici instrumentation is not
// installed, so fetch is untouched too: trace context leaves the agent only
// through the backend client's middleware (api/backend.ts, §4). Metrics and
// logs readers are passed empty, or NodeSDK would build OTLP exporters for them.

export const SERVICE_NAME = 'scadbuddy-agent'

type Env = Readonly<Record<string, string | undefined>>

function present(value: string | undefined): boolean {
  return value !== undefined && value.trim() !== ''
}

export function agentResource(env: Env = process.env): Resource {
  return resourceFromAttributes({
    'service.name': SERVICE_NAME,
    'service.version': env.SCADBUDDY_VERSION?.trim() || 'dev',
    'service.instance.id': os.hostname(),
    'scadbuddy.revision': env.SCADBUDDY_REVISION?.trim() || 'unknown',
  })
}

export function tracingDisabled(env: Env = process.env): boolean {
  return env.OTEL_SDK_DISABLED?.trim().toLowerCase() === 'true'
}

/**
 * No endpoint: one NoopSpanProcessor. NodeSDK registers no tracer provider at
 * all for an empty list (sdk-node 0.222.0 `start()`), which would stop
 * propagation; §3 wants spans created and dropped.
 */
export function spanProcessors(env: Env = process.env, exporter?: SpanExporter): SpanProcessor[] {
  if (!present(env.OTEL_EXPORTER_OTLP_ENDPOINT)) return [new NoopSpanProcessor()]
  return [new BatchSpanProcessor(new ScrubbingSpanExporter(exporter ?? new OTLPTraceExporter()))]
}

/**
 * Incoming requests left untraced: the kubelet's probe (a trace per probe
 * otherwise), and the two server-sent-event streams, whose spans would last as
 * long as the stream, the failure §4 rejects session-long traces for. The
 * instrumentation runs these under suppressTracing, so nothing below them is
 * traced either.
 */
export function untracedIncoming(method: string | undefined, url: string | undefined): boolean {
  const path = (url ?? '').split('?')[0] ?? ''
  if (path === '/healthz') return true
  return method === 'GET' && (path === '/mcp' || /^\/api\/v1\/ai\/sessions\/[^/]+\/events$/.test(path))
}

export function httpInstrumentation(): HttpInstrumentation {
  return new HttpInstrumentation({
    ignoreOutgoingRequestHook: () => true,
    ignoreIncomingRequestHook: (request) => untracedIncoming(request.method, request.url),
  })
}

let sdk: NodeSDK | undefined

/** Starts the SDK once; undefined when OTEL_SDK_DISABLED=true. */
export function startTelemetry(env: Env = process.env): NodeSDK | undefined {
  if (sdk || tracingDisabled(env)) return sdk
  sdk = new NodeSDK({
    serviceName: SERVICE_NAME,
    resource: agentResource(env),
    resourceDetectors: [envDetector],
    spanProcessors: spanProcessors(env),
    ...(present(env.OTEL_TRACES_SAMPLER) ? {} : { sampler: DEFAULT_SAMPLER }),
    textMapPropagator: new W3CTraceContextPropagator(),
    instrumentations: [httpInstrumentation()],
    metricReaders: [],
    logRecordProcessors: [],
  })
  sdk.start()
  return sdk
}

/** Flushes and stops the SDK, within `timeoutMs`; never throws (main.ts, on SIGTERM). */
export async function shutdownTelemetry(timeoutMs = 2_000): Promise<void> {
  const running = sdk
  sdk = undefined
  if (!running) return
  await Promise.race([
    running.shutdown().catch(() => {}),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs).unref()),
  ])
}
```

- [ ] **Step 5: Write the entry**

```ts
// agent/src/telemetry.ts
import { register } from 'node:module'

// The agent's OpenTelemetry entry point (spec 2026-10-01 §5.4), loaded before
// the app: `node --import ./dist/telemetry.js dist/main.js` (Dockerfile,
// package.json `start`). The agent is ESM, so the instrumentation needs the
// import-in-the-middle loader hook to patch node:http as the app imports it;
// CommonJS hooks alone would patch nothing. The hook is registered first, and
// the SDK is imported only after it: a static import would be hoisted above
// `register()`.

register('@opentelemetry/instrumentation/hook.mjs', import.meta.url)
const { startTelemetry } = await import('./telemetry/setup.js')
startTelemetry()
```

- [ ] **Step 6: Flush on shutdown**

In `agent/src/main.ts`, add the import after `import { shutdown } from './shutdown.js'`:

```ts
import { shutdownTelemetry } from './telemetry/setup.js'
```

and at the end of `stop()` replace

```ts
  if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
  process.exit(result === 'clean' ? 0 : 1)
```

with

```ts
  if (result === 'timed out') console.error('shutdown: requests still in flight after 10s; exiting')
  // The last spans (this shutdown's turns among them), within 2 s of the
  // pod's grace period. Without --import no SDK started and this is a no-op.
  await shutdownTelemetry()
  process.exit(result === 'clean' ? 0 : 1)
```

- [ ] **Step 7: Every entry point**

`agent/package.json`: `"start": "node --import ./dist/telemetry.js dist/main.js",`

`Dockerfile`, agent stage: after the `ENV CLAUDE_CODE_VERSION=… CLAUDE_CONFIG_DIR=…` block
and before `USER 10001:10001`, add

```dockerfile
# Build provenance for the trace resource (service.version, scadbuddy.revision;
# agent/src/telemetry/setup.ts), passed by build-image.yml like the runtime
# image's.
ARG SCADBUDDY_REVISION=unknown
ARG SCADBUDDY_VERSION=dev
ENV SCADBUDDY_REVISION=${SCADBUDDY_REVISION} \
    SCADBUDDY_VERSION=${SCADBUDDY_VERSION}
```

and replace `CMD ["node", "dist/main.js"]` with

```dockerfile
# --import loads OpenTelemetry before the app (agent/src/telemetry.ts, #988):
# the ESM loader hook must be registered before node:http is imported.
CMD ["node", "--import", "./dist/telemetry.js", "dist/main.js"]
```

`.github/workflows/build-image.yml`, `agent` job: after the `Derive tags and labels`
step, add

```yaml
      # The same label the `build` job stamps into the API image (see "Pick the
      # deploy label" there): `X.Y.Z` for a `vX.Y.Z` tag, `sha-<short>` otherwise.
      # It becomes the agent's trace `service.version` (#988).
      - name: Pick the version label
        id: label
        env:
          REF: ${{ github.ref }}
          SHA: ${{ github.sha }}
        run: |
          set -euo pipefail
          if [[ "$REF" =~ ^refs/tags/v([0-9]+\.[0-9]+\.[0-9]+)$ ]]; then
            version="${BASH_REMATCH[1]}"
          else
            version="sha-${SHA:0:7}"
          fi
          echo "version=$version" | tee -a "$GITHUB_OUTPUT"
```

and in its `Build and push` step's `with:`, after `labels:`, add

```yaml
          build-args: |
            SCADBUDDY_REVISION=${{ github.sha }}
            SCADBUDDY_VERSION=${{ steps.label.outputs.version }}
```

`frontend/e2e/agent-link.real.spec.ts`: in the header comment, replace
`` `node dist/main.js` in agent/ `` with `` `node --import ./dist/telemetry.js dist/main.js` in agent/ ``
(line 15), and `SCADBUDDY_BACKEND_URL=http://127.0.0.1:9 node dist/main.js` with
`SCADBUDDY_BACKEND_URL=http://127.0.0.1:9 node --import ./dist/telemetry.js dist/main.js`
(line 21). Then `grep -rn "node dist/main.js" Dockerfile agent frontend/e2e docs README.md`
from the repo root prints nothing.

- [ ] **Step 8: Run everything this task touches**

Run: `cd agent && pnpm exec vitest run test/telemetry.setup.test.ts test/telemetry.process.test.ts && pnpm lint && pnpm typecheck && pnpm build && test -f dist/telemetry.js`
Expected: PASS; `dist/telemetry.js` exists. The process test fails until Task 4 only on
the `/healthz` `traceparent` assertions (the backend client has no middleware yet):
that is expected here, and Task 4 makes it pass. All other assertions pass now.

Then, from the repo root: `actionlint .github/workflows/build-image.yml` and
`hadolint --config .hadolint.yaml Dockerfile` (as the `lint` job runs them): clean.
If Docker is available: `docker build --target agent -t scadbuddy-agent:dev .` then
`docker run -d --rm --name sb-agent -p 8081:8081 scadbuddy-agent:dev && sleep 5 && curl -fsS http://127.0.0.1:8081/healthz; docker stop sb-agent`
answers `/healthz` (the `agent` CI job checks the same).

- [ ] **Step 9: Commit**

```bash
git add agent/src/telemetry/setup.ts agent/src/telemetry.ts agent/src/main.ts agent/package.json \
  agent/test/telemetry.setup.test.ts agent/test/telemetry.process.test.ts Dockerfile \
  .github/workflows/build-image.yml frontend/e2e/agent-link.real.spec.ts
git commit -m "feat(tracing): start the agent under node --import with the OpenTelemetry SDK (#988)"
```

---

### Task 4: The backend client injects `traceparent`, and nothing else does

**Files:**
- Modify: `agent/src/api/backend.ts`
- Test: `agent/test/telemetry.backend.test.ts`

**Interfaces:**
- Consumes: `tracer`, `recordFailure` (Task 2).
- Produces: `function tracingMiddleware(): Middleware` (exported from `api/backend.ts`); `createBackendClient` now always installs it. Client span name: `` `${method} ${schemaPath}` `` (e.g. `GET /api/v1/models`), kind `CLIENT`, attributes `http.request.method`, `url.template`, `server.address`, `http.response.status_code`.

- [ ] **Step 1: Write the failing tests**

```ts
// agent/test/telemetry.backend.test.ts
import { SpanKind, SpanStatusCode } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import { createBackendClient } from '../src/api/backend.js'
import { tracer } from '../src/telemetry/trace.js'
import { testTracing } from './support/tracing.js'

// The one outgoing call that carries trace context (spec 2026-10-01 §4): a
// tool call's backend request is a child of its span.

const spans = testTracing()
beforeEach(() => spans.reset())

function recording(status = 200) {
  const headers: (string | null)[] = []
  const client = createBackendClient('http://backend.test', async (request) => {
    headers.push((request as Request).headers.get('traceparent'))
    return Response.json([], { status })
  })
  return { client, headers }
}

describe('backend client tracing', () => {
  it('opens a CLIENT span under the active one and injects its context', async () => {
    const { client, headers } = recording()
    await tracer().startActiveSpan('agent.tool/list_models', async (span) => {
      await client.GET('/api/v1/models')
      span.end()
    })
    const call = spans.getFinishedSpans().find((s) => s.name === 'GET /api/v1/models')!
    const parent = spans.getFinishedSpans().find((s) => s.name === 'agent.tool/list_models')!
    expect(call.parentSpanContext?.spanId).toBe(parent.spanContext().spanId)
    expect(call.kind).toBe(SpanKind.CLIENT)
    expect(call.attributes).toMatchObject({
      'http.request.method': 'GET',
      'url.template': '/api/v1/models',
      'server.address': 'backend.test',
      'http.response.status_code': 200,
    })
    expect(headers).toEqual([`00-${parent.spanContext().traceId}-${call.spanContext().spanId}-01`])
  })

  it('marks a 5xx answer as an error with its class', async () => {
    const { client } = recording(503)
    await tracer().startActiveSpan('tool', async (span) => {
      await client.GET('/healthz')
      span.end()
    })
    const call = spans.getFinishedSpans().find((s) => s.name === 'GET /healthz')!
    expect(call.status.code).toBe(SpanStatusCode.ERROR)
    expect(call.attributes['scadbuddy.failure_class']).toBe('http-503')
  })

  it('records a failed fetch by class, never its message, and rethrows', async () => {
    const client = createBackendClient('http://backend.test', async () => {
      throw new TypeError('fetch failed: s3ntinel-77')
    })
    await tracer().startActiveSpan('tool', async (span) => {
      await expect(client.GET('/healthz')).rejects.toThrow('s3ntinel-77')
      span.end()
    })
    const call = spans.getFinishedSpans().find((s) => s.name === 'GET /healthz')!
    expect(call.attributes['scadbuddy.failure_class']).toBe('TypeError')
    expect(JSON.stringify(call.events)).not.toContain('s3ntinel-77')
  })

  it('exports no span for a call made outside any span (the /healthz probe)', async () => {
    const { client } = recording()
    await client.GET('/healthz')
    expect(spans.getFinishedSpans()).toEqual([])
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.backend.test.ts`
Expected: FAIL: no `GET /api/v1/models` span (`call` is undefined).

- [ ] **Step 3: Add the middleware**

Replace `agent/src/api/backend.ts` with:

```ts
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
          'server.address': new URL(request.url).hostname,
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
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && pnpm exec vitest run test/telemetry.backend.test.ts test/backend.test.ts test/telemetry.process.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS, including every process-test assertion that Task 3 left failing.

- [ ] **Step 5: Commit**

```bash
git add agent/src/api/backend.ts agent/test/telemetry.backend.test.ts
git commit -m "feat(tracing): the backend client is the agent's one traced outgoing call (#988)"
```

---

### Task 5: `/mcp` and the tool projections

**Files:**
- Modify: `agent/src/mcp/http.ts`
- Modify: `agent/src/tools/projections.ts`
- Test: `agent/test/telemetry.mcp.test.ts`

**Interfaces:**
- Consumes: `withSpan`, `toolContextFor`, `bindToolContext`, `unbindToolContext`, `tracer` (Task 2); `tracingMiddleware` via `createBackendClient` (Task 4).
- Produces: `async function mcpMethodOf(request: Request): Promise<string>` (exported from `mcp/http.ts`); spans `agent.mcp/<method>` (POST only) and, over `/mcp`, `agent.tool/<registry name>` with `scadbuddy.tool`, `scadbuddy.tier`, `scadbuddy.outcome`. The harness projection runs each call in the context bound for its `_meta["claudecode/toolUseId"]`.

- [ ] **Step 1: Write the failing tests**

```ts
// agent/test/telemetry.mcp.test.ts
import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { Client as McpClient } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { ROOT_CONTEXT, trace } from '@opentelemetry/api'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import { harnessPrincipal } from '../src/auth/principal.js'
import { mcpMethodOf } from '../src/mcp/http.js'
import { bindToolContext, tracer, unbindToolContext } from '../src/telemetry/trace.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { createHarnessServer } from '../src/tools/projections.js'
import { BACKEND, connect, services, testApp } from './helpers/mcp.js'
import { browser } from './support/sessions.js'
import { PARENT_SPAN_ID, TRACE_ID, TRACEPARENT, testTracing } from './support/tracing.js'

// /mcp continues the caller's trace (spec 2026-10-01 §4): `agent.mcp/<method>`
// per POST, `agent.tool/<name>` per call, and the backend request under it.
// In-process (harness) calls find their span by the tool_use id in `_meta`.

const spans = testTracing()
const seen: (string | null)[] = []
const server = setupServer(
  http.get(`${BACKEND}/api/v1/models`, ({ request }) => {
    seen.push(request.headers.get('traceparent'))
    return HttpResponse.json([])
  }),
)
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())
beforeEach(() => {
  spans.reset()
  seen.length = 0
})
const clients: Client[] = []
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.close()))
})

const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)

describe('/mcp spans', () => {
  it('a tools/call continues the caller’s traceparent down to the backend request', async () => {
    const t = testApp()
    const { token } = await t.tokens.mint({ name: 'test', tier: 'read' })
    const client = await connect(t.app, { headers: { authorization: `Bearer ${token}`, traceparent: TRACEPARENT } })
    clients.push(client)
    await client.callTool({ name: 'list_models', arguments: {} })

    expect(named('agent.mcp/initialize')).toHaveLength(1)
    const [call] = named('agent.mcp/tools/call')
    expect(call!.spanContext().traceId).toBe(TRACE_ID)
    expect(call!.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    const [tool] = named('agent.tool/list_models')
    expect(tool!.parentSpanContext?.spanId).toBe(call!.spanContext().spanId)
    expect(tool!.attributes).toMatchObject({ 'scadbuddy.tool': 'list_models', 'scadbuddy.tier': 'read', 'scadbuddy.outcome': 'ok' })
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(tool!.spanContext().spanId)
    expect(seen).toEqual([`00-${TRACE_ID}-${request!.spanContext().spanId}-01`])
  })

  it('names the span after the JSON-RPC method, and only a well-formed one', async () => {
    const post = (body: string) => new Request('http://x/mcp', { method: 'POST', body })
    expect(await mcpMethodOf(post('{"jsonrpc":"2.0","id":1,"method":"tools/list"}'))).toBe('tools/list')
    expect(await mcpMethodOf(post('[{"jsonrpc":"2.0","method":"notifications/initialized"}]'))).toBe('notifications/initialized')
    expect(await mcpMethodOf(post('{"method":"x y <script>"}'))).toBe('unknown')
    expect(await mcpMethodOf(post('not json'))).toBe('unknown')
  })
})

describe('the harness projection', () => {
  it('runs a call in the span bound for its tool_use id', async () => {
    const headers: (string | null)[] = []
    const { createBackendClient } = await import('../src/api/backend.js')
    const backend = createBackendClient(BACKEND, async (request) => {
      headers.push((request as Request).headers.get('traceparent'))
      return Response.json([])
    })
    const toolSpan = tracer().startSpan('agent.tool/mcp__scadbuddy__list_models')
    bindToolContext('toolu_bound', trace.setSpan(ROOT_CONTEXT, toolSpan))
    const principal = harnessPrincipal(browser)
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createHarnessServer(ALL_TOOLS, services({ backend }), principal).instance.connect(serverSide)
    const client = new McpClient({ name: 'projection-test', version: '0' })
    await client.connect(clientSide)
    await client.callTool({ name: 'list_models', arguments: {}, _meta: { 'claudecode/toolUseId': 'toolu_bound' } })
    await client.close()
    unbindToolContext('toolu_bound')
    toolSpan.end()
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(toolSpan.spanContext().spanId)
    expect(headers).toEqual([`00-${toolSpan.spanContext().traceId}-${request!.spanContext().spanId}-01`])
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.mcp.test.ts`
Expected: FAIL: `mcpMethodOf` is not exported; no `agent.mcp/initialize` span.

- [ ] **Step 3: Trace `/mcp`**

In `agent/src/mcp/http.ts` add after the existing imports:

```ts
import { context, propagation, ROOT_CONTEXT, trace } from '@opentelemetry/api'
import { withSpan } from '../telemetry/trace.js'
```

Add after `jsonRpcError`:

```ts
/** The JSON-RPC method of a POST, for its span name; `unknown` for anything else. Reads a clone. */
export async function mcpMethodOf(request: Request): Promise<string> {
  try {
    const body: unknown = await request.clone().json()
    const first: unknown = Array.isArray(body) ? body[0] : body
    const method = (first as { method?: unknown } | undefined)?.method
    return typeof method === 'string' && /^[A-Za-z0-9_./-]{1,64}$/.test(method) ? method : 'unknown'
  } catch {
    return 'unknown'
  }
}

/**
 * `agent.mcp/<method>` around one POST (spec 2026-10-01 §5.4). Under the HTTP
 * instrumentation's server span in production; where there is none (tests, or
 * an untraced listener), the caller's `traceparent` is continued here. GET is
 * the SSE stream (left untraced, telemetry/setup.ts) and DELETE has no method.
 */
async function traced(request: Request, handle: () => Promise<Response>): Promise<Response> {
  if (request.method !== 'POST') return handle()
  const method = await mcpMethodOf(request)
  const active = context.active()
  const parent = trace.getSpan(active)
    ? active
    : propagation.extract(ROOT_CONTEXT, request.headers, {
        keys: (headers) => [...headers.keys()],
        get: (headers, key) => headers.get(key) ?? undefined,
      })
  return withSpan(`agent.mcp/${method}`, { parent, attributes: { 'rpc.method': method } }, async (span) => {
    const response = await handle()
    span.setAttribute('http.response.status_code', response.status)
    return response
  })
}
```

In the `app.all('/mcp', …)` handler, replace

```ts
      return session.transport.handleRequest(request, { authInfo: authInfoFor(principal) })
```

with

```ts
      return traced(request, () => session.transport.handleRequest(request, { authInfo: authInfoFor(principal) }))
```

and

```ts
    const response = await transport.handleRequest(request, { authInfo: authInfoFor(principal) })
```

with

```ts
    const response = await traced(request, () => transport.handleRequest(request, { authInfo: authInfoFor(principal) }))
```

- [ ] **Step 4: Trace the projections**

In `agent/src/tools/projections.ts` add after the existing imports:

```ts
import { context as otelContext, SpanStatusCode } from '@opentelemetry/api'
import { toolContextFor, withSpan } from '../telemetry/trace.js'
```

In `createHarnessServer`, replace the tool handler

```ts
        (args, extra) =>
          // `gate: 'harness'`: the query's permission seam has already parked
          // an outward call for approval (registry.ts ToolContext.gate).
          runTool(t, args, {
            ...services,
            principal,
            session,
            progress: progressFrom(extra),
            signal: signalFrom(extra),
            lookup,
            gate: 'harness',
          }),
```

with

```ts
        (args, extra) =>
          // In the call's own span (telemetry/turn.ts TurnTrace), found by the
          // tool_use id Claude Code sends in `_meta`: the SDK runs this handler
          // in the query's context, not the tool's (telemetry/trace.ts).
          otelContext.with(toolContextFor(extra), () =>
            // `gate: 'harness'`: the query's permission seam has already parked
            // an outward call for approval (registry.ts ToolContext.gate).
            runTool(t, args, {
              ...services,
              principal,
              session,
              progress: progressFrom(extra),
              signal: signalFrom(extra),
              lookup,
              gate: 'harness',
            }),
          ),
```

In `createExternalServer`, replace

```ts
        const startedAt = new Date()
        const run = await runToolWithOutcome(t, args, {
          ...services,
          principal,
          progress: progressFrom(extra),
          signal: extra.signal,
          lookup,
        })
```

with

```ts
        const startedAt = new Date()
        // `agent.tool/<name>` under the request's `agent.mcp/tools/call` (spec
        // 2026-10-01 §5.4); never its input or result (§6).
        const run = await withSpan(
          `agent.tool/${t.name}`,
          { attributes: { 'scadbuddy.tool': t.name, 'scadbuddy.tier': t.risk } },
          async (span) => {
            const done = await runToolWithOutcome(t, args, {
              ...services,
              principal,
              progress: progressFrom(extra),
              signal: extra.signal,
              lookup,
            })
            span.setAttribute('scadbuddy.outcome', done.outcome)
            if (done.outcome === 'error') span.setStatus({ code: SpanStatusCode.ERROR })
            return done
          },
        )
```

- [ ] **Step 5: Run the tests**

Run: `cd agent && pnpm exec vitest run test/telemetry.mcp.test.ts test/mcp.test.ts test/projections.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agent/src/mcp/http.ts agent/src/tools/projections.ts agent/test/telemetry.mcp.test.ts
git commit -m "feat(tracing): agent.mcp and agent.tool spans for /mcp and the harness projection (#988)"
```

---

### Task 6: `ai_approvals` trace columns and the `agent.approval` decision span

**Files:**
- Create: `agent/src/db/migrations/<UTC timestamp>_approval_traceparent.sql` (name in Step 3)
- Modify: `agent/src/approvals/service.ts`
- Test: `agent/test/telemetry.approvals.pg.test.ts`

**Interfaces:**
- Consumes: `tracer`, `traceparentOf`, `linkTo`, `contextFrom`, `recordFailure` (Task 2).
- Produces (used by Tasks 7, 8):
  - `ApprovalRecord` gains `traceparent: string | null` (the parked tool span) and `decisionTraceparent: string | null` (its `agent.approval` span). `approvalView` (routes/approvals.ts) does not expose them.
  - `CreateApproval` gains `traceparent?: string`.
  - `type ParkTrace = { traceparent: string | undefined; parked(approvalId: string): void; decided(approval: Pick<ApprovalRecord, 'id' | 'decision' | 'decisionTraceparent'>, runs: boolean): void }`
  - `type GateTrace = { park(toolUseId: string, toolName: string): ParkTrace }`
  - `GateContext.trace?: GateTrace`
  - Span `agent.approval`: attributes `scadbuddy.approval_id`, `scadbuddy.tool`, `scadbuddy.tier`, `scadbuddy.outcome` (the decision), `scadbuddy.wait_seconds`, `scadbuddy.session_id`/`scadbuddy.turn_id` when set, `scadbuddy.decided_by_kind` when a principal decided; a link to `traceparent` when stored; root when no principal decided.
  - `decide()` runs an orphan's resume inside the decision span's context.

- [ ] **Step 1: Write the failing tests**

```ts
// agent/test/telemetry.approvals.pg.test.ts
import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { GateTrace } from '../src/approvals/service.js'
import type { Database } from '../src/db.js'
import { approvalView } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { tracer } from '../src/telemetry/trace.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { PARENT_SPAN_ID, TRACE_ID, TRACEPARENT, testTracing, waitForSpan } from './support/tracing.js'

// Approvals end and link (spec 2026-10-01 §5.4): the parked span's context is
// stored on the row, and the decision is its own trace linked to it.

const spans = testTracing()

describe.skipIf(!TEST_DATABASE_URL)(`approval tracing${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager

  beforeEach(async () => {
    spans.reset()
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const { runner } = scriptedRunner(() => ({ reply: 'ok' }))
    m = manager({ sql: db.sql, paths: await tempPaths(), run: runner, approvalPollMs: 20 })
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  async function orphan(traceparent?: string) {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
      ...(traceparent ? { traceparent } : {}),
    })
    return { session, approval }
  }

  it('stores the parked span’s context, and never shows it in the approval view', async () => {
    const { approval } = await orphan(TRACEPARENT)
    expect(approval.traceparent).toBe(TRACEPARENT)
    expect(approval.decisionTraceparent).toBeNull()
    expect(Object.keys(approvalView(approval))).not.toContain('traceparent')
    expect(JSON.stringify(approvalView(approval))).not.toContain(TRACE_ID)
  })

  it('a human decision is a child of the request that made it, linked to the parked span', async () => {
    const { approval } = await orphan(TRACEPARENT)
    let requestSpanId = ''
    await tracer().startActiveSpan('POST /api/v1/ai/approvals/:id/deny', async (request) => {
      requestSpanId = request.spanContext().spanId
      await m.approvals.decide(browser, approval.id, false)
      request.end()
    })
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.parentSpanContext?.spanId).toBe(requestSpanId)
    expect(decision.links.map((l) => [l.context.traceId, l.context.spanId])).toEqual([[TRACE_ID, PARENT_SPAN_ID]])
    expect(decision.attributes).toMatchObject({
      'scadbuddy.approval_id': approval.id,
      'scadbuddy.outcome': 'denied',
      'scadbuddy.tool': 'mcp__stub__print',
      'scadbuddy.tier': 'outward',
      'scadbuddy.decided_by_kind': 'browser',
    })
    expect(decision.attributes['scadbuddy.wait_seconds']).toBeGreaterThanOrEqual(0)
    const row = await m.approvals.get(approval.id, browser)
    expect(row.decisionTraceparent).toBe(`00-${decision.spanContext().traceId}-${decision.spanContext().spanId}-01`)
  })

  it('an expiry is a trace of its own, linked to the parked span', async () => {
    const { approval } = await orphan(TRACEPARENT)
    await db.sql`UPDATE ai_approvals SET expires_at = now() - interval '1 second' WHERE id = ${approval.id}`
    expect(await m.approvals.expireDue()).toBe(1)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.parentSpanContext).toBeUndefined()
    expect(decision.attributes['scadbuddy.outcome']).toBe('expired')
    expect(decision.links[0]?.context.spanId).toBe(PARENT_SPAN_ID)
  })

  it('a row with no stored context (written before the migration) is decided without a link or an error', async () => {
    const { approval } = await orphan()
    expect(approval.traceparent).toBeNull()
    await m.approvals.decide(browser, approval.id, false)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.links).toEqual([])
  })

  it('the gate stores the park’s traceparent and hands the decision back to the turn’s trace', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    const turnId = randomUUID()
    // The turn is live on this session, so the decision is a parked call's, not an orphan's.
    await db.sql`UPDATE ai_sessions SET turn_id = ${turnId}, lease_until = now() + interval '1 minute' WHERE id = ${session.id}`
    const calls: string[] = []
    let decidedWith: string | null = null
    const trace: GateTrace = {
      park: (toolUseId, toolName) => {
        calls.push(`park ${toolUseId} ${toolName}`)
        return {
          traceparent: TRACEPARENT,
          parked: (approvalId) => calls.push(`parked ${approvalId}`),
          decided: (approval, runs) => {
            calls.push(`decided ${approval.decision} ${runs}`)
            decidedWith = approval.decisionTraceparent
          },
        }
      },
    }
    const stop = new AbortController()
    const gate = m.approvals.gate({ sessionId: session.id, turnId, requestedBy: agentA, secrets: () => [], signal: stop.signal, trace })
    const verdict = gate({ toolName: 'mcp__stub__print', input: { job: 'box' }, toolUseId: 'toolu_g', tier: 'outward', signal: stop.signal })
    await expect.poll(async () => (await m.approvals.list(browser, { sessionId: session.id, pending: true })).length).toBe(1)
    const [pending] = await m.approvals.list(browser, { sessionId: session.id, pending: true })
    expect(pending!.traceparent).toBe(TRACEPARENT)
    await m.approvals.decide(browser, pending!.id, true)
    expect(await verdict).toMatchObject({ approved: true })
    expect(calls).toEqual([`park toolu_g mcp__stub__print`, `parked ${pending!.id}`, 'decided approved true'])
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decidedWith).toBe(`00-${decision.spanContext().traceId}-${decision.spanContext().spanId}-01`)
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm exec vitest run test/telemetry.approvals.pg.test.ts`
(start Postgres first: `docker run -d -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=scadbuddy_test -p 5432:5432 postgres:17`)
Expected: FAIL: `pnpm typecheck`-level errors are reported by vitest as `traceparent` missing on `ApprovalRecord`, and the first test fails with `expected undefined to be '00-4bf9…'`.

- [ ] **Step 3: The migration**

```bash
cd agent
MIGRATION="src/db/migrations/$(date -u +%Y%m%dT%H%MZ)_approval_traceparent.sql"
cat > "$MIGRATION" <<'SQL'
-- #988 (docs/superpowers/specs/2026-10-01-distributed-tracing-design.md §5.4):
-- approvals end and link. A parked call's tool span ends when it parks; its
-- W3C traceparent is stored here, so the decision, its own trace, can link to
-- it. decision_traceparent is that decision's `agent.approval` span, written
-- with the decision, so the turn that continues (on whichever replica parked
-- it, or a resumed orphan's after a restart) is the decision's child.
-- NULL: tracing was off or unsampled then, or the row predates this file.
ALTER TABLE ai_approvals
  ADD COLUMN traceparent text,
  ADD COLUMN decision_traceparent text;
SQL
echo "$MIGRATION"
```

- [ ] **Step 4: Columns, types and the insert**

In `agent/src/approvals/service.ts`:

Imports, after `import { type AuditOutcome, … } from '../audit/log.js'`:

```ts
import { context as otelContext, type Span, SpanKind } from '@opentelemetry/api'
import { contextFrom, linkTo, recordFailure, traceparentOf, tracer } from '../telemetry/trace.js'
```

`ApprovalRecord`: replace

```ts
  /** Approved but voided before it was used. */
  revokedAt: string | null
}
```

with

```ts
  /** Approved but voided before it was used. */
  revokedAt: string | null
  /** The parked call's tool span (W3C traceparent), for the decision's link (#988); never shown to clients. */
  traceparent: string | null
  /** The decision's `agent.approval` span, the parent of what the turn does next (#988). */
  decisionTraceparent: string | null
}
```

`Row`: replace `  revoked_at: Date | null\n  due: boolean\n}` with

```ts
  revoked_at: Date | null
  traceparent: string | null
  decision_traceparent: string | null
  due: boolean
}
```

`COLUMNS`: replace the constant with

```ts
const COLUMNS = `id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
  requested_by_kind, requested_by_id, requested_by_label, requested_tiers, created_at, expires_at, decision,
  decided_by_kind, decided_by_id, decided_by_label, decided_at, reason, usable_until, resume_turn_id,
  consumed_at, revoked_at, traceparent, decision_traceparent, (decision IS NULL AND expires_at <= now()) AS due`
```

`record()`: replace `    revokedAt: row.revoked_at?.toISOString() ?? null,\n  }` with

```ts
    revokedAt: row.revoked_at?.toISOString() ?? null,
    traceparent: row.traceparent,
    decisionTraceparent: row.decision_traceparent,
  }
```

`GateContext`: add as its last field

```ts
  /** The turn's trace (telemetry/turn.ts TurnTrace): told when a call parks and when it is decided. */
  trace?: GateTrace
```

After `GateContext`, add

```ts
/**
 * What a parked call tells its turn's trace (spec 2026-10-01 §5.4, "Approvals
 * end and link"). `traceparent` is the parked tool span's, stored on the row;
 * `parked` ends that span and the open turn segment once the row exists;
 * `decided` hands over the decision (its `decisionTraceparent` is the parent
 * of what the turn does next), and whether the call now runs.
 */
export type ParkTrace = {
  traceparent: string | undefined
  parked(approvalId: string): void
  decided(approval: Pick<ApprovalRecord, 'id' | 'decision' | 'decisionTraceparent'>, runs: boolean): void
}

export type GateTrace = { park(toolUseId: string, toolName: string): ParkTrace }
```

`CreateApproval`: add after `secrets?: readonly string[]`

```ts
  /** The parked tool span's traceparent (ParkTrace.traceparent). */
  traceparent?: string
```

`insert()`: replace the `INSERT … RETURNING` call with

```ts
    const [row] = await db.unsafe<Row[]>(
      `INSERT INTO ai_approvals (id, session_id, turn_id, tool_use_id, tool, input_summary, input_hash, tier,
                                 requested_by_kind, requested_by_id, requested_by_label, requested_tiers, expires_at,
                                 traceparent)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now() + ($13 * interval '1 second'), $14)
       RETURNING ${COLUMNS}`,
      [
        randomUUID(),
        request.sessionId,
        request.turnId,
        request.toolUseId,
        request.tool,
        summary,
        this.hash(request.tool, request.input),
        request.tier,
        by.kind,
        by.id,
        by.label,
        request.requestedTiers ? [...request.requestedTiers] : null,
        ttl,
        request.traceparent ?? null,
      ],
    )
```

- [ ] **Step 5: The decision span in `settle`**

Add above `export class ApprovalService`:

```ts
/**
 * The decision's own trace (spec 2026-10-01 §5.4): a principal's decision is a
 * child of the request that made it; an expiry or a cancellation, which no one
 * asked for, is a root. Either way it links to the parked call's span.
 */
function decisionSpan(approval: ApprovalRecord, by: Owner | undefined): Span {
  const link = linkTo(approval.traceparent)
  const waited = approval.decidedAt ? (Date.parse(approval.decidedAt) - Date.parse(approval.createdAt)) / 1000 : 0
  return tracer().startSpan('agent.approval', {
    kind: SpanKind.INTERNAL,
    root: by === undefined,
    ...(link ? { links: [link] } : {}),
    attributes: {
      'scadbuddy.approval_id': approval.id,
      'scadbuddy.tool': approval.tool,
      'scadbuddy.tier': approval.tier,
      'scadbuddy.outcome': approval.decision ?? 'unknown',
      'scadbuddy.wait_seconds': Math.max(waited, 0),
      ...(approval.sessionId ? { 'scadbuddy.session_id': approval.sessionId } : {}),
      ...(approval.turnId ? { 'scadbuddy.turn_id': approval.turnId } : {}),
      ...(by ? { 'scadbuddy.decided_by_kind': by.kind } : {}),
    },
  })
}
```

Replace the whole `settle` method with:

```ts
  private async settle(
    id: string,
    decision: Decision,
    by: Owner | undefined,
    reason: string | null,
    where: Pick<DecideOptions, 'clientIp' | 'surface'> = {},
  ): Promise<ApprovalRecord | undefined> {
    const ttl = decision === 'approved' ? await this.expirySeconds() : 0
    // The decision and its `approval.resolved` commit together: a parked gate
    // polling the row must not see the decision (and log the session's
    // `running`) before the event that reports it is in the log.
    let logged: { sessionId: string; events: ServerEvent[]; seqs: number[] } | undefined
    // Started only once the UPDATE has won, so a lost race leaves no span.
    const traced: { span?: Span } = {}
    try {
      const approval = await this.deps.sql.begin(async (tx) => {
        const [row] = await tx.unsafe<Row[]>(
          `UPDATE ai_approvals
           SET decision = $2, decided_by_kind = $3, decided_by_id = $4, decided_by_label = $5,
               decided_at = now(), reason = $6,
               usable_until = CASE WHEN $2 = 'approved' THEN now() + ($7 * interval '1 second') END
           WHERE id = $1 AND decision IS NULL AND ($2 IN ('expired', 'cancelled') OR expires_at > now())
           RETURNING ${COLUMNS}`,
          [id, decision, by?.kind ?? null, by?.id ?? null, by?.label ?? null, reason, ttl],
        )
        if (!row) return undefined
        const settled = record(row)
        // In the same transaction, so whoever sees the decision sees its span too.
        traced.span = decisionSpan(settled, by)
        const traceparent = traceparentOf(traced.span)
        if (traceparent !== undefined) {
          await tx`UPDATE ai_approvals SET decision_traceparent = ${traceparent} WHERE id = ${id}`
          settled.decisionTraceparent = traceparent
        }
        if (settled.sessionId !== null) {
          const resolved = event({
            type: 'approval.resolved',
            sessionId: settled.sessionId,
            id,
            approved: decision === 'approved',
            ...(by && (decision === 'approved' || decision === 'denied') ? { by } : {}),
          })
          const events = [scrubForLog(resolved, [])]
          logged = { sessionId: settled.sessionId, events, seqs: await this.deps.events.append(settled.sessionId, events, tx) }
        }
        return settled
      })
      if (!approval) return undefined
      // Committed: wake followers, and announce it on the bus (#300).
      if (logged) this.deps.events.committed(logged.sessionId, logged.events, logged.seqs)
      this.wakeWaiters(id)
      await this.audited(approval, decision, auditOutcome(decision), by, reason, where)
      return approval
    } catch (err) {
      if (traced.span) recordFailure(traced.span, err)
      throw err
    } finally {
      traced.span?.end()
    }
  }
```

- [ ] **Step 6: An orphan resumes under its decision**

In `decide()`, replace

```ts
        if (approve) await this.resumeOrphan(settled, principal)
```

with

```ts
        // The resumed turn is the decision's child (spec 2026-10-01 §5.4).
        if (approve) {
          await otelContext.with(contextFrom(settled.decisionTraceparent, otelContext.active()), () =>
            this.resumeOrphan(settled, principal),
          )
        }
```

- [ ] **Step 7: The gate tells the turn's trace**

In `gate()`, replace from `const approval = await this.create({` to the end of the
returned function (the final `return { approved: false, message: refusal(decided), ...source }`)
with:

```ts
      // The turn's trace (#988): the call's span context goes on the row, and
      // the span and the turn's open segment end as soon as the row exists.
      const park = context.trace?.park(request.toolUseId, request.toolName)
      const approval = await this.create({
        sessionId: context.sessionId,
        turnId: context.turnId,
        toolUseId: request.toolUseId,
        tool: request.toolName,
        input,
        tier: request.tier,
        requestedBy: context.requestedBy,
        ...(context.requestedTiers ? { requestedTiers: context.requestedTiers } : {}),
        secrets: context.secrets(),
        ...(park?.traceparent ? { traceparent: park.traceparent } : {}),
      })
      park?.parked(approval.id)
      // An abort (interrupt, shutdown) ends the wait and leaves the row
      // pending: the finishing turn cancels it or, on shutdown, keeps it
      // (sessions/manager.ts finish). The SDK has dropped the request by then.
      const decided = await this.waitFor(approval.id, AbortSignal.any([context.signal, request.signal]))
      await this.refreshStatus(context.sessionId)
      const source = { approvalId: decided.id, decision: decided.decision ?? undefined }
      if (decided.decision === 'approved') {
        if (await this.consumeById(decided.id)) {
          park?.decided(decided, true)
          return { approved: true, input, ...source }
        }
        // Voided between the decision and now (interrupt, handoff).
        park?.decided(decided, false)
        const now = await this.row(decided.id)
        return { approved: false, message: refusal(now ?? decided), ...source }
      }
      park?.decided(decided, false)
      return { approved: false, message: refusal(decided), ...source }
    }
  }
```

(`context` here is the method's `GateContext` parameter, which is why the OpenTelemetry
`context` is imported as `otelContext`.)

- [ ] **Step 8: Run the tests**

Run: `cd agent && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm exec vitest run test/telemetry.approvals.pg.test.ts test/approvals.pg.test.ts test/pg.test.ts test/db.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add agent/src/db/migrations/*_approval_traceparent.sql agent/src/approvals/service.ts agent/test/telemetry.approvals.pg.test.ts
git commit -m "feat(tracing): ai_approvals stores the parked span; each decision is its own linked trace (#988)"
```

---

### Task 7: `TurnTrace`, the segment machine

**Files:**
- Create: `agent/src/telemetry/turn.ts`
- Test: `agent/test/telemetry.turn.test.ts`

**Interfaces:**
- Consumes: `tracer`, `traceparentOf`, `spanContextFrom`, `recordFailure`, `bindToolContext`, `unbindToolContext` (Task 2); `GateTrace`, `ParkTrace`, `ApprovalRecord` (Task 6); `TurnOutcome` (type, `sessions/manager.ts`); `RiskTier` (`harness/permissions.ts`); `ServerEvent` (`sessions/protocol.ts`).
- Produces (used by Task 8):
  - `const TURN_SPAN = 'agent.turn'`, `RESUME_SPAN = 'agent.turn.resume'`; `function toolSpanName(tool: string): string`
  - `type TurnTraceOptions = { sessionId: string; turnId: string; parent: Context; tierOf: (toolName: string) => RiskTier }`
  - `class TurnTrace implements GateTrace` with `context(): Context`, `hooks(): Partial<Record<HookEvent, HookCallbackMatcher[]>>`, `toolStarted(toolUseId: string, name: string): void`, `toolEnded(toolUseId: string, ok: boolean): void`, `observe(e: ServerEvent): void`, `park(toolUseId: string, toolName: string): ParkTrace`, `fail(err: unknown): void`, `finish(outcome: TurnOutcome, result?: SDKResultMessage): void`

- [ ] **Step 1: Write the failing tests**

```ts
// agent/test/telemetry.turn.test.ts
import type { SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { ROOT_CONTEXT, SpanStatusCode, trace, TraceFlags } from '@opentelemetry/api'
import { beforeEach, describe, expect, it } from 'vitest'
import { TurnTrace } from '../src/telemetry/turn.js'
import { toolContextFor, tracer, traceparentOf } from '../src/telemetry/trace.js'
import { exportedText, PARENT_SPAN_ID, TRACE_ID, testTracing } from './support/tracing.js'

// The turn's segments (spec 2026-10-01 §5.4): a park ends the open segment and
// the parked tool span at once; each decision is its own trace; the next
// segment is `agent.turn.resume`, the last decision's child.

const spans = testTracing()
beforeEach(() => spans.reset())
const SENTINEL = 's3ntinel-9e5b'

const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)
const one = (name: string) => {
  const all = named(name)
  expect(all, name).toHaveLength(1)
  return all[0]!
}
const segment = (n: number) =>
  [...named('agent.turn'), ...named('agent.turn.resume')].find((s) => s.attributes['scadbuddy.segment'] === n)!
const success = { kind: 'result', subtype: 'success', costUsd: 0.02, turns: 2 } as const

function turn(parent = ROOT_CONTEXT): TurnTrace {
  return new TurnTrace({ sessionId: 's-1', turnId: 't-1', parent, tierOf: () => 'outward' })
}

/** A decision span as ApprovalService.settle makes one, and the traceparent the row stores. */
function decision() {
  const span = tracer().startSpan('agent.approval', { root: true })
  span.end()
  return { traceparent: traceparentOf(span)!, spanId: span.spanContext().spanId, traceId: span.spanContext().traceId }
}
const approved = (id: string, decisionTraceparent: string | null) => ({ id, decision: 'approved' as const, decisionTraceparent })
const execution = (toolUseId: string) => trace.getSpan(toolContextFor({ _meta: { 'claudecode/toolUseId': toolUseId } }))!.spanContext()

describe('TurnTrace', () => {
  it('a parked call ends its tool span and the turn at once, outcome parked', () => {
    const t = turn()
    t.toolStarted('toolu_1', 'mcp__scadbuddy__print')
    const park = t.park('toolu_1', 'mcp__scadbuddy__print')
    park.parked('appr-1')
    const tool = one('agent.tool/mcp__scadbuddy__print')
    const seg0 = one('agent.turn')
    expect(tool.attributes).toMatchObject({
      'scadbuddy.outcome': 'parked',
      'scadbuddy.approval_id': 'appr-1',
      'scadbuddy.tier': 'outward',
      'scadbuddy.tool': 'mcp__scadbuddy__print',
    })
    expect(seg0.attributes).toMatchObject({
      'scadbuddy.outcome': 'parked',
      'scadbuddy.approval_id': 'appr-1',
      'scadbuddy.segment': 0,
      'scadbuddy.turn_id': 't-1',
      'scadbuddy.session_id': 's-1',
      'scadbuddy.tool_calls': 1,
    })
    expect(tool.parentSpanContext?.spanId).toBe(seg0.spanContext().spanId)
    expect(park.traceparent).toBe(`00-${tool.spanContext().traceId}-${tool.spanContext().spanId}-01`)
  })

  it('is the browser’s child when started under its context', () => {
    const parent = trace.setSpanContext(ROOT_CONTEXT, { traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.SAMPLED, isRemote: true })
    turn(parent).finish(success)
    const seg0 = one('agent.turn')
    expect(seg0.spanContext().traceId).toBe(TRACE_ID)
    expect(seg0.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'success', 'scadbuddy.cost_usd': 0.02, 'scadbuddy.turns': 2 })
  })

  it('the decision starts agent.turn.resume, and the approved call runs inside it', () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    const d = decision()
    park.decided(approved('a1', d.traceparent), true)
    const exec = execution('toolu_1')
    t.toolEnded('toolu_1', true)
    t.finish(success, { total_cost_usd: 0.02, num_turns: 2, usage: { input_tokens: 10, output_tokens: 3 } } as unknown as SDKResultMessage)
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(d.spanId)
    expect(resume.spanContext().traceId).toBe(d.traceId)
    expect(resume.links).toEqual([])
    expect(resume.attributes).toMatchObject({
      'scadbuddy.segment': 1,
      'scadbuddy.outcome': 'success',
      'scadbuddy.turn_id': 't-1',
      'scadbuddy.input_tokens': 10,
      'scadbuddy.output_tokens': 3,
    })
    const ran = named('agent.tool/x').find((s) => s.spanContext().spanId === exec.spanId)!
    expect(ran.parentSpanContext?.spanId).toBe(resume.spanContext().spanId)
    expect(ran.attributes).toMatchObject({ 'scadbuddy.outcome': 'ok', 'scadbuddy.approval_id': 'a1' })
  })

  it('a turn that parks twice yields segments 0, 1 and 2, each ending as the next begins', () => {
    const t = turn()
    const p1 = t.park('toolu_1', 'x')
    p1.parked('a1')
    const d1 = decision()
    p1.decided(approved('a1', d1.traceparent), true)
    t.toolEnded('toolu_1', true)
    expect(named('agent.turn.resume')).toHaveLength(0)
    const p2 = t.park('toolu_2', 'x')
    p2.parked('a2')
    expect(segment(1).attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': 'a2' })
    const d2 = decision()
    p2.decided(approved('a2', d2.traceparent), true)
    t.toolEnded('toolu_2', true)
    t.finish(success)
    expect([0, 1, 2].map((n) => segment(n).attributes['scadbuddy.turn_id'])).toEqual(['t-1', 't-1', 't-1'])
    expect(segment(1).parentSpanContext?.spanId).toBe(d1.spanId)
    expect(segment(2).parentSpanContext?.spanId).toBe(d2.spanId)
    expect(segment(2).name).toBe('agent.turn.resume')
    expect(segment(2).attributes['scadbuddy.outcome']).toBe('success')
  })

  it('two calls parked at once end the segment once; the resume is the last decision’s child, linked to the other', () => {
    const t = turn()
    t.toolStarted('toolu_r', 'mcp__scadbuddy__list_models')
    const pa = t.park('toolu_a', 'x')
    const pb = t.park('toolu_b', 'x')
    pa.parked('a')
    pb.parked('b')
    expect(named('agent.turn')).toHaveLength(1)
    expect(named('agent.tool/x')).toHaveLength(2)
    expect(named('agent.tool/mcp__scadbuddy__list_models')).toHaveLength(0)
    const da = decision()
    pa.decided(approved('a', da.traceparent), true)
    expect(named('agent.turn.resume')).toHaveLength(0)
    const execA = execution('toolu_a')
    t.toolEnded('toolu_a', true)
    const db = decision()
    pb.decided(approved('b', db.traceparent), true)
    t.toolEnded('toolu_b', true)
    t.toolEnded('toolu_r', true)
    t.finish(success)
    const ranA = named('agent.tool/x').find((s) => s.spanContext().spanId === execA.spanId)!
    expect(ranA.parentSpanContext?.spanId).toBe(da.spanId)
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(db.spanId)
    expect(resume.links.map((l) => l.context.spanId)).toEqual([da.spanId])
    expect(one('agent.tool/mcp__scadbuddy__list_models').parentSpanContext?.spanId).toBe(one('agent.turn').spanContext().spanId)
  })

  it('an expired or denied call opens the resume but runs nothing', () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    const d = decision()
    park.decided({ id: 'a1', decision: 'expired', decisionTraceparent: d.traceparent }, false)
    t.finish(success)
    expect(named('agent.tool/x')).toHaveLength(1)
    expect(one('agent.turn.resume').parentSpanContext?.spanId).toBe(d.spanId)
  })

  it('a decision with no trace context starts the resume as a root', () => {
    const t = turn()
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    park.decided(approved('a1', null), true)
    t.finish(success)
    const resume = one('agent.turn.resume')
    expect(resume.parentSpanContext).toBeUndefined()
    expect(resume.links).toEqual([])
  })

  it('an interrupt while parked ends nothing twice, and ends an open sibling as unfinished', () => {
    const t = turn()
    t.toolStarted('toolu_s', 'y')
    const park = t.park('toolu_1', 'x')
    park.parked('a1')
    t.finish({ kind: 'interrupted' })
    expect(one('agent.turn').attributes['scadbuddy.outcome']).toBe('parked')
    expect(one('agent.tool/x').attributes['scadbuddy.outcome']).toBe('parked')
    expect(one('agent.tool/y').attributes['scadbuddy.outcome']).toBe('unfinished')
  })

  it('under an unsampled parent nothing is stored on the row', () => {
    const parent = trace.setSpanContext(ROOT_CONTEXT, { traceId: TRACE_ID, spanId: PARENT_SPAN_ID, traceFlags: TraceFlags.NONE, isRemote: true })
    const park = turn(parent).park('toolu_1', 'x')
    expect(park.traceparent).toBeUndefined()
    park.parked('a1')
    expect(spans.getFinishedSpans()).toEqual([])
  })

  it('a failed turn records the class and nothing of the message', () => {
    const t = turn()
    t.fail(new TypeError(`could not start: ${SENTINEL}`))
    t.finish({ kind: 'failed', message: SENTINEL })
    const seg0 = one('agent.turn')
    expect(seg0.status.code).toBe(SpanStatusCode.ERROR)
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'failed', 'scadbuddy.failure_class': 'TypeError' })
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })

  it('its hooks and events start and end tool spans, and never record an input or a result', async () => {
    const t = turn()
    const hooks = t.hooks()
    const signal = new AbortController().signal
    const base = { session_id: 's', transcript_path: '', cwd: '' }
    await hooks.PreToolUse![0]!.hooks[0]!(
      { ...base, hook_event_name: 'PreToolUse', tool_name: 'x', tool_input: { job: SENTINEL }, tool_use_id: 'toolu_h' },
      'toolu_h',
      { signal },
    )
    await hooks.PostToolUse![0]!.hooks[0]!(
      { ...base, hook_event_name: 'PostToolUse', tool_name: 'x', tool_input: { job: SENTINEL }, tool_response: SENTINEL, tool_use_id: 'toolu_h' },
      'toolu_h',
      { signal },
    )
    t.observe({ type: 'tool.call', sessionId: 's-1', id: 'toolu_e', name: 'z', input: { job: SENTINEL }, risk: 'read' })
    t.observe({ type: 'tool.result', sessionId: 's-1', id: 'toolu_e', ok: false, summary: SENTINEL })
    t.finish(success)
    expect(one('agent.tool/x').attributes['scadbuddy.outcome']).toBe('ok')
    expect(one('agent.tool/z').attributes['scadbuddy.outcome']).toBe('error')
    expect(one('agent.tool/z').status.code).toBe(SpanStatusCode.ERROR)
    expect(exportedText(spans)).not.toContain(SENTINEL)
  })
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/telemetry.turn.test.ts`
Expected: FAIL: `../src/telemetry/turn.js` cannot be loaded.

- [ ] **Step 3: Write `TurnTrace`**

```ts
// agent/src/telemetry/turn.ts
import type { HookCallbackMatcher, HookEvent, SDKResultMessage } from '@anthropic-ai/claude-agent-sdk'
import { type Attributes, type Context, type Link, ROOT_CONTEXT, type Span, type SpanContext, SpanStatusCode, trace } from '@opentelemetry/api'
import type { ApprovalRecord, GateTrace, ParkTrace } from '../approvals/service.js'
import type { RiskTier } from '../harness/permissions.js'
import type { TurnOutcome } from '../sessions/manager.js'
import type { ServerEvent } from '../sessions/protocol.js'
import { bindToolContext, recordFailure, spanContextFrom, tracer, traceparentOf, unbindToolContext } from './trace.js'

// One chat turn's trace (spec 2026-10-01 §5.4). The turn is a sequence of
// SEGMENTS: `agent.turn` (segment 0), then an `agent.turn.resume` (1, 2, …)
// after each round of approvals. A span is exported only when it ends, so an
// outward call that parks ends its own `agent.tool/<name>` span and the open
// segment AT ONCE (outcome `parked`): a parked turn shows up in Tempo as soon
// as it parks, never after a 24-hour wait. The parked tool span's context goes
// on the ai_approvals row (ParkTrace.traceparent); the human's decision is its
// own trace (`agent.approval`, approvals/service.ts) linking back to it.
//
//   - Several calls of one segment may park (parallel tool use). The segment
//     ends once, at the first park; a later park ends only its own tool span,
//     and a call that did not park keeps its span open until it finishes.
//   - The next segment begins when the segment's LAST parked call is decided
//     (the harness continues only then): a child of that decision, linked to
//     the segment's other decisions. Names never nest (`.resume.resume`);
//     every segment carries `scadbuddy.turn_id` and `scadbuddy.segment`.
//   - An approved call's execution is a new `agent.tool/<name>` span: under
//     the new segment when its decision opened one, else under its own
//     decision. Denied and expired calls run nothing.
//
// Tool spans start at whichever comes first of the PreToolUse hook, the
// mapped `tool.call` event and a park, and end at PostToolUse(Failure) or the
// `tool.result` event, whichever comes first. Each open tool span's context is
// bound under its tool_use id (telemetry/trace.ts), where the in-process tools
// find it. Nothing a tool is given or returns is recorded (§6).

export const TURN_SPAN = 'agent.turn'
export const RESUME_SPAN = 'agent.turn.resume'

export function toolSpanName(tool: string): string {
  return `agent.tool/${tool}`
}

export type TurnTraceOptions = {
  sessionId: string
  turnId: string
  /** The context the turn starts in: a browser frame's traceparent, an MCP call, a decision (an orphan's resume), or none. */
  parent: Context
  /** Each tool's tier, for its span. */
  tierOf: (toolName: string) => RiskTier
}

type Segment = { span: Span; index: number; ended: boolean; undecided: number; decisions: SpanContext[]; tools: number }
type Call = { name: string; span: Span; ended: boolean; parkedIn?: Segment }

function outcomeOf(outcome: TurnOutcome): string {
  return outcome.kind === 'result' ? outcome.subtype : outcome.kind
}

function usageOf(result: SDKResultMessage): Attributes {
  const out: Attributes = { 'scadbuddy.cost_usd': result.total_cost_usd, 'scadbuddy.turns': result.num_turns }
  const usage = result.usage as Partial<Record<'input_tokens' | 'output_tokens', number>> | undefined
  if (typeof usage?.input_tokens === 'number') out['scadbuddy.input_tokens'] = usage.input_tokens
  if (typeof usage?.output_tokens === 'number') out['scadbuddy.output_tokens'] = usage.output_tokens
  return out
}

export class TurnTrace implements GateTrace {
  readonly #sessionId: string
  readonly #turnId: string
  readonly #tierOf: (toolName: string) => RiskTier
  readonly #calls = new Map<string, Call>()
  #segment: Segment

  constructor(options: TurnTraceOptions) {
    this.#sessionId = options.sessionId
    this.#turnId = options.turnId
    this.#tierOf = options.tierOf
    this.#segment = this.#open(TURN_SPAN, 0, options.parent, [])
  }

  #ids(): Attributes {
    return { 'scadbuddy.session_id': this.#sessionId, 'scadbuddy.turn_id': this.#turnId }
  }

  #open(name: string, index: number, parent: Context, links: Link[]): Segment {
    const span = tracer().startSpan(name, { attributes: { ...this.#ids(), 'scadbuddy.segment': index }, links }, parent)
    return { span, index, ended: false, undecided: 0, decisions: [], tools: 0 }
  }

  #endSegment(segment: Segment, attributes: Attributes): void {
    if (segment.ended) return
    segment.span.setAttributes({ ...attributes, 'scadbuddy.tool_calls': segment.tools })
    segment.span.end()
    segment.ended = true
  }

  /** The open segment's context: what the turn's own work runs under. */
  context(): Context {
    return trace.setSpan(ROOT_CONTEXT, this.#segment.span)
  }

  #startTool(toolUseId: string, name: string, parent: Context, attributes: Attributes = {}): Span {
    const span = tracer().startSpan(
      toolSpanName(name),
      {
        attributes: {
          ...this.#ids(),
          'scadbuddy.tool': name,
          'scadbuddy.tier': this.#tierOf(name),
          'scadbuddy.tool_use_id': toolUseId,
          ...attributes,
        },
      },
      parent,
    )
    bindToolContext(toolUseId, trace.setSpan(ROOT_CONTEXT, span))
    return span
  }

  #call(toolUseId: string, name: string): Call {
    const known = this.#calls.get(toolUseId)
    if (known) return known
    const segment = this.#segment
    segment.tools += 1
    const call: Call = { name, ended: false, span: this.#startTool(toolUseId, name, trace.setSpan(ROOT_CONTEXT, segment.span)) }
    this.#calls.set(toolUseId, call)
    return call
  }

  #endCall(toolUseId: string, call: Call, outcome: string, attributes: Attributes = {}): void {
    call.span.setAttributes({ 'scadbuddy.outcome': outcome, ...attributes })
    if (outcome === 'error') call.span.setStatus({ code: SpanStatusCode.ERROR })
    call.span.end()
    call.ended = true
    unbindToolContext(toolUseId)
  }

  toolStarted(toolUseId: string, name: string): void {
    this.#call(toolUseId, name)
  }

  toolEnded(toolUseId: string, ok: boolean): void {
    const call = this.#calls.get(toolUseId)
    if (call && !call.ended) this.#endCall(toolUseId, call, ok ? 'ok' : 'error')
  }

  /** The turn's mapped panel events (sessions/sdkEvents.ts): a backstop for the hooks. */
  observe(e: ServerEvent): void {
    if (e.type === 'tool.call') this.toolStarted(e.id, e.name)
    else if (e.type === 'tool.result') this.toolEnded(e.id, e.ok)
  }

  /** SDK callback hooks (harness/run.ts HarnessRun.traceHooks): every tool's start and end. */
  hooks(): Partial<Record<HookEvent, HookCallbackMatcher[]>> {
    return {
      PreToolUse: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PreToolUse') this.toolStarted(input.tool_use_id, input.tool_name)
              return Promise.resolve({})
            },
          ],
        },
      ],
      PostToolUse: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PostToolUse') this.toolEnded(input.tool_use_id, true)
              return Promise.resolve({})
            },
          ],
        },
      ],
      PostToolUseFailure: [
        {
          hooks: [
            (input) => {
              if (input.hook_event_name === 'PostToolUseFailure') this.toolEnded(input.tool_use_id, false)
              return Promise.resolve({})
            },
          ],
        },
      ],
    }
  }

  park(toolUseId: string, toolName: string): ParkTrace {
    const call = this.#call(toolUseId, toolName)
    return {
      traceparent: traceparentOf(call.span),
      parked: (approvalId) => {
        if (!call.ended) this.#endCall(toolUseId, call, 'parked', { 'scadbuddy.approval_id': approvalId })
        const segment = this.#segment
        segment.undecided += 1
        call.parkedIn = segment
        this.#endSegment(segment, { 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
      },
      decided: (approval, runs) => this.#decided(toolUseId, call, approval, runs),
    }
  }

  #decided(
    toolUseId: string,
    call: Call,
    approval: Pick<ApprovalRecord, 'id' | 'decision' | 'decisionTraceparent'>,
    runs: boolean,
  ): void {
    const segment = call.parkedIn
    if (!segment) return
    call.parkedIn = undefined
    segment.undecided -= 1
    const decision = spanContextFrom(approval.decisionTraceparent)
    if (decision) segment.decisions.push(decision)
    const decisionContext = decision ? trace.setSpanContext(ROOT_CONTEXT, decision) : ROOT_CONTEXT
    let parent = decisionContext
    if (segment.undecided === 0 && segment === this.#segment) {
      const links = segment.decisions.filter((d) => d !== decision).map((context) => ({ context }))
      this.#segment = this.#open(RESUME_SPAN, segment.index + 1, decisionContext, links)
      parent = this.context()
    }
    if (!runs) return
    if (parent !== decisionContext) this.#segment.tools += 1
    call.span = this.#startTool(toolUseId, call.name, parent, { 'scadbuddy.approval_id': approval.id })
    call.ended = false
  }

  /** The turn failed with `err`: its class on the open segment, never its message. */
  fail(err: unknown): void {
    if (!this.#segment.ended) recordFailure(this.#segment.span, err)
  }

  /** Ends whatever is still open: tool spans as `unfinished`, the open segment with the turn's outcome and cost. */
  finish(outcome: TurnOutcome, result?: SDKResultMessage): void {
    for (const [toolUseId, call] of this.#calls) if (!call.ended) this.#endCall(toolUseId, call, 'unfinished')
    this.#endSegment(this.#segment, { 'scadbuddy.outcome': outcomeOf(outcome), ...(result ? usageOf(result) : {}) })
  }
}
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && pnpm exec vitest run test/telemetry.turn.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/src/telemetry/turn.ts agent/test/telemetry.turn.test.ts
git commit -m "feat(tracing): TurnTrace, a turn's segments that end at every park (#988)"
```

---

### Task 8: Wiring: the session manager, the harness, the chat socket, end to end

**Files:**
- Modify: `agent/src/harness/run.ts` (`HarnessRun.traceHooks`)
- Modify: `agent/src/sessions/manager.ts` (`runTurn`)
- Modify: `agent/src/sessions/clientProtocol.ts` (`user.message.traceparent`)
- Modify: `agent/src/routes/chat.ts` (`ChatConnection.handle`)
- Test: `agent/test/telemetry.pg.test.ts`, `agent/test/telemetry.e2e.test.ts`, plus one case in `agent/test/run.test.ts`

**Interfaces:**
- Consumes: `TurnTrace` (Task 7); `GateContext.trace` (Task 6); `contextFrom` (Task 2); the harness projection's tool-context lookup (Task 5); `tracingMiddleware` (Task 4).
- Produces: `HarnessRun.traceHooks?: Partial<Record<HookEvent, HookCallbackMatcher[]>>`; the client message `user.message` accepts an optional `traceparent` string (≤ 256 characters; anything malformed is ignored, never refused).

- [ ] **Step 1: Write the failing tests**

Add to `agent/test/run.test.ts`, inside the `describe` that defines `base`, after
`'loads plugins by absolute local path, and wires both permission seams'`:

```ts
  it('adds the trace hooks after the permission seam (#988)', () => {
    const traceHooks = { PreToolUse: [{ hooks: [() => Promise.resolve({})] }], PostToolUse: [{ hooks: [() => Promise.resolve({})] }] }
    const options = buildHarnessOptions({ ...base, traceHooks })
    expect(options.hooks?.PreToolUse).toHaveLength(2)
    expect(options.hooks?.PreToolUse?.[1]).toBe(traceHooks.PreToolUse[0])
    expect(options.hooks?.PostToolUse).toEqual(traceHooks.PostToolUse)
  })
```

```ts
// agent/test/telemetry.pg.test.ts
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { ChatConnection } from '../src/routes/chat.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { PROTOCOL_VERSION } from '../src/sessions/protocol.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'
import { PARENT_SPAN_ID, TRACE_ID, TRACEPARENT, testTracing, waitForSpan } from './support/tracing.js'

// The turn's trace against a real SessionManager, without the SDK: the chat
// socket's first frame carries the browser's traceparent (spec 2026-10-01 §4),
// and an orphan approved after a restart resumes under its decision.

const spans = testTracing()

describe.skipIf(!TEST_DATABASE_URL)(`turn tracing${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let m: SessionManager
  let runs: unknown[]

  beforeEach(async () => {
    spans.reset()
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    const scripted = scriptedRunner(() => ({ reply: 'ok' }))
    runs = scripted.runs
    m = manager({ sql: db.sql, paths: await tempPaths(), run: scripted.runner, pollMs: 20 })
  })
  afterEach(async () => {
    m.abortAll()
    await drop()
  })

  const frame = (extra: Record<string, unknown>) =>
    JSON.stringify({ v: PROTOCOL_VERSION, type: 'user.message', text: 'hi', context: { route: '/' }, ...extra })

  it('a chat turn is the child of the traceparent in its first frame', async () => {
    const connection = new ChatConnection(m, () => {})
    await connection.open()
    await connection.receive(frame({ traceparent: TRACEPARENT }))
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    connection.close()
    expect(turn.spanContext().traceId).toBe(TRACE_ID)
    expect(turn.parentSpanContext?.spanId).toBe(PARENT_SPAN_ID)
    expect(turn.attributes).toMatchObject({ 'scadbuddy.segment': 0, 'scadbuddy.outcome': 'success' })
  })

  it('a garbage traceparent is ignored: the message is handled and the turn is a root', async () => {
    const out: { type: string }[] = []
    const connection = new ChatConnection(m, (e) => out.push(e))
    await connection.open()
    await connection.receive(frame({ traceparent: 'not-a-traceparent' }))
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    connection.close()
    expect(turn.parentSpanContext).toBeUndefined()
    expect(runs).toHaveLength(1)
    expect(out.filter((e) => e.type === 'error')).toEqual([])
  })

  it('an orphan approved after a restart resumes as a new turn under its decision', async () => {
    const { session } = await m.start(agentA, { origin: 'mcp', title: 't' })
    await db.sql`UPDATE ai_sessions SET status = 'waiting_approval' WHERE id = ${session.id}`
    const approval = await m.approvals.create({
      sessionId: session.id,
      turnId: null,
      toolUseId: 'toolu_1',
      tool: 'mcp__stub__print',
      input: { job: 'box.3mf' },
      tier: 'outward',
      requestedBy: agentA,
      traceparent: TRACEPARENT,
    })
    await m.approvals.decide(browser, approval.id, true)
    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    const turn = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    expect(turn.parentSpanContext?.spanId).toBe(decision.spanContext().spanId)
    expect(decision.links[0]?.context.spanId).toBe(PARENT_SPAN_ID)
  })
})
```

```ts
// agent/test/telemetry.e2e.test.ts
import { createServer, type Server } from 'node:http'
import type { AddressInfo } from 'node:net'
import { Hono } from 'hono'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { z } from 'zod'
import { createBackendClient } from '../src/api/backend.js'
import { connectDatabase, type Database } from '../src/db.js'
import { bundledCliPath } from '../src/harness/cliVersion.js'
import { ensureStateDirs } from '../src/harness/stateDirs.js'
import { originPolicy } from '../src/http/origins.js'
import { registerApprovalRoutes } from '../src/routes/approvals.js'
import type { SessionManager } from '../src/sessions/manager.js'
import { harnessTools } from '../src/tools/harness.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { defineTool, text } from '../src/tools/registry.js'
import { type FakeAnthropic, type RecordedRequest, type Reply, startFakeAnthropic } from './support/fakeAnthropic.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, collectUntil, manager, tempPaths } from './support/sessions.js'
import { exportedText, testTracing, waitForSpan } from './support/tracing.js'

// The agent's trace on the real Agent SDK and its bundled Claude Code binary,
// pointed at the fake Anthropic endpoint, with sessions and approvals in
// Postgres (spec 2026-10-01 §8, agent): a turn yields agent.turn → agent.tool,
// and the tool's backend request carries the tool's context; a parked call
// ends its spans before any decision; the decision links to it through
// ai_approvals.traceparent, and agent.turn.resume is its child.

let cliMissing: string | undefined
try {
  bundledCliPath()
} catch (err) {
  cliMissing = (err as Error).message
}
const skip = cliMissing ?? (TEST_DATABASE_URL ? undefined : `${TEST_DATABASE_URL_ENV} is not set`)

const spans = testTracing()
const TOKEN = 'gw-tracing-e2e-token-8888999900001111'
const SENTINEL = 's3ntinel-e2e-5c71'
const UI = { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'https' }

/** An outward stand-in for a ScadBuddy tool that calls the backend. */
const PING = defineTool({
  name: 'ping_backend',
  description: 'Ping the backend (test only).',
  input: z.object({ job: z.string() }),
  risk: 'outward',
  routes: ['GET /healthz'],
  handler: async ({ job }, { backend }) => {
    await backend.GET('/healthz')
    return text(`pinged ${job.slice(0, 1)}`)
  },
})
const LIST_MODELS = ALL_TOOLS.find((t) => t.name === 'list_models')!

type Hit = { path: string; traceparent: string | undefined }

describe.skipIf(skip !== undefined)(`agent tracing against the real SDK${skip ? ` (skipped: ${skip})` : ''}`, () => {
  let fake: FakeAnthropic
  let script: (request: RecordedRequest) => Reply
  let db: Database
  let schema: string
  let drop: () => Promise<void>
  let stop: AbortController
  let backend: Server
  let backendUrl: string
  const hits: Hit[] = []
  const pools: Database[] = []

  beforeEach(async () => {
    spans.reset()
    hits.length = 0
    fake = await startFakeAnthropic((r) => script(r))
    ;({ db, schema, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    stop = new AbortController()
    backend = createServer((req, res) => {
      hits.push({ path: (req.url ?? '').split('?')[0] ?? '', traceparent: req.headers.traceparent as string | undefined })
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(req.url?.startsWith('/api/v1/models') ? '[]' : '{"status":"ok"}')
    })
    await new Promise<void>((resolve) => backend.listen(0, '127.0.0.1', resolve))
    backendUrl = `http://127.0.0.1:${(backend.address() as AddressInfo).port}`
  })
  afterEach(async () => {
    stop.abort()
    await fake.close()
    await new Promise<void>((resolve) => backend.close(() => resolve()))
    for (const pool of pools.splice(0)) await pool.close()
    await drop()
  })

  async function agent(): Promise<SessionManager> {
    const paths = await tempPaths()
    await ensureStateDirs(paths)
    const pool = connectDatabase(TEST_DATABASE_URL!, { searchPath: schema })
    pools.push(pool)
    const services = {
      backend: createBackendClient(backendUrl),
      pending: new PendingActionStore(),
      pollIntervalMs: 5,
      renderWaitMs: 1000,
    }
    return manager({
      sql: pool.sql,
      paths,
      credential: () => Promise.resolve({ kind: 'gateway', baseUrl: fake.url, secret: TOKEN }),
      settings: { get: <T>(key: string) => Promise.resolve((key === 'model' ? 'claude-sonnet-4-5' : undefined) as T) },
      approvalPollMs: 50,
      ...harnessTools(services, [LIST_MODELS, PING]),
    })
  }

  function routes(m: SessionManager): Hono {
    const app = new Hono()
    registerApprovalRoutes(app, {
      approvals: m.approvals,
      ready: () => Promise.resolve(true),
      remoteAddress: () => '10.0.0.7',
      origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    })
    return app
  }

  const lastContent = (r: RecordedRequest) => JSON.stringify(r.body?.messages?.at(-1)?.content ?? '')
  const named = (name: string) => spans.getFinishedSpans().filter((s) => s.name === name)

  async function approve(app: Hono, id: string): Promise<void> {
    const res = await app.request(`/api/v1/ai/approvals/${id}/approve`, { method: 'POST', headers: UI })
    expect(res.status).toBe(200)
  }

  /** The approval ids the session asked for, once it has asked for `count` (attach replays from the start). */
  async function required(m: SessionManager, sessionId: string, count: number): Promise<string[]> {
    let n = 0
    const all = await collectUntil(
      await m.attach(sessionId, browser, { signal: stop.signal }),
      (e) => e.event.type === 'approval.required' && ++n === count,
      30_000,
    )
    return all.flatMap((e) => (e.event.type === 'approval.required' ? [e.event.id] : []))
  }

  it('a turn is agent.turn → agent.tool, and its backend request carries the tool’s context; Anthropic gets none', async () => {
    script = (r) => (lastContent(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: 'mcp__scadbuddy__list_models', input: {} } })
    const m = await agent()
    const { turn } = await m.start(browser, { origin: 'chat', prompt: `list the models ${SENTINEL}` })
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const seg0 = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    const [tool] = named('agent.tool/mcp__scadbuddy__list_models')
    expect(tool!.parentSpanContext?.spanId).toBe(seg0.spanContext().spanId)
    expect(tool!.attributes).toMatchObject({ 'scadbuddy.outcome': 'ok', 'scadbuddy.tier': 'read' })
    const [request] = named('GET /api/v1/models')
    expect(request!.parentSpanContext?.spanId).toBe(tool!.spanContext().spanId)
    expect(hits.find((h) => h.path === '/api/v1/models')?.traceparent).toBe(
      `00-${seg0.spanContext().traceId}-${request!.spanContext().spanId}-01`,
    )
    expect(fake.requests.every((r) => r.headers.traceparent === undefined)).toBe(true)
    expect(exportedText(spans)).not.toContain(SENTINEL)
  }, 60_000)

  it('a parked call ends its spans before any decision; the decision links to it; the resume is its child', async () => {
    script = (r) =>
      lastContent(r).includes('tool_result') ? { text: 'done' } : { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: SENTINEL } } }
    const m = await agent()
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ping it' })
    const [approvalId] = await required(m, session.id, 1)

    // Parked: both spans are exported already, and the row holds the tool span's context.
    const parkedTool = await waitForSpan(spans, (s) => s.name === 'agent.tool/mcp__scadbuddy__ping_backend')
    const seg0 = await waitForSpan(spans, (s) => s.name === 'agent.turn')
    expect(parkedTool.attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
    expect(seg0.attributes).toMatchObject({ 'scadbuddy.outcome': 'parked', 'scadbuddy.approval_id': approvalId })
    const row = await m.approvals.get(approvalId!, browser)
    expect(row.traceparent).toBe(`00-${parkedTool.spanContext().traceId}-${parkedTool.spanContext().spanId}-01`)
    expect(hits.filter((h) => h.path === '/healthz')).toEqual([])

    await approve(routes(m), approvalId!)
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const decision = await waitForSpan(spans, (s) => s.name === 'agent.approval')
    expect(decision.links.map((l) => l.context.spanId)).toEqual([parkedTool.spanContext().spanId])
    expect(decision.attributes['scadbuddy.outcome']).toBe('approved')
    const resume = await waitForSpan(spans, (s) => s.name === 'agent.turn.resume')
    expect(resume.parentSpanContext?.spanId).toBe(decision.spanContext().spanId)
    expect(resume.attributes).toMatchObject({ 'scadbuddy.segment': 1, 'scadbuddy.turn_id': seg0.attributes['scadbuddy.turn_id'] })
    const ran = named('agent.tool/mcp__scadbuddy__ping_backend').find((s) => s.attributes['scadbuddy.outcome'] === 'ok')!
    expect(ran.parentSpanContext?.spanId).toBe(resume.spanContext().spanId)
    const [ping] = named('GET /healthz')
    expect(ping!.parentSpanContext?.spanId).toBe(ran.spanContext().spanId)
    expect(hits.find((h) => h.path === '/healthz')?.traceparent).toBe(
      `00-${decision.spanContext().traceId}-${ping!.spanContext().spanId}-01`,
    )
    expect(exportedText(spans)).not.toContain(SENTINEL)
  }, 90_000)

  it('a turn that parks twice yields segments 0, 1 and 2, each under its own decision', async () => {
    script = (r) => {
      const last = lastContent(r)
      if (last.includes('pinged b')) return { text: 'done' }
      if (last.includes('pinged a')) return { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: 'b' } } }
      return { toolUse: { name: 'mcp__scadbuddy__ping_backend', input: { job: 'a' } } }
    }
    const m = await agent()
    const app = routes(m)
    const { session, turn } = await m.start(browser, { origin: 'chat', prompt: 'ping twice' })
    const [first] = await required(m, session.id, 1)
    await approve(app, first!)
    const [, second] = await required(m, session.id, 2)
    await approve(app, second!)
    expect(await turn!.done).toMatchObject({ kind: 'result', subtype: 'success' })

    const segments = [...named('agent.turn'), ...named('agent.turn.resume')].sort(
      (a, b) => Number(a.attributes['scadbuddy.segment']) - Number(b.attributes['scadbuddy.segment']),
    )
    expect(segments.map((s) => [s.name, s.attributes['scadbuddy.segment'], s.attributes['scadbuddy.outcome']])).toEqual([
      ['agent.turn', 0, 'parked'],
      ['agent.turn.resume', 1, 'parked'],
      ['agent.turn.resume', 2, 'success'],
    ])
    const decisions = named('agent.approval')
    const byApproval = (id: string) => decisions.find((d) => d.attributes['scadbuddy.approval_id'] === id)!
    expect(segments[1]!.parentSpanContext?.spanId).toBe(byApproval(first!).spanContext().spanId)
    expect(segments[2]!.parentSpanContext?.spanId).toBe(byApproval(second!).spanContext().spanId)
    expect(new Set(segments.map((s) => s.attributes['scadbuddy.turn_id'])).size).toBe(1)
  }, 120_000)
})
```

- [ ] **Step 2: Run them to see them fail**

Run: `cd agent && pnpm exec vitest run test/run.test.ts -t 'trace hooks' && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm exec vitest run test/telemetry.pg.test.ts test/telemetry.e2e.test.ts`
Expected: FAIL: `traceHooks` is not a `HarnessRun` field; `no such span; exported: none` for `agent.turn`.

- [ ] **Step 3: `HarnessRun.traceHooks`**

In `agent/src/harness/run.ts`, add to `HarnessRun` after `memoryHooks`:

```ts
  /**
   * SDK callback hooks for the turn's trace (telemetry/turn.ts
   * `TurnTrace.hooks`): every tool's start and end, after the permission
   * seam's `PreToolUse` and the memory hooks. In-process callbacks, like the
   * memory hooks, so not the command hooks plugins.ts refuses.
   */
  traceHooks?: Partial<Record<HookEvent, HookCallbackMatcher[]>>
```

and in `buildHarness`, replace

```ts
    hooks: mergeHooks(
      { PreToolUse: [makePreToolUseHook(tierOf, run.onDecision, gate, guard)] },
      run.memoryHooks,
    ),
```

with

```ts
    hooks: mergeHooks(
      mergeHooks({ PreToolUse: [makePreToolUseHook(tierOf, run.onDecision, gate, guard)] }, run.memoryHooks),
      run.traceHooks,
    ),
```

- [ ] **Step 4: The turn's trace in `runTurn`**

In `agent/src/sessions/manager.ts`, add imports:

```ts
import { context as otelContext } from '@opentelemetry/api'
import { TurnTrace } from '../telemetry/turn.js'
```

In `runTurn`, after `const mapper = new SdkEventMapper(id, (name, input) => eventTierOf(name, input))`, add:

```ts
    // The turn's trace (spec 2026-10-01 §5.4, telemetry/turn.ts): a child of
    // whatever started it (the browser's traceparent from the chat frame, an
    // MCP call, or the decision an orphan resumes under), else a root.
    const traced = new TurnTrace({
      sessionId: id,
      turnId,
      parent: otelContext.active(),
      // Each tool's tier for its span (TierResolver takes an optional input).
      tierOf: (name) => eventTierOf(name) ?? 'outward',
    })
```

In the `this.approvals.gate({ … })` call, add `trace: traced,` after `signal: controller.signal,`.

In the `run: HarnessRun = { … }` literal, add after `...(memory ? { memoryHooks: memory.hooks } : {}),`:

```ts
        traceHooks: traced.hooks(),
```

Replace the loop head `for await (const message of this.run(run)) {` with

```ts
      for await (const message of otelContext.with(traced.context(), () => this.run(run))) {
```

and inside the loop, replace

```ts
        if (auditor) for (const e of events) await auditor.observe(e)
```

with

```ts
        for (const e of events) traced.observe(e)
        if (auditor) for (const e of events) await auditor.observe(e)
```

In the `catch (err)` of that `try`, replace

```ts
      if (!result && !controller.signal.aborted) failure = redact(describe(err), secrets)
```

with

```ts
      if (!result && !controller.signal.aborted) {
        failure = redact(describe(err), secrets)
        traced.fail(err)
      }
```

Replace the method's tail

```ts
    if (lost) return { kind: 'lost_claim' }
    const stopped = controller.signal.aborted ? abortMessage(controller.signal) : undefined
    return this.finish(session, turnId, stopped, result, failure, secrets)
  }
```

with

```ts
    if (lost) {
      traced.finish({ kind: 'lost_claim' }, result)
      return { kind: 'lost_claim' }
    }
    const stopped = controller.signal.aborted ? abortMessage(controller.signal) : undefined
    let outcome: TurnOutcome = { kind: 'failed', message: 'the turn could not finish' }
    try {
      outcome = await this.finish(session, turnId, stopped, result, failure, secrets)
      return outcome
    } finally {
      traced.finish(outcome, result)
    }
  }
```

- [ ] **Step 5: The chat frame's `traceparent`**

In `agent/src/sessions/clientProtocol.ts`, in the `user.message` object, add after `context: PageContextSchema,`:

```ts
    /**
     * The browser's W3C traceparent for this turn (spec 2026-10-01 §4: a
     * WebSocket cannot carry headers, so it rides in the turn's first frame).
     * Optional; a malformed one is ignored (routes/chat.ts), never refused.
     */
    traceparent: z.string().max(256).optional(),
```

In `agent/src/routes/chat.ts`, add imports:

```ts
import { context as otelContext } from '@opentelemetry/api'
import { contextFrom } from '../telemetry/trace.js'
```

and in `handle()` replace the whole `case 'user.message': { … }` block with

```ts
        case 'user.message': {
          const context = renderPageContext(message.context)
          // The turn is the browser's child when the frame names its span,
          // else a root (spec 2026-10-01 §4); a malformed one is ignored.
          const parent = contextFrom(message.traceparent)
          if (!message.sessionId) {
            const { session } = await otelContext.with(parent, () =>
              this.sessions.start(this.principal, { origin: 'chat', prompt: message.text, context }),
            )
            this.pairTab(session.id)
            // From the start: session.started is what the panel adopts its new chat by.
            this.follow(session.id, 0)
            return
          }
          const id = message.sessionId
          // Ownership first, every time (get() refuses another owner's session):
          // pairing this tab must never outrun the check that send() repeats.
          await this.sessions.get(id, this.principal)
          if (!this.follows.has(id)) {
            this.follow(id, await this.sessions.events.lastSeq(id))
          }
          // Before the turn starts, so its first browser_* call already finds this tab.
          this.pairTab(id)
          await otelContext.with(parent, () => this.sessions.send(id, this.principal, message.text, { context }))
          return
        }
```

- [ ] **Step 6: Run the tests**

Run: `cd agent && pnpm exec vitest run test/run.test.ts test/chat.test.ts test/sessions.manager.test.ts && SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm exec vitest run test/telemetry.pg.test.ts test/telemetry.e2e.test.ts test/chat.pg.test.ts test/approvals.e2e.test.ts test/sessions.e2e.test.ts && pnpm lint && pnpm typecheck`
Expected: PASS. If `telemetry.e2e.test.ts`'s backend request has no `traceparent`
whose parent is the tool span, Claude Code did not send `_meta["claudecode/toolUseId"]`
to the in-process server: stop and report it (decision 10), do not weaken the assertion.

- [ ] **Step 7: Commit**

```bash
git add agent/src/harness/run.ts agent/src/sessions/manager.ts agent/src/sessions/clientProtocol.ts agent/src/routes/chat.ts \
  agent/test/run.test.ts agent/test/telemetry.pg.test.ts agent/test/telemetry.e2e.test.ts
git commit -m "feat(tracing): chat turns are traced, park and resume across approvals (#988)"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md` (section "Tracing (#988)" under "Deploying")
- Modify: `docs/ai/operating.md` (§2 "Environment variables", §7 "Database tables")
- Modify: `CLAUDE.md` ("Layout", the `agent/` bullet)

- [ ] **Step 1: README**

In `README.md`, replace the first sentence of "### Tracing (#988)"

```markdown
The API and the render worker export OpenTelemetry traces over OTLP/HTTP when
```

with

```markdown
The API, the render worker and the agent sidecar export OpenTelemetry traces over OTLP/HTTP when
```

and append to the end of that section:

```markdown
The agent starts as `node --import ./dist/telemetry.js dist/main.js` (the image's
`CMD` and `pnpm start`): the import registers the ESM loader hook and the SDK before
the app loads. A chat turn is one trace; an approval ends the turn's spans when the
call parks, and the decision is a trace of its own linked to it
(`ai_approvals.traceparent`).
```

- [ ] **Step 2: operating.md**

In `docs/ai/operating.md` §2, after the paragraph that begins "Variables set in the image,
not read by `config.ts`", add:

```markdown
Tracing (#988) is configured by the standard OpenTelemetry variables only, read by
[`agent/src/telemetry/setup.ts`](../../agent/src/telemetry/setup.ts) when the process
starts under `node --import ./dist/telemetry.js` (the image's `CMD`):
`OTEL_EXPORTER_OTLP_ENDPOINT` (unset: spans are created, so context propagates, and
dropped), `OTEL_SDK_DISABLED=true` (the kill switch: no spans at all),
`OTEL_TRACES_SAMPLER` (replaces the default, which drops parentless client spans) and
`OTEL_RESOURCE_ATTRIBUTES`. `SCADBUDDY_VERSION` and `SCADBUDDY_REVISION` are stamped
into the image by `build-image.yml` and become `service.version` and
`scadbuddy.revision`. None of these reach the Claude Code subprocess, whose `env` is
explicit (above). Only the backend client sends `traceparent`; plugins, `http_request`,
the headless browser and Anthropic never get one. Design:
`docs/superpowers/specs/2026-10-01-distributed-tracing-design.md` §5.4.
```

In §7, extend the `ai_approvals` bullet with:

```markdown
  `traceparent` is the parked call's tool span and `decision_traceparent` the decision's
  `agent.approval` span (#988); both are internal and never in an approval view.
```

- [ ] **Step 3: CLAUDE.md**

In `CLAUDE.md`, in the `agent/` bullet of "Layout", after the sentence that ends
"`src/api/backend.ts` is the `openapi-fetch` client over the generated
`src/api/schema.d.ts`.", add:

```markdown
  Tracing (#988): `src/telemetry.ts` is the `node --import` entry (Dockerfile `CMD`,
  `pnpm start`) that registers the OTel ESM hook, then `src/telemetry/setup.ts` starts
  the SDK (standard `OTEL_*` variables only; incoming HTTP only).
  `src/telemetry/scrub.ts` strips exception messages, query strings and user agents
  before export; `src/telemetry/turn.ts` (`TurnTrace`) ends a turn's spans at every
  park and opens `agent.turn.resume` under the decision (`ai_approvals.traceparent`,
  `decision_traceparent`). Only `api/backend.ts`'s middleware injects `traceparent`;
  never add trace context to another outgoing call. Tests share one provider
  (`test/support/tracing.ts` `testTracing`).
```

- [ ] **Step 4: Commit**

```bash
git add README.md docs/ai/operating.md CLAUDE.md
git commit -m "docs: the agent's tracing, its entry point and its approval columns (#988)"
```

---

## After the last task

Run the full agent CI set, with Postgres up:

```bash
cd agent
pnpm lint && pnpm typecheck && \
  SCADBUDDY_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/scadbuddy_test pnpm test && \
  pnpm build && test -f dist/telemetry.js
docker build --target agent -t scadbuddy-agent:dev ..   # from agent/, context is the repo root
```

Then open the PR `feat(tracing): agent telemetry and end-and-link approvals (#988)` with
`Refs #988` (the epic stays open for rows 2, 4 and 5), and copy "Decisions where the
spec is silent or the code forces a choice" into its description.
