import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockSessionLimitWrites } from '../mocks/features/assistantSessions'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SessionLimitsSetting } from './SessionLimitsSetting'

describe('SessionLimitsSetting (#790)', () => {
  it('shows the defaults, and saves a new budget and turn limit', async () => {
    const { user } = renderPage(<SessionLimitsSetting />)
    const budget = await screen.findByRole('spinbutton', { name: 'Session budget (USD)' })
    const turns = screen.getByRole('spinbutton', { name: 'Max turns per reply' })
    expect(budget).toHaveValue(1)
    expect(turns).toHaveValue(25)
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(budget)
    await user.type(budget, '2.5')
    await user.clear(turns)
    await user.type(turns, '40')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. New chats use these limits.')
    expect(mockSessionLimitWrites()).toEqual([{ budget_usd: 2.5, max_turns: 40 }])
    expect(budget).toHaveValue(2.5)
  })

  it('is for the user only: an agent cannot raise its own budget', async () => {
    renderPage(<SessionLimitsSetting />)
    const save = await screen.findByRole('button', { name: 'Save' })
    expect(save).toHaveAttribute('data-agent-user-only')
  })

  it('shows why the agent refused a value', async () => {
    server.use(
      http.put('/api/v1/ai/settings/session-limits', () =>
        HttpResponse.json({ detail: 'budget_usd: Too big: expected number to be <=100' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<SessionLimitsSetting />)
    const turns = await screen.findByRole('spinbutton', { name: 'Max turns per reply' })
    await user.clear(turns)
    await user.type(turns, '30')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('budget_usd: Too big')
    expect(mockSessionLimitWrites()).toEqual([])
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/session-limits', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<SessionLimitsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Assistant chat limits')).not.toBeInTheDocument())
  })
})
