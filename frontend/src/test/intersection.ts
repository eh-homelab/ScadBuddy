import { act } from '@testing-library/react'

/**
 * jsdom lays nothing out, so no IntersectionObserver ever fires on its own. This one
 * records what it watches, and `intersect` says an element came into view.
 */
const observers = new Set<FakeIntersectionObserver>()

export class FakeIntersectionObserver {
  readonly root = null
  readonly rootMargin: string
  readonly thresholds = []
  private readonly targets = new Set<Element>()
  private readonly callback: IntersectionObserverCallback

  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.callback = callback
    this.rootMargin = options?.rootMargin ?? ''
  }

  observe(target: Element) {
    this.targets.add(target)
    observers.add(this)
  }
  unobserve(target: Element) {
    this.targets.delete(target)
  }
  disconnect() {
    this.targets.clear()
    observers.delete(this)
  }
  takeRecords() {
    return []
  }

  fire(within: Element) {
    const entries = [...this.targets]
      .filter((target) => within.contains(target))
      .map((target) => ({ target, isIntersecting: true }) as IntersectionObserverEntry)
    if (entries.length > 0) this.callback(entries, this as unknown as IntersectionObserver)
  }
}

/** Every watched element inside `within` (itself included) comes into view. */
export function intersect(within: Element) {
  act(() => {
    for (const observer of [...observers]) observer.fire(within)
  })
}
