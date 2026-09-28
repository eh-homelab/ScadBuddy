import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, delay, http } from 'msw'
import type { ReactNode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { GALLERY_SLUG, media, models } from '../mocks/fixtures'
import { server } from '../mocks/server'
import { toSlides } from '../components/media/slides'
import { renderPage } from '../test/utils'
import { CustomizePage } from './CustomizePage'

// WebGL does not exist in jsdom; the page's own buttons laid over the scene are kept.
vi.mock('../components/Preview', () => ({
  Preview: ({ leading, controls }: { leading?: ReactNode; controls?: ReactNode }) => (
    <div data-testid="preview">
      {leading}
      {controls}
    </div>
  ),
}))

const slides = toSlides(GALLERY_SLUG, media[GALLERY_SLUG]!)

function render(slug: string) {
  return renderPage(<CustomizePage />, { route: `/m/${slug}`, path: '/m/:slug' })
}

async function strip() {
  return await screen.findByRole('list', { name: 'Gallery' })
}

describe('template page gallery (#280)', () => {
  it('shows every image and the video under the preview, which stays the default view', async () => {
    render(GALLERY_SLUG)
    const list = await strip()

    const items = within(list).getAllByRole('button')
    expect(items.map((item) => item.getAttribute('aria-label'))).toEqual(
      slides.map((slide) => `Open ${slide.alt}`),
    )
    const pictures = [...list.querySelectorAll('img')]
    expect(pictures.map((picture) => picture.getAttribute('src'))).toEqual([
      slides[0]!.src,
      slides[1]!.src,
      slides[2]!.src,
      slides[3]!.poster,
    ])
    expect(within(list).getByTestId(`play-badge-${slides[3]!.key}`)).toBeInTheDocument()

    expect(await screen.findByTestId('preview')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Preview' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: /Gallery/ })).toHaveAttribute('aria-selected', 'false')
    expect(screen.queryByRole('region', { name: 'Crème Coaster' })).not.toBeInTheDocument()
  })

  it('opens the lightbox at the item clicked in the strip', async () => {
    const { user } = render(GALLERY_SLUG)
    const list = await strip()

    await user.click(within(list).getByRole('button', { name: `Open ${slides[1]!.alt}` }))

    const dialog = await screen.findByRole('dialog', {}, { timeout: 3000 })
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute('src', slides[1]!.src),
    )
    expect(dialog).toHaveTextContent('The raised rim')
  })

  it('has a Gallery tab whose carousel shows all the media and opens the lightbox, keeping the preview mounted', async () => {
    const { user } = render(GALLERY_SLUG)
    await strip()
    const preview = await screen.findByTestId('preview')

    await user.click(screen.getByRole('tab', { name: /Gallery/ }))
    expect(screen.getByRole('tab', { name: /Gallery/ })).toHaveAttribute('aria-selected', 'true')
    const carousel = screen.getByRole('region', { name: 'Crème Coaster' })
    expect(within(carousel).getAllByRole('group')).toHaveLength(4)
    // Hidden on narrow widths, not unmounted: the camera and the loaded model survive.
    expect(screen.getByTestId('preview')).toBe(preview)

    await user.click(within(carousel).getByRole('button', { name: `Open ${slides[0]!.alt}` }))
    const dialog = await screen.findByRole('dialog', {}, { timeout: 3000 })
    expect(dialog).toHaveTextContent('Printed in blue and orange')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(screen.getByRole('tab', { name: 'Preview' }))
    expect(screen.queryByRole('region', { name: 'Crème Coaster' })).not.toBeInTheDocument()
    expect(screen.getByTestId('preview')).toBe(preview)
  })

  it('does not remount the preview when the media arrives', async () => {
    const record = models.find((model) => model.slug === GALLERY_SLUG)!
    server.use(
      http.get('/api/v1/models/:slug', async () => {
        await delay(150)
        return HttpResponse.json(record)
      }),
    )
    render(GALLERY_SLUG)
    const preview = await screen.findByTestId('preview')
    expect(screen.queryByRole('list', { name: 'Gallery' })).not.toBeInTheDocument()

    await strip()

    expect(screen.getByTestId('preview')).toBe(preview)
  })

  it('shows no gallery at all for a template with no media', async () => {
    render('gridfinity-bin')
    await screen.findByTestId('preview')
    await screen.findByRole('heading', { name: 'Gridfinity Bin' })

    expect(screen.queryByRole('list', { name: 'Gallery' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tablist', { name: 'View' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Gallery/ })).not.toBeInTheDocument()
  })

  it('shows no gallery when every item has lost its file', async () => {
    const gone = models.find((model) => model.slug === GALLERY_SLUG)!
    server.use(
      http.get('/api/v1/models/:slug', () =>
        HttpResponse.json({
          ...gone,
          media: [{ ...media[GALLERY_SLUG]![3]!, missing: true, size: null }],
        }),
      ),
    )
    render(GALLERY_SLUG)
    await screen.findByRole('heading', { name: 'Crème Coaster' })
    await screen.findByTestId('preview')

    expect(screen.queryByRole('list', { name: 'Gallery' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Gallery/ })).not.toBeInTheDocument()
  })

  it('leaves the gallery out of full screen', async () => {
    const { user } = render(GALLERY_SLUG)
    await strip()

    await user.click(await screen.findByRole('button', { name: 'Full screen' }))

    expect(screen.queryByRole('list', { name: 'Gallery' })).not.toBeInTheDocument()
    expect(screen.queryByRole('tab', { name: /Gallery/ })).not.toBeInTheDocument()
    expect(screen.getByTestId('preview')).toBeInTheDocument()
  })
})
