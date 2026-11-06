import userEvent from '@testing-library/user-event'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { notifyChannelsState } from '../mocks/features/notifyChannels'
import { NotifyChannelsSetting } from './NotifyChannelsSetting'

// #815 — ntfy and Home Assistant channels in Settings: added, tested and removed by the person only.

describe('NotifyChannelsSetting', () => {
  it('adds an ntfy channel, tests it and deletes it', async () => {
    render(<NotifyChannelsSetting />)
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Phone' } })
    fireEvent.change(screen.getByLabelText('ntfy topic URL'), { target: { value: 'https://ntfy.sh/scadbuddy-x' } })
    fireEvent.change(screen.getByLabelText('Access token (optional)'), { target: { value: 'tk_secret_ntfy_token' } })
    const add = screen.getByRole('button', { name: 'Add channel' })
    expect(add.hasAttribute('data-agent-user-only')).toBe(true)
    fireEvent.click(add)
    const row = await screen.findByRole('listitem', { name: 'Phone' })
    expect(within(row).getByText(/ntfy/)).toBeTruthy()
    expect(within(row).getByText(/…oken/)).toBeTruthy()
    expect(notifyChannelsState()[0]).toMatchObject({ name: 'Phone', has_token: true })

    fireEvent.click(within(row).getByRole('button', { name: 'Send a test' }))
    expect(await within(row).findByText('Sent.')).toBeTruthy()

    fireEvent.click(within(row).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.queryByRole('listitem', { name: 'Phone' })).toBeNull())
    expect(notifyChannelsState()).toEqual([])
  })

  it('asks Home Assistant for its notify service and token, and shows a refusal', async () => {
    render(<NotifyChannelsSetting />)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('combobox', { name: 'Kind' }))
    await user.click(await screen.findByRole('option', { name: 'Home Assistant' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'HA' } })
    fireEvent.change(screen.getByLabelText('Home Assistant URL'), { target: { value: 'http://192.168.1.10:8123' } })
    fireEvent.change(screen.getByLabelText('Notify service'), { target: { value: 'mobile_app_pixel' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
    expect(await screen.findByText(/long-lived access token/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Long-lived access token'), { target: { value: 'ha_long_lived_token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add channel' }))
    expect(await screen.findByRole('listitem', { name: 'HA' })).toBeTruthy()
  })
})
