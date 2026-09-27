import { useCallback, useEffect, useState, type RefObject } from 'react'

/**
 * `screen` is the browser's Fullscreen API. `window` is the stand-in where the page may
 * not use it: the element covers the whole window instead — inside Bambuddy, the frame.
 */
export type FullscreenMode = 'screen' | 'window'

export interface Fullscreen {
  /** How the element fills the screen now, or `null` while it sits in the page. */
  mode: FullscreenMode | null
  toggle: () => void
}

/**
 * Full screen for one element. A cross-origin iframe may only use the Fullscreen API
 * when its `<iframe>` allows it (`allow="fullscreen"` or `allowfullscreen`), and
 * Bambuddy's External Link frame is only known to set its sandbox flags (spec §1);
 * Safari on iPhone offers the API for video alone. Wherever the API is refused, the
 * element covers the window instead: the same toggle, and Escape leaves either — the
 * browser handles it for the API.
 */
export function useFullscreen(ref: RefObject<HTMLElement | null>): Fullscreen {
  const [native, setNative] = useState(false)
  const [windowed, setWindowed] = useState(false)

  // The browser also ends full screen on its own (Escape, the element leaving the
  // page), so the API's state is read back from its event rather than assumed.
  useEffect(() => {
    const sync = () => setNative(ref.current !== null && document.fullscreenElement === ref.current)
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [ref])

  useEffect(() => {
    if (!windowed) return
    const onKey = (event: KeyboardEvent) => {
      // A handler nearer the focus (the assistant panel's own Escape) goes first.
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      setWindowed(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [windowed])

  const toggle = useCallback(() => {
    const element = ref.current
    if (!element) return
    if (native) {
      document.exitFullscreen().catch(() => undefined)
    } else if (windowed) {
      setWindowed(false)
    } else if (document.fullscreenEnabled && typeof element.requestFullscreen === 'function') {
      // A refusal the flag did not predict still fills the window rather than nothing.
      element.requestFullscreen().catch(() => setWindowed(true))
    } else {
      setWindowed(true)
    }
  }, [ref, native, windowed])

  return { mode: native ? 'screen' : windowed ? 'window' : null, toggle }
}
