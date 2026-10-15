import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockSessionRuntimeWrites } from '../mocks/features/assistantSessions'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SessionRuntimeSetting } from './SessionRuntimeSetting'

describe('SessionRuntimeSetting', () => {
  it('shows the four fields with their defaults, and saves what changed', async () => {
    const { user } = renderPage(<SessionRuntimeSetting />)
    const hold = await screen.findByRole('spinbutton', { name: 'Park hold (seconds)' })
    const warm = screen.getByRole('spinbutton', { name: 'Warm processes per replica' })
    const idle = screen.getByRole('spinbutton', { name: 'Warm process idle time (seconds)' })
    const turn = screen.getByRole('spinbutton', { name: 'Longest turn (seconds)' })
    expect([hold, warm, idle, turn].map((f) => (f as HTMLInputElement).value)).toEqual(['300', '8', '600', '7200'])
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(warm)
    await user.type(warm, '0')
    await user.clear(idle)
    await user.type(idle, '120')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved.')
    expect(mockSessionRuntimeWrites()).toEqual([{ warm_max_per_replica: 0, warm_idle_seconds: 120 }])
    expect(warm).toHaveValue(0)
    expect(hold).toHaveValue(300)
  })

  it('is for the user only', async () => {
    renderPage(<SessionRuntimeSetting />)
    expect(await screen.findByRole('button', { name: 'Save' })).toHaveAttribute('data-agent-user-only')
  })

  it('shows why the agent refused a value', async () => {
    server.use(
      http.put('/api/v1/ai/settings/session-runtime', () =>
        HttpResponse.json({ detail: 'park_hold_seconds: Too small: expected number to be >=30' }, { status: 422 }),
      ),
    )
    const { user } = renderPage(<SessionRuntimeSetting />)
    const hold = await screen.findByRole('spinbutton', { name: 'Park hold (seconds)' })
    await user.clear(hold)
    await user.type(hold, '31')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('park_hold_seconds: Too small')
    expect(mockSessionRuntimeWrites()).toEqual([])
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/session-runtime', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<SessionRuntimeSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Assistant session runtime')).not.toBeInTheDocument())
  })
})
