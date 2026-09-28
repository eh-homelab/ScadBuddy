import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
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

  it.each([
    ['opens its slides', vi.fn()],
    ['opens nothing', undefined],
  ])('leaves focus elsewhere alone when a carousel that %s mounts', (_, onOpen) => {
    const { container } = render(
      <>
        <button type="button">Elsewhere</button>
        <MediaCarousel slides={images} onOpen={onOpen} label="Crème Coaster" lazy />
      </>,
    )
    const elsewhere = screen.getByRole('button', { name: 'Elsewhere' })
    elsewhere.focus()

    intersect(container)

    expect(screen.getByRole('region', { name: 'Crème Coaster' })).toBeInTheDocument()
    expect(elsewhere).toHaveFocus()
  })
})
