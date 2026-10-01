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

W3C `traceparent` and `baggage` everywhere.

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
- **Coalescing and dedupe.** When `render_key` joins a request onto a job that
  is already running, or `piece_key` reuses a piece another job is rendering,
  the joining span gets a **span link** to the existing workflow's trace, not a
  parent. Both requests lead to the one render, and neither trace grows a
  foreign subtree.
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
names), httpx, psycopg, and the Temporal interceptor. Dependencies:
`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`opentelemetry-instrumentation-{fastapi,httpx,psycopg}`, and
`temporalio[opentelemetry]` inside the existing `<1.34` pin.

The render stages in `render/jobs.py` get child spans named after the
`RenderStage` set: `render.source`, `render.render`, `render.split`,
`render.solids`, `render.thumbnail`, `render.write`. Each `openscad` invocation
(`render/runner.py`) is an `openscad.export` span with format, backend, exit
code and, for `solids`, the colour index.

### 5.2 Browser relay route

`POST /api/v1/telemetry/traces`, on the backend, excluded from the OpenAPI
schema like `/metrics`. It is a transport, not an operation, so it needs no
agent tool and no `coverage.ts` entry. Same origin, so it works inside the
Bambuddy iframe.

- Accepts OTLP/JSON only (the web exporter's default).
- Body ≤ 256 KiB and ≤ 512 spans, else 413. A per-client in-memory token
  bucket (client IP from the trusted proxy chain), else 429; the client drops
  the batch and does not retry.
- Browser spans are untrusted. The relay parses the payload and rewrites the
  resource: `service.name` forced to `scadbuddy-web`; every other resource
  attribute dropped except `service.version` and `user_agent.original`; per-span
  attribute count and value length capped. It then forwards to the configured
  endpoint with httpx in the background; the browser never waits on the
  collector. A forged span can still name any trace ID, but never claim to be
  the API or the worker.
- **Tracing off** (no endpoint): `204` with `X-ScadBuddy-Tracing: off`. The
  frontend's exporter sees it on its first flush and stops exporting for the
  rest of the page's life. No new config endpoint.

### 5.3 Frontend

`src/lib/tracing.ts`, loaded lazily after first paint so it never delays the 3D
viewer: `WebTracerProvider`, `BatchSpanProcessor`, the OTLP/HTTP JSON exporter
pointed at the relay; fetch instrumentation that injects `traceparent` **only
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
the browser's child), `agent.tool/<name>`, `agent.approval` (spanning the park
in `canUseTool` until a human decides), `agent.mcp/<method>`.

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

**Not traced:** `/healthz`, `/metrics`, `/api/v1/telemetry/traces` (it would
trace its own exports), and the reconciler's idle polls (a span only when it
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
image digest, to the same `REVISION` stamped into the image. Its rules apply
unchanged: exactly one matching line before and after the rewrite (anchored
`sed`), round-trip checked; a missing line is reported and skipped until
clusters adds it, as the render worker's manifest was (#547). The dashboard
shown is then always the one written for the build that is serving.

Clusters needs, in clusters#1596 Phase 5: the remote resource line, and the
`unsetOnly` NamespaceTransformer in place of the overlay's plain
`namespace: bambuddy`, so the ConfigMap keeps `cattle-dashboards`.

**Checks in this repo** (the `lint` job): the JSON parses, the uid is
`scadbuddy`, every panel's datasource is a variable, and `kustomize build
deploy/grafana` succeeds.

## 8. Testing

- **Backend:** an `InMemorySpanExporter` fixture. In `tests/api`, a render
  through the real Temporal path produces one trace containing the API request,
  the workflow, every activity and every `openscad.export`. A coalesced second
  submit carries a link to it. Relay tests: 413, 429, resource rewrite, attribute
  caps, the `off` response, nothing forwarded when off. A redaction test renders
  with a sentinel parameter value and a sentinel Bambuddy key and asserts
  neither appears in any exported span. Tests in which the endpoint is unset
  assert no export is attempted.
- **Agent:** vitest with an in-memory exporter. A turn against the fake
  Anthropic endpoint yields `agent.turn` → `agent.tool/*`, and the backend
  request it makes carries the tool span's `traceparent`. A parked approval
  spans the wait. No prompt or tool input appears in any attribute.
- **Frontend:** vitest for the exporter's off switch; mocked e2e asserting
  `traceparent` is on same-origin requests and absent on cross-origin ones.
- **No collector in CI.** Nothing here needs network export.

## 9. Delivery

One PR per row, in order; each is useful alone.

| # | Change | Needs |
|---|---|---|
| 1 | Backend tracing core, FastAPI/httpx/psycopg, Temporal interceptor, render stage spans | — |
| 2 | Relay route | 1 |
| 3 | Agent telemetry and manual spans | 1 (for end-to-end), not for its own tests |
| 4 | Frontend SDK | 2 |
| 5 | `deploy/grafana/`, its lint, and the `ref` rewrite in `deploy.reusable.yml` | clusters#1596 Phase 4 for the Tempo panels |

Clusters#1596 Phases 1–4 can proceed in parallel. Spans flow end to end once
Phase 3 routes `alloy-receiver` to Tempo and Phase 5 sets the endpoint on the
Deployments.
