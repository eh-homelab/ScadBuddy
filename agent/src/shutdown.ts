// Graceful stop on SIGTERM (what Kubernetes sends on rollout) and SIGINT.
// Order matters: stop accepting connections and let in-flight requests finish
// FIRST, and only then the last step (main.ts: the session-event publisher those
// requests may still use).
// Bounded, so a request that never finishes cannot keep the pod in
// Terminating until the kubelet's SIGKILL.

export type ShutdownSteps = {
  /**
   * Ends long-lived streams the server would otherwise wait on until the
   * deadline: the `/mcp` sessions' standing SSE responses (app.ts `close`).
   * Run first, because `http.Server#close` waits for open connections and
   * never ends them itself.
   */
  closeSessions?: () => Promise<void>
  /** Stops accepting connections; resolves once in-flight requests have finished. */
  closeServer: () => Promise<void>
  /** Runs after the drain, however it ended: what the requests may still use. */
  closeLast?: () => Promise<void>
  /** Upper bound on waiting for the server to drain. */
  timeoutMs?: number
}

export type ShutdownResult = 'clean' | 'timed out'

export async function shutdown(steps: ShutdownSteps): Promise<ShutdownResult> {
  const { closeSessions, closeServer, closeLast, timeoutMs = 10_000 } = steps
  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<'timed out'>((resolve) => {
    timer = setTimeout(() => resolve('timed out'), timeoutMs)
  })
  let result: ShutdownResult
  try {
    const drained = (closeSessions?.() ?? Promise.resolve()).catch(() => {}).then(closeServer)
    result = await Promise.race([drained.then(() => 'clean' as const), deadline])
  } catch {
    // http.Server#close only errors when the server was not listening, in
    // which case there is nothing in flight to wait for.
    result = 'clean'
  } finally {
    clearTimeout(timer)
  }
  try {
    await closeLast?.()
  } catch {
    // Exiting anyway; a last step that fails has nothing left to protect.
  }
  return result
}
