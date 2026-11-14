import { act, screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Link, Route, Routes, useLocation } from 'react-router'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import type { Job } from '../api/types'
import { keychainSchema, models } from '../mocks/fixtures'
import { mockSettings, setMockPresets, setMockSettings } from '../mocks/handlers'
import { server } from '../mocks/server'
import { selectOption } from '../test/select'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// #1022 — `/m/<slug>?preset=<id>` opens the customizer with that preset applied.

// WebGL does not exist in jsdom; the page is under test here, not the viewer.
vi.mock('../components/Preview', () => ({
  Preview: ({ job }: { job?: Job }) => (
    <div data-testid="preview">{job?.bbox_mm && <span data-testid="bbox">rendered</span>}</div>
  ),
}))

const MUM = 'a1b2c3d4e5f60718293a4b5c6d7e8f90'

/** Reads back where the router is, so the page's own URL writes are observable. */
function Where() {
  const { pathname, search } = useLocation()
  return <div data-testid="where">{pathname + search}</div>
}

function render(route: string) {
  return renderPage(
    <Routes>
      <Route
        path="/m/:slug"
        element={
          <>
            <Where />
            <Link to="/m/name-keychain?preset=template-tiny">Open Tiny</Link>
            <CustomizePage />
          </>
        }
      />
    </Routes>,
    { route },
  )
}

function where(): string {
  return screen.getByTestId('where').textContent ?? ''
}

/** Every render request's params, so what was ASKED of the server can be asserted. */
function watchRenders(): Promise<{ inputs: { params: Record<string, unknown> } }>[] {
  const bodies: Promise<{ inputs: { params: Record<string, unknown> } }>[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/render')) {
      bodies.push(request.clone().json() as Promise<{ inputs: { params: Record<string, unknown> } }>)
    }
  })
  return bodies
}

async function picker() {
  const select = await screen.findByRole('combobox', { name: 'Preset' })
  await waitFor(() => expect(select).toBeEnabled())
  return select
}

describe('a link to a preset (#1022)', () => {
  it("opens on a saved preset's values, selected in the picker, and renders them", async () => {
    const renders = watchRenders()
    render(`/m/name-keychain?preset=${MUM}`)
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Mum'))
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
    // Applied, not changed: the picker does not call it modified.
    expect(screen.queryByTestId('preset-modified')).toBeNull()
    await waitFor(() => expect(renders.length).toBeGreaterThan(0), { timeout: 5_000 })
    const bodies = await Promise.all(renders)
    expect(bodies.at(-1)?.inputs.params).toMatchObject({ name: 'Mum', body_color: '#222222' })
    // Nothing else rendered first: the defaults never went out for a render.
    expect(bodies.every((body) => body.inputs.params['name'] === 'Mum')).toBe(true)
    expect(where()).toBe(`/m/name-keychain?preset=${MUM}`)
  })

  it('opens on a preset the template ships', async () => {
    render('/m/name-keychain?preset=template-tiny')
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Tiny'))
    expect(screen.getByRole('spinbutton', { name: 'Text size value' })).toHaveValue(10)
  })

  it('keeps the preset in the URL after a change, and shows the change', async () => {
    const { user } = render(`/m/name-keychain?preset=${MUM}`)
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Mum'))
    await user.type(screen.getByRole('textbox', { name: 'Name on the tag' }), 'my')
    expect(await screen.findByTestId('preset-modified')).toHaveTextContent('Changed from Mum')
    expect(where()).toBe(`/m/name-keychain?preset=${MUM}`)
  })

  it('follows the picker: a pick writes its id, No preset takes it out', async () => {
    const { user } = render('/m/name-keychain?x=1')
    const select = await picker()
    await selectOption(user, select, 'Tiny')
    await waitFor(() => expect(where()).toBe('/m/name-keychain?x=1&preset=template-tiny'))
    await selectOption(user, select, 'Mum')
    await waitFor(() => expect(where()).toBe(`/m/name-keychain?x=1&preset=${MUM}`))
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
    await selectOption(user, select, 'No preset')
    await waitFor(() => expect(where()).toBe('/m/name-keychain?x=1'))
    // Clearing the selection leaves the values on screen, as the picker always has.
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
  })

  it('applies a link followed on the page, and the same link again once it was let go', async () => {
    const { user } = render(`/m/name-keychain?preset=${MUM}`)
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Mum'))
    await user.click(screen.getByRole('link', { name: 'Open Tiny' }))
    await waitFor(() => expect(select).toHaveTextContent('Tiny'))
    expect(screen.getByRole('spinbutton', { name: 'Text size value' })).toHaveValue(10)
    await selectOption(user, select, 'No preset')
    await waitFor(() => expect(where()).toBe('/m/name-keychain'))
    await user.click(screen.getByRole('link', { name: 'Open Tiny' }))
    // With no preset selected the values on screen are no preset's, so it asks, as a
    // pick of Tiny in the picker would.
    const asked = await screen.findByRole('alertdialog', { name: 'Apply preset Tiny?' })
    await user.click(within(asked).getByRole('button', { name: 'Replace my changes' }))
    await waitFor(() => expect(select).toHaveTextContent('Tiny'))
    expect(where()).toBe('/m/name-keychain?preset=template-tiny')
  })

  it('says an unknown preset is not there and opens on the defaults', async () => {
    render('/m/name-keychain?preset=template-gone')
    expect(await screen.findByTestId('preset-link-missing')).toHaveTextContent(
      'The linked preset is not on this model',
    )
    const select = await picker()
    expect(select).not.toHaveTextContent('Tiny')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue(
      String(keychainSchema.parameters?.find((p) => p.name === 'name')?.initial),
    )
    // The URL no longer names a preset the page is not showing.
    await waitFor(() => expect(where()).toBe('/m/name-keychain'))
  })

  it('says so when the presets cannot be read, and opens on the defaults', async () => {
    server.use(
      http.get('/api/v1/models/:slug/presets', () =>
        HttpResponse.json({ title: 'Bad Gateway', status: 502, detail: 'injected' }, { status: 502 }),
      ),
    )
    render(`/m/name-keychain?preset=${MUM}`)
    expect(await screen.findByTestId('preset-link-missing')).toHaveTextContent('injected')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan')
  })

  it('stops waiting for presets that never come, and opens on the defaults', async () => {
    // Real time still passes, so the page's own waits run; the test advances the fakes.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let answer = () => {}
    const stalled = new Promise<void>((resolve) => {
      answer = resolve
    })
    onTestFinished(() => {
      answer()
      vi.useRealTimers()
    })
    const renders = watchRenders()
    server.use(
      http.get('/api/v1/models/:slug/presets', async () => {
        await stalled
        return HttpResponse.json([])
      }),
    )
    render(`/m/name-keychain?preset=${MUM}`)
    await screen.findByRole('textbox', { name: 'Name on the tag' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(await screen.findByTestId('preset-link-missing')).toHaveTextContent('did not answer in time')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan')
    // The defaults render once the link has been given up on.
    await waitFor(() => expect(renders.length).toBeGreaterThan(0), { timeout: 5_000 })
    await waitFor(() => expect(where()).toBe('/m/name-keychain'))
  })

  it('stops waiting for a migration that never answers, and opens on the defaults', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let answer = () => {}
    const stalled = new Promise<void>((resolve) => {
      answer = resolve
    })
    onTestFinished(() => {
      answer()
      vi.useRealTimers()
    })
    const renders = watchRenders()
    server.use(
      // A version no stored preset is at, so the linked one has to be migrated.
      http.get('/api/v1/models/name-keychain', () =>
        HttpResponse.json({ ...models.find((m) => m.slug === 'name-keychain'), inputs_version: 7 }),
      ),
      http.post('/api/v1/models/:slug/inputs/migrate', async () => {
        await stalled
        return HttpResponse.json({})
      }),
    )
    render(`/m/name-keychain?preset=${MUM}`)
    await screen.findByRole('textbox', { name: 'Name on the tag' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(await screen.findByTestId('preset-link-missing')).toHaveTextContent('did not answer in time')
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan')
    await waitFor(() => expect(renders.length).toBeGreaterThan(0), { timeout: 5_000 })
  })

  it('keeps a migration it gave up on from showing anything when it fails late', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let answer = () => {}
    const stalled = new Promise<void>((resolve) => {
      answer = resolve
    })
    onTestFinished(() => {
      answer()
      vi.useRealTimers()
    })
    // Set once msw has sent the 504: the failure is suppressed, so nothing on screen marks it.
    let answered = false
    const onAnswer = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname.endsWith('/inputs/migrate')) answered = true
    }
    server.events.on('response:mocked', onAnswer)
    onTestFinished(() => {
      server.events.removeListener('response:mocked', onAnswer)
    })
    server.use(
      http.get('/api/v1/models/name-keychain', () =>
        HttpResponse.json({ ...models.find((m) => m.slug === 'name-keychain'), inputs_version: 7 }),
      ),
      http.post('/api/v1/models/:slug/inputs/migrate', async () => {
        await stalled
        return HttpResponse.json({ title: 'Gateway Timeout', status: 504, detail: 'late' }, { status: 504 })
      }),
    )
    render(`/m/name-keychain?preset=${MUM}`)
    await screen.findByRole('textbox', { name: 'Name on the tag' })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(await screen.findByTestId('preset-link-missing')).toHaveTextContent('did not answer in time')
    // Now the migration fails: the link was given up on, so its inputs are not shown.
    answer()
    await waitFor(() => expect(answered).toBe(true))
    // Let the page take the answer in: the response body, then the migration's `then`.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.queryByRole('textbox', { name: 'Saved inputs' })).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Reagan')
  })

  it('applies a link on a reopened output without asking: nothing was edited', async () => {
    const renders = watchRenders()
    render(`/m/name-keychain?from=${'c'.repeat(32)}&preset=${MUM}`)
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Mum'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByRole('textbox', { name: 'Name on the tag' })).toHaveValue('Mum')
    await waitFor(() => expect(renders.length).toBeGreaterThan(0), { timeout: 5_000 })
    const bodies = await Promise.all(renders)
    expect(bodies.at(-1)?.inputs.params).toMatchObject({ name: 'Mum' })
  })

  it('asks before a link replaces unsaved edits, as a pick does', async () => {
    const { user } = render(`/m/name-keychain?preset=${MUM}`)
    const select = await picker()
    await waitFor(() => expect(select).toHaveTextContent('Mum'))
    const name = screen.getByRole('textbox', { name: 'Name on the tag' })
    await user.type(name, 'my')
    await screen.findByTestId('preset-modified')

    await user.click(screen.getByRole('link', { name: 'Open Tiny' }))
    const asked = await screen.findByRole('alertdialog', { name: 'Apply preset Tiny?' })
    expect(name).toHaveValue('Mummy')
    await user.click(within(asked).getByRole('button', { name: 'Cancel' }))
    // Declined: the edits stay, and the URL names the preset still selected.
    await waitFor(() => expect(where()).toBe(`/m/name-keychain?preset=${MUM}`))
    expect(name).toHaveValue('Mummy')
    expect(select).toHaveTextContent('Mum')
    expect(screen.queryByTestId('preset-link-missing')).toBeNull()

    await user.click(screen.getByRole('link', { name: 'Open Tiny' }))
    const again = await screen.findByRole('alertdialog', { name: 'Apply preset Tiny?' })
    await user.click(within(again).getByRole('button', { name: 'Replace my changes' }))
    await waitFor(() => expect(select).toHaveTextContent('Tiny'))
    expect(screen.getByRole('spinbutton', { name: 'Text size value' })).toHaveValue(10)
    expect(where()).toBe('/m/name-keychain?preset=template-tiny')
  })
})

describe('Copy link (#1022)', () => {
  function stubClipboard() {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    return writeText
  }

  it("copies the preset's link on this origin when no public URL is set", async () => {
    setMockSettings({ ...mockSettings(), public_url: null })
    const { user } = render('/m/name-keychain')
    const writeText = stubClipboard()
    const select = await picker()
    await selectOption(user, select, 'Mum')
    await user.click(screen.getByRole('button', { name: 'More for preset Mum' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Copy link to preset Mum' }))
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/m/name-keychain?preset=${MUM}`),
    )
    expect(await screen.findByText('Link copied.')).toBeInTheDocument()
  })

  it('takes the status away when the menu next opens', async () => {
    setMockSettings({ ...mockSettings(), public_url: null })
    const { user } = render('/m/name-keychain')
    stubClipboard()
    const select = await picker()
    await selectOption(user, select, 'Mum')
    await user.click(screen.getByRole('button', { name: 'More for preset Mum' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Copy link to preset Mum' }))
    expect(await screen.findByText('Link copied.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'More for preset Mum' }))
    await screen.findByRole('menuitem', { name: 'Copy link to preset Mum' })
    expect(screen.queryByTestId('preset-link-copy')).toBeNull()
  })

  // #6232 — the link is on the host the user is on, not the public URL's.
  it("uses the page's host, not the configured public URL", async () => {
    setMockSettings({ ...mockSettings(), public_url: 'https://scadbuddy.internal.nullreference.io/' })
    vi.stubGlobal('location', new URL('https://scadbuddy.sso.nullreference.io/m/name-keychain'))
    onTestFinished(() => {
      vi.unstubAllGlobals()
    })
    const { user } = render('/m/name-keychain')
    const writeText = stubClipboard()
    const select = await picker()
    await selectOption(user, select, 'Tiny')
    await user.click(screen.getByRole('button', { name: 'More for preset Tiny' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Copy link to preset Tiny' }))
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        'https://scadbuddy.sso.nullreference.io/m/name-keychain?preset=template-tiny',
      ),
    )
  })

  it('offers the link to copy by hand where the clipboard is refused', async () => {
    setMockPresets('name-keychain', [
      { id: 'template-tiny', name: 'Tiny', origin: 'template', params: {}, description: '', tags: [] },
    ])
    setMockSettings({ ...mockSettings(), public_url: null })
    const { user } = render('/m/name-keychain')
    vi.stubGlobal('navigator', {
      ...navigator,
      clipboard: { writeText: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')) },
    })
    const select = await picker()
    await selectOption(user, select, 'Tiny')
    await user.click(screen.getByRole('button', { name: 'More for preset Tiny' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Copy link to preset Tiny' }))
    const status = await screen.findByTestId('preset-link-copy')
    expect(within(status).getByText(`${window.location.origin}/m/name-keychain?preset=template-tiny`)).toBeInTheDocument()
    expect(status).toHaveTextContent('Copy this link')
  })
})
