import { screen, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { getDisplayUnit } from '../lib/units'
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

  it('saves the display unit and switches every open view to it', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const select = screen.getByLabelText('Show dimensions in')
    expect(select).toHaveValue('mm')
    expect(screen.getByRole('option', { name: '256 × 256 mm' })).toBeInTheDocument()

    await user.selectOptions(select, 'in')
    // The plate list follows the picker before the save, so the choice can be judged.
    expect(screen.getByRole('option', { name: '10.08 × 10.08 in' })).toBeInTheDocument()
    expect(getDisplayUnit()).toBe('mm')

    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(getDisplayUnit()).toBe('in'))
    expect(put.mock.calls[0]?.[0]).toMatchObject({ display_unit: 'in' })
    expect((await api.getSettings()).display_unit).toBe('in')
    put.mockRestore()
  })

  it('registers the Bambuddy sidebar entry', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    await user.click(screen.getByRole('button', { name: 'Add to Bambuddy sidebar' }))
    // Bambuddy renders the link in a sandboxed iframe at /external/{id}.
    expect(await screen.findByRole('status')).toHaveTextContent('/external/3')
  })

  it('shows how much the upload store holds against its caps (#296)', async () => {
    server.use(
      http.get('/api/v1/assets/usage', () =>
        HttpResponse.json({
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('asset-usage')
    expect(usage).toHaveTextContent('12 of 10000')
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
  })

  it('says a cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/assets/usage', () =>
        HttpResponse.json({ count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('asset-usage')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })
})

describe('SettingsPage, live (#269)', () => {
  const OTHER = 'https://scadbuddy.elsewhere.test'

  it('follows settings saved elsewhere while the form is untouched', async () => {
    renderPage(<SettingsPage />)
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    await api.putSettings({ public_url: OTHER })
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await waitFor(() => expect(field).toHaveValue(OTHER))
  })

  it('keeps an edited form and says the settings changed elsewhere', async () => {
    const { user } = renderPage(<SettingsPage />)
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    // Seeded from the stored settings first; editing before that would be overwritten.
    await waitFor(() => expect(field).not.toHaveValue(''))
    await user.clear(field)
    await user.type(field, 'https://mine.test')

    await api.putSettings({ public_url: OTHER })
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    expect(await screen.findByText(/Settings were changed elsewhere/)).toBeInTheDocument()
    expect(field).toHaveValue('https://mine.test')

    await user.click(screen.getByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(field).toHaveValue(OTHER))
  })

  it("does not call this tab's own Test connection a change made elsewhere", async () => {
    const { user } = renderPage(<SettingsPage />)
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    await waitFor(() => expect(field).not.toHaveValue(''))
    await user.clear(field)
    await user.type(field, 'https://tested.test')
    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    await screen.findByRole('status')

    // The test saved the form; this is that save's own event.
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByText(/Settings were changed elsewhere/)).not.toBeInTheDocument()
    expect(field).toHaveValue('https://tested.test')
  })
})
