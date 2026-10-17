/**
 * MSW intercepts at the socket, and a request can slip past it to the real network: one
 * on a kept-alive socket, or the one after an aborted request (mswjs/interceptors#862,
 * still in 0.45.8). Nothing listens there, so the page shows "could not reach its
 * server" (#3936, #3935). It is a race and shows under load. The connection was refused,
 * so nothing was sent: `resending` sends it again, up to `tries` times, and MSW sees it.
 *
 * It writes to `log` (stderr in the setup, rather than console.warn, which tests spy
 * on) on the first resend and on giving up after every try was refused: a line means the
 * upstream escape still happens, and this wrapper is still needed. A resend that ends
 * any other way (answered, failed otherwise, aborted) logs only its first line. With
 * `tries` 1 there is no resend, so the only line is the one for giving up.
 */
// Taken at load, before a test fakes the timers: a faked one would never fire.
const realSetTimeout = globalThis.setTimeout

export function resending(
  intercepted: typeof fetch,
  log: (line: string) => void,
  tries = 3,
): typeof fetch {
  return async (input, init) => {
    // As fetch does: init.signal overrides the Request's own, an explicit null included;
    // an undefined member is absent (WebIDL), so the Request's own stands.
    const signal = init?.signal !== undefined ? init.signal : input instanceof Request ? input.signal : undefined
    const url = input instanceof Request ? input.url : String(input)
    for (let attempt = 1; ; attempt++) {
      try {
        // A clone each try, so a refused try's consumed body leaves the next one its own.
        return await intercepted(input instanceof Request ? input.clone() : input, init)
      } catch (error) {
        const refused = (error as { cause?: { code?: string } }).cause?.code === 'ECONNREFUSED'
        if (!refused || signal?.aborted) throw error
        if (attempt >= tries) {
          log(`vitest.setup: gave up on ${url} after ${tries} refused tries (mswjs/interceptors#862)`)
          throw error
        }
        if (attempt === 1) log(`vitest.setup: resending ${url}, which MSW let through (mswjs/interceptors#862)`)
        // A task's pause, so the socket that let it through can settle before the resend.
        await new Promise((resolve) => realSetTimeout(resolve, 0))
        if (signal?.aborted) throw error
      }
    }
  }
}
