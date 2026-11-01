import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import * as fixtures from '../../../mocks/fixtures'
import { setMockSettings } from '../../../mocks/handlers'
import { region, renderSettingsSection } from '../testing'
import { Component as GitRemote } from './git-remote'

const route = '/settings/library'

describe('Off-box history (#4974)', () => {
  it('saves the remote, its username and a write-only token', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<GitRemote />, { route })
    await user.type(await screen.findByLabelText('Remote URL'), 'https://git.example/history.git')
    await user.type(screen.getByLabelText('Username'), 'me')
    await user.type(screen.getByLabelText('Token'), 'tok-123')
    await user.click(within(region('Off-box history')).getByRole('button', { name: 'Save Off-box history' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({
      git_remote_url: 'https://git.example/history.git',
      git_remote_username: 'me',
      git_remote_token: 'tok-123',
    })
    put.mockRestore()
  })

  it('shows each repository’s lag and the last push error once a remote is set', async () => {
    setMockSettings({ ...fixtures.settings, git_remote_url: 'https://git.example/history.git', has_git_remote_token: true })
    renderSettingsSection(<GitRemote />, { route })
    const lag = await screen.findByTestId('git-remote-lag')
    expect(within(lag).getByTestId('git-remote-models')).toHaveTextContent('Up to date')
    expect(within(lag).getByTestId('git-remote-flows')).toHaveTextContent('2 commits behind')
    expect(within(lag).getByRole('alert')).toHaveTextContent('Permission denied')
  })

  it('shows no lag without a remote', async () => {
    renderSettingsSection(<GitRemote />, { route })
    await screen.findByLabelText('Remote URL')
    expect(screen.queryByTestId('git-remote-lag')).not.toBeInTheDocument()
  })
})
