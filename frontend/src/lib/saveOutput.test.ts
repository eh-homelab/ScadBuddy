import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import type { Job } from '../api/types'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { fakeRealtime } from './realtime.fake'
import { ExtraOutputsError, saveOutput, saveRemaining } from './saveOutput'

const summary = (index: number) => ({ index, name: `out ${index}`, bom: [], files: [] })

function aJob(id: string, outputs: number): Job {
  return {
    id,
    slug: 'demo',
    status: 'done',
    created_at: '2026-09-30T00:00:00Z',
    params: { w: 1 },
    inputs: { params: { w: 1 }, v: 1, house: { cols: 1 } },
    outputs: Array.from({ length: outputs }, (_, index) => summary(index)),
  }
}

function recordOutputs(refuse?: (body: Record<string, unknown>) => boolean, status = 422) {
  const bodies: Record<string, unknown>[] = []
  server.use(
    http.post('/api/v1/models/demo/outputs', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      bodies.push(body)
      if (refuse?.(body)) {
        return HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: `inputs are not the ones job ${String(body.job_id)} rendered` },
          { status, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }
      return HttpResponse.json({ id: `o-${bodies.length}`, slug: 'demo', job_id: body.job_id }, { status: 201 })
    }),
  )
  return bodies
}

/** The job moves on, as a real one does, and says so over the socket (#1909). */
function announceLater(jobId: string) {
  setTimeout(() => emitRealtime('job.running', [`job:${jobId}`], { job_id: jobId, slug: 'demo' }), 20)
}

describe('saveOutput', () => {
  it('saves every output of a pipeline job and returns the first', async () => {
    const bodies = recordOutputs()
    const created = await saveOutput({
      slug: 'demo',
      job: aJob('j1', 3),
      extra: { v: 1, house: { cols: 1 } },
      capture: async () => null,
    })
    expect(created.id).toBe('o-1')
    expect(bodies.map((b) => b.index)).toEqual([undefined, 1, 2])
    expect(bodies.every((b) => b.job_id === 'j1')).toBe(true)
  })

  it('renders the inputs first when the job did not render them (a UI-state-only change)', async () => {
    const bodies = recordOutputs((body) => body.job_id === 'j1')
    const renders: unknown[] = []
    let reads = 0
    server.use(
      http.post('/api/v1/models/demo/render', async ({ request }) => {
        renders.push(await request.json())
        return HttpResponse.json({ job_id: 'j2', status_url: '/api/v1/jobs/j2' }, { status: 202 })
      }),
      http.get('/api/v1/jobs/j2', () => {
        reads += 1
        if (reads >= 2) return HttpResponse.json(aJob('j2', 2))
        announceLater('j2')
        return HttpResponse.json({ ...aJob('j2', 2), status: 'running' })
      }),
    )
    const created = await saveOutput({
      slug: 'demo',
      job: aJob('j1', 1),
      extra: { v: 1, house: { cols: 2 } },
      capture: async () => null,
    })
    const inputs = { v: 1, house: { cols: 2 }, params: { w: 1 } }
    expect(renders).toEqual([{ inputs, version: null }])
    expect(bodies.map((b) => [b.job_id, b.index])).toEqual([
      ['j1', undefined],
      ['j2', undefined],
      ['j2', 1],
    ])
    expect(bodies.every((b) => JSON.stringify(b.inputs) === JSON.stringify(inputs))).toBe(true)
    expect(created.job_id).toBe('j2')
  })

  /** j1 cannot be saved with new inputs; j2, their render, runs until `finish`. */
  function renderUntilFinished() {
    recordOutputs((body) => body.job_id === 'j1')
    let done = false
    let reads = 0
    server.use(
      http.post('/api/v1/models/demo/render', () =>
        HttpResponse.json({ job_id: 'j2', status_url: '/api/v1/jobs/j2' }, { status: 202 }),
      ),
      http.get('/api/v1/jobs/j2', () => {
        reads += 1
        return HttpResponse.json(done ? aJob('j2', 1) : { ...aJob('j2', 1), status: 'running' })
      }),
    )
    return {
      reads: () => reads,
      finish() {
        done = true
      },
    }
  }

  const save = (signal?: AbortSignal) =>
    saveOutput({ slug: 'demo', job: aJob('j1', 1), extra: { v: 1, house: { cols: 2 } }, capture: async () => null, signal })

  it('reads the render on each event for it, not on a timer (#1909)', async () => {
    const render = renderUntilFinished()
    const saving = save()
    await vi.waitFor(() => expect(render.reads()).toBe(1))
    await new Promise((resolve) => setTimeout(resolve, 1200))
    expect(render.reads()).toBe(1)
    render.finish()
    emitRealtime('job.done', ['job:j2'], { job_id: 'j2', slug: 'demo' })
    expect((await saving).job_id).toBe('j2')
    expect(render.reads()).toBe(2)
  })

  it('reads the render on a timer only while the socket is unavailable (#1909)', async () => {
    const realtime = fakeRealtime({ confirm: false })
    const render = renderUntilFinished()
    const saving = save()
    await new Promise((resolve) => setTimeout(resolve, 700))
    expect(render.reads()).toBe(0)
    realtime.setStatus('unavailable')
    await vi.waitFor(() => expect(render.reads()).toBeGreaterThanOrEqual(2))
    render.finish()
    expect((await saving).job_id).toBe('j2')
  })

  it('leaves no abort listener behind on the signal once its wait is done', async () => {
    recordOutputs((body) => body.job_id === 'j1')
    let reads = 0
    server.use(
      http.post('/api/v1/models/demo/render', () =>
        HttpResponse.json({ job_id: 'j2', status_url: '/api/v1/jobs/j2' }, { status: 202 }),
      ),
      http.get('/api/v1/jobs/j2', () => {
        reads += 1
        if (reads >= 4) return HttpResponse.json(aJob('j2', 1))
        announceLater('j2')
        return HttpResponse.json({ ...aJob('j2', 1), status: 'running' })
      }),
    )
    const controller = new AbortController()
    const added = vi.spyOn(controller.signal, 'addEventListener')
    const removed = vi.spyOn(controller.signal, 'removeEventListener')
    await saveOutput({
      slug: 'demo',
      job: aJob('j1', 1),
      extra: { v: 1 },
      capture: async () => null,
      signal: controller.signal,
    })
    expect(added.mock.calls.length).toBeGreaterThanOrEqual(1)
    expect(removed.mock.calls.length).toBe(added.mock.calls.length)
  })

  it('passes any other refusal through', async () => {
    server.use(
      http.post('/api/v1/models/demo/outputs', () =>
        HttpResponse.json(
          { title: 'Conflict', status: 409, detail: 'Job not finished' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    await expect(
      saveOutput({ slug: 'demo', job: aJob('j1', 1), extra: {}, capture: async () => null }),
    ).rejects.toThrow('Job not finished')
  })

  it('reports the first output as soon as it is saved, and the extras failing on their own', async () => {
    let refuseExtras = true
    const bodies = recordOutputs((body) => refuseExtras && body.index === 1, 500)
    const shown: string[] = []
    const failure = await saveOutput({
      slug: 'demo',
      job: aJob('j1', 3),
      extra: { v: 1, house: { cols: 1 } },
      capture: async () => null,
      onSaved: (output) => shown.push(output.id),
    }).catch((caught: unknown) => caught)
    expect(shown).toEqual(['o-1'])
    expect(failure).toBeInstanceOf(ExtraOutputsError)
    expect((failure as ExtraOutputsError).saved.id).toBe('o-1')
    expect((failure as Error).message).toMatch(/^Saved the first output; outputs 2 to 3 could not be saved/)
    // A retry saves only what is missing: never output 0 again.
    refuseExtras = false
    await saveRemaining(failure as ExtraOutputsError)
    expect(bodies.map((b) => b.index)).toEqual([undefined, 1, 1, 2])
  })

  function renderThatNeverEnds() {
    recordOutputs((body) => body.job_id === 'j1')
    let reads = 0
    server.use(
      http.post('/api/v1/models/demo/render', () =>
        HttpResponse.json({ job_id: 'j2', status_url: '/api/v1/jobs/j2' }, { status: 202 }),
      ),
      http.get('/api/v1/jobs/j2', () => {
        reads += 1
        return HttpResponse.json({ ...aJob('j2', 1), status: 'running' })
      }),
    )
    return () => reads
  }

  it('stops reading the render once the caller aborts', async () => {
    const reads = renderThatNeverEnds()
    const controller = new AbortController()
    const saving = saveOutput({
      slug: 'demo',
      job: aJob('j1', 1),
      extra: { v: 1, house: { cols: 2 } },
      capture: async () => null,
      signal: controller.signal,
    })
    await vi.waitFor(() => expect(reads()).toBeGreaterThan(0))
    controller.abort()
    await expect(saving).rejects.toThrow()
    const after = reads()
    await new Promise((resolve) => setTimeout(resolve, 1200))
    expect(reads()).toBe(after)
  })

  it('gives up on a render that does not finish in time', async () => {
    renderThatNeverEnds()
    await expect(
      saveOutput({
        slug: 'demo',
        job: aJob('j1', 1),
        extra: { v: 1, house: { cols: 2 } },
        capture: async () => null,
        renderWaitMs: 600,
      }),
    ).rejects.toThrow(/did not finish within/)
  })
})
