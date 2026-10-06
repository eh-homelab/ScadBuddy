import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'
import { GALLERY_SLUG, media } from '../../mocks/fixtures'
import { MediaCarousel } from './MediaCarousel'
import { MediaLightbox } from './MediaLightbox'
import { toSlides } from './slides'

const slides = toSlides(GALLERY_SLUG, media[GALLERY_SLUG]!)

/** A carousel whose media opens the lightbox, as every surface wires them. */
function Gallery() {
  const [index, setIndex] = useState<number | null>(null)
  return (
    <>
      <button type="button">Before</button>
      <MediaCarousel slides={slides} onOpen={setIndex} label="Crème Coaster" />
      <MediaLightbox slides={slides} index={index} onClose={() => setIndex(null)} />
    </>
  )
}

async function opened() {
  return await screen.findByRole('dialog', {}, { timeout: 3000 })
}

describe('MediaLightbox (#275)', () => {
  it('renders nothing while closed', () => {
    const { container } = render(<MediaLightbox slides={slides} index={null} onClose={() => {}} />)
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens at the index of the media clicked', async () => {
    const user = userEvent.setup()
    render(<Gallery />)

    await user.click(screen.getByRole('button', { name: 'Next slide' }))
    await user.click(screen.getByRole('button', { name: `Open ${slides[1]!.alt}` }))

    const dialog = await opened()
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    await waitFor(() =>
      expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
        'src',
        slides[1]!.src,
      ),
    )
    expect(dialog).toHaveTextContent('The raised rim')
  })

  it('closes on Esc and gives focus back to the media that opened it', async () => {
    const user = userEvent.setup()
    render(<Gallery />)
    const trigger = screen.getByRole('button', { name: `Open ${slides[0]!.alt}` })

    await user.click(trigger)
    await opened()
    await user.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
      timeout: 3000,
    })
    expect(trigger).toHaveFocus()
  })

  // #1322 — Tab can carry focus out of the lightbox to <body>, where its own
  // keyboard listener (on its container) never hears a key.
  describe('once focus has left it for <body>', () => {
    async function openedThenBlurred() {
      const user = userEvent.setup()
      render(<Gallery />)
      await user.click(screen.getByRole('button', { name: `Open ${slides[0]!.alt}` }))
      await opened()
      ;(document.activeElement as HTMLElement | null)?.blur()
      expect(document.activeElement).toBe(document.body)
      return user
    }

    it('still closes on Esc', async () => {
      const user = await openedThenBlurred()
      await user.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), {
        timeout: 3000,
      })
    })

    it('still moves between slides with the arrow keys, and takes focus back', async () => {
      const user = await openedThenBlurred()
      await user.keyboard('{ArrowRight}')
      await waitFor(() =>
        expect(document.querySelector('.yarl__slide_current img')).toHaveAttribute(
          'src',
          slides[1]!.src,
        ),
      )
      expect(screen.getByRole('dialog').contains(document.activeElement)).toBe(true)
    })
  })

  it('plays a video only here, with native controls and without autoplay', async () => {
    const user = userEvent.setup()
    render(<Gallery />)
    const video = slides[3]!

    await user.click(screen.getByRole('button', { name: 'Go to slide 4' }))
    await user.click(screen.getByRole('button', { name: `Open ${video.alt}` }))
    await opened()

    const player = await waitFor(() => {
      const element = document.querySelector('.yarl__slide_current video')
      expect(element).not.toBeNull()
      return element as HTMLVideoElement
    })
    expect(player.controls).toBe(true)
    expect(player.autoplay).toBe(false)
    expect(player).toHaveAttribute('poster', video.poster)
    expect(player.querySelector('source')).toHaveAttribute('type', 'video/mp4')
  })
})
