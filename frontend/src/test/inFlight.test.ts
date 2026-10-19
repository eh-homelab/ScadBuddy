import { HttpResponse, http } from 'msw'
import { describe, expect, it, onTestFinished } from 'vitest'
import { server } from '../mocks/server'
import { trackInFlight } from './inFlight'

/** A GET to `path` that MSW answers only once `answer` is called. */
function held(path: string) {
  const state = { entered: false, answer: () => {} }
  server.use(
    http.get(path, async () => {
      state.entered = true
      await new Promise<void>((resolve) => (state.answer = resolve))
      return HttpResponse.json({})
    }),
  )
  return state
}

describe('trackInFlight (#3243)', () => {
  it('names a request still open, once, and none once it is answered', async () => {
    const tracked = trackInFlight(server)
    onTestFinished(tracked.dispose)
    const never = held('/api/v1/never')
    const controller = new AbortController()
    void fetch('/api/v1/never', { signal: controller.signal }).catch(() => {})
    await expect.poll(() => never.entered).toBe(true)
    expect(await tracked.drained(20)).toEqual(['GET /api/v1/never'])
    // Forgotten once named: the next test is not blamed for it.
    expect(await tracked.drained(0)).toEqual([])
    controller.abort()
    never.answer()

    const later = held('/api/v1/later')
    const read = fetch('/api/v1/later')
    await expect.poll(() => later.entered).toBe(true)
    later.answer()
    expect(await tracked.drained(500)).toEqual([])
    await read
  })

  it('lets go of a request once it is aborted', async () => {
    const tracked = trackInFlight(server)
    onTestFinished(tracked.dispose)
    const never = held('/api/v1/never')
    const controller = new AbortController()
    void fetch('/api/v1/never', { signal: controller.signal }).catch(() => {})
    await expect.poll(() => never.entered).toBe(true)
    controller.abort()
    expect(await tracked.drained(500)).toEqual([])
    never.answer()
  })

  it.each([undefined, 'request:start', 'request:end'] as const)(
    'still hears requests after a test removes its listeners (%s)',
    async (type) => {
      const tracked = trackInFlight(server)
      onTestFinished(tracked.dispose)
      server.events.removeAllListeners(type)
      const never = held('/api/v1/never')
      const controller = new AbortController()
      void fetch('/api/v1/never', { signal: controller.signal }).catch(() => {})
      await expect.poll(() => never.entered).toBe(true)
      expect(tracked.open()).toBe(1)
      expect(await tracked.drained(0)).toEqual(['GET /api/v1/never'])
      controller.abort()
      never.answer()

      const later = held('/api/v1/later')
      const read = fetch('/api/v1/later')
      await expect.poll(() => later.entered).toBe(true)
      later.answer()
      await read
      expect(await tracked.drained(500)).toEqual([])
    },
  )
})
