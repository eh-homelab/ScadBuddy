import { useLayoutEffect, useRef } from 'react'

/**
 * A ref that always holds the latest value — what a handler waiting on a debounce or a
 * render reads, since the closure it started in holds the old one.
 */
export function useLatest<T>(value: T) {
  const ref = useRef(value)
  useLayoutEffect(() => {
    ref.current = value
  })
  return ref
}
