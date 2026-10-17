import { act } from '@testing-library/react'

/**
 * jsdom lays nothing out, so no IntersectionObserver ever fires on its own. This one
 * records what it watches, and `intersect` says an element came into view. An element
 * stays in view: a target observed inside it later is told at once, as a browser tells
 * a new observer about a target already in view. Without that, an `intersect` that ran
 * before a row's effect had observed it (React runs effects after the commit a find*
 * already saw) was lost, and the row was never read (#3606, #3607).
 */
const observers = new Set<FakeIntersectionObserver>()
const inView = new Set<Element>()

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
    if ([...inView].some((element) => element.contains(target))) this.fire(target)
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
  inView.add(within)
  act(() => {
    for (const observer of [...observers]) observer.fire(within)
  })
}

/** Nothing is in view: each test starts with its elements out of view (vitest.setup.ts). */
export function resetIntersections() {
  inView.clear()
}
