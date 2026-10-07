import { useCallback, useSyncExternalStore } from 'react'

/** #1741 — the `phone` variant in index.css: below Tailwind's `sm`. */
export const PHONE_QUERY = '(max-width: 639.98px)'

function list(query: string): MediaQueryList | undefined {
  return typeof window.matchMedia === 'function' ? window.matchMedia(query) : undefined
}

/** Whether `query` matches, following it as it changes; false where there is no matchMedia. */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const media = list(query)
      media?.addEventListener('change', onChange)
      return () => media?.removeEventListener('change', onChange)
    },
    [query],
  )
  return useSyncExternalStore(subscribe, () => list(query)?.matches ?? false)
}
