import type { SetupServer } from 'msw/node'

// Taken at load, before a test fakes the timers: a faked one would never fire.
const realSetTimeout = globalThis.setTimeout

/**
 * #3243 — the requests MSW has started and neither answered nor seen aborted. A test that
 * ends with one in flight could have it still running in the next test, and a failure
 * there names a test that did nothing wrong. The setup waits for them once a test has
 * unmounted (`drained`), so the next starts with none, and names any still open against
 * the test that left them. One the unmount aborted is not waited for: should the next
 * read slip past MSW after it (mswjs/interceptors#862), `resending` sends it again.
 *
 * Tests call `server.events.removeAllListeners()` to drop their own listeners, which would
 * drop these too, so that call puts them back. A request started before it is forgotten,
 * as its end may have gone unheard.
 */
export function trackInFlight(server: SetupServer) {
  const open = new Map<string, string>()
  const started = ({ requestId, request }: { requestId: string; request: Request }) => {
    open.set(requestId, `${request.method} ${new URL(request.url).pathname}`)
    // Aborted, its answer goes nowhere and MSW never ends it: no longer this test's.
    request.signal.addEventListener('abort', () => open.delete(requestId), { once: true })
  }
  const ended = ({ requestId }: { requestId: string }) => {
    open.delete(requestId)
  }
  const attach = () => {
    server.events.on('request:start', started)
    server.events.on('request:end', ended)
    // A handler that threw ends here, never at request:end.
    server.events.on('unhandledException', ended)
  }
  const events = server.events
  const removeAll = events.removeAllListeners.bind(events)
  events.removeAllListeners = (...args: Parameters<typeof removeAll>) => {
    removeAll(...args)
    open.clear()
    attach()
  }
  attach()
  return {
    /**
     * Waits up to `ms` (real time) for every open request to be answered, and returns
     * those still open, then forgets them: a handler that never answers is the test's
     * own choice, and must not be named again against the next.
     */
    async drained(ms: number): Promise<string[]> {
      for (let waited = 0; open.size > 0 && waited < ms; waited += 10) {
        await new Promise((resolve) => realSetTimeout(resolve, 10))
      }
      const left = [...open.values()]
      open.clear()
      return left
    },
  }
}
