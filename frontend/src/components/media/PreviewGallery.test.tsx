import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { GALLERY_SLUG, media } from '../../mocks/fixtures'
import { PreviewGallery } from './PreviewGallery'

const items = media[GALLERY_SLUG]!
const OTHER = 'other-model'

function gallery(slug: string) {
  return (
    <PreviewGallery slug={slug} media={items} label="Crème Coaster" hidden={false}>
      <p>live preview</p>
    </PreviewGallery>
  )
}

function setup(slug = GALLERY_SLUG) {
  const user = userEvent.setup()
  const view = render(gallery(slug))
  return { user, ...view }
}

describe('PreviewGallery (#280, #624)', () => {
  it('fills the strip with thumbnails, never the full-size originals', () => {
    const { container } = setup()
    const strip = screen.getByRole('list', { name: 'Gallery' })
    const sources = [...strip.querySelectorAll('img')].map((img) => img.getAttribute('src'))
    expect(sources).toHaveLength(items.length)
    for (const src of sources) expect(src).toMatch(/\/thumbnail\?v=\d+$/)
    expect(container.querySelector(`img[src$="/media/${items[0]!.id}"]`)).toBeNull()
  })

  it('falls back to the original, or a video\'s poster, when a thumbnail fails (#1427)', () => {
    setup()
    const strip = screen.getByRole('list', { name: 'Gallery' })
    const imgs = () => [...strip.querySelectorAll('img')]
    const image = items.findIndex((item) => item.kind === 'image')
    const video = items.findIndex((item) => item.kind === 'video' && item.poster)

    fireEvent.error(imgs()[image]!)
    expect(imgs()[image]!.getAttribute('src')).toMatch(new RegExp(`/media/${items[image]!.id}$`))
    fireEvent.error(imgs()[video]!)
    expect(imgs()[video]!.getAttribute('src')).toMatch(new RegExp(`/media/${items[video]!.id}/poster$`))

    // The fallback failing too leaves an empty tile, not a broken image.
    const before = imgs().length
    fireEvent.error(imgs()[image]!)
    expect(imgs()).toHaveLength(before - 1)
  })

  it('points aria-controls only at a panel that is rendered', async () => {
    const { user } = setup()
    const preview = screen.getByRole('tab', { name: 'Preview' })
    const galleryTab = screen.getByRole('tab', { name: /Gallery/ })
    for (const tab of [preview, galleryTab]) {
      const controls = tab.getAttribute('aria-controls')
      if (controls) expect(document.getElementById(controls)).not.toBeNull()
    }
    expect(galleryTab).not.toHaveAttribute('aria-controls')

    await user.click(galleryTab)
    const controls = galleryTab.getAttribute('aria-controls')
    expect(controls).toBeTruthy()
    expect(document.getElementById(controls!)).toHaveAttribute('role', 'tabpanel')
  })

  it('moves between the tabs with the arrow keys, carrying focus', async () => {
    const { user } = setup()
    const preview = screen.getByRole('tab', { name: 'Preview' })
    const galleryTab = screen.getByRole('tab', { name: /Gallery/ })
    preview.focus()

    await user.keyboard('{ArrowRight}')
    expect(galleryTab).toHaveAttribute('aria-selected', 'true')
    expect(galleryTab).toHaveFocus()

    await user.keyboard('{ArrowLeft}')
    expect(preview).toHaveAttribute('aria-selected', 'true')
    expect(preview).toHaveFocus()
  })

  it('goes back to Preview when the slug changes', async () => {
    const { user, rerender } = setup()
    await user.click(screen.getByRole('tab', { name: /Gallery/ }))
    expect(screen.getByRole('tab', { name: /Gallery/ })).toHaveAttribute('aria-selected', 'true')

    rerender(gallery(OTHER))

    expect(screen.getByRole('tab', { name: 'Preview' })).toHaveAttribute('aria-selected', 'true')
  })

  it('closes the lightbox when the slug changes', async () => {
    const { user, rerender } = setup()
    const strip = screen.getByRole('list', { name: 'Gallery' })
    await user.click(strip.querySelector('button')!)
    expect(await screen.findByRole('dialog', {}, { timeout: 3000 })).toBeInTheDocument()

    rerender(gallery(OTHER))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
