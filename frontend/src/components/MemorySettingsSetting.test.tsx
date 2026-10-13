import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { MemorySettingsSetting } from './MemorySettingsSetting'

const LABEL = 'Recall query limit (characters)'

describe('MemorySettingsSetting', () => {
  it('shows the stored limit, 800 by default, and saves a new one', async () => {
    const { user } = renderPage(<MemorySettingsSetting />)
    const field = await screen.findByRole('spinbutton', { name: LABEL })
    expect(field).toHaveValue(800)
    expect(field).toHaveAttribute('min', '0')
    expect(field).toHaveAttribute('max', '100000')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(field)
    await user.type(field, '1200')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect((await api.getMemorySettings()).recall_max_query_chars).toBe(1200)
  })

  it('saves 0, which sends the whole message', async () => {
    const { user } = renderPage(<MemorySettingsSetting />)
    const field = await screen.findByRole('spinbutton', { name: LABEL })
    await user.clear(field)
    await user.type(field, '0')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect((await api.getMemorySettings()).recall_max_query_chars).toBe(0)
  })

  it('is for the user only: an agent cannot save it', async () => {
    renderPage(<MemorySettingsSetting />)
    await screen.findByRole('spinbutton', { name: LABEL })
    expect(screen.getByRole('button', { name: 'Save' }).closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused', async () => {
    server.use(
      http.put('/api/v1/ai/settings/memory', () =>
        HttpResponse.json({ detail: 'recall_max_query_chars: Too big' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<MemorySettingsSetting />)
    const field = await screen.findByRole('spinbutton', { name: LABEL })
    await user.clear(field)
    await user.type(field, '2000')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('recall_max_query_chars: Too big')
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/memory', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<MemorySettingsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Hindsight memory')).not.toBeInTheDocument())
  })
})
