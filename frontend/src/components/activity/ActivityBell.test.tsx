import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Activity } from '../../api/types'
import { fakeRealtime } from '../../lib/realtime.fake'
import { seedActivity } from '../../mocks/features/activity'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { clearToasts } from '../../lib/toast'
import { renderPage } from '../../test/utils'
import { Toasts } from '../ui/Toasts'
import { ActivityBell } from './ActivityBell'

function finished(principal: string): Activity {
  const now = new Date(Date.now() + 1000).toISOString()
  return {
    id: 'render:x',
    kind: 'render',
    status: 'succeeded',
    title: 'Render box',
    principal,
    session: null,
    via_tool: null,
    subject: 'box',
    parent_id: null,
    detail: {},
    created_at: now,
    updated_at: now,
    finished_at: now,
  }
}

describe('ActivityBell', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    clearToasts()
    resetMockState()
    window.localStorage.clear()
    seedActivity([])
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('counts finished activity from followed principals and toasts on finish', async () => {
    const live = fakeRealtime()
    const { user } = renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('browser')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'browser' })
    expect(await screen.findByRole('status')).toHaveTextContent('Render box')
    expect(screen.getByTestId('activity-bell-count')).toHaveTextContent('1')
    await user.click(screen.getByRole('button', { name: /Activity, 1 finished/ }))
    expect(screen.getByTestId('activity-bell-count')).not.toBeVisible()
    expect(screen.getByText(/Render box/, { selector: 'li span' })).toBeInTheDocument()
  })

  it('ignores principals not followed', async () => {
    const live = fakeRealtime()
    renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('token:t1')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'token:t1' })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800)
    })
    expect(screen.queryByRole('status')).toBeNull()
    expect(screen.getByTestId('activity-bell-count')).not.toBeVisible()
  })

  it('follows what the setting names', async () => {
    setMockSettings({ ...mockSettings(), activity_follow: ['token:t1'] })
    const live = fakeRealtime()
    renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('token:t1')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'token:t1' })
    expect(await screen.findByRole('status')).toHaveTextContent('Render box')
  })
})
