// agent/src/telemetry/runtime.ts

// The telemetry state main.ts needs, with no OpenTelemetry SDK import: main.ts
// imports this, never setup.ts, so with OTEL_SDK_DISABLED=true (src/telemetry.ts
// then never imports setup.ts) the SDK's packages are not even loaded. setup.ts
// reads the traced port and hands its started SDK over here.

/** The app's own listener (main.ts); requests on any other port are not traced. */
let tracedPort: number | undefined

/**
 * Names the one listener whose requests are traced (main.ts, before it
 * listens). Every other node:http server in the process, the plugin
 * forwarder's loopback server above all (plugins/forwarder.ts: Claude Code
 * sends no traceparent, and its path `/p/<token>` is a capability), is left
 * untraced, as is everything before a listener is named.
 */
export function traceListener(port: number): void {
  tracedPort = port
}

export function tracedListener(): number | undefined {
  return tracedPort
}

type Running = { shutdown(): Promise<unknown> }

let running: Running | undefined

/** setup.ts startTelemetry: the SDK shutdownTelemetry stops. */
export function setRunning(sdk: Running | undefined): void {
  running = sdk
}

/** Flushes and stops the SDK, within `timeoutMs`; never throws (main.ts, on SIGTERM). */
export async function shutdownTelemetry(timeoutMs = 2_000): Promise<void> {
  const sdk = running
  running = undefined
  if (!sdk) return
  await Promise.race([
    sdk.shutdown().catch(() => {}),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs).unref()),
  ])
}
