import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { BrowserRouter, MemoryRouter, Link, Route, Routes, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Settings } from '../api/types'
import { restartMockBackend } from '../mocks/features/settings'
import { mockSettings, setMockRemembered, setMockSettings } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'

// #322 — every runtime setting in the UI, with where its value came from; a save per
// section; the remembered choices; About.

async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

function region(name: string) {
  return screen.getByRole('region', { name })
}

describe('SettingsPage sources (#322)', () => {
  it('badges each field with where its value comes from', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByTestId('source-bambuddy_url')).toHaveTextContent('Set here')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL')
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('From SCADBUDDY_RENDER_TIMEOUT')
    expect(screen.getByTestId('source-default_plate')).toHaveTextContent('Default')
    const cleared = screen.getByTestId('source-google_fonts_api_key')
    expect(cleared).toHaveTextContent('Cleared')
    expect(cleared).toHaveTextContent('SCADBUDDY_GOOGLE_FONTS_API_KEY is ignored')
    // Only a stored or cleared value has something to reset.
    expect(within(cleared).getByRole('button', { name: /Reset google_fonts_api_key/ })).toBeInTheDocument()
    expect(within(screen.getByTestId('source-public_url')).queryByRole('button')).toBeNull()
  })

  it('resets a saved value back to the deployment value', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await user.type(field, 'https://mine.test')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    const put = vi.spyOn(api, 'putSettings')
    await user.click(screen.getByRole('button', { name: 'Reset public_url to the deployment value' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL'))
    expect(put).toHaveBeenCalledWith({ reset: ['public_url'] })
    await waitFor(() => expect(field).toHaveValue('https://scadbuddy.internal.nullreference.io'))
    put.mockRestore()
  })

  it('resets a cleared key so the deployment one applies again', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(screen.getByRole('button', { name: 'Reset google_fonts_api_key to the deployment value' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-google_fonts_api_key')).toHaveTextContent('From SCADBUDDY_GOOGLE_FONTS_API_KEY'),
    )
    expect(screen.getByLabelText('Google Fonts API key')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('A key is stored'),
    )
  })

  it('says a restart-only field waits for a restart once saved', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Applies on restart')
    const field = screen.getByLabelText('Renders at once')
    await user.clear(field)
    await user.type(field, '4')
    await user.click(screen.getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Saved; restart ScadBuddy to apply'),
    )
    restartMockBackend()
  })
})

describe('SettingsPage sections (#322)', () => {
  it('saves one section and leaves another one’s edits unsaved', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.selectOptions(screen.getByLabelText('Show dimensions in'), 'in')
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // Only what changed in that section: the rest of the page is not posted back.
    expect(put.mock.calls[0]?.[0]).toEqual({ render_timeout: 45 })
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here')
    // The other section kept its edit.
    expect(screen.getByLabelText('Show dimensions in')).toHaveValue('in')
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it('discards one section’s edits', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const ttl = screen.getByLabelText('Refresh the font catalogue after')
    await user.clear(ttl)
    await user.type(ttl, '10')
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')

    await user.click(screen.getByRole('button', { name: 'Discard Fonts changes' }))
    expect(ttl).toHaveValue(86400)
    expect(within(region('Fonts')).queryByText('Unsaved')).toBeNull()
    expect(screen.getByRole('button', { name: 'Save Fonts' })).toBeDisabled()
    expect(timeout).toHaveValue(45)
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })

  it('shows a refused value beside its field', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    // Queried inside the section, not across the whole page, which is slow (#906).
    const rendering = within(region('Rendering'))
    const field = rendering.getByLabelText('Editor checks at once')
    await user.clear(field)
    await user.type(field, '0')
    await user.click(rendering.getByRole('button', { name: 'Save Rendering' }))
    expect(await rendering.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(
      'SCADBUDDY_CHECK_CONCURRENCY must be at least 1',
    )
    expect(rendering.getByText('Unsaved')).toBeInTheDocument()
    // The alert's own wait is 5 s, so the test needs more than vitest's default 5 s (#906).
  }, 15_000)

  it('edits the upload limit in MB or GB and stores bytes', async () => {
    // The default is 1 GiB, shown as exactly that.
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const limit = screen.getByLabelText('Largest media upload')
    expect(limit).toHaveValue(1)
    expect(screen.getByLabelText('Unit for setting-media_upload_max_bytes')).toHaveValue('GiB')

    // Picking a unit converts the number; typing one reinterprets it in that unit.
    await user.selectOptions(screen.getByLabelText('Unit for setting-media_upload_max_bytes'), 'MB')
    expect(limit).toHaveValue(1073.742)
    await user.clear(limit)
    await user.type(limit, '500')
    await user.click(screen.getByRole('button', { name: 'Save Uploads' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ media_upload_max_bytes: 500_000_000 })
    put.mockRestore()
  })

  it('holds an in-app link while anything is unsaved', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route
            path="/settings"
            element={
              <>
                <Link to="/">Catalogue</Link>
                <SettingsPage />
              </>
            }
          />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </MemoryRouter>,
    )
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')

    await user.click(screen.getByRole('link', { name: 'Catalogue' }))
    const dialog = await screen.findByRole('dialog', { name: 'Leave without saving?' })
    expect(dialog).toHaveTextContent('Rendering')
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.queryByText('The catalogue')).toBeNull()
    expect(timeout).toHaveValue(45)

    await user.click(screen.getByRole('link', { name: 'Catalogue' }))
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
  })

  it('holds a programmatic navigation while anything is unsaved', async () => {
    function GoHome() {
      const navigate = useNavigate()
      return (
        <button type="button" onClick={() => void navigate('/')}>
          Go home
        </button>
      )
    }
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route
            path="/settings"
            element={
              <>
                <GoHome />
                <SettingsPage />
              </>
            }
          />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </MemoryRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    await user.click(screen.getByRole('button', { name: 'Go home' }))
    const dialog = await screen.findByRole('dialog', { name: 'Leave without saving?' })
    expect(screen.queryByText('The catalogue')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
  })

  it('holds the browser Back button while anything is unsaved', async () => {
    window.history.replaceState(null, '', '/')
    window.history.pushState(null, '', '/settings')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </BrowserRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    act(() => window.history.back())
    const dialog = await screen.findByRole('dialog', { name: 'Leave without saving?' })
    expect(window.location.pathname).toBe('/settings')
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)

    // Held again after staying, and this time let through.
    act(() => window.history.back())
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('holds a second Back pressed before the dialog is answered', async () => {
    window.history.replaceState(null, '', '/')
    window.history.pushState(null, '', '/elsewhere')
    window.history.pushState(null, '', '/settings')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/elsewhere" element={<p>Elsewhere</p>} />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </BrowserRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    act(() => window.history.back())
    await screen.findByRole('dialog', { name: 'Leave without saving?' })
    act(() => window.history.back())
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(window.location.pathname).toBe('/settings')
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)

    await user.click(screen.getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })
})

describe('SettingsPage secrets (#322)', () => {
  it('typing a key after Remove key replaces it instead of clearing it', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const key = screen.getByLabelText('API key')
    const remove = screen.getByRole('button', { name: 'Remove key' })
    await user.click(remove)
    expect(key).toHaveAttribute('placeholder', 'Cleared when you save.')
    expect(remove).toBeDisabled()

    await user.type(key, 'n')
    expect(remove).toBeEnabled()
    // Emptied again, the field is back to leaving the stored key alone.
    await user.clear(key)
    expect(key).toHaveAttribute('placeholder', 'A key is stored. Paste a new one to replace it.')
    expect(screen.getByRole('button', { name: 'Save Connection' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })
})

describe('SettingsPage remembered choices (#322)', () => {
  it('forgets one remembered choice and leaves the rest', async () => {
    setMockRemembered({
      modelChoices: { 'name-keychain': { tier: 'fine' }, gear: { tier: 'draft' } },
      printerBedTypes: { '1': 'Textured PEI Plate' },
    })
    const { user } = renderPage(<SettingsPage />)
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    expect(within(table).getAllByRole('row')).toHaveLength(4)

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for gear' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3))
    expect(within(table).queryByText('draft quality')).toBeNull()
    expect(within(table).getByText('fine quality')).toBeInTheDocument()
    expect(within(table).getByText('Textured PEI Plate')).toBeInTheDocument()

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for name-keychain' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2))
    expect((await api.getRemembered()).model_print_choices).toEqual({})
    expect((await api.getRemembered()).printer_bed_types).toEqual({ '1': 'Textured PEI Plate' })
  })

  it('forgets everything after a confirmation', async () => {
    setMockRemembered({ printerBedTypes: { '1': 'Textured PEI Plate' }, modelChoices: { gear: { tier: 'draft' } } })
    const { user } = renderPage(<SettingsPage />)
    await screen.findByRole('table', { name: 'Remembered choices' })
    await user.click(screen.getByRole('button', { name: 'Forget all' }))
    await user.click(screen.getByRole('button', { name: 'Yes, forget all' }))
    expect(await screen.findByText('Nothing is remembered yet.')).toBeInTheDocument()
  })
})

describe('SettingsPage connection and About (#322)', () => {
  it('lists every scope, the write scopes as not checked', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    const scopes = await screen.findByRole('list', { name: 'Scopes' })
    expect(within(scopes).getAllByRole('listitem')).toHaveLength(5)
    expect(scopes).toHaveTextContent('Read Status: granted.')
    expect(scopes).toHaveTextContent('Manage Queue: not checked.')
    expect(scopes).toHaveTextContent('Manage Archives (optional): not checked.')
  })

  it('says Bambuddy takes no finish photo, with a link to turn it on', async () => {
    renderPage(<SettingsPage />)
    const status = await screen.findByTestId('bambuddy-status')
    await waitFor(() => expect(status).toHaveTextContent('Finish photo in Bambuddy: off'))
    expect(within(status).getByRole('link', { name: 'Turn it on in Bambuddy' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/settings',
    )
  })

  it('shows the build and the values only the deployment sets', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const about = screen.getByTestId('about')
    expect(about).toHaveTextContent('v0.42.0 (abc1234)')
    expect(about).toHaveTextContent('OpenSCAD version 2026.09.28')
    await waitFor(() => expect(about).toHaveTextContent('1.2.5.6'))
    const table = screen.getByRole('table', { name: 'Deployment values' })
    expect(table).toHaveTextContent('SCADBUDDY_DATABASE_URL')
    expect(table).toHaveTextContent('postgres.scadbuddy.svc:5432/scadbuddy')
    expect(table).toHaveTextContent('SCADBUDDY_OPENSCAD')
    // Read-only: nothing here is an input.
    expect(within(table).queryByRole('textbox')).toBeNull()
  })
})

// #426 — the render key, the blob store choice and the blob store's usage, in #322's
// sections: the key beside the API key in Connection, the store beside the inbox folder it
// needs in Projects & files.
describe('SettingsPage blob store (#426)', () => {
  const stored = {
    ...mockSettings(),
    bambuddy_url: 'https://bambuddy.internal.nullreference.io',
    has_api_key: true,
    library_folder_id: 2,
    store_backend: 'local' as const,
  }

  function serve(settings: Settings) {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(settings)),
      http.put('/api/v1/settings', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        const { bambuddy_render_api_key: key, ...rest } = body
        return HttpResponse.json({
          ...settings,
          ...rest,
          ...(typeof key === 'string' ? { has_render_api_key: key !== '', render_key_fallback: key === '' } : {}),
        })
      }),
    )
    return bodies
  }

  it('warns while render workers would hold the full key', async () => {
    serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
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
    serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByTestId('render-key-fallback')).toBeNull()
    const key = screen.getByLabelText('Render key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('sends a typed render key with Connection and leaves the stored API key alone', async () => {
    const bodies = serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.type(screen.getByLabelText('Render key'), 'narrow')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: 'narrow' })
    await waitFor(() => expect(screen.queryByTestId('render-key-fallback')).toBeNull())
    expect(screen.getByLabelText('Render key')).toHaveValue('')
  })

  it('seeds the Blob store choice and sends a change with Projects & files', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const store = screen.getByLabelText('Blob store')
    expect(store).toHaveValue('local')
    expect(screen.getByRole('option', { name: /Bambuddy library/ })).toBeEnabled()
    await user.selectOptions(store, 'bambuddy')
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ store_backend: 'bambuddy' })
  })

  it('keeps a stored Bambuddy store backend on an unrelated save', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    const own = screen.getByLabelText(/ScadBuddy.s own URL/)
    await user.clear(own)
    await user.type(own, 'https://scadbuddy.test')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).not.toHaveProperty('store_backend')
    expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy')
  })

  it('falls back to the local store when the Bambuddy store loses its inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const store = screen.getByLabelText('Blob store')
    await waitFor(() => expect(store).toHaveValue('bambuddy'))
    await user.selectOptions(screen.getByLabelText(/Inbox folder/), '')
    expect(screen.getByRole('option', { name: /Bambuddy library/ })).toBeDisabled()
    expect(store).toHaveValue('local')
    await user.click(screen.getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: null, store_backend: 'local' })
  })

  it('falls back to the local store when a Connection save clears the Bambuddy URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    // The choice follows the saved URL, so it moves once Connection is saved.
    expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_url: null, store_backend: 'local' })
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('local'))
  })

  it('falls back to the local store when the Bambuddy URL is reset while on the Bambuddy store', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    await user.click(screen.getByRole('button', { name: 'Reset bambuddy_url to the deployment value' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'], store_backend: 'local' })
  })

  it('resets only the Bambuddy URL while on the local store', async () => {
    const bodies = serve(stored)
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(screen.getByRole('button', { name: 'Reset bambuddy_url to the deployment value' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'] })
  })

  it('keeps the Bambuddy store on a Connection save while an unsaved Projects edit drops the inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    await user.selectOptions(screen.getByLabelText(/Inbox folder/), '')
    const own = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(own)
    await user.type(own, 'https://mine.test')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ public_url: 'https://mine.test' })
  })

  it('keeps the Bambuddy store on a Projects save while an unsaved Connection edit clears the URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveValue('bambuddy'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    await user.selectOptions(screen.getByLabelText(/Inbox folder/), '3')
    await user.click(screen.getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: 3 })
  })

  it('removes a stored render key when Connection is saved', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const row = screen.getByLabelText('Render key').closest('div') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Remove key' }))
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Cleared when you save.')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: '' })
  })

  it('offers the Bambuddy store only once its URL is saved, then marks choosing it unsaved', async () => {
    serve({ ...stored, bambuddy_url: null })
    const { user } = renderPage(<SettingsPage />)
    // Saved: an inbox folder, no Bambuddy URL.
    const url = await screen.findByLabelText('Bambuddy URL')
    await waitFor(() => expect(screen.getByTestId('source-store_backend')).toBeInTheDocument())
    await user.type(url, 'https://bambuddy.new.test')
    const option = screen.getByRole('option', { name: /Bambuddy library/ })
    expect(option).toBeDisabled()
    expect(screen.getByTestId('store-backend-hint')).toHaveTextContent('Bambuddy URL is not saved yet')
    await user.click(screen.getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(option).toBeEnabled())
    expect(screen.queryByTestId('store-backend-hint')).toBeNull()
    await user.selectOptions(screen.getByLabelText('Blob store'), 'bambuddy')
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save Projects & files' })).toBeEnabled()
  })

  it('offers the Bambuddy store only once an inbox folder is chosen', async () => {
    serve({ ...stored, library_folder_id: null })
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByRole('option', { name: /Bambuddy library/ })).toBeDisabled()
  })

  it('shows what the blob store holds, where, against its caps', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'bambuddy',
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
          by_kind: { piece: 1024, asset: 1024 },
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('Bambuddy library')
    expect(within(usage).getByText('Files').nextElementSibling).toHaveTextContent(/^12 of 10000$/)
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
    expect(
      screen.getByText(
        'The Where row is the store this process uses; it moves to the Blob store choice above at its next restart, so the two can differ until then.',
      ),
    ).toBeInTheDocument()
  })

  it('says a store cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({ backend: 'local', by_kind: {}, count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('This server’s volume')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })
})

describe('SettingsPage Administration (#668)', () => {
  it('shows no Temporal UI link while its URL is empty', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const administration = region('Administration')
    expect(within(administration).getByLabelText('Temporal UI URL')).toHaveValue('')
    expect(within(administration).queryByRole('link', { name: /Temporal UI/ })).toBeNull()
  })

  it('links to the Temporal UI in a new tab once its URL is saved', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const administration = region('Administration')
    await user.type(within(administration).getByLabelText('Temporal UI URL'), 'https://temporal.lan')
    await user.click(screen.getByRole('button', { name: 'Save Administration' }))
    const link = await within(administration).findByRole('link', { name: /Temporal UI/ })
    expect(link).toHaveAttribute('href', 'https://temporal.lan')
    // A page that is not ScadBuddy's: a new tab, which also escapes Bambuddy's sandbox.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(mockSettings().temporal_ui_url).toBe('https://temporal.lan')
    expect(mockSettings().sources?.['temporal_ui_url']).toBe('stored')
  })

  it('links to a Temporal UI the deployment set', async () => {
    setMockSettings({
      ...mockSettings(),
      temporal_ui_url: 'https://temporal.env',
      sources: { ...mockSettings().sources, temporal_ui_url: 'env' },
    })
    renderPage(<SettingsPage />)
    await seeded()
    const link = await within(region('Administration')).findByRole('link', { name: /Temporal UI/ })
    expect(link).toHaveAttribute('href', 'https://temporal.env')
    expect(screen.getByTestId('source-temporal_ui_url')).toHaveTextContent('From SCADBUDDY_TEMPORAL_UI_URL')
  })
})
