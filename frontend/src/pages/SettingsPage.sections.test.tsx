import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BrowserRouter, MemoryRouter, Link, Route, Routes, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { restartMockBackend } from '../mocks/features/settings'
import { mockSettings, setMockRemembered, setMockSettings } from '../mocks/handlers'
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
