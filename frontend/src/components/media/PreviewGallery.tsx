import { useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { MediaView } from '../../api/types'
import { MediaCarousel } from './MediaCarousel'
import { MediaLightbox } from './MediaLightbox'
import { toSlides, type Slide } from './slides'

interface Props {
  slug: string
  media: MediaView[] | undefined
  /** The template's name: what the carousel is of. */
  label: string
  /** Full screen is the view and its parameters, so the gallery stays out of it. */
  hidden: boolean
  /** The live preview, which stays the default view and is never unmounted. */
  children: ReactNode
}

type View = 'preview' | 'gallery'

/**
 * The template page's gallery beside its live preview (#280): a strip of thumbnails
 * under the preview on wide screens, and a Preview | Gallery tab on narrow ones. With
 * no media (or none whose file is still there) there is no gallery chrome at all.
 * Switching tabs only hides the preview, so the loaded model and the camera survive.
 */
export function PreviewGallery({ slug, media, label, hidden, children }: Props) {
  const slides = useMemo(() => toSlides(slug, media ?? []), [slug, media])
  // Held as the slug it was chosen on, so it never follows the user to another model.
  const [galleryOf, setGalleryOf] = useState<string | null>(null)
  // The lightbox's slide, held with its slug too, so Back with it open never leaves it
  // showing over the next model (#624).
  const [openOn, setOpenOn] = useState<{ slug: string; index: number } | null>(null)
  const open = openOn?.slug === slug ? openOn.index : null
  const setOpen = (index: number | null) => setOpenOn(index === null ? null : { slug, index })
  const ids = useId()
  const tabs = useRef<Array<HTMLButtonElement | null>>([])

  // The preview keeps its place in the tree whether or not there is media, so the
  // record arriving (or its media changing live) never remounts the viewer.
  const chrome = slides.length > 0 && !hidden
  const view: View = galleryOf === slug && chrome ? 'gallery' : 'preview'
  const select = (next: View) => setGalleryOf(next === 'gallery' ? slug : null)

  function onTabKey(event: KeyboardEvent) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    const next: View = view === 'preview' ? 'gallery' : 'preview'
    select(next)
    tabs.current[next === 'preview' ? 0 : 1]?.focus()
  }

  const tab = (value: View, index: number, text: ReactNode) => (
    <button
      ref={(element) => {
        tabs.current[index] = element
      }}
      type="button"
      role="tab"
      id={`${ids}-${value}-tab`}
      aria-selected={view === value}
      // The gallery panel is rendered only while it is selected (#624).
      aria-controls={value === 'preview' || view === 'gallery' ? `${ids}-${value}` : undefined}
      tabIndex={view === value ? 0 : -1}
      onClick={() => select(value)}
      className={`-mb-px border-b-2 px-2.5 py-1.5 text-[12px] transition-colors ${
        view === value ? 'border-accent text-ink' : 'border-transparent text-muted hover:text-ink'
      }`}
    >
      {text}
    </button>
  )

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col">
      {chrome && (
        <div
          role="tablist"
          aria-label="View"
          onKeyDown={onTabKey}
          className="flex shrink-0 gap-0.5 border-b border-line bg-surface px-2 lg:hidden"
        >
          {tab('preview', 0, 'Preview')}
          {tab('gallery', 1, <>Gallery <span className="sb-num text-faint">{slides.length}</span></>)}
        </div>
      )}
      <div
        id={`${ids}-preview`}
        role={chrome ? 'tabpanel' : undefined}
        aria-labelledby={chrome ? `${ids}-preview-tab` : undefined}
        className={`min-h-0 min-w-0 flex-1 ${view === 'gallery' ? 'max-lg:hidden' : ''}`}
      >
        {children}
      </div>
      {view === 'gallery' && (
        <div
          id={`${ids}-gallery`}
          role="tabpanel"
          aria-labelledby={`${ids}-gallery-tab`}
          className="min-h-0 flex-1 overflow-y-auto bg-bg p-3 lg:hidden"
        >
          <MediaCarousel slides={slides} onOpen={setOpen} label={label} className="mx-auto max-w-xl" />
        </div>
      )}
      {chrome && (
        <ul
          aria-label="Gallery"
          className="flex shrink-0 gap-1.5 overflow-x-auto border-t border-line bg-surface px-3 py-2 max-lg:hidden"
        >
          {slides.map((slide, index) => (
            <li key={slide.key} className="shrink-0">
              <Thumbnail slide={slide} onOpen={() => setOpen(index)} />
            </li>
          ))}
        </ul>
      )}
      <MediaLightbox slides={slides} index={open} onClose={() => setOpen(null)} />
    </div>
  )
}

/** One item in the strip: a small copy of the image, or of a video's poster with a play badge. */
function Thumbnail({ slide, onOpen }: { slide: Slide; onOpen: () => void }) {
  // The small copy, never the original: eight 4 MB photos are 32 MB of strip (#624).
  // Should it fail, the original image or the video's poster, then an empty tile (#1427).
  const [failed, setFailed] = useState<string[]>([])
  const fallback = slide.kind === 'video' ? slide.poster : slide.src
  const src = [slide.thumbnail, fallback].find((url) => url && !failed.includes(url))
  return (
    <button
      type="button"
      aria-label={`Open ${slide.alt}`}
      onClick={onOpen}
      className="relative block h-14 w-[4.5rem] cursor-zoom-in overflow-hidden rounded-[4px] bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
    >
      {src ? (
        <img
          src={src}
          alt=""
          loading="lazy"
          draggable={false}
          onError={() => setFailed((urls) => [...urls, src])}
          className="h-full w-full object-cover"
        />
      ) : null}
      {slide.kind === 'video' && (
        <span
          data-testid={`play-badge-${slide.key}`}
          aria-hidden="true"
          className="absolute inset-0 m-auto flex h-6 w-6 items-center justify-center rounded-full bg-black/60 text-white"
        >
          <svg viewBox="0 0 16 16" className="ml-0.5 h-3 w-3">
            <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
          </svg>
        </span>
      )}
    </button>
  )
}
