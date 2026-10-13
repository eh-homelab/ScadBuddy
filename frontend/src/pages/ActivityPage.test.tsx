import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { useLocation, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { fakeRealtime } from '../lib/realtime.fake'
import { seedActivity } from '../mocks/features/activity'
import { resetMockState } from '../mocks/handlers'
import { renderPage } from '../test/utils'
import type { Activity } from '../api/types'
import { principalLabel, sinceTime } from '../components/activity/labels'
import { ActivityPage } from './ActivityPage'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

function Back() {
  const navigate = useNavigate()
  return (
    <button type="button" onClick={() => void navigate(-1)}>
      Go back
    </button>
  )
}

function render(route = '/activity') {
  return renderPage(
    <>
      <ActivityPage />
      <Location />
      <Back />
    </>,
    { route },
  )
}

function activity(id: string, at: string, extra: Partial<Activity> = {}): Activity {
  return {
    id,
    kind: 'render',
    status: 'running',
    title: `Row ${id}`,
    principal: 'browser',
    session: null,
    via_tool: null,
    subject: '',
    parent_id: null,
    detail: {},
    created_at: at,
    updated_at: at,
    finished_at: null,
    ...extra,
  }
}

/** `count` rows, newest first, one minute apart. */
function many(count: number): Activity[] {
  return Array.from({ length: count }, (_, i) =>
    activity(`r${String(i).padStart(3, '0')}`, new Date(Date.UTC(2026, 9, 9, 12) - i * 60_000).toISOString()),
  )
}

describe('labels', () => {
  it('names the principals', () => {
    expect(principalLabel('browser')).toBe('You')
    expect(principalLabel('system')).toBe('System')
    expect(principalLabel(null)).toBe('Unknown')
    expect(principalLabel('token:t1')).toBe('token:t1')
    expect(principalLabel('Zo%C3%AB')).toBe('Zoë')
    expect(principalLabel('100%')).toBe('100%')
  })

  it('turns a since token into a time, and an unknown one into none', () => {
    const now = Date.parse('2026-10-09T12:00:00Z')
    expect(sinceTime('24h', now)).toBe('2026-10-08T12:00:00.000Z')
    expect(sinceTime('7d', now)).toBe('2026-10-02T12:00:00.000Z')
    expect(sinceTime('bogus', now)).toBe('')
    expect(sinceTime('', now)).toBe('')
  })
})

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

  it('loads more, and a live insert loses no row', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const realtime = fakeRealtime()
    seedActivity(many(60))
    const { user } = render()
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50))
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(60))
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    seedActivity([activity('new', '2026-10-09T13:00:00Z'), ...many(60)])
    await realtime.signal('activity', 'activity.changed', { activity_id: 'new' })
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(61))
    expect(screen.getByTestId('activity-row-r059')).toBeInTheDocument()
  })

  it('restores chips and a facet input from a deep link', async () => {
    render('/activity?kind=render&facet.slug=box')
    expect(await screen.findByRole('button', { name: 'Render', pressed: true })).toBeInTheDocument()
    expect(await screen.findByLabelText('Template')).toHaveValue('box')
  })

  it('shows an applied value the facets do not list, and clears it', async () => {
    const { user } = render('/activity?principal=ghost')
    expect(await screen.findByRole('button', { name: 'ghost', pressed: true })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/activity'))
  })

  it('nests a child under its parent when both are loaded', async () => {
    seedActivity([
      activity('child', '2026-10-09T12:05:00Z', { parent_id: 'parent' }),
      activity('other', '2026-10-09T12:03:00Z'),
      activity('parent', '2026-10-09T12:00:00Z'),
    ])
    render()
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(3))
    const order = screen.getAllByTestId(/^activity-row-/).map((r) => r.getAttribute('data-testid'))
    expect(order).toEqual(['activity-row-other', 'activity-row-parent', 'activity-row-child'])
    expect(within(screen.getByTestId('activity-row-child')).getByText('Row child').closest('td')).toHaveClass('pl-5')
  })

  it('keeps the loaded rows when a Load more fails, and says to try again', async () => {
    seedActivity(many(60))
    const { user } = render()
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50))
    server.use(http.get('/api/v1/activity', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await screen.findByText('Could not load more. Try again.')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50)
    expect(screen.getByRole('button', { name: 'Load more' })).toBeEnabled()
  })

  it('pauses live updates past the cap, and Show latest brings them back', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const realtime = fakeRealtime()
    seedActivity(many(260))
    const { user } = render()
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50))
    for (const n of [100, 150, 200]) {
      await user.click(screen.getByRole('button', { name: 'Load more' }))
      await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(n))
    }
    expect(screen.queryByText(/Live updates paused/)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(250))
    expect(screen.getByText(/Live updates paused, showing older activity/)).toBeInTheDocument()
    seedActivity([activity('new', '2026-10-09T13:00:00Z'), ...many(260)])
    await realtime.signal('activity', 'activity.changed', { activity_id: 'new' })
    expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(250)
    expect(screen.queryByTestId('activity-row-new')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Show latest' }))
    expect(await screen.findByTestId('activity-row-new')).toBeInTheDocument()
    expect(screen.queryByText(/Live updates paused/)).toBeNull()
  })

  it('re-reads the facets at most once per throttle window', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const realtime = fakeRealtime()
    const facets = vi.spyOn(api, 'activityFacets')
    render()
    await screen.findAllByTestId(/^activity-row-/)
    await vi.advanceTimersByTimeAsync(7000)
    const before = facets.mock.calls.length
    for (let i = 0; i < 3; i++) await realtime.signal('activity', 'activity.changed')
    expect(facets.mock.calls.length).toBe(before + 1)
    await vi.advanceTimersByTimeAsync(3500)
    expect(facets.mock.calls.length).toBe(before + 2)
  })

  it('drops the held pages when the query changes, and Back shows a fresh first page', async () => {
    seedActivity(many(260))
    const { user } = render()
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50))
    for (const n of [100, 150, 200, 250]) {
      await user.click(screen.getByRole('button', { name: 'Load more' }))
      await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(n))
    }
    expect(screen.getByText(/Live updates paused/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Running' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toContain('status=running'))
    await user.click(screen.getByRole('button', { name: 'Go back' }))
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/activity'))
    await waitFor(() => expect(screen.getAllByTestId(/^activity-row-/)).toHaveLength(50))
    expect(screen.queryByText(/Live updates paused/)).toBeNull()
  })
})
