// Graceful stop on SIGTERM (what Kubernetes sends on rollout) and SIGINT.
// Order matters: stop accepting connections and let in-flight requests finish
// FIRST, and only then close the database pool those requests may be using.
// Bounded, so a request that never finishes cannot keep the pod in
// Terminating until the kubelet's SIGKILL.

export type ShutdownSteps = {
  /** Stops accepting connections; resolves once in-flight requests have finished. */
  closeServer: () => Promise<void>
  /** Closes the database pool, when there is one. */
  closeDatabase?: () => Promise<void>
  /** Upper bound on waiting for the server to drain. */
  timeoutMs?: number
}

export type ShutdownResult = 'clean' | 'timed out'

export async function shutdown(steps: ShutdownSteps): Promise<ShutdownResult> {
  const { closeServer, closeDatabase, timeoutMs = 10_000 } = steps
  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<'timed out'>((resolve) => {
    timer = setTimeout(() => resolve('timed out'), timeoutMs)
  })
  let result: ShutdownResult
  try {
    result = await Promise.race([closeServer().then(() => 'clean' as const), deadline])
  } catch {
    // http.Server#close only errors when the server was not listening, in
    // which case there is nothing in flight to wait for.
    result = 'clean'
  } finally {
    clearTimeout(timer)
  }
  try {
    await closeDatabase?.()
  } catch {
    // Exiting anyway; a pool that fails to close has nothing left to protect.
  }
  return result
}
