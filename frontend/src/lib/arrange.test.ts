import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, printRunPoll, STILL_ACCEPTING } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import {
  arrangedSources,
  backfillFailures,
  backfillOutputs,
  listNames,
  MAX_REASON_CHARS,
  runArrange,
  savedNote,
} from './arrange'
import { getRealtime } from './realtime'
import { followUntil } from './waitForJob'
import { fakeRealtime } from './realtime.fake'

const nova = fixtures.outputs[1] as Output
const first = fixtures.outputs[0] as Output
const body: ArrangeRequest = {
  slug: 'name-keychain',
  goal: 'fewest_plates',
  objects: [{ output_id: first.id, part: 'piece-wall', count: 2 }],
}

/** Every job reads `running` until `finish`, which settles it as done. */
function jobsRunUntilFinished() {
  let done = false
  const reads: string[] = []
  server.use(
    http.get('/api/v1/jobs/:id', ({ params }) => {
      reads.push(String(params['id']))
      return HttpResponse.json({
        id: String(params['id']),
        slug: 'name-keychain',
        status: done ? 'done' : 'running',
        created_at: '2026-09-28T12:00:00Z',
        plates: [],
      })
    }),
  )
  return {
    reads,
    finish() {
      done = true
    },
  }
}

const idle = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

/** An event on `topic` to its latest follower (`fakeRealtime`), on real timers. */
function send(topic: string, kind: string, data: Record<string, unknown>) {
  const call = vi.mocked(getRealtime().subscribe).mock.calls.findLast(([t]) => t === topic)
  call?.[1]({ id: 'e', kind, topics: [topic], data })
}

describe('runArrange follows its job over the socket (#1909)', () => {
  it('reads the job on each event for it, not on a timer', async () => {
    const jobs = jobsRunUntilFinished()
    const arranging = runArrange('name-keychain', body, { pollMs: 20 })
    await vi.waitFor(() => expect(jobs.reads).toHaveLength(1))
    await idle(300)
    expect(jobs.reads).toHaveLength(1)
    jobs.finish()
    emitRealtime('job.done', [`job:${jobs.reads[0]!}`], { job_id: jobs.reads[0], slug: 'name-keychain' })
    const { output, plates } = await arranging
    expect(output.slug).toBe('name-keychain')
    expect(plates).toBe(1)
    expect(jobs.reads).toHaveLength(2)
  })

  it('reads the job on a timer only while the socket is unavailable', async () => {
    const realtime = fakeRealtime({ confirm: false })
    const jobs = jobsRunUntilFinished()
    const arranging = runArrange('name-keychain', body, { pollMs: 20 })
    await idle(150)
    expect(jobs.reads).toHaveLength(0)
    realtime.setStatus('unavailable')
    await vi.waitFor(() => expect(jobs.reads.length).toBeGreaterThanOrEqual(2))
    jobs.finish()
    await expect(arranging).resolves.toMatchObject({ plates: 1 })
  })

  it('stops following the job once the caller aborts', async () => {
    const realtime = fakeRealtime()
    const jobs = jobsRunUntilFinished()
    const controller = new AbortController()
    const arranging = runArrange('name-keychain', body, { pollMs: 20, signal: controller.signal })
    await vi.waitFor(() => expect(jobs.reads).toHaveLength(1))
    controller.abort()
    await expect(arranging).rejects.toThrow()
    expect(realtime.following()).toEqual([])
  })
})

beforeEach(() => resetMockState())
afterEach(() => vi.restoreAllMocks())

describe('arrangeOutputs (#2280)', () => {
  it('sends an arrange still being accepted again with the same key', async () => {
    printRunPoll.intervalMs = 1
    try {
      const keys: (string | null)[] = []
      server.use(
        http.post('/api/v1/outputs/arrange', ({ request }) => {
          keys.push(request.headers.get('Idempotency-Key'))
          if (keys.length > 1) return undefined
          return HttpResponse.json(
            { type: STILL_ACCEPTING, title: 'Still accepting', status: 503, detail: 'Send it again to follow it.' },
            { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }),
      )
      const job = await api.arrangeOutputs(body)
      expect(job.id).toBeTruthy()
      expect(keys).toHaveLength(2)
      expect(keys[1]).toBe(keys[0])
    } finally {
      printRunPoll.intervalMs = 1000
    }
  })
})

describe('backfillOutputs (#902)', () => {
  it('follows each re-render over the socket, not on a timer (#1909)', async () => {
    // The fake client, so the mock render's own events do not reach this wait.
    const realtime = fakeRealtime()
    const jobs = jobsRunUntilFinished()
    const backfilling = backfillOutputs([nova], { pollMs: 20 })
    await vi.waitFor(() => expect(jobs.reads).toHaveLength(1))
    await idle(300)
    expect(jobs.reads).toHaveLength(1)
    jobs.finish()
    const [topic, listener] = vi.mocked(getRealtime().subscribe).mock.calls[0]!
    expect(topic).toBe(`job:${jobs.reads[0]!}`)
    listener({ id: 'e', kind: 'job.done', topics: [topic], data: {} })
    await vi.waitFor(() => expect(realtime.following()).toEqual(['outputs']))
    send('outputs', 'output.updated', { output_id: nova.id })
    await backfilling
    expect(jobs.reads).toHaveLength(2)
  })

  it('reads the output on its output.updated, not on a timer (#1970)', async () => {
    fakeRealtime()
    const jobs = jobsRunUntilFinished()
    jobs.finish()
    // Waiting on the re-render until the test says it is attached.
    let attached = false
    let reads = 0
    const done = { ...nova, manifest: [{ part: 'p', file: 'f', slug: nova.slug, revision: null }], backfill: null }
    server.use(
      http.get('/api/v1/outputs/:id', () => {
        reads++
        return HttpResponse.json(attached ? done : { ...nova, backfill: { job_id: 'j', error: null } })
      }),
    )
    const backfilling = backfillOutputs([nova], { pollMs: 20 })
    // The subscription's confirmation reads once.
    await vi.waitFor(() => expect(reads).toBe(1))
    await idle(200)
    send('outputs', 'output.created', { output_id: 'someone-else' })
    await idle(50)
    expect(reads).toBe(1)
    attached = true
    send('outputs', 'output.updated', { output_id: nova.id })
    expect((await backfilling).ready).toEqual([done])
    expect(reads).toBe(2)
  })

  it('reads the output on a timer while the socket is unavailable (#1970)', async () => {
    const realtime = fakeRealtime({ confirm: false })
    realtime.setStatus('unavailable')
    const jobs = jobsRunUntilFinished()
    jobs.finish()
    const { ready } = await backfillOutputs([nova], { pollMs: 10 })
    expect(ready[0]?.manifest).toHaveLength(1)
  })

  it('reads the output again while the finished re-render is not yet attached', async () => {
    const reads = vi.spyOn(api, 'getOutput')
    const { ready } = await backfillOutputs([nova], { pollMs: 10 })
    expect(ready[0]?.manifest).toHaveLength(1)
    // The first read after the job is done still shows the marker; the next has the objects.
    expect(reads.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('sends a re-render still being accepted again with the same key, and follows it (#2280)', async () => {
    printRunPoll.intervalMs = 1
    try {
      const keys: (string | null)[] = []
      let answered = 0
      server.use(
        http.post('/api/v1/outputs/:id/backfill', ({ request }) => {
          keys.push(request.headers.get('Idempotency-Key'))
          if (answered++ > 0) return undefined // the default handler queues it
          return HttpResponse.json(
            { type: STILL_ACCEPTING, title: 'Still accepting', status: 503, detail: 'Send it again to follow it.' },
            { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
          )
        }),
      )
      const { ready, failed } = await backfillOutputs([nova], { pollMs: 10 })
      expect(failed).toEqual([])
      expect(ready[0]?.manifest).toHaveLength(1)
      expect(keys).toHaveLength(2)
      expect(keys[0]).toBeTruthy()
      expect(keys[1]).toBe(keys[0])
    } finally {
      printRunPoll.intervalMs = 1000
    }
  })

  // #1007: only the "already records its objects" 409 means the output is ready.
  it('reads an output another backfill already finished, but fails on any other 409', async () => {
    const done = { ...nova, manifest: [{ part: 'p', file: 'f', slug: 'name-keychain', revision: null }] } as Output
    server.use(
      http.post('/api/v1/outputs/:id/backfill', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'already records its objects', code: 'already_backfilled' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
      http.get('/api/v1/outputs/:id', () => HttpResponse.json(done)),
    )
    expect((await backfillOutputs([nova], { pollMs: 10 })).ready).toEqual([done])
    server.use(
      http.post('/api/v1/outputs/:id/backfill', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: 'something else' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    const { ready, failed } = await backfillOutputs([nova], { pollMs: 10 })
    expect(ready).toEqual([])
    expect(backfillFailures(failed)).toMatch(/could not be re-rendered: something else/)
  })

  it('stops waiting after its limit and says the re-render is still running', async () => {
    server.use(
      http.get('/api/v1/jobs/:id', ({ params }) =>
        HttpResponse.json({
          id: String(params['id']),
          slug: 'name-keychain',
          status: 'running',
          created_at: '2026-09-28T12:00:00Z',
        }),
      ),
    )
    const { ready, failed } = await backfillOutputs([nova], { pollMs: 10, waitMs: 50 })
    expect(ready).toEqual([])
    expect(backfillFailures(failed)).toBe('Nova is still re-rendering; try Arrange again later.')
  })
})

describe('followUntil reads once before its limit (#2038)', () => {
  it('a limit spent before the first read still reads once', async () => {
    fakeRealtime({ confirm: false })
    const read = vi.fn(() => Promise.resolve('ready'))
    await expect(
      followUntil('outputs', {
        read,
        done: (value) => value === 'ready',
        stillRunning: () => new Error('still running'),
        pollMs: 10_000,
        waitMs: 0,
      }),
    ).resolves.toBe('ready')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('gives up after that read when it is not done', async () => {
    fakeRealtime({ confirm: false })
    const read = vi.fn(() => Promise.resolve('pending'))
    await expect(
      followUntil('outputs', {
        read,
        done: (value) => value === 'ready',
        stillRunning: () => new Error('still running'),
        pollMs: 10_000,
        waitMs: 0,
      }),
    ).rejects.toThrow('still running')
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('stays a bound when that read never answers (#2045 review)', async () => {
    fakeRealtime({ confirm: false })
    // fetch has no timeout: a held request never settles.
    const read = vi.fn(() => new Promise<string>(() => {}))
    await expect(
      followUntil('outputs', {
        read,
        done: (value) => value === 'ready',
        stillRunning: () => new Error('still running'),
        pollMs: 20,
        waitMs: 0,
      }),
    ).rejects.toThrow('still running')
  })
})

describe('followUntil survives a failed read (#2125)', () => {
  it('tries a failed read again and resolves when the next one answers', async () => {
    fakeRealtime({ confirm: true })
    const read = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new ApiError(502, 'Bad Gateway'))
      .mockResolvedValue('ready')
    await expect(
      followUntil('outputs', {
        read,
        done: (value) => value === 'ready',
        stillRunning: () => new Error('still running'),
        pollMs: 10_000,
      }),
    ).resolves.toBe('ready')
    expect(read).toHaveBeenCalledTimes(2)
  })

  it('rejects at once on a 404: the job is gone', async () => {
    fakeRealtime({ confirm: true })
    const read = vi.fn(() => Promise.reject(new ApiError(404, 'no such job')))
    await expect(
      followUntil('outputs', {
        read,
        done: () => false,
        stillRunning: () => new Error('still running'),
        pollMs: 10_000,
      }),
    ).rejects.toThrow('no such job')
    expect(read).toHaveBeenCalledTimes(1)
  })
})

describe('backfillFailures (#1007)', () => {
  const ref = { id: 'o1', name: 'Nova' }

  it('names the button that tries again where it is shown', () => {
    const running = [{ output: ref, error: 'still re-rendering', running: true }]
    expect(backfillFailures(running)).toBe('Nova is still re-rendering; try Arrange again later.')
    expect(backfillFailures(running, 'Re-arrange')).toBe('Nova is still re-rendering; try Re-arrange again later.')
  })

  it("cuts a long server reason rather than quoting it whole", () => {
    const long = `openscad: ${'x'.repeat(5000)}`
    const said = backfillFailures([{ output: ref, error: long }])
    expect(said.startsWith('Nova could not be re-rendered: openscad: xxx')).toBe(true)
    expect(said.endsWith('….')).toBe(true)
    expect(said.length).toBeLessThan(MAX_REASON_CHARS + 50)
    expect(backfillFailures([{ output: ref, error: 'revision abc is gone.' }])).toBe(
      'Nova could not be re-rendered: revision abc is gone.',
    )
  })
})

describe('where an arrange went (#2284)', () => {
  const output = { ...(fixtures.outputs[0] as Output), slug: 'lemon', name: 'pw-arrange-test' }

  it("names the result and the model it is filed under", () => {
    expect(savedNote({ output, plates: 1, filedUnder: 'Lemon' })).toBe("Saved as pw-arrange-test in Lemon's history.")
    expect(savedNote({ output: { ...output, name: null }, plates: 1, filedUnder: 'Lemon' })).toBe(
      "Saved in Lemon's history.",
    )
  })

  it('never names the model by its slug when its name is unknown (#2541)', () => {
    expect(savedNote({ output, plates: 1 }, 'lemon')).toBe("Saved as pw-arrange-test in this model's history.")
    expect(savedNote({ output, plates: 1 }, 'lime')).toBe("Saved as pw-arrange-test in its model's history.")
    expect(savedNote({ output: { ...output, name: null }, plates: 1 })).toBe("Saved in its model's history.")
  })

  it('counts every source, library files too', () => {
    expect(arrangedSources(['a', 'b', 'c', 'library:89'])).toBe('3 outputs and 1 library file')
    expect(arrangedSources(['a'])).toBe('1 output')
    expect(arrangedSources(['library:1', 'library:2'])).toBe('2 library files')
  })
})

describe('listNames', () => {
  it('names no outputs as nothing', () => {
    expect(listNames([])).toBe('')
  })
})
