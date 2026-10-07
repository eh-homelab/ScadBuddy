import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Output } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { resetMockState } from '../mocks/handlers'
import { server } from '../mocks/server'
import { backfillFailures, backfillOutputs, listNames } from './arrange'

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

describe('listNames', () => {
  it('names no outputs as nothing', () => {
    expect(listNames([])).toBe('')
  })
})
