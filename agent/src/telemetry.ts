// agent/src/telemetry.ts
import { register } from 'node:module'

// The agent's OpenTelemetry entry point (spec 2026-10-01 §5.4), loaded before
// the app: `node --import ./dist/telemetry.js dist/main.js` (Dockerfile,
// package.json `start`). The agent is ESM, so the instrumentation needs the
// import-in-the-middle loader hook to patch node:http as the app imports it;
// CommonJS hooks alone would patch nothing. The hook is registered first, and
// the SDK is imported only after it: a static import would be hoisted above
// `register()`. With OTEL_SDK_DISABLED=true (read as setup.ts tracingDisabled
// reads it, which cannot be imported before the hook) neither is loaded: the
// hook wraps every ESM module the app loads, a cost the kill switch removes.

if (process.env.OTEL_SDK_DISABLED?.trim().toLowerCase() !== 'true') {
  register('@opentelemetry/instrumentation/hook.mjs', import.meta.url)
  const { startTelemetry } = await import('./telemetry/setup.js')
  startTelemetry()
}
