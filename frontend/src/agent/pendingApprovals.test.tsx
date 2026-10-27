import { act, render, renderHook, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { Toasts } from '../components/ui/Toasts'
import type { PendingApproval } from './attention'
import { MAX_APPROVAL_TOASTS, notTakenText, useApprovalToasts } from './pendingApprovals'
import { RespondError } from './respond'

const approval = (i: number): PendingApproval => ({ id: `approval:a${i}`, tool: 'print_output', summary: `{"n":${i}}` })

describe('useApprovalToasts (#4813)', () => {
  it(`shows at most ${MAX_APPROVAL_TOASTS} at once, and one toast for the rest, linking to them all`, () => {
    render(
      <MemoryRouter>
        <Toasts />
      </MemoryRouter>,
    )
    const refresh = vi.fn()
    const all = Array.from({ length: MAX_APPROVAL_TOASTS + 2 }, (_, i) => approval(i))
    const { rerender } = renderHook(({ list }) => useApprovalToasts(list, refresh), { initialProps: { list: all } })
    const toasts = within(screen.getByTestId('toasts'))
    expect(toasts.getAllByText('Needs your approval')).toHaveLength(MAX_APPROVAL_TOASTS)
    expect(toasts.getByText('More approvals wait for you')).toBeInTheDocument()
    expect(toasts.getAllByRole('link', { name: 'Review' }).map((a) => a.getAttribute('href'))).toEqual(
      Array.from({ length: MAX_APPROVAL_TOASTS + 1 }, () => '/activity'),
    )
    // None waits any more: every toast goes.
    rerender({ list: [] })
    expect(toasts.queryByText('Needs your approval')).not.toBeInTheDocument()
    expect(toasts.queryByText('More approvals wait for you')).not.toBeInTheDocument()
  })
})

describe('useApprovalToasts, as approvals come and go (#4813)', () => {
  it('drops the toast of one that stops waiting, keeps the others, and toasts one held past the cap once a place frees', () => {
    render(
      <MemoryRouter>
        <Toasts />
      </MemoryRouter>,
    )
    const all = Array.from({ length: MAX_APPROVAL_TOASTS + 1 }, (_, i) => approval(i))
    const { rerender } = renderHook(({ list }) => useApprovalToasts(list, vi.fn()), { initialProps: { list: all } })
    const toasts = within(screen.getByTestId('toasts'))
    const details = () => toasts.queryAllByText(/^print_output: /).map((e) => e.textContent)
    expect(details()).toEqual(all.slice(0, MAX_APPROVAL_TOASTS).map((a) => `print_output: ${a.summary}`))
    // The first is decided elsewhere: its toast goes, the others stay, and the one held back gets its own.
    rerender({ list: all.slice(1) })
    expect(details()).toEqual(all.slice(1).map((a) => `print_output: ${a.summary}`))
    expect(toasts.queryByText('More approvals wait for you')).not.toBeInTheDocument()
  })

  it('never stays past the default window, however far off the approval expires', () => {
    vi.useFakeTimers()
    try {
      render(
        <MemoryRouter>
          <Toasts />
        </MemoryRouter>,
      )
      const far = { ...approval(0), expiresAt: new Date(Date.now() + 60 * 86_400_000).toISOString() }
      renderHook(() => useApprovalToasts([far], vi.fn()))
      const toasts = within(screen.getByTestId('toasts'))
      // Not fired at once (a timer past 2^31 ms would be), and gone after ten minutes.
      act(() => vi.advanceTimersByTime(1_000))
      expect(toasts.getByText('Needs your approval')).toBeInTheDocument()
      act(() => vi.advanceTimersByTime(10 * 60_000))
      expect(toasts.queryByText('Needs your approval')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('notTakenText', () => {
  it('says the agent’s reason, or that it no longer waits, or what went wrong', () => {
    expect(notTakenText(new RespondError('x', 409, 'it was already approved'))).toBe('Your decision was not taken: it was already approved.')
    expect(notTakenText(new RespondError('gone', 410))).toBe('Your decision was not taken: the request is no longer waiting.')
    expect(notTakenText(new RespondError('The assistant could not be reached; try again.'))).toBe(
      'Your decision was not taken: The assistant could not be reached; try again.',
    )
  })
})
