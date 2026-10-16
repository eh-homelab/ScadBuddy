import { useId, useMemo, useState, type ReactNode } from 'react'
import { PlayIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'
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

  // The preview keeps its place in the tree whether or not there is media, so the
  // record arriving (or its media changing live) never remounts the viewer.
  const chrome = slides.length > 0 && !hidden
  const view: View = galleryOf === slug && chrome ? 'gallery' : 'preview'
  const select = (next: View) => setGalleryOf(next === 'gallery' ? slug : null)

  // Radix's ids are replaced by these: a tab points at its panel only while that panel
  // is rendered, and the gallery's is rendered only while it is selected (#624).
  const tab = (value: View, text: ReactNode) => (
    <TabsTrigger
      value={value}
      id={`${ids}-${value}-tab`}
      aria-controls={value === 'preview' || view === 'gallery' ? `${ids}-${value}` : undefined}
      className="flex-none px-2.5 text-xs"
    >
      {text}
    </TabsTrigger>
  )

  return (
    <Tabs
      value={view}
      onValueChange={(next) => select(next as View)}
      className="flex h-full min-h-0 min-w-0 flex-col gap-0"
    >
      {chrome && (
        <TabsList variant="line" aria-label="View" className="w-full shrink-0 justify-start border-b bg-background px-2 lg:hidden">
          {tab('preview', 'Preview')}
          {tab(
            'gallery',
            <>
              Gallery <span className="sb-num text-muted-foreground">{slides.length}</span>
            </>,
          )}
        </TabsList>
      )}
      {/* Mounted always and never hidden by Radix: only narrow screens hide it, and only
          for the gallery. Without the tabs it is no tab panel. */}
      <TabsContent
        value="preview"
        forceMount
        hidden={false}
        id={`${ids}-preview`}
        role={chrome ? 'tabpanel' : undefined}
        aria-labelledby={chrome ? `${ids}-preview-tab` : undefined}
        tabIndex={chrome ? 0 : undefined}
        className={cn('min-h-0 min-w-0 flex-1', view === 'gallery' && 'max-lg:hidden')}
      >
        {children}
      </TabsContent>
      {chrome && (
        <TabsContent
          value="gallery"
          id={`${ids}-gallery`}
          aria-labelledby={`${ids}-gallery-tab`}
          className="min-h-0 flex-1 overflow-y-auto bg-background p-3 lg:hidden"
        >
          <MediaCarousel slides={slides} onOpen={setOpen} label={label} className="mx-auto max-w-xl" />
        </TabsContent>
      )}
      {chrome && (
        <ul
          aria-label="Gallery"
          className="flex shrink-0 gap-1.5 overflow-x-auto border-t bg-background px-3 py-2 max-lg:hidden"
        >
          {slides.map((slide, index) => (
            <li key={slide.key} className="shrink-0">
              <Thumbnail slide={slide} onOpen={() => setOpen(index)} />
            </li>
          ))}
        </ul>
      )}
      <MediaLightbox slides={slides} index={open} onClose={() => setOpen(null)} />
    </Tabs>
  )
}

/** One item in the strip: a small copy of the image, or of a video's poster with a play badge. */
function Thumbnail({ slide, onOpen }: { slide: Slide; onOpen: () => void }) {
  // The small copy, never the original: eight 4 MB photos are 32 MB of strip (#624).
  // Should it fail, the original image or the video's poster, then an empty tile (#1427).
  // #1698 — per read of the media: `slides` is rebuilt only when it is read again, so a
  // URL that failed is tried once more then, never in a loop. Its key is not proven to
  // change with the bytes (api/models.py), so a failure may not be for good.
  const [failures, setFailures] = useState<{ slide: Slide; urls: string[] }>({ slide, urls: [] })
  const failed = failures.slide === slide ? failures.urls : []
  const fallback = slide.kind === 'video' ? slide.poster : slide.src
  const src = [slide.thumbnail, fallback].find((url) => url && !failed.includes(url))
  return (
    <button
      type="button"
      aria-label={`Open ${slide.alt}`}
      onClick={onOpen}
      className="relative block h-14 w-[4.5rem] cursor-zoom-in overflow-hidden rounded-md bg-ui-muted ring-1 ring-foreground/10 outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
    >
      {src ? (
        <img
          src={src}
          alt=""
          loading="lazy"
          draggable={false}
          onError={() =>
            setFailures((now) => ({ slide, urls: [...(now.slide === slide ? now.urls : []), src] }))
          }
          className="h-full w-full object-cover"
        />
      ) : null}
      {slide.kind === 'video' && (
        <Badge
          data-testid={`play-badge-${slide.key}`}
          aria-hidden="true"
          className="absolute inset-0 m-auto size-6 rounded-full bg-black/60 p-0 text-white"
        >
          <PlayIcon className="ml-0.5 fill-current" />
        </Badge>
      )}
    </button>
  )
}
