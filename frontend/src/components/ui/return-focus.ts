import { useRef } from "react"

type Handler = ((event: Event) => void) | undefined

/**
 * Focus back where it was when a dialog opened (#3672). Radix returns it only to a
 * Dialog.Trigger, and most dialogs here are opened by state (a menu item, a pick, a
 * Generate) with no trigger, so it fell to `body`. The opener is read as the content
 * takes the focus, each time it opens; the results are the content's two handlers.
 */
export function useReturnFocus(
  onOpenAutoFocus: Handler,
  onCloseAutoFocus: Handler
): { onOpenAutoFocus: (event: Event) => void; onCloseAutoFocus: (event: Event) => void } {
  const opener = useRef<Element | null>(null)
  return {
    onOpenAutoFocus: (event) => {
      opener.current = document.activeElement
      onOpenAutoFocus?.(event)
    },
    onCloseAutoFocus: (event) => {
      onCloseAutoFocus?.(event)
      const back = opener.current
      opener.current = null
      if (event.defaultPrevented) return
      if (back instanceof HTMLElement && back !== document.body && back.isConnected) {
        event.preventDefault()
        back.focus()
      }
    },
  }
}
