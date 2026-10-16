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
