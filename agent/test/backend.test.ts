import { describe, expect, it } from 'vitest'
import { backendReachable, createBackendClient } from '../src/api/backend.js'

describe('backend client', () => {
  it('calls the backend health route under the configured base URL', async () => {
    const seen: string[] = []
    const client = createBackendClient('http://127.0.0.1:8080', async (input) => {
      seen.push(input instanceof Request ? input.url : String(input))
      return Response.json({ status: 'ok' })
    })
    expect(await backendReachable(client)).toBe(true)
    expect(seen).toEqual(['http://127.0.0.1:8080/healthz'])
  })

  it('is unreachable on a non-2xx answer or a network error', async () => {
    const failing = createBackendClient('http://b', async () => new Response('no', { status: 503 }))
    expect(await backendReachable(failing)).toBe(false)
    const throwing = createBackendClient('http://b', async () => {
      throw new TypeError('fetch failed')
    })
    expect(await backendReachable(throwing)).toBe(false)
  })
})
