import { act, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { restartMockBackend } from '../../../mocks/features/settings'
import { choose } from '../../../test/select'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Connection } from './connection'
import { Component as Fonts } from './fonts'
import { Component as Preview } from './preview'
import { Component as Rendering } from './rendering'

const route = '/settings/library'

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

/** The section renders once the settings are loaded. */
async function loaded() {
  await screen.findByLabelText('Render timeout')
}

describe('Rendering, field checks (#2347, #2348)', () => {
  it('refuses a negative or fractional number before sending, and ties the error to the field', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Rendering />, { route })
    await loaded()
    const timeout = screen.getByLabelText('Render timeout')
    const renders = screen.getByLabelText('Renders at once')
    // The help is read with the field, before any error.
    const helpId = timeout.getAttribute('aria-describedby')
    expect(helpId).toBe('setting-render_timeout-help')
    expect(document.getElementById(helpId!)).toBeInTheDocument()
    expect(timeout).not.toHaveAttribute('aria-invalid')

    await user.clear(timeout)
    await enter(user, timeout, '-5')
    await user.clear(renders)
    await enter(user, renders, '1.5')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))

    expect(await screen.findByText('Enter 0 or more.')).toBeInTheDocument()
    expect(screen.getByText('Enter a whole number.')).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
    expect(timeout).toHaveAttribute('aria-invalid', 'true')
    expect(timeout).toHaveAttribute('aria-describedby', 'setting-render_timeout-help setting-render_timeout-error')
    expect(document.getElementById('setting-render_timeout-error')).toHaveTextContent('Enter 0 or more.')
    // The help stays beside the error.
    expect(document.getElementById('setting-render_timeout-help')).toBeInTheDocument()
    put.mockRestore()
  })
})

describe('Settings sources (#322)', () => {
  it('badges each field with where its value comes from', async () => {
    renderSettingsSection(
      <>
        <Connection />
        <Rendering />
        <Fonts />
        <Preview />
      </>,
      { route },
    )
    await seeded()
    expect(screen.getByTestId('source-bambuddy_url')).toHaveTextContent('Set here')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL')
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('From SCADBUDDY_RENDER_TIMEOUT')
    expect(screen.getByTestId('source-default_plate')).toHaveTextContent('Default')
    const cleared = screen.getByTestId('source-google_fonts_api_key')
    expect(cleared).toHaveTextContent('Cleared')
    expect(cleared).toHaveTextContent('SCADBUDDY_GOOGLE_FONTS_API_KEY is ignored')
    // Only a stored or cleared value has something to reset.
    expect(within(cleared).getByRole('button', { name: 'Reset to deployment value: Google Fonts API key' })).toBeInTheDocument()
    expect(within(screen.getByTestId('source-public_url')).queryByRole('button')).toBeNull()
  })

  it('resets a saved value back to the deployment value', async () => {
    const { user } = renderSettingsSection(<Connection />, { route })
    await seeded()
    // Each keystroke re-renders the section, and a role query over all of it runs jsdom's
    // getComputedStyle on every element (#1485). So the value is pasted, and the buttons
    // are looked for in their section and badge. Reset is the point here.
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await user.click(field)
    await user.paste('https://mine.test')
    await user.click(within(field.closest('section')!).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    const put = vi.spyOn(api, 'putSettings')
    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL'))
    expect(put).toHaveBeenCalledWith({ reset: ['public_url'] })
    await waitFor(() => expect(field).toHaveValue('https://scadbuddy.internal.nullreference.io'))
    put.mockRestore()
  })

  it('resets a cleared key so the deployment one applies again', async () => {
    const { user } = renderSettingsSection(<Fonts />, { route })
    const cleared = await screen.findByTestId('source-google_fonts_api_key')
    await user.click(within(cleared).getByRole('button', { name: 'Reset to deployment value: Google Fonts API key' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-google_fonts_api_key')).toHaveTextContent('From SCADBUDDY_GOOGLE_FONTS_API_KEY'),
    )
    expect(screen.getByLabelText('Google Fonts API key')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('A key is stored'),
    )
  })

  it('says a restart-only field waits for a restart once saved', async () => {
    const { user } = renderSettingsSection(<Rendering />, { route })
    await loaded()
    expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Applies on restart')
    const field = screen.getByLabelText('Renders at once')
    await user.clear(field)
    await user.type(field, '4')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Saved; restart ScadBuddy to apply'),
    )
    restartMockBackend()
  })
})

describe('Settings sections (#322)', () => {
  it('saves one section and leaves another one’s edits unsaved', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(
      <>
        <Connection />
        <Rendering />
        <Preview />
      </>,
      { route },
    )
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await choose(user, screen.getByLabelText('Show dimensions in'), 'Inches (in)')
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()

    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // Only what changed in that section: the rest of the page is not posted back.
    expect(put.mock.calls[0]?.[0]).toEqual({ render_timeout: 45 })
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here')
    // The other section kept its edit.
    expect(screen.getByLabelText('Show dimensions in')).toHaveTextContent('Inches (in)')
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it('keeps an edit typed while its section is saving (#767)', async () => {
    // Held open so the edit lands mid-save; the save's seed must not take it back.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      await held
      return saved
    })
    const { user } = renderSettingsSection(<Rendering />, { route })
    await loaded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it('keeps that edit when another section saves while the first is in flight (#767)', async () => {
    // Rendering's save is held; Preview's goes straight through. Preview's request must
    // not erase the edit Rendering's answer has to leave alone.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('render_timeout' in body) await held
      return saved
    })
    const { user } = renderSettingsSection(
      <>
        <Rendering />
        <Preview />
      </>,
      { route },
    )
    await loaded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')
    await choose(user, screen.getByLabelText('Show dimensions in'), 'Inches (in)')
    await user.click(within(region('Preview')).getByRole('button', { name: 'Save Preview' }))
    await waitFor(() => expect(within(region('Preview')).queryByText('Unsaved')).toBeNull())

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    put.mockRestore()
  })

  it('discards one section’s edits', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(
      <>
        <Rendering />
        <Fonts />
      </>,
      { route },
    )
    await loaded()

    const ttl = screen.getByLabelText('Refresh the font catalogue after')
    await user.clear(ttl)
    await user.type(ttl, '10')
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')

    await user.click(within(region('Fonts')).getByRole('button', { name: 'Discard Fonts changes' }))
    expect(ttl).toHaveValue(86400)
    expect(within(region('Fonts')).queryByText('Unsaved')).toBeNull()
    expect(within(region('Fonts')).getByRole('button', { name: 'Save Fonts' })).toBeDisabled()
    expect(timeout).toHaveValue(45)
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })

  it('shows a refused value beside its field', async () => {
    const { user } = renderSettingsSection(<Rendering />, { route })
    await loaded()
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
})
