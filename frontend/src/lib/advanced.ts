import { useState } from 'react'

/** #313 — remembered per viewer: whether a page shows its advanced detail. Storage that
 * throws (private mode, a sandboxed iframe) still toggles for this visit. */
export function useAdvanced(key: string): [boolean, () => void] {
  const [advanced, setAdvanced] = useState(() => {
    try {
      return window.localStorage.getItem(key) === '1'
    } catch {
      return false
    }
  })
  function toggle() {
    const next = !advanced
    setAdvanced(next)
    try {
      window.localStorage.setItem(key, next ? '1' : '0')
    } catch {
      // A browser that refuses storage still toggles for this visit.
    }
  }
  return [advanced, toggle]
}
