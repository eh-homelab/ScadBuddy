import { useEffect, useState } from 'react'

/** Trailing-edge debounce. Parameter changes settle for `ms` before a render is submitted. */
export function useDebounced<T>(value: T, ms: number): T {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])

  return debounced
}
