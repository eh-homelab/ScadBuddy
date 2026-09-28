import { screen, waitFor, within } from '@testing-library/react'
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

/** The render key's warning (#426) is a status too, so find the one that says `text`. */
function findStatus(text: string) {
  return screen.findByText(
    (_, element) =>
      element?.getAttribute('role') === 'status' && (element.textContent ?? '').includes(text),
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

  it('offers the AI headless browser switch, off by default (#349)', async () => {
    renderPage(<SettingsPage />)
    expect(
      await screen.findByRole('checkbox', {
        name: 'Let AI sessions use ScadBuddy in a headless browser',
      }),
    ).not.toBeChecked()
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
    expect(await findStatus('3DP-31B-598')).toBeInTheDocument()
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
    expect(await findStatus("'Read Status' scope")).toBeInTheDocument()
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
    expect(await findStatus('/external/3')).toBeInTheDocument()
  })

  it('shows how much the store holds against its caps (#296, #426)', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'local',
          by_kind: {},
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('12 of 10000')
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
  })

  it('says a cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'local',
          by_kind: {},
          count: 2,
          bytes: 640,
          max_count: 0,
          max_total_bytes: 0,
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })

  const stored = {
    bambuddy_url: 'https://bambuddy.internal.nullreference.io',
    has_api_key: true,
    library_folder_id: 7,
    store_backend: 'local',
  }

  it('warns while render workers would hold the full key', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: false, render_key_fallback: true }),
      ),
    )
    renderPage(<SettingsPage />)
    await seeded()
    const warning = screen.getByTestId('render-key-fallback')
    expect(warning).toHaveAttribute('role', 'status')
    expect(warning).toHaveTextContent(
      /^Render workers hold the full Bambuddy key; template code can print\. Create a key with only Manage Library in Bambuddy and paste it above as the render key\.$/,
    )
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('drops the warning once a render key is stored, and never shows the key', async () => {
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: true, render_key_fallback: false }),
      ),
    )
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByTestId('render-key-fallback')).toBeNull()
    const key = screen.getByLabelText('Render key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('sends a typed render key and leaves the stored API key alone', async () => {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('/api/v1/settings', () =>
        HttpResponse.json({ ...stored, has_render_api_key: false, render_key_fallback: true }),
      ),
      http.put('/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json({ ...stored, has_render_api_key: true, render_key_fallback: false })
      }),
    )
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.type(screen.getByLabelText('Render key'), 'narrow')
    await user.click(screen.getByRole('button', { name: /^save/i }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ bambuddy_render_api_key: 'narrow' })
    expect(bodies[0]).not.toHaveProperty('bambuddy_api_key')
  })

  it('shows store usage in place of the uploads line', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'bambuddy',
          count: 12,
          bytes: 2048,
          max_count: 0,
          max_total_bytes: 0,
          by_kind: { piece: 1024, asset: 1024 },
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('Bambuddy library')
    expect(within(usage).getByText('Files').nextElementSibling).toHaveTextContent(
      /^12 \(no limit\)$/,
    )
    expect(screen.queryByTestId('asset-usage')).toBeNull()
    expect(
      screen.getByText(
        'The Where row is the store this process uses; it moves to the Blob store choice above at its next restart, so the two can differ until then.',
      ),
    ).toBeInTheDocument()
  })

  it('keeps a stored Bambuddy store backend on an unrelated save', async () => {
    const bodies: Record<string, unknown>[] = []
    const bambuddy = { ...stored, store_backend: 'bambuddy', has_render_api_key: true }
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(bambuddy)),
      http.put('/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(bambuddy)
      }),
    )
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    expect(screen.getByRole('option', { name: /Bambuddy library/ })).toBeEnabled()

    const own = screen.getByLabelText(/ScadBuddy.s own URL/)
    await user.clear(own)
    await user.type(own, 'https://scadbuddy.test')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toMatchObject({ store_backend: 'bambuddy' })
  })

  it('offers the Bambuddy store only once an inbox folder is chosen', async () => {
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json({ ...stored, library_folder_id: null })),
    )
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByRole('option', { name: /Bambuddy library/ })).toBeDisabled()
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
    await findStatus('3DP-31B-598')

    // The test saved the form; this is that save's own event.
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByText(/Settings were changed elsewhere/)).not.toBeInTheDocument()
    expect(field).toHaveValue('https://tested.test')
  })

  it('shows the assistant plugin sections only while AI is available', async () => {
    const hidden = renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByRole('heading', { name: 'Plugin packages' })).not.toBeInTheDocument()
    hidden.unmount()

    vi.stubEnv('VITE_MOCK_API', '1')
    try {
      renderPage(<SettingsPage />)
      expect(await screen.findByRole('heading', { name: 'Plugin packages' })).toBeInTheDocument()
      expect(screen.getByRole('heading', { name: 'Plugin endpoints' })).toBeInTheDocument()
      expect(await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })).toBeInTheDocument()
    } finally {
      vi.unstubAllEnvs()
    }
  })
})
