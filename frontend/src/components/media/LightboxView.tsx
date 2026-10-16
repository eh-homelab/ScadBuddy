import { useEffect, useRef, type RefObject } from 'react'
import Lightbox, { type ControllerRef } from 'yet-another-react-lightbox'
import Captions from 'yet-another-react-lightbox/plugins/captions'
import Video from 'yet-another-react-lightbox/plugins/video'
import Zoom from 'yet-another-react-lightbox/plugins/zoom'
import 'yet-another-react-lightbox/plugins/captions.css'
import 'yet-another-react-lightbox/styles.css'
import { Link } from 'react-router'
import { isEmbedded } from '../../lib/embed'
import { toLightboxSlides, type Slide, type SlideLink } from './slides'

const PLUGINS = [Captions, Video, Zoom]

/** The keys the lightbox answers on its own container. */
const LIGHTBOX_KEYS = new Set(['Escape', 'ArrowLeft', 'ArrowRight'])

/**
 * #1322 — the library listens for keys on its container, and Tab can carry focus
 * past its last button to <body>, where Esc and the arrows are never heard. While
 * it is open, a key pressed outside it takes focus back in and is handed to the
 * container, so the library's own handling (finite ends, RTL, throttle) applies.
 */
function useKeysFromOutside(controller: RefObject<ControllerRef | null>) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!LIGHTBOX_KEYS.has(event.key) || event.defaultPrevented) return
      const root = document.querySelector('.yarl__root')
      if (!root || (event.target instanceof Node && root.contains(event.target))) return
      controller.current?.focus()
      const container = document.activeElement
      if (!container || !root.contains(container)) return
      event.preventDefault()
      container.dispatchEvent(
        new KeyboardEvent('keydown', { key: event.key, code: event.code, bubbles: true, cancelable: true }),
      )
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [controller])
}

/** The lightbox library itself, loaded with this chunk the first time media opens. */
export default function LightboxView({
  slides,
  index,
  onClose,
  link,
}: {
  slides: Slide[]
  index: number
  onClose: () => void
  link?: SlideLink
}) {
  const controller = useRef<ControllerRef>(null)
  useKeysFromOutside(controller)
  // A link in the toolbar goes on from the media without closing it first (the
  // catalogue's "Open template"); the plugins add their buttons before 'close'.
  const buttons = link
    ? [
        <Link
          key="link"
          to={link.to}
          className="yarl__button self-center rounded-[4px] px-3 text-[13px] font-medium"
        >
          {link.label}
        </Link>,
        'close',
      ]
    : ['close']
  return (
    <Lightbox
      open
      index={index}
      close={onClose}
      slides={toLightboxSlides(slides, isEmbedded())}
      plugins={PLUGINS}
      carousel={{ finite: true }}
      captions={{ descriptionTextAlign: 'center' }}
      controller={{ closeOnBackdropClick: true, ref: controller }}
      toolbar={{ buttons }}
    />
  )
}
