import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { ModelSetting } from './ModelSetting'

const LABEL = 'Claude model'

describe('ModelSetting (#1917)', () => {
  it("starts on Claude Code's default, saves a model, and goes back to the default", async () => {
    const { user } = renderPage(<ModelSetting />)
    const input = await screen.findByRole('combobox', { name: LABEL })
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', expect.stringContaining('default'))
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    await user.type(input, 'claude-sonnet-5')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(await api.getModelSetting()).toEqual({ model: 'claude-sonnet-5' })

    await user.click(screen.getByRole('button', { name: 'Use the default' }))
    await waitFor(() => expect(input).toHaveValue(''))
    expect(await api.getModelSetting()).toEqual({ model: null })
  })

  it('offers the model aliases', async () => {
    renderPage(<ModelSetting />)
    await screen.findByRole('combobox', { name: LABEL })
    const values = [...document.querySelectorAll('#assistant-model-options option')].map((o) => o.getAttribute('value'))
    expect(values).toEqual(expect.arrayContaining(['opus', 'sonnet', 'haiku']))
  })

  it('is for the user only: an agent cannot pick its own model', async () => {
    renderPage(<ModelSetting />)
    const save = await screen.findByRole('button', { name: 'Save' })
    expect(save).toHaveAttribute('data-agent-user-only')
    expect(screen.getByRole('button', { name: 'Use the default' })).toHaveAttribute('data-agent-user-only')
  })

  it('shows why the agent refused a name, and keeps what was stored', async () => {
    const { user } = renderPage(<ModelSetting />)
    const input = await screen.findByRole('combobox', { name: LABEL })
    await user.type(input, 'sonnet; rm')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('must be a model name')
    expect(await api.getModelSetting()).toEqual({ model: null })
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/model', () => HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 })),
    )
    renderPage(<ModelSetting />)
    await new Promise((r) => setTimeout(r, 50))
    expect(screen.queryByText('Assistant model')).not.toBeInTheDocument()
  })
})
