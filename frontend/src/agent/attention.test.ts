import { act, renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setPendingApprovals } from '../mocks/features/approvals'
import { server } from '../mocks/server'
import { ATTENTION_POLL_MS, attentionLabel, fetchPendingApprovals, titleWithAttention, useAttention } from './attention'

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
    expect(result.current.waiting).toBe(0)
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
  it('names the count for the button and the tab title', () => {
    expect(attentionLabel(0)).toBe('')
    expect(attentionLabel(1)).toBe('1 action waiting for your approval')
    expect(attentionLabel(4)).toBe('4 actions waiting for your approval')
    expect(titleWithAttention('ScadBuddy', 0)).toBe('ScadBuddy')
    expect(titleWithAttention('ScadBuddy', 2)).toBe('(2) ScadBuddy')
    // Never stacks a second prefix on one it wrote.
    expect(titleWithAttention('(2) ScadBuddy', 3)).toBe('(3) ScadBuddy')
    expect(titleWithAttention('(2) ScadBuddy', 0)).toBe('ScadBuddy')
  })
})
