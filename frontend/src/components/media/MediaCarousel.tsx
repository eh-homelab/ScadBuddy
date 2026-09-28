import useEmblaCarousel from 'embla-carousel-react'
import { useCallback, useEffect, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import { useReducedMotion } from '../../lib/useReducedMotion'
import { carouselOptions, type Slide } from './slides'

interface Props {
  slides: Slide[]
  /** A click, Enter or Space on the media itself; never a slide change. */
  onOpen?: (index: number) => void
  /** What to show with no slides: the template's placeholder. */
  fallback?: ReactNode
  className?: string
  /** What the carousel is of, for assistive technology: the template's name. */
  label: string
}

/**
 * A template's images and videos, one at a time (#275). Nothing in it plays: a video
 * is its poster with a play badge, and plays in the lightbox. Its controls keep their
 * clicks to themselves, so inside a card (#277) they never follow the card's link.
 */
export function MediaCarousel({ slides, onOpen, fallback, className, label }: Props) {
  if (slides.length === 0) return <>{fallback}</>
  if (slides.length === 1) {
    return (
      <div className={className}>
        <SlideMedia slide={slides[0]!} index={0} onOpen={onOpen} focusable eager />
      </div>
    )
  }
  return <Carousel slides={slides} onOpen={onOpen} className={className} label={label} />
}

/** A click that must not reach a link or a click handler the carousel sits in. */
function contained(event: MouseEvent) {
  event.preventDefault()
  event.stopPropagation()
}

function Carousel({ slides, onOpen, className, label }: Omit<Props, 'fallback'>) {
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
                focusable={position === index}
                eager={position === 0}
              />
            </div>
          ))}
        </div>
      </div>

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
 * none, the video ones with a play badge. A button when it opens the lightbox, and
 * then only the visible slide's is in the tab order.
 */
function SlideMedia({
  slide,
  index,
  onOpen,
  focusable,
  eager,
}: {
  slide: Slide
  index: number
  onOpen?: (index: number) => void
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
  if (!onOpen) return <span className={frame}>{picture}</span>
  return (
    <button
      type="button"
      aria-label={`Open ${slide.alt}`}
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
