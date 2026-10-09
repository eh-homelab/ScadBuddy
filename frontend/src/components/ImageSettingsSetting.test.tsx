import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ImageSettingsSetting } from './ImageSettingsSetting'

const LABEL = 'Image long edge (px)'

describe('ImageSettingsSetting', () => {
  it('shows the stored edge, 1568 px by default, and saves a new one', async () => {
    const { user } = renderPage(<ImageSettingsSetting />)
    const field = await screen.findByRole('spinbutton', { name: LABEL })
    expect(field).toHaveValue(1568)
    expect(field).toHaveAttribute('min', '200')
    expect(field).toHaveAttribute('max', '2576')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.clear(field)
    await user.type(field, '2576')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect((await api.getImageSettings()).long_edge).toBe(2576)
  })

  it('is for the user only: an agent cannot save it', async () => {
    renderPage(<ImageSettingsSetting />)
    await screen.findByRole('spinbutton', { name: LABEL })
    expect(screen.getByRole('button', { name: 'Save' }).closest('[data-agent-user-only]')).not.toBeNull()
  })

  it('shows why a save was refused', async () => {
    server.use(
      http.put('/api/v1/ai/settings/images', () =>
        HttpResponse.json({ detail: 'long_edge: Too big' }, { status: 400 }),
      ),
    )
    const { user } = renderPage(<ImageSettingsSetting />)
    const field = await screen.findByRole('spinbutton', { name: LABEL })
    await user.clear(field)
    await user.type(field, '2000')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('long_edge: Too big')
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/images', () =>
        HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 }),
      ),
    )
    renderPage(<ImageSettingsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Assistant images')).not.toBeInTheDocument())
  })
})
