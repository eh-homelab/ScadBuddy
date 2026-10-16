import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { WorkspaceSettingsSetting } from './WorkspaceSettingsSetting'

const MiB = 1024 * 1024
const FILE = 'Largest file (MiB)'
const TOTAL = 'Per chat, in all (MiB)'

describe('WorkspaceSettingsSetting (#2329)', () => {
  it('shows the caps in MiB, 25 and 200 by default, and saves them as bytes', async () => {
    const { user } = renderPage(<WorkspaceSettingsSetting />)
    const file = await screen.findByRole('spinbutton', { name: FILE })
    const total = screen.getByRole('spinbutton', { name: TOTAL })
    expect(file).toHaveValue(25)
    expect(total).toHaveValue(200)
    expect(file).toHaveAttribute('max', '100')
    expect(total).toHaveAttribute('max', '2048')
    const save = screen.getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()
    expect(save.closest('[data-agent-user-only]')).not.toBeNull()

    await user.clear(file)
    await user.type(file, '50')
    await user.clear(total)
    await user.type(total, '500')
    await user.click(save)
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(await api.getWorkspaceSettings()).toMatchObject({ file_max_bytes: 50 * MiB, workspace_max_bytes: 500 * MiB })
  })

  it('shows why a save was refused', async () => {
    const { user } = renderPage(<WorkspaceSettingsSetting />)
    const file = await screen.findByRole('spinbutton', { name: FILE })
    const total = screen.getByRole('spinbutton', { name: TOTAL })
    await user.clear(total)
    await user.type(total, '10')
    await user.clear(file)
    await user.type(file, '20')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('file cap no larger')
  })

  it('is hidden when the agent service or its database is not there', async () => {
    server.use(
      http.get('/api/v1/ai/settings/workspace', () => HttpResponse.json({ detail: 'AI features need the database' }, { status: 503 })),
    )
    renderPage(<WorkspaceSettingsSetting />)
    await new Promise((r) => setTimeout(r, 50))
    await waitFor(() => expect(screen.queryByText('Assistant workspace')).not.toBeInTheDocument())
  })
})
