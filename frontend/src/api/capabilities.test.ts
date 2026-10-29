import { describe, expect, it } from 'vitest'
import { server } from '../mocks/server'
import { aiCapabilities } from './capabilities'
import { ApiError } from './client'

type Sent = { method: string; path: string; body: string }

/** Every request the client sends while `run` runs, as it left the client. */
async function sent(run: () => Promise<unknown>): Promise<Sent[]> {
  const requests: Promise<Sent>[] = []
  const listener = ({ request }: { request: Request }) => {
    const copy = request.clone()
    requests.push(copy.text().then((body) => ({ method: copy.method, path: new URL(copy.url).pathname, body })))
  }
  server.events.on('request:start', listener)
  try {
    await run()
  } finally {
    // Only this test's: another listener on the shared server stays (#3457).
    server.events.removeListener('request:start', listener)
  }
  return Promise.all(requests)
}

const BASE = '/api/v1/ai/capabilities'

describe('aiCapabilities', () => {
  it('sends each write as the agent expects', async () => {
    await aiCapabilities.refresh('remote:hindsight')

    expect(await sent(() => aiCapabilities.setItem('remote:hindsight', 'files.list', 'off'))).toEqual([
      { method: 'PUT', path: `${BASE}/remote%3Ahindsight/items/files.list`, body: '{"access":"off"}' },
    ])
    expect(await sent(() => aiCapabilities.resetItem('remote:hindsight', 'files.list'))).toEqual([
      { method: 'DELETE', path: `${BASE}/remote%3Ahindsight/items/files.list`, body: '' },
    ])
    expect(await sent(() => aiCapabilities.setEnabled('builtin:http', false))).toEqual([
      { method: 'PUT', path: `${BASE}/builtin%3Ahttp`, body: '{"enabled":false}' },
    ])
    expect(await sent(() => aiCapabilities.refresh('remote:hindsight'))).toEqual([
      { method: 'POST', path: `${BASE}/remote%3Ahindsight/refresh`, body: '' },
    ])
    expect(await sent(() => aiCapabilities.details('remote:hindsight', 'a/b').catch(() => undefined))).toEqual([
      expect.objectContaining({ method: 'GET', path: `${BASE}/remote%3Ahindsight/items/a%2Fb` }),
    ])
  })

  it('answers an ApiError with the agent\'s detail on 409', async () => {
    const error = await aiCapabilities.setEnabled('builtin:scadbuddy', false).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(409)
    expect((error as ApiError).detail).toBe('"builtin:scadbuddy" cannot be switched off or on')
  })
})
