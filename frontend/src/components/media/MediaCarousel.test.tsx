import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { GALLERY_SLUG, media } from '../../mocks/fixtures'
import { intersect } from '../../test/intersection'
import { MediaCarousel } from './MediaCarousel'
import { toSlides, type Slide } from './slides'

const slides: Slide[] = toSlides(GALLERY_SLUG, media[GALLERY_SLUG]!)
const images = slides.slice(0, 3)

function setup(props: Partial<Parameters<typeof MediaCarousel>[0]> = {}) {
  const onOpen = vi.fn()
  const user = userEvent.setup()
  const view = render(
    <MediaCarousel slides={images} onOpen={onOpen} label="Crème Coaster" {...props} />,
  )
  return { user, onOpen, ...view }
}

/** The slide the carousel says it is on, from its visible "2 of 3" counter. */
function current() {
  return screen.getByTestId('carousel-position').textContent
}

describe('MediaCarousel (#275)', () => {
  it('renders the fallback, and no carousel, with no slides', () => {
    render(
      <MediaCarousel slides={[]} label="Empty" fallback={<p>No pictures yet</p>} />,
    )
    expect(screen.getByText('No pictures yet')).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('renders one slide with no controls', () => {
    const { onOpen } = setup({ slides: images.slice(0, 1) })
    expect(screen.getByRole('img', { name: images[0]!.alt })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /slide/i })).not.toBeInTheDocument()
    expect(screen.queryByTestId('carousel-position')).not.toBeInTheDocument()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('is a labelled carousel of labelled slides', () => {
    setup()
    const region = screen.getByRole('region', { name: 'Crème Coaster' })
    expect(region).toHaveAttribute('aria-roledescription', 'carousel')
    const groups = within(region).getAllByRole('group')
    expect(groups.map((group) => group.getAttribute('aria-label'))).toEqual([
      '1 of 3',
      '2 of 3',
      '3 of 3',
    ])
    expect(groups[0]).toHaveAttribute('aria-roledescription', 'slide')
  })

  it('changes slide with next, previous and the dots without opening anything', async () => {
    const { user, onOpen } = setup()
    expect(current()).toBe('1 of 3')
    expect(screen.getByRole('button', { name: 'Previous slide' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    expect(current()).toBe('2 of 3')
    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    expect(current()).toBe('3 of 3')
    expect(screen.getByRole('button', { name: 'Next slide' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Previous slide' }))
    expect(current()).toBe('2 of 3')
    await user.click(screen.getByRole('button', { name: 'Go to slide 1' }))
    expect(current()).toBe('1 of 3')
    expect(screen.getByRole('button', { name: 'Go to slide 1' })).toHaveAttribute(
      'aria-current',
      'true',
    )

    expect(onOpen).not.toHaveBeenCalled()
  })

  it('stays on a real slide when its slides shrink under it (#624)', async () => {
    const { user, rerender } = setup()
    await user.click(screen.getByRole('button', { name: 'Go to slide 3' }))
    expect(current()).toBe('3 of 3')

    rerender(<MediaCarousel slides={images.slice(0, 2)} onOpen={vi.fn()} label="Crème Coaster" />)

    expect(current()).toBe('2 of 2')
    expect(screen.getByRole('button', { name: 'Go to slide 2' })).toHaveAttribute('aria-current', 'true')
    expect(screen.getByRole('button', { name: 'Next slide' })).toBeDisabled()
    // The current slide's media is still the carousel's tab stop.
    expect(screen.getByRole('button', { name: `Open ${images[1]!.alt}` })).toHaveAttribute('tabindex', '0')
  })

  it('keeps a click on its controls from reaching a link around it', async () => {
    const outer = vi.fn()
    const user = userEvent.setup()
    render(
      <div onClick={outer}>
        <MediaCarousel slides={images} onOpen={vi.fn()} label="In a card" />
      </div>,
    )

    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    await user.click(screen.getByRole('button', { name: 'Go to slide 3' }))
    await user.click(screen.getByRole('button', { name: `Open ${images[2]!.alt}` }))

    expect(outer).not.toHaveBeenCalled()
  })

  it('opens the media clicked, at its index', async () => {
    const { user, onOpen } = setup()
    await user.click(screen.getByRole('button', { name: 'Next slide' }))

    await user.click(screen.getByRole('button', { name: `Open ${images[1]!.alt}` }))

    expect(onOpen).toHaveBeenCalledExactlyOnceWith(1)
  })

  it('opens the focused media with Enter or Space, but not an arrow with Enter', async () => {
    const { user, onOpen } = setup()

    screen.getByRole('button', { name: 'Next slide' }).focus()
    await user.keyboard('{Enter}')
    expect(current()).toBe('2 of 3')
    expect(onOpen).not.toHaveBeenCalled()

    screen.getByRole('button', { name: `Open ${images[1]!.alt}` }).focus()
    await user.keyboard('{Enter}')
    await user.keyboard(' ')
    expect(onOpen.mock.calls).toEqual([[1], [1]])
  })

  it('only the visible slide takes focus', () => {
    setup()
    const media = images.map((slide) => screen.getByRole('button', { name: `Open ${slide.alt}` }))
    expect(media.map((button) => button.tabIndex)).toEqual([0, -1, -1])
  })

  it('makes only the current dot a tab stop, and moves it with the arrow keys', async () => {
    const { user, onOpen } = setup()
    const dots = () =>
      [1, 2, 3].map((n) => screen.getByRole('button', { name: `Go to slide ${n}` }))
    expect(dots().map((dot) => dot.tabIndex)).toEqual([0, -1, -1])

    await user.click(screen.getByRole('button', { name: 'Go to slide 3' }))
    expect(dots().map((dot) => dot.tabIndex)).toEqual([-1, -1, 0])

    dots()[2]!.focus()
    await user.keyboard('{ArrowLeft}')
    expect(current()).toBe('2 of 3')
    expect(dots().map((dot) => dot.tabIndex)).toEqual([-1, 0, -1])
    expect(dots()[1]).toHaveFocus()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('moves with the arrow keys when focused', async () => {
    const { user, onOpen } = setup()
    screen.getByRole('region', { name: 'Crème Coaster' }).focus()

    await user.keyboard('{ArrowRight}')
    expect(current()).toBe('2 of 3')
    await user.keyboard('{ArrowRight}{ArrowRight}')
    expect(current()).toBe('3 of 3')
    await user.keyboard('{ArrowLeft}')
    expect(current()).toBe('2 of 3')
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('shows a video as its poster with a play badge, and never plays it', () => {
    const { container } = setup({ slides })
    const video = slides[3]!

    const poster = screen.getByRole('img', { name: video.alt })
    expect(poster).toHaveAttribute('src', video.poster)
    expect(screen.getByTestId(`play-badge-${video.key}`)).toBeInTheDocument()
    expect(container.querySelector('video')).toBeNull()
  })

  it('shows a video with no poster as a neutral tile with a play badge', () => {
    const bare: Slide = { ...slides[3]!, poster: undefined }
    const { container } = setup({ slides: [bare] })

    expect(screen.getByRole('img', { name: bare.alt })).not.toHaveAttribute('src')
    expect(screen.getByTestId(`play-badge-${bare.key}`)).toBeInTheDocument()
    expect(container.querySelector('video')).toBeNull()
  })

  it('loads only the first image eagerly', () => {
    setup()
    const loading = images.map((slide) =>
      screen.getByRole('img', { name: slide.alt }).getAttribute('loading'),
    )
    expect(loading).toEqual(['eager', 'lazy', 'lazy'])
  })

  it('is not interactive without onOpen', () => {
    render(<MediaCarousel slides={images.slice(0, 1)} label="Read-only" />)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByRole('img', { name: images[0]!.alt })).toBeInTheDocument()
  })
})

describe('MediaCarousel, lazy (#558)', () => {
  it('shows the first slide alone, with no carousel, until it nears the viewport', () => {
    const { container } = render(
      <MediaCarousel slides={images} onOpen={vi.fn()} label="Crème Coaster" lazy />,
    )

    const cover = screen.getByRole('img', { name: images[0]!.alt })
    expect(cover).toHaveAttribute('loading', 'eager')
    expect(screen.getByRole('button', { name: `Open ${images[0]!.alt}` })).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByTestId('carousel-position')).not.toBeInTheDocument()
    expect(container.querySelectorAll('img')).toHaveLength(1)
  })

  it('mounts the carousel once it nears the viewport, and keeps it', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <MediaCarousel slides={images} onOpen={vi.fn()} label="Crème Coaster" lazy />,
    )

    intersect(container)

    expect(screen.getByRole('region', { name: 'Crème Coaster' })).toBeInTheDocument()
    expect(current()).toBe('1 of 3')
    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    intersect(container)
    expect(current()).toBe('2 of 3')
  })

  it('moves focus on the cover to the carousel when it mounts', () => {
    const { container } = render(
      <MediaCarousel slides={images} onOpen={vi.fn()} label="Crème Coaster" lazy />,
    )
    screen.getByRole('button', { name: `Open ${images[0]!.alt}` }).focus()

    intersect(container)

    const first = screen.getByRole('button', { name: `Open ${images[0]!.alt}` })
    expect(screen.getByRole('region', { name: 'Crème Coaster' })).toContainElement(first)
    expect(first).toHaveFocus()
  })

  it('leaves focus elsewhere alone when the carousel mounts', () => {
    const { container } = render(
      <>
        <button type="button">Elsewhere</button>
        <MediaCarousel slides={images} onOpen={vi.fn()} label="Crème Coaster" lazy />
      </>,
    )
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()

    intersect(container)

    expect(screen.getByRole('region', { name: 'Crème Coaster' })).toBeInTheDocument()
    expect(elsewhere).toHaveFocus()
  })
})

describe('MediaCarousel linked to a template (catalogue cards)', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function setupLinked(props: Partial<Parameters<typeof MediaCarousel>[0]> = {}) {
    const onOpen = vi.fn()
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/']}>
        <Routes>
          <Route
            path="/"
            element={
              <MediaCarousel
                slides={images}
                onOpen={onOpen}
                to="/m/creme-coaster"
                label="Crème Coaster"
                {...props}
              />
            }
          />
          <Route path="/m/:slug" element={<p>Customizer</p>} />
        </Routes>
      </MemoryRouter>,
    )
    return { user, onOpen }
  }

  it('follows the link from a click on the picture, and opens nothing', async () => {
    const { user, onOpen } = setupLinked()
    await user.click(screen.getByRole('img', { name: images[0]!.alt }))
    expect(await screen.findByText('Customizer')).toBeInTheDocument()
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('keeps the picture an image, not a second link', () => {
    setupLinked()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Open / })).not.toBeInTheDocument()
  })

  it('opens the lightbox at the slide shown from the expand button', async () => {
    const { user, onOpen } = setupLinked()
    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    await user.click(screen.getByRole('button', { name: `View ${images[1]!.alt} full size` }))
    expect(onOpen).toHaveBeenCalledWith(1)
    expect(screen.queryByText('Customizer')).not.toBeInTheDocument()
  })

  it('has the expand button on a single slide too', async () => {
    const { user, onOpen } = setupLinked({ slides: images.slice(0, 1) })
    await user.click(screen.getByRole('button', { name: `View ${images[0]!.alt} full size` }))
    expect(onOpen).toHaveBeenCalledWith(0)
    expect(screen.queryByRole('button', { name: /slide/i })).not.toBeInTheDocument()
  })

  it('changes slide with the arrows without following the link', async () => {
    const { user } = setupLinked()
    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    expect(current()).toBe('2 of 3')
    expect(screen.queryByText('Customizer')).not.toBeInTheDocument()
  })

  it('opens the template in a new tab from a modified or middle click', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { user } = setupLinked()
    const picture = screen.getByRole('img', { name: images[0]!.alt })

    await user.keyboard('{Control>}')
    await user.click(picture)
    await user.keyboard('{/Control}')
    await user.pointer({ keys: '[MouseMiddle]', target: picture })

    expect(open).toHaveBeenCalledTimes(2)
    expect(open).toHaveBeenCalledWith('/m/creme-coaster', '_blank', 'noopener')
    expect(screen.queryByText('Customizer')).not.toBeInTheDocument()
  })

  it('has no expand button without onOpen', () => {
    setupLinked({ onOpen: undefined })
    expect(screen.queryByRole('button', { name: /full size$/ })).not.toBeInTheDocument()
  })

  it('keeps focus on the expand button when a lazy carousel mounts (#558)', () => {
    setupLinked({ lazy: true })
    const name = `View ${images[0]!.alt} full size`
    screen.getByRole('button', { name }).focus()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()

    intersect(document.body)

    const expand = screen.getByRole('button', { name })
    expect(screen.getByRole('region', { name: 'Crème Coaster' })).toContainElement(expand)
    expect(expand).toHaveFocus()
  })
})
