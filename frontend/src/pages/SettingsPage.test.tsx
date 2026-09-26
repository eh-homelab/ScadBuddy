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
        HttpResponse.json({ bambuddy_url: '', has_api_key: false }),
      ),
    )
    renderPage(<SettingsPage />)
    expect(await screen.findByLabelText('API key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('tests the connection and names the printers it found', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByRole('status')).toHaveTextContent('3DP-31B-598')
  })

  it('names the missing scope rather than the bare status code', async () => {
    server.use(
      http.post('/api/v1/settings/test', () =>
        HttpResponse.json({
          ok: false,
          detail: "The key needs the 'Read Status' scope",
          printers: [],
        }),
      ),
    )
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    expect(await screen.findByRole('status')).toHaveTextContent("'Read Status' scope")
  })

  it('offers the folders and pipelines Bambuddy reports', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    expect(await screen.findByRole('option', { name: 'ScadBuddy' })).toBeInTheDocument()
    // Bambuddy's ids are integers, so the <select> values are their decimal strings.
    expect(screen.getByLabelText('Library folder')).toHaveValue('2')
    expect(screen.getByLabelText('Slicer pipeline')).toHaveValue('1')
    expect(screen.getByLabelText('Printer')).toHaveValue('1')
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
    expect(put.mock.calls[0]?.[0]).not.toHaveProperty('bambuddy_api_key')
    await waitFor(() => expect(screen.getByText(/Saved at/)).toBeInTheDocument())

    await user.type(screen.getByLabelText('API key'), 'secret')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1]?.[0]).toMatchObject({ bambuddy_api_key: 'secret' })
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

  it('saves the plate the preview falls back to (#81)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const select = screen.getByLabelText('Default plate')
    await waitFor(() => expect(screen.getByRole('option', { name: /A1 mini/ })).toBeInTheDocument())
    await user.selectOptions(select, 'A1 mini')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ default_plate: 'A1 mini' })
    expect(await api.getPlate(null)).toMatchObject({ name: 'A1 mini' })
    put.mockRestore()
  })

  it('registers the Bambuddy sidebar entry', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Add to Bambuddy sidebar' }))
    // Bambuddy renders the link in a sandboxed iframe at /external/{id}.
    expect(await screen.findByRole('status')).toHaveTextContent('/external/3')
  })
})
