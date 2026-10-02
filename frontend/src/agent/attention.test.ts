import { act, renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setPendingApprovals } from '../mocks/features/approvals'
import { server } from '../mocks/server'
import { ATTENTION_POLL_MS, attentionCount, attentionLabel, fetchPendingApprovals, useAttention, useAttentionTitle } from './attention'

afterEach(() => {
  vi.useRealTimers()
})

describe('fetchPendingApprovals', () => {
  it('counts the pending approvals the agent lists', async () => {
    setPendingApprovals(2)
    expect(await fetchPendingApprovals()).toBe(2)
  })

  it('is unknown (null), not zero, when the agent cannot answer', async () => {
    server.use(http.get('/api/v1/ai/approvals', () => HttpResponse.json({ detail: 'no database' }, { status: 503 })))
    expect(await fetchPendingApprovals()).toBeNull()
    server.use(http.get('/api/v1/ai/approvals', () => HttpResponse.text('<html></html>')))
    expect(await fetchPendingApprovals()).toBeNull()
  })

  it('gives up on an agent that accepts and never answers', async () => {
    server.use(http.get('/api/v1/ai/approvals', () => new Promise<never>(() => {})))
    expect(await fetchPendingApprovals(20)).toBeNull()
  })

  it('settles by its deadline even if fetch ignores the abort signal', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>(() => {}))
    try {
      expect(await fetchPendingApprovals(20)).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })

  it('asks only for pending ones', async () => {
    let url = ''
    server.use(
      http.get('/api/v1/ai/approvals', ({ request }) => {
        url = request.url
        return HttpResponse.json({ approvals: [] })
      }),
    )
    await fetchPendingApprovals()
    expect(new URL(url).searchParams.get('pending')).toBe('true')
  })
})

describe('useAttention', () => {
  it('reads nothing while the assistant is off', async () => {
    const seen = vi.fn()
    server.use(
      http.get('/api/v1/ai/approvals', () => {
        seen()
        return HttpResponse.json({ approvals: [] })
      }),
    )
    const { result } = renderHook(() => useAttention(false))
    await act(async () => {})
    expect(result.current.waiting).toBeNull()
    expect(seen).not.toHaveBeenCalled()
  })

  it('polls, and keeps the last count through a failed read', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    setPendingApprovals(1)
    const { result } = renderHook(() => useAttention(true))
    await waitFor(() => expect(result.current.waiting).toBe(1))

    setPendingApprovals(3)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS)
    })
    await waitFor(() => expect(result.current.waiting).toBe(3))

    server.use(http.get('/api/v1/ai/approvals', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS)
    })
    expect(result.current.waiting).toBe(3)
  })

  it('is unknown (null), not zero, until a read succeeds', async () => {
    server.use(http.get('/api/v1/ai/approvals', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    const { result } = renderHook(() => useAttention(true))
    expect(result.current.waiting).toBeNull()
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    expect(result.current.waiting).toBeNull()
    server.resetHandlers()
    act(() => result.current.refresh())
    await waitFor(() => expect(result.current.waiting).toBe(0))
  })

  it('starts no second read while one is still waiting for the agent', async () => {
    let calls = 0
    let answer: (() => void) | undefined
    server.use(
      http.get('/api/v1/ai/approvals', async () => {
        calls += 1
        await new Promise<void>((resolve) => {
          answer = resolve
        })
        return HttpResponse.json({ approvals: [{}] })
      }),
    )
    const { result } = renderHook(() => useAttention(true))
    await waitFor(() => expect(calls).toBe(1))
    act(() => {
      result.current.refresh()
      window.dispatchEvent(new Event('focus'))
    })
    await act(async () => {})
    expect(calls).toBe(1)

    act(() => answer?.())
    await waitFor(() => expect(result.current.waiting).toBe(1))
    act(() => result.current.refresh())
    await waitFor(() => expect(calls).toBe(2))
    act(() => answer?.())
  })

  it('reads again on refresh and when the tab comes back into view', async () => {
    setPendingApprovals(1)
    const { result } = renderHook(() => useAttention(true))
    await waitFor(() => expect(result.current.waiting).toBe(1))

    setPendingApprovals(0)
    act(() => result.current.refresh())
    await waitFor(() => expect(result.current.waiting).toBe(0))

    setPendingApprovals(2)
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => expect(result.current.waiting).toBe(2))
  })
})

describe('labels', () => {
  it('names the count for the button', () => {
    expect(attentionLabel(null)).toBe('')
    expect(attentionLabel(0)).toBe('')
    expect(attentionLabel(1)).toBe('1 action waiting for your approval')
    expect(attentionLabel(4)).toBe('4 actions waiting for your approval')
    // The agent lists at most 500: a full page means at least that many.
    expect(attentionCount(499)).toBe('499')
    expect(attentionCount(500)).toBe('500+')
    expect(attentionLabel(500)).toBe('500+ actions waiting for your approval')
  })
})

describe('useAttentionTitle', () => {
  it('prefixes the count and restores the title it found, even one that looks prefixed', () => {
    document.title = '(3) copies'
    const { rerender, unmount } = renderHook(({ n }) => useAttentionTitle(n, true), { initialProps: { n: 0 } })
    expect(document.title).toBe('(3) copies')
    rerender({ n: 2 })
    expect(document.title).toBe('(2) (3) copies')
    rerender({ n: 5 })
    expect(document.title).toBe('(5) (3) copies')
    rerender({ n: 0 })
    expect(document.title).toBe('(3) copies')
    rerender({ n: 1 })
    unmount()
    expect(document.title).toBe('(3) copies')
  })

  it('leaves the title alone when off (embedded in Bambuddy)', () => {
    document.title = 'ScadBuddy'
    renderHook(() => useAttentionTitle(4, false))
    expect(document.title).toBe('ScadBuddy')
  })
})
