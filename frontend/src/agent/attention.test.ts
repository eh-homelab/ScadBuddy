import { act, renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { setPendingAnswers, setPendingApprovals } from '../mocks/features/pendingInput'
import { server } from '../mocks/server'
import { ATTENTION_POLL_MS, attentionCount, attentionDetail, attentionLabel, fetchPendingInput, useAttention, useAttentionTitle } from './attention'

afterEach(() => {
  vi.useRealTimers()
})

describe('fetchPendingInput', () => {
  it('counts what the agent lists as parked on the user, by kind', async () => {
    setPendingApprovals(2)
    expect(await fetchPendingInput()).toEqual({ approvals: 2, questions: 0, attention: 0 })
    setPendingAnswers(1, 3)
    expect(await fetchPendingInput()).toEqual({ approvals: 2, questions: 1, attention: 3 })
  })

  it('is unknown (null), not zero, when the agent cannot answer', async () => {
    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.json({ detail: 'no database' }, { status: 503 })))
    expect(await fetchPendingInput()).toBeNull()
    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.text('<html></html>')))
    expect(await fetchPendingInput()).toBeNull()
    // An older agent, which has no such route, answers with something else.
    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.json({ approvals: [] })))
    expect(await fetchPendingInput()).toBeNull()
  })

  it('gives up on an agent that accepts and never answers', async () => {
    server.use(http.get('/api/v1/ai/pending-input', () => new Promise<never>(() => {})))
    expect(await fetchPendingInput(20)).toBeNull()
  })

  it('settles by its deadline even if fetch ignores the abort signal', async () => {
    const spy = vi.spyOn(globalThis, 'fetch').mockReturnValue(new Promise<Response>(() => {}))
    try {
      expect(await fetchPendingInput(20)).toBeNull()
    } finally {
      spy.mockRestore()
    }
  })
})

describe('useAttention', () => {
  it('reads nothing while the assistant is off', async () => {
    const seen = vi.fn()
    server.use(
      http.get('/api/v1/ai/pending-input', () => {
        seen()
        return HttpResponse.json({ entries: [] })
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

    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ATTENTION_POLL_MS)
    })
    expect(result.current.waiting).toBe(3)
  })

  it('is unknown (null), not zero, until a read succeeds', async () => {
    server.use(http.get('/api/v1/ai/pending-input', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
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
      http.get('/api/v1/ai/pending-input', async () => {
        calls += 1
        await new Promise<void>((resolve) => {
          answer = resolve
        })
        return HttpResponse.json({ entries: [{ kind: 'approval' }] })
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

  it('reads at once after a quick off and on, though the read from before is still open', async () => {
    let calls = 0
    server.use(
      http.get('/api/v1/ai/pending-input', async () => {
        calls += 1
        if (calls === 1) await new Promise<never>(() => {})
        return HttpResponse.json({ entries: [{ kind: 'approval' }, { kind: 'answer' }] })
      }),
    )
    const { result, rerender } = renderHook(({ on }) => useAttention(on), { initialProps: { on: true } })
    await waitFor(() => expect(calls).toBe(1))
    rerender({ on: false })
    rerender({ on: true })
    await waitFor(() => expect(calls).toBe(2))
    await waitFor(() => expect(result.current.waiting).toBe(2))
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
  it('names the count for the button: things waiting for you, of any kind', () => {
    expect(attentionLabel(null)).toBe('')
    expect(attentionLabel(0)).toBe('')
    expect(attentionLabel(1)).toBe('1 waiting for you')
    expect(attentionLabel(4)).toBe('4 waiting for you')
    // The agent lists at most 500 of a kind: a full page means at least that many.
    expect(attentionCount(499)).toBe('499')
    expect(attentionCount(500)).toBe('500+')
    expect(attentionLabel(500)).toBe('500+ waiting for you')
  })

  it('lists the counts by kind for the title, leaving out the kinds with none', () => {
    expect(attentionDetail(null)).toBe('')
    expect(attentionDetail({ approvals: 2, questions: 1, attention: 0 })).toBe('2 approvals, 1 question')
    expect(attentionDetail({ approvals: 0, questions: 0, attention: 1 })).toBe('1 attention request')
    expect(attentionDetail({ approvals: 1, questions: 2, attention: 3 })).toBe('1 approval, 2 questions, 3 attention requests')
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
