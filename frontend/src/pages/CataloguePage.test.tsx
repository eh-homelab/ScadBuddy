import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { Link, Route, Routes, useLocation, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { toSlides } from '../components/media/slides'
import { BUILTIN_SLUG, GALLERY_SLUG, media, models } from '../mocks/fixtures'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { intersect } from '../test/intersection'
import { COPY, UPSTREAM, duplicateWithUpdate } from '../test/upstream'
import { renderPage } from '../test/utils'
import { CataloguePage } from './CataloguePage'

function Search() {
  const { search } = useLocation()
  return <p data-testid="search">{search}</p>
}

/** The browser's Back and Forward buttons. */
function History() {
  const navigate = useNavigate()
  return (
    <>
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
      <button type="button" onClick={() => void navigate(1)}>
        Forward
      </button>
    </>
  )
}

/** In-app navigation to the catalogue while it stays mounted: the Models tab, the agent. */
function AppNav() {
  const navigate = useNavigate()
  return (
    <>
      <Link to="/">Models</Link>
      <button type="button" onClick={() => void navigate('/?tag=keychain')}>
        Agent navigate
      </button>
    </>
  )
}

function renderCatalogue(route = '/') {
  return renderPage(
    <>
      <CataloguePage />
      <Search />
      <History />
      <AppNav />
    </>,
    { route },
  )
}

function names(): string[] {
  return screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent ?? '')
}

describe('CataloguePage filters (#276)', () => {
  it('filters by a debounced search, folding accents, and puts it in the URL', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    await user.type(screen.getByRole('searchbox', { name: 'Search models' }), 'CREME')
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent('?q=CREME'))
    expect(names()).toEqual(['Crème Coaster'])
    expect(screen.getByTestId('result-count')).toHaveTextContent('1 of 4')
  })

  it('adds a card tag to the filter from the URL-encoded chip', async () => {
    const { user } = renderCatalogue()
    const coaster = (await screen.findByRole('heading', { name: 'Crème Coaster' })).closest(
      'li',
    ) as HTMLElement

    await user.click(within(coaster).getByRole('button', { name: 'Filter by Tea & Coffee' }))
    expect(screen.getByTestId('search')).toHaveTextContent('?tag=Tea+%26+Coffee')
    expect(names()).toEqual(['Crème Coaster'])
    expect(screen.getByRole('button', { name: 'Tea & Coffee 1' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // The chip is a button beside the card's link, not inside it.
    expect(within(coaster).getByRole('link').contains(within(coaster).getByRole('button', {
      name: 'Filter by kitchen',
    }))).toBe(false)
  })

  it('reads a deep link: several tags, origin and sort', async () => {
    renderCatalogue('/?tag=keychain&origin=builtin')
    expect(await screen.findByRole('heading', { name: 'Keychain Template' })).toBeInTheDocument()
    expect(names()).toEqual(['Keychain Template'])
    expect(screen.getByTestId('result-count')).toHaveTextContent('1 of 4')
  })

  it('sorts by name', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    expect(names()[0]).toBe('Name Keychain')

    await user.selectOptions(screen.getByRole('combobox', { name: 'Sort' }), 'Name')
    expect(screen.getByTestId('search')).toHaveTextContent('?sort=name')
    expect(names()).toEqual(['Crème Coaster', 'Gridfinity Bin', 'Keychain Template', 'Name Keychain'])
  })

  it('says when nothing matches, apart from an empty catalogue, and clears back', async () => {
    const { user } = renderCatalogue('/?q=nothing-like-this&sort=name')
    expect(await screen.findByRole('heading', { name: 'No models match' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'No models yet' })).not.toBeInTheDocument()

    await user.click(screen.getAllByRole('button', { name: 'Clear filters' })[0] as HTMLElement)
    expect(screen.getByTestId('search')).toHaveTextContent('?sort=name')
    expect(screen.getByRole('searchbox')).toHaveValue('')
    expect(names()).toHaveLength(4)
  })

  it('counts tags over the models the other filters leave, so no chip is a dead end', async () => {
    const { user } = renderCatalogue()
    const tags = within(await screen.findByRole('group', { name: 'Tags' }))
    expect(tags.getByRole('button', { name: 'template 1' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Mine' }))
    expect(tags.queryByRole('button', { name: /^template/ })).not.toBeInTheDocument()

    await user.type(screen.getByRole('searchbox', { name: 'Search models' }), 'coaster')
    await waitFor(() => expect(names()).toEqual(['Crème Coaster']))
    expect(tags.getByRole('button', { name: 'kitchen 1' })).toBeInTheDocument()
    expect(tags.queryByRole('button', { name: /^keychain/ })).not.toBeInTheDocument()
  })

  it('keeps a selected tag that nothing matches any more, so it can be unselected', async () => {
    const { user } = renderCatalogue('/?tag=template&origin=mine')
    expect(await screen.findByRole('heading', { name: 'No models match' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'template 0' }))
    expect(screen.getByTestId('search')).toHaveTextContent('?origin=mine')
  })

  it('shows no filters over an empty catalogue', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.json([])))
    renderCatalogue()
    await screen.findByRole('heading', { name: 'No models yet' })
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  })
})

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
    // Covered by an output: no thumbnail.png, so no media and the card shows the fallback.
    const base = { ...(models[0] as (typeof models)[number]), version: 'a'.repeat(40), media: [] }
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
      media: [],
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

describe('CataloguePage cards (#277)', () => {
  function renderWithRoutes() {
    return renderPage(
      <Routes>
        <Route
          path="/"
          element={
            <>
              <CataloguePage />
              <Search />
            </>
          }
        />
        <Route path="/m/:slug" element={<p>Customizer</p>} />
      </Routes>,
    )
  }

  /** The coaster's card, scrolled near enough to have mounted its carousel. */
  async function coasterCard() {
    const card = (await screen.findByRole('heading', { name: 'Crème Coaster' })).closest(
      'li',
    ) as HTMLElement
    intersect(card)
    return card
  }

  it('browses a card inline: next changes the slide, stays here and opens nothing', async () => {
    const { user } = renderWithRoutes()
    const card = await coasterCard()
    const carousel = within(card).getByRole('region', { name: 'Crème Coaster' })
    expect(within(carousel).getByTestId('carousel-position')).toHaveTextContent('1 of 4')

    await user.click(within(card).getByRole('button', { name: 'Next slide' }))
    await user.click(within(card).getByRole('button', { name: 'Next slide' }))

    expect(within(carousel).getByTestId('carousel-position')).toHaveTextContent('3 of 4')
    // An uncaptioned slide is named after the template.
    expect(
      within(card).getByRole('button', { name: 'Open Crème Coaster, image 3 of 4' }),
    ).toHaveAttribute('tabindex', '0')
    expect(screen.queryByText('Customizer')).not.toBeInTheDocument()
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=cards$/)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens the lightbox at the media clicked', async () => {
    const { user } = renderWithRoutes()
    const card = await coasterCard()

    await user.click(within(card).getByRole('button', { name: 'Next slide' }))
    await user.click(within(card).getByRole('button', { name: 'Open The raised rim' }))

    const dialog = await screen.findByRole('dialog', {}, { timeout: 3000 })
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'src',
        `/api/v1/models/${GALLERY_SLUG}/media/b2c3d4e5f6a1`,
      ),
    )
    expect(dialog).toHaveTextContent('The raised rim')
    expect(screen.queryByText('Customizer')).not.toBeInTheDocument()
  })

  it('links the title, with the media outside the link', async () => {
    const { user } = renderWithRoutes()
    const card = await coasterCard()
    const link = within(card).getByRole('link', { name: 'Crème Coaster' })
    expect(link).toHaveAttribute('href', `/m/${GALLERY_SLUG}`)
    expect(link.contains(within(card).getByRole('region', { name: 'Crème Coaster' }))).toBe(false)

    await user.click(link)
    expect(await screen.findByText('Customizer')).toBeInTheDocument()
  })

  it('tabs from one card into the next, with a bounded set of stops per card', async () => {
    const { user } = renderWithRoutes()
    const card = await coasterCard()
    // Sorted by updated: the keychain comes just before the coaster, the bin after it.
    const keychain = screen.getByRole('heading', { name: 'Name Keychain' }).closest(
      'li',
    ) as HTMLElement
    within(keychain).getByRole('button', { name: 'Duplicate' }).focus()

    const stops: string[] = []
    for (;;) {
      await user.tab()
      const focused = document.activeElement as HTMLElement
      if (!card.contains(focused)) break
      stops.push(focused.getAttribute('aria-label') ?? focused.textContent ?? '')
    }

    // The carousel, its visible media, the enabled arrow and only the current dot:
    // not one stop per slide.
    expect(stops).toEqual([
      'Crème Coaster',
      'Open Printed in blue and orange',
      'Next slide',
      'Go to slide 1',
      'Crème Coaster',
      'Filter by kitchen',
      'Filter by Tea & Coffee',
      'Duplicate',
    ])
    const bin = screen.getByRole('heading', { name: 'Gridfinity Bin' }).closest('li') as HTMLElement
    expect(document.activeElement).toBe(within(bin).getByRole('link', { name: 'Gridfinity Bin' }))
  })

  it('shows the fallback, with no carousel chrome, for a template with no media', async () => {
    renderWithRoutes()
    const bin = (await screen.findByRole('heading', { name: 'Gridfinity Bin' })).closest(
      'li',
    ) as HTMLElement
    expect(
      within(bin).getByRole('img', { name: 'Gridfinity Bin — not generated yet' }),
    ).toBeInTheDocument()
    expect(within(bin).queryByRole('region')).not.toBeInTheDocument()
    expect(within(bin).queryByRole('button', { name: /slide|^Open / })).not.toBeInTheDocument()

    // The built-in has no media, so its default-render preview stands in.
    const builtin = screen.getByRole('heading', { name: 'Keychain Template' }).closest(
      'li',
    ) as HTMLElement
    expect(within(builtin).getByRole('img', { name: 'Keychain Template' })).toHaveAttribute(
      'src',
      expect.stringContaining('/thumbnail'),
    )
  })

  it('shows a legacy thumbnail as the one slide, which opens the lightbox', async () => {
    const { user } = renderWithRoutes()
    const keychain = (await screen.findByRole('heading', { name: 'Name Keychain' })).closest(
      'li',
    ) as HTMLElement
    expect(within(keychain).queryByRole('button', { name: 'Next slide' })).not.toBeInTheDocument()

    await user.click(within(keychain).getByRole('button', { name: 'Open Name Keychain' }))
    expect(await screen.findByRole('dialog', {}, { timeout: 3000 })).toBeInTheDocument()
  })
})

describe('CataloguePage, live (#269)', () => {
  it('shows a model created elsewhere, and drops one deleted elsewhere', async () => {
    renderPage(<CataloguePage />)
    await screen.findByRole('heading', { name: 'Name Keychain' })

    await api.createModelFromSource({
      name: 'Made By An Agent',
      description: '',
      source: 'cube(1);\n',
      force: false,
    })
    emitRealtime('model.created', ['models'], { slug: 'made-by-an-agent' })
    expect(await screen.findByRole('heading', { name: 'Made By An Agent' })).toBeInTheDocument()

    await api.deleteModel('made-by-an-agent')
    emitRealtime('model.deleted', ['models'], { slug: 'made-by-an-agent' })
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Made By An Agent' })).not.toBeInTheDocument(),
    )
  })
})

describe('CataloguePage list mode (#278)', () => {
  function rows(): HTMLElement[] {
    return [...document.querySelectorAll<HTMLElement>('[data-model-row]')]
  }

  function rowOf(name: string): HTMLElement {
    return screen.getByRole('heading', { name }).closest('[data-model-row]') as HTMLElement
  }

  it('shows cards for a URL with no view, and writes the view into it', async () => {
    const { user } = renderCatalogue('/?tag=keychain')
    await screen.findByRole('heading', { name: 'Name Keychain' })
    const view = screen.getByRole('group', { name: 'View' })
    expect(within(view).getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true')
    expect(rows()).toHaveLength(0)
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?tag=keychain&view=cards$/)

    // Replaced, not pushed: List then Back returns to the normalized entry.
    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?tag=keychain&view=list$/)
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?tag=keychain&view=cards$/)
    expect(rows()).toHaveLength(0)
  })

  it('treats an unknown view as cards and rewrites it', async () => {
    renderCatalogue('/?view=grid')
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=cards$/)
    expect(rows()).toHaveLength(0)
  })

  it('switches between List and Cards, naming each in the URL', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=list$/)
    expect(rows()).toHaveLength(4)

    await user.click(screen.getByRole('button', { name: 'Cards' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=cards$/)
    expect(rows()).toHaveLength(0)
  })

  it('shows what the URL says on in-app navigation to the catalogue', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(rows()).toHaveLength(4)

    // The agent's `navigate` and the Models tab name no view, so they show Cards.
    await user.click(screen.getByRole('button', { name: 'Agent navigate' }))
    await waitFor(() =>
      expect(screen.getByTestId('search')).toHaveTextContent(/^\?tag=keychain&view=cards$/),
    )
    expect(rows()).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: 'List' }))
    await user.click(screen.getByRole('link', { name: 'Models' }))
    await waitFor(() => expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=cards$/))
    expect(screen.getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('keeps List on Back from a model', async () => {
    // The real route topology: the catalogue and the customizer are sibling routes, so
    // leaving for a model unmounts the catalogue and Back mounts a fresh one.
    const user = renderPage(
      <Routes>
        <Route
          path="/"
          element={
            <>
              <CataloguePage />
              <Search />
              <History />
            </>
          }
        />
        <Route
          path="/m/:slug"
          element={
            <>
              <p>Customizer</p>
              <History />
            </>
          }
        />
      </Routes>,
      { route: '/?view=list' },
    ).user
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    expect(rows()).toHaveLength(4)

    await user.click(screen.getByRole('link', { name: 'Crème Coaster' }))
    await screen.findByText('Customizer')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=list$/)
    expect(rows()).toHaveLength(4)
  })

  it('undoes and redoes a view toggle with back and forward', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(rows()).toHaveLength(4)

    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=cards$/)
    expect(rows()).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Cards' })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('button', { name: 'Forward' }))
    expect(screen.getByTestId('search')).toHaveTextContent(/^\?view=list$/)
    expect(rows()).toHaveLength(4)
  })

  it('keeps the filters and sort when the view changes', async () => {
    const { user } = renderCatalogue('/?tag=keychain&sort=name')
    await screen.findByRole('heading', { name: 'Keychain Template' })
    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(screen.getByTestId('search')).toHaveTextContent('?tag=keychain&sort=name&view=list')
    expect(names()).toEqual(['Keychain Template', 'Name Keychain'])
  })

  it('gives each row its name link, built-in badge, description, tags, time and Duplicate', async () => {
    renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    const coaster = rowOf('Crème Coaster')
    expect(within(coaster).getByRole('link', { name: 'Crème Coaster' })).toHaveAttribute(
      'href',
      `/m/${GALLERY_SLUG}`,
    )
    expect(within(coaster).getByText('A drinks coaster with a raised rim.')).toBeInTheDocument()
    expect(within(coaster).getByRole('button', { name: 'Filter by Tea & Coffee' })).toBeInTheDocument()
    expect(within(coaster).getByText(/^Updated /)).toBeInTheDocument()
    expect(within(coaster).getByRole('button', { name: /Duplicate/ })).toBeInTheDocument()
    expect(within(coaster).queryByTestId('builtin-badge')).not.toBeInTheDocument()

    expect(within(rowOf('Keychain Template')).getByTestId('builtin-badge')).toBeInTheDocument()
  })

  it('says on a row what it was duplicated from, linked by name, as a card does', async () => {
    await api.duplicateModel(BUILTIN_SLUG, 'My Keychain')
    renderCatalogue('/?view=list')

    await screen.findByRole('heading', { name: 'My Keychain' })
    const copy = rowOf('My Keychain')
    expect(within(copy).getByTestId('duplicated-from')).toHaveTextContent(
      'Duplicated from Keychain Template',
    )
    expect(within(copy).getByRole('link', { name: 'Keychain Template' })).toHaveAttribute(
      'href',
      `/m/${encodeURIComponent(BUILTIN_SLUG)}`,
    )
    expect(within(rowOf('Crème Coaster')).queryByTestId('duplicated-from')).not.toBeInTheDocument()
  })

  it('links a row back to where an imported model came from, and nothing else', async () => {
    const origin = 'https://raw.githubusercontent.com/someone/models/main/bin.scad'
    const list = models.map((model) =>
      model.slug === GALLERY_SLUG
        ? { ...model, origin_url: origin }
        : model.slug === 'gridfinity-bin'
          ? { ...model, origin_url: 'javascript:alert(1)' }
          : model,
    )
    server.use(http.get('/api/v1/models', () => HttpResponse.json(list)))
    renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    const link = within(rowOf('Crème Coaster')).getByRole('link', { name: 'raw.githubusercontent.com' })
    expect(link).toHaveAttribute('href', origin)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.closest('p')).toHaveTextContent('From raw.githubusercontent.com')
    expect(within(rowOf('Gridfinity Bin')).queryByText(/^From/)).not.toBeInTheDocument()
    // With no upstream either, the row leaves no empty line for the unlinked origin.
    expect(within(rowOf('Crème Coaster')).getByTestId('row-provenance')).toBeInTheDocument()
    expect(within(rowOf('Gridfinity Bin')).queryByTestId('row-provenance')).not.toBeInTheDocument()
  })

  it('adds a row tag to the filter', async () => {
    const { user } = renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    await user.click(
      within(rowOf('Crème Coaster')).getByRole('button', { name: 'Filter by Tea & Coffee' }),
    )
    expect(screen.getByTestId('search')).toHaveTextContent('?tag=Tea+%26+Coffee&view=list')
    expect(names()).toEqual(['Crème Coaster'])
  })

  it('badges the media count and opens the lightbox at the cover from the thumbnail', async () => {
    const { user } = renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    const coaster = rowOf('Crème Coaster')

    const thumbnail = within(coaster).getByRole('button', { name: 'View media of Crème Coaster (4)' })
    expect(within(thumbnail).getByTestId('media-count')).toHaveTextContent('4')
    await user.click(thumbnail)

    const dialog = await screen.findByRole('dialog', {}, { timeout: 3000 })
    const slides = toSlides(GALLERY_SLUG, media[GALLERY_SLUG]!)
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'src',
        slides[0]!.src,
      ),
    )
    expect(dialog).toHaveTextContent('Printed in blue and orange')
    // Opening the media is not a navigation.
    expect(screen.getByTestId('search')).toHaveTextContent('?view=list')
  })

  it('skips a video with no poster when picking the cover, as the backend does', async () => {
    const posterless = {
      id: 'e5f6a1b2c3d4',
      file: 'e5f6a1b2c3d4.mp4',
      kind: 'video' as const,
      caption: '',
      poster: null,
      missing: false,
      content_type: 'video/mp4',
      size: 24,
    }
    const list = models.map((model) =>
      model.slug === GALLERY_SLUG ? { ...model, media: [posterless, ...media[GALLERY_SLUG]!] } : model,
    )
    server.use(http.get('/api/v1/models', () => HttpResponse.json(list)))
    const { user } = renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Crème Coaster' })

    await user.click(
      within(rowOf('Crème Coaster')).getByRole('button', { name: 'View media of Crème Coaster (5)' }),
    )
    await screen.findByRole('dialog', {}, { timeout: 3000 })
    const slides = toSlides(GALLERY_SLUG, media[GALLERY_SLUG]!)
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'src',
        slides[0]!.src,
      ),
    )
  })

  it('opens a template whose only media is a video with no poster', async () => {
    const posterless = {
      id: 'e5f6a1b2c3d4',
      file: 'e5f6a1b2c3d4.mp4',
      kind: 'video' as const,
      caption: '',
      poster: null,
      missing: false,
      content_type: 'video/mp4',
      size: 24,
    }
    const list = models.map((model) =>
      model.slug === 'gridfinity-bin' ? { ...model, media: [posterless] } : model,
    )
    server.use(http.get('/api/v1/models', () => HttpResponse.json(list)))
    const { user } = renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })

    const thumbnail = within(rowOf('Gridfinity Bin')).getByRole('button', {
      name: 'View media of Gridfinity Bin',
    })
    expect(within(thumbnail).queryByTestId('media-count')).not.toBeInTheDocument()
    await user.click(thumbnail)
    await screen.findByRole('dialog', {}, { timeout: 3000 })
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current video source')).toHaveAttribute(
        'src',
        api.mediaUrl('gridfinity-bin', posterless),
      ),
    )
  })

  it('shows what it opens on when the media has no picture, never the output thumbnail', async () => {
    const posterless = {
      id: 'e5f6a1b2c3d4',
      file: 'e5f6a1b2c3d4.mp4',
      kind: 'video' as const,
      caption: '',
      poster: null,
      missing: false,
      content_type: 'video/mp4',
      size: 24,
    }
    // A thumbnail from an output, but media with nothing the backend would take as cover.
    const list = models.map((model) =>
      model.slug === 'gridfinity-bin'
        ? { ...model, has_thumbnail: true, thumbnail_source: 'output' as const, media: [posterless] }
        : model,
    )
    server.use(http.get('/api/v1/models', () => HttpResponse.json(list)))
    renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })

    const thumbnail = within(rowOf('Gridfinity Bin')).getByRole('button', {
      name: 'View media of Gridfinity Bin',
    })
    const bin = list.find((model) => model.slug === 'gridfinity-bin')!
    expect(thumbnail.querySelector(`img[src="${api.modelThumbnailUrl(bin)}"]`)).toBeNull()
    expect(thumbnail.querySelector('img')).toBeNull()
    // A neutral video tile, as a card shows, not the never-generated placeholder.
    expect(within(thumbnail).getByTestId('video-tile')).toBeInTheDocument()
    expect(within(thumbnail).queryByRole('img', { name: /not generated yet/ })).not.toBeInTheDocument()
    expect(thumbnail).toHaveAccessibleName('View media of Gridfinity Bin')
  })

  it('names the models list the same in both views', async () => {
    const { user } = renderCatalogue()
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    expect(within(screen.getByRole('list', { name: 'Models' })).getAllByRole('heading', { level: 2 })).toHaveLength(4)

    await user.click(screen.getByRole('button', { name: 'List' }))
    expect(rows()).toHaveLength(4)
    expect(within(screen.getByRole('list', { name: 'Models' })).getAllByRole('heading', { level: 2 })).toHaveLength(4)
  })

  it('names an uncaptioned image after the template in the lightbox, as a card does', async () => {
    const { user } = renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Name Keychain' })
    await user.click(
      within(rowOf('Name Keychain')).getByRole('button', { name: 'View media of Name Keychain' }),
    )
    await screen.findByRole('dialog', {}, { timeout: 3000 })
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'alt',
        'Name Keychain',
      ),
    )
  })

  it('gives a single cover no count badge', async () => {
    renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Name Keychain' })
    const thumbnail = within(rowOf('Name Keychain')).getByRole('button', {
      name: 'View media of Name Keychain',
    })
    expect(within(thumbnail).queryByTestId('media-count')).not.toBeInTheDocument()
  })

  it('shows a template with no media by its placeholder, with nothing to open', async () => {
    renderCatalogue('/?view=list')
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })
    const bin = rowOf('Gridfinity Bin')
    expect(within(bin).getByRole('img', { name: 'Gridfinity Bin — not generated yet' })).toBeInTheDocument()
    expect(within(bin).queryByRole('button', { name: /^View media/ })).not.toBeInTheDocument()

    // The built-in has no media either; its default-render preview stands in, unopenable.
    const builtin = rowOf('Keychain Template')
    expect(within(builtin).getByRole('img', { name: 'Keychain Template' })).toBeInTheDocument()
    expect(within(builtin).queryByRole('button', { name: /^View media/ })).not.toBeInTheDocument()
  })
})
