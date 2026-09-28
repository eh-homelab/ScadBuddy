import type useEmblaCarousel from 'embla-carousel-react'
import type { Slide as LightboxSlide } from 'yet-another-react-lightbox'
import type {} from 'yet-another-react-lightbox/plugins/captions'
import type {} from 'yet-another-react-lightbox/plugins/video'
import { api } from '../../api/client'
import type { MediaView } from '../../api/types'

/** One image or video as the carousel and the lightbox show it (#275). */
export type Slide = {
  key: string
  kind: 'image' | 'video'
  src: string
  poster?: string
  alt: string
  caption?: string
  /** The file's MIME type: a video's `<source type>` needs it. */
  contentType?: string
}

/** A template's media as slides, in order. An item whose file is missing is skipped. */
export function toSlides(slug: string, media: MediaView[]): Slide[] {
  const shown = media.filter((item) => !item.missing)
  return shown.map((item, index) => {
    const caption = item.caption.trim() || undefined
    const kind = item.kind === 'video' ? 'Video' : 'Image'
    return {
      key: item.id,
      kind: item.kind,
      src: api.mediaUrl(slug, item),
      poster: api.mediaPosterUrl(slug, item),
      alt: caption ?? `${kind} ${index + 1} of ${shown.length}`,
      caption,
      contentType: item.content_type,
    }
  })
}

type EmblaOptions = NonNullable<Parameters<typeof useEmblaCarousel>[0]>

/** Embla's options. Under `prefers-reduced-motion` a slide change jumps (duration 0). */
export function carouselOptions(reducedMotion: boolean): EmblaOptions {
  return { loop: false, align: 'start', duration: reducedMotion ? 0 : 25 }
}

/**
 * The lightbox's slides. A video plays only here, with native controls and never on
 * its own. Inside Bambuddy's iframe, which does not grant fullscreen, the video's
 * fullscreen control is hidden rather than left to fail.
 */
export function toLightboxSlides(slides: Slide[], embedded: boolean): LightboxSlide[] {
  return slides.map((slide) =>
    slide.kind === 'video'
      ? {
          type: 'video',
          poster: slide.poster,
          sources: [{ src: slide.src, type: slide.contentType ?? 'video/mp4' }],
          description: slide.caption,
          controls: true,
          autoPlay: false,
          playsInline: true,
          controlsList: embedded ? 'nofullscreen' : undefined,
        }
      : { src: slide.src, alt: slide.alt, description: slide.caption },
  )
}
