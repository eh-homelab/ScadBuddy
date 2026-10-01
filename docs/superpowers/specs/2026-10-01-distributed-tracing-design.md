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
- **Resource.** Each process sets `service.name`: `scadbuddy-api`,
  `scadbuddy-worker`, `scadbuddy-agent`, `scadbuddy-web`. `service.version` is
  the build's version, and `service.instance.id` the pod's own, with the build's
  `SCADBUDDY_REVISION` as `scadbuddy.revision`. The in-process worker
  (`SCADBUDDY_TEMPORAL_WORKER_INPROCESS`) keeps `scadbuddy-api`, with
  `scadbuddy.worker.inprocess=true` on its spans. Clusters adds
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
  A row gets no `traceparent`, and a coalesced request no link, only when the
  row predates this change or the sampler dropped the first request.
- **Piece dedupe (`piece_key`).** This happens inside the workflow
  (`TemplatePipeline._piece`): a second job's workflow signals the running
  piece (`wait_for_me`) instead of starting it. The interceptor already puts
  the signal's send and handle spans in the waiting job's trace. No link back
  to the piece's own trace is built. The two are correlated by the
  `scadbuddy.piece_key` attribute both carry, which Tempo can search on.
- **Agent chat.** Browsers cannot set headers on a WebSocket, so the client
  sends `traceparent` in the first frame of each turn. Each chat turn is its own
  trace. A session-long trace would run for hours and be unusable.
- **Agent to backend.** The agent's `fetch` (undici) instrumentation injects
  `traceparent`, so a tool call's backend request is a child of its
  `agent.tool` span. `/mcp` requests continue the caller's context when one is
  sent.

## 5. Per service

### 5.1 Backend API and worker (Python)

`scadbuddy/core/tracing.py`: `configure_tracing(service_name, settings)` builds
the provider and is called once from `create_app` and from
`python -m scadbuddy.worker`. Instrumentations: FastAPI (route templates as span
names), httpx (per client, as §4 limits it), psycopg, and the Temporal
interceptor. Dependencies:
`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`opentelemetry-instrumentation-{fastapi,httpx,psycopg}`, and
`temporalio[opentelemetry]` inside the existing `<1.34` pin.

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

- Accepts OTLP/JSON only (the web exporter's default).
- Body ≤ 256 KiB and ≤ 512 spans, else 413.
- Rate limits, in memory, else 429; the client drops the batch and does not
  retry. A **global** bucket caps the relay's total rate whatever the client,
  so no spoofing can raise what reaches the collector. A **per-client** bucket
  sits under it. The client is the immediate peer's address unless that peer
  is in a new `SCADBUDDY_TRUSTED_PROXIES` (CIDRs, default empty: trust no
  forwarding header). Only then is `X-Forwarded-For` read, from the right,
  taking the first hop not in the list. These are the same semantics as the
  agent's `SCADBUDDY_AGENT_TRUSTED_PROXIES`. Like that variable it is
  infrastructure, an env-only `Settings` field, not a Postgres setting. The
  backend has no proxy-trust handling today (uvicorn runs with its defaults,
  so behind the gateway every client is the gateway). With the list empty,
  every browser shares one per-client bucket, which only lowers the cap.
  Clusters sets it to the gateway's range in clusters#1596 Phase 5.
- Browser spans are untrusted. The relay parses the payload and rewrites the
  resource: `service.name` forced to `scadbuddy-web`; every other resource
  attribute dropped except `service.version` and `user_agent.original`; per-span
  attribute count and value length capped. It then forwards to the configured
  endpoint with httpx in the background; the browser never waits on the
  collector. A forged span can still name any trace ID, but never claim to be
  the API or the worker.
- **Tracing off** (no endpoint): `204` with `X-ScadBuddy-Tracing: off`. The
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
- On `X-ScadBuddy-Tracing: off` it switches itself off and returns success for
  every later batch without sending.
- On 413 or 429 it drops the batch. Unit tests cover all three cases.

Also: fetch instrumentation that injects `traceparent` **only
for same-origin URLs** (never Bambuddy deep links or Google Fonts);
document-load instrumentation; manual spans around Generate, Print and Send,
named after the action. The msw mocks get a handler for the relay returning
the `off` response (`src/mocks/features/telemetry.ts`), so vitest and the
mocked e2e never export.

### 5.4 Agent (TypeScript)

`src/telemetry.ts`, loaded before the app with `node --import
./dist/telemetry.js dist/main.js` (ESM needs the loader hook for
instrumentation to patch modules; the Dockerfile's agent `CMD` changes to
match). `@opentelemetry/sdk-node` with the HTTP and undici instrumentations.
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

Every span therefore ends within one interaction, and a parked turn shows up
in Tempo as soon as it parks.

## 6. Span content

**Attributes** (`scadbuddy.*`): model `slug`, `job_id`, `render_key`,
`piece_key`, `output_id`; 3MF colour count, triangle count, size in bytes;
piece cache hit; printer and plate IDs on send and print; for the agent, tool
name, tier, outcome, token counts and cost. High cardinality is fine here.
These are span attributes and never become metric labels.

**Never recorded**, matching the `ai_audit` rules:

- parameter values;
- OpenSCAD source and its stderr (only the exit code and a failure class);
- prompts, model output, tool inputs and results;
- request or response headers, cookies, and query strings (no header capture
  is configured; URLs are recorded without the query);
- SQL parameter values (psycopg statement text only, sqlcommenter off);
- anything from Bambuddy beyond the status code.

**Not traced:** `/healthz`, `/metrics`, `/telemetry/v1/traces` and the
relay's httpx forwarder (they would trace their own exports), and the reconciler's idle polls (a span only when it
starts a row).

**Sampling:** `parentbased_always_on` by default; every trace is kept at
homelab volume. `OTEL_TRACES_SAMPLER` changes it from clusters. The backend
and agent honour the browser's decision.

**Errors:** `ERROR` status with `record_exception`. A failed render's
`scadbuddy.failure_class` matches the outcome recorded on its job, so a trace
and the `render_jobs` row agree.

## 7. Dashboard and how it deploys

**In this repo:** `deploy/grafana/` holds the dashboard JSON (uid `scadbuddy`,
checked against the live Grafana's uids before merge) and a
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
file that already exists. So the new step counts the anchored line first:

- **0 matches:** `::notice::` that clusters has no dashboard pin yet, and pin
  the images only. This keeps deploys working until clusters#1596 Phase 5 adds
  the line.
- **1 match:** rewrite it with the same anchored `sed`, then `expect_one` the
  rewritten line (`ref=` followed by exactly `REVISION`), as the image line is
  round-tripped.
- **More than 1:** `::error::` and stop, as for any other pinned line.

The anchor is `^\s*- https://github\.com/eh-homelab/ScadBuddy//deploy/grafana\?ref=[0-9a-f]{40}$`.
The `.github/scripts/*.test.sh` suite gets cases for all three counts.

Clusters needs, in clusters#1596 Phase 5: the remote resource line, and the
`unsetOnly` NamespaceTransformer in place of the overlay's plain
`namespace: bambuddy`, so the ConfigMap keeps `cattle-dashboards`.

**Checks in this repo** (the `lint` job): the JSON parses, the uid is
`scadbuddy`, every panel's datasource is a variable, and `kustomize build
deploy/grafana` succeeds. The `lint` job installs no kustomize today, so PR #5
adds a setup step that downloads a pinned kustomize release and checks its
published sha256 before use. It does not rely on whatever happens to be on the
runner image.

## 8. Testing

- **Backend:** an `InMemorySpanExporter` fixture. In `tests/api`, a render
  through the real Temporal path produces one trace containing the API request,
  the workflow, every activity and every `openscad.export`. A coalesced second
  submit's span links to the `traceparent` stored on the row. A stale row the
  reconciler starts lands in that same trace. A row without one gets no link.
  Relay tests: 413; 429 from the per-client bucket and from the global one;
  `X-Forwarded-For` ignored from an untrusted peer and read right to left from
  a trusted one; resource rewrite; attribute caps; the `off` response; nothing
  forwarded when off; the path never serves `index.html`. A redaction test renders
  with a sentinel parameter value and a sentinel Bambuddy key and asserts
  neither appears in any exported span. Tests in which the endpoint is unset
  assert no export is attempted.
- **Agent:** vitest with an in-memory exporter. A turn against the fake
  Anthropic endpoint yields `agent.turn` → `agent.tool/*`, and the backend
  request it makes carries the tool span's `traceparent`. A parked call ends
  its tool and turn spans with `outcome=parked` before any decision. The
  decision's `agent.approval` links to the parked tool span through the
  `ai_approvals.traceparent` column, and `agent.turn.resume` is its child. A
  turn that parks twice yields segments 0, 1 and 2, each ending as the next
  begins. An expired approval records `outcome=expired`. No prompt or tool input appears in any attribute.
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
| 4 | Frontend SDK | 2 |
| 5 | `deploy/grafana/`, its lint with a pinned kustomize, and the optional-line `ref` rewrite in `deploy.reusable.yml` with its tests | clusters#1596 Phase 4 for the Tempo panels |

Clusters#1596 Phases 1–4 can proceed in parallel. Spans flow end to end once
Phase 3 routes `alloy-receiver` to Tempo and Phase 5 sets the endpoint on the
Deployments.
