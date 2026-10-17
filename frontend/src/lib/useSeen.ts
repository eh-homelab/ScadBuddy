import { type RefObject, useEffect, useState } from 'react'

/**
 * #1014 — whether the element has come into view (within a little of it), once and for
 * good: for reading a row's data only when it is seen. Without an IntersectionObserver
 * every element counts as seen. Tested through `src/test/intersection.ts`'s fake.
 */
export function useSeen(element: RefObject<Element | null>): boolean {
  const [seen, setSeen] = useState(() => typeof IntersectionObserver === 'undefined')
  useEffect(() => {
    const target = element.current
    if (seen || !target) return
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) setSeen(true)
      },
      { rootMargin: '200px' },
    )
    observer.observe(target)
    return () => observer.disconnect()
  }, [element, seen])
  return seen
}
