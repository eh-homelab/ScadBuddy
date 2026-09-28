import { describe, expect, it } from 'vitest'
import { fetchAiAvailability } from './availability'

function answer(body: unknown, init: ResponseInit = {}, type = 'application/json'): typeof fetch {
  return () =>
    Promise.resolve(
      new Response(typeof body === 'string' ? body : JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': type },
        ...init,
      }),
    )
}

describe('fetchAiAvailability', () => {
  it('is configured when the agent says it is available', async () => {
    expect(await fetchAiAvailability(answer({ available: true, state: 'enabled', ai: 'enabled' }))).toEqual({
      available: true,
      state: 'configured',
    })
  })

  it('is not configured, with the agent’s reason, when a setup step is missing', async () => {
    const body = {
      available: false,
      state: 'disabled',
      ai: 'disabled (no Claude credential)',
      reason: 'No Claude credential is configured yet.',
    }
    expect(await fetchAiAvailability(answer(body))).toEqual({
      available: false,
      state: 'not_configured',
      reason: 'No Claude credential is configured yet.',
    })
  })

  it('is unavailable when the agent answers but cannot serve', async () => {
    const body = { available: false, state: 'unavailable', ai: 'unavailable (database unreachable)' }
    expect(await fetchAiAvailability(answer(body))).toEqual({
      available: false,
      state: 'unavailable',
      reason: 'unavailable (database unreachable)',
    })
  })

  it('is unreachable when nothing answers, the path is not routed, or the SPA answers instead', async () => {
    const down: typeof fetch = () => Promise.reject(new TypeError('Failed to fetch'))
    expect(await fetchAiAvailability(down)).toMatchObject({
      state: 'unreachable',
      reason: 'Could not reach the agent service (Failed to fetch).',
    })
    expect(await fetchAiAvailability(answer({ detail: 'Not Found' }, { status: 404 }))).toMatchObject({
      state: 'unreachable',
      reason: expect.stringMatching(/ingress routes \/api\/v1\/ai/),
    })
    expect(await fetchAiAvailability(answer('<!doctype html><title>ScadBuddy</title>', {}, 'text/html'))).toMatchObject({
      state: 'unreachable',
      reason: expect.stringMatching(/ingress/),
    })
    expect(await fetchAiAvailability(answer({ something: 'else' }))).toMatchObject({ state: 'unreachable' })
    expect(await fetchAiAvailability(answer({}, { status: 502 }))).toMatchObject({
      state: 'unreachable',
      reason: 'The agent service answered HTTP 502.',
    })
  })
})
