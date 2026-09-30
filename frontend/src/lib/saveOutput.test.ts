import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { Job } from '../api/types'
import { server } from '../mocks/server'
import { saveOutput } from './saveOutput'

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

function recordOutputs(refuse?: (body: Record<string, unknown>) => boolean) {
  const bodies: Record<string, unknown>[] = []
  server.use(
    http.post('/api/v1/models/demo/outputs', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>
      bodies.push(body)
      if (refuse?.(body)) {
        return HttpResponse.json(
          { title: 'Unprocessable Content', status: 422, detail: `inputs are not the ones job ${String(body.job_id)} rendered` },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }
      return HttpResponse.json({ id: `o-${bodies.length}`, slug: 'demo', job_id: body.job_id }, { status: 201 })
    }),
  )
  return bodies
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
        return HttpResponse.json(reads < 2 ? { ...aJob('j2', 2), status: 'running' } : aJob('j2', 2))
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
})
