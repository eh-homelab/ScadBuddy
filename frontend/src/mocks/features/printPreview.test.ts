import { http } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { PrintRunRequest } from '../../api/types'
import { resetMockState } from '../handlers'
import { server } from '../server'

beforeEach(() => resetMockState())

describe('preview-slice mock (#2824)', () => {
  it('answers 202 with its operation, as the backend does, and the client follows it', async () => {
    const statuses: number[] = []
    const followed: string[] = []
    const listener = ({ request, response }: { request: Request; response: Response }) => {
      const path = new URL(request.url).pathname
      if (path.endsWith('/preview-slice')) statuses.push(response.status)
      if (path.startsWith('/api/v1/operations/')) followed.push(`${response.status} ${path}`)
    }
    server.events.on('response:mocked', listener)
    try {
      const started = await api.previewSlice('out-1', { plate_id: 2 } as PrintRunRequest)

      expect(started).toMatchObject({ plate_id: 2 })
      expect(started.job_id).toEqual(expect.any(Number))
      expect(statuses).toEqual([202])
      expect(followed).toEqual([expect.stringMatching(/^200 \/api\/v1\/operations\//)])
    } finally {
      // Only this test's: another listener on the shared server stays (#3457).
      server.events.removeListener('response:mocked', listener)
    }
  })

  it('answers 404 for an operation it never recorded', async () => {
    const response = await fetch(new URL('/api/v1/operations/nope', window.location.href))
    expect(response.status).toBe(404)
  })

  it('forgets its operations on reset', async () => {
    let location = ''
    server.use(
      http.get('/api/v1/operations/:id', ({ request }) => {
        location = new URL(request.url).pathname
        return undefined
      }),
    )
    await api.previewSlice('out-1', {} as PrintRunRequest)
    resetMockState()
    expect((await fetch(new URL(location, window.location.href))).status).toBe(404)
  })
})
