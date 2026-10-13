import { screen, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import { seedActivity } from '../mocks/features/activity'
import { resetMockState } from '../mocks/handlers'
import { renderPage } from '../test/utils'
import { ActivityPage } from './ActivityPage'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

function render() {
  return renderPage(
    <>
      <ActivityPage />
      <Location />
    </>,
    { route: '/activity' },
  )
}

describe('ActivityPage', () => {
  beforeEach(() => {
    resetMockState()
    window.localStorage.clear()
  })
  afterEach(() => vi.useRealTimers())

  it('builds its filter chips from the facets, including a kind it has no code for', async () => {
    render()
    expect(await screen.findByRole('button', { name: 'Install a font' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^You/ })).toBeInTheDocument()
  })

  it('filters by status and keeps it in the URL', async () => {
    const { user } = render()
    await user.click(await screen.findByRole('button', { name: 'Failed' }))
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(1))
    expect(screen.getByTestId('location').textContent).toContain('status=failed')
  })

  it('shows a kind’s own facets once the kind is selected', async () => {
    const { user } = render()
    expect(screen.queryByLabelText('Template')).toBeNull()
    await user.click(await screen.findByRole('button', { name: 'Render' }))
    expect(await screen.findByLabelText('Template')).toBeInTheDocument()
  })

  it('updates live when an activity changes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const realtime = fakeRealtime()
    render()
    await screen.findAllByTestId(/^activity-row-/)
    seedActivity([
      {
        id: 'render:new',
        kind: 'render',
        status: 'running',
        title: 'Render lid',
        principal: 'browser',
        session: null,
        via_tool: null,
        subject: '',
        parent_id: null,
        detail: {},
        created_at: '2026-10-09T11:00:00Z',
        updated_at: '2026-10-09T11:00:00Z',
        finished_at: null,
      },
    ])
    await realtime.signal('activity', 'activity.changed', { activity_id: 'render:new' })
    expect(await screen.findByText('Render lid')).toBeInTheDocument()
  })
})
