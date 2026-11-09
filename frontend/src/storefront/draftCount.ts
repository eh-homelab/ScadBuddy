import { createContext, useContext } from 'react'
import type { Draft } from './api/client'

/**
 * The header's "Your order" count: a page that changes the draft hands the new one in. Off
 * the App (a page rendered alone in a test) it does nothing.
 */
export const DraftChanged = createContext<(draft: Draft | null) => void>(() => undefined)

export function useDraftChanged() {
  return useContext(DraftChanged)
}
