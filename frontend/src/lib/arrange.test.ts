import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { backfillFailures, backfillOutputs, listNames, MAX_REASON_CHARS, runArrange } from './arrange'
import { getRealtime } from './realtime'
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

describe('backfillOutputs (#902)', () => {
  it('follows each re-render over the socket, not on a timer (#1909)', async () => {
    // The fake client, so the mock render's own events do not reach this wait.
    fakeRealtime()
    const jobs = jobsRunUntilFinished()
    const backfilling = backfillOutputs([nova], { pollMs: 20 })
    await vi.waitFor(() => expect(jobs.reads).toHaveLength(1))
    await idle(300)
    expect(jobs.reads).toHaveLength(1)
    jobs.finish()
    const [topic, listener] = vi.mocked(getRealtime().subscribe).mock.calls[0]!
    expect(topic).toBe(`job:${jobs.reads[0]!}`)
    listener({ id: 'e', kind: 'job.done', topics: [topic], data: {} })
    await backfilling
    expect(jobs.reads).toHaveLength(2)
  })

  it('reads the output again while the finished re-render is not yet attached', async () => {
    const reads = vi.spyOn(api, 'getOutput')
    const { ready } = await backfillOutputs([nova], { pollMs: 10 })
    expect(ready[0]?.manifest).toHaveLength(1)
    // The first read after the job is done still shows the marker; the next has the objects.
    expect(reads.mock.calls.length).toBeGreaterThanOrEqual(2)
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

describe('listNames', () => {
  it('names no outputs as nothing', () => {
    expect(listNames([])).toBe('')
  })
})
