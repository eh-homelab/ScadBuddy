import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockSessionModeWrites } from '../mocks/features/sessionMode'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SessionModeSetting } from './SessionModeSetting'

describe('SessionModeSetting (#1056)', () => {
  it('shows the stored default and saves a change through PUT', async () => {
    const { user } = renderPage(<SessionModeSetting />)
    const select = await screen.findByRole('combobox', { name: 'Default mode for new chats' })
    expect(select).toHaveValue('durable')

    await user.selectOptions(select, 'classic')
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. New chats start in classic mode')
    expect(mockSessionModeWrites()).toEqual(['classic'])
    expect(select).toHaveValue('classic')
  })

  it('is for the user only', async () => {
    renderPage(<SessionModeSetting />)
    const select = await screen.findByRole('combobox', { name: 'Default mode for new chats' })
    expect(select).toHaveAttribute('data-agent-user-only')
  })

  it.each([
    [400, 'mode: Invalid option'],
    [503, 'the AI database is unreachable'],
  ])('shows the detail of a %i on save and keeps the stored value', async (status, detail) => {
    server.use(http.put('/api/v1/ai/settings/session-mode', () => HttpResponse.json({ detail }, { status })))
    const { user } = renderPage(<SessionModeSetting />)
    const select = await screen.findByRole('combobox', { name: 'Default mode for new chats' })
    await user.selectOptions(select, 'classic')
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(select).toHaveValue('durable')
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/session-mode', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<SessionModeSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Assistant session mode')).not.toBeInTheDocument())
  })
})
