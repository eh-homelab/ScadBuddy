import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { BUILTIN_SLUG, models } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { COPY, UPSTREAM, duplicateWithUpdate } from '../test/upstream'
import { renderPage } from '../test/utils'
import { CataloguePage } from './CataloguePage'

describe('CataloguePage', () => {
  it('lists every model with its tags and when it last changed', async () => {
    renderPage(<CataloguePage />)

    const keychain = await screen.findByRole('heading', { name: 'Name Keychain' })
    const card = keychain.closest('li') as HTMLElement
    expect(within(card).getByText('keychain')).toBeInTheDocument()
    expect(within(card).getByText(/^Updated /)).toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Gridfinity Bin' })).toBeInTheDocument()
  })

  it('shows an empty build plate for a model with no thumbnail', async () => {
    renderPage(<CataloguePage />)
    const gridfinity = await screen.findByRole('heading', { name: 'Gridfinity Bin' })
    const card = gridfinity.closest('li') as HTMLElement
    expect(
      within(card).getByRole('img', { name: 'Gridfinity Bin — not generated yet' }),
    ).toBeInTheDocument()
  })

  it('links each card at the customizer', async () => {
    renderPage(<CataloguePage />)
    const link = await screen.findByRole('link', { name: /Name Keychain/ })
    expect(link).toHaveAttribute('href', '/m/name-keychain')
  })

  it('points at the models folder when there is nothing to show', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.json([])))
    renderPage(<CataloguePage />)

    expect(await screen.findByRole('heading', { name: 'No models yet' })).toBeInTheDocument()
    expect(screen.getByText('models/')).toBeInTheDocument()
  })

  it('reports a failed catalogue load and offers a retry', async () => {
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json({ title: 'Data directory is unreadable', status: 500 }, { status: 500 }),
      ),
    )
    renderPage(<CataloguePage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Data directory is unreadable')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('uploads a chosen .scad file and closes the dialog', async () => {
    // The multipart POST itself is exercised by the Playwright smoke test: jsdom's
    // Blob/File and Node's fetch cannot agree on a multipart body, which fails
    // inside undici rather than in any ScadBuddy code.
    const uploaded = { ...(models[0] as (typeof models)[number]), slug: 'vase-mode', name: 'Vase Mode' }
    const upload = vi.spyOn(api, 'uploadModel').mockResolvedValue(uploaded)

    const { user } = renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Add model' }))
    const dialog = screen.getByRole('dialog')
    await user.upload(
      within(dialog).getByLabelText('OpenSCAD source file'),
      new File(['cube(10);'], 'Vase Mode.scad', { type: 'text/plain' }),
    )
    expect(within(dialog).getByText('Vase Mode.scad')).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Add model' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(upload).toHaveBeenCalledOnce()
    expect(upload.mock.calls[0]?.[0].name).toBe('Vase Mode.scad')
    upload.mockRestore()
  })

  it('refuses anything that is not a .scad file', async () => {
    const { user } = renderPage(<CataloguePage />, {
      userEventOptions: { applyAccept: false },
    })
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Add model' }))
    await user.upload(
      screen.getByLabelText('OpenSCAD source file'),
      new File(['x'], 'model.stl', { type: 'model/stl' }),
    )

    expect(screen.getByRole('alert')).toHaveTextContent('not a .scad file')
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('button', { name: 'Add model' })).toBeDisabled()
  })

  it('imports a model from a URL and opens it', async () => {
    const { user } = renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Import from URL' }))
    const dialog = screen.getByRole('dialog')
    await user.type(
      within(dialog).getByLabelText('URL'),
      'https://raw.githubusercontent.com/someone/models/main/Vase%20Mode.scad',
    )
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps the dialog open with the reason when a URL cannot be imported', async () => {
    const { user } = renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await user.click(screen.getByRole('button', { name: 'Import from URL' }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByLabelText('URL'), 'https://makerworld.com/en/models/1398039')
    await user.click(within(dialog).getByRole('button', { name: 'Import' }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('MakerWorld')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('offers an import from the empty state too', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.json([])))
    renderPage(<CataloguePage />)

    await screen.findByRole('heading', { name: 'No models yet' })
    expect(screen.getAllByRole('button', { name: 'Import from URL' })).toHaveLength(2)
  })

  it('refetches a thumbnail whose fallback moved to another output with no new commit', async () => {
    const base = { ...(models[0] as (typeof models)[number]), version: 'a'.repeat(40) }
    let record: typeof base = { ...base, thumbnail_source: 'output', thumbnail_output_id: 'f'.repeat(32) }
    server.use(http.get('/api/v1/models', () => HttpResponse.json([record])))
    const imageOf = async () =>
      (await screen.findByRole('img', { name: 'Name Keychain' })).getAttribute('src')

    const first = renderPage(<CataloguePage />)
    const before = await imageOf()
    first.unmount()

    // Same revision, a different covering output: the URL must still change.
    record = { ...base, thumbnail_source: 'output', thumbnail_output_id: 'e'.repeat(32) }
    const second = renderPage(<CataloguePage />)
    const moved = await imageOf()
    second.unmount()
    expect(moved).not.toBe(before)

    // Same revision again, the model's own image now: different again.
    record = { ...base, thumbnail_source: 'model', thumbnail_output_id: null }
    renderPage(<CataloguePage />)
    const own = await imageOf()
    expect(new Set([before, moved, own]).size).toBe(3)
    expect(own).toContain(`/api/v1/models/name-keychain/thumbnail?v=`)
  })

  it('shows a model created without a thumbnail by its preview once that has rendered', async () => {
    // The mock answers the create first and has the preview on the next read, as
    // the backend's background render does.
    const created = await api.createModelFromSource({
      name: 'Fresh Widget',
      source: 'cube(1);\n',
      description: '',
      force: false,
    })
    expect(created.has_thumbnail).toBe(false)

    renderPage(<CataloguePage />)

    const image = await screen.findByRole('img', { name: 'Fresh Widget' })
    const preview = (await api.getModel(created.slug)).thumbnail_preview_id ?? ''
    expect(preview).not.toBe('')
    expect(image.getAttribute('src')).toContain(preview)
  })

  it('refetches a default-render preview re-rendered after a source edit', async () => {
    const base = {
      ...(models[0] as (typeof models)[number]),
      version: 'a'.repeat(40),
      has_thumbnail: true,
      thumbnail_source: 'preview' as const,
    }
    let record = { ...base, thumbnail_preview_id: '1'.repeat(16) }
    server.use(http.get('/api/v1/models', () => HttpResponse.json([record])))
    const imageOf = async () =>
      (await screen.findByRole('img', { name: 'Name Keychain' })).getAttribute('src')

    const first = renderPage(<CataloguePage />)
    const before = await imageOf()
    first.unmount()

    // The same revision key otherwise: only the preview's id says it was re-rendered.
    record = { ...base, thumbnail_preview_id: '2'.repeat(16) }
    renderPage(<CataloguePage />)
    expect(await imageOf()).not.toBe(before)
  })

  it('links an imported model back to where it came from', async () => {
    const origin = 'https://raw.githubusercontent.com/someone/models/main/bin.scad'
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json([{ ...(models[0] as (typeof models)[number]), origin_url: origin }]),
      ),
    )
    renderPage(<CataloguePage />)

    const link = await screen.findByRole('link', { name: /raw\.githubusercontent\.com/ })
    expect(link).toHaveAttribute('href', origin)
    expect(link).toHaveAttribute('target', '_blank')
  })

  it.each([
    'javascript:alert(document.domain)',
    'JAVASCRIPT:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'not a url',
  ])('links no origin that is not http(s): %s', async (origin) => {
    server.use(
      http.get('/api/v1/models', () =>
        HttpResponse.json([{ ...(models[0] as (typeof models)[number]), origin_url: origin }]),
      ),
    )
    renderPage(<CataloguePage />)

    const heading = await screen.findByRole('heading', { name: 'Name Keychain' })
    const card = heading.closest('li') as HTMLElement
    expect(within(card).queryByText(/^From/)).not.toBeInTheDocument()
    // The card's own link is the only anchor, and it points into the app.
    for (const anchor of card.querySelectorAll('a')) {
      expect(anchor.getAttribute('href')).toBe('/m/name-keychain')
    }
  })

  it('marks a built-in template read-only and links it with its id encoded (#184)', async () => {
    renderPage(<CataloguePage />)
    const builtin = (await screen.findByRole('heading', { name: 'Keychain Template' })).closest(
      'li',
    ) as HTMLElement
    expect(within(builtin).getByTestId('builtin-badge')).toHaveTextContent(
      'Built-in template — read-only',
    )
    expect(within(builtin).getByRole('link')).toHaveAttribute(
      'href',
      `/m/${encodeURIComponent(BUILTIN_SLUG)}`,
    )

    const mine = screen.getByRole('heading', { name: 'Name Keychain' }).closest('li') as HTMLElement
    expect(within(mine).queryByTestId('builtin-badge')).not.toBeInTheDocument()
  })

  it('duplicates a built-in from its card and opens the copy (#159)', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/" element={<CataloguePage />} />
        <Route path="/m/:slug" element={<p>Customizer</p>} />
      </Routes>,
    )
    const builtin = (await screen.findByRole('heading', { name: 'Keychain Template' })).closest(
      'li',
    ) as HTMLElement

    await user.click(within(builtin).getByRole('button', { name: 'Duplicate' }))
    const dialog = screen.getByRole('dialog', { name: 'Duplicate Keychain Template' })
    await user.click(within(dialog).getByRole('button', { name: 'Duplicate' }))

    expect(await screen.findByText('Customizer')).toBeInTheDocument()
    expect((await api.getModel('keychain-template-copy')).upstream?.id).toBe(BUILTIN_SLUG)
  })

  it('offers Duplicate on every card', async () => {
    renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })
    expect(screen.getAllByRole('button', { name: 'Duplicate' })).toHaveLength(models.length)
  })

  it('says what a duplicate was duplicated from, linked by name (#159)', async () => {
    await api.duplicateModel(BUILTIN_SLUG, 'My Keychain')
    renderPage(<CataloguePage />)

    const copy = (await screen.findByRole('heading', { name: 'My Keychain' })).closest(
      'li',
    ) as HTMLElement
    expect(within(copy).getByTestId('duplicated-from')).toHaveTextContent(
      'Duplicated from Keychain Template',
    )
    expect(within(copy).getByRole('link', { name: 'Keychain Template' })).toHaveAttribute(
      'href',
      `/m/${encodeURIComponent(BUILTIN_SLUG)}`,
    )
  })

  it('badges a duplicate whose upstream has an update (#160)', async () => {
    await duplicateWithUpdate()
    renderPage(<CataloguePage />)

    const copy = (await screen.findByRole('heading', { name: 'Keychain for Nova' })).closest(
      'li',
    ) as HTMLElement
    expect(within(copy).getByTestId('update-badge')).toHaveTextContent('Update available')
    expect(screen.getAllByTestId('update-badge')).toHaveLength(1)
  })

  it('says so on a duplicate whose upstream is gone (#160)', async () => {
    await api.duplicateModel(UPSTREAM, 'Keychain for Nova')
    await api.deleteModel(UPSTREAM, true)
    renderPage(<CataloguePage />)

    const copy = (await screen.findByRole('heading', { name: 'Keychain for Nova' })).closest(
      'li',
    ) as HTMLElement
    expect(within(copy).getByTestId('upstream-gone')).toHaveTextContent('Upstream gone')
    expect((await api.getModel(COPY)).upstream_state).toBe('gone')
  })
})
