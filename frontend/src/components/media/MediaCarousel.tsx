import useEmblaCarousel from 'embla-carousel-react'
import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import { useHref, useNavigate } from 'react-router'
import { useReducedMotion } from '../../lib/useReducedMotion'
import { carouselOptions, type Slide } from './slides'

interface Props {
  slides: Slide[]
  /**
   * A click, Enter or Space on the media itself; never a slide change. With `to`, it
   * is the expand button's instead, since a click on the media follows the link.
   */
  onOpen?: (index: number) => void
  /**
   * Where a click on the media goes: a catalogue card's template, so the picture
   * opens what the rest of the card opens rather than a lightbox to close first.
   */
  to?: string
  /** What to show with no slides: the template's placeholder. */
  fallback?: ReactNode
  className?: string
  /** What the carousel is of, for assistive technology: the template's name. */
  label: string
  /**
   * Mount the carousel only once it nears the viewport, showing its first slide until
   * then (#558): a catalogue has one per card, and renders them all.
   */
  lazy?: boolean
}

/**
 * A template's images and videos, one at a time (#275). Nothing in it plays: a video
 * is its poster with a play badge, and plays in the lightbox. Its controls keep their
 * clicks to themselves, so inside a card (#277) they never follow the card's link.
 */
export function MediaCarousel({ slides, onOpen, to, fallback, className, label, lazy }: Props) {
  if (slides.length === 0) return <>{fallback}</>
  if (slides.length === 1) return <Cover slide={slides[0]!} onOpen={onOpen} to={to} className={className} />
  if (lazy) return <LazyCarousel slides={slides} onOpen={onOpen} to={to} className={className} label={label} />
  return <Carousel slides={slides} onOpen={onOpen} to={to} className={className} label={label} />
}

/** One slide on its own: a template with one picture, or a carousel's first until it mounts. */
function Cover({
  slide,
  onOpen,
  to,
  className,
}: {
  slide: Slide
  onOpen?: (index: number) => void
  to?: string
  className?: string
}) {
  return (
    <div className={`relative ${className ?? ''}`}>
      <SlideMedia slide={slide} index={0} onOpen={onOpen} to={to} focusable eager />
      {to && onOpen && <ExpandButton slide={slide} onClick={() => onOpen(0)} />}
    </div>
  )
}

/** How far outside the viewport a card starts to mount its carousel. */
const NEAR_VIEWPORT = '200px'

/**
 * The first slide until the card nears the viewport, then the carousel, which stays
 * mounted. The two are the same height, so nothing moves. Without IntersectionObserver
 * it mounts at once.
 */
function LazyCarousel(props: Omit<Props, 'fallback' | 'lazy'>) {
  const [near, setNear] = useState(() => typeof IntersectionObserver === 'undefined')
  const wrapper = useRef<HTMLDivElement>(null)
  // Focus on the cover (a Tab that scrolled it in) moves to the carousel's first slide.
  const refocus = useRef(false)

  useEffect(() => {
    const element = wrapper.current
    if (near || !element) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return
        refocus.current = element.contains(document.activeElement)
        setNear(true)
      },
      { rootMargin: NEAR_VIEWPORT },
    )
    observer.observe(element)
    return () => observer.disconnect()
  }, [near])

  useEffect(() => {
    if (!near || !refocus.current) return
    // The cover's own control in the carousel, named rather than found by render order:
    // the first slide's button, or with a link the expand button. A carousel with
    // neither is itself a tab stop.
    const carousel = wrapper.current?.querySelector<HTMLElement>('[aria-roledescription="carousel"]')
    const control = carousel?.querySelector<HTMLElement>('[data-slide-open="0"], [data-carousel-expand]')
    ;(control ?? carousel)?.focus()
  }, [near])

  return (
    <div ref={wrapper}>
      {near ? (
        <Carousel {...props} />
      ) : (
        <Cover slide={props.slides[0]!} onOpen={props.onOpen} to={props.to} className={props.className} />
      )}
    </div>
  )
}

/** A click that must not reach a link or a click handler the carousel sits in. */
function contained(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
}

function Carousel({ slides, onOpen, to, className, label }: Omit<Props, 'fallback'>) {
  const [viewportRef, embla] = useEmblaCarousel(carouselOptions(useReducedMotion()))
  // The carousel's own record of where it is, so the controls and labels never
  // depend on Embla having measured anything; a swipe moves it through `select`.
  const [index, setIndex] = useState(0)
  const count = slides.length

  useEffect(() => {
    if (!embla) return
    const onSelect = () => setIndex(embla.selectedScrollSnap())
    embla.on('select', onSelect)
    return () => {
      embla.off('select', onSelect)
    }
  }, [embla])

  const go = useCallback(
    (target: number) => {
      const next = Math.min(Math.max(target, 0), count - 1)
      setIndex(next)
      embla?.scrollTo(next)
    },
    [embla, count],
  )

  function onKeyDown(event: KeyboardEvent) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    event.stopPropagation()
    const target = Math.min(Math.max(index + (event.key === 'ArrowRight' ? 1 : -1), 0), count - 1)
    go(target)
    // Roving focus: from a dot, the arrow keys carry focus to the new current dot.
    if (event.target instanceof HTMLElement && event.target.dataset.carouselDot !== undefined) {
      const dots = event.currentTarget.querySelectorAll<HTMLElement>('[data-carousel-dot]')
      dots[target]?.focus()
    }
  }

  return (
    <section
      aria-roledescription="carousel"
      aria-label={label}
      tabIndex={0}
      onKeyDown={onKeyDown}
      className={`group relative rounded-[4px] outline-none focus-visible:ring-2 focus-visible:ring-accent ${className ?? ''}`}
    >
      <div ref={viewportRef} className="overflow-hidden rounded-[4px]">
        <div className="flex touch-pan-y">
          {slides.map((slide, position) => (
            <div
              key={slide.key}
              role="group"
              aria-roledescription="slide"
              aria-label={`${position + 1} of ${count}`}
              className="min-w-0 shrink-0 grow-0 basis-full"
            >
              <SlideMedia
                slide={slide}
                index={position}
                onOpen={onOpen}
                to={to}
                focusable={position === index}
                eager={position === 0}
              />
            </div>
          ))}
        </div>
      </div>

      {to && onOpen && <ExpandButton slide={slides[index]!} onClick={() => onOpen(index)} />}

      <ArrowButton
        direction="previous"
        disabled={index === 0}
        onClick={(event) => {
          contained(event)
          go(index - 1)
        }}
      />
      <ArrowButton
        direction="next"
        disabled={index === count - 1}
        onClick={(event) => {
          contained(event)
          go(index + 1)
        }}
      />

      <div className="pointer-events-none absolute inset-x-0 bottom-1.5 flex items-center justify-center gap-1.5">
        {slides.map((slide, position) => (
          <button
            key={slide.key}
            type="button"
            aria-label={`Go to slide ${position + 1}`}
            aria-current={position === index ? 'true' : undefined}
            // Only the current dot is a tab stop; the others are a click or an arrow key away.
            tabIndex={position === index ? 0 : -1}
            data-carousel-dot=""
            onClick={(event) => {
              contained(event)
              go(position)
            }}
            className={`pointer-events-auto h-1.5 w-1.5 rounded-full shadow ${
              position === index ? 'bg-white' : 'bg-white/50 hover:bg-white/80'
            }`}
          />
        ))}
      </div>
      <span
        data-testid="carousel-position"
        aria-live="polite"
        className="absolute right-1.5 top-1.5 rounded bg-black/55 px-1.5 py-0.5 text-[11px] text-white"
      >
        {index + 1} of {count}
      </span>
    </section>
  )
}

/** Opens the lightbox where a click on the media follows a link instead. */
function ExpandButton({ slide, onClick }: { slide: Slide; onClick: () => void }) {
  return (
    <button
      type="button"
      aria-label={`View ${slide.alt} full size`}
      data-carousel-expand=""
      onClick={(event) => {
        contained(event)
        onClick()
      }}
      className="absolute left-1.5 top-1.5 flex h-7 w-7 cursor-zoom-in items-center justify-center rounded-full bg-black/55 text-white opacity-80 hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
    >
      <svg viewBox="0 0 16 16" className="h-4 w-4" aria-hidden="true">
        <path
          d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  )
}

function ArrowButton({
  direction,
  disabled,
  onClick,
}: {
  direction: 'previous' | 'next'
  disabled: boolean
  onClick: (event: MouseEvent) => void
}) {
  const previous = direction === 'previous'
  return (
    <button
      type="button"
      aria-label={previous ? 'Previous slide' : 'Next slide'}
      disabled={disabled}
      onClick={onClick}
      className={`absolute top-1/2 -translate-y-1/2 ${previous ? 'left-1.5' : 'right-1.5'} flex h-7 w-7 items-center justify-center rounded-full bg-black/55 text-white opacity-80 hover:opacity-100 focus-visible:opacity-100 disabled:hidden`}
    >
      <svg viewBox="0 0 16 16" className="h-4 w-4" aria-hidden="true">
        <path
          d={previous ? 'M10 3 5 8l5 5' : 'M6 3l5 5-5 5'}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  )
}

/**
 * One slide's picture: an image, a video's poster, or a neutral tile for a video with
 * none, the video ones with a play badge. With somewhere to go, a click goes there;
 * otherwise a button when it opens
 * the lightbox, and then only the visible slide's is in the tab order.
 */
function SlideMedia({
  slide,
  index,
  onOpen,
  to,
  focusable,
  eager,
}: {
  slide: Slide
  index: number
  onOpen?: (index: number) => void
  to?: string
  focusable: boolean
  eager: boolean
}) {
  const src = slide.kind === 'video' ? slide.poster : slide.src
  const frame = 'relative block aspect-[4/3] w-full overflow-hidden rounded-[4px] bg-surface-2'
  const picture = (
    <>
      {src ? (
        <img
          src={src}
          alt={slide.alt}
          loading={eager ? 'eager' : 'lazy'}
          draggable={false}
          className="h-full w-full object-cover"
        />
      ) : (
        <span role="img" aria-label={slide.alt} className="block h-full w-full" />
      )}
      {slide.kind === 'video' && (
        <span
          data-testid={`play-badge-${slide.key}`}
          aria-hidden="true"
          className="absolute inset-0 m-auto flex h-10 w-10 items-center justify-center rounded-full bg-black/60 text-white"
        >
          <svg viewBox="0 0 16 16" className="ml-0.5 h-4 w-4">
            <path d="M4 2.5v11l9-5.5z" fill="currentColor" />
          </svg>
        </span>
      )}
    </>
  )
  if (to) return <LinkedPicture to={to} className={frame} picture={picture} />
  if (!onOpen) return <span className={frame}>{picture}</span>
  return (
    <button
      type="button"
      aria-label={`Open ${slide.alt}`}
      data-slide-open={index}
      tabIndex={focusable ? 0 : -1}
      onClick={(event) => {
        contained(event)
        onOpen(index)
      }}
      className={`${frame} cursor-zoom-in focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none`}
    >
      {picture}
    </button>
  )
}

/**
 * The picture where a click on it follows a link: a card's template. Not a link of its
 * own, since the card's title already is that link (and its keyboard route); the
 * expand button beside it opens the lightbox. A modified click (Ctrl, Cmd or Shift) or
 * a middle click opens it in a new tab, as it would on the title.
 */
function LinkedPicture({ to, className, picture }: { to: string; className: string; picture: ReactNode }) {
  const navigate = useNavigate()
  const href = useHref(to)
  const newTab = () => window.open(href, '_blank', 'noopener')
  return (
    <span
      data-media-link={href}
      onClick={(event) => {
        contained(event)
        if (event.metaKey || event.ctrlKey || event.shiftKey) newTab()
        else void navigate(to)
      }}
      onAuxClick={(event) => {
        if (event.button !== 1) return
        contained(event)
        newTab()
      }}
      className={`${className} cursor-pointer`}
    >
      {picture}
    </span>
  )
}
