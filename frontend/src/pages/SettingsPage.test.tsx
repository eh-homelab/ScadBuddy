import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue(
      'https://bambuddy.internal.nullreference.io',
    ),
  )
}

describe('SettingsPage', () => {
  it('loads the stored connection without revealing the key', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const key = screen.getByLabelText('API key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('says when no key is stored yet', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({
          bambuddy_url: '',
          api_key_set: false,
          sidebar_registered: false,
        }),
      ),
    )
    renderPage(<SettingsPage />)
    expect(await screen.findByLabelText('API key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('tests the connection and names the printers it found', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Manage Library, Manage Queue and Read Status')
    expect(status).toHaveTextContent('X1C · Workshop')
  })

  it('reports a bad URL instead of claiming success', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const url = screen.getByLabelText('Bambuddy URL')
    await user.clear(url)
    await user.type(url, 'bambuddy.local')

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByRole('status')).toHaveTextContent('starting with http')
  })

  it('offers the folders and pipelines Bambuddy reports', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    expect(await screen.findByRole('option', { name: 'ScadBuddy' })).toBeInTheDocument()
    expect(screen.getByLabelText('Library folder')).toHaveValue('folder-scadbuddy')
    expect(screen.getByLabelText('Slicer pipeline')).toHaveValue('pipeline-textured-pei')
    expect(
      screen.getByRole('option', { name: 'Textured PEI · 0.20 mm · AMS' }),
    ).toBeInTheDocument()
  })

  it('sends the key only when one was typed', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).not.toHaveProperty('api_key')
    await waitFor(() => expect(screen.getByText(/Saved at/)).toBeInTheDocument())

    await user.type(screen.getByLabelText('API key'), 'secret')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ api_key: 'secret' })
    put.mockRestore()
  })

  it('clears the key field once it is saved', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.type(screen.getByLabelText('API key'), 'secret')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(screen.getByLabelText('API key')).toHaveValue(''))
    expect(screen.getByText(/Saved at/)).toBeInTheDocument()
  })

  it('registers the Bambuddy sidebar entry', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Add to Bambuddy sidebar' }))
    expect(await screen.findByRole('status')).toHaveTextContent('appears in the Bambuddy sidebar')
  })
})
