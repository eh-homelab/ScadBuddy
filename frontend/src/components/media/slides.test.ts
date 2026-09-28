import { describe, expect, it } from 'vitest'
import type { MediaView } from '../../api/types'
import { BUILTIN_SLUG, media } from '../../mocks/fixtures'
import { carouselOptions, toLightboxSlides, toSlides } from './slides'

describe('toSlides (#275)', () => {
  it('turns media into slides, in order, with their URLs', () => {
    const slides = toSlides(BUILTIN_SLUG, media[BUILTIN_SLUG]!)

    expect(slides.map((slide) => slide.kind)).toEqual(['image', 'image', 'image', 'video'])
    expect(slides[0]).toEqual({
      key: 'a1b2c3d4e5f6',
      kind: 'image',
      src: '/api/v1/models/builtin%3Akeychain-template/media/a1b2c3d4e5f6',
      poster: undefined,
      alt: 'Printed in blue and orange',
      caption: 'Printed in blue and orange',
      contentType: 'image/png',
    })
    expect(slides[3]).toMatchObject({
      kind: 'video',
      poster: '/api/v1/models/builtin%3Akeychain-template/media/d4e5f6a1b2c3/poster',
      contentType: 'video/mp4',
    })
  })

  it('names an uncaptioned item by its place, and leaves its caption out', () => {
    const [, , third] = toSlides(BUILTIN_SLUG, media[BUILTIN_SLUG]!)
    expect(third).toMatchObject({ alt: 'Image 3 of 4', caption: undefined })
  })

  it('skips an item whose file is missing, counting only what is shown', () => {
    const items: MediaView[] = media[BUILTIN_SLUG]!.map((item, index) =>
      index === 0 ? { ...item, missing: true, size: null } : item,
    )
    const slides = toSlides(BUILTIN_SLUG, items)
    expect(slides.map((slide) => slide.key)).toEqual(['b2c3d4e5f6a1', 'c3d4e5f6a1b2', 'd4e5f6a1b2c3'])
    expect(slides[1]?.alt).toBe('Image 2 of 3')
  })
})

describe('carouselOptions', () => {
  it('jumps between slides under prefers-reduced-motion', () => {
    expect(carouselOptions(true).duration).toBe(0)
    expect(carouselOptions(false).duration).toBeGreaterThan(0)
  })
})

describe('toLightboxSlides', () => {
  const slides = toSlides(BUILTIN_SLUG, media[BUILTIN_SLUG]!)

  it('gives an image its caption and a video its poster, source and controls', () => {
    const [image, , , video] = toLightboxSlides(slides, false)
    expect(image).toEqual({
      src: slides[0]!.src,
      alt: 'Printed in blue and orange',
      description: 'Printed in blue and orange',
    })
    expect(video).toEqual({
      type: 'video',
      poster: slides[3]!.poster,
      sources: [{ src: slides[3]!.src, type: 'video/mp4' }],
      description: 'Printing on an H2C',
      controls: true,
      autoPlay: false,
      playsInline: true,
      controlsList: undefined,
    })
  })

  it('hides the fullscreen control inside the Bambuddy iframe, which does not grant it', () => {
    const video = toLightboxSlides(slides, true)[3]
    expect(video).toMatchObject({ controlsList: 'nofullscreen' })
  })
})
