import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

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

const LEAVE = 'scadbuddy:leave-fullscreen'

/**
 * Takes whatever is full screen out of it, for something outside it that is about to be
 * used — the assistant panel, which full screen hides along with the rest of the page.
 * Says whether anything was full screen.
 */
export function leaveFullscreen(): boolean {
  // An element that was full screen cancels the event on its way out.
  return !window.dispatchEvent(new Event(LEAVE, { cancelable: true }))
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
  // A call to the API still in flight. `native` only changes when the browser's event
  // arrives, so without this a second press in the meantime would ask again.
  const pending = useRef(false)

  // The browser also ends full screen on its own (Escape, the element leaving the
  // page), so the API's state is read back from its event rather than assumed.
  useEffect(() => {
    const sync = () => {
      const on = ref.current !== null && document.fullscreenElement === ref.current
      setNative(on)
      // Never both: a stand-in left under the API's full screen would come back when
      // that ends, instead of the page.
      if (on) setWindowed(false)
    }
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [ref])

  useEffect(() => {
    if (!windowed) return
    // On the window, which an event reaches last, so any Escape handled on its way —
    // the assistant panel's, or a dialog's (the font picker opens from the flyout) —
    // is seen as taken and leaves full screen alone.
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return
      event.preventDefault()
      setWindowed(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [windowed])

  useEffect(() => {
    if (!native && !windowed) return
    const leave = (event: Event) => {
      event.preventDefault()
      if (native) document.exitFullscreen().catch(() => undefined)
      setWindowed(false)
    }
    window.addEventListener(LEAVE, leave)
    return () => window.removeEventListener(LEAVE, leave)
  }, [native, windowed])

  const toggle = useCallback(() => {
    const element = ref.current
    if (!element || pending.current) return
    if (native) {
      pending.current = true
      document
        .exitFullscreen()
        .catch(() => undefined)
        .finally(() => {
          pending.current = false
        })
    } else if (windowed) {
      setWindowed(false)
    } else if (document.fullscreenEnabled && typeof element.requestFullscreen === 'function') {
      pending.current = true
      element
        .requestFullscreen()
        .catch(() => {
          // A refusal the flag did not predict still fills the window rather than
          // nothing.
          if (document.fullscreenElement !== element) setWindowed(true)
        })
        .finally(() => {
          pending.current = false
        })
    } else {
      setWindowed(true)
    }
  }, [ref, native, windowed])

  return { mode: native ? 'screen' : windowed ? 'window' : null, toggle }
}
