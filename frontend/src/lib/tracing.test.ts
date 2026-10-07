import { trace } from '@opentelemetry/api'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, printRunPoll } from '../api/client'
import { queuedResult } from '../mocks/choices'
import { server } from '../mocks/server'
import { RELAY_PATH } from './relayExporter'
import { TRACER_NAME, messageTraceparent, traceAction } from './traceAction'
import { startTracing } from './tracing'

const TRACEPARENT = /^00-[0-9a-f]{32}-[0-9a-f]{16}-01$/
/** Spec §6: a request outside an action is a parentless CLIENT span, propagated unsampled. */
const UNSAMPLED = /^00-[0-9a-f]{32}-[0-9a-f]{16}-00$/
const CROSS_ORIGIN = 'https://fonts.googleapis.com/css2'

/** The `traceparent` each request to `url` arrived with (null: none). */
function capture(url: string): (string | null)[] {
  const seen: (string | null)[] = []
  server.use(
    http.get(url, ({ request }) => {
      seen.push(request.headers.get('traceparent'))
      return HttpResponse.json({})
    }),
  )
  return seen
}

describe('startTracing', () => {
  let stop: (() => Promise<void>) | undefined
  afterEach(async () => {
    await stop?.()
    stop = undefined
  })

  it('injects traceparent into a same-origin request', async () => {
    const seen = capture('/api/v1/models')
    stop = startTracing()
    await traceAction('generate', {}, () => fetch('/api/v1/models'))
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatch(TRACEPARENT)
  })

  it('sends a request outside any action (a poll) unsampled, so the backend drops it too', async () => {
    const seen = capture('/api/v1/jobs/x')
    stop = startTracing()
    await fetch('/api/v1/jobs/x')
    expect(seen).toHaveLength(1)
    expect(seen[0]).toMatch(UNSAMPLED)
  })

  it('never injects traceparent into a cross-origin request', async () => {
    const foreign = capture(CROSS_ORIGIN)
    const own = capture('/api/v1/models')
    stop = startTracing()
    await traceAction('generate', {}, async (within) => {
      await within(() => fetch(CROSS_ORIGIN))
      await within(() => fetch('/api/v1/models'))
    })
    expect(foreign).toEqual([null])
    // The same page, the same moment, its own origin: injected, so the absence is the origin's doing.
    expect(own[0]).toMatch(TRACEPARENT)
  })

  it('parents an action’s first request on the action’s span', async () => {
    const seen = capture('/api/v1/outputs')
    stop = startTracing()
    let actionTrace: string | undefined
    await traceAction('generate', {}, async () => {
      actionTrace = trace.getActiveSpan()?.spanContext().traceId
      await fetch('/api/v1/outputs')
    })
    expect(seen[0]?.split('-')[1]).toBe(actionTrace)
  })

  it('parents the real createOutput write on the generate action', async () => {
    const seen: (string | null)[] = []
    server.use(
      http.post('/api/v1/models/box/outputs', ({ request }) => {
        seen.push(request.headers.get('traceparent'))
        return HttpResponse.json({})
      }),
    )
    stop = startTracing()
    let actionTrace: string | undefined
    await traceAction('generate', {}, () => {
      actionTrace = trace.getActiveSpan()?.spanContext().traceId
      return api.createOutput('box', 'job-1')
    })
    expect(seen[0]).toMatch(TRACEPARENT)
    expect(seen[0]?.split('-')[1]).toBe(actionTrace)
  })

  it('parents the real runPrint POST, and its retry, on the print action', async () => {
    const seen: (string | null)[] = []
    server.use(
      http.post('/api/v1/print/outputs/out1/run', ({ request }) => {
        seen.push(request.headers.get('traceparent'))
        return seen.length === 1
          ? new HttpResponse(null, { status: 502 })
          : HttpResponse.json({ id: 'r1', status: 'succeeded', result: queuedResult })
      }),
    )
    stop = startTracing()
    const saved = printRunPoll.intervalMs
    printRunPoll.intervalMs = 1
    let actionTrace: string | undefined
    try {
      await traceAction('print', {}, (within) => {
        actionTrace = trace.getActiveSpan()?.spanContext().traceId
        return api.runPrint('out1', {} as never, undefined, within)
      })
    } finally {
      printRunPoll.intervalMs = saved
    }
    expect(seen).toHaveLength(2)
    for (const header of seen) {
      expect(header).toMatch(TRACEPARENT)
      expect(header?.split('-')[1]).toBe(actionTrace)
    }
  })

  it('carries no baggage header', async () => {
    let baggage: string | null = 'unset'
    server.use(
      http.get('/api/v1/models', ({ request }) => {
        baggage = request.headers.get('baggage')
        return HttpResponse.json([])
      }),
    )
    stop = startTracing()
    await fetch('/api/v1/models')
    expect(baggage).toBeNull()
  })

  it('is started once however often it is called', () => {
    stop = startTracing()
    expect(startTracing()).toBe(stop)
  })

  it('stops tracing for good once the relay answers off', async () => {
    const seen = capture('/api/v1/models')
    stop = startTracing()
    // msw's default relay answers off.
    trace.getTracer(TRACER_NAME).startSpan('before-off').end()
    window.dispatchEvent(new Event('pagehide'))
    await vi.waitFor(async () => {
      await fetch('/api/v1/models')
      expect(seen.at(-1)).toBeNull()
    })
    await traceAction('assistant.message', {}, async () => {
      expect(messageTraceparent()).toBeUndefined()
    })
    expect(messageTraceparent()).toBeUndefined()
  })

  describe('flushing when the page goes away', () => {
    /** Posts the relay receives, with the `traceparent` each carried. */
    function relay(): (string | null)[] {
      const posts: (string | null)[] = []
      server.use(
        http.post(RELAY_PATH, ({ request }) => {
          posts.push(request.headers.get('traceparent'))
          return new HttpResponse(null, { status: 204 })
        }),
      )
      return posts
    }
    const endSpan = () => trace.getTracer(TRACER_NAME).startSpan('hidden-test').end()
    const hide = () => {
      vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      document.dispatchEvent(new Event('visibilitychange'))
    }

    afterEach(() => vi.restoreAllMocks())

    it('flushes on visibilitychange to hidden, with no traceparent on the relay post', async () => {
      const posts = relay()
      stop = startTracing()
      endSpan()
      hide()
      await vi.waitFor(() => expect(posts).toHaveLength(1))
      expect(posts[0]).toBeNull()
    })

    it('does not flush on visibilitychange to visible', async () => {
      const posts = relay()
      stop = startTracing()
      endSpan()
      document.dispatchEvent(new Event('visibilitychange'))
      await new Promise((r) => setTimeout(r, 100))
      expect(posts).toHaveLength(0)
    })

    it('flushes on pagehide', async () => {
      const posts = relay()
      stop = startTracing()
      endSpan()
      window.dispatchEvent(new Event('pagehide'))
      await vi.waitFor(() => expect(posts).toHaveLength(1))
    })

    it('stops listening once undone', async () => {
      const posts = relay()
      const undo = startTracing()
      await undo()
      stop = undefined
      hide()
      window.dispatchEvent(new Event('pagehide'))
      await new Promise((r) => setTimeout(r, 100))
      expect(posts).toHaveLength(0)
    })
  })
})
