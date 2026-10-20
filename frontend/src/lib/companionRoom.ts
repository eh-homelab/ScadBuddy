import { useContext, useLayoutEffect, useState } from 'react'
import { ModalCompanionContext, roomFor } from './modal'

/**
 * #798 — the room to keep at the right edge for the assistant panel (the modal
 * companion), so it stays usable beside the drawer: null with no companion, 0 when there
 * is none to spare (a phone), where the drawer covers it.
 */
export function useCompanionRoom(): { companion: HTMLElement | null; room: number | null } {
  const companion = useContext(ModalCompanionContext)
  // Measured as the drawer first renders, so on a phone it mounts modal rather than
  // mounting beside and then remounting modal (Radix remounts the content when `modal` flips).
  const [room, setRoom] = useState<number | null>(() => (companion ? roomFor(companion) : null))
  useLayoutEffect(() => {
    if (!companion) return
    const measure = () => setRoom(roomFor(companion))
    measure()
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null
    observer?.observe(companion)
    window.addEventListener('resize', measure)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', measure)
      setRoom(null)
    }
  }, [companion])
  return { companion, room }
}
