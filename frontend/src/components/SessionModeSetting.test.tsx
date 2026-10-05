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

    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    await user.selectOptions(select, 'classic')
    expect(mockSessionModeWrites()).toEqual([])
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved. New chats start in classic mode')
    expect(mockSessionModeWrites()).toEqual(['classic'])
    expect(select).toHaveValue('classic')
  })

  it('is for the user only: Save is not for an agent', async () => {
    renderPage(<SessionModeSetting />)
    const save = await screen.findByRole('button', { name: 'Save' })
    expect(save).toHaveAttribute('data-agent-user-only')
  })

  it.each([
    [400, 'mode: Invalid option'],
    [503, 'the AI database is unreachable'],
  ])('shows the detail of a %i on save and keeps the choice unsaved', async (status, detail) => {
    server.use(http.put('/api/v1/ai/settings/session-mode', () => HttpResponse.json({ detail }, { status })))
    const { user } = renderPage(<SessionModeSetting />)
    const select = await screen.findByRole('combobox', { name: 'Default mode for new chats' })
    await user.selectOptions(select, 'classic')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(select).toHaveValue('classic')
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('says a chat choice overrides the default', async () => {
    renderPage(<SessionModeSetting />)
    expect(await screen.findByText(/remembered on that browser and overrides this default/)).toBeInTheDocument()
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/session-mode', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<SessionModeSetting />)
    await waitFor(() => expect(screen.queryByText('Assistant session mode')).not.toBeInTheDocument())
  })
})
