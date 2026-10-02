# Distributed tracing with native OpenTelemetry

Issue: #988. Cluster side: eh-homelab/clusters#1596.

## 1. Goal and scope

One trace per user action, from the click in the browser through the API,
Temporal, the render worker, `openscad`, and the agent, viewed in the in-cluster
(rancher-monitoring) Grafana. Plus a ScadBuddy dashboard that lives in this
repo and is deployed at the same revision as the image.

Every service uses the OpenTelemetry SDKs directly and exports OTLP. Which store
receives the spans is collector config in clusters (Tempo on a dedicated DO
Spaces bucket, clusters#1596), never app config.

### Non-goals

- **Metrics.** `core/metrics.py` stays on `prometheus_client`. Moving it to the
  OTel metrics API is a later change; metric names must not move when it does.
  The one addition here is the relay's own outcome counter (§5.2), a
  `prometheus_client` counter like the rest.
- **Log correlation** (trace IDs in log lines). Later.
- **Tracing Claude Code itself.** The bundled CLI runs with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` and the credential in its
  environment; giving it an exporter is out of scope. The agent traces around
  each `query()` instead (§5.4).
- **Tail sampling.** If ever needed, it goes in alloy, not in the apps.
- **ClickHouse.** Recorded in clusters#1596 as a separate, cluster-wide decision.

## 2. Approach

Explicit SDK setup in each service: one small module per service builds the
`TracerProvider` from the standard `OTEL_*` variables and enables named
instrumentations. Rejected: zero-code wrappers (`opentelemetry-instrument`,
`auto-instrumentations-node/register`), because Temporal's interceptor still
needs code, the Node bundle pulls in dozens of unused instrumentations, and the
behaviour is harder to pin in tests; and OTel Operator injection, which the
cluster does not run and which covers neither the browser nor Temporal context.

## 3. Configuration and identity

- **Only standard `OTEL_*` variables**, no `SCADBUDDY_` aliases:
  `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_PROTOCOL` (the services
  ship only the `http/protobuf` exporter and set it as their default),
  `OTEL_TRACES_SAMPLER`, `OTEL_SDK_DISABLED`, `OTEL_RESOURCE_ATTRIBUTES`. This
  is a deliberate exception to "settings live in Postgres": the exporter is
  configured before the database is reachable, and it is infrastructure, like
  `SCADBUDDY_DATABASE_URL`.
- **No endpoint, no export.** With `OTEL_EXPORTER_OTLP_ENDPOINT` unset, each
  service installs a provider with no exporter: spans are created (so context
  still propagates) and dropped. Tests, CI and a local `docker run` need nothing.
- **`OTEL_SDK_DISABLED=true`** is the kill switch, for a suspected SDK
  problem, not for "tracing off": it replaces the provider with the API's
  no-op one. No spans are created, so nothing propagates.
  `render_jobs.traceparent` and `ai_approvals.traceparent` stay null. The
  relay answers `off` too (§5.2), so the switch silences browser spans as
  well, not only the backend's own. Leaving the
  endpoint unset is the normal way to run without tracing. Clusters never
  sets this variable unless it is ruling the SDK out of an incident.
- **Resource.** Each process sets `service.name`: `scadbuddy-api`,
  `scadbuddy-worker`, `scadbuddy-agent`, `scadbuddy-web`. `service.version` is
  the build's version, and `service.instance.id` the process's host name
  (Python `socket.gethostname()`, Node `os.hostname()`), with the build's
  `SCADBUDDY_REVISION` as `scadbuddy.revision`. The in-process worker
  (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`) keeps `scadbuddy-api`, with
  `scadbuddy.worker.inprocess=true` on its spans. In the cluster the host name
  is the pod's name, because Kubernetes sets it so, not because the app asks.
  Elsewhere it is whatever the host is called. No Kubernetes API or downward
  API is read. Clusters adds
  `deployment.environment` and the k8s attributes via `OTEL_RESOURCE_ATTRIBUTES`;
  the apps know nothing about Kubernetes.

## 4. Propagation

W3C Trace Context only: `OTEL_PROPAGATORS=tracecontext` in every service and
the same single propagator in the browser. Baggage is not propagated. Nothing
needs it, and its values travel as plain text to whatever is called next.

**Trace context never leaves ScadBuddy.** It goes to ScadBuddy's own services
(API, worker, agent, Temporal) and nowhere else. httpx is not instrumented
process-wide (no `HTTPXClientInstrumentor().instrument()`).
`HTTPXClientInstrumentor.instrument_client` is applied only to clients that
call ScadBuddy's own services. The Bambuddy client (`bambuddy/client.py`)
instead gets a manual client span per call (`bambuddy.<operation>`, with the
status code and the client's `Scope`) and **injects no headers**. A test
asserts that a Bambuddy request carries no `traceparent`. The relay's
forwarder to the collector is not instrumented at all (§6).

```
browser ──fetch/WS(traceparent)──▶ API ──Temporal headers──▶ workflow ──▶ activities (worker)
   │                                │                                       └─ openscad spans
   │                                └─httpx──▶ Bambuddy (client span only)
   └──WS /api/v1/ai/chat(traceparent)──▶ agent ──fetch──▶ API
                                           └─ turn ▶ tool calls ▶ approvals (manual spans)
```

- **Temporal.** `temporalio.contrib.opentelemetry.TracingInterceptor` on the
  API's client and on the worker (`workflows/client.py` `connect`,
  `render_worker`). Context rides in workflow headers, so a retried activity, or
  a workflow resumed by another worker after a drain, stays in its trace. Inside
  workflow code only the interceptor's replay-safe spans run; real work is
  traced in the activities.
- **Coalescing (`render_key`).** Coalescing happens in the store before
  Temporal is touched: `RenderService.submit` gets the existing row back from
  `store.submit` and returns without starting anything. So the first caller's
  context is persisted on the row: a new nullable `render_jobs.traceparent`
  column (a new migration), written by the insert. A coalesced request reads it
  from the returned job and adds a **span link** to it on its own submit span,
  not a parent. The reconciler starts a stale row's workflow under that same
  `traceparent`, so a render started late still lands in its first caller's
  trace. The column is written whenever the submit span's context is valid
  and sampled. That includes a process with no exporter, which still creates
  and propagates spans (§3); persisting a context nobody exports is harmless.
  A row gets no `traceparent`, and a coalesced request no link, only when:
  - the row predates this change;
  - the sampler dropped the first request;
  - or the SDK was off when the row was written (`OTEL_SDK_DISABLED`, §3),
    whose no-op provider produces no valid context.
- **Piece dedupe (`piece_key`).** This happens inside the workflow
  (`TemplatePipeline._piece`): a second job's workflow signals the running
  piece (`wait_for_me`) instead of starting it. The interceptor already puts
  the signal's send and handle spans in the waiting job's trace. No link back
  to the piece's own trace is built. The two are correlated by the
  `scadbuddy.piece_key` attribute both carry, which Tempo can search on.
- **Agent chat.** Browsers cannot set headers on a WebSocket, so the client
  sends `traceparent` in the first frame of each turn. Each chat turn is its own
  trace. A session-long trace would run for hours and be unusable.
- **Agent to backend.** The agent applies the same boundary as the backend.
  Outgoing requests are not instrumented process-wide (§5.4). The backend
  client (`src/api/backend.ts`, `openapi-fetch`) gets an openapi-fetch
  middleware that opens a client span and injects `traceparent` itself, so a
  tool call's backend request is a child of its `agent.tool` span. Nothing
  else the agent calls gets trace context: the remote-plugin forwarder
  (`src/plugins/forwarder.ts`, over `http/pinned.ts`), the `http_request`
  tool, the headless browser, plugin package fetches, and Anthropic. `/mcp`
  requests continue the caller's context when one is sent.

## 5. Per service

### 5.1 Backend API and worker (Python)

`scadbuddy/core/tracing.py`: `configure_tracing(service_name, settings)` builds
the provider and is called once from `create_app` and from
`python -m scadbuddy.worker`. Instrumentations: FastAPI (route templates as span
names), httpx (per client, as §4 limits it), psycopg, and the Temporal
interceptor. Dependencies:
`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`opentelemetry-instrumentation-{fastapi,httpx,psycopg}`, and
`temporalio[opentelemetry]` inside the existing `<1.34` pin. Verified against
PyPI on 2026-10-01: 1.33.0 declares the `opentelemetry` extra
(`opentelemetry-api` and `opentelemetry-sdk`, `>=1.26,<2`) and ships
`temporalio.contrib.opentelemetry`, so no Temporal bump comes first.

The render stages in `render/jobs.py` get child spans named after the
`RenderStage` set: `render.source`, `render.render`, `render.split`,
`render.solids`, `render.thumbnail`, `render.write`. Each `openscad` invocation
(`render/runner.py`) is an `openscad.export` span with format, backend, exit
code and, for `solids`, the colour index.

### 5.2 Browser relay route

`POST /telemetry/v1/traces`, on the backend. Like `/metrics` and `/healthz` it
is mounted outside `/api/v1` (beside them in `main.py`, before
`_api_router()`) and excluded from the OpenAPI schema. It is a transport, not a
versioned operation, so nothing `_api_router()` attaches applies to it, and it
needs no agent tool and no `coverage.ts` entry. It is registered before the
SPA's static fallback, so the path never serves `index.html`. Same origin, so
it works inside the Bambuddy iframe. Clusters' HTTPRoute already sends every
path except `/api/v1/ai/*` to the backend.

- **Browser, same origin only.** A page on another origin must not be able
  to drive the relay through a LAN user's browser, to spend its budget or
  inject spans. The relay is **not** unusual in being exposed to this. Today
  the backend's REST writes accept any `Origin`: creating and deleting models,
  restoring revisions, submitting renders, sending and printing through
  Bambuddy. That is tracked in #962, and it is a much more serious exposure
  than this one.
  - The relay's check is the same check #962 calls for, written so that
    #962 can lift it into a shared guard for every write. When #962 lands,
    the relay uses that guard instead of its own copy.
  - Until then the relay ships with its own copy, rather than adding one
    more unchecked POST.
  - This design does not fix #962 and does not depend on it.
  - First, a separate check: a request with no `Origin` is refused.
    `origin_allowed` cannot do this, because it returns `True` for `None`
    on purpose (for the socket, a non-browser caller is as trusted as a REST
    call). A browser always sends `Origin` on a `fetch` POST, so its absence
    means the caller is not this page.
  - Then `origin_allowed` (`api/realtime.py`, the socket's check against
    `SCADBUDDY_PUBLIC_URL` and `SCADBUDDY_ALLOWED_ORIGINS`) must accept the
    `Origin` that is present.
  - When `Sec-Fetch-Site` is present it must be `same-origin`.
  - Anything else is 403, before the body is read.
  - The `Origin` check is the control that matters. A cross-origin page can
    skip the preflight altogether (`mode: 'no-cors'` with a safelisted
    `Content-Type` such as `text/plain`). The browser then still sends the
    request, and only hides the response. That request carries the foreign
    `Origin` and is refused with 403. Its `Content-Type` would be refused
    with 415 anyway.
  - The route sends no CORS headers and answers no preflight. That keeps a
    cross-origin `fetch` in `cors` mode from ever reading a response, but it
    is not relied on to stop a request.
- Accepts OTLP/JSON only (the web exporter's default); any other
  `Content-Type` is 415.
- Body ≤ 256 KiB through the existing `BodySizeGate`: a `RouteLimit` for
  `POST /telemetry/v1/traces` beside the media upload's in `main.py`, so an
  oversized body is refused on its headers, or as it streams when it has no
  `Content-Length`, before the handler runs. Without one the
  `application/json` default (8 MiB) would apply. The 512-span cap is checked
  after parsing; over it is also 413.
- Rate limits, in memory with `RateLimit` (`api/realtime.py`'s token bucket),
  else 429; the client drops the batch and does not
  retry. A **per-process** bucket caps the relay's total rate whatever the
  client, so no spoofing of the client's address raises what one pod sends
  the collector. It is per pod, not cluster-wide: with N API replicas the
  ceiling is N times the configured rate. The API runs one replica (as of
  2026-10-01, `replicas: 1` in eh-homelab/clusters'
  `applications/scadbuddy/scadbuddy.yaml`; clusters#1596 Phase 5 is where a
  change to that would have to adjust this rate), and a shared bucket in Postgres would cost a write per
  batch on a path whose only job is to be cheap. So this approximation is
  accepted and documented beside the setting. Scaling the API means dividing
  the rate by the replica count. A **per-client** bucket sits under it. The client is the immediate peer's address unless that peer
  is in a new `SCADBUDDY_TRUSTED_PROXIES` (CIDRs, default empty: trust no
  forwarding header). Only then is `X-Forwarded-For` read, from the right,
  taking the first hop not in the list. This reuses the CIDR-matching idea of
  the agent's `SCADBUDDY_AGENT_TRUSTED_PROXIES`, but on a different header for
  a different purpose. The agent's list (`src/http/origins.ts`) decides
  whether to believe `X-Forwarded-Proto`/`X-Forwarded-Host` when it
  reconstructs an origin. This one decides whether to believe
  `X-Forwarded-For` when it picks a client address for a rate-limit bucket.
  The agent has no `X-Forwarded-For` handling to copy; PR #2 writes it. Like that variable it is
  infrastructure, an env-only `Settings` field, not a Postgres setting. The
  backend has no proxy-trust handling today (uvicorn runs with its defaults,
  so behind the gateway every client is the gateway). With the list empty,
  every browser shares one per-client bucket, which only lowers the cap.
  Clusters sets it to the gateway's range in clusters#1596 Phase 5.
- Browser spans are untrusted. The relay parses the payload and rewrites the
  resource: `service.name` forced to `scadbuddy-web`; every other resource
  attribute dropped except `service.version` and `user_agent.original`. Then
  per-span caps; beyond them the excess is dropped (truncated, for strings) and
  the span counts it in `otel.dropped_attributes_count` (OTel's own field):
  - 64 attributes;
  - string values of 1024 characters;
  - arrays of 32 items;
  - 16 events with 16 attributes each;
  - 8 links;
  - a span name of 128 characters.

  These match the SDK limits `RelayExporter`'s provider is configured with
  (`spanLimits`), so a well-behaved page never hits them.
- **Forwarding** happens in the background, so the browser never waits on the
  collector. It must not lose spans silently:
  - An accepted batch goes on a bounded in-memory queue: 64 batches,
    16 MiB at most, given the 256 KiB cap.
  - One forwarding task, started and stopped in the app's lifespan, posts
    the queue to the endpoint with httpx, with a 5 s timeout.
  - **No retries in the app.** A failed post (unreachable, timeout, any
    non-2xx) drops its batch. Retrying and buffering are alloy's job, and
    the browser is already gone.
  - When the queue is full, a new batch is dropped at once, and the browser
    still gets its 204.
  - **Shutdown:** on SIGTERM the lifespan stops accepting (new requests get
    503), then drains the queue for up to 5 s in total, inside the pod's
    grace period, and drops whatever is left.
    - During the drain, each post's timeout is whatever remains of the
      budget, not a fresh 5 s.
    - The first post that fails or times out ends the drain at once: an
      unreachable collector would fail every later post the same way.
    - The rest is dropped and counted as `shutdown`.
    - This is an accepted trade-off. When the collector is down at shutdown,
      the queued browser batches are lost either way. The only choice is
      whether the pod waits out its grace period first, and it should not.
  - **Visibility:** every outcome is counted in
    `scadbuddy_trace_relay_batches_total{outcome}`, with the outcomes
    `forwarded`, `failed`, `queue_full` and `shutdown`. The counter goes in
    `core/metrics.py` beside the others and is pre-created at zero, so its
    first increase alerts. Failures also log one warning a minute at most,
    naming the status or the error class. This one counter is the only
    metric change in this design: it watches the tracing path itself, and
    it is not a move of metrics to OTel. A forged span can still name any trace ID, but never claim to be
  the API or the worker.
- **Accepted residual risk.** Script running on ScadBuddy's origin (today
  only the app; in future an XSS or a compromised dependency) can post spans
  into any trace ID it knows or guesses, so they show up inside someone
  else's render or turn in Tempo. This is accepted:
  - the harm is to what the trace view shows, never to data;
  - the relay never reads anything back;
  - the forged spans are always `service.name=scadbuddy-web` and carry the
    relay's client address, so they can be told apart from the backend's
    own;
  - an attacker who can already run script on the origin can call the API
    directly, which is far worse.

  The dashboard's trace panels filter on server-side services by default.
- **Error bodies.** Every refusal is an RFC 9457 problem document
  (`application/problem+json`), like the rest of the API's errors, though the
  route is outside `/api/v1`.
  - 403, 415 and 429 are raised as `ApiError` and rendered by
    `core/problems.py`'s `problem_response`. They are not hand-built here.
  - 429 also carries `Retry-After`, the seconds until the bucket that refused
    it refills one batch.
  - 413 is `BodySizeGate`'s own problem document, also RFC 9457.
  - A refusal's `detail` names the rule ("Origin not allowed", "the relay
    accepts application/json only"), never the request's own values.
- **Tracing off** (no endpoint, or `OTEL_SDK_DISABLED=true`): `204` with
  `X-ScadBuddy-Tracing: off`. The
  frontend's exporter (§5.3) sees it on its first flush and stops exporting for
  the rest of the page's life. No new config endpoint.

### 5.3 Frontend

`src/lib/tracing.ts`, loaded lazily after first paint so it never delays the 3D
viewer: `WebTracerProvider` and `BatchSpanProcessor`, exporting through
`RelayExporter`, a small `SpanExporter` of our own. The stock
`@opentelemetry/exporter-trace-otlp-http` does not hand response headers to
its caller, so it cannot see the relay's off signal.
- `RelayExporter` serialises with `@opentelemetry/otlp-transformer`'s JSON
  trace serializer and POSTs to `/telemetry/v1/traces` with `fetch`
  (`keepalive` so a batch flushed on page hide still goes).
- Browsers cap a page's in-flight `keepalive` bodies at 64 KiB in total. So:
  - the provider batches at most 64 spans (`maxExportBatchSize`);
  - `RelayExporter` splits any serialised batch over 48 KiB into several
    requests;
  - it sends one request at a time, so in-flight `keepalive` bytes stay
    under the cap;
  - a single span over 48 KiB after the SDK limits below is dropped and
    counted.

  The relay's 256 KiB ceiling only bounds non-browser callers; the browser
  never comes near it. A `fetch` that rejects (offline, the page torn down
  mid-send) drops its batch. Unit tests cover the split and the one-at-a-time
  send.
- On `X-ScadBuddy-Tracing: off` it switches itself off and returns success for
  every later batch without sending.
- On 413 or 429 it drops the batch. Unit tests cover all three cases.

Also: fetch instrumentation that injects `traceparent` **only
for same-origin URLs** (never Bambuddy deep links or Google Fonts);
document-load instrumentation; manual spans around Generate, Print and Send,
named after the action.

**Dev server.** `frontend/vite.config.ts`'s proxy table (also used by
`pnpm preview`) gets a rule for `^/telemetry(?:/|$)` to the backend, beside
the existing `/api` rule. Without it, `vite dev` against a real backend would
send every batch to Vite's own SPA fallback. The batch would be lost with no
error, because `RelayExporter` drops batches without retrying. Vite's
`changeOrigin` rewrites `Host`, not `Origin`, so the page's
`http://localhost:5173` still reaches the relay. `origin_allowed` accepts
loopback hosts.

The msw mocks get a handler for the relay returning
the `off` response (`src/mocks/features/telemetry.ts`), so vitest and the
mocked e2e never export.

### 5.4 Agent (TypeScript)

`src/telemetry.ts`, loaded before the app with `node --import
./dist/telemetry.js dist/main.js` (ESM needs the loader hook for
instrumentation to patch modules). Every entry point that runs
`dist/main.js` changes together, so tracing is never something only Docker
knows how to start:
- the Dockerfile's agent `CMD`;
- `agent/package.json`'s `start` script;
- the manual setup comments in `frontend/e2e/agent-link.real.spec.ts`.

`src/telemetry.ts` registers `@opentelemetry/instrumentation/hook.mjs` with
`node:module`'s `register()` before starting the SDK. The agent compiles to
ESM (`"type": "module"`, `module: NodeNext`), so CommonJS hooks alone would
patch nothing.

**Verified against npm on 2026-10-01** (every package declares
`node ^18.19.0 || >=20.6.0`, which covers the pinned Node 24):
- `@opentelemetry/sdk-node`, `@opentelemetry/instrumentation-http`,
  `@opentelemetry/instrumentation`, `@opentelemetry/otlp-transformer` and
  `@opentelemetry/instrumentation-fetch` are at 0.222.0;
- `@opentelemetry/sdk-trace-web` is at 2.11.0;
- `@opentelemetry/instrumentation-document-load` is at 0.67.0;
- `@opentelemetry/instrumentation` 0.222.0 ships `hook.mjs` and depends on
  `import-in-the-middle` ^3.

These packages do not touch the Claude Agent SDK. They patch `node:http`
only, and the SDK's bundled CLI runs as a child process the hook never
loads into. The implementing PRs pin exact versions, as `package.json`
already does for `@anthropic-ai/claude-agent-sdk`, and record their own
check if the versions have moved by then. `@opentelemetry/sdk-node` with the HTTP instrumentation for
**incoming** requests only (`ignoreOutgoingRequestHook: () => true`, so no
outgoing `node:http`/`https` request is touched, the pinned plugin
forwarder's included). The undici instrumentation is not installed, so
`fetch` is untouched too. The one outgoing call that carries context is the
backend client's middleware (§4). Tests assert that a remote plugin request
through the forwarder, an `http_request` call and a request to the fake
Anthropic endpoint carry no `traceparent`, and that a backend request does.
Porsager's `postgres` has no instrumentation; database work is not traced in
this phase. Manual spans: `agent.turn` (one per chat turn, the trace root or
the browser's child), `agent.tool/<name>`, `agent.mcp/<method>`.

**Approvals end and link; they never nest the wait.** An outward call can park
in `canUseTool` for up to `MAX_APPROVAL_EXPIRY_SECONDS` (24 hours;
`approvals/service.ts`). A span is exported only when it ends, so nesting the
park inside the turn would keep the turn's root and tool spans out of Tempo for
the whole wait. That is the failure §4 rejects session-long traces for. So:

- When a call parks, its `agent.tool/<name>` span and the `agent.turn` span
  end at once with `scadbuddy.outcome=parked` and the approval id. The parked
  tool span's context is stored on the `ai_approvals` row (a new nullable
  `traceparent` column, in a new agent migration).
- The human's decision is its own trace, rooted at the approve or deny
  request (a traced HTTP call from the UI, or the `sessions_approve` tool). Its
  `agent.approval` span carries a **link** to the parked tool span and records
  the decision and the wait in seconds.
- When the call goes ahead, the rest of the turn is traced as
  `agent.turn.resume`, a child of the decision span: the tool's execution, its
  backend calls, and the turn's remaining tool calls. An expired approval
  records `agent.approval` with `outcome=expired` and the same link.
- **A turn can park any number of times.** Each park ends the segment that is
  open (`agent.turn` the first time, the current `agent.turn.resume` after
  that), together with its tool span, with `outcome=parked`. Each decision is
  its own trace, and its continuation is a new `agent.turn.resume`, so the
  names never nest (`.resume.resume`). Every segment carries
  `scadbuddy.turn_id` and `scadbuddy.segment` (0 for the turn, 1, 2, … for
  each resume), so one search on the turn id returns all of them, in order.
- **Parks at the same time.** A parallel tool-use turn can park several calls
  at once, and `ai_approvals` already holds several undecided rows per
  session. The segment ends once, at the first park. Ending it is idempotent,
  so a second park in the same segment ends only its own tool span. A call
  from that segment that did not park keeps its span open until it finishes;
  OTel allows a child to end after its parent. Each pending call gets its own
  decision trace, and its tool execution is a child of that decision. The
  next `agent.turn.resume` begins when the harness continues, which is after
  the last of the segment's parked calls is decided. It is a child of that
  last decision, with links to the other decisions of the same segment.

Every span therefore ends within one interaction, and a parked turn shows up
in Tempo as soon as it parks.

## 6. Span content

**Attributes** (`scadbuddy.*`): model `slug`, `job_id`, `render_key`,
`piece_key`, `output_id`; 3MF colour count, triangle count, size in bytes;
piece cache hit; printer and plate IDs on send and print; for the agent, tool
name, tier, outcome, token counts and cost. High cardinality is fine here.
These are span attributes and never become metric labels.

**Never recorded.** This list is this design's own. It follows the same
approach as `ai_audit` (`agent/src/audit/log.ts`), which records a fixed set
of fields and keeps inputs only as keyed hashes and scrubbed summaries. It is
not copied from that module, which has no such list:

- parameter values;
- OpenSCAD source and its stderr (only the exit code and a failure class);
- prompts, model output, tool inputs and results;
- request or response headers, cookies, and query strings (no header capture
  is configured; URLs are recorded without the query);
- SQL parameter values (psycopg statement text only, sqlcommenter off);
- anything from Bambuddy beyond the status code.

**Not traced:**
- `/healthz`, `/metrics` and `/telemetry/v1/traces`, through
  `FastAPIInstrumentor.instrument_app(app,
  excluded_urls="/healthz,/metrics,/telemetry/v1/traces")`. This is set in
  code, not by `OTEL_PYTHON_FASTAPI_EXCLUDED_URLS`, so a deployment cannot
  lose it. The worker's own `/healthz` and `/metrics` on 9090 are served by
  its health server, which is never instrumented.
- The relay's httpx forwarder, whose client is never passed to
  `instrument_client` (§4). Both would otherwise trace their own exports.
- The reconciler's idle polls: a span only when it starts a row.

A test requests each excluded path and asserts no span was recorded.

**Sampling:** `parentbased_always_on` by default; every trace is kept at
homelab volume. `OTEL_TRACES_SAMPLER` changes it from clusters. The backend
and agent honour the browser's decision.

**Errors:** `ERROR` status, with the exception's type and where it was raised,
**never its message**. A failed render's `scadbuddy.failure_class` matches the
outcome recorded on its job, so a trace and the `render_jobs` row agree.

Exception messages carry exactly what the list above forbids:
- `ParameterValueError` and the other checks in `render/runner.py` interpolate
  the raw value (`got {value!r}`);
- `map_response` in `bambuddy/errors.py` puts Bambuddy's own `detail` into the
  `ApiError` message.

Our code is not the only caller of `record_exception`. The FastAPI/ASGI
instrumentation records unhandled exceptions, and Temporal's
`TracingInterceptor` records failures and sets the status description to
`str(error)`. So the rule is enforced **once, in front of every exporter**, not
at each call site. A `ScrubbingSpanExporter` wraps the OTLP exporter in each
service (and `RelayExporter` in the browser), and every span passes through it
before it leaves the process. It:

- drops `exception.message` from every `exception` event;
- replaces `exception.stacktrace` with its frame lines only (Python: the
  `File "…", line N, in f` lines; Node: the `at …` lines). A formatted
  traceback otherwise ends with, and for chained exceptions repeats, the
  messages;
- keeps `exception.type`;
- replaces a non-empty status description with the exception type, or with
  `error` when there is none.

The relay applies the same scrub to browser spans before forwarding. Our own
spans set `scadbuddy.failure_class` (the problem `type_` for an `ApiError`,
plus the client's `Scope` for a Bambuddy call) as the readable cause. Where
the message is needed, it is already in the job row or the response the user
saw.

## 7. Dashboard and how it deploys

**In this repo:** `deploy/grafana/` holds the dashboard JSON (uid `scadbuddy`) and a
`kustomization.yaml` whose `configMapGenerator` makes a ConfigMap labelled
`grafana_dashboard: "1"` in `cattle-dashboards`, the namespace the
rancher-monitoring sidecar watches. Datasources are dashboard variables
(Prometheus, and Tempo defaulting to uid `tempo` from clusters#1596), never
hard-coded uids.

**Panels:**

- *Renders:* the existing Prometheus series (queue depth, wait, stage seconds,
  outcomes, SLOs), with a trace-search panel beside them:
  `{resource.service.name="scadbuddy-worker" && name="render.render"}`, slowest
  and failed first.
- *HTTP:* request rate and duration by route template (existing metrics), and
  a Tempo table of the slowest API traces.
- *Agent:* recent `agent.turn` traces with tool count, approvals and errors.
- *Browser:* recent `scadbuddy-web` traces by action.

Panels that need Tempo's metrics-generator (TraceQL metrics, service graph)
are left out until clusters#1596 decides on it.

**Pinned at the deployed revision.** Clusters' `clusters/prod/scadbuddy`
overlay references it as a remote kustomize resource:

```yaml
resources:
  - https://github.com/eh-homelab/ScadBuddy//deploy/grafana?ref=<40-char revision>
```

`deploy.reusable.yml` rewrites that `ref` in the same deploy PR that moves the
image digest, to the same `REVISION` stamped into the image. The dashboard
shown is then always the one written for the build that is serving.

This needs **new logic** in the workflow, not just its existing rules. Today's
`expect_one` is unconditional: a count other than 1 fails the whole deploy.
The #547 precedent skips a whole manifest file that is absent (`[ -f ... ]`),
but the dashboard line sits in `clusters/prod/scadbuddy/kustomization.yaml`, a
file that already exists. So the new step counts the anchored line first.
The step runs under `set -euo pipefail`, so both counts use `expect_one`'s
existing idiom, `n=$(grep -c… "$file" || true)`. A bare `grep -c` exits 1 on
zero matches and would abort the step in exactly the "not configured" case it
has to handle:

- **Not configured:** the anchored pattern matches 0 lines and the file
  does not mention `eh-homelab/ScadBuddy//deploy/grafana` at all (an
  unanchored, fixed-string, case-insensitive `grep -iF`, so a near-miss in
  casing still counts as a mention). Post a `::notice::` that clusters has
  no dashboard pin yet, and pin the images only. This keeps deploys working
  until clusters#1596 Phase 5 adds the line.
- **Malformed:** the anchored pattern matches 0 lines but the path does
  appear (a short SHA, a tag or branch for `ref`, a comment on the line,
  different spacing or casing). `::error::` and stop. A near miss must never
  be read as "not configured", or a stale dashboard would stay pinned
  silently.
- **1 match:** rewrite it with the same anchored `sed`, then `expect_one` the
  rewritten line (`ref=` followed by exactly `REVISION`), as the image line is
  round-tripped.
- **More than 1:** `::error::` and stop, as for any other pinned line.

The anchor is `^\s*- https://github\.com/eh-homelab/ScadBuddy//deploy/grafana\?ref=[0-9a-f]{40}$`.
The `.github/scripts/*.test.sh` suite gets cases for all four outcomes, the
malformed one among them with a short SHA, a branch ref, a trailing comment
and a lower-cased `scadbuddy//deploy/grafana`.

Clusters needs, in clusters#1596 Phase 5: the remote resource line, and the
`unsetOnly` NamespaceTransformer in place of the overlay's plain
`namespace: bambuddy`, so the ConfigMap keeps `cattle-dashboards`.

**Checks in this repo** (the `lint` job): the JSON parses, the uid is
`scadbuddy`, every panel's datasource is a variable, and `kustomize build
deploy/grafana` succeeds. The `lint` job installs no kustomize today, so PR #5
adds a setup step that downloads a pinned kustomize release and checks its
published sha256 before use. It does not rely on whatever happens to be on the
runner image.

**A manual step CI cannot do.** Nothing in CI checks that uid `scadbuddy` is
free in the live Grafana: the hosted runners cannot reach the cluster, and
this repo's jobs never run on the LAN pools. If two dashboards share a uid,
the sidecar loads both and one silently replaces the other. So PR #5's
description carries a checkbox: whoever opens it lists the live uids (Grafana
`GET /api/search?type=dash-db` from the LAN) and confirms `scadbuddy` is not
among them, as the bambuddy dashboard's `bambuddy` uid was checked in clusters.
The uid never changes after that, so the check is needed once.

## 8. Testing

- **Backend:** an `InMemorySpanExporter` fixture. In `tests/api`, a render
  through the real Temporal path produces one trace containing the API request,
  the workflow, every activity and every `openscad.export`. A coalesced second
  submit's span links to the `traceparent` stored on the row. A stale row the
  reconciler starts lands in that same trace. A row without one gets no link.
  Relay tests (each refusal asserting its status, `application/problem+json`
  body and, for 429, `Retry-After`): 403 for a missing or foreign `Origin` and for a
  `Sec-Fetch-Site` other than `same-origin`, with no CORS headers on any
  response; 415; 413 from the `RouteLimit` with and without `Content-Length`
  and past 512 spans; 429 from the per-client bucket and from the per-process one;
  `X-Forwarded-For` ignored from an untrusted peer and read right to left from
  a trusted one; resource rewrite; each attribute, event, link and name cap at
  its limit and one past it, with the dropped count; the `off` response; nothing
  forwarded when off; the path never serves `index.html`; a failing
  collector, a full queue and a shutdown with batches still queued each
  increment their outcome and never fail the browser's request.
  A shutdown with a dead collector ends its drain after the first failed post,
  well inside the 5 s budget, and counts the rest as `shutdown`. Redaction tests put a
  sentinel string in each forbidden place and assert it appears nowhere in any
  exported span: not in attributes, event attributes or status descriptions,
  on success or on failure. The places are:
  - a parameter value on a successful render;
  - the same value made invalid, so `ParameterValueError` fires;
  - the Bambuddy API key;
  - a Bambuddy 409 whose `detail` is the sentinel;
  - an unhandled exception in a route, raised with the sentinel as its message;
  - an activity that fails with it;
  - a browser span whose exception event carries it, sent through the relay.

  `ScrubbingSpanExporter` has unit tests of its own for chained exceptions and
  for both stack formats. Tests in which the endpoint is unset
  assert no export is attempted.
- **Agent:** vitest with an in-memory exporter. A turn against the fake
  Anthropic endpoint yields `agent.turn` → `agent.tool/*`, and the backend
  request it makes carries the tool span's `traceparent`. A parked call ends
  its tool and turn spans with `outcome=parked` before any decision. The
  decision's `agent.approval` links to the parked tool span through the
  `ai_approvals.traceparent` column, and `agent.turn.resume` is its child. A
  turn that parks twice yields segments 0, 1 and 2, each ending as the next
  begins. Two calls parked at once end segment 0 once and get a decision trace
  each, and segment 1 is the last decision's child, linked to the other. An
  expired approval records `outcome=expired`. No prompt, tool input or tool
  result appears in any attribute, and a tool that throws with a sentinel
  message leaves no trace of it after `ScrubbingSpanExporter`.
- **Frontend:** vitest for the exporter's off switch; mocked e2e asserting
  `traceparent` is on same-origin requests and absent on cross-origin ones.
- **No collector in CI.** Nothing here needs network export.

## 9. Delivery

One PR per row, in order; each is useful alone.

| # | Change | Needs |
|---|---|---|
| 1 | Backend tracing core, FastAPI/httpx/psycopg, Temporal interceptor, render stage spans, `render_jobs.traceparent` and the coalesce link | — |
| 2 | Relay route, `SCADBUDDY_TRUSTED_PROXIES` | 1 |
| 3 | Agent telemetry, manual spans, `ai_approvals.traceparent` and the end-and-link approvals | 1 (for end-to-end), not for its own tests |
| 4 | Frontend SDK, `RelayExporter`, the `vite.config.ts` proxy rule | 2 |
| 5 | `deploy/grafana/`, its lint with a pinned kustomize, and the optional-line `ref` rewrite in `deploy.reusable.yml` with its tests | clusters#1596 Phase 4 for the Tempo panels |

Row 3 can ship before rows 2 and 4. Until they land, an agent turn's trace
simply starts at the agent instead of the browser: the chat socket's
`traceparent` is optional (§4), and without one `agent.turn` is the root.
Nothing in the agent waits on the relay. Rows 2 and 4 only add the browser
segment in front of it.

Clusters#1596 Phases 1–4 can proceed in parallel. Spans flow end to end once
Phase 3 routes `alloy-receiver` to Tempo and Phase 5 sets the endpoint on the
Deployments.
