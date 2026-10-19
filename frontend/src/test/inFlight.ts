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
 * drop these too, so that call puts back the ones it removed, before any event can pass.
 * `sharedInFlight` is the one the setup waits on; a test reads its `open` count.
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
  const listeners = {
    'request:start': () => server.events.on('request:start', started),
    'request:end': () => server.events.on('request:end', ended),
    // A handler that threw ends here, never at request:end.
    unhandledException: () => server.events.on('unhandledException', ended),
  }
  const events = server.events
  const original = events.removeAllListeners
  const removeAll = original.bind(events)
  events.removeAllListeners = (...args: Parameters<typeof removeAll>) => {
    removeAll(...args)
    const [type] = args
    for (const [name, attach] of Object.entries(listeners)) if (type === undefined || type === name) attach()
  }
  for (const attach of Object.values(listeners)) attach()
  return {
    /** Stops tracking: its listeners off, `removeAllListeners` as it was. */
    dispose: () => {
      events.removeAllListeners = original
      server.events.removeListener('request:start', started)
      server.events.removeListener('request:end', ended)
      server.events.removeListener('unhandledException', ended)
    },
    /** How many requests are open now. */
    open: () => open.size,
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

let shared: ReturnType<typeof trackInFlight> | undefined

/** The one tracker on `server` that the setup waits on after each test. */
export function sharedInFlight(server: SetupServer): ReturnType<typeof trackInFlight> {
  shared ??= trackInFlight(server)
  return shared
}
