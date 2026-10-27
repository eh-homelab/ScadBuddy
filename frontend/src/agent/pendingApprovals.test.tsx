import { render, renderHook, screen, within } from '@testing-library/react'
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

describe('notTakenText', () => {
  it('says the agent’s reason, or that it no longer waits, or what went wrong', () => {
    expect(notTakenText(new RespondError('x', 409, 'it was already approved'))).toBe('Your decision was not taken: it was already approved.')
    expect(notTakenText(new RespondError('gone', 410))).toBe('Your decision was not taken: the request is no longer waiting.')
    expect(notTakenText(new RespondError('The assistant could not be reached; try again.'))).toBe(
      'Your decision was not taken: The assistant could not be reached; try again.',
    )
  })
})
