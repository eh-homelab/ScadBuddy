import { lazy, Suspense, useEffect, useRef } from 'react'
import type { Slide, SlideLink } from './slides'

// Only needed once something is opened, so its library loads then (#275).
const LightboxView = lazy(() => import('./LightboxView'))

interface Props {
  slides: Slide[]
  /** The slide to open at; null is closed. */
  index: number | null
  onClose: () => void
  /** A link in its toolbar, to where the media came from. */
  link?: SlideLink
}

/**
 * A template's media full size, over the page (#275): arrows, swipe, Esc, captions,
 * zoom on images, and videos with native controls. It fills the viewport -- inside
 * Bambuddy's iframe, the iframe -- and never asks for the Fullscreen API, which the
 * iframe does not grant. Focus goes back to whatever opened it once it closes.
 */
export function MediaLightbox({ slides, index, onClose, link }: Props) {
  const open = index !== null
  const opener = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (open) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
      return
    }
    // Closed (or never opened): hand focus back, then forget the opener.
    opener.current?.focus()
    opener.current = null
  }, [open])

  if (index === null) return null
  return (
    <Suspense fallback={null}>
      <LightboxView slides={slides} index={index} onClose={onClose} link={link} />
    </Suspense>
  )
}
