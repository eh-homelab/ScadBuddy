import { http, HttpResponse } from 'msw'
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { server } from '../../mocks/server'
import { AI_STATUS_PATH, fetchAiAvailability, recheckAiAvailability, resetAiAvailability, useAiAvailability } from './availability'

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

  it('is unavailable, with the reason, when the chat socket would refuse this page', async () => {
    const body = {
      available: false,
      state: 'enabled',
      ai: 'enabled',
      chat: 'refused',
      reason: 'The assistant must come through the HTTPS ingress. Open ScadBuddy at its public HTTPS address to use it.',
    }
    expect(await fetchAiAvailability(answer(body))).toEqual({
      available: false,
      state: 'unavailable',
      reason: body.reason,
      chat: 'refused',
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

  it('keeps the gate’s refusal when the agent cannot serve for another reason too', async () => {
    // The agent names the outage first, but this page is refused whatever happens to
    // the database: the shell must not ride this out, and the transport must say so.
    const body = {
      available: false,
      state: 'unavailable',
      ai: 'unavailable (database unreachable)',
      reason: 'The database is unreachable.',
      chat: 'refused',
    }
    expect(await fetchAiAvailability(answer(body))).toEqual({
      available: false,
      state: 'unavailable',
      reason: 'The database is unreachable.',
      chat: 'refused',
    })
    const off = {
      available: false,
      state: 'disabled',
      ai: 'disabled (no Claude credential)',
      reason: 'No Claude credential is configured yet.',
      chat: 'refused',
    }
    expect(await fetchAiAvailability(answer(off))).toEqual({
      available: false,
      state: 'not_configured',
      reason: 'No Claude credential is configured yet.',
      chat: 'refused',
    })
  })

  it('is unavailable, not "not set up", when the agent was started without its chat socket', async () => {
    const body = {
      available: false,
      state: 'enabled',
      ai: 'enabled',
      reason: 'The agent service was started without its chat socket.',
    }
    expect(await fetchAiAvailability(answer(body))).toEqual({
      available: false,
      state: 'unavailable',
      reason: 'The agent service was started without its chat socket.',
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

  it('gives up on a status read that never answers, as unreachable', async () => {
    const hang: typeof fetch = () => new Promise<Response>(() => {})
    const started = Date.now()
    expect(await fetchAiAvailability(hang, 50)).toEqual({
      available: false,
      state: 'unreachable',
      reason: 'The agent service did not answer within 0.05 s.',
    })
    expect(Date.now() - started).toBeLessThan(2000)
    // And one that honours the abort signal is aborted.
    let aborted = false
    const listening: typeof fetch = (_input, init) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          aborted = true
          reject(new DOMException('aborted', 'AbortError'))
        })
      })
    expect(await fetchAiAvailability(listening, 50)).toMatchObject({ state: 'unreachable' })
    expect(aborted).toBe(true)
  })
})

describe('recheckAiAvailability', () => {
  it('resolves with the answer it published, so the chat transport can act on it', async () => {
    server.use(
      http.get(AI_STATUS_PATH, () =>
        HttpResponse.json({ available: false, state: 'enabled', ai: 'enabled', chat: 'refused', reason: 'not here' }),
      ),
    )
    const { result } = renderHook(() => useAiAvailability())
    let answer: Awaited<ReturnType<typeof recheckAiAvailability>> | undefined
    await act(async () => {
      answer = await recheckAiAvailability()
    })
    expect(answer).toEqual({ available: false, state: 'unavailable', reason: 'not here', chat: 'refused' })
    expect(result.current).toEqual(answer)
  })

  it('with force, answers from a read started after the call, not one already in flight (#1000)', async () => {
    let answerOld: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      answerOld = resolve
    })
    server.use(
      http.get(
        AI_STATUS_PATH,
        async () => {
          await gate
          return HttpResponse.json({ available: false, state: 'disabled', ai: 'disabled', reason: 'no credential' })
        },
        { once: true },
      ),
    )
    const before = recheckAiAvailability()
    server.use(http.get(AI_STATUS_PATH, () => HttpResponse.json({ available: true, state: 'enabled', ai: 'enabled' })))
    const { result } = renderHook(() => useAiAvailability())
    await act(async () => {
      expect(await recheckAiAvailability({ force: true })).toEqual({ available: true, state: 'configured' })
    })
    answerOld()
    await before
    // The older read lands last and is not published over the fresh one.
    expect(result.current).toEqual({ available: true, state: 'configured' })
  })
})

describe('useAiAvailability on focus', () => {
  it('does not read again on focus just after a read, whatever the wall clock does (#1485)', async () => {
    resetAiAvailability()
    let reads = 0
    server.use(
      http.get(AI_STATUS_PATH, () => {
        reads += 1
        return HttpResponse.json({ available: false, state: 'disabled', ai: 'disabled', reason: 'no credential' })
      }),
    )
    renderHook(() => useAiAvailability())
    await act(() => recheckAiAvailability())
    const before = reads
    const now = Date.now
    const stepped = vi.spyOn(Date, 'now').mockImplementation(() => now.call(Date) + 3_600_000)
    try {
      await act(async () => {
        window.dispatchEvent(new Event('focus'))
      })
      expect(reads).toBe(before)
    } finally {
      stepped.mockRestore()
    }
  })
})

describe('resetAiAvailability', () => {
  it('drops a status read that was still in flight', async () => {
    let answerNow: () => void = () => {}
    const gate = new Promise<void>((resolve) => {
      answerNow = resolve
    })
    server.use(
      http.get(AI_STATUS_PATH, async () => {
        await gate
        return HttpResponse.json({ available: true, state: 'enabled', ai: 'enabled' })
      }),
    )
    const stale = recheckAiAvailability()
    resetAiAvailability({ available: false, state: 'not_configured', reason: 'reset' })
    answerNow()
    await stale
    // What the stale read found is not published over the reset.
    const { result } = renderHook(() => useAiAvailability())
    expect(result.current).toEqual({ available: false, state: 'not_configured', reason: 'reset' })
    // And it no longer blocks a fresh read.
    server.use(http.get(AI_STATUS_PATH, () => HttpResponse.json({ available: true, state: 'enabled', ai: 'enabled' })))
    await act(() => recheckAiAvailability())
    expect(result.current).toEqual({ available: true, state: 'configured' })
  })
})
