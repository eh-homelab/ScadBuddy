import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { setDurableAvailable } from '../mocks/features/sessionMode'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SessionModeSetting } from './SessionModeSetting'

const LABEL = 'Default session mode'

describe('SessionModeSetting', () => {
  it('shows the stored default, durable until set, and saves a new one', async () => {
    const { user } = renderPage(<SessionModeSetting />)
    const select = await screen.findByRole('combobox', { name: LABEL })
    expect(select).toHaveValue('durable')
    expect(screen.queryByTestId('session-mode-unavailable')).not.toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    await user.selectOptions(select, 'classic')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect((await api.getSessionMode()).mode).toBe('classic')
  })

  it('says when durable sessions cannot start now, and why', async () => {
    setDurableAvailable(false)
    renderPage(<SessionModeSetting />)
    const note = await screen.findByTestId('session-mode-unavailable')
    expect(note).toHaveTextContent(/run as Classic/)
    expect(note).toHaveTextContent(/no durable session worker/)
  })

  it('is for the user only: an agent cannot save it', async () => {
    renderPage(<SessionModeSetting />)
    await screen.findByRole('combobox', { name: LABEL })
    expect(screen.getByRole('button', { name: 'Save' }).closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused', async () => {
    server.use(
      http.put('/api/v1/ai/settings/session-mode', () => HttpResponse.json({ detail: 'mode: Nope' }, { status: 400 })),
    )
    const { user } = renderPage(<SessionModeSetting />)
    await user.selectOptions(await screen.findByRole('combobox', { name: LABEL }), 'classic')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('mode: Nope')
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
