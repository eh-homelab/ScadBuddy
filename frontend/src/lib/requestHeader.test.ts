import { describe, expect, it, vi } from 'vitest'
import { REQUEST_HEADER, needsHeader, withRequestHeader } from './requestHeader'

// #4975 — the CSRF header on every request to the page's own API (spec 2026-11-01 §5.3).

const ORIGIN = window.location.origin

function sent(call: unknown[]): Headers {
  const [input, init] = call as [RequestInfo | URL, RequestInit | undefined]
  if (input instanceof Request && init === undefined) return input.headers
  return new Headers(init?.headers)
}

describe('needsHeader', () => {
  it('is the page’s own API and trace relay only', () => {
    expect(needsHeader('/api/v1/models')).toBe(true)
    expect(needsHeader(`${ORIGIN}/telemetry/v1/traces`)).toBe(true)
    expect(needsHeader('/assets/index.js')).toBe(false)
    expect(needsHeader('https://idp.example/api/v1/x')).toBe(false)
  })
})

describe('withRequestHeader', () => {
  it('adds the header to a path, keeping the caller’s own headers', async () => {
    const fetchImpl = vi.fn(async () => new Response(null))
    await withRequestHeader(fetchImpl)('/api/v1/models', { headers: { Accept: 'application/json' } })
    const headers = sent(fetchImpl.mock.calls[0]!)
    expect(headers.get(REQUEST_HEADER)).toBe('1')
    expect(headers.get('accept')).toBe('application/json')
  })

  it('adds it to a Request', async () => {
    const fetchImpl = vi.fn(async () => new Response(null))
    await withRequestHeader(fetchImpl)(new Request(`${ORIGIN}/api/v1/jobs`))
    expect(sent(fetchImpl.mock.calls[0]!).get(REQUEST_HEADER)).toBe('1')
  })

  it('leaves another origin alone', async () => {
    const fetchImpl = vi.fn(async () => new Response(null))
    const init = { headers: { Accept: 'text/plain' } }
    await withRequestHeader(fetchImpl)('https://other.example/api/v1/x', init)
    expect(fetchImpl).toHaveBeenCalledWith('https://other.example/api/v1/x', init)
  })
})
