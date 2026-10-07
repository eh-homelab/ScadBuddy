import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { backfillFailures, backfillOutputs, listNames, MAX_REASON_CHARS } from './arrange'

const nova = fixtures.outputs[1] as Output

beforeEach(() => resetMockState())
afterEach(() => vi.restoreAllMocks())

describe('backfillOutputs (#902)', () => {
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
